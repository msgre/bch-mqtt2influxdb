#!/usr/bin/env python3

import sys
import logging
import json
from datetime import datetime
import paho.mqtt.client
from paho.mqtt.client import topic_matches_sub
import influxdb
import jsonpath_ng


class Mqtt2InfluxDB:

    def __init__(self, config):

        self._points = config['points']

        self._influxdb = influxdb.InfluxDBClient(config['influxdb']['host'],
                                                 config['influxdb']['port'],
                                                 config['influxdb'].get('username', 'root'),
                                                 config['influxdb'].get('password', 'root'),
                                                 ssl=config['influxdb'].get('ssl', False))

        self._influxdb.create_database(config['influxdb']['database'])
        self._influxdb.switch_database(config['influxdb']['database'])

        for point in self._points:
            if 'database' in point:
                self._influxdb.create_database(point['database'])

        self._mqtt = paho.mqtt.client.Client()

        if config['mqtt'].get('username', None):
            self._mqtt.username_pw_set(config['mqtt']['username'],
                                       config['mqtt'].get('password', None))

        if config['mqtt'].get('cafile', None):
            self._mqtt.tls_set(config['mqtt']['cafile'],
                               config['mqtt'].get('certfile', None),
                               config['mqtt'].get('keyfile', None))

        self._mqtt.on_connect = self._on_mqtt_connect
        self._mqtt.on_disconnect = self._on_mqtt_disconnect
        self._mqtt.on_message = self._on_mqtt_message

        logging.info('MQTT broker host: %s, port: %d, use tls: %s',
                     config['mqtt']['host'],
                     config['mqtt']['port'],
                     bool(config['mqtt'].get('cafile', None)))

        self._mqtt.connect_async(config['mqtt']['host'], config['mqtt']['port'], keepalive=10)
        self._mqtt.loop_forever()

    def _on_mqtt_connect(self, client, userdata, flags, rc):
        logging.info('Connected to MQTT broker with code %s', rc)

        lut = {paho.mqtt.client.CONNACK_REFUSED_PROTOCOL_VERSION: 'incorrect protocol version',
               paho.mqtt.client.CONNACK_REFUSED_IDENTIFIER_REJECTED: 'invalid client identifier',
               paho.mqtt.client.CONNACK_REFUSED_SERVER_UNAVAILABLE: 'server unavailable',
               paho.mqtt.client.CONNACK_REFUSED_BAD_USERNAME_PASSWORD: 'bad username or password',
               paho.mqtt.client.CONNACK_REFUSED_NOT_AUTHORIZED: 'not authorised'}

        if rc != paho.mqtt.client.CONNACK_ACCEPTED:
            logging.error('Connection refused from reason: %s', lut.get(rc, 'unknown code'))

        if rc == paho.mqtt.client.CONNACK_ACCEPTED:
            for point in self._points:
                logging.info('subscribe %s', point['topic'])
                client.subscribe(point['topic'])

    def _on_mqtt_disconnect(self, client, userdata, rc):
        logging.info('Disconnect from MQTT broker with code %s', rc)

    def _on_mqtt_message(self, client, userdata, message):
        logging.debug('mqtt_on_message %s %s', message.topic, message.payload)

        msg = None

        for point in self._points:
            if topic_matches_sub(point['topic'], message.topic):
                if not msg:
                    payload = message.payload.decode('utf-8')

                    if payload == '':
                        payload = 'null'
                    try:
                        payload = json.loads(payload)
                    except Exception as e:
                        logging.error('parse json: %s topic: %s payload: %s', e, message.topic, message.payload)
                        return
                    msg = {
                        "topic": message.topic.split('/'),
                        "payload": payload,
                        "timestamp": message.timestamp,
                        "qos": message.qos
                    }

                measurement = self._get_value_from_str_or_JSONPath(point['measurement'], msg)
                if measurement is None:
                    logging.warning('unknown measurement')
                    return

                record = {'measurement': measurement,
                          'time': datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
                          'tags': {},
                          'fields': {}}

                if 'fields' in point:
                    for key in point['fields']:
                        val = self._get_value_from_str_or_JSONPath(jsonpath_ng.parse(point['fields'][key]), msg)
                        if val is None:
                            continue
                        record['fields'][key] = val

                if not record['fields']:
                    logging.warning('empty fields')
                    return

                if len(record['fields']) != len(point['fields']):
                    logging.warning('different number of fields')

                if 'tags' in point:
                    for key in point['tags']:
                        val = self._get_value_from_str_or_JSONPath(jsonpath_ng.parse(point['tags'][key]), msg)
                        if val is None:
                            continue
                        record['tags'][key] = val

                if len(record['tags']) != len(point['tags']):
                    logging.warning('different number of tags')

                logging.debug('influxdb write %s', record)

                self._influxdb.write_points([record], database=point.get('database', None))

    def _get_value_from_str_or_JSONPath(self, param, msg):
        if isinstance(param, str):
            return param

        elif isinstance(param, jsonpath_ng.JSONPath):
            tmp = param.find(msg)
            if tmp:
                return tmp[0].value

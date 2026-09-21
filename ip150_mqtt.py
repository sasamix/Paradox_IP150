import argparse
import json
import logging
import signal
import threading
from datetime import datetime, timezone
import time
import urllib.parse

import paho.mqtt.client as mqtt

import ip150


class IP150_MQTT_Error(Exception):
    pass


class IP150_MQTT:
    _status_map = {
        'areas_status': {
            'topic': 'ALARM_PUBLISH_TOPIC',
            'map': {
                'Disarmed': 'disarmed', 'Armed': 'armed_away',
                'Triggered': 'triggered', 'Armed_sleep': 'armed_night',
                'Armed_stay': 'armed_home', 'Entry_delay': 'pending',
                'Exit_delay': 'arming', 'Ready': 'disarmed'
            }
        },
        'zones_status': {
            'topic': 'ZONE_PUBLISH_TOPIC',
            'map': {
                'Closed': 'off', 'Open': 'on', 'In_alarm': 'on',
                'Closed_Trouble': 'off', 'Open_Trouble': 'on',
                'Closed_Memory': 'off', 'Open_Memory': 'on',
                'Bypass': 'off', 'Closed_Trouble2': 'off',
                'Open_Trouble2': 'on'
            }
        }
    }
    _alarm_action_map = {
        'DISARM': 'Disarm', 'ARM_AWAY': 'Arm',
        'ARM_NIGHT': 'Arm_sleep', 'ARM_HOME': 'Arm_stay'
    }

    def __init__(self, opt_file):
        with opt_file:
            self._cfg = json.load(opt_file)
        log_level = getattr(logging, str(self._cfg['LOG_LEVEL']).upper(), None)
        if not isinstance(log_level, int):
            log_level = logging.WARNING
        logging.basicConfig(level=log_level)
        self._will = (self._cfg['CTRL_PUBLISH_TOPIC'], 'Disconnected', 1, True)
        self._reconnect_lock = threading.Lock()
        self._stopping = False
        self._ip_connected = False
        self.ip = ip150.Paradox_IP150(self._cfg['IP150_ADDRESS'])
        self._mqtt_client = None
        self._diag_prefix = self._cfg['CTRL_PUBLISH_TOPIC'].rsplit('/', 1)[0] + '/diagnostic'
        self._reconnect_count = 0
        self._disconnect_started = None

    def _diag_publish(self, client, name, value):
        client.publish(self._diag_prefix + '/' + name, str(value), 1, True)

    def _diag_state(self, client, state, error=None):
        self._diag_publish(client, 'state', state)
        if error is not None:
            self._diag_publish(client, 'last_error', error)
        self._diag_publish(
            client, 'last_seen',
            datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds'))

    def on_paradox_new_state(self, state, client):
        self._diag_state(client, 'connected')
        for group, values in state.items():
            mapping = self._status_map.get(group)
            if not mapping:
                continue
            for number, state_name in values:
                value = mapping['map'].get(state_name)
                if value:
                    client.publish(self._cfg[mapping['topic']] + '/' + str(number), value, 1, True)

    def on_paradox_update_error(self, error, client):
        if self._stopping:
            return
        logging.warning('Lost connection to Paradox IP150: %s', error)
        self._ip_connected = False
        if self._disconnect_started is None:
            self._disconnect_started = time.monotonic()
        self._diag_state(client, 'reconnecting', error)
        client.publish(*self._will)
        self._start_ip150_reconnect(client)

    def _start_ip150_reconnect(self, client):
        if not self._stopping:
            threading.Thread(target=self._reconnect_ip150, args=(client,), daemon=True).start()

    def _reconnect_ip150(self, client):
        if not self._reconnect_lock.acquire(False):
            return
        try:
            delay = 5
            while not self._stopping:
                try:
                    try:
                        if self.ip.logged_in:
                            self.ip.logout()
                    except Exception as error:
                        logging.debug('Cleanup before reconnect failed: %s', error)
                    new_ip = ip150.Paradox_IP150(self._cfg['IP150_ADDRESS'])
                    new_ip.login(self._cfg['PANEL_CODE'], self._cfg['PANEL_PASSWORD'])
                    new_ip.get_updates(
                        on_update=self.on_paradox_new_state,
                        on_error=self.on_paradox_update_error,
                        userdata=client,
                        poll_interval=self._cfg['REFRESH_RATE'])
                    self.ip = new_ip
                    self._ip_connected = True
                    self._reconnect_count += 1
                    self._diag_publish(client, 'reconnects', self._reconnect_count)
                    if self._disconnect_started is not None:
                        outage = time.monotonic() - self._disconnect_started
                        self._diag_publish(client, 'last_outage_seconds', '{:.1f}'.format(outage))
                        self._disconnect_started = None
                    self._diag_state(client, 'connected')
                    client.publish(self._cfg['CTRL_PUBLISH_TOPIC'], 'Connected', 1, True)
                    logging.warning('Paradox IP150 connection restored.')
                    return
                except Exception as error:
                    self._ip_connected = False
                    if self._disconnect_started is None:
                        self._disconnect_started = time.monotonic()
                    self._diag_state(client, 'reconnecting', error)
                    logging.warning(
                        'Paradox IP150 reconnect failed: %s Retrying in %s seconds.',
                        error, delay)
                    if self._wait_or_stop(delay):
                        return
                    delay = min(delay * 2, 30)
        finally:
            self._reconnect_lock.release()

    def _wait_or_stop(self, delay):
        end = time.monotonic() + delay
        while not self._stopping and time.monotonic() < end:
            time.sleep(min(0.5, max(0, end - time.monotonic())))
        return self._stopping

    def on_mqtt_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code != 0:
            logging.error('MQTT broker rejected connection: %s', reason_code)
            return
        client.subscribe([
            (self._cfg['ALARM_SUBSCRIBE_TOPIC'] + '/+', 1),
            (self._cfg['CTRL_SUBSCRIBE_TOPIC'], 1)
        ])
        if self._ip_connected:
            client.publish(self._cfg['CTRL_PUBLISH_TOPIC'], 'Connected', 1, True)
        else:
            self._diag_state(client, 'reconnecting', 'IP150 is not connected')
            client.publish(*self._will)
            self._start_ip150_reconnect(client)

    def on_mqtt_disconnect(self, client, userdata, disconnect_flags, reason_code, properties=None):
        if not self._stopping and reason_code != 0:
            logging.warning('Lost connection to MQTT broker (%s); reconnecting.', reason_code)

    def on_mqtt_alarm_message(self, client, userdata, message):
        area = message.topic.rpartition('/')[2]
        if not area.isdigit():
            return
        try:
            payload = message.payload.decode()
        except UnicodeDecodeError:
            logging.warning('Ignoring non-UTF8 alarm command.')
            return
        action = self._alarm_action_map.get(payload)
        if not action:
            logging.warning('Ignoring unknown alarm command: %s', payload)
            return
        if not self._ip_connected:
            logging.warning('Ignoring alarm command for area %s while IP150 is disconnected.', area)
            return
        try:
            self.ip.set_area_action(area, action)
        except Exception as error:
            logging.warning('Alarm command failed: %s', error)
            self._ip_connected = False
            client.publish(*self._will)
            self._start_ip150_reconnect(client)

    def mqtt_ctrl_disconnect(self, client):
        self._stopping = True
        self._ip_connected = False
        try:
            self.ip.cancel_updates()
        except Exception:
            pass
        client.publish(*self._will)
        try:
            self.ip.logout()
        except Exception as error:
            logging.debug('IP150 logout failed during shutdown: %s', error)
        client.disconnect()

    def on_mqtt_ctrl_message(self, client, userdata, message):
        try:
            payload = message.payload.decode()
        except UnicodeDecodeError:
            logging.warning('Ignoring non-UTF8 control command.')
            return
        if payload == 'Disconnect':
            self.mqtt_ctrl_disconnect(client)

    def parse_mqtt_url(self):
        parsed = urllib.parse.urlsplit(self._cfg['MQTT_ADDRESS'])
        if parsed.scheme not in ('mqtt', 'mqtts'):
            raise IP150_MQTT_Error('MQTT_ADDRESS must use mqtt:// or mqtts://.')
        if not parsed.hostname:
            raise IP150_MQTT_Error('MQTT_ADDRESS does not contain a hostname.')
        return parsed, parsed.port or (1883 if parsed.scheme == 'mqtt' else 8883)

    def _handle_signal(self, signum, frame):
        logging.info('Received signal %s; shutting down.', signum)
        self._stopping = True
        client = self._mqtt_client
        if client is None:
            return
        try:
            self.ip.cancel_updates()
        except Exception:
            pass
        try:
            if self.ip.logged_in:
                self.ip.logout()
        except Exception as error:
            logging.debug('IP150 logout failed during signal shutdown: %s', error)
        try:
            client.publish(*self._will)
        except Exception:
            pass
        client.disconnect()

    def loop_forever(self):
        parsed, mqtt_port = self.parse_mqtt_url()
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self._mqtt_client = client
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)
        client.on_connect = self.on_mqtt_connect
        client.on_disconnect = self.on_mqtt_disconnect
        client.message_callback_add(self._cfg['ALARM_SUBSCRIBE_TOPIC'] + '/+', self.on_mqtt_alarm_message)
        client.message_callback_add(self._cfg['CTRL_SUBSCRIBE_TOPIC'], self.on_mqtt_ctrl_message)
        client.username_pw_set(self._cfg['MQTT_USERNAME'], self._cfg['MQTT_PASSWORD'])
        client.will_set(*self._will)
        client.reconnect_delay_set(min_delay=2, max_delay=30)
        if parsed.scheme == 'mqtts':
            client.tls_set()
        client.connect_async(parsed.hostname, mqtt_port)
        client.loop_forever(retry_first_connection=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='MQTT adapter for IP150 Alarms')
    parser.add_argument('config', type=argparse.FileType(), default='options.json', nargs='?')
    args = vars(parser.parse_args())
    IP150_MQTT(args['config']).loop_forever()

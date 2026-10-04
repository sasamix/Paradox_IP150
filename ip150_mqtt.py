import argparse
import json
import logging
import signal
import threading
import time
import urllib.parse

import paho.mqtt.client as mqtt

import ip150


class IP150_MQTT_Error(Exception):
    pass


class IP150_MQTT:
    # Names used by the classic IP150 web interface (tbl_troublename).
    # tbl_troubles contains indexes into this table on supported firmware.
    _trouble_names = [
        'Clean smoke detector, fire zone trouble, or zone communication failure',
        'Fire loop trouble',
        'Module missing or failed to communicate',
        'Module power failure (AC fail)',
        'Module reporting via phone line failed',
        'Module auxiliary power interrupted (aux limit)',
        'Module battery low or disconnected',
        'Module enclosure tampered',
        'Module ROM memory corrupted',
        'Printer module does not detect a printer',
        'System cannot detect a phone line (TLM)',
        'System communication with modules failed (combus)',
        'System power failure (AC fail)',
        'System reporting via phone failed',
        'System auxiliary power interrupted (aux limit)',
        'System battery low or disconnected',
        'System bell/siren disconnected',
        'System bell/siren power interrupted (bell limit)',
        'System ROM memory corrupted',
        'Loss of time and date',
        'Too many modules connected to the system',
        'Wireless zone low battery',
        'Wireless zone supervision loss',
        'Zone tampered',
        'System RF interference',
        'System reporting with receiver failed',
        'Module missing or wireless module supervision loss',
        'Zone antimask transparency trouble',
        'Zone antimask proximity trouble',
        'Zone antimask dirty lens trouble',
        'Module direct light trouble',
        'Module low bus voltage',
        'Module selftest failed',
        'IP150 lost communication with panel',
        'IP150 receiver supervision',
        'IP150 receiver unregistered',
        'IP150 receiver registration failed',
        'PCS module missing',
        'PCS module tamper',
        'PCS GPRS no service',
        'PCS receiver supervision',
        'PCS receiver unregistered',
        'PCS receiver registration failed',
        'System enclosure tampered'
    ]
    _status_map = {
        'areas_status': {
            'topic': 'ALARM_PUBLISH_TOPIC',
            'map': {
                'Disarmed': 'disarmed', 'Armed': 'armed_away',
                'Triggered': 'triggered', 'Armed_sleep': 'armed_night',
                'Armed_stay': 'armed_home', 'Entry_delay': 'pending',
                'Exit_delay': 'arming', 'Ready': 'disarmed',
                'Not_ready': 'disarmed'
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
        self._ever_ip_connected = False
        self._recovery_active = False
        self._initial_connect_active = False
        self.ip = ip150.Paradox_IP150(self._cfg['IP150_ADDRESS'])
        self._mqtt_client = None
        ctrl_topic = self._cfg['CTRL_PUBLISH_TOPIC'].strip('/').split('/')
        self._diag_prefix = (ctrl_topic[0] if ctrl_topic else 'paradox') + '/diagnostic'
        self._reconnect_count = 0
        self._disconnect_started = None
        self._diag_state_value = None
        self._discovery_published = False
        self._discovered_zones = set()

    def _diag_publish(self, client, name, value):
        client.publish(self._diag_prefix + '/' + name, str(value), 1, True)

    def _diag_state(self, client, state, error=None):
        if state != self._diag_state_value:
            self._diag_publish(client, 'state', state)
            self._diag_state_value = state
        if error is not None:
            error_text = str(error).strip()
            if error_text:
                self._diag_publish(client, 'last_error', error_text)

    def _publish_discovery(self, client):
        if self._discovery_published:
            return
        root = self._diag_prefix
        device = {
            'identifiers': ['paradox_ip150_mqtt'],
            'name': 'Paradox IP150',
            'manufacturer': 'Paradox',
            'model': 'IP150 MQTT Adapter'
        }
        entities = {
            'last_error': {
                'name': 'Last error',
                'state_topic': root + '/last_error',
                'entity_category': 'diagnostic',
                'icon': 'mdi:alert-circle-outline'
            },
            'reconnects': {
                'name': 'Reconnects',
                'state_topic': root + '/reconnects',
                'state_class': 'total_increasing',
                'entity_category': 'diagnostic',
                'icon': 'mdi:connection'
            },
            'panel_troubles': {
                'name': 'Panel troubles',
                'state_topic': root + '/panel_troubles',
                'entity_category': 'diagnostic',
                'icon': 'mdi:shield-alert-outline'
            },
            'last_outage_seconds': {
                'name': 'Last outage',
                'state_topic': root + '/last_outage_seconds',
                'unit_of_measurement': 's',
                'device_class': 'duration',
                'entity_category': 'diagnostic',
                'icon': 'mdi:timer-alert-outline'
            }
        }
        binary_entities = {
            'panel_trouble': {
                'name': 'Panel trouble',
                'state_topic': root + '/panel_trouble',
                'payload_on': 'ON',
                'payload_off': 'OFF',
                'device_class': 'problem',
                'entity_category': 'diagnostic'
            },
            'connection': {
                'state_topic': root + '/state',
                'payload_on': 'connected',
                'payload_off': 'reconnecting',
                'device_class': 'connectivity',
                'entity_category': 'diagnostic'
            }
        }
        for object_id, config in entities.items():
            payload = dict(config)
            payload['unique_id'] = 'paradox_ip150_' + object_id
            payload['device'] = device
            client.publish(
                'homeassistant/sensor/paradox_ip150/' + object_id + '/config',
                json.dumps(payload), 1, True)
        for object_id, config in binary_entities.items():
            payload = dict(config)
            payload['unique_id'] = 'paradox_ip150_' + object_id
            payload['device'] = device
            client.publish(
                'homeassistant/binary_sensor/paradox_ip150/' + object_id + '/config',
                json.dumps(payload), 1, True)
        # Discover alarm areas so they no longer need manual YAML.
        # Areas 1 and 2 are currently used by this installation; publishing
        # discovery for both is harmless when an area is unused.
        for area in (1, 2):
            payload = {
                'name': 'Paradox {}'.format(area),
                'unique_id': 'paradox_alarm_area_{}'.format(area),
                'state_topic': self._cfg['ALARM_PUBLISH_TOPIC'] + '/' + str(area),
                'command_topic': self._cfg['ALARM_SUBSCRIBE_TOPIC'] + '/' + str(area),
                'qos': 1,
                'availability_topic': self._cfg['CTRL_PUBLISH_TOPIC'],
                'payload_available': 'Connected',
                'payload_not_available': 'Disconnected',
                'payload_disarm': 'DISARM',
                'payload_arm_home': 'ARM_HOME',
                'payload_arm_away': 'ARM_AWAY',
                'payload_arm_night': 'ARM_NIGHT',
                'supported_features': ['arm_home', 'arm_away', 'arm_night'],
                'device': device
            }
            client.publish(
                'homeassistant/alarm_control_panel/paradox_ip150/area_{}/config'.format(area),
                json.dumps(payload), 1, True)

        # Clear retained generic zone discovery from older 1.5.8 test
        # builds. Configured zones are republished below from tbl_zone metadata.
        for zone_number in range(1, 33):
            client.publish(
                'homeassistant/binary_sensor/paradox_ip150/zone_{}/config'.format(
                    zone_number),
                '', 1, True)
        self._discovered_zones.clear()

        # Remove obsolete Last seen diagnostic entity and retained state.
        client.publish(
            'homeassistant/sensor/paradox_ip150/last_seen/config',
            '', 1, True)
        client.publish(root + '/last_seen', '', 1, True)
        # Remove the old sensor discovery config for Connection from 1.5.6.
        client.publish(
            'homeassistant/sensor/paradox_ip150/connection/config',
            '', 1, True)
        self._discovery_published = True

    def _publish_zone_discovery(self, client, number, name):
        if number in self._discovered_zones:
            return
        payload = {
            'name': name,
            'unique_id': 'paradox_zone_{}'.format(number),
            'state_topic': self._cfg['ZONE_PUBLISH_TOPIC'] + '/' + str(number),
            'payload_on': 'on',
            'payload_off': 'off',
            'qos': 1,
            'availability_topic': self._cfg['CTRL_PUBLISH_TOPIC'],
            'payload_available': 'Connected',
            'payload_not_available': 'Disconnected',
            'device': {
                'identifiers': ['paradox_ip150_mqtt'],
                'name': 'Paradox IP150',
                'manufacturer': 'Paradox',
                'model': 'IP150 MQTT Adapter'
            }
        }
        client.publish(
            'homeassistant/binary_sensor/paradox_ip150/zone_{}/config'.format(number),
            json.dumps(payload), 1, True)
        self._discovered_zones.add(number)


    def on_paradox_new_state(self, state, client):
        zone_meta = state.get('zones_meta')
        configured_zones = set()
        if zone_meta is not None:
            for number, name in zone_meta:
                configured_zones.add(number)
                self._publish_zone_discovery(client, number, name)

        troubles = state.get('troubles')
        if troubles is not None:
            labels = []
            for code in troubles:
                try:
                    index = int(code)
                except (TypeError, ValueError):
                    labels.append(str(code))
                    continue
                # Known IP150 pages use zero-based indexes into
                # tbl_troublename. Preserve unknown codes instead of hiding.
                if 0 <= index < len(self._trouble_names):
                    labels.append(self._trouble_names[index])
                else:
                    labels.append('Unknown trouble {}'.format(index))
            self._diag_publish(client, 'panel_trouble', 'ON' if labels else 'OFF')
            self._diag_publish(
                client, 'panel_troubles',
                '; '.join(labels) if labels else 'None')
            if labels:
                logging.warning(
                    'IP150 active panel troubles: codes=%r; %s',
                    troubles, '; '.join(labels))
            else:
                logging.debug('IP150 reports no active panel troubles.')

        for group, values in state.items():
            mapping = self._status_map.get(group)
            if not mapping:
                continue
            for number, state_name in values:
                if group == 'zones_status' and configured_zones and number not in configured_zones:
                    continue
                value = mapping['map'].get(state_name)
                if value:
                    client.publish(self._cfg[mapping['topic']] + '/' + str(number), value, 1, True)

    def on_paradox_update_error(self, error, client):
        if self._stopping or self._recovery_active:
            return
        self._recovery_active = True
        logging.warning('IP150 polling failed repeatedly: %s', error)
        # First try to rebuild the IP150 web session without exposing a
        # disconnect to Home Assistant. Only the normal reconnect worker will
        # publish "reconnecting" if this recovery attempt fails.
        threading.Thread(
            target=self._recover_ip150_session,
            args=(client, error),
            daemon=True).start()

    def _recover_ip150_session(self, client, original_error):
        # Recovery owns the reconnect lock for the whole grace period. HA
        # remains connected while we try to replace the broken web session.
        if not self._reconnect_lock.acquire(False):
            self._recovery_active = False
            return
        recovery_started = time.monotonic()
        last_error = original_error
        try:
            for attempt, delay_after_failure in enumerate((1, 2, 4, 0), start=1):
                if self._stopping:
                    return
                new_ip = None
                try:
                    try:
                        self.ip.logout(force_remote=True)
                    except Exception as cleanup_error:
                        logging.debug(
                            'Cleanup before session recovery failed: %s',
                            cleanup_error)
                    new_ip = ip150.Paradox_IP150(self._cfg['IP150_ADDRESS'])
                    new_ip.login(
                        self._cfg['PANEL_CODE'],
                        self._cfg['PANEL_PASSWORD'])
                    # Verify the new session synchronously before swapping it
                    # in and before starting its background poller.
                    current = new_ip.get_info(self._cfg['REFRESH_RATE'])
                    self.ip = new_ip
                    self._ip_connected = True
                    self._ever_ip_connected = True
                    self.on_paradox_new_state(current, client)
                    new_ip.get_updates(
                        on_update=self.on_paradox_new_state,
                        on_error=self.on_paradox_update_error,
                        userdata=client,
                        poll_interval=self._cfg['REFRESH_RATE'])
                    logging.warning(
                        'Paradox IP150 session recovered silently on attempt %s.',
                        attempt)
                    return
                except ip150.Paradox_IP150_Unsupported_Firmware_Error as recovery_error:
                    if new_ip is not None and new_ip is not self.ip:
                        try:
                            new_ip.logout()
                        except Exception:
                            pass
                    self._ip_connected = False
                    self._disconnect_started = recovery_started
                    self._diag_state(client, 'reconnecting', recovery_error)
                    client.publish(*self._will)
                    logging.error('%s', recovery_error)
                    return
                except Exception as recovery_error:
                    if new_ip is not None and new_ip is not self.ip:
                        try:
                            new_ip.logout()
                        except Exception as cleanup_error:
                            logging.debug(
                                'Failed to clean up recovery candidate: %s',
                                cleanup_error)
                    last_error = recovery_error
                    logging.warning(
                        'Silent IP150 session recovery attempt %s/4 failed: %s',
                        attempt, recovery_error)
                    if delay_after_failure and self._wait_or_stop(
                            delay_after_failure):
                        return

            # Only now expose an outage to Home Assistant. Measure it from
            # the first failed recovery attempt, not from this publication.
            self._ip_connected = False
            self._disconnect_started = recovery_started
            self._diag_state(client, 'reconnecting', last_error)
            client.publish(*self._will)
        finally:
            self._recovery_active = False
            self._reconnect_lock.release()

        if not self._stopping:
            self._start_ip150_reconnect(client)

    def _start_ip150_reconnect(self, client):
        if self._stopping:
            return
        if not self._ever_ip_connected:
            if self._initial_connect_active:
                return
            self._initial_connect_active = True
        threading.Thread(
            target=self._reconnect_ip150,
            args=(client,),
            daemon=True).start()

    def _reconnect_ip150(self, client):
        if not self._reconnect_lock.acquire(False):
            return
        try:
            delay = 5
            while not self._stopping:
                new_ip = None
                try:
                    try:
                        self.ip.logout(force_remote=True)
                    except Exception as error:
                        logging.debug('Cleanup before reconnect failed: %s', error)
                    new_ip = ip150.Paradox_IP150(self._cfg['IP150_ADDRESS'])
                    new_ip.login(self._cfg['PANEL_CODE'], self._cfg['PANEL_PASSWORD'])
                    current = new_ip.get_info(self._cfg['REFRESH_RATE'])
                    self.ip = new_ip
                    self.on_paradox_new_state(current, client)
                    new_ip.get_updates(
                        on_update=self.on_paradox_new_state,
                        on_error=self.on_paradox_update_error,
                        userdata=client,
                        poll_interval=self._cfg['REFRESH_RATE'])
                    self._ip_connected = True
                    first_connection = not self._ever_ip_connected
                    self._ever_ip_connected = True
                    if not first_connection:
                        self._reconnect_count += 1
                    if not first_connection:
                        self._diag_publish(client, 'reconnects', self._reconnect_count)
                    if not first_connection and self._disconnect_started is not None:
                        outage = time.monotonic() - self._disconnect_started
                        self._diag_publish(client, 'last_outage_seconds', '{:.1f}'.format(outage))
                        self._disconnect_started = None
                    self._diag_state(client, 'connected')
                    if first_connection:
                        # Diagnostics are per app run. Clear retained values
                        # left by the previous container before publishing
                        # fresh counters for this run.
                        self._diag_publish(client, 'last_error', '')
                        self._diag_publish(client, 'last_outage_seconds', 'None')
                        self._diag_publish(client, 'reconnects', 0)
                    client.publish(self._cfg['CTRL_PUBLISH_TOPIC'], 'Connected', 1, True)
                    if first_connection:
                        logging.info('Paradox IP150 initial connection established.')
                    else:
                        logging.warning('Paradox IP150 connection restored.')
                    return
                except ip150.Paradox_IP150_Unsupported_Firmware_Error as error:
                    if new_ip is not None and new_ip is not self.ip:
                        try:
                            new_ip.logout()
                        except Exception:
                            pass
                    self._ip_connected = False
                    if self._disconnect_started is None:
                        self._disconnect_started = time.monotonic()
                    self._diag_state(client, 'reconnecting', error)
                    client.publish(*self._will)
                    logging.error('%s Automatic reconnect stopped until the app is restarted.', error)
                    return
                except Exception as error:
                    if new_ip is not None and new_ip is not self.ip:
                        try:
                            new_ip.logout()
                        except Exception as cleanup_error:
                            logging.debug(
                                'Failed to clean up reconnect candidate: %s',
                                cleanup_error)
                    self._ip_connected = False
                    if self._ever_ip_connected:
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
            self._initial_connect_active = False
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
        self._publish_discovery(client)
        if self._ip_connected:
            self._diag_state(client, 'connected')
            client.publish(self._cfg['CTRL_PUBLISH_TOPIC'], 'Connected', 1, True)
        else:
            # A new process has not verified the IP150 session yet. Publish
            # OFF explicitly so HA also records a full HA reboot where the
            # previous process could not update MQTT during shutdown.
            self._diag_state(client, 'reconnecting')
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
            if self._disconnect_started is None:
                self._disconnect_started = time.monotonic()
            self._diag_state(client, 'reconnecting', error)
            client.publish(*self._will)
            self._start_ip150_reconnect(client)

    def mqtt_ctrl_disconnect(self, client):
        self._stopping = True
        self._ip_connected = False
        try:
            self.ip.cancel_updates()
        except Exception:
            pass
        self._diag_state(client, 'reconnecting')
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
            self.ip.logout()
        except Exception as error:
            logging.debug('IP150 logout failed during signal shutdown: %s', error)
        try:
            self._diag_state(client, 'reconnecting')
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

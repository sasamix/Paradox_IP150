import functools
import hashlib
import json
import logging
import re
import threading
import time

import requests
import urllib3
from bs4 import BeautifulSoup


urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class Paradox_IP150_Error(Exception):
    pass


class KeepAlive(threading.Thread):
    def __init__(self, ip150url, interval):
        super().__init__(daemon=True, name='ip150-keepalive')
        self.ip150url = ip150url.rstrip('/')
        self.interval = interval
        self.stopped = threading.Event()

    def _one_keepalive(self):
        try:
            response = requests.get(
                self.ip150url + '/keep_alive.html',
                params={'msgid': 1},
                verify=False,
                timeout=(max(1.0, self.interval / 2), max(2.0, self.interval)))
            response.raise_for_status()
        except requests.RequestException as error:
            logging.warning('Keepalive request failed: %s', error)

    def run(self):
        while not self.stopped.wait(self.interval):
            self._one_keepalive()

    def cancel(self):
        self.stopped.set()


class Paradox_IP150:
    _tables_map = {
        'zones_status': {
            'name': 'tbl_statuszone',
            'map': {
                0: 'Closed', 1: 'Open', 2: 'In_alarm',
                3: 'Closed_Trouble', 4: 'Open_Trouble',
                5: 'Closed_Memory', 6: 'Open_Memory', 7: 'Bypass',
                8: 'Closed_Trouble2', 9: 'Open_Trouble2'
            }
        },
        'areas_status': {
            'name': 'tbl_useraccess',
            'map': {
                0: 'Unset', 1: 'Disarmed', 2: 'Armed', 3: 'Triggered',
                4: 'Armed_sleep', 5: 'Armed_stay', 6: 'Entry_delay',
                7: 'Exit_delay', 8: 'Ready', 9: 'Not_ready', 10: 'Instant'
            }
        }
    }

    _areas_action_map = {
        'Disarm': 'd', 'Arm': 'r', 'Arm_sleep': 'p', 'Arm_stay': 's'
    }

    def __init__(self, ip150url):
        self.ip150url = ip150url.rstrip('/')
        self.logged_in = False
        self._keepalive = None
        self._updates = None
        self._stop_updates = threading.Event()

    @staticmethod
    def _logged_only(func):
        @functools.wraps(func)
        def wrapped(self, *args, **kwargs):
            if not self.logged_in:
                raise Paradox_IP150_Error('Not logged in; please use login() first.')
            return func(self, *args, **kwargs)
        return wrapped

    @staticmethod
    def _to_8bits(value):
        return ''.join(chr(ord(char) % 256) for char in value)

    @staticmethod
    def _paradox_rc4(data, key):
        state, j, output = list(range(256)), 0, []
        for i in range(len(key) - 1, -1, -1):
            j = (j + state[i] + ord(key[i])) % 256
            state[i], state[j] = state[j], state[i]
        i = j = 0
        for char in data:
            i %= 256
            j = (j + state[i]) % 256
            state[i], state[j] = state[j], state[i]
            output.append(ord(char) ^ state[(state[i] + state[j]) % 256])
            i += 1
        return ''.join('{0:02x}'.format(value) for value in output).upper()

    def _prep_cred(self, user, pwd, sess):
        pwd_8bits = self._to_8bits(pwd)
        pwd_md5 = hashlib.md5(pwd_8bits.encode('ascii')).hexdigest().upper()
        spass = pwd_md5 + sess
        return {
            'p': hashlib.md5(spass.encode('ascii')).hexdigest().upper(),
            'u': self._paradox_rc4(user, spass)
        }

    @staticmethod
    def _check_response(response, context):
        try:
            response.raise_for_status()
        except requests.RequestException as error:
            raise Paradox_IP150_Error(
                '{} HTTP request failed: {}'.format(context, error)) from error
        return response

    @staticmethod
    def _looks_like_login_page(text):
        return (
            "top.location.href='login_page.html';" in text
            or 'loginaff' in text
        )

    def login(self, user, pwd, keep_alive_interval=5.0):
        if self.logged_in:
            raise Paradox_IP150_Error('Already logged in; please use logout() first.')

        try:
            login_page = requests.get(
                self.ip150url + '/login_page.html',
                verify=False,
                timeout=(5, 10))
        except requests.RequestException as error:
            raise Paradox_IP150_Error(
                'Could not retrieve IP150 login page: {}'.format(error)) from error
        self._check_response(login_page, 'Login page')

        match = None
        # IP150 can briefly return an incomplete/stale login page while its
        # web server is recovering. Retry the page before treating this as a
        # persistent session/login problem.
        for attempt in range(1, 4):
            match = re.search(
                r'loginaff.{0,20}?([A-Za-z0-9]{16})',
                login_page.text,
                re.DOTALL)
            if match:
                break
            if attempt < 3:
                time.sleep(1)
                try:
                    login_page = requests.get(
                        self.ip150url + '/login_page.html',
                        verify=False,
                        timeout=(5, 10))
                    self._check_response(login_page, 'Login page')
                except requests.RequestException as error:
                    logging.warning(
                        'IP150 login page retry %s failed: %s',
                        attempt, error)

        if not match:
            # Do not dump the complete IP150 HTML into logs: it is noisy and
            # can contain user/site-specific data.
            raise Paradox_IP150_Error(
                'Unexpected IP150 login page after 3 attempts; another web session may be active or the firmware is unsupported.')
        sess = match.group(1)

        creds = self._prep_cred(user, pwd, sess)
        try:
            default_page = requests.get(
                self.ip150url + '/default.html',
                params=creds,
                verify=False,
                timeout=(5, 10))
        except requests.RequestException as error:
            raise Paradox_IP150_Error(
                'IP150 login request failed: {}'.format(error)) from error
        self._check_response(default_page, 'Login')

        if "top.location.href='login_page.html';" in default_page.text:
            raise Paradox_IP150_Error('Could not login, wrong credentials provided.')

        time.sleep(3)
        self.logged_in = True
        if keep_alive_interval:
            self._keepalive = KeepAlive(self.ip150url, keep_alive_interval)
            self._keepalive.start()
        logging.info('Successfully logged into the Paradox web interface.')

    @_logged_only
    def logout(self):
        self.cancel_updates(silent=True)
        if self._keepalive:
            self._keepalive.cancel()
            self._keepalive.join(timeout=10)
            self._keepalive = None
        try:
            response = requests.get(
                self.ip150url + '/logout.html',
                verify=False,
                timeout=(5, 10))
            self._check_response(response, 'Logout')
        finally:
            self.logged_in = False
        logging.info('Logged out from the Paradox web interface.')

    @staticmethod
    def _js2array(varname, script):
        if not script:
            raise Paradox_IP150_Error('IP150 status page contains no JavaScript data.')
        pattern = r'\b{}\s*=\s*new\s+Array\((.*?)\)\s*;'.format(re.escape(varname))
        match = re.search(pattern, script, re.DOTALL)
        if not match:
            raise Paradox_IP150_Error(
                'IP150 status page does not contain {}.'.format(varname))
        try:
            return json.loads('[{}]'.format(match.group(1)))
        except json.JSONDecodeError as error:
            raise Paradox_IP150_Error(
                'Could not parse {} from IP150 status page.'.format(varname)) from error

    def _retry_get(self, url, params=None, **kwargs):
        last_error = None
        for attempt in range(1, 6):
            try:
                response = requests.get(url, params=params, **kwargs)
                response.raise_for_status()
                return response
            except requests.RequestException as error:
                last_error = error
                remaining = 5 - attempt
                logging.warning(
                    'GET request failed (%s attempts left): %s',
                    remaining, error)
                if remaining:
                    time.sleep(0.5)
        raise Paradox_IP150_Error(
            'GET request failed after 5 attempts: {}'.format(last_error))

    @_logged_only
    def get_info(self, timeout):
        status_page = self._retry_get(
            self.ip150url + '/statuslive.html',
            verify=False,
            timeout=(3.0, max(5.0, timeout * 2)))

        if self._looks_like_login_page(status_page.text):
            self.logged_in = False
            raise Paradox_IP150_Error('IP150 session expired or was replaced by another login.')

        parsed = BeautifulSoup(status_page.text, 'html.parser')
        form = parsed.find('form', attrs={'name': 'statuslive'})
        if form is None:
            raise Paradox_IP150_Error('Could not retrieve IP150 status information.')

        scripts = [str(tag.string) for tag in parsed.find_all('script') if tag.string]
        script = '\n'.join(scripts)
        result = {}
        for table, definition in self._tables_map.items():
            values = self._js2array(definition['name'], script)
            mapped = []
            for index, value in enumerate(values, start=1):
                state = definition['map'].get(value)
                if state is None:
                    logging.warning(
                        'Unknown IP150 %s value %r at index %s.',
                        table, value, index)
                    state = 'Unknown_{}'.format(value)
                mapped.append((index, state))
            result[table] = mapped
        return result

    def _get_updates(self, on_update, on_error, on_success, userdata, interval):
        try:
            previous = {}
            while not self._stop_updates.wait(interval):
                current = self.get_info(interval)
                if on_success:
                    on_success(userdata)
                updated = {}
                for group, values in current.items():
                    if group not in previous:
                        updated[group] = values
                        continue
                    for cur, prev in zip(values, previous[group]):
                        if cur != prev:
                            updated.setdefault(group, []).append(cur)
                    if len(values) > len(previous[group]):
                        updated.setdefault(group, []).extend(values[len(previous[group]):])
                if updated:
                    on_update(updated, userdata)
                previous = current
        except Exception as error:
            if on_error and not self._stop_updates.is_set():
                on_error(error, userdata)
        finally:
            self._updates = None
            self._stop_updates.clear()

    @_logged_only
    def get_updates(self, on_update=None, on_error=None, on_success=None, userdata=None, poll_interval=1.0):
        if not on_update:
            raise Paradox_IP150_Error('The callable on_update must be provided.')
        if poll_interval <= 0:
            raise Paradox_IP150_Error('The polling interval must be greater than 0 seconds.')
        if self._updates and self._updates.is_alive():
            raise Paradox_IP150_Error('Status updates are already running.')
        self._stop_updates.clear()
        self._updates = threading.Thread(
            target=self._get_updates,
            args=(on_update, on_error, on_success, userdata, poll_interval),
            daemon=True,
            name='ip150-updates')
        self._updates.start()

    @_logged_only
    def cancel_updates(self, silent=False):
        if self._updates and self._updates.is_alive():
            thread = self._updates
            self._stop_updates.set()
            if thread is not threading.current_thread():
                thread.join(timeout=10)
            self._updates = None
        elif not silent:
            raise Paradox_IP150_Error(
                'Not currently getting updates. Use get_updates() first.')

    @_logged_only
    def set_area_action(self, area, action):
        try:
            area = int(area)
        except (TypeError, ValueError) as error:
            raise Paradox_IP150_Error('Invalid area provided.') from error
        area -= 1
        if area < 0:
            raise Paradox_IP150_Error('Invalid area provided.')
        if action not in self._areas_action_map:
            raise Paradox_IP150_Error(
                'Invalid action "{}". Valid actions are {}.'.format(
                    action, list(self._areas_action_map)))
        response = self._retry_get(
            self.ip150url + '/statuslive.html',
            params={
                'area': '{:02d}'.format(area),
                'value': self._areas_action_map[action]
            },
            verify=False,
            timeout=(2, 5))
        if self._looks_like_login_page(response.text):
            self.logged_in = False
            raise Paradox_IP150_Error('IP150 session expired while sending command.')

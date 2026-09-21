# Changelog

## 1.5.0

- Update requests, Beautiful Soup, and Paho MQTT for current Python 3.14 environments.
- Migrate MQTT callbacks to Paho MQTT 2.x Callback API v2.
- Remove obsolete transitive dependency pins so pip can resolve compatible urllib3/certifi/idna versions.

## 1.4.0

- Keep MQTT alive while the IP150 is unavailable at startup.
- Automatically reconnect to the IP150 after transient HTTP failures.
- Retry the initial MQTT connection and reconnect with bounded backoff.
- Avoid duplicate IP150 polling threads after MQTT reconnects.
- Reject alarm commands safely while the IP150 is disconnected and recover after command failures.
- Add MQTT TLS support for `mqtts://` addresses.
- Improve shutdown behavior and reconnect logging.
- Update the Home Assistant app build for current Supervisor/Python releases.

## 1.3

- Upstream baseline.

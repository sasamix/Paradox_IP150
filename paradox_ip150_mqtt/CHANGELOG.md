# Changelog

## 1.5.2

- Forward SIGTERM/SIGINT from the Home Assistant app container to the Python process.
- Stop MQTT polling and log out from IP150 during normal shutdown.
- Do not restart the adapter after an intentional clean exit.
- Keep the 20-second restart delay only for unexpected process failures.
- Make the restart delay interruptible so app stop/restart is responsive.

## 1.5.1

- Validate HTTP status codes and convert transport failures to concise IP150 errors.
- Detect expired/replaced IP150 sessions and trigger automatic recovery.
- Stop dumping full unexpected login HTML into logs.
- Harden JavaScript array parsing and handle missing/multiple script tags.
- Guard against duplicate polling threads and join polling cleanly on shutdown.
- Preserve newly added status entries and tolerate unknown IP150 state values.
- Normalize IP150 URLs and improve command/session error handling.

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

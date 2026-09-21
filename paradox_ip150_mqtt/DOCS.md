# Paradox IP150 MQTT Adapter

MQTT bridge for Paradox IP150 web interface V3.

## Reliability

Version 1.4.0 keeps the MQTT client running while the IP150 is temporarily unavailable and automatically retries the IP150 session with bounded backoff. The availability topic is published as `Disconnected` during recovery and `Connected` after recovery.

The app also retries the initial MQTT broker connection instead of exiting when the broker is temporarily unavailable.

## Configuration

Configure the IP150 address, panel code/password, MQTT broker address and credentials in the Home Assistant app configuration screen.

Use `mqtt://host:1883` for plain MQTT or `mqtts://host:8883` for MQTT over TLS.

The default topics are:

- alarm state: `paradox/alarm/state`
- alarm commands: `paradox/alarm/cmnd`
- zone state: `paradox/zone/state`
- availability/control state: `paradox/ctrl/state`
- control commands: `paradox/ctrl/cmnd`

## Notes

The IP150 web interface generally permits only a limited number of sessions. If another web session is active, the app can temporarily report `Disconnected`; it will continue retrying automatically.

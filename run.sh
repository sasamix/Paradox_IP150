#!/usr/bin/with-contenv bashio

set -u

child_pid=""

shutdown() {
    bashio::log.info "Stopping Paradox IP150 MQTT Adapter..."
    if [[ -n "${child_pid}" ]] && kill -0 "${child_pid}" 2>/dev/null; then
        kill -TERM "${child_pid}" 2>/dev/null || true
        wait "${child_pid}" 2>/dev/null || true
    fi
    exit 0
}

trap shutdown TERM INT

while true; do
    python3 /ip150_mqtt.py /data/options.json &
    child_pid=$!

    set +e
    wait "${child_pid}"
    exit_code=$?
    set -e
    child_pid=""

    if [[ "${exit_code}" -eq 0 ]]; then
        bashio::log.info "Paradox IP150 MQTT Adapter stopped normally."
        exit 0
    fi

    bashio::log.warning "Adapter exited with code ${exit_code}; restarting in 20 seconds."

    for _ in $(seq 1 20); do
        sleep 1 &
        child_pid=$!
        wait "${child_pid}" || true
        child_pid=""
    done
done

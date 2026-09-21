FROM ghcr.io/home-assistant/base:latest

ARG BUILD_VERSION
ARG BUILD_ARCH

LABEL \
    io.hass.version="$BUILD_VERSION" \
    io.hass.type="app" \
    io.hass.arch="$BUILD_ARCH"

ENV LANG=C.UTF-8

COPY run.sh ip150.py ip150_mqtt.py requirements.txt /

RUN apk add --no-cache python3 py3-pip \
    && pip3 install --no-cache-dir --break-system-packages -r /requirements.txt \
    && chmod a+x /run.sh

CMD ["/run.sh"]

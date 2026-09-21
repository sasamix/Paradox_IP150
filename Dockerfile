FROM ghcr.io/home-assistant/base:latest

ENV LANG C.UTF-8

# Copy data for app
COPY run.sh ip150.py ip150_mqtt.py requirements.txt /

RUN apk add --no-cache python3 py3-pip &&\
    pip3 install --no-cache-dir --break-system-packages -r /requirements.txt

RUN chmod a+x /run.sh

CMD [ "/run.sh" ]

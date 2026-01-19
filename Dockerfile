FROM python:3.11-alpine

ENV PYTHONUNBUFFERED=1
WORKDIR /app

RUN apk add --no-cache bash

COPY app/ /app/
COPY www/ /app/www/
COPY config.yaml /app/config.yaml
COPY run.sh /run.sh

RUN chmod +x /run.sh && \
    pip install --no-cache-dir websockets paho-mqtt

EXPOSE 8080
CMD ["/run.sh"]

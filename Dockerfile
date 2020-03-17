FROM python:3.8-slim

WORKDIR /app

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

COPY requirements.txt .

RUN pip install -r requirements.txt

COPY template.conf dfbugbot.conf

ENTRYPOINT supybot --allow-root dfbugbot.conf

FROM python:3.12-alpine

WORKDIR /app
COPY tgbot.py /app/tgbot.py

ENV PYTHONUNBUFFERED=1 \
    TG_STATE_FILE=/data/state.json

VOLUME ["/data"]

CMD ["python", "tgbot.py"]

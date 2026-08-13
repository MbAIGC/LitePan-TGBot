FROM python:3.12-alpine@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df

WORKDIR /app
RUN pip install --no-cache-dir pypinyin==0.54.0 \
    && adduser -D app \
    && mkdir -p /data && chown -R app:app /data
COPY tgbot.py /app/tgbot.py

ENV PYTHONUNBUFFERED=1 \
    TG_STATE_FILE=/data/state.json

VOLUME ["/data"]

USER app

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD ["python", "tgbot.py", "--health"]

CMD ["python", "tgbot.py"]

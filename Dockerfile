FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --system --uid 10001 bot \
    && mkdir /data \
    && chown bot /data

COPY queue_bot ./queue_bot
USER bot
CMD ["python", "-m", "queue_bot"]

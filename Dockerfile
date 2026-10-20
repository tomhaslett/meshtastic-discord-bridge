FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NODE_DB_PATH=/data/nodes.json

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY meshtastic_to_discord.py VERSION ./
RUN mkdir -p /data && chown 10001:10001 /data
USER 10001:10001
CMD ["python", "-u", "meshtastic_to_discord.py"]

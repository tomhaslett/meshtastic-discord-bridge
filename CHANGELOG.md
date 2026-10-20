# Changelog

`YYYY.MM.DD.N` — same day, incrementing minor. Newest first.

## 2026.10.20.1

- Decrypted Meshtastic protobuf (`ServiceEnvelope`) on `/e/` MQTT topics.
- Configurable channel key via `MQTT_PSK` (accepts `default`, `none`, `simpleN`, `base64:…`, or hex).
- Only `TEXT_MESSAGE_APP` packets are posted to Discord; `NODEINFO_APP` is consumed silently to learn node names; every other portnum (position, routing, telemetry, …) is dropped.
- Persistent node DB at `NODE_DB_PATH` (bind-mounted `nodes.json`), names shown as the Discord webhook `username` per message.
- Per-node avatars via `DISCORD_AVATAR_URL_TEMPLATE` (DiceBear initials by default); background colour matches the Meshtastic app's node colour (low 24 bits of the node number, i.e. CRC-32 of the public key on 2.8+).
- Structured startup logs; webhook URL is never logged.
- Dockerfile runs as uid `10001`; `docker compose` deployment.
- Date-stamped version in `VERSION`, echoed on startup and used as the Docker image tag via `APP_VERSION` in `.env`.

# Meshtastic MQTT → Discord webhook bridge

A small Dockerized Python service that subscribes to an MQTT topic and forwards each received message to a Discord channel through a **Discord webhook URL**. It does not use a Discord bot token or `discord.py`.

## Development summary

This project was developed from an initial bot-based approach:

1. The first version used `discord.py` and a bot token. It hit a `discord.Client` intents requirement, followed by `401 Unauthorized` during bot login.
2. The target was clarified: use Discord's **Copy Webhook URL** instead. A webhook URL is a complete credential and is used directly with an HTTP POST; it is not a bot token.
3. The bridge was simplified to `paho-mqtt` plus `requests`, with settings supplied by environment variables.
4. This packaged version adds Docker Compose, MQTT username/password support, structured logs, a bounded HTTP timeout, safe handling for non-JSON MQTT payloads, disabled Discord mentions, and avoids logging the secret webhook URL.

**Important troubleshooting note:** if Discord still returns `401` when using this version, the webhook URL is invalid, incomplete, revoked, or not the exact URL copied from Discord. A bot token cannot be used as a webhook URL. Do not paste the webhook URL into logs or public chats; regenerate it if exposed.

## Files

- `meshtastic_to_discord.py` — MQTT subscriber and Discord webhook sender.
- `Dockerfile` — container image.
- `compose.yaml` — deployment configuration.
- `.env.example` — environment-variable template.
- `requirements.txt` — Python dependencies.
- `VERSION` — date-stamp version, read at startup and logged.
- `CHANGELOG.md` — user-visible change log, newest first.

## Quick start

1. Copy `.env.example` to `.env`:

   ```sh
   cp .env.example .env
   ```

2. Edit `.env`:
   - `MQTT_BROKER`: broker hostname/IP reachable from the container.
   - `MQTT_PORT`: normally `1883` for unencrypted MQTT.
   - `MQTT_TOPIC`: topic filter that matches the Meshtastic messages on your broker.
   - `MQTT_USERNAME` / `MQTT_PASSWORD`: optional broker credentials.
   - `MQTT_PSK`: channel key used to decrypt protobuf (`/e/`) traffic. `default` is the public Meshtastic key; use `base64:...`/hex/`simpleN` for a private channel, or `none` to disable.
   - `DISCORD_WEBHOOK_URL`: the **full** URL copied from Discord's webhook settings.

3. Build and start:

   ```sh
   docker compose up -d --build
   ```

4. Follow logs:

   ```sh
   docker compose logs -f
   ```

5. Stop:

   ```sh
   docker compose down
   ```

Keep `.env` private. It contains the webhook credential; `.gitignore` it if you put this directory in Git.

## MQTT topic and payload notes

Meshtastic MQTT topic layouts depend on your configuration. If no root topic is set, the default is `msh/REGION`, and per-channel topics look like `msh/REGION/2/<format>/CHANNELNAME/USERID`:

- `/e/` — raw protobuf, encrypted with the channel PSK when encryption is enabled.
- `/c/` — cleartext protobuf (firmware < 2.3.0).
- `/json/` — JSON, **only if JSON output was enabled**. Note: JSON support has been removed from recent firmware, so `/json/` topics are usually absent.

The bridge handles all three: JSON payloads are parsed directly; `/e/` and `/c/` `ServiceEnvelope` protobufs are parsed and, when encrypted, decrypted with `MQTT_PSK`; anything else is forwarded as raw text. Only `TEXT_MESSAGE_APP` is posted to Discord; `NODEINFO_APP` is used to learn node names but not posted, and other portnums (position, routing, telemetry, etc.) are dropped.

Set `MQTT_TOPIC` to a filter that matches the topic(s) your broker actually publishes. For an encrypted channel, remember the trailing `#`, e.g. `msh/EU_868/2/e/BERKS/#`.

## Node names

When the bridge sees a `nodeinfo` packet (JSON, or protobuf on `/e/`/`/c/`), it records the node's long name (or short name) in `NODE_DB_PATH` (default `/data/nodes.json` in Docker). Messages are posted with that name as the Discord webhook **username** (per-message `username` override), so the sender appears as the message author rather than in the content. Nodes without a matching entry show their numeric id.

`compose.yaml` mounts a named volume at `/data`, so learned names survive container restarts. To reset the names, remove the volume (`docker compose down -v`) or edit the JSON file.

## Sender avatars

Each message is sent with a generated avatar via `DISCORD_AVATAR_URL_TEMPLATE` (default: DiceBear initials). `{seed}` is filled with the sender name and `{color}` with the same colour the Meshtastic app uses for that node — the low 24 bits of the node number (which, since firmware 2.8, is the CRC-32 of the node's public key), matching `nodeColorsFromNum` in the apps. Set the variable empty to disable avatars. Discord fetches the image itself; the container only builds the URL. Meshtastic does not transmit node images, so avatars are derived, not read from the mesh.

## Testing the webhook separately

Before debugging MQTT, test the webhook from a trusted shell. This sends a visible test message:

```sh
curl -i -H 'Content-Type: application/json' \
  -d '{"content":"Test message from Meshtastic bridge"}' \
  "$DISCORD_WEBHOOK_URL"
```

Expected success is usually HTTP `204 No Content` (or `200` if a response body is requested/configured). A `401` points to the webhook URL/credential, not the MQTT subscription. Ensure the environment variable contains the full webhook URL, not a bot token, channel URL, or invite URL.

Avoid printing the full webhook URL to terminal logs, screenshots, or support requests.

## Versioning

`YYYY.MM.DD.N` — date plus a same-day minor. Bump the date for a new day, bump `N` for a second/third release today (start at `.1`). Three places must agree, and none of them is authoritative on its own:

1. `VERSION` — read at startup by the container and logged on line 2 of `docker compose logs`.
2. `APP_VERSION` in `.env` — used as the Docker image tag: `meshtastic-to-discord:<APP_VERSION>`.
3. `CHANGELOG.md` — prepend a new section describing the change.

To bump the version:

```bash
# set NEW=YYYY.MM.DD.N manually (date +%Y.%m.%d for a new day, +1 on the last digit for same-day)
NEW=2026.10.20.2
echo "$NEW" > VERSION
sed -i "s/^APP_VERSION=.*/APP_VERSION=$NEW/" .env
# edit CHANGELOG.md: add a new top section with today's version
docker compose up -d --build                   # rebuilds and re-tags
```

If you forget to update `APP_VERSION`, the running code and log line still use `VERSION`; only the image tag goes stale. If you forget `VERSION`, both the log line and the image tag stay stale. The log line is the easy check that both files moved together.

## Security

- Treat `DISCORD_WEBHOOK_URL` like a password.
- Do not bake secrets into the Docker image.
- Use `.env` or a secrets manager and restrict file permissions (`chmod 600 .env`).
- `allowed_mentions` is disabled to prevent forwarded messages from triggering Discord mentions.

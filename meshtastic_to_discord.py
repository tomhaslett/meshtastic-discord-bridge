#!/usr/bin/env python3
"""Forward Meshtastic MQTT messages to a Discord webhook."""

import base64
import binascii
import hashlib
import json
import logging
import os
import signal
import threading
import urllib.parse

import paho.mqtt.client as mqtt
import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from meshtastic.protobuf import mesh_pb2, mqtt_pb2, portnums_pb2

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("meshtastic-to-discord")

MQTT_BROKER = os.getenv("MQTT_BROKER", "mqtt")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_TOPIC = os.getenv("MQTT_TOPIC", "msh/+/2/e/+/+" )
MQTT_USERNAME = os.getenv("MQTT_USERNAME")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
NODE_DB_PATH = os.getenv("NODE_DB_PATH", "nodes.json")
MQTT_PSK = os.getenv("MQTT_PSK", "default").strip()

try:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION")) as _fh:
        APP_VERSION = _fh.read().strip() or "dev"
except OSError:
    APP_VERSION = "dev"
# Per-message Discord avatar. {seed} and {color} are filled per node; set empty to disable.
DISCORD_AVATAR_URL_TEMPLATE = os.getenv(
    "DISCORD_AVATAR_URL_TEMPLATE",
    "https://api.dicebear.com/9.x/initials/png"
    "?seed={seed}&backgroundColor={color}&size=128&fontWeight=600",
).strip()
# Palette used to give each node a stable, distinct avatar background.
NODE_COLORS = (
    "5865F2", "EB459E", "57F287", "FEE75C", "ED4245",
    "3498DB", "9B59B6", "E67E22", "1ABC9C", "E91E63",
)

# The public/default Meshtastic channel key (AES-128); 1-byte PSKs expand against it.
DEFAULT_PSK = bytes.fromhex("d4f1bb3a20290759f0bcffabcf4e6901")
# Filled in by main() once MQTT_PSK is parsed; None disables decryption.
PSK_KEY = None

stop_event = threading.Event()
http = requests.Session()
node_db = {}
node_db_lock = threading.Lock()


def normalize_node_id(value) -> str | None:
    """Return a Meshtastic node id as lowercase !hex, or None if unavailable."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    if text.startswith("!"):
        return text
    try:
        return f"!{int(text) & 0xFFFFFFFF:08x}"
    except ValueError:
        return text


def load_node_db() -> None:
    try:
        with open(NODE_DB_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        log.info("No node database at %s yet", NODE_DB_PATH)
        return
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read node database %s: %s", NODE_DB_PATH, exc)
        return
    if not isinstance(data, dict):
        log.warning("Ignoring node database %s: expected a JSON object", NODE_DB_PATH)
        return
    with node_db_lock:
        for key, name in data.items():
            node_id = normalize_node_id(key)
            if node_id and isinstance(name, str) and name.strip():
                node_db[node_id] = name.strip()
        count = len(node_db)
    log.info("Loaded %d node names from %s", count, NODE_DB_PATH)


def save_node_db() -> None:
    directory = os.path.dirname(os.path.abspath(NODE_DB_PATH))
    try:
        os.makedirs(directory, exist_ok=True)
        with node_db_lock:
            snapshot = dict(node_db)
        tmp_path = f"{NODE_DB_PATH}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(snapshot, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_path, NODE_DB_PATH)
    except OSError as exc:
        log.error("Could not save node database %s: %s", NODE_DB_PATH, exc)


def expand_psk(value: str) -> bytes | None:
    """Expand a Meshtastic PSK string into an AES key, or None to disable.

    Accepts "default", "none", "simpleN", base64 ("base64:..." or raw), or hex.
    """
    if not value or value.lower() == "none":
        return None
    lowered = value.lower()
    if lowered == "default":
        raw = b"\x01"
    elif lowered.startswith("simple"):
        try:
            raw = bytes([int(lowered[6:]) & 0xFF])
        except ValueError:
            log.error("Invalid MQTT_PSK 'simple' value: %s", value)
            return None
    else:
        text = value[7:] if lowered.startswith("base64:") else value
        try:
            raw = base64.b64decode(text, validate=True)
        except (binascii.Error, ValueError):
            try:
                raw = bytes.fromhex(lowered[2:] if lowered.startswith("0x") else value)
            except ValueError:
                log.error(
                    "Could not parse MQTT_PSK; use default, none, simpleN, base64:..., or hex"
                )
                return None

    if len(raw) == 0:
        return None
    if len(raw) == 1:
        index = raw[0]
        if index == 0:
            return None
        key = bytearray(DEFAULT_PSK)
        key[-1] = (key[-1] + index - 1) & 0xFF
        return bytes(key)
    if len(raw) < 16:
        return raw.ljust(16, b"\x00")
    if len(raw) == 16:
        return raw
    if len(raw) < 32:
        return raw.ljust(32, b"\x00")
    return raw[:32]


def record_node(node_id, name) -> None:
    """Persist a learned node name."""
    node_id = normalize_node_id(node_id)
    if not node_id or not isinstance(name, str) or not name.strip():
        return
    name = name.strip()
    with node_db_lock:
        if node_db.get(node_id) == name:
            return
        node_db[node_id] = name
    save_node_db()
    log.info("Learned node name %s for %s", name, node_id)


def learn_node_info(data: dict) -> None:
    """Record node names from JSON Meshtastic nodeinfo packets."""
    payload = data.get("payload")
    if not isinstance(payload, dict):
        return
    msg_type = str(data.get("type") or "").lower()
    portnum = data.get("portnum")
    if msg_type not in ("nodeinfo", "node_info") and portnum not in (4, "NODEINFO_APP"):
        return
    record_node(payload.get("id") or data.get("from"), payload.get("longname") or payload.get("shortname"))


def resolve_sender(node_id, fallback: str) -> str:
    normalized = normalize_node_id(node_id)
    if normalized:
        with node_db_lock:
            name = node_db.get(normalized)
        if name:
            return name
    return fallback


def node_color(node_key) -> str:
    """Meshtastic app node colour.

    The node number is CRC-32 of the node's public key (2.8+), and the app
    derives the colour from its low 24 bits (r = bits 16-23, g = 8-15, b = 0-7).
    """
    normalized = normalize_node_id(node_key)
    if normalized and normalized.startswith("!"):
        try:
            number = int(normalized[1:], 16)
        except ValueError:
            pass
        else:
            return f"{number & 0xFFFFFF:06X}"
    # No numeric id available: fall back to a stable palette colour.
    digest = hashlib.sha256(str(node_key).encode("utf-8")).digest()
    return NODE_COLORS[digest[0] % len(NODE_COLORS)]


def build_avatar(sender, node_key) -> str | None:
    if not DISCORD_AVATAR_URL_TEMPLATE or not sender or sender == "Unknown":
        return None
    return DISCORD_AVATAR_URL_TEMPLATE.format(
        seed=urllib.parse.quote(str(sender)),
        color=node_color(node_key or sender),
    )


def render_content(text) -> str:
    return str(text)[:1500]


def decrypt_meshpacket(packet) -> bytes | None:
    """Decrypt a Meshtastic MeshPacket using the configured channel PSK."""
    if PSK_KEY is None:
        return None
    nonce = (
        packet.id.to_bytes(8, "little")
        + getattr(packet, "from").to_bytes(4, "little")
        + b"\x00" * 4
    )
    decryptor = Cipher(algorithms.AES(PSK_KEY), modes.CTR(nonce)).decryptor()
    return decryptor.update(packet.encrypted) + decryptor.finalize()


def format_protobuf(payload: bytes, topic: str) -> tuple[str, str] | None:
    """Decode a Meshtastic ServiceEnvelope protobuf, or None if it isn't one."""
    envelope = mqtt_pb2.ServiceEnvelope()
    try:
        envelope.ParseFromString(payload)
    except Exception:
        return None
    if not envelope.HasField("packet"):
        return None
    packet = envelope.packet
    if not (packet.id or getattr(packet, "from")):
        return None

    sender = resolve_sender(getattr(packet, "from"), str(getattr(packet, "from")))
    node_key = normalize_node_id(getattr(packet, "from")) or str(getattr(packet, "from"))

    if packet.HasField("decoded"):
        data = packet.decoded
    elif packet.encrypted:
        plaintext = decrypt_meshpacket(packet)
        if plaintext is None:
            return sender, node_key, render_content("(encrypted; set MQTT_PSK to decrypt)")
        data = mesh_pb2.Data()
        try:
            data.ParseFromString(plaintext)
        except Exception:
            return None
    else:
        return None

    if data.portnum == portnums_pb2.NODEINFO_APP:
        user = mesh_pb2.User()
        try:
            user.ParseFromString(data.payload)
        except Exception:
            return None
        record_node(user.id or getattr(packet, "from"), user.long_name or user.short_name)
        return None

    if data.portnum != portnums_pb2.TEXT_MESSAGE_APP:
        return None

    text = data.payload.decode("utf-8", errors="replace")
    return sender, node_key, render_content(text)


def format_message(payload: bytes, topic: str) -> tuple[str, str | None, str] | None:
    """Format a Meshtastic MQTT payload: JSON, protobuf, or raw fallback. None to drop."""
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        data = None

    if isinstance(data, dict):
        learn_node_info(data)
        sender = data.get("from") or data.get("sender") or data.get("fromId") or "Unknown"
        node_key = normalize_node_id(sender) or str(sender)
        sender = resolve_sender(sender, str(sender))
        text = data.get("text") or data.get("message")
        if text is None:
            # Some deployments publish a JSON envelope whose useful data is nested.
            for key in ("decoded", "payload"):
                nested = data.get(key)
                if isinstance(nested, dict):
                    text = nested.get("text") or nested.get("payload")
                    if text is not None:
                        break
        if isinstance(text, (dict, list)):
            text = json.dumps(text, ensure_ascii=False)
        if text is None:
            text = json.dumps(data, ensure_ascii=False)
        return sender, node_key, render_content(text)

    if data is not None:
        return "Unknown", None, render_content(json.dumps(data, ensure_ascii=False))

    return format_protobuf(payload, topic)


def on_connect(client, userdata, flags, reason_code, properties=None):
    # paho-mqtt v2 passes a ReasonCode object; v1 passes an integer.
    code = int(getattr(reason_code, "value", reason_code))
    if code == 0:
        log.info("Connected to MQTT broker; subscribing to %s", MQTT_TOPIC)
        client.subscribe(MQTT_TOPIC)
    else:
        log.error("MQTT connection failed: %s", reason_code)


def post_to_discord(content: str, sender: str | None, node_key: str | None) -> None:
    body = {"content": content, "allowed_mentions": {"parse": []}}
    username = "".join(ch for ch in str(sender) if ch.isprintable()).strip()[:80] if sender else ""
    if username and username != "Unknown":
        body["username"] = username
    avatar = build_avatar(sender, node_key)
    if avatar:
        body["avatar_url"] = avatar
    try:
        response = http.post(DISCORD_WEBHOOK_URL, json=body, timeout=15)
        if response.status_code not in (200, 204) and (
            "username" in body or "avatar_url" in body
        ):
            # Discord can reject some override names/avatars; fall back to defaults.
            log.warning("Discord rejected username/avatar (HTTP %s); retrying without them",
                        response.status_code)
            body.pop("username", None)
            body.pop("avatar_url", None)
            response = http.post(DISCORD_WEBHOOK_URL, json=body, timeout=15)
        if response.status_code in (200, 204):
            log.info("Forwarded MQTT message")
        else:
            # Never log the webhook URL: it contains the secret token.
            log.error("Discord webhook returned HTTP %s: %s",
                      response.status_code, response.text[:500])
    except requests.RequestException as exc:
        log.error("Could not post to Discord: %s", exc)


def on_message(client, userdata, msg):
    formatted = format_message(msg.payload, msg.topic)
    if formatted is None:
        return
    sender, node_key, content = formatted
    post_to_discord(content, sender, node_key)


def main():
    global PSK_KEY
    if not DISCORD_WEBHOOK_URL:
        raise SystemExit("DISCORD_WEBHOOK_URL is required")
    if not DISCORD_WEBHOOK_URL.startswith("https://"):
        raise SystemExit("DISCORD_WEBHOOK_URL must be a full HTTPS webhook URL")

    PSK_KEY = expand_psk(MQTT_PSK)
    psk_descriptor = MQTT_PSK if MQTT_PSK.lower() in ("default", "none") else "<set>"
    if PSK_KEY is None:
        log.info("Protobuf decryption disabled (MQTT_PSK=%s)", psk_descriptor or "none")
    else:
        log.info("Protobuf decryption enabled (%d-bit key, MQTT_PSK=%s)",
                 len(PSK_KEY) * 8, psk_descriptor)

    # Callback API v2 is supported by paho-mqtt 2.x.
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    if MQTT_USERNAME:
        client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

    client.on_connect = on_connect
    client.on_message = on_message

    def request_stop(*_):
        stop_event.set()
        client.disconnect()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    log.info("Starting Meshtastic MQTT to Discord webhook bridge (version %s)", APP_VERSION)
    log.info("MQTT broker=%s port=%s topic=%s", MQTT_BROKER, MQTT_PORT, MQTT_TOPIC)
    load_node_db()
    # Deliberately do not print the webhook URL.
    client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
    try:
        client.loop_forever()
    finally:
        http.close()


if __name__ == "__main__":
    main()

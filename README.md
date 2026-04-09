# hanotifications

A lightweight Linux service that bridges [Home Assistant](https://www.home-assistant.io/) notifications to native **KDE Plasma** desktop alerts, with support for camera snapshots and image feeds served by the Home Assistant API.

Designed for **CachyOS / Arch Linux**, but works on any systemd-based Linux with KDE Plasma.

---

## How it works

```
Home Assistant automation
        │
        │  POST /notify  (JSON + optional image reference)
        ▼
hanotifications  (aiohttp webhook server, systemd user service)
        │
        │  fetches image from HA API (camera_proxy or arbitrary URL)
        ▼
KDE Plasma notification  (D-Bus org.freedesktop.Notifications)
```

- HA calls a local HTTP webhook with a JSON payload
- The service authenticates the request, optionally fetches a camera snapshot from the HA API using a long-lived token, then pops up a native KDE Plasma notification
- Notifications use the **D-Bus `image-data` hint** for an embedded image preview; falls back to `notify-send -i` if `python-dbus` or `Pillow` are unavailable

---

## Requirements

| Package | Purpose |
|---|---|
| `python` | Runtime |
| `python-aiohttp` | Webhook HTTP server |
| `python-yaml` | Config file parsing |
| `python-dbus` | Rich D-Bus notifications with embedded images |
| `python-pillow` | Image resizing before embedding in D-Bus payload |
| `libnotify` | `notify-send` fallback |

---

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/koconnorgit/hanotifications.git
cd hanotifications
```

### 2. Run the installer

The installer installs dependencies via `pacman`, copies files into place, and registers the systemd user service.

```bash
bash install.sh
```

This will:
- Install all required packages with `pacman`
- Copy `hanotifications.py` to `~/.local/lib/hanotifications/`
- Copy `config.yaml.example` to `~/.config/hanotifications/config.yaml` (if not already present)
- Install and enable the systemd user service

### 3. Edit the config

```bash
nano ~/.config/hanotifications/config.yaml
```

See [Configuration](#configuration) below for all options.

### 4. Start the service

```bash
systemctl --user start hanotifications
systemctl --user status hanotifications
```

To view live logs:

```bash
journalctl --user -u hanotifications -f
```

---

## Configuration

`~/.config/hanotifications/config.yaml`

```yaml
# Base URL of your HA instance (no trailing slash)
ha_url: "http://homeassistant.local:8123"

# Long-lived access token
# Profile → Security → Long-Lived Access Tokens
ha_token: "YOUR_HA_LONG_LIVED_TOKEN"

# Set to false only if HA uses a self-signed TLS cert
ha_ssl_verify: true

# Webhook listener address. Use 127.0.0.1 if HA is on the same machine.
# Use 0.0.0.0 if HA is on a different host (make sure webhook_secret is set).
host: "127.0.0.1"
port: 8765

# Secret that Home Assistant must supply when calling the webhook.
# HA can send it as:
#   Authorization: Bearer <webhook_secret>
# or as an HMAC-SHA256 signature header:
#   X-HA-Signature: sha256=<hex digest of body>
#
# Leave empty ("") to disable auth (only safe on loopback).
webhook_secret: "change-me-to-a-random-string"

# Notification defaults
default_timeout_ms: 10000   # 0 = never auto-dismiss
default_urgency: "normal"   # low | normal | critical

# Max image dimension (px) when embedding image data in D-Bus payload
max_image_px: 512
```

---

## Home Assistant setup

### 1. Add the REST command

Add the following to your `configuration.yaml` (or a file included from it):

```yaml
rest_command:
  desktop_notify:
    url: "http://127.0.0.1:8765/notify"
    method: POST
    headers:
      Authorization: "Bearer change-me-to-a-random-string"
      Content-Type: "application/json"
    payload: >-
      {
        "title": "{{ title }}",
        "message": "{{ message }}",
        "urgency": "{{ urgency | default('normal') }}",
        "timeout_ms": {{ timeout_ms | default(10000) }}
        {% if image_url is defined %}, "image_url": "{{ image_url }}"{% endif %}
        {% if camera_entity is defined %}, "camera_entity": "{{ camera_entity }}"{% endif %}
      }
```

Use the same value for `webhook_secret` in both `config.yaml` and the `Authorization` header above.

Reload HA after adding the REST command (`Developer Tools → Restart` or `ha core reload`).

### 2. Use in automations

**Simple text notification:**

```yaml
- alias: "Notify desktop: doorbell"
  trigger:
    - platform: state
      entity_id: binary_sensor.doorbell
      to: "on"
  action:
    - service: rest_command.desktop_notify
      data:
        title: "Doorbell"
        message: "Someone is at the front door."
```

**Camera snapshot (entity shortcut):**

Pass `camera_entity` and hanotifications fetches the snapshot automatically using your HA token.

```yaml
- alias: "Notify desktop: front door motion with camera"
  trigger:
    - platform: state
      entity_id: binary_sensor.front_door_motion
      to: "on"
  action:
    - service: rest_command.desktop_notify
      data:
        title: "Motion Detected"
        message: "Front door camera triggered."
        camera_entity: "camera.front_door"
        urgency: "normal"
        timeout_ms: 15000
```

**Explicit image URL** (e.g. from Frigate):

```yaml
- alias: "Notify desktop: Frigate person detected"
  trigger:
    - platform: mqtt
      topic: "frigate/events"
  condition:
    - condition: template
      value_template: >-
        {{ trigger.payload_json.type == 'new' and
           trigger.payload_json.after.label == 'person' }}
  action:
    - service: rest_command.desktop_notify
      data:
        title: "Person Detected"
        message: "Frigate: {{ trigger.payload_json.after.camera }}"
        image_url: "http://homeassistant.local:8123/api/frigate/notifications/{{ trigger.payload_json.after.id }}/snapshot.jpg"
```

**Critical alert** (persistent, never auto-dismisses):

```yaml
- alias: "Notify desktop: smoke alarm"
  trigger:
    - platform: state
      entity_id: binary_sensor.smoke_detector
      to: "on"
  action:
    - service: rest_command.desktop_notify
      data:
        title: "SMOKE ALARM"
        message: "Smoke detected in the house!"
        urgency: "critical"
        timeout_ms: 0
```

More examples are in [`ha_examples/automations.yaml`](ha_examples/automations.yaml).

---

## Webhook payload reference

`POST /notify` accepts JSON with the following fields:

| Field | Type | Description |
|---|---|---|
| `title` | string | Notification title. Defaults to `"Home Assistant"` |
| `message` | string | Notification body text |
| `camera_entity` | string | HA camera entity ID (e.g. `camera.front_door`). The service fetches the snapshot using your `ha_token` |
| `image_url` | string | Explicit image URL. If the URL starts with `ha_url`, the `ha_token` is added automatically |
| `urgency` | string | `low`, `normal`, or `critical`. Defaults to `default_urgency` from config |
| `timeout_ms` | integer | Display duration in milliseconds. `0` = never dismiss. Defaults to `default_timeout_ms` |

`camera_entity` and `image_url` are mutually exclusive; `image_url` takes precedence if both are set.

---

## Testing

Test without Home Assistant using `curl`:

```bash
# Text-only notification
curl -X POST http://127.0.0.1:8765/notify \
  -H 'Authorization: Bearer YOUR_WEBHOOK_SECRET' \
  -H 'Content-Type: application/json' \
  -d '{"title":"Test","message":"hanotifications works!"}'

# With a camera snapshot
curl -X POST http://127.0.0.1:8765/notify \
  -H 'Authorization: Bearer YOUR_WEBHOOK_SECRET' \
  -H 'Content-Type: application/json' \
  -d '{"title":"Camera Test","message":"Snapshot attached.","camera_entity":"camera.front_door"}'

# Health check (no auth required)
curl http://127.0.0.1:8765/health
```

---

## Service management

```bash
# Start / stop / restart
systemctl --user start hanotifications
systemctl --user stop hanotifications
systemctl --user restart hanotifications

# Enable / disable autostart
systemctl --user enable hanotifications
systemctl --user disable hanotifications

# View logs
journalctl --user -u hanotifications -f
```

---

## License

MIT

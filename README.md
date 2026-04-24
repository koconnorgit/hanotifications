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
 ┌──────────────────────────────────┐
 │  image present?                  │
 │  yes → custom tkinter popup      │  ← full-width image, auto-dismisses
 │  no  → KDE Plasma notification   │  ← D-Bus org.freedesktop.Notifications
 └──────────────────────────────────┘
```

- HA calls a local HTTP webhook with a JSON payload
- The service authenticates the request, optionally fetches a camera snapshot from the HA API using a long-lived token, then displays a notification
- **When an image is present** (and `tkinter` + `Pillow` are available), a custom popup window appears in the bottom-right corner of the primary monitor, showing the image at configurable full width — much larger than a standard notification thumbnail
- **Without an image**, or if tkinter is unavailable, a native KDE Plasma notification is used via the D-Bus `image-data` hint; falls back to `notify-send -i` if `python-dbus` or `Pillow` are also unavailable
- **Click to livestream** — clicking a camera-snapshot popup opens the live feed, either in a standalone `mpv` window (default) or in a browser tab served by the daemon's `/viewer` endpoint (`live_stream_mode: browser`, ~2 s latency via hls.js). See [Click-to-livestream](#click-to-livestream-camera-popups) below.
- **Optional tray icon** — when `system_tray: true` is set, a Home Assistant icon appears in the KDE/Plasma system tray; blue when HA is reachable, grey with a red diagonal slash when it is not. Supports an inbound heartbeat from HA for detecting one-way outages. See [System tray icon](#system-tray-icon-optional) below.

---

## Requirements

| Package | Required | Purpose |
|---|---|---|
| `python` | yes | Runtime |
| `python-aiohttp` | yes | Webhook HTTP server |
| `python-yaml` | yes | Config file parsing |
| `python-pillow` | recommended | Image resizing for the custom popup (and D-Bus payload) |
| `tk` | recommended | Custom large-image popup window (`python-tk` / `tk` package) |
| `python-pyqt6` | optional | KDE/Plasma system tray icon (only used when `system_tray: true`) |
| `python-dbus` | optional | D-Bus notifications with embedded images (text-only fallback if absent) |
| `libnotify` | optional | `notify-send` fallback when D-Bus is unavailable |
| `mpv` | recommended | Click-to-livestream player under the default `live_stream_mode: mpv`. Not needed when `live_stream_mode: browser`. |
| web browser + `xdg-open` | optional | Required only for `live_stream_mode: browser`; loads `hls.js` from `cdn.jsdelivr.net` at viewer-page load time. |

> **Without Pillow + tkinter:** image notifications fall back to a standard KDE Plasma notification with a small embedded thumbnail.

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
# (only used when falling back to the standard notification path)
max_image_px: 512

# Width (px) of the custom image popup window.
# When an image is present and tkinter + Pillow are available, a standalone
# popup window is shown at this width instead of the standard notification.
# Set to 0 to disable the custom popup and always use the standard notification.
image_popup_width: 640

# Show a Home Assistant icon in the KDE/Plasma system tray.
# Blue when the HA server is reachable; grey with a red diagonal slash when not.
# Requires python-pyqt6.
system_tray: false

# Seconds between HA reachability checks used to color the tray icon.
ha_check_interval_s: 30

# Inbound heartbeat (opt-in). When true, the tray also goes grey if HA has
# not POSTed to /heartbeat within heartbeat_grace_s. Pairs with the
# hanotifications_heartbeat rest_command + time_pattern automation in
# ha_examples/.
heartbeat_required: false
heartbeat_grace_s: 90

# Click-to-livestream: clicking a camera-snapshot popup launches the live
# feed. Tries HLS first (via HA's stream integration over WebSocket for
# real framerate + audio), falls back to MJPEG from camera_proxy_stream
# (mpv mode only). live_stream_fps pins mpv to HA's MJPEG push rate
# (~2 fps typical) — only used on the MJPEG fallback path; HLS ignores it.
live_stream_on_click: true
live_stream_player: "mpv"
live_stream_fps: 2

# How the live stream is launched on click:
#   "mpv"     — spawn live_stream_player on the stream directly; HLS via
#                ffmpeg (no LL-HLS part support → ~10 s live latency), with
#                MJPEG fallback at live_stream_fps.
#   "browser" — xdg-open the daemon's /viewer page, which plays HA's HLS
#                stream via hls.js (LL-HLS part-aware → matches HA's UI
#                latency, ~2 s). No MJPEG fallback in this mode.
live_stream_mode: "mpv"
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

### Click-to-livestream (camera popups)

When a notification includes `camera_entity`, clicking the snapshot popup opens the live feed alongside dismissing the popup. Auto-dismiss on timeout does **not** launch the stream — only an explicit click. Controlled by `live_stream_on_click` (default `true`).

Two launch modes, controlled by `live_stream_mode`:

- **`mpv` (default)** — launches `mpv` on the stream directly. Tries HLS first via HA's stream integration (WebSocket `camera/stream` handshake → signed HLS URL), falls back to MJPEG from `/api/camera_proxy_stream/{entity}` if the handshake fails. Drawback: ffmpeg's HLS demuxer (what mpv uses) doesn't speak LL-HLS parts, so live latency floor is HA's segment duration — typically ~10 s. MJPEG fallback is ~2 fps snapshot polling, pinned via `live_stream_fps`. No-ops gracefully if `mpv` is not installed.
- **`browser`** — `xdg-open`s a page served by the daemon at `/viewer` which plays the same HLS via `hls.js` in your default browser. `hls.js` **does** speak `EXT-X-PART`, so latency matches HA's own UI (~2 s). The viewer page lives fully on your loopback (`127.0.0.1:{port}`); the browser tab loads `hls.js` from the jsDelivr CDN (outbound internet needed only for that one script). The `/viewer` endpoint is gated by a short-lived (5 min) per-notification token so the `webhook_secret` never ends up in the browser URL, history, or Referer headers; the daemon's access log also masks `token=…` values. There is no MJPEG fallback in browser mode — if HLS init fails the page returns an error.

In both modes playback starts muted so a motion alert doesn't suddenly play sound. Unmute in mpv mode by pressing `m` or clicking the OSC mute button; in browser mode use the HTML5 video controls.

Security notes:
- `mpv` mode, MJPEG fallback path: the bearer token is passed to mpv via a short-lived `0600` include file in `/tmp` so it never appears on the command line. The HLS path uses the signed URL from HA and needs no token at mpv time.
- `browser` mode: the signed HLS URL is embedded in the page source, but the page is served `Cache-Control: no-store` and meta `referrer=no-referrer` so it doesn't persist or leak across origins.

Popup-subprocess warnings (HLS fetch failures, mpv errors, `xdg-open` failures, etc.) go to the daemon's journal — tail them with `journalctl --user -u hanotifications -f`.

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

## System tray icon (optional)

hanotifications can show a Home Assistant icon in the KDE/Plasma system tray that reflects HA reachability at a glance:

- **Blue** house icon — the HA server responded to `GET {ha_url}/api/` within the poll interval
- **Grey** house icon with a **red diagonal slash** — the server did not respond (wrong URL, HA down, network issue, bad token, etc.), or — when the inbound heartbeat is enabled — no heartbeat from HA has arrived within the grace window

Right-clicking the icon shows the current reachability state and a **Quit** item that stops the webhook server.

### Enabling

Add to `~/.config/hanotifications/config.yaml`:

```yaml
system_tray: true          # show the tray icon
ha_check_interval_s: 30    # seconds between HA reachability checks
```

Then restart the service:

```bash
systemctl --user restart hanotifications
```

### Requirements

The tray icon requires `python-pyqt6`, which `install.sh` installs by default on Arch/CachyOS. If PyQt6 is missing the service logs a warning and continues without the tray; notifications still work normally.

The reachability check calls `GET {ha_url}/api/` with your `ha_token` and a 5-second timeout. It is cheap and safe to run frequently; 30 seconds is a reasonable default.

### Inbound heartbeat (optional)

The outbound check above catches failures where this machine can't reach HA. To also catch the other direction — HA is up but can no longer reach this host (firewall change, network move, broken automation) — enable the inbound heartbeat.

When `heartbeat_required: true`, the tray also goes grey if HA has not POSTed to `/heartbeat` within `heartbeat_grace_s` seconds. The default 90-second grace pairs with a 60-second HA cadence and tolerates one missed beat.

Add the heartbeat `rest_command` and automation from `ha_examples/rest_command.yaml` and `ha_examples/automations.yaml` to your HA config, then flip the flag:

```yaml
heartbeat_required: true
heartbeat_grace_s: 90
```

Right-clicking the tray icon distinguishes the failure modes — `outbound unreachable`, `no heartbeat (Ns)`, or `unreachable` (both) — so you can tell which leg broke.

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

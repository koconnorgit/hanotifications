#!/usr/bin/env python3
"""
hanotifications — Home Assistant → KDE Plasma notification bridge

Runs as a systemd user service. Receives webhook POSTs from Home Assistant
and pops up KDE Plasma notifications, optionally fetching camera/image data
from the HA API.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import sys
import tempfile
import time
from pathlib import Path

import aiohttp
from aiohttp import web
from aiohttp.abc import AbstractAccessLogger
import yaml

# Optional: rich D-Bus notifications with embedded image data
try:
    import dbus
    HAS_DBUS = True
except ImportError:
    HAS_DBUS = False

# Optional: Pillow for image resizing before embedding in D-Bus payload
try:
    from PIL import Image
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False

# Optional: tkinter for custom large-image popup windows
try:
    import tkinter as _tk_test; del _tk_test
    HAS_TKINTER = True
except Exception:
    HAS_TKINTER = False

# Optional: PyQt6 for the KDE/Plasma system tray icon
try:
    from PyQt6.QtWidgets import QApplication, QSystemTrayIcon, QMenu
    from PyQt6.QtGui import QIcon, QPixmap, QPainter, QColor, QBrush, QPainterPath, QPen, QAction
    from PyQt6.QtCore import QTimer, Qt, QObject, pyqtSignal
    HAS_QT = True
except ImportError:
    HAS_QT = False

# ---------------------------------------------------------------------------
# HTML page served by /viewer — plays HA's LL-HLS stream via hls.js, which
# (unlike ffmpeg) speaks EXT-X-PART and can approach HA's ~2s live edge.
# ---------------------------------------------------------------------------

_VIEWER_HTML = '''<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<!-- Block Referer so the URL-embedded token never leaks to the hls.js CDN
     (or anywhere else the page might reach out to). -->
<meta name="referrer" content="no-referrer">
<title>%%TITLE%%</title>
<style>
  html, body { margin: 0; padding: 0; background: #000; }
  video { width: 100vw; height: 100vh; object-fit: contain; background: #000; }
</style>
</head>
<body>
<video id="v" autoplay muted controls playsinline></video>
<script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.13/dist/hls.min.js"
        referrerpolicy="no-referrer"></script>
<script>
  const v = document.getElementById('v');
  const src = %%SRC_JSON%%;
  // muted is required for autoplay on fresh-tab loads (no user gesture).
  // Users can unmute via the controls; the stream keeps playing.
  if (window.Hls && Hls.isSupported()) {
    const hls = new Hls({ lowLatencyMode: true, liveSyncDurationCount: 1 });
    hls.loadSource(src);
    hls.attachMedia(v);
    hls.on(Hls.Events.MANIFEST_PARSED, () => { v.play().catch(() => {}); });
  } else if (v.canPlayType('application/vnd.apple.mpegurl')) {
    v.src = src;
    v.addEventListener('loadedmetadata', () => { v.play().catch(() => {}); });
  }
</script>
</body>
</html>
'''

# ---------------------------------------------------------------------------
# Custom image popup script (run in a subprocess for isolation)
# ---------------------------------------------------------------------------

_POPUP_SCRIPT = r"""
import sys, os, json, shutil, subprocess, tempfile, threading, asyncio
try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False
try:
    import tkinter as tk
    from PIL import Image, ImageTk
except ImportError as exc:
    print(f"popup: missing dependency: {exc}", file=sys.stderr)
    sys.exit(1)

data              = json.loads(sys.argv[1])
title             = data['title']
body              = data['body']
path              = data['path']
width             = data['width']
timeout           = data['timeout_ms']
live_stream_url   = data.get('live_stream_url')
live_stream_player = data.get('live_stream_player', 'mpv')
live_stream_fps   = data.get('live_stream_fps', 2)
live_stream_mode  = data.get('live_stream_mode', 'mpv')
viewer_url        = data.get('viewer_url')
ha_url            = data.get('ha_url', '')
ha_token          = data.get('ha_token', '')
ssl_verify        = data.get('ha_ssl_verify', True)
camera_entity     = data.get('camera_entity', '')


def fetch_hls_url():
    '''HA WS handshake -> signed HLS path. Returns None on any failure.

    HA's /api/hls/<entity>/master_playlist.m3u8 is NOT a real URL; the stream
    integration requires a WS request (camera/stream) that returns a signed,
    session-bound URL like /api/hls/<32-char-token>/master_playlist.m3u8.
    '''
    if not (HAS_AIOHTTP and ha_url and ha_token and camera_entity):
        return None
    # http(s)://host -> ws(s)://host
    ws_url = ha_url.rstrip('/').replace('http', 'ws', 1) + '/api/websocket'

    async def go():
        timeout = aiohttp.ClientTimeout(total=5)
        ssl_arg = None if ssl_verify else False
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.ws_connect(ws_url, ssl=ssl_arg) as ws:
                hello = await ws.receive_json()
                if hello.get('type') != 'auth_required':
                    return None
                await ws.send_json({'type': 'auth', 'access_token': ha_token})
                resp = await ws.receive_json()
                if resp.get('type') != 'auth_ok':
                    return None
                await ws.send_json({
                    'id': 1, 'type': 'camera/stream',
                    'entity_id': camera_entity, 'format': 'hls',
                })
                resp = await ws.receive_json()
                if not resp.get('success'):
                    print(f"popup: camera/stream failed: {resp}", file=sys.stderr)
                    return None
                return resp.get('result', {}).get('url')

    try:
        return asyncio.run(go())
    except Exception as exc:
        print(f"popup: HLS fetch failed: {exc}", file=sys.stderr)
        return None


def launch_browser_view():
    '''Open the daemon's /viewer page in the default browser.

    That page pulls a signed HLS URL from HA and plays it via hls.js, which
    (unlike ffmpeg) speaks EXT-X-PART and reaches HA's ~2s LL-HLS floor.
    URL is prebuilt by the daemon and includes the webhook token.
    '''
    if not viewer_url:
        print("popup: browser mode but daemon provided no viewer_url",
              file=sys.stderr)
        return
    try:
        subprocess.Popen(['xdg-open', viewer_url], start_new_session=True,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True)
    except Exception as exc:
        print(f"popup: xdg-open failed: {exc}", file=sys.stderr)


def launch_live_stream():
    if not live_stream_url:
        return
    if live_stream_mode == 'browser':
        launch_browser_view()
        return
    player = shutil.which(live_stream_player) if live_stream_player else None
    if not player:
        print(f"popup: player {live_stream_player!r} not on PATH — skipping live stream",
              file=sys.stderr)
        return

    # Prefer HLS (true video, real framerate, audio when the camera provides
    # it). Fall back to MJPEG polling if the WS handshake fails for any reason.
    use_hls = False
    stream_url = live_stream_url
    hls_path = fetch_hls_url()
    if hls_path:
        stream_url = ha_url.rstrip('/') + hls_path
        use_hls = True

    # mpv conf file holds secrets we don't want on argv (and TLS-skip for
    # self-signed). For HLS the URL is signed so no auth header is needed;
    # for MJPEG we must send the bearer token.
    conf_path = None
    want_auth = (not use_hls) and bool(ha_token)
    want_no_tls = not ssl_verify
    if want_auth or want_no_tls:
        fd, conf_path = tempfile.mkstemp(prefix='hanofy_mpv_', suffix='.conf', dir='/tmp')
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as fh:
            if want_auth:
                fh.write(f'http-header-fields="Authorization: Bearer {ha_token}"\n')
            if want_no_tls:
                fh.write('tls-verify=no\n')

    args = [player, '--mute=yes', '--force-window=yes', f'--title={title}']
    if use_hls:
        # HA emits LL-HLS with 10s full segments + 0.9s EXT-X-PART chunks.
        # ffmpeg's HLS demuxer ignores parts (as of 8.x), so the best we
        # can do is honor HA's EXT-X-START:TIME-OFFSET=-2.000,PRECISE=YES
        # tag via prefer_x_start, which starts us ~2s inside the last
        # segment instead of at its beginning (=10s behind). Paired with
        # aggressive buffer-reduction flags so we stay near the live edge.
        # These flags are safe on HLS (real PTS); they broke MJPEG where
        # timestamps are synthesized, which is why the MJPEG branch keeps
        # its own tuning. Floor on this path is ~2s — HA's PART-HOLD-BACK;
        # beating it requires hls.js-style part support or WebRTC/go2rtc.
        args.extend(['--profile=low-latency',
                     '--demuxer-lavf-o-add=prefer_x_start=1',
                     '--demuxer-lavf-o-add=live_start_index=-1',
                     '--cache=no',
                     '--demuxer-readahead-secs=0',
                     '--framedrop=decoder'])
    else:
        # MJPEG has no PTS or framerate metadata; pin playback to HA's
        # snapshot rate so mpv doesn't fast-forward through a buffer.
        args.extend(['--no-correct-pts',
                     f'--container-fps-override={live_stream_fps}'])
    if conf_path:
        args.append(f'--include={conf_path}')
    args.append(stream_url)

    try:
        subprocess.Popen(args, start_new_session=True,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True)
    except Exception as exc:
        print(f"popup: player launch failed: {exc}", file=sys.stderr)
        if conf_path:
            try: os.unlink(conf_path)
            except OSError: pass
        return

    if conf_path:
        def _cleanup():
            try: os.unlink(conf_path)
            except OSError: pass
        threading.Timer(2.0, _cleanup).start()

def primary_screen_geom(tk_root):
    # Returns (x, y, w, h) of the primary monitor. Prefers xrandr so we get
    # per-monitor geometry on multi-head X11 setups (winfo_screenwidth() on
    # X11 is the virtual-desktop span, which places popups on the wrong head).
    import re, subprocess
    try:
        out = subprocess.check_output(
            ['xrandr', '--query', '--current'],
            stderr=subprocess.DEVNULL, timeout=2,
        ).decode()
        for line in out.splitlines():
            if ' connected' in line and 'primary' in line.split()[:4]:
                m = re.search(r'(\d+)x(\d+)\+(\d+)\+(\d+)', line)
                if m:
                    w, h, x, y = (int(v) for v in m.groups())
                    return x, y, w, h
    except Exception:
        pass
    return 0, 0, tk_root.winfo_screenwidth(), tk_root.winfo_screenheight()


root = tk.Tk()
root.title(title)
# overrideredirect stops the window manager from re-centering or decorating
# the popup, so the explicit geometry below is honored exactly.
root.overrideredirect(True)
root.attributes('-topmost', True)
root.configure(bg='#2b2b2b')
root.resizable(False, False)

try:
    img = Image.open(path).convert('RGB')
    iw, ih = img.size
    if iw > width:
        img = img.resize((width, int(ih * width / iw)), Image.LANCZOS)
    elif iw < width:
        # Scale up small images to fill the configured width
        img = img.resize((width, int(ih * width / iw)), Image.LANCZOS)
    photo = ImageTk.PhotoImage(img)
    tk.Label(root, image=photo, bg='#2b2b2b', cursor='hand2').pack(padx=0, pady=0)
except Exception as exc:
    print(f"popup: image load error: {exc}", file=sys.stderr)

tk.Label(root, text=title, fg='white', bg='#2b2b2b',
         font=('Sans', 11, 'bold'), anchor='w').pack(fill='x', padx=10, pady=(6, 2))
if body:
    tk.Label(root, text=body, fg='#aaaaaa', bg='#2b2b2b',
             font=('Sans', 10), wraplength=width - 20, anchor='w',
             justify='left').pack(fill='x', padx=10, pady=(0, 8))

root.update_idletasks()
px, py, pw, ph = primary_screen_geom(root)
w  = root.winfo_reqwidth()
h  = root.winfo_reqheight()
# Bottom-right of primary screen, with margins above the KDE system tray.
x = px + pw - w - 20
y = py + ph - h - 60
root.geometry(f'{w}x{h}+{x}+{y}')

def on_click(_e):
    launch_live_stream()
    root.destroy()

root.bind('<Button-1>', on_click)
root.after(timeout, root.destroy)

try:
    os.unlink(path)
except OSError:
    pass

root.mainloop()
"""

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("hanotifications")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class Config:
    def __init__(self, path: str):
        with open(path) as f:
            d = yaml.safe_load(f) or {}

        self.ha_url: str = d.get("ha_url", "").rstrip("/")
        self.ha_token: str = d.get("ha_token", "")
        # Secret used to authenticate incoming webhook calls.
        # HA will send it as:  Authorization: Bearer <webhook_secret>
        # or as an HMAC-SHA256 signature header: X-HA-Signature: sha256=<hex>
        self.webhook_secret: str = d.get("webhook_secret", "")
        self.host: str = d.get("host", "127.0.0.1")
        self.port: int = int(d.get("port", 8765))
        self.default_timeout_ms: int = int(d.get("default_timeout_ms", 10000))
        self.default_urgency: str = d.get("default_urgency", "normal")
        # Maximum dimension (px) when resizing images for D-Bus payload
        self.max_image_px: int = int(d.get("max_image_px", 512))
        # Width (px) of the custom image popup window; 0 = use standard notification
        self.image_popup_width: int = int(d.get("image_popup_width", 640))
        # When True, skip TLS verification for self-signed HA certs
        self.ha_ssl_verify: bool = bool(d.get("ha_ssl_verify", True))
        # Show a KDE/Plasma system tray icon indicating HA reachability
        self.system_tray: bool = bool(d.get("system_tray", False))
        # Seconds between HA reachability checks for the tray icon
        self.ha_check_interval_s: int = int(d.get("ha_check_interval_s", 30))
        # When True, the tray also goes grey if HA hasn't posted to /heartbeat
        # within heartbeat_grace_s. Opt-in; requires an HA automation.
        self.heartbeat_required: bool = bool(d.get("heartbeat_required", False))
        # Max age (seconds) of the last /heartbeat before the link is stale.
        # Default 90s pairs with a 60s HA cadence (one missed beat tolerated).
        self.heartbeat_grace_s: int = int(d.get("heartbeat_grace_s", 90))
        # When True, clicking a camera-snapshot popup also spawns an external
        # player on the live MJPEG feed (in addition to dismissing the popup).
        self.live_stream_on_click: bool = bool(d.get("live_stream_on_click", True))
        # External player used for the live feed. Must accept a URL as the
        # last positional arg and understand --http-header-fields via a conf
        # file (currently hard-wired to mpv's CLI surface).
        self.live_stream_player: str = d.get("live_stream_player", "mpv")
        # Frames-per-second hint for the MJPEG stream. MJPEG has no framerate
        # metadata so mpv defaults to 25fps, which makes low-fps HA cameras
        # play in fast-forward bursts. HA's camera_proxy_stream is typically
        # ~2fps. Raise for faster cameras, lower if playback still runs ahead.
        self.live_stream_fps: float = float(d.get("live_stream_fps", 2))
        # "mpv" (default) launches the configured player on an HLS stream
        # (MJPEG fallback if the HA WS handshake fails). "browser" xdg-opens
        # the daemon's /viewer page, which plays the same HLS via hls.js —
        # part-aware, so it matches HA's own UI ~2s latency.
        self.live_stream_mode: str = d.get("live_stream_mode", "mpv")


# ---------------------------------------------------------------------------
# Connection-health state (shared between webhook server and tray)
# ---------------------------------------------------------------------------

class HealthState:
    """Tracks the last inbound heartbeat from Home Assistant.

    The webhook server stamps this when HA hits /heartbeat; the tray reads it
    to decide whether the link is still considered alive. Writes/reads of a
    single float attribute are atomic under the GIL, so no lock is needed.
    """

    def __init__(self):
        # Start the clock at init so the tray has a grace window on startup
        # instead of immediately going grey before the first heartbeat arrives.
        self.last_heartbeat_ts: float = time.time()

    def stamp_heartbeat(self) -> None:
        self.last_heartbeat_ts = time.time()

    def seconds_since_heartbeat(self) -> float:
        return time.time() - self.last_heartbeat_ts


class ViewerTokens:
    """Short-lived entity-bound tokens for the /viewer endpoint.

    The daemon issues one at notification time and embeds it in the popup's
    viewer URL, so the webhook_secret never touches the browser — and thus
    can't leak via browser history, the Referer header sent to the hls.js
    CDN, or xdg-open's argv. A TTL (no single-use) avoids 403s on refresh
    or prefetch, while still bounding long-term exposure.
    """
    TTL_SECS = 300  # 5 minutes: covers realistic click delays.

    def __init__(self):
        self._tokens: dict[str, tuple[float, str]] = {}  # token -> (expiry, entity)

    def issue(self, entity: str) -> str:
        import secrets
        now = time.time()
        # Opportunistic prune so the dict can't grow unbounded.
        self._tokens = {t: v for t, v in self._tokens.items() if v[0] > now}
        token = secrets.token_urlsafe(24)
        self._tokens[token] = (now + self.TTL_SECS, entity)
        return token

    def validate(self, token: str, entity: str) -> bool:
        entry = self._tokens.get(token)
        if not entry:
            return False
        expiry, ent = entry
        return time.time() <= expiry and ent == entity


# Redacts `token=<value>` query params in request URLs before they hit the
# journal. The viewer token is short-lived so leakage is already bounded,
# but there's no reason for the secret-shaped value to live in logs at all.
_TOKEN_LOG_RE = re.compile(r"([?&](?:token|access_token)=)[^&\s]+")


class _MaskingAccessLogger(AbstractAccessLogger):
    """aiohttp access logger that masks auth tokens in the request line."""
    def log(self, request, response, time):
        path = _TOKEN_LOG_RE.sub(r"\1<redacted>", request.path_qs)
        self.logger.info(
            '%s "%s %s HTTP/%d.%d" %d %s',
            request.remote, request.method, path,
            request.version.major, request.version.minor,
            response.status, response.body_length,
        )


# ---------------------------------------------------------------------------
# Notification sender
# ---------------------------------------------------------------------------

_URGENCY = {"low": 0, "normal": 1, "critical": 2}


class Notifier:
    def __init__(self, config: Config, viewer_tokens: "ViewerTokens | None" = None):
        self.cfg = config
        self.viewer_tokens = viewer_tokens

    # -- image fetching ------------------------------------------------------

    async def _fetch_image(self, url: str) -> str | None:
        """Download image URL → temp file path (caller must unlink)."""
        headers = {}
        if self.cfg.ha_url and url.startswith(self.cfg.ha_url):
            headers["Authorization"] = f"Bearer {self.cfg.ha_token}"

        connector = aiohttp.TCPConnector(ssl=None if self.cfg.ha_ssl_verify else False)
        timeout = aiohttp.ClientTimeout(total=15)
        try:
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as s:
                async with s.get(url, headers=headers) as resp:
                    if resp.status != 200:
                        log.warning("Image fetch failed: HTTP %s for %s", resp.status, url)
                        return None
                    ct = resp.headers.get("Content-Type", "image/jpeg")
                    ext = ".png" if "png" in ct else ".jpg"
                    data = await resp.read()
        except Exception as exc:
            log.warning("Image fetch error: %s", exc)
            return None

        tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False, dir="/tmp",
                                          prefix="hanofy_")
        tmp.write(data)
        tmp.close()
        return tmp.name

    # -- D-Bus ---------------------------------------------------------------

    def _dbus_send(self, title: str, body: str, image_path: str | None,
                   urgency: str, timeout_ms: int) -> bool:
        """Send via org.freedesktop.Notifications. Returns True on success."""
        if not HAS_DBUS:
            return False
        try:
            hints: dict = {"urgency": dbus.Byte(_URGENCY.get(urgency, 1))}

            if image_path:
                if HAS_PILLOW:
                    self._add_image_data_hint(hints, image_path)
                else:
                    hints["image-path"] = dbus.String(image_path)

            bus = dbus.SessionBus()
            obj = bus.get_object("org.freedesktop.Notifications",
                                 "/org/freedesktop/Notifications")
            iface = dbus.Interface(obj, "org.freedesktop.Notifications")
            iface.Notify(
                "Home Assistant Notification",  # app_name
                dbus.UInt32(0),             # replaces_id (0 = new)
                dbus.String(""),            # app_icon
                dbus.String(title),
                dbus.String(body),
                dbus.Array([], signature="s"),  # actions
                hints,
                dbus.Int32(timeout_ms),
            )
            return True
        except Exception as exc:
            log.warning("D-Bus send failed: %s", exc)
            return False

    def _add_image_data_hint(self, hints: dict, image_path: str):
        """Embed RGBA pixel data in the image-data D-Bus hint."""
        try:
            img = Image.open(image_path).convert("RGBA")
            img.thumbnail((self.cfg.max_image_px, self.cfg.max_image_px),
                          Image.LANCZOS)
            w, h = img.size
            row = w * 4
            raw = img.tobytes()
            hints["image-data"] = dbus.Struct(
                [
                    dbus.Int32(w),
                    dbus.Int32(h),
                    dbus.Int32(row),
                    dbus.Boolean(True),   # has_alpha
                    dbus.Int32(8),        # bits_per_sample
                    dbus.Int32(4),        # channels
                    dbus.Array([dbus.Byte(b) for b in raw], signature="y"),
                ],
                signature="iiibiiay",
            )
        except Exception as exc:
            log.warning("image-data encoding failed: %s", exc)
            hints["image-path"] = dbus.String(image_path)

    # -- custom image popup --------------------------------------------------

    def _show_image_popup(self, title: str, body: str, image_path: str,
                          timeout_ms: int,
                          live_stream_url: str | None = None,
                          camera_entity: str | None = None) -> bool:
        """Launch a large custom popup window for the image in a subprocess.

        The subprocess takes ownership of *image_path* and unlinks it when done.
        If *live_stream_url* is given, clicking the popup also spawns the
        configured live-stream player on that URL; *camera_entity* lets the
        popup request a signed HLS URL from HA instead of the MJPEG fallback.
        Returns True if the subprocess was launched successfully.
        """
        import subprocess
        import json

        payload = {
            "title": title,
            "body": body,
            "path": image_path,
            "width": self.cfg.image_popup_width,
            "timeout_ms": timeout_ms,
        }
        if live_stream_url and self.cfg.live_stream_on_click:
            payload["live_stream_url"] = live_stream_url
            payload["live_stream_player"] = self.cfg.live_stream_player
            payload["live_stream_fps"] = self.cfg.live_stream_fps
            payload["live_stream_mode"] = self.cfg.live_stream_mode
            payload["ha_url"] = self.cfg.ha_url
            payload["ha_token"] = self.cfg.ha_token
            payload["ha_ssl_verify"] = self.cfg.ha_ssl_verify
            if camera_entity:
                payload["camera_entity"] = camera_entity
            # Browser mode: point xdg-open at our own /viewer endpoint,
            # which serves hls.js and the signed HA HLS URL. 127.0.0.1 so
            # it works regardless of whether the daemon binds loopback or
            # 0.0.0.0. Auth uses a short-lived entity-bound viewer token,
            # NOT webhook_secret, so the URL (visible in browser history,
            # ps argv, etc.) carries nothing reusable after the TTL.
            if (self.cfg.live_stream_mode == "browser" and camera_entity
                    and self.viewer_tokens is not None):
                from urllib.parse import quote
                vt = self.viewer_tokens.issue(camera_entity)
                payload["viewer_url"] = (
                    f"http://127.0.0.1:{self.cfg.port}/viewer"
                    f"?token={vt}"
                    f"&entity={quote(camera_entity)}"
                )
        data = json.dumps(payload)
        try:
            # Inherit stderr so popup-subprocess warnings (HLS fetch failures,
            # mpv-not-found, etc.) land in the daemon's journal for debugging.
            subprocess.Popen(
                [sys.executable, "-c", _POPUP_SCRIPT, data],
                stdout=subprocess.DEVNULL,
                close_fds=True,
            )
            return True
        except Exception as exc:
            log.warning("Image popup launch failed: %s", exc)
            return False

    # -- notify-send fallback ------------------------------------------------

    def _notify_send(self, title: str, body: str, image_path: str | None,
                     urgency: str, timeout_ms: int):
        import subprocess
        cmd = ["notify-send", "-a", "Home Assistant Notification",
               "-u", urgency, "-t", str(timeout_ms)]
        if image_path:
            cmd += ["-i", image_path]
        cmd += [title, body]
        try:
            subprocess.run(cmd, check=True, timeout=5)
        except Exception as exc:
            log.error("notify-send failed: %s", exc)

    # -- public API ----------------------------------------------------------

    async def send(self, title: str, body: str,
                   image_url: str | None = None,
                   urgency: str | None = None,
                   timeout_ms: int | None = None,
                   live_stream_url: str | None = None,
                   camera_entity: str | None = None):
        urgency = urgency or self.cfg.default_urgency
        timeout_ms = timeout_ms if timeout_ms is not None else self.cfg.default_timeout_ms

        image_path: str | None = None
        if image_url:
            image_path = await self._fetch_image(image_url)

        # When an image is present and the custom popup is enabled, use it
        # instead of the standard notification so the image appears at full size.
        if (image_path and self.cfg.image_popup_width > 0
                and HAS_TKINTER and HAS_PILLOW):
            if self._show_image_popup(title, body, image_path, timeout_ms,
                                      live_stream_url, camera_entity):
                # Subprocess owns image_path cleanup; nothing left to do here.
                return

        try:
            if not self._dbus_send(title, body, image_path, urgency, timeout_ms):
                self._notify_send(title, body, image_path, urgency, timeout_ms)
        finally:
            if image_path:
                try:
                    os.unlink(image_path)
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# System tray icon (optional, KDE/Plasma)
# ---------------------------------------------------------------------------

_HA_BLUE = "#18BCF2"
_HA_GREY = "#888888"
_HA_SLASH_RED = "#FF3B30"


def _render_ha_icon(color: str, size: int = 64, disconnected: bool = False) -> "QIcon":
    """Draw a Home-Assistant-style house glyph in the given color.

    When disconnected=True, overlays a thin red diagonal slash across the
    whole icon (bottom-left → top-right) to make the offline state obvious.
    """
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(QColor(color)))

    s = size / 64.0
    path = QPainterPath()
    path.moveTo(32 * s, 5 * s)
    path.lineTo(59 * s, 29 * s)
    path.lineTo(53 * s, 29 * s)
    path.lineTo(53 * s, 58 * s)
    path.lineTo(11 * s, 58 * s)
    path.lineTo(11 * s, 29 * s)
    path.lineTo(5 * s, 29 * s)
    path.closeSubpath()
    painter.drawPath(path)

    # White "H" monogram on the house
    painter.setBrush(QBrush(QColor("white")))
    painter.drawRect(int(22 * s), int(32 * s), max(1, int(5 * s)), int(18 * s))
    painter.drawRect(int(37 * s), int(32 * s), max(1, int(5 * s)), int(18 * s))
    painter.drawRect(int(22 * s), int(39 * s), int(20 * s), max(1, int(4 * s)))

    if disconnected:
        pen = QPen(QColor(_HA_SLASH_RED))
        pen.setWidthF(max(2.0, 3.0 * s))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawLine(int(4 * s), int(60 * s), int(60 * s), int(4 * s))

    painter.end()
    return QIcon(pm)


_HOST_SENSOR_ENTITY = "sensor.hanotifications_host"


def _local_ip_to(host: str, port: int) -> str | None:
    """Return the source IP the OS would use to reach host:port.

    Uses a connected UDP socket — no packets are sent, but the kernel
    still picks an outgoing interface and assigns a source address. This
    is correct on multi-NIC / VPN hosts where gethostbyname(gethostname())
    would return the wrong interface.
    """
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(2)
            s.connect((host, port))
            return s.getsockname()[0]
    except Exception:
        return None


def _check_ha_reachable(cfg: Config) -> tuple[bool, str | None]:
    """Synchronous reachability check used by the tray poller.

    Returns (ok, registered_ip). registered_ip is the source IP that was
    successfully POSTed to sensor.hanotifications_host on this call, or
    None if the check failed or the registration step didn't run.

    Piggybacks an IP-registration POST onto each successful check: writes
    this workstation's source IP to sensor.hanotifications_host on HA, so
    HA's rest_commands can target {{ states('sensor.hanotifications_host') }}
    instead of a hostname that may fail to resolve.
    """
    import ssl
    import urllib.parse
    import urllib.request

    if not cfg.ha_url:
        return False, None

    ctx = None
    if cfg.ha_url.startswith("https") and not cfg.ha_ssl_verify:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(f"{cfg.ha_url}/api/")
    if cfg.ha_token:
        req.add_header("Authorization", f"Bearer {cfg.ha_token}")
    try:
        with urllib.request.urlopen(req, timeout=5, context=ctx) as resp:
            ok = 200 <= resp.status < 300
    except Exception:
        return False, None

    registered_ip: str | None = None
    if ok and cfg.ha_token:
        parsed = urllib.parse.urlparse(cfg.ha_url)
        default_port = 443 if parsed.scheme == "https" else 80
        ha_host = parsed.hostname or ""
        ha_port = parsed.port or default_port
        ip = _local_ip_to(ha_host, ha_port) if ha_host else None
        if ip:
            payload = json.dumps({
                "state": ip,
                "attributes": {
                    "port": cfg.port,
                    "friendly_name": "hanotifications host",
                    "source": "hanotifications agent",
                },
            }).encode()
            reg = urllib.request.Request(
                f"{cfg.ha_url}/api/states/{_HOST_SENSOR_ENTITY}",
                data=payload, method="POST",
            )
            reg.add_header("Authorization", f"Bearer {cfg.ha_token}")
            reg.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(reg, timeout=5, context=ctx) as r:
                    if 200 <= r.status < 300:
                        registered_ip = ip
            except Exception as exc:
                log.debug("Host IP registration failed: %s", exc)

    return ok, registered_ip


class SystemTray:
    """KDE/Plasma system tray icon showing HA reachability.

    Runs a Qt event loop on the calling thread. The reachability check runs
    in a short-lived worker thread and delivers its result via a Qt signal.
    """

    def __init__(self, cfg: Config, health: HealthState, on_quit):
        self.cfg = cfg
        self.health = health
        self._on_quit = on_quit

    def run(self):
        import threading

        app = QApplication(sys.argv[:1])
        app.setQuitOnLastWindowClosed(False)
        app.setApplicationName("hanotifications")

        if not QSystemTrayIcon.isSystemTrayAvailable():
            log.warning("System tray not available on this session — tray disabled")
            return

        icon_blue = _render_ha_icon(_HA_BLUE)
        icon_grey = _render_ha_icon(_HA_GREY, disconnected=True)

        tray = QSystemTrayIcon()
        tray.setIcon(icon_grey)
        tray.setToolTip("hanotifications — checking Home Assistant…")

        menu = QMenu()
        status = QAction("HA: checking…")
        status.setEnabled(False)
        menu.addAction(status)
        menu.addSeparator()
        quit_act = QAction("Quit")
        menu.addAction(quit_act)
        tray.setContextMenu(menu)
        tray.show()

        class _Signals(QObject):
            # (ok, registered_ip) — registered_ip is "" when none was reported
            # this cycle; pyqtSignal can't carry Optional[str] cleanly.
            outbound = pyqtSignal(bool, str)
        sig = _Signals()

        # Updated by the outbound poll worker, read on the Qt thread by refresh().
        # registered_ip persists across cycles so a transient registration miss
        # doesn't blank the tooltip.
        state = {"outbound_ok": False, "registered_ip": ""}

        def refresh():
            outbound_ok = state["outbound_ok"]
            if self.cfg.heartbeat_required:
                age = self.health.seconds_since_heartbeat()
                hb_fresh = age < self.cfg.heartbeat_grace_s
                healthy = outbound_ok and hb_fresh
                if healthy:
                    label = "reachable"
                elif outbound_ok and not hb_fresh:
                    label = f"no heartbeat ({int(age)}s)"
                elif not outbound_ok and hb_fresh:
                    label = "outbound unreachable"
                else:
                    label = "unreachable"
            else:
                healthy = outbound_ok
                label = "reachable" if healthy else "unreachable"

            if healthy:
                tray.setIcon(icon_blue)
            else:
                tray.setIcon(icon_grey)
            ip_suffix = f" — reporting {state['registered_ip']}" if state["registered_ip"] else ""
            tray.setToolTip(f"hanotifications — HA {label} ({self.cfg.ha_url}){ip_suffix}")
            status.setText(f"HA: {label}")

        def on_outbound(ok: bool, ip: str):
            state["outbound_ok"] = ok
            if ip:
                state["registered_ip"] = ip
            refresh()
        sig.outbound.connect(on_outbound)

        def poll():
            def worker():
                ok, ip = _check_ha_reachable(self.cfg)
                sig.outbound.emit(ok, ip or "")
            threading.Thread(target=worker, daemon=True).start()

        timer = QTimer()
        timer.timeout.connect(poll)
        timer.start(max(5, self.cfg.ha_check_interval_s) * 1000)
        poll()  # initial check

        # Re-evaluate tray state on a faster cadence when heartbeats matter, so
        # the icon flips within a few seconds of the grace window expiring
        # instead of waiting up to a full outbound-check interval.
        if self.cfg.heartbeat_required:
            hb_timer = QTimer()
            hb_timer.timeout.connect(refresh)
            hb_timer.start(5000)

        def do_quit():
            try:
                self._on_quit()
            finally:
                app.quit()
        quit_act.triggered.connect(do_quit)

        app.exec()


# ---------------------------------------------------------------------------
# Webhook server
# ---------------------------------------------------------------------------

class WebhookServer:
    def __init__(self, config: Config, notifier: Notifier, health: HealthState,
                 viewer_tokens: ViewerTokens):
        self.cfg = config
        self.notifier = notifier
        self.health = health
        self.viewer_tokens = viewer_tokens

    # -- auth ----------------------------------------------------------------

    def _authorized(self, request: web.Request, body: bytes) -> bool:
        if not self.cfg.webhook_secret:
            return True

        # HMAC-SHA256 signature: X-HA-Signature: sha256=<hex>
        sig = request.headers.get("X-HA-Signature", "")
        if sig.startswith("sha256="):
            expected = "sha256=" + hmac.new(
                self.cfg.webhook_secret.encode(),
                body,
                digestmod=hashlib.sha256,
            ).hexdigest()
            return hmac.compare_digest(sig, expected)

        # Simple bearer token: Authorization: Bearer <webhook_secret>
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return hmac.compare_digest(auth[7:], self.cfg.webhook_secret)

        return False

    # -- handlers ------------------------------------------------------------

    async def handle_notify(self, request: web.Request) -> web.Response:
        body = await request.read()

        if not self._authorized(request, body):
            log.warning("Rejected unauthenticated request from %s", request.remote)
            return web.Response(status=403, text="Forbidden")

        try:
            data: dict = await request.json()
        except Exception:
            return web.Response(status=400, text="Bad JSON")

        title: str = data.get("title", "Home Assistant")
        message: str = data.get("message") or data.get("body", "")
        image_url: str | None = data.get("image_url") or data.get("image")
        urgency: str | None = data.get("urgency")
        timeout_ms: int | None = data.get("timeout_ms") or data.get("timeout")

        # Convenience: pass camera_entity instead of full image_url
        camera_entity: str | None = data.get("camera_entity")
        live_stream_url: str | None = None
        if camera_entity:
            if not image_url:
                image_url = f"{self.cfg.ha_url}/api/camera_proxy/{camera_entity}"
            # MJPEG stream via camera_proxy_stream. Works for any HA camera
            # that can produce a snapshot. Video-only (MJPEG has no audio
            # track). HLS would carry audio but requires initializing a
            # signed stream session via HA's WebSocket API, which is a
            # bigger change than was in scope here.
            live_stream_url = f"{self.cfg.ha_url}/api/camera_proxy_stream/{camera_entity}"

        log.info("Notification: %r  image=%s  live=%s",
                 title, bool(image_url), bool(live_stream_url))

        # Pre-warm HA's stream worker now so the first segment is already
        # being written by the time the user clicks the popup. HA's stream
        # cold-start (FFmpeg spinning up against the source RTSP) is the
        # bulk of the click-to-play delay; firing camera/stream here pays
        # that cost in the background. HA dedupes concurrent stream
        # requests, so the later /viewer fetch reuses the same worker.
        if camera_entity and self.cfg.live_stream_mode == "browser":
            asyncio.create_task(self._fetch_hls_url(camera_entity))

        # Fire and forget — respond immediately so HA doesn't time out
        asyncio.create_task(
            self.notifier.send(title, message, image_url, urgency, timeout_ms,
                               live_stream_url, camera_entity)
        )
        return web.Response(text="OK")

    async def handle_health(self, request: web.Request) -> web.Response:
        return web.Response(text="hanotifications OK")

    async def handle_heartbeat(self, request: web.Request) -> web.Response:
        body = await request.read()
        if not self._authorized(request, body):
            log.warning("Rejected unauthenticated heartbeat from %s", request.remote)
            return web.Response(status=403, text="Forbidden")
        self.health.stamp_heartbeat()
        return web.Response(text="OK")

    async def _fetch_hls_url(self, entity: str) -> str | None:
        """HA WS handshake -> signed HLS path. None on any failure.

        Same dance as the popup subprocess does for mpv, but run here so the
        browser viewer gets the signed URL inline in the HTML response.
        """
        ws_url = self.cfg.ha_url.rstrip('/').replace('http', 'ws', 1) + '/api/websocket'
        ssl_arg = None if self.cfg.ha_ssl_verify else False
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=5),
            ) as session:
                async with session.ws_connect(ws_url, ssl=ssl_arg) as ws:
                    hello = await ws.receive_json()
                    if hello.get('type') != 'auth_required':
                        return None
                    await ws.send_json({'type': 'auth',
                                        'access_token': self.cfg.ha_token})
                    ok = await ws.receive_json()
                    if ok.get('type') != 'auth_ok':
                        return None
                    await ws.send_json({'id': 1, 'type': 'camera/stream',
                                        'entity_id': entity, 'format': 'hls'})
                    resp = await ws.receive_json()
                    if not resp.get('success'):
                        log.warning("camera/stream failed: %s", resp)
                        return None
                    return resp.get('result', {}).get('url')
        except Exception as exc:
            log.warning("HLS fetch failed for %s: %s", entity, exc)
            return None

    async def handle_viewer(self, request: web.Request) -> web.Response:
        """Serve a tiny HTML page that plays the camera's LL-HLS stream
        via hls.js (which, unlike ffmpeg, supports EXT-X-PART).

        Auth: `token` query param must be an entity-bound viewer token
        previously issued for this entity. Plain query string rather than
        an Authorization header because the browser loads this URL from
        an xdg-open spawn. The token is short-lived, so even if it leaks
        via browser history it's useless well before anyone sees it.
        """
        entity = request.query.get("entity", "")
        token = request.query.get("token", "")
        if not self.viewer_tokens.validate(token, entity):
            return web.Response(status=403, text="Forbidden")
        if not entity.startswith("camera."):
            return web.Response(status=400, text="Bad entity")

        hls_path = await self._fetch_hls_url(entity)
        if not hls_path:
            return web.Response(status=502, text="Stream init failed")

        abs_hls = self.cfg.ha_url.rstrip("/") + hls_path
        page = _VIEWER_HTML.replace("%%TITLE%%", entity) \
                           .replace("%%SRC_JSON%%", json.dumps(abs_hls))
        # no-store keeps the page (and embedded signed HLS URL) out of the
        # browser's back-button cache / disk cache.
        return web.Response(text=page, content_type="text/html",
                            headers={"Cache-Control": "no-store"})

    # -- app -----------------------------------------------------------------

    def build_app(self) -> web.Application:
        app = web.Application(client_max_size=4 * 1024 * 1024)
        app.router.add_post("/notify", self.handle_notify)
        app.router.add_get("/health", self.handle_health)
        app.router.add_post("/heartbeat", self.handle_heartbeat)
        app.router.add_get("/viewer", self.handle_viewer)
        return app


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def default_config_path() -> str:
    xdg = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    return os.path.join(xdg, "hanotifications", "config.yaml")


async def run_server(cfg: Config, notifier: Notifier, health: HealthState,
                     viewer_tokens: ViewerTokens, stop: asyncio.Event):
    server = WebhookServer(cfg, notifier, health, viewer_tokens)
    app = server.build_app()

    runner = web.AppRunner(app, access_log_class=_MaskingAccessLogger)
    await runner.setup()
    site = web.TCPSite(runner, cfg.host, cfg.port)
    await site.start()
    log.info("Ready.")

    try:
        await stop.wait()
    finally:
        await runner.cleanup()


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else default_config_path()

    if not os.path.exists(config_path):
        print(f"Config file not found: {config_path}", file=sys.stderr)
        print("See config.yaml.example for reference.", file=sys.stderr)
        sys.exit(1)

    cfg = Config(config_path)
    viewer_tokens = ViewerTokens()
    notifier = Notifier(cfg, viewer_tokens)
    health = HealthState()

    log.info("hanotifications starting on %s:%s", cfg.host, cfg.port)
    if not HAS_DBUS:
        log.warning("python-dbus not found — falling back to notify-send (no inline images)")
    if not HAS_PILLOW:
        log.warning("Pillow not found — images sent as icon path only (no embedded preview)")

    tray_enabled = cfg.system_tray and HAS_QT
    if cfg.system_tray and not HAS_QT:
        log.warning("system_tray=true but PyQt6 is not installed — tray icon disabled")

    if not tray_enabled:
        try:
            asyncio.run(run_server(cfg, notifier, health, viewer_tokens,
                                   asyncio.Event()))
        except KeyboardInterrupt:
            pass
        return

    # Qt must own the main thread; run the aiohttp server on a background thread.
    import threading

    loop = asyncio.new_event_loop()
    stop_event: asyncio.Event | None = None
    ready = threading.Event()

    def asyncio_thread():
        nonlocal stop_event
        asyncio.set_event_loop(loop)
        stop_event = asyncio.Event()
        ready.set()
        try:
            loop.run_until_complete(run_server(cfg, notifier, health,
                                                viewer_tokens, stop_event))
        except Exception as exc:
            log.error("Webhook server crashed: %s", exc)
        finally:
            loop.close()

    t = threading.Thread(target=asyncio_thread, name="hanotif-server", daemon=True)
    t.start()
    ready.wait()

    def on_quit():
        log.info("Tray quit requested — stopping webhook server")
        loop.call_soon_threadsafe(stop_event.set)
        t.join(timeout=5)

    try:
        SystemTray(cfg, health, on_quit).run()
    except KeyboardInterrupt:
        on_quit()


if __name__ == "__main__":
    main()

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
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

import aiohttp
from aiohttp import web
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
    from PyQt6.QtGui import QIcon, QPixmap, QPainter, QColor, QBrush, QPainterPath, QAction
    from PyQt6.QtCore import QTimer, Qt, QObject, pyqtSignal
    HAS_QT = True
except ImportError:
    HAS_QT = False

# ---------------------------------------------------------------------------
# Custom image popup script (run in a subprocess for isolation)
# ---------------------------------------------------------------------------

_POPUP_SCRIPT = r"""
import sys, os, json
try:
    import tkinter as tk
    from PIL import Image, ImageTk
except ImportError as exc:
    print(f"popup: missing dependency: {exc}", file=sys.stderr)
    sys.exit(1)

data     = json.loads(sys.argv[1])
title    = data['title']
body     = data['body']
path     = data['path']
width    = data['width']
timeout  = data['timeout_ms']

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

root.bind('<Button-1>', lambda e: root.destroy())
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


# ---------------------------------------------------------------------------
# Notification sender
# ---------------------------------------------------------------------------

_URGENCY = {"low": 0, "normal": 1, "critical": 2}


class Notifier:
    def __init__(self, config: Config):
        self.cfg = config

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
                          timeout_ms: int) -> bool:
        """Launch a large custom popup window for the image in a subprocess.

        The subprocess takes ownership of *image_path* and unlinks it when done.
        Returns True if the subprocess was launched successfully.
        """
        import subprocess
        import json

        data = json.dumps({
            "title": title,
            "body": body,
            "path": image_path,
            "width": self.cfg.image_popup_width,
            "timeout_ms": timeout_ms,
        })
        try:
            subprocess.Popen(
                [sys.executable, "-c", _POPUP_SCRIPT, data],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
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
                   timeout_ms: int | None = None):
        urgency = urgency or self.cfg.default_urgency
        timeout_ms = timeout_ms if timeout_ms is not None else self.cfg.default_timeout_ms

        image_path: str | None = None
        if image_url:
            image_path = await self._fetch_image(image_url)

        # When an image is present and the custom popup is enabled, use it
        # instead of the standard notification so the image appears at full size.
        if (image_path and self.cfg.image_popup_width > 0
                and HAS_TKINTER and HAS_PILLOW):
            if self._show_image_popup(title, body, image_path, timeout_ms):
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


def _render_ha_icon(color: str, size: int = 64) -> "QIcon":
    """Draw a Home-Assistant-style house glyph in the given color."""
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
    painter.end()
    return QIcon(pm)


def _check_ha_reachable(cfg: Config) -> bool:
    """Synchronous reachability check used by the tray poller."""
    import ssl
    import urllib.request

    if not cfg.ha_url:
        return False

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
            return 200 <= resp.status < 300
    except Exception:
        return False


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
        icon_grey = _render_ha_icon(_HA_GREY)

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
            outbound = pyqtSignal(bool)
        sig = _Signals()

        # Updated by the outbound poll worker, read on the Qt thread by refresh().
        state = {"outbound_ok": False}

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
            tray.setToolTip(f"hanotifications — HA {label} ({self.cfg.ha_url})")
            status.setText(f"HA: {label}")

        def on_outbound(ok: bool):
            state["outbound_ok"] = ok
            refresh()
        sig.outbound.connect(on_outbound)

        def poll():
            def worker():
                ok = _check_ha_reachable(self.cfg)
                sig.outbound.emit(ok)
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
    def __init__(self, config: Config, notifier: Notifier, health: HealthState):
        self.cfg = config
        self.notifier = notifier
        self.health = health

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
        if camera_entity and not image_url:
            image_url = f"{self.cfg.ha_url}/api/camera_proxy/{camera_entity}"

        log.info("Notification: %r  image=%s", title, bool(image_url))

        # Fire and forget — respond immediately so HA doesn't time out
        asyncio.create_task(
            self.notifier.send(title, message, image_url, urgency, timeout_ms)
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

    # -- app -----------------------------------------------------------------

    def build_app(self) -> web.Application:
        app = web.Application(client_max_size=4 * 1024 * 1024)
        app.router.add_post("/notify", self.handle_notify)
        app.router.add_get("/health", self.handle_health)
        app.router.add_post("/heartbeat", self.handle_heartbeat)
        return app


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def default_config_path() -> str:
    xdg = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    return os.path.join(xdg, "hanotifications", "config.yaml")


async def run_server(cfg: Config, notifier: Notifier, health: HealthState,
                     stop: asyncio.Event):
    server = WebhookServer(cfg, notifier, health)
    app = server.build_app()

    runner = web.AppRunner(app)
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
    notifier = Notifier(cfg)
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
            asyncio.run(run_server(cfg, notifier, health, asyncio.Event()))
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
            loop.run_until_complete(run_server(cfg, notifier, health, stop_event))
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

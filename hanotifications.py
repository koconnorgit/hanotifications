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

# ---------------------------------------------------------------------------
# Custom image popup script (run in a subprocess for isolation)
# ---------------------------------------------------------------------------

_POPUP_SCRIPT = r"""
import sys, os, json, webbrowser
try:
    import tkinter as tk
    from PIL import Image, ImageTk
except ImportError as exc:
    print(f"popup: missing dependency: {exc}", file=sys.stderr)
    sys.exit(1)

data       = json.loads(sys.argv[1])
title      = data['title']
body       = data['body']
path       = data['path']
width      = data['width']
timeout    = data['timeout_ms']
camera_url = data.get('camera_url')  # live stream URL, or None

root = tk.Tk()
root.title(title)
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
sw = root.winfo_screenwidth()
sh = root.winfo_screenheight()
w  = root.winfo_reqwidth()
h  = root.winfo_reqheight()
root.geometry(f'{w}x{h}+{sw - w - 20}+{sh - h - 60}')

def on_click(e):
    if camera_url:
        webbrowser.open(camera_url)
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
                "hanotifications",          # app_name
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
                          timeout_ms: int, camera_url: str | None = None) -> bool:
        """Launch a large custom popup window for the image in a subprocess.

        The subprocess takes ownership of *image_path* and unlinks it when done.
        If *camera_url* is provided, clicking the popup opens it in the browser.
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
            "camera_url": camera_url,
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
        cmd = ["notify-send", "-a", "hanotifications",
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
                   camera_url: str | None = None):
        urgency = urgency or self.cfg.default_urgency
        timeout_ms = timeout_ms if timeout_ms is not None else self.cfg.default_timeout_ms

        image_path: str | None = None
        if image_url:
            image_path = await self._fetch_image(image_url)

        # When an image is present and the custom popup is enabled, use it
        # instead of the standard notification so the image appears at full size.
        if (image_path and self.cfg.image_popup_width > 0
                and HAS_TKINTER and HAS_PILLOW):
            if self._show_image_popup(title, body, image_path, timeout_ms, camera_url):
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
# Webhook server
# ---------------------------------------------------------------------------

class WebhookServer:
    def __init__(self, config: Config, notifier: Notifier):
        self.cfg = config
        self.notifier = notifier

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
        camera_url: str | None = None
        if camera_entity:
            if not image_url:
                image_url = f"{self.cfg.ha_url}/api/camera_proxy/{camera_entity}"
            camera_url = f"{self.cfg.ha_url}/api/camera_proxy_stream/{camera_entity}"

        log.info("Notification: %r  image=%s  camera=%s", title, bool(image_url), bool(camera_url))

        # Fire and forget — respond immediately so HA doesn't time out
        asyncio.create_task(
            self.notifier.send(title, message, image_url, urgency, timeout_ms, camera_url)
        )
        return web.Response(text="OK")

    async def handle_health(self, request: web.Request) -> web.Response:
        return web.Response(text="hanotifications OK")

    # -- app -----------------------------------------------------------------

    def build_app(self) -> web.Application:
        app = web.Application(client_max_size=4 * 1024 * 1024)
        app.router.add_post("/notify", self.handle_notify)
        app.router.add_get("/health", self.handle_health)
        return app


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def default_config_path() -> str:
    xdg = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    return os.path.join(xdg, "hanotifications", "config.yaml")


async def run():
    config_path = sys.argv[1] if len(sys.argv) > 1 else default_config_path()

    if not os.path.exists(config_path):
        print(f"Config file not found: {config_path}", file=sys.stderr)
        print("See config.yaml.example for reference.", file=sys.stderr)
        sys.exit(1)

    cfg = Config(config_path)
    notifier = Notifier(cfg)
    server = WebhookServer(cfg, notifier)
    app = server.build_app()

    log.info("hanotifications starting on %s:%s", cfg.host, cfg.port)
    if not HAS_DBUS:
        log.warning("python-dbus not found — falling back to notify-send (no inline images)")
    if not HAS_PILLOW:
        log.warning("Pillow not found — images sent as icon path only (no embedded preview)")

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, cfg.host, cfg.port)
    await site.start()
    log.info("Ready.")

    try:
        await asyncio.Event().wait()  # run forever
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(run())

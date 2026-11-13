"""System tray integration for NetPulse Pro."""

from __future__ import annotations

import logging
import queue
import threading
from typing import Callable, Optional

logger = logging.getLogger(__name__)

APP_TITLE = "NetPulse Pro"

try:
    import pystray
    from PIL import Image, ImageDraw
except Exception as exc:  # pragma: no cover - optional dependency
    pystray = None
    Image = None
    ImageDraw = None
    logger.warning("System tray unavailable, missing dependency: %s", exc)


class TrayManager:
    """Hosts the tray icon on its own thread and forwards actions via a queue."""

    def __init__(self, actions: queue.Queue) -> None:
        self._actions = actions
        self._icon = None
        self._thread: Optional[threading.Thread] = None

    @property
    def available(self) -> bool:
        return pystray is not None

    def start(self) -> bool:
        if pystray is None:
            return False
        if self._icon is not None:
            return True
        try:
            image = self._create_image()
            menu = pystray.Menu(
                pystray.MenuItem(
                    "Show Dashboard", self._emit("show"), default=True
                ),
                pystray.MenuItem("Start Monitoring", self._emit("start")),
                pystray.MenuItem("Stop Monitoring", self._emit("stop")),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Exit", self._emit("exit")),
            )
            self._icon = pystray.Icon(APP_TITLE, image, APP_TITLE, menu)
            self._thread = threading.Thread(
                target=self._run_icon, name="netpulse-tray", daemon=True
            )
            self._thread.start()
            logger.info("System tray icon started")
            return True
        except Exception:
            logger.exception("Failed to start system tray icon")
            self._icon = None
            return False

    def stop(self) -> None:
        icon = self._icon
        self._icon = None
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                logger.exception("Failed to stop tray icon")
        thread = self._thread
        self._thread = None
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=2.0)

    def _run_icon(self) -> None:
        try:
            self._icon.run()
        except Exception:
            logger.exception("Tray icon loop crashed")
            self._icon = None

    def _emit(self, action: str) -> Callable[..., None]:
        def _handler(icon=None, item=None) -> None:
            try:
                self._actions.put_nowait(action)
            except Exception:
                logger.exception("Failed to dispatch tray action %r", action)

        return _handler

    @staticmethod
    def _create_image():
        size = 64
        image = Image.new("RGB", (size, size), (15, 23, 42))
        draw = ImageDraw.Draw(image)
        draw.ellipse((14, 14, 50, 50), fill=(34, 197, 94))
        draw.line((22, 34, 30, 26, 38, 34, 44, 22), fill=(15, 23, 42), width=3)
        return image

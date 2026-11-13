"""NetPulse Pro - Windows desktop network monitor entry point."""

from __future__ import annotations

import logging
import os
import queue
import sys
import traceback
from pathlib import Path

import customtkinter as ctk

APP_NAME = "NetPulse Pro"
APP_BG = "#0B0F19"


def _app_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    path = Path(base) / APP_NAME if base else Path.home() / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _configure_logging(log_dir: Path) -> None:
    handlers = [logging.FileHandler(log_dir / "netpulse.log", encoding="utf-8")]
    handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )


def _configure_scaling(root, zoom: float) -> float:
    """Pin CustomTkinter's window scale so geometry stays in physical pixels.

    Widgets keep the detected display scale multiplied by the user zoom, while
    the window scale is folded back to 1.0. Content and window size therefore
    stay consistent regardless of when Windows reports DPI changes.
    """
    try:
        display_scale = float(ctk.ScalingTracker.get_window_dpi_scaling(root))
    except Exception:
        logger.exception("Failed to detect display scaling")
        display_scale = 1.0
    if display_scale <= 0:
        display_scale = 1.0

    try:
        zoom = max(0.5, min(2.0, float(zoom)))
    except (TypeError, ValueError):
        zoom = 1.0

    window_scale = min(1.0, 1.0 / display_scale)
    ctk.ScalingTracker.deactivate_automatic_dpi_awareness = False
    ctk.set_widget_scaling(zoom)
    ctk.set_window_scaling(window_scale)
    return display_scale


def main() -> int:
    log_dir = _app_data_dir()
    _configure_logging(log_dir)
    logger = logging.getLogger(__name__)
    logger.info("Starting %s", APP_NAME)

    try:
        os.chdir(log_dir)
    except OSError:
        logger.exception("Could not set working directory to %s", log_dir)
    logger.info("Data directory: %s", Path.cwd())

    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("dark-blue")

    try:
        from core.database import init_db

        init_db()
    except Exception:
        logger.critical("Database initialization failed", exc_info=True)
        return 1

    from core.network import NetworkMonitor
    from core.settings_manager import load_settings
    from ui.dashboard import Dashboard
    from ui.tray import TrayManager

    settings = load_settings()
    logger.info("Settings loaded: %s", settings)

    try:
        root = ctk.CTk(fg_color=APP_BG)
    except Exception:
        logger.critical("Could not create the application window", exc_info=True)
        return 1

    logger.info(
        "Display scaling: %.2f, zoom %.2f",
        _configure_scaling(root, settings.get("zoom_factor", 1.0)),
        settings.get("zoom_factor", 1.0),
    )

    root.report_callback_exception = lambda exc, val, tb: logger.exception(
        "Tkinter callback exception: %s", val
    )

    actions: queue.Queue = queue.Queue()
    monitor = NetworkMonitor(ping_host=settings.get("ping_host"))
    dashboard = Dashboard(root, monitor, actions, settings)

    tray = TrayManager(actions)
    dashboard.set_tray_available(tray.start())
    if not dashboard.tray_available:
        logger.warning("System tray unavailable; window close will exit the app")

    try:
        dashboard.run()
    except Exception:
        logger.critical("Unhandled UI error", exc_info=True)
        traceback.print_exc()
    finally:
        logger.info("Shutting down")
        try:
            monitor.stop()
        except Exception:
            logger.exception("Failed to stop network monitor")
        try:
            tray.stop()
        except Exception:
            logger.exception("Failed to stop tray icon")

    logger.info("%s stopped", APP_NAME)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)

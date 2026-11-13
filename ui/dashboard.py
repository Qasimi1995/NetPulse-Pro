"""NetPulse Pro main dashboard window (CustomTkinter, dark obsidian theme)."""

from __future__ import annotations

import logging
import math
import queue
import time
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime, timedelta, timezone
from tkinter import filedialog, messagebox
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import customtkinter as ctk

from core.database import (
    export_range_to_csv,
    query_range,
    query_today_total,
    query_total_lifetime_usage,
    query_tracked_usage_since,
)
from core.network import (
    DEFAULT_PING_HOST,
    UNKNOWN,
    NetworkHealth,
    NetworkMonitor,
    NetworkSample,
    format_bytes,
    format_data,
    format_gb,
)
from core.settings_manager import (
    DEFAULT_SETTINGS,
    save_settings,
    set_windows_autostart,
)

logger = logging.getLogger(__name__)

REFRESH_MS = 1000
TOTAL_REFRESH_TICKS = 5
DATE_FORMATS = ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S")
MIN_PROGRESS_PEAK = 10.0 * 1024.0 ** 2

BG = "#0B0F19"
CARD = "#111827"
BORDER = "#1F2937"
TEXT = "#E5E7EB"
MUTED = "#9CA3AF"
EMERALD = "#10B981"
EMERALD_DARK = "#059669"
SAPPHIRE = "#3B82F6"
SAPPHIRE_DARK = "#2563EB"
AMBER = "#F59E0B"
RED = "#EF4444"
BADGE_GREEN = "#052e16"
BADGE_AMBER = "#451a03"
BADGE_RED = "#450a0a"

GRAPH_BG = "#0B0F19"
GRAPH_GRID = "#1F2937"

UI_FONT_CANDIDATES = ["Segoe UI Variable Display", "Segoe UI", "Tahoma"]
MONO_FONT_CANDIDATES = ["Consolas", "Cascadia Mono", "Courier New"]


def _pick_font(root: tk.Misc, candidates: List[str]) -> str:
    try:
        available = set(tkfont.families(root))
    except Exception:
        logger.exception("Failed to enumerate fonts")
        return candidates[0]
    for name in candidates:
        if name in available:
            return name
    return candidates[-1]


class SpeedGaugeCanvas:
    """Custom-drawn glowing speedometer for live network throughput."""

    START_ANGLE = 155.0
    SWEEP = 220.0
    TRACK_COLOR = "#1F2937"
    TRACK_WIDTH = 16
    TICK_SPEEDS = (1e6, 1e7, 5e7, 1e8)
    MAX_FLOOR = 100e6
    MAX_CEILING = 10e9

    DOWNLOAD_GLOW: Tuple[Tuple[float, str], ...] = (
        (24, "#064E3B"),
        (16, "#047857"),
        (9, "#10B981"),
    )
    UPLOAD_GLOW: Tuple[Tuple[float, str], ...] = (
        (18, "#1E3A8A"),
        (12, "#2563EB"),
        (6, "#3B82F6"),
    )

    def __init__(self, parent: ctk.CTkFrame, ui_font: str, mono_font: str) -> None:
        self._ui_font = ui_font
        self._mono_font = mono_font
        self.canvas = ctk.CTkCanvas(
            parent, bg=GRAPH_BG, highlightthickness=0, bd=0, width=120, height=80
        )
        self.max_speed = self.MAX_FLOOR
        self.peak_download = 0.0
        self.peak_upload = 0.0
        self.status = "LIVE"
        self._download = 0.0
        self._upload = 0.0
        self._resize_after: Optional[str] = None
        self.canvas.bind("<Configure>", self._on_resize)

    def _on_resize(self, event: tk.Event) -> None:
        if self._resize_after is not None:
            try:
                self.canvas.after_cancel(self._resize_after)
            except Exception:
                pass
        self._resize_after = self.canvas.after(120, self.draw)

    def pack(self, **kwargs: Any) -> None:
        self.canvas.pack(**kwargs)

    def grid(self, **kwargs: Any) -> None:
        self.canvas.grid(**kwargs)

    def set_speeds(self, download: float, upload: float, status: str = "LIVE") -> None:
        self._download = max(0.0, float(download))
        self._upload = max(0.0, float(upload))
        self.status = status
        self.peak_download = max(self.peak_download, self._download)
        self.peak_upload = max(self.peak_upload, self._upload)
        if (
            max(self._download, self._upload) > self.max_speed * 0.92
            and self.max_speed < self.MAX_CEILING
        ):
            self.max_speed = min(self.MAX_CEILING, self.max_speed * 2.0)
        self.draw()

    def reset_peaks(self) -> None:
        self.peak_download = 0.0
        self.peak_upload = 0.0
        self.draw()

    def _fraction(self, speed: float) -> float:
        if speed <= 0:
            return 0.0
        low = math.log10(1.0)
        high = math.log10(self.max_speed)
        if high <= low:
            return 0.0
        value = (math.log10(speed) - low) / (high - low)
        return max(0.0, min(1.0, value))

    def _ticks(self) -> List[float]:
        speeds = [speed for speed in self.TICK_SPEEDS if speed <= self.max_speed]
        if not speeds or self.max_speed > speeds[-1]:
            speeds.append(self.max_speed)
        return speeds

    @staticmethod
    def _tick_label(speed: float) -> str:
        if speed <= 0:
            return "0"
        if speed >= 1e9:
            return f"{speed / 1e9:.0f}G+"
        if speed >= 1e6:
            return f"{speed / 1e6:.0f}M+"
        if speed >= 1e3:
            return f"{speed / 1e3:.0f}K"
        return f"{speed:.0f}"

    def _arc_points(
        self,
        cx: float,
        cy: float,
        radius: float,
        start: float,
        end: float,
        steps: int,
    ) -> List[float]:
        points: List[float] = []
        for index in range(steps + 1):
            angle = start + (end - start) * (index / steps)
            radians = math.radians(angle)
            points.append(cx + radius * math.cos(radians))
            points.append(cy + radius * math.sin(radians))
        return points

    def draw(self) -> None:
        canvas = self.canvas
        canvas.delete("all")
        width = canvas.winfo_width()
        height = canvas.winfo_height()
        if width <= 20 or height <= 20:
            return

        cx = width / 2.0
        cy = height * 0.58
        radius = min(width * 0.36, height * 0.44)

        self._draw_track(cx, cy, radius)
        self._draw_progress(
            cx, cy, radius, self._fraction(self._download), self.DOWNLOAD_GLOW
        )
        self._draw_progress(
            cx, cy, radius - 22, self._fraction(self._upload), self.UPLOAD_GLOW
        )
        self._draw_ticks(cx, cy, radius)
        self._draw_readouts(cx, cy, radius)

    def _draw_track(self, cx: float, cy: float, radius: float) -> None:
        points = self._arc_points(
            cx, cy, radius, self.START_ANGLE, self.START_ANGLE + self.SWEEP, 64
        )
        self.canvas.create_line(
            points, fill=self.TRACK_COLOR, width=self.TRACK_WIDTH, capstyle="butt"
        )

    def _draw_progress(
        self,
        cx: float,
        cy: float,
        radius: float,
        fraction: float,
        layers: Sequence[Tuple[float, str]],
    ) -> None:
        if fraction <= 0.0:
            return
        end = self.START_ANGLE + self.SWEEP * fraction
        steps = max(2, int(56 * fraction))
        points = self._arc_points(cx, cy, radius, self.START_ANGLE, end, steps)
        for width, color in layers:
            self.canvas.create_line(points, fill=color, width=width, capstyle="round")

    def _draw_ticks(self, cx: float, cy: float, radius: float) -> None:
        for speed in self._ticks():
            angle = self.START_ANGLE + self.SWEEP * self._fraction(speed)
            radians = math.radians(angle)
            inner = radius + self.TRACK_WIDTH / 2 + 3
            outer = inner + 9
            self.canvas.create_line(
                cx + inner * math.cos(radians),
                cy + inner * math.sin(radians),
                cx + outer * math.cos(radians),
                cy + outer * math.sin(radians),
                fill=MUTED,
                width=2,
            )
            self.canvas.create_text(
                cx + (outer + 21) * math.cos(radians),
                cy + (outer + 21) * math.sin(radians),
                text=self._tick_label(speed),
                fill=MUTED,
                font=(self._mono_font, 8),
            )

    def _draw_readouts(self, cx: float, cy: float, radius: float) -> None:
        canvas = self.canvas
        canvas.create_text(
            cx,
            cy - radius * 0.58,
            text="DOWNLOAD",
            fill=EMERALD,
            font=(self._ui_font, 7, "bold"),
        )
        canvas.create_text(
            cx,
            cy - radius * 0.36,
            text=f"{format_bytes(self._download)}/s",
            fill=EMERALD,
            font=(self._mono_font, 12, "bold"),
        )
        canvas.create_text(
            cx,
            cy - radius * 0.10,
            text=f"{format_bytes(self._upload)}/s",
            fill=SAPPHIRE,
            font=(self._mono_font, 10, "bold"),
        )
        canvas.create_text(
            cx,
            cy + radius * 0.10,
            text="UPLOAD",
            fill=SAPPHIRE,
            font=(self._ui_font, 7, "bold"),
        )
        canvas.create_text(
            cx,
            cy + radius * 0.38,
            text=(
                f"PEAK  \u2193 {format_bytes(self.peak_download)}/s"
                f"   \u2191 {format_bytes(self.peak_upload)}/s"
            ),
            fill=MUTED,
            font=(self._mono_font, 7),
        )
        canvas.create_text(
            cx,
            cy + radius * 0.70,
            text=self.status,
            fill=EMERALD if self.status == "LIVE" else MUTED,
            font=(self._ui_font, 8, "bold"),
        )


class Dashboard:
    """Main dashboard: hero usage, live speeds, health, graph and range export."""

    def __init__(
        self,
        root: ctk.CTk,
        monitor: NetworkMonitor,
        actions: queue.Queue,
        settings: Dict[str, Any],
    ) -> None:
        self.root = root
        self.monitor = monitor
        self.actions = actions
        self.settings = dict(settings)
        self.gauge: Optional[SpeedGaugeCanvas] = None
        self.tray_available = False
        self._running = False
        self._tick_count = 0
        self._after_id: Optional[str] = None
        self._peak_rate = 0.0
        self._zoom = float(self.settings.get("zoom_factor", 1.0)) or 1.0

        self._ui_font = _pick_font(self.root, UI_FONT_CANDIDATES)
        self._mono_font = _pick_font(self.root, MONO_FONT_CANDIDATES)

        self._action_handlers: Dict[str, Callable[[], None]] = {
            "show": self.show,
            "hide": self.hide,
            "start": self._on_start,
            "stop": self._on_stop,
            "exit": self._on_exit,
        }

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _font(self, size: int, weight: str = "normal", mono: bool = False) -> ctk.CTkFont:
        return ctk.CTkFont(
            family=self._mono_font if mono else self._ui_font,
            size=size,
            weight=weight,
        )

    def _build_ui(self) -> None:
        self.root.title("NetPulse Pro")
        self.root.configure(fg_color=BG)

        self._build_header()
        self._build_kpi_row()
        self._build_health_bar()
        self._build_tabs()
        self._fit_window()

    def _window_scale(self) -> float:
        try:
            scale = float(ctk.ScalingTracker.get_window_scaling(self.root))
        except Exception:
            logger.exception("Failed to resolve the CustomTkinter window scale")
            return 1.0
        return scale if scale > 0 else 1.0

    def _fit_window(self) -> None:
        """Size the window to its content, clamped to the available screen."""
        self.root.update_idletasks()
        req_w = self.root.winfo_reqwidth()
        req_h = self.root.winfo_reqheight()
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        scale = self._window_scale()

        avail_w = max(640, int(screen_w * 0.96))
        avail_h = max(480, int(screen_h * 0.97))
        logical_w = min(max(680.0, req_w / scale), avail_w / scale)
        logical_h = min(max(520.0, req_h / scale), avail_h / scale)
        width = max(1, int(round(logical_w)))
        height = max(1, int(round(logical_h)))

        self.root.maxsize(4000, 4000)
        self.root.minsize(int(width * 0.70), int(height * 0.62))
        self.root.geometry(f"{width}x{height}")
        logger.info(
            "Window layout: content %dx%d, screen %dx%d, scale %.2f, window %dx%d",
            req_w,
            req_h,
            screen_w,
            screen_h,
            scale,
            int(round(logical_w * scale)),
            int(round(logical_h * scale)),
        )


    def _build_header(self) -> None:
        header = ctk.CTkFrame(self.root, fg_color="transparent")
        header.pack(fill="x", padx=16, pady=(8, 4))

        ctk.CTkLabel(
            header,
            text="NetPulse Pro",
            font=self._font(20, "bold"),
            text_color=TEXT,
        ).pack(side="left")

        self.status_badge = ctk.CTkLabel(
            header,
            text="PAUSED",
            font=self._font(10, "bold"),
            text_color=MUTED,
            fg_color=BORDER,
            corner_radius=8,
            width=92,
            height=24,
        )
        self.status_badge.pack(side="right")

        self.clock_label = ctk.CTkLabel(
            header,
            text="",
            font=self._font(10, "bold", mono=True),
            text_color=MUTED,
        )
        self.clock_label.pack(side="right", padx=(0, 12))
        self._update_clock()

    def _build_kpi_row(self) -> None:
        row = ctk.CTkFrame(self.root, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(0, 6))
        row.columnconfigure(0, weight=5, uniform="kpi")
        row.columnconfigure(1, weight=3, uniform="kpi")
        row.columnconfigure(2, weight=3, uniform="kpi")
        row.rowconfigure(0, weight=1)

        self._build_hero_card(row, 0)
        self._build_speed_card(row, 1, "DOWNLOAD", EMERALD, EMERALD_DARK, "down")
        self._build_speed_card(row, 2, "UPLOAD", SAPPHIRE, SAPPHIRE_DARK, "up")

    def _build_hero_card(self, parent: ctk.CTkFrame, column: int) -> None:
        hero = ctk.CTkFrame(
            parent,
            fg_color=CARD,
            border_color=BORDER,
            border_width=1,
            corner_radius=12,
        )
        hero.grid(row=0, column=column, sticky="nsew", padx=(0, 6))

        top = ctk.CTkFrame(hero, fg_color="transparent")
        top.pack(fill="x", padx=14, pady=(10, 0))
        ctk.CTkLabel(
            top,
            text="TOTAL LIFETIME USAGE",
            font=self._font(10, "bold"),
            text_color=MUTED,
        ).pack(side="left")



        self.lifetime_label = ctk.CTkLabel(
            hero,
            text="0.00 GB",
            font=self._font(25, "bold", mono=True),
            text_color=TEXT,
        )
        self.lifetime_label.pack(anchor="w", padx=14)

        self.lifetime_split = ctk.CTkLabel(
            hero,
            text="\u2193 0.00 GB    \u2191 0.00 GB",
            font=self._font(11, mono=True),
            text_color=MUTED,
        )
        self.lifetime_split.pack(anchor="w", padx=14)

        quota_box = ctk.CTkFrame(hero, fg_color="transparent")
        quota_box.pack(fill="x", padx=14)
        self.quota_bar = ctk.CTkProgressBar(
            quota_box,
            height=6,
            corner_radius=3,
            progress_color=EMERALD,
            fg_color=BORDER,
        )
        self.quota_bar.set(0)
        self.quota_bar.pack(fill="x")
        self.quota_label = ctk.CTkLabel(
            quota_box,
            text="QUOTA  --",
            font=self._font(9, "bold"),
            text_color=MUTED,
        )
        self.quota_label.pack(anchor="w", pady=(2, 0))
        self.quota_box = quota_box

        bottom = ctk.CTkFrame(hero, fg_color="transparent")
        bottom.pack(fill="x", padx=14, pady=(0, 10))
        self.bottom = bottom
        self.today_label = ctk.CTkLabel(
            bottom,
            text="TODAY  0.00 GB",
            font=self._font(10, "bold"),
            text_color=MUTED,
        )
        self.today_label.pack(side="left")

    def _build_speed_card(
        self,
        parent: ctk.CTkFrame,
        column: int,
        title: str,
        color: str,
        accent: str,
        key: str,
    ) -> None:
        card = ctk.CTkFrame(
            parent,
            fg_color=CARD,
            border_color=BORDER,
            border_width=1,
            corner_radius=12,
        )
        card.grid(
            row=0,
            column=column,
            sticky="nsew",
            padx=(0, 6) if column < 2 else (6, 0),
        )

        ctk.CTkLabel(
            card,
            text=title,
            font=self._font(10, "bold"),
            text_color=color,
        ).pack(anchor="w", padx=14, pady=(10, 0))

        rate = ctk.CTkLabel(
            card,
            text="0 B/s",
            font=self._font(18, "bold", mono=True),
            text_color=color,
        )
        rate.pack(anchor="w", padx=14)

        bar = ctk.CTkProgressBar(
            card,
            width=140,
            height=7,
            corner_radius=4,
            progress_color=accent,
            fg_color=BORDER,
        )
        bar.set(0)
        bar.pack(fill="x", padx=14, pady=(8, 12))

        if key == "down":
            self.down_rate = rate
            self.down_bar = bar
        else:
            self.up_rate = rate
            self.up_bar = bar

    def _build_health_bar(self) -> None:
        bar = ctk.CTkFrame(
            self.root,
            fg_color=CARD,
            border_color=BORDER,
            border_width=1,
            corner_radius=12,
        )
        bar.pack(fill="x", padx=16, pady=(0, 6))

        self.ping_badge = ctk.CTkLabel(
            bar,
            text="-- ms",
            font=self._font(11, "bold", mono=True),
            text_color=MUTED,
            fg_color=BORDER,
            corner_radius=8,
            width=84,
            height=26,
        )
        self.ping_badge.pack(side="left", padx=10, pady=7)

        self.nic_label = ctk.CTkLabel(
            bar,
            text="NIC  --",
            font=self._font(10, "bold", mono=True),
            text_color=TEXT,
        )
        self.nic_label.pack(side="left", padx=(2, 10), pady=7)
        self.ip_label = ctk.CTkLabel(
            bar,
            text="IP  --",
            font=self._font(10, "bold", mono=True),
            text_color=TEXT,
        )
        self.ip_label.pack(side="left", padx=(2, 10), pady=7)
        self.link_label = ctk.CTkLabel(
            bar,
            text="LINK  --",
            font=self._font(10, "bold", mono=True),
            text_color=TEXT,
        )
        self.link_label.pack(side="left", padx=(2, 10), pady=7)

        session = ctk.CTkFrame(bar, fg_color="transparent")
        session.pack(side="right", padx=12, pady=7)
        ctk.CTkLabel(
            session,
            text="SESSION",
            font=self._font(10, "bold"),
            text_color=MUTED,
        ).pack(side="left", padx=(0, 8))
        self.session_down = ctk.CTkLabel(
            session,
            text="\u2193 0.0 MB",
            font=self._font(11, "bold", mono=True),
            text_color=EMERALD,
        )
        self.session_down.pack(side="left", padx=(0, 8))
        self.session_up = ctk.CTkLabel(
            session,
            text="\u2191 0.0 MB",
            font=self._font(11, "bold", mono=True),
            text_color=SAPPHIRE,
        )
        self.session_up.pack(side="left")

    def _build_tabs(self) -> None:
        self.tabs = ctk.CTkTabview(
            self.root,
            height=300,
            fg_color=CARD,
            border_color=BORDER,
            border_width=1,
            corner_radius=12,
            segmented_button_fg_color=BG,
            segmented_button_selected_color=BORDER,
            segmented_button_selected_hover_color="#374151",
            segmented_button_unselected_color=BG,
            segmented_button_unselected_hover_color=CARD,
            text_color=TEXT,
        )
        self.tabs.pack(fill="both", expand=True, padx=16, pady=(0, 10))
        self.tabs.add("Live Telemetry")
        self.tabs.add("Custom Range Usage")
        self.tabs.add("Settings")
        self._build_live_tab(self.tabs.tab("Live Telemetry"))
        self._build_range_tab(self.tabs.tab("Custom Range Usage"))
        self._build_settings_tab(self.tabs.tab("Settings"))
        self._refresh_quota(refit=False)

        self.tabs.set("Live Telemetry")

    def _build_live_tab(self, tab: ctk.CTkFrame) -> None:
        outer = ctk.CTkFrame(tab, fg_color="transparent")
        outer.pack(fill="both", expand=True, padx=8, pady=(8, 2))
        outer.columnconfigure(0, weight=5, uniform="telemetry")
        outer.columnconfigure(1, weight=6, uniform="telemetry")
        outer.rowconfigure(0, weight=1)

        self.gauge = SpeedGaugeCanvas(outer, self._ui_font, self._mono_font)
        self.gauge.grid(row=0, column=0, sticky="nsew", padx=(0, 6))

        right = ctk.CTkFrame(outer, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew")

        self.graph = ctk.CTkCanvas(
            right, bg=GRAPH_BG, highlightthickness=0, bd=0, width=200, height=80
        )
        self.graph.pack(fill="both", expand=True)

        legend = ctk.CTkFrame(right, fg_color="transparent")
        legend.pack(fill="x", pady=(4, 2))
        ctk.CTkLabel(
            legend, text="DOWNLOAD", font=self._font(10, "bold"), text_color=EMERALD
        ).pack(side="left")
        ctk.CTkLabel(
            legend, text="UPLOAD", font=self._font(10, "bold"), text_color=SAPPHIRE
        ).pack(side="left", padx=14)
        self.peak_label = ctk.CTkLabel(
            legend,
            text="PEAK  0 B/s    180s window",
            font=self._font(10, "bold", mono=True),
            text_color=MUTED,
        )
        self.peak_label.pack(side="right")

        self._build_usage_banner(tab)

    def _build_usage_banner(self, parent: ctk.CTkFrame) -> None:
        banner = ctk.CTkFrame(
            parent,
            fg_color=CARD,
            border_color=BORDER,
            border_width=1,
            corner_radius=10,
        )
        banner.pack(fill="x", padx=8, pady=(4, 8))

        ctk.CTkLabel(
            banner,
            text="TOTAL MONITORED",
            font=self._font(9, "bold"),
            text_color=MUTED,
        ).pack(side="left", padx=(12, 4), pady=(8, 0))
        self.tracked_label = ctk.CTkLabel(
            banner,
            text="0.0 MB",
            font=self._font(15, "bold", mono=True),
            text_color=TEXT,
        )
        self.tracked_label.pack(side="left", padx=(0, 18), pady=(8, 0))

        ctk.CTkLabel(
            banner,
            text="CYCLE USAGE",
            font=self._font(9, "bold"),
            text_color=MUTED,
        ).pack(side="left", padx=4, pady=(8, 0))
        self.cycle_label = ctk.CTkLabel(
            banner,
            text="0.0 MB",
            font=self._font(15, "bold", mono=True),
            text_color=SAPPHIRE,
        )
        self.cycle_label.pack(side="left", padx=(0, 18), pady=(8, 0))

        ctk.CTkButton(
            banner,
            text="RESET USAGE",
            command=self._on_reset_usage,
            font=self._font(9, "bold"),
            corner_radius=8,
            width=120,
            height=26,
            fg_color="transparent",
            border_color=RED,
            border_width=1,
            hover_color=BADGE_RED,
            text_color=RED,
        ).pack(side="right", padx=12, pady=(6, 0))

        self.tracking_since_label = ctk.CTkLabel(
            banner,
            text="Tracking since: All-time (Epoch)",
            font=self._font(8, "bold", mono=True),
            text_color=MUTED,
        )
        self.tracking_since_label.pack(anchor="w", padx=12, pady=(0, 6))

    def _build_range_tab(self, tab: ctk.CTkFrame) -> None:
        top = ctk.CTkFrame(tab, fg_color="transparent")
        top.pack(fill="x", padx=14, pady=(12, 6))

        self.presets = ctk.CTkSegmentedButton(
            top,
            values=["Today", "Last 7 Days", "This Month"],
            command=self._on_preset,
            font=self._font(11, "bold"),
            corner_radius=8,
            border_width=2,
            selected_color=EMERALD,
            selected_hover_color=EMERALD_DARK,
            unselected_color=BG,
            unselected_hover_color=BORDER,
            text_color=TEXT,
        )
        self.presets.pack(side="left")
        self.presets.set("Today")

        ctk.CTkButton(
            top,
            text="Query",
            command=self._on_query,
            font=self._font(11, "bold"),
            corner_radius=8,
            width=90,
            height=30,
            fg_color=EMERALD,
            hover_color=EMERALD_DARK,
            text_color=BG,
        ).pack(side="left", padx=10)

        ctk.CTkButton(
            top,
            text="Export to CSV",
            command=self._on_export,
            font=self._font(11, "bold"),
            corner_radius=8,
            width=140,
            height=30,
            fg_color="transparent",
            border_color=EMERALD,
            border_width=1,
            hover_color=BADGE_GREEN,
            text_color=EMERALD,
        ).pack(side="left")

        dates = ctk.CTkFrame(tab, fg_color="transparent")
        dates.pack(fill="x", padx=14, pady=(0, 8))

        today = datetime.now().strftime("%Y-%m-%d")
        ctk.CTkLabel(
            dates, text="Start", font=self._font(10, "bold"), text_color=MUTED
        ).pack(side="left")
        self.start_var = tk.StringVar(value=today)
        ctk.CTkEntry(
            dates,
            textvariable=self.start_var,
            width=130,
            height=28,
            corner_radius=8,
            font=self._font(11, mono=True),
            fg_color=BG,
            border_color=BORDER,
            border_width=1,
            text_color=TEXT,
        ).pack(side="left", padx=(6, 14))

        ctk.CTkLabel(
            dates, text="End", font=self._font(10, "bold"), text_color=MUTED
        ).pack(side="left")
        self.end_var = tk.StringVar(value=today)
        ctk.CTkEntry(
            dates,
            textvariable=self.end_var,
            width=130,
            height=28,
            corner_radius=8,
            font=self._font(11, mono=True),
            fg_color=BG,
            border_color=BORDER,
            border_width=1,
            text_color=TEXT,
        ).pack(side="left", padx=(6, 0))

        results = ctk.CTkFrame(tab, fg_color="transparent")
        results.pack(fill="x", padx=14, pady=(2, 12))
        results.columnconfigure((0, 1, 2), weight=1, uniform="range")

        self.range_down = self._make_result_card(results, 0, "DOWNLOADED", EMERALD)
        self.range_up = self._make_result_card(results, 1, "UPLOADED", SAPPHIRE)
        self.range_total = self._make_result_card(results, 2, "TOTAL TRANSFERRED", TEXT)

    def _make_result_card(
        self, parent: ctk.CTkFrame, column: int, title: str, color: str
    ) -> ctk.CTkLabel:
        card = ctk.CTkFrame(
            parent,
            fg_color=BG,
            border_color=BORDER,
            border_width=1,
            corner_radius=10,
        )
        card.grid(row=0, column=column, sticky="nsew", padx=6)

        ctk.CTkLabel(
            card, text=title, font=self._font(9, "bold"), text_color=MUTED
        ).pack(anchor="w", padx=12, pady=(4, 0))
        value = ctk.CTkLabel(
            card,
            text="0.00 GB",
            font=self._font(16, "bold", mono=True),
            text_color=color,
        )
        value.pack(anchor="w", padx=12, pady=(0, 8))
        return value

    def _build_settings_tab(self, tab: ctk.CTkFrame) -> None:
        scroll = ctk.CTkScrollableFrame(
            tab, fg_color="transparent", corner_radius=0, height=150
        )
        scroll.pack(fill="both", expand=True, padx=4, pady=(2, 6))
        for column in range(3):
            scroll.columnconfigure(column, weight=1, uniform="settings")

        self._settings_card(scroll, 0, 0, "INTERFACE ZOOM", self._fill_zoom_card)
        self._settings_card(scroll, 0, 1, "GRAPH DURATION", self._fill_duration_card)
        self._settings_card(scroll, 0, 2, "PING HOST", self._fill_ping_card)
        self._settings_card(scroll, 1, 0, "DATA QUOTA", self._fill_quota_card)
        self._settings_card(scroll, 1, 1, "WINDOWS STARTUP", self._fill_autostart_card)
        self._settings_card(scroll, 1, 2, "PREFERENCES", self._fill_save_card)

    def _settings_card(
        self,
        parent: ctk.CTkFrame,
        row: int,
        column: int,
        title: str,
        filler: Callable[[ctk.CTkFrame], None],
    ) -> None:
        card = ctk.CTkFrame(
            parent,
            fg_color=BG,
            border_color=BORDER,
            border_width=1,
            corner_radius=10,
        )
        card.grid(row=row, column=column, sticky="nsew", padx=6, pady=5)
        ctk.CTkLabel(
            card, text=title, font=self._font(9, "bold"), text_color=MUTED
        ).pack(anchor="w", padx=10, pady=(6, 0))
        filler(card)

    def _fill_zoom_card(self, card: ctk.CTkFrame) -> None:
        box = ctk.CTkFrame(card, fg_color="transparent")
        box.pack(fill="x", padx=12, pady=(6, 8))
        self.zoom_slider = ctk.CTkSlider(
            box,
            from_=0.7,
            to=1.5,
            number_of_steps=16,
            command=self._on_zoom_changed,
            width=118,
            progress_color=EMERALD,
            button_color=EMERALD,
            button_hover_color=EMERALD_DARK,
        )
        self.zoom_slider.set(self._zoom)
        self.zoom_slider.pack(side="left")
        self.zoom_value = ctk.CTkLabel(
            box,
            text=f"{self._zoom:.2f}x",
            font=self._font(10, "bold", mono=True),
            text_color=MUTED,
            width=42,
        )
        self.zoom_value.pack(side="left", padx=6)

    def _fill_duration_card(self, card: ctk.CTkFrame) -> None:
        box = ctk.CTkFrame(card, fg_color="transparent")
        box.pack(fill="x", padx=10, pady=(4, 6))
        self.duration_var = tk.StringVar(
            value=str(self.settings.get("graph_duration_seconds", 180))
        )
        ctk.CTkEntry(
            box,
            textvariable=self.duration_var,
            width=80,
            height=28,
            corner_radius=8,
            font=self._font(11, mono=True),
            fg_color=BG,
            border_color=BORDER,
            border_width=1,
            text_color=TEXT,
        ).pack(side="left")
        ctk.CTkLabel(
            box, text="seconds", font=self._font(10, "bold"), text_color=MUTED
        ).pack(side="left", padx=8)

    def _fill_ping_card(self, card: ctk.CTkFrame) -> None:
        box = ctk.CTkFrame(card, fg_color="transparent")
        box.pack(fill="x", padx=10, pady=(4, 6))
        self.ping_host_var = tk.StringVar(
            value=str(self.settings.get("ping_host", DEFAULT_PING_HOST))
        )
        ctk.CTkEntry(
            box,
            textvariable=self.ping_host_var,
            width=150,
            height=28,
            corner_radius=8,
            font=self._font(11, mono=True),
            fg_color=BG,
            border_color=BORDER,
            border_width=1,
            text_color=TEXT,
        ).pack(side="left")

    def _fill_quota_card(self, card: ctk.CTkFrame) -> None:
        box = ctk.CTkFrame(card, fg_color="transparent")
        box.pack(fill="x", padx=12, pady=(6, 8))
        self.quota_var = tk.BooleanVar(
            value=bool(self.settings.get("quota_enabled", False))
        )
        ctk.CTkSwitch(
            box,
            text="Enable",
            variable=self.quota_var,
            command=self._on_quota_toggled,
            font=self._font(10, "bold"),
            text_color=TEXT,
            progress_color=EMERALD,
            fg_color=BORDER,
        ).pack(side="left")
        self.quota_entry_var = tk.StringVar(
            value=str(
                self.settings.get("quota_gb", DEFAULT_SETTINGS.get("quota_gb", 100.0))
            )
        )
        ctk.CTkEntry(
            box,
            textvariable=self.quota_entry_var,
            width=70,
            height=28,
            corner_radius=8,
            font=self._font(11, mono=True),
            fg_color=BG,
            border_color=BORDER,
            border_width=1,
            text_color=TEXT,
        ).pack(side="left", padx=8)
        ctk.CTkLabel(
            box, text="GB", font=self._font(10, "bold"), text_color=MUTED
        ).pack(side="left")

    def _fill_autostart_card(self, card: ctk.CTkFrame) -> None:
        box = ctk.CTkFrame(card, fg_color="transparent")
        box.pack(fill="x", padx=12, pady=(6, 8))
        self.autostart_var = tk.BooleanVar(
            value=bool(self.settings.get("autostart", False))
        )
        ctk.CTkSwitch(
            box,
            text="Launch at Windows startup",
            variable=self.autostart_var,
            command=self._on_autostart_changed,
            font=self._font(10, "bold"),
            text_color=TEXT,
            progress_color=EMERALD,
            fg_color=BORDER,
        ).pack(side="left")

    def _fill_save_card(self, card: ctk.CTkFrame) -> None:
        box = ctk.CTkFrame(card, fg_color="transparent")
        box.pack(fill="x", padx=12, pady=(6, 8))
        ctk.CTkButton(
            box,
            text="Save Settings",
            command=self._on_save_settings,
            font=self._font(10, "bold"),
            corner_radius=8,
            width=130,
            height=30,
            fg_color=EMERALD,
            hover_color=EMERALD_DARK,
            text_color=BG,
        ).pack(side="left")
        self.settings_status = ctk.CTkLabel(
            box, text="", font=self._font(10, "bold"), text_color=MUTED
        )
        self.settings_status.pack(side="left", padx=10)

    def run(self) -> None:
        self._running = True
        try:
            self.monitor.start()
        except Exception:
            logger.exception("Failed to start network monitoring")
        self._schedule_tick()
        self.root.mainloop()

    def set_tray_available(self, available: bool) -> None:
        self.tray_available = bool(available)

    def show(self) -> None:
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
            self._update_totals()
        except Exception:
            logger.exception("Failed to show dashboard")

    def hide(self) -> None:
        try:
            self.root.withdraw()
        except Exception:
            logger.exception("Failed to hide dashboard")

    def _schedule_tick(self) -> None:
        if not self._running:
            return
        self._after_id = self.root.after(REFRESH_MS, self._tick)

    def _tick(self) -> None:
        try:
            self._update_clock()
            self._drain_actions()
            self._update_live()
            self._tick_count += 1
            if self._tick_count % TOTAL_REFRESH_TICKS == 0:
                self._update_totals()
        except Exception:
            logger.exception("Dashboard refresh failed")
        finally:
            self._schedule_tick()

    def _update_clock(self) -> None:
        try:
            self.clock_label.configure(
                text=datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
            )
        except Exception:
            logger.exception("Failed to update the live clock")

    def _drain_actions(self) -> None:
        while True:
            try:
                action = self.actions.get_nowait()
            except queue.Empty:
                return
            try:
                handler = self._action_handlers.get(action)
                if handler is None:
                    logger.warning("Unknown tray action: %r", action)
                    continue
                handler()
            except Exception:
                logger.exception("Failed to handle tray action %r", action)

    def _update_live(self) -> None:
        running = self.monitor.is_running
        if running:
            self.status_badge.configure(text="LIVE", fg_color=BADGE_GREEN, text_color=EMERALD)
        else:
            self.status_badge.configure(text="PAUSED", fg_color=BORDER, text_color=MUTED)

        sample = self.monitor.get_last_sample()
        recv_rate = sample.recv_rate if sample else 0.0
        send_rate = sample.send_rate if sample else 0.0

        self.down_rate.configure(text=f"{format_bytes(recv_rate)}/s")
        self.up_rate.configure(text=f"{format_bytes(send_rate)}/s")

        if self.gauge is not None:
            self.gauge.set_speeds(
                recv_rate, send_rate, "LIVE" if running else "PAUSED"
            )

        self._peak_rate = max(self._peak_rate, recv_rate, send_rate)
        peak = max(self._peak_rate, MIN_PROGRESS_PEAK)
        self.down_bar.set(min(recv_rate / peak, 1.0))
        self.up_bar.set(min(send_rate / peak, 1.0))
        self.peak_label.configure(text=f"PEAK  {format_bytes(self._peak_rate)}/s")

        session_sent, session_recv = self.monitor.get_session_totals()
        self.session_down.configure(text=f"\u2193 {format_data(session_recv)}")
        self.session_up.configure(text=f"\u2191 {format_data(session_sent)}")

        self._update_health(self.monitor.get_health())
        self._draw_graph()

    def _update_health(self, health: Optional[NetworkHealth]) -> None:
        if health is not None and health.is_online and health.ping_ms is not None:
            if health.ping_ms < 60:
                badge_bg, badge_fg = BADGE_GREEN, EMERALD
            elif health.ping_ms < 150:
                badge_bg, badge_fg = BADGE_AMBER, AMBER
            else:
                badge_bg, badge_fg = BADGE_RED, RED
            self.ping_badge.configure(
                text=f"{health.ping_ms:.0f} ms", fg_color=badge_bg, text_color=badge_fg
            )
        elif health is not None:
            self.ping_badge.configure(
                text="OFFLINE", fg_color=BADGE_RED, text_color=RED
            )
        else:
            self.ping_badge.configure(
                text="-- ms", fg_color=BORDER, text_color=MUTED
            )

        nic = health.nic_name if health else UNKNOWN
        ip = health.local_ip if health else UNKNOWN
        speed = (
            f"{health.link_speed_mbps:.0f} Mbps"
            if health and health.link_speed_mbps
            else UNKNOWN
        )
        self.nic_label.configure(text=f"NIC  {nic}")
        self.ip_label.configure(text=f"IP  {ip}")
        self.link_label.configure(text=f"LINK  {speed}")

    def _update_totals(self) -> None:
        try:
            sent, recv = query_total_lifetime_usage()
            self.lifetime_label.configure(text=format_gb(sent + recv))
            self.lifetime_split.configure(
                text=f"\u2193 {format_gb(recv)}    \u2191 {format_gb(sent)}"
            )
            self._update_quota(sent + recv)
            self._update_usage_banner()
        except Exception:
            logger.exception("Lifetime usage query failed")
        try:
            self.today_label.configure(text=f"TODAY  {format_gb(query_today_total())}")
        except Exception:
            logger.exception("Today usage query failed")

    def _draw_graph(self) -> None:
        history = self.monitor.get_history()
        if not history:
            return
        duration = self._graph_duration()
        cutoff = time.time() - duration
        samples = [s for s in history if s.timestamp >= cutoff]
        if len(samples) < 2:
            return
        width = self.graph.winfo_width()
        height = self.graph.winfo_height()
        if width <= 20 or height <= 20:
            return

        peak = max((max(s.recv_rate, s.send_rate) for s in samples), default=0.0)
        peak = max(peak, 1.0)

        self.graph.delete("all")
        for fraction in (0.25, 0.5, 0.75):
            y = height * fraction
            self.graph.create_line(0, y, width, y, fill=GRAPH_GRID)

        self._draw_series(samples, lambda s: s.recv_rate, peak, width, height, EMERALD)
        self._draw_series(samples, lambda s: s.send_rate, peak, width, height, SAPPHIRE)
        self.peak_label.configure(
            text=f"PEAK  {format_bytes(peak)}/s    {duration}s window"
        )

    def _draw_series(
        self,
        samples: List[NetworkSample],
        value_of: Callable[[NetworkSample], float],
        peak: float,
        width: int,
        height: int,
        color: str,
    ) -> None:
        count = len(samples)
        if count < 2:
            return
        step = width / (count - 1)
        points: List[float] = []
        for index, sample in enumerate(samples):
            x = index * step
            ratio = min(value_of(sample) / peak, 1.0)
            y = height - ratio * (height - 12) - 6
            points.extend((x, y))
        self.graph.create_line(points, fill=color, width=2, smooth=True)

    def _on_zoom_changed(self, value: float) -> None:
        self._zoom = round(float(value), 2)
        try:
            self.zoom_value.configure(text=f"{self._zoom:.2f}x")
        except Exception:
            logger.exception("Failed to update the zoom label")

    def _on_quota_toggled(self) -> None:
        self.settings["quota_enabled"] = bool(self.quota_var.get())
        save_settings(self.settings)
        self._refresh_quota()

    def _on_autostart_changed(self) -> None:
        self.settings["autostart"] = bool(self.autostart_var.get())
        save_settings(self.settings)
        ok = set_windows_autostart(self.settings["autostart"])
        state = "enabled" if self.settings["autostart"] else "disabled"
        self._set_settings_status(f"Autostart {state}", ok)

    def _set_settings_status(self, message: str, ok: bool) -> None:
        try:
            self.settings_status.configure(
                text=message if ok else f"{message} (registry failed)",
                text_color=EMERALD if ok else RED,
            )
        except Exception:
            logger.exception("Failed to update the settings status")

    def _apply_zoom(self, zoom: float) -> None:
        try:
            zoom = max(0.5, min(2.0, float(zoom)))
        except (TypeError, ValueError):
            zoom = 1.0
        self._zoom = zoom
        try:
            ctk.set_widget_scaling(zoom)
        except Exception:
            logger.exception("Failed to apply the interface zoom")
        try:
            self.zoom_slider.set(zoom)
            self.zoom_value.configure(text=f"{zoom:.2f}x")
        except Exception:
            logger.exception("Failed to refresh the zoom controls")
        self._fit_window()

    def _refresh_quota(self, refit: bool = True) -> None:
        enabled = bool(self.settings.get("quota_enabled", False))
        try:
            if enabled:
                self.quota_box.pack(fill="x", padx=14)
            else:
                self.quota_box.pack_forget()
            self.bottom.pack(fill="x", padx=14, pady=(0, 10))
        except Exception:
            logger.exception("Failed to toggle the quota display")
        if refit:
            self._fit_window()

    def _tracking_since_text(self) -> str:
        raw = str(
            self.settings.get("reset_timestamp")
            or DEFAULT_SETTINGS.get("reset_timestamp", "1970-01-01 00:00:00")
        )
        if raw.startswith("1970-01-01"):
            return "Tracking since: All-time (Epoch)"
        try:
            utc_dt = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            return f"Tracking since: {raw}"
        local_dt = utc_dt.astimezone()
        return f"Tracking since: {local_dt.strftime('%b %d, %Y %I:%M %p')}"

    def _update_usage_banner(self) -> None:
        baseline = str(
            self.settings.get("reset_timestamp")
            or DEFAULT_SETTINGS.get("reset_timestamp", "1970-01-01 00:00:00")
        )
        try:
            sent, recv = query_tracked_usage_since(baseline)
            self.tracked_label.configure(text=format_data(sent + recv))
        except Exception:
            logger.exception("Tracked usage query failed")
            self.tracked_label.configure(text="--")
        session_sent, session_recv = self.monitor.get_session_totals()
        self.cycle_label.configure(text=format_data(session_sent + session_recv))
        try:
            self.tracking_since_label.configure(text=self._tracking_since_text())
        except Exception:
            logger.exception("Failed to update the tracking baseline label")

    def _on_reset_usage(self) -> None:
        try:
            confirmed = messagebox.askyesno(
                "Reset Usage",
                "Reset the monitored usage baseline?\n\n"
                "Historical records are kept; monitored totals will count "
                "from now on.",
                parent=self.root,
            )
            if not confirmed:
                return
            baseline = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            self.settings["reset_timestamp"] = baseline
            save_settings(self.settings)
            self.monitor.reset_session()
            if self.gauge is not None:
                self.gauge.reset_peaks()
            self._peak_rate = 0.0
            self._update_usage_banner()
            self._update_totals()
            logger.info("Usage baseline reset to %s", baseline)
        except Exception:
            logger.exception("Failed to reset usage")
            messagebox.showerror(
                "Reset Failed", "Could not reset the usage baseline.", parent=self.root
            )

    def _update_quota(self, total_bytes: int) -> None:
        if not bool(self.settings.get("quota_enabled", False)):
            return
        try:
            quota_gb = max(0.0, float(self.settings.get("quota_gb", 0.0)))
        except (TypeError, ValueError):
            quota_gb = 0.0
        if quota_gb <= 0:
            self.quota_label.configure(text="QUOTA  --")
            self.quota_bar.set(0)
            return
        used_gb = total_bytes / 1024.0 ** 3
        ratio = min(used_gb / quota_gb, 1.0)
        self.quota_bar.set(ratio)
        color = EMERALD if ratio < 0.8 else (AMBER if ratio < 1.0 else RED)
        self.quota_bar.configure(progress_color=color)
        self.quota_label.configure(text=f"QUOTA  {used_gb:.2f} / {quota_gb:.2f} GB")

    def _graph_duration(self) -> int:
        try:
            duration = int(float(self.settings.get("graph_duration_seconds", 180)))
        except (TypeError, ValueError):
            duration = 180
        return max(30, duration)

    def _on_save_settings(self) -> None:
        settings = dict(self.settings)
        settings["zoom_factor"] = self._zoom
        settings["autostart"] = bool(self.autostart_var.get())
        settings["ping_host"] = self.ping_host_var.get().strip() or DEFAULT_PING_HOST
        settings["quota_enabled"] = bool(self.quota_var.get())
        try:
            settings["quota_gb"] = max(0.0, float(self.quota_entry_var.get()))
        except ValueError:
            settings["quota_gb"] = float(DEFAULT_SETTINGS.get("quota_gb", 100.0))
        try:
            settings["graph_duration_seconds"] = max(
                30, int(float(self.duration_var.get()))
            )
        except ValueError:
            settings["graph_duration_seconds"] = int(
                DEFAULT_SETTINGS.get("graph_duration_seconds", 180)
            )

        save_settings(settings)
        self.settings = settings

        self._apply_zoom(settings["zoom_factor"])
        self.monitor.set_ping_host(settings["ping_host"])
        ok = set_windows_autostart(settings["autostart"])
        self._refresh_quota()
        self._set_settings_status("Settings saved", ok)
        logger.info("Settings saved: %s", settings)

    def _on_preset(self, value: str) -> None:
        try:
            now = datetime.now()
            end = now.replace(hour=23, minute=59, second=59)
            if value == "Today":
                start = now.replace(hour=0, minute=0, second=0)
            elif value == "Last 7 Days":
                start = (now - timedelta(days=6)).replace(hour=0, minute=0, second=0)
            elif value == "This Month":
                start = now.replace(day=1, hour=0, minute=0, second=0)
            else:
                return
            self.start_var.set(start.strftime("%Y-%m-%d %H:%M:%S"))
            self.end_var.set(end.strftime("%Y-%m-%d %H:%M:%S"))
            self._on_query()
        except Exception:
            logger.exception("Failed to apply preset %r", value)

    def _on_query(self) -> None:
        start = self.start_var.get().strip()
        end = self.end_var.get().strip()
        if not self._validate_range(start, end):
            return
        try:
            sent, recv = query_range(start, end)
        except Exception:
            logger.exception("Range query failed")
            messagebox.showerror("Query Failed", "Could not read the local database.")
            return
        self.range_down.configure(text=format_gb(recv))
        self.range_up.configure(text=format_gb(sent))
        self.range_total.configure(text=format_gb(sent + recv))

    def _on_export(self) -> None:
        start = self.start_var.get().strip()
        end = self.end_var.get().strip()
        if not self._validate_range(start, end):
            return
        initial = f"netpulse_{start.replace(' ', '_')}_{end.replace(' ', '_')}.csv"
        filepath = filedialog.asksaveasfilename(
            title="Export Bandwidth History",
            defaultextension=".csv",
            initialfile=initial,
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if not filepath:
            return
        try:
            count = export_range_to_csv(start, end, filepath)
        except Exception:
            logger.exception("CSV export failed")
            messagebox.showerror("Export Failed", f"Could not write:\n{filepath}")
            return
        messagebox.showinfo("Export Complete", f"Exported {count} records to:\n{filepath}")

    def _on_start(self) -> None:
        try:
            self.monitor.start()
        except Exception:
            logger.exception("Failed to start monitoring")
            messagebox.showerror("Error", "Could not start network monitoring.")
        self._update_live()

    def _on_stop(self) -> None:
        try:
            self.monitor.stop()
        except Exception:
            logger.exception("Failed to stop monitoring")
        self._update_live()

    def _on_close(self) -> None:
        if self.tray_available:
            self.hide()
        else:
            self._on_exit()

    def _on_exit(self) -> None:
        logger.info("Shutdown requested")
        self._running = False
        try:
            self.root.withdraw()
            self.root.quit()
        except Exception:
            logger.exception("Failed to stop the UI")

    @staticmethod
    def _validate_range(start: str, end: str) -> bool:
        start_dt = Dashboard._parse_datetime(start)
        end_dt = Dashboard._parse_datetime(end)
        if start_dt is None or end_dt is None:
            messagebox.showerror(
                "Invalid Date",
                "Dates must use YYYY-MM-DD or YYYY-MM-DD HH:MM:SS.",
            )
            return False
        if start_dt > end_dt:
            messagebox.showerror("Invalid Range", "Start date must be before end date.")
            return False
        return True

    @staticmethod
    def _parse_datetime(value: str) -> Optional[datetime]:
        for fmt in DATE_FORMATS:
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue
        return None

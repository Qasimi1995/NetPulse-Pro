"""Network I/O sampling, connection health probing and persistence worker."""

from __future__ import annotations

import logging
import socket
import threading
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

import psutil

from core.database import insert_record

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 2.0
HEALTH_INTERVAL_SECONDS = 3.0
MAX_HISTORY_SAMPLES = 1800
PING_TIMEOUT_SECONDS = 1.5
PING_PORT = 443
DEFAULT_PING_HOST = "1.1.1.1"
PING_FALLBACK_HOSTS = ("1.1.1.1", "8.8.8.8", "1.0.0.1", "208.67.222.222")
UNKNOWN = "--"

VIRTUAL_NIC_HINTS = (
    "loopback",
    "tap",
    "tun",
    "vpn",
    "wireguard",
    "wintun",
    "vethernet",
    "vmware",
    "virtualbox",
    "hyper-v",
    "pseudo",
    "protun",
    "tailscale",
    "zerotier",
    "softether",
    "openvpn",
)

STANDARD_SPEEDS = (
    10,
    100,
    150,
    300,
    433,
    500,
    866,
    1000,
    1200,
    1733,
    2000,
    2500,
    5000,
    10000,
    20000,
    25000,
    40000,
    50000,
    100000,
)


@dataclass(frozen=True)
class NetworkSample:
    """One polling interval of network activity."""

    timestamp: float
    interval: float
    bytes_sent: int
    bytes_recv: int

    @property
    def send_rate(self) -> float:
        return self.bytes_sent / self.interval if self.interval > 0 else 0.0

    @property
    def recv_rate(self) -> float:
        return self.bytes_recv / self.interval if self.interval > 0 else 0.0


@dataclass(frozen=True)
class NetworkHealth:
    """Snapshot of the live connection quality and interface details."""

    timestamp: float
    ping_ms: Optional[float]
    is_online: bool
    nic_name: str
    local_ip: str
    link_speed_mbps: Optional[float]


def format_bytes(num_bytes: float) -> str:
    """Render a byte rate using binary units."""
    value = max(0.0, float(num_bytes))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} PB"


def format_gb(num_bytes: float) -> str:
    """Render a data volume as gigabytes with two decimals."""
    return f"{max(0.0, float(num_bytes)) / 1024.0 ** 3:.2f} GB"


def format_data(num_bytes: float) -> str:
    """Render a data volume, switching between gigabytes and megabytes."""
    value = max(0.0, float(num_bytes))
    if value >= 1024.0 ** 3:
        return f"{value / 1024.0 ** 3:.2f} GB"
    return f"{value / 1024.0 ** 2:.1f} MB"


def ping_tcp(host: str, port: int, timeout: float = PING_TIMEOUT_SECONDS) -> Optional[float]:
    """Measure TCP handshake round-trip time in milliseconds, or None on failure."""
    start = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return (time.perf_counter() - start) * 1000.0
    except OSError:
        return None


def collect_health(ping_host: Optional[str] = None) -> NetworkHealth:
    """Probe latency and resolve the active interface details."""
    hosts: List[str] = []
    if ping_host:
        hosts.append(ping_host)
    hosts.extend(host for host in PING_FALLBACK_HOSTS if host != ping_host)

    ping_ms: Optional[float] = None
    for host in hosts:
        ping_ms = ping_tcp(host, PING_PORT)
        if ping_ms is not None:
            break
    nic_name, local_ip, link_speed = _interface_info()
    return NetworkHealth(
        timestamp=time.time(),
        ping_ms=ping_ms,
        is_online=ping_ms is not None,
        nic_name=nic_name,
        local_ip=local_ip,
        link_speed_mbps=link_speed,
    )


def _is_virtual_nic(name: str) -> bool:
    lowered = name.lower()
    return any(hint in lowered for hint in VIRTUAL_NIC_HINTS)


def _normalize_speed(raw: Optional[float]) -> Optional[float]:
    """Snap a reported link rate to a standard Ethernet/Wi-Fi value."""
    if raw is None or raw <= 0:
        return None
    for standard in STANDARD_SPEEDS:
        if abs(raw - standard) / standard <= 0.10:
            return float(standard)
    return None


def _interface_info() -> Tuple[str, str, Optional[float]]:
    name = UNKNOWN
    ip = UNKNOWN
    speed: Optional[float] = None
    try:
        stats = psutil.net_if_stats()
        addrs = psutil.net_if_addrs()
        counters = psutil.net_io_counters(pernic=True)
    except Exception:
        logger.exception("Failed to enumerate network interfaces")
        return name, ip, speed

    candidates = []
    for nic, nic_counters in counters.items():
        stat = stats.get(nic)
        if stat is None or not stat.isup:
            continue
        candidates.append(
            {
                "nic": nic,
                "total": nic_counters.bytes_sent + nic_counters.bytes_recv,
                "virtual": _is_virtual_nic(nic),
                "speed": _normalize_speed(getattr(stat, "speed", None)),
            }
        )

    if not candidates:
        return name, ip, speed

    physical = [entry for entry in candidates if not entry["virtual"]]
    best = max(physical or candidates, key=lambda entry: entry["total"])
    name = best["nic"]
    speed = best["speed"]

    for addr in addrs.get(name, []):
        if addr.family == socket.AF_INET and addr.address != "127.0.0.1":
            ip = addr.address
            break

    return name, ip, speed


class NetworkMonitor:
    """Background worker that polls NIC counters and stores usage deltas."""

    def __init__(
        self,
        poll_interval: float = POLL_INTERVAL_SECONDS,
        ping_host: Optional[str] = None,
    ) -> None:
        self.poll_interval = max(0.5, float(poll_interval))
        self._ping_host = ping_host or DEFAULT_PING_HOST
        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None
        self._health_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._last_counters = None
        self._last_poll_time: Optional[float] = None
        self._last_sample: Optional[NetworkSample] = None
        self._health: Optional[NetworkHealth] = None
        self._session_sent = 0
        self._session_recv = 0
        self._history: List[NetworkSample] = []

    def start(self) -> None:
        with self._lock:
            if self._worker_thread is not None and self._worker_thread.is_alive():
                return
            self._stop_event.clear()
            self._worker_thread = threading.Thread(
                target=self._run, name="netpulse-worker", daemon=True
            )
            self._health_thread = threading.Thread(
                target=self._health_loop, name="netpulse-health", daemon=True
            )
            self._worker_thread.start()
            self._health_thread.start()
        logger.info("Network monitoring started")

    def stop(self) -> None:
        self._stop_event.set()
        for thread in (self._worker_thread, self._health_thread):
            if (
                thread is not None
                and thread.is_alive()
                and thread is not threading.current_thread()
            ):
                thread.join(timeout=self.poll_interval * 3.0 + 1.0)
                if thread.is_alive():
                    logger.warning("Network monitor thread did not stop in time")
        self._worker_thread = None
        self._health_thread = None
        logger.info("Network monitoring stopped")

    @property
    def is_running(self) -> bool:
        thread = self._worker_thread
        return thread is not None and thread.is_alive()

    def get_last_sample(self) -> Optional[NetworkSample]:
        with self._lock:
            return self._last_sample

    def get_history(self) -> List[NetworkSample]:
        with self._lock:
            return list(self._history)

    def get_health(self) -> Optional[NetworkHealth]:
        with self._lock:
            return self._health

    def get_session_totals(self) -> Tuple[int, int]:
        """Bytes uploaded and downloaded since the application was launched."""
        with self._lock:
            return self._session_sent, self._session_recv

    def set_ping_host(self, host: Optional[str]) -> None:
        """Use a custom host for the connection health probe."""
        candidate = (host or "").strip()
        self._ping_host = candidate or DEFAULT_PING_HOST

    def reset_session(self) -> None:
        """Zero the session counters without touching persisted history."""
        with self._lock:
            self._session_sent = 0
            self._session_recv = 0

    def _run(self) -> None:
        try:
            self._last_counters = self._read_counters()
            self._last_poll_time = time.monotonic()
        except Exception:
            logger.exception("Failed to read initial network counters")
            self._last_counters = None
            self._last_poll_time = None

        while not self._stop_event.is_set():
            try:
                self._stop_event.wait(self.poll_interval)
                if self._stop_event.is_set():
                    break
                self._sample_once()
            except Exception:
                logger.exception("Unexpected error in network monitor loop")

    def _health_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                health = collect_health(self._ping_host)
                with self._lock:
                    self._health = health
            except Exception:
                logger.exception("Connection health check failed")
            self._stop_event.wait(HEALTH_INTERVAL_SECONDS)

    def _sample_once(self) -> None:
        current = self._read_counters()
        if current is None:
            return

        now = time.monotonic()
        last = self._last_counters
        last_time = self._last_poll_time
        self._last_counters = current
        self._last_poll_time = now
        if last is None or last_time is None:
            return

        interval = max(now - last_time, 1e-6)
        sent = max(0, int(current.bytes_sent) - int(last.bytes_sent))
        recv = max(0, int(current.bytes_recv) - int(last.bytes_recv))

        if sent > 0 or recv > 0:
            try:
                insert_record(sent, recv)
            except Exception:
                logger.exception("Failed to persist bandwidth record")

        sample = NetworkSample(
            timestamp=time.time(),
            interval=interval,
            bytes_sent=sent,
            bytes_recv=recv,
        )
        with self._lock:
            self._session_sent += sent
            self._session_recv += recv
            self._last_sample = sample
            self._history.append(sample)
            if len(self._history) > MAX_HISTORY_SAMPLES:
                del self._history[: len(self._history) - MAX_HISTORY_SAMPLES]

    @staticmethod
    def _read_counters():
        counters = psutil.net_io_counters(pernic=False)
        return counters if counters is not None else None

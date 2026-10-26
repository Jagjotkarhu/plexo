"""QoS watchdog and adapter health monitoring.

Tracks real-time throughput, detects hotspot stalls, and triggers
failover to healthy connections.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict


@dataclass
class AdapterMetrics:
    """Live performance metrics for a specific adapter."""
    name: str
    ip_address: str
    kind: str
    bytes_transferred: int = 0
    active_chunks: int = 0
    last_byte_time: float = field(default_factory=time.time)
    start_time: float = field(default_factory=time.time)
    errors: int = 0
    is_healthy: bool = True

    def record_bytes(self, count: int) -> None:
        self.bytes_transferred += count
        self.last_byte_time = time.time()

    def record_error(self) -> None:
        self.errors += 1
        if self.errors > 3:
            self.is_healthy = False

    @property
    def speed_bytes_per_sec(self) -> float:
        elapsed = time.time() - self.start_time
        if elapsed <= 0:
            return 0.0
        return self.bytes_transferred / elapsed

    @property
    def speed_mbps(self) -> float:
        return (self.speed_bytes_per_sec * 8) / (1024 * 1024)

    @property
    def is_stalled(self) -> bool:
        """Returns true if active chunks exist but no data received for > 8 seconds."""
        if self.active_chunks > 0 and (time.time() - self.last_byte_time > 8.0):
            return True
        return False


class QoSWatchdog:
    """Coordinates multi-adapter health and failure detection."""

    def __init__(self):
        self.adapters: Dict[str, AdapterMetrics] = {}

    def register(self, name: str, ip_address: str, kind: str) -> AdapterMetrics:
        metric = AdapterMetrics(name=name, ip_address=ip_address, kind=kind)
        self.adapters[ip_address] = metric
        return metric

    def get(self, ip_address: str) -> AdapterMetrics:
        return self.adapters[ip_address]

    def check_health(self) -> Dict[str, bool]:
        """Audits all adapters and returns active health status."""
        status = {}
        for ip, metric in self.adapters.items():
            if metric.is_stalled:
                metric.is_healthy = False
            status[ip] = metric.is_healthy
        return status

"""Parallel multi-adapter bonding download engine.

Distributes file chunks across verified network adapters concurrently,
tracks live per-adapter throughput, and writes to disk via sparse file mapping.
"""

from __future__ import annotations

import concurrent.futures
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional
from .adapter import NetworkAdapter, get_bonded_pool
from .client import BoundRangeClient, ProbeResult, probe_url
from .qos import QoSWatchdog
from .sparse import SparseFileWriter


@dataclass
class DownloadChunk:
    chunk_id: int
    start_byte: int
    end_byte: int
    retries: int = 0


@dataclass
class ProgressUpdate:
    total_bytes: int
    downloaded_bytes: int
    speed_bytes_sec: float
    adapter_speeds: dict[str, float]  # ip -> bytes/sec
    is_completed: bool = False
    filename: str = ""


class BondingDownloadEngine:
    """Orchestrates multi-adapter parallel downloads."""

    def __init__(
        self,
        url: str,
        output_path: Optional[str | Path] = None,
        adapters: Optional[List[NetworkAdapter]] = None,
        chunk_size: int = 8 * 1024 * 1024,  # 8 MB default chunk
        workers_per_adapter: int = 2,
        custom_headers: Optional[dict[str, str]] = None,
        cookie: Optional[str] = None,
        referer: Optional[str] = None,
    ):
        self.url = url
        self.chunk_size = chunk_size
        self.workers_per_adapter = workers_per_adapter
        self.adapters = adapters or get_bonded_pool()
        self.output_path = output_path
        self.custom_headers = custom_headers or {}
        self.cookie = cookie
        self.referer = referer

        self.probe: Optional[ProbeResult] = None
        self.watchdog = QoSWatchdog()
        self._cancel_event = threading.Event()
        self._completed_bytes = 0
        self._bytes_lock = threading.Lock()
        self._start_time = 0.0

    def _prepare(self) -> ProbeResult:
        """Probes the remote server and sets output path."""
        self.probe = probe_url(
            self.url,
            custom_headers=self.custom_headers,
            cookie=self.cookie,
            referer=self.referer,
        )
        if not self.cookie and self.probe.session_cookies:
            self.cookie = self.probe.session_cookies
        if not self.referer and self.probe.referer:
            self.referer = self.probe.referer

        if not self.output_path:
            self.output_path = Path.cwd() / self.probe.filename
        else:
            self.output_path = Path(self.output_path)
            if self.output_path.is_dir():
                self.output_path = self.output_path / self.probe.filename

        return self.probe

    def execute(
        self,
        progress_callback: Optional[Callable[[ProgressUpdate], None]] = None,
    ) -> Path:
        """Executes the bonded download and returns final output file path."""
        probe = self._prepare()
        target_path = Path(self.output_path)
        self._start_time = time.time()

        # If file doesn't support ranges or file size unknown, fall back to single-stream
        if not probe.accepts_ranges or probe.total_bytes <= 0:
            return self._execute_single_stream(probe, target_path, progress_callback)

        return self._execute_bonded_parallel(probe, target_path, progress_callback)

    def _execute_single_stream(
        self,
        probe: ProbeResult,
        target_path: Path,
        progress_callback: Optional[Callable[[ProgressUpdate], None]],
    ) -> Path:
        """Fallback for non-range servers."""
        adapter = self.adapters[0] if self.adapters else None
        local_ip = adapter.ip_address if adapter else "0.0.0.0"
        client = BoundRangeClient(
            local_ip=local_ip,
            custom_headers=self.custom_headers,
            cookie=self.cookie,
            referer=self.referer,
        )

        target_path.parent.mkdir(parents=True, exist_ok=True)
        downloaded = 0

        with open(target_path, "wb") as f:
            for chunk in client.fetch_range(probe.url, 0, probe.total_bytes - 1 if probe.total_bytes > 0 else 0):
                f.write(chunk)
                downloaded += len(chunk)
                if progress_callback:
                    elapsed = max(0.001, time.time() - self._start_time)
                    progress_callback(
                        ProgressUpdate(
                            total_bytes=probe.total_bytes or downloaded,
                            downloaded_bytes=downloaded,
                            speed_bytes_sec=downloaded / elapsed,
                            adapter_speeds={local_ip: downloaded / elapsed},
                            is_completed=False,
                            filename=target_path.name,
                        )
                    )

        if progress_callback:
            elapsed = max(0.001, time.time() - self._start_time)
            progress_callback(
                ProgressUpdate(
                    total_bytes=downloaded,
                    downloaded_bytes=downloaded,
                    speed_bytes_sec=downloaded / elapsed,
                    adapter_speeds={local_ip: downloaded / elapsed},
                    is_completed=True,
                    filename=target_path.name,
                )
            )

        return target_path

    def _execute_bonded_parallel(
        self,
        probe: ProbeResult,
        target_path: Path,
        progress_callback: Optional[Callable[[ProgressUpdate], None]],
    ) -> Path:
        """Executes multi-socket bonded chunk downloading."""
        total_bytes = probe.total_bytes
        chunk_queue: queue.Queue[DownloadChunk] = queue.Queue()

        # Split into chunks
        offset = 0
        chunk_id = 0
        while offset < total_bytes:
            end = min(offset + self.chunk_size - 1, total_bytes - 1)
            chunk_queue.put(DownloadChunk(chunk_id=chunk_id, start_byte=offset, end_byte=end))
            offset = end + 1
            chunk_id += 1

        total_chunks = chunk_id

        # Register adapters in QoS watchdog
        for a in self.adapters:
            self.watchdog.register(a.name, a.ip_address, a.kind)

        # Pre-allocate sparse file
        with SparseFileWriter(target_path, total_bytes) as writer:
            workers = []
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=len(self.adapters) * self.workers_per_adapter
            ) as executor:
                for adapter in self.adapters:
                    for _ in range(self.workers_per_adapter):
                        future = executor.submit(
                            self._worker_loop,
                            adapter,
                            chunk_queue,
                            writer,
                            probe.url,
                            progress_callback,
                        )
                        workers.append(future)

                # Wait for all workers or cancellation
                for future in concurrent.futures.as_completed(workers):
                    # Raise any unexpected exceptions
                    future.result()

        if progress_callback:
            elapsed = max(0.001, time.time() - self._start_time)
            progress_callback(
                ProgressUpdate(
                    total_bytes=total_bytes,
                    downloaded_bytes=total_bytes,
                    speed_bytes_sec=total_bytes / elapsed,
                    adapter_speeds={a.ip_address: self.watchdog.get(a.ip_address).speed_bytes_per_sec for a in self.adapters},
                    is_completed=True,
                    filename=target_path.name,
                )
            )

        return target_path

    def _worker_loop(
        self,
        adapter: NetworkAdapter,
        chunk_queue: queue.Queue[DownloadChunk],
        writer: SparseFileWriter,
        url: str,
        progress_callback: Optional[Callable[[ProgressUpdate], None]],
    ) -> None:
        """Worker thread dedicated to a specific network adapter."""
        client = BoundRangeClient(
            local_ip=adapter.ip_address,
            custom_headers=self.custom_headers,
            cookie=self.cookie,
            referer=self.referer,
        )
        metric = self.watchdog.get(adapter.ip_address)

        while not self._cancel_event.is_set():
            try:
                chunk = chunk_queue.get_nowait()
            except queue.Empty:
                break

            metric.active_chunks += 1
            chunk_data = bytearray()
            success = False

            try:
                def on_bytes(count: int):
                    metric.record_bytes(count)
                    with self._bytes_lock:
                        self._completed_bytes += count
                    if progress_callback:
                        elapsed = max(0.001, time.time() - self._start_time)
                        speeds = {
                            a.ip_address: self.watchdog.get(a.ip_address).speed_bytes_per_sec
                            for a in self.adapters
                        }
                        progress_callback(
                            ProgressUpdate(
                                total_bytes=self.probe.total_bytes,
                                downloaded_bytes=self._completed_bytes,
                                speed_bytes_sec=self._completed_bytes / elapsed,
                                adapter_speeds=speeds,
                                is_completed=False,
                                filename=self.output_path.name if self.output_path else "",
                            )
                        )

                for block in client.fetch_range(
                    url, chunk.start_byte, chunk.end_byte, on_bytes=on_bytes
                ):
                    chunk_data.extend(block)

                # Write directly into sparse file offset
                writer.write_chunk(chunk.start_byte, bytes(chunk_data))
                success = True

            except Exception as err:
                metric.record_error()
                chunk.retries += 1
                if chunk.retries < 5:
                    # Re-queue for another healthy adapter
                    chunk_queue.put(chunk)
                else:
                    self._cancel_event.set()
                    raise RuntimeError(f"Chunk {chunk.chunk_id} failed permanently: {err}")

            finally:
                metric.active_chunks -= 1
                chunk_queue.task_done()

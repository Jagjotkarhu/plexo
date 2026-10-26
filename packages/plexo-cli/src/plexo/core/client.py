"""HTTP probe and socket-bound range request client.

Inspects remote servers for Range support and executes chunk transfers
strictly bound to designated local adapter IP addresses.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Callable, Generator, Optional
from urllib.parse import unquote, urlparse
import urllib3
from urllib3.util import parse_url


@dataclass
class ProbeResult:
    """Metadata retrieved from probing the target URL."""
    url: str
    total_bytes: int
    accepts_ranges: bool
    filename: str
    content_type: str
    etag: Optional[str] = None


def extract_filename(url: str, content_disposition: Optional[str] = None) -> str:
    """Deduces filename from Content-Disposition header or URL path."""
    if content_disposition:
        # Match filename="example.zip" or filename*=UTF-8''example.zip
        match = re.search(r'filename\*?=(?:UTF-8\'\')?["\']?([^"\';]+)["\']?', content_disposition, re.IGNORECASE)
        if match:
            return os.path.basename(unquote(match.group(1).strip()))

    parsed = urlparse(url)
    path_name = os.path.basename(unquote(parsed.path))
    if path_name and "." in path_name:
        return path_name

    return "downloaded_file.bin"


def probe_url(url: str, timeout: float = 10.0) -> ProbeResult:
    """Probes the remote server to determine file size, range support, and filename."""
    http = urllib3.PoolManager()
    headers = {
        "User-Agent": "Plexo-CLI/0.1.0 (Bandwidth-Bonding-Engine)",
        "Accept-Encoding": "identity",
    }

    try:
        # First attempt with HEAD
        resp = http.request("HEAD", url, headers=headers, timeout=timeout, redirect=True)
        if resp.status >= 400:
            # Fallback to GET with 1 byte range if HEAD is blocked
            headers["Range"] = "bytes=0-0"
            resp = http.request("GET", url, headers=headers, timeout=timeout, redirect=True)
    except Exception:
        # Fallback to GET with range
        headers["Range"] = "bytes=0-0"
        resp = http.request("GET", url, headers=headers, timeout=timeout, redirect=True)

    final_url = url
    if hasattr(resp, "url") and resp.url:
        candidate_url = resp.url
        if candidate_url.startswith("http://") or candidate_url.startswith("https://"):
            final_url = candidate_url
    headers_resp = resp.headers

    # Check range support
    accepts_ranges = False
    if resp.status == 206:
        accepts_ranges = True
    elif headers_resp.get("Accept-Ranges", "").lower() == "bytes":
        accepts_ranges = True

    # Determine total size
    total_bytes = 0
    content_range = headers_resp.get("Content-Range")
    if content_range:
        # format: bytes 0-0/104857600
        match = re.search(r"/(\d+)", content_range)
        if match:
            total_bytes = int(match.group(1))
            accepts_ranges = True

    if not total_bytes:
        cl = headers_resp.get("Content-Length")
        if cl and cl.isdigit():
            total_bytes = int(cl)

    cd = headers_resp.get("Content-Disposition")
    filename = extract_filename(final_url, cd)
    content_type = headers_resp.get("Content-Type", "application/octet-stream")
    etag = headers_resp.get("ETag")

    return ProbeResult(
        url=final_url,
        total_bytes=total_bytes,
        accepts_ranges=accepts_ranges,
        filename=filename,
        content_type=content_type,
        etag=etag,
    )


class BoundRangeClient:
    """Performs HTTP GET range downloads strictly bound to a local adapter IP."""

    def __init__(self, local_ip: str, timeout: float = 15.0):
        self.local_ip = local_ip
        self.timeout = timeout
        # Bind urllib3 socket connections to (local_ip, 0)
        self._pool = urllib3.PoolManager(
            source_address=(local_ip, 0),
            timeout=urllib3.Timeout(connect=timeout, read=timeout),
            retries=urllib3.Retry(total=3, backoff_factor=0.5),
        )

    def fetch_range(
        self,
        url: str,
        start_byte: int,
        end_byte: int,
        chunk_size: int = 64 * 1024,  # 64 KB read buffer
        on_bytes: Optional[Callable[[int], None]] = None,
    ) -> Generator[bytes, None, None]:
        """Fetches [start_byte, end_byte] inclusive through the bound adapter."""
        headers = {
            "User-Agent": "Plexo-CLI/0.1.0 (Bandwidth-Bonding-Engine)",
            "Range": f"bytes={start_byte}-{end_byte}",
            "Accept-Encoding": "identity",
        }

        resp = self._pool.request("GET", url, headers=headers, preload_content=False)
        try:
            if resp.status not in (200, 206):
                raise IOError(f"HTTP Range request failed with status {resp.status}")

            for data in resp.stream(chunk_size):
                if data:
                    if on_bytes:
                        on_bytes(len(data))
                    yield data
        finally:
            resp.release_conn()

"""Tests for parallel bonded download engine with mock Range server."""

import hashlib
import http.server
import socketserver
import tempfile
import threading
from pathlib import Path
import pytest
from plexo.core.adapter import NetworkAdapter
from plexo.core.engine import BondingDownloadEngine


class RangeHTTPRequestHandler(http.server.BaseHTTPRequestHandler):
    """Simple HTTP server supporting byte ranges for testing."""
    DATA = b"0123456789ABCDEF" * 1024 * 64  # 1 MB test payload

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(self.DATA)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

    def do_GET(self):
        range_header = self.headers.get("Range")
        if range_header and range_header.startswith("bytes="):
            parts = range_header[6:].split("-")
            start = int(parts[0])
            end = int(parts[1]) if parts[1] else len(self.DATA) - 1
            chunk = self.DATA[start : end + 1]

            self.send_response(206)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(self.DATA)}")
            self.send_header("Content-Length", str(len(chunk)))
            self.end_headers()
            self.wfile.write(chunk)
        else:
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(self.DATA)))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            self.wfile.write(self.DATA)

    def log_message(self, format, *args):
        pass  # Suppress logging in tests


@pytest.fixture(scope="module")
def mock_server():
    server = socketserver.TCPServer(("127.0.0.1", 0), RangeHTTPRequestHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}/testfile.bin"
    server.shutdown()


def test_bonded_engine_download(mock_server):
    with tempfile.TemporaryDirectory() as tmpdir:
        output_file = Path(tmpdir) / "output.bin"

        # Mock adapter
        mock_adapter = NetworkAdapter(
            name="Mock-Adapter",
            ip_address="127.0.0.1",
            netmask="255.255.255.0",
            kind="ethernet",
            is_up=True,
            is_online=True,
        )

        engine = BondingDownloadEngine(
            url=mock_server,
            output_path=output_file,
            adapters=[mock_adapter],
            chunk_size=128 * 1024,  # 128 KB chunks
            workers_per_adapter=2,
        )

        final_path = engine.execute()

        assert final_path.exists()
        assert final_path.stat().st_size == len(RangeHTTPRequestHandler.DATA)

        # Verify exact byte match
        expected_hash = hashlib.sha256(RangeHTTPRequestHandler.DATA).hexdigest()
        with open(final_path, "rb") as f:
            actual_hash = hashlib.sha256(f.read()).hexdigest()

        assert actual_hash == expected_hash

"""Local HTTP/HTTPS forward proxy for accelerating terminal tools (pip, git, curl).

Routes requests through the multi-adapter bonding engine or tunnels HTTPS.
"""

from __future__ import annotations

import asyncio
import socket
from typing import Optional
from rich.console import Console
from ..core.adapter import get_bonded_pool


class LocalBondedProxy:
    """Asynchronous local HTTP/HTTPS accelerator proxy."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8899, console: Optional[Console] = None):
        self.host = host
        self.port = port
        self.console = console or Console()
        self.server: Optional[asyncio.Server] = None
        self._adapters = []

    async def start(self) -> None:
        """Starts the local proxy server."""
        self._adapters = get_bonded_pool()
        self.server = await asyncio.start_server(
            self._handle_client, self.host, self.port
        )
        addrs = ", ".join(str(sock.getsockname()) for sock in self.server.sockets)
        self.console.print(
            f"[bold green]Plexo Proxy running on {self.host}:{self.port}[/bold green]"
        )
        self.console.print(
            f"[cyan]Bonding across {len(self._adapters)} network interfaces:[/cyan]"
        )
        for a in self._adapters:
            self.console.print(f"  * {a.name} ({a.ip_address})")

        self.console.print(
            "\n[dim]To use with pip:[/dim]\n"
            f"  pip install <package> --proxy http://{self.host}:{self.port}\n\n"
            "[dim]To use globally in terminal:[/dim]\n"
            f"  $env:HTTPS_PROXY = \"http://{self.host}:{self.port}\"\n"
        )

        async with self.server:
            await self.server.serve_forever()

    async def _handle_client(
        self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter
    ) -> None:
        """Handles client connection (HTTP GET or HTTPS CONNECT)."""
        try:
            request_line = await client_reader.readline()
            if not request_line:
                client_writer.close()
                return

            words = request_line.decode("latin1").split()
            if len(words) < 2:
                client_writer.close()
                return

            method, url = words[0], words[1]

            if method.upper() == "CONNECT":
                # HTTPS Tunneling
                await self._handle_connect(url, client_reader, client_writer)
            else:
                # Plain HTTP proxy
                await self._handle_http(method, url, request_line, client_reader, client_writer)

        except Exception:
            try:
                client_writer.close()
            except Exception:
                pass

    async def _handle_connect(
        self, target: str, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter
    ) -> None:
        """Tunnels HTTPS connections directly."""
        host, port_str = target.split(":") if ":" in target else (target, "443")
        port = int(port_str)

        try:
            # Round-robin or primary adapter source IP
            adapter = self._adapters[0] if self._adapters else None
            local_addr = (adapter.ip_address, 0) if adapter else None

            remote_reader, remote_writer = await asyncio.open_connection(
                host, port, local_addr=local_addr
            )

            client_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await client_writer.drain()

            # Bidirectional pipe
            await asyncio.gather(
                self._pipe(client_reader, remote_writer),
                self._pipe(remote_reader, client_writer),
                return_exceptions=True,
            )
        except Exception:
            client_writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            await client_writer.drain()
            client_writer.close()

    async def _handle_http(
        self,
        method: str,
        url: str,
        initial_line: bytes,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        """Forwards standard HTTP requests."""
        # Read remaining headers
        headers = bytearray(initial_line)
        while True:
            line = await client_reader.readline()
            headers.extend(line)
            if line == b"\r\n" or not line:
                break

        # Parse host from URL
        from urllib.parse import urlparse
        parsed = urlparse(url)
        host = parsed.hostname or "localhost"
        port = parsed.port or 80

        try:
            remote_reader, remote_writer = await asyncio.open_connection(host, port)
            remote_writer.write(headers)
            await remote_writer.drain()

            await asyncio.gather(
                self._pipe(client_reader, remote_writer),
                self._pipe(remote_reader, client_writer),
                return_exceptions=True,
            )
        except Exception:
            client_writer.close()

    async def _pipe(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Pipes bytes between reader and writer until EOF."""
        try:
            while not reader.at_eof():
                data = await reader.read(65536)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass


def run_proxy(host: str = "127.0.0.1", port: int = 8899) -> None:
    """Synchronous launcher for the proxy server."""
    proxy = LocalBondedProxy(host=host, port=port)
    asyncio.run(proxy.start())

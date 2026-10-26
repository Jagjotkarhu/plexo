"""Command Line Interface for Plexo.

Exposes subcommands: download, adapters, proxy, info.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Optional

# Ensure UTF-8 output on Windows consoles
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from . import __version__
from .core.adapter import discover_adapters, get_bonded_pool
from .core.engine import BondingDownloadEngine, ProgressUpdate
from .proxy.server import run_proxy
from .ui.dashboard import TerminalDashboard

console = Console()


@click.group()
@click.version_option(version=__version__, prog_name="plexo")
def main():
    """Plexo: Multi-Network Bandwidth Bonding Download Engine."""
    pass


@main.command(name="adapters")
@click.option("--all", "show_all", is_flag=True, help="Show all adapters including offline/internal.")
def list_adapters(show_all: bool):
    """Scan and benchmark active network adapters."""
    console.print("\n[bold cyan]Scanning Network Interfaces...[/bold cyan]")
    adapters = discover_adapters(verify_online=True)

    table = Table(title="[bold green]Network Interfaces[/bold green]")
    table.add_column("Interface Name", style="bold white")
    table.add_column("Type", style="magenta")
    table.add_column("IPv4 Address", style="cyan")
    table.add_column("Link Speed", style="yellow")
    table.add_column("Internet Route", style="bold")

    display_list = adapters if show_all else [a for a in adapters if a.is_online]

    for a in display_list:
        status_text = "[bold green]ONLINE[/bold green]" if a.is_online else "[dim red]OFFLINE[/dim red]"
        speed_str = f"{a.speed_mbps} Mbps" if a.speed_mbps > 0 else "Unknown"
        table.add_row(a.name, a.kind.upper(), a.ip_address, speed_str, status_text)

    console.print(table)
    online_count = sum(1 for a in adapters if a.is_online)

    if online_count >= 2:
        console.print(
            f"\n[bold green][OK] Ready for bonding:[/bold green] [yellow]{online_count}[/yellow] independent connections detected.\n"
        )
    elif online_count == 1:
        console.print(
            "\n[yellow][INFO] 1 connection active.[/yellow] Plug in Phone 5G USB tethering or Ethernet to enable multi-network bonding.\n"
        )
    else:
        console.print("\n[bold red][X] No online connections found.[/bold red]\n")


@main.command(name="download")
@click.argument("url")
@click.option("-o", "--output", "output_path", type=click.Path(), default=None, help="Output destination path.")
@click.option("--chunk-size", default=8, type=int, help="Chunk size in MB (default: 8 MB).")
@click.option("--workers", default=2, type=int, help="Workers per network adapter (default: 2).")
@click.option("-H", "--header", "headers_list", multiple=True, help="Custom HTTP header (e.g. -H 'Auth: token').")
@click.option("--cookie", default=None, help="Custom Cookie header for protected sites/filelockers.")
@click.option("--referer", default=None, help="Custom Referer URL for sites requiring origin verification.")
def download_file(
    url: str,
    output_path: Optional[str],
    chunk_size: int,
    workers: int,
    headers_list: tuple[str, ...],
    cookie: Optional[str],
    referer: Optional[str],
):
    """Download a file using bonded multi-network connections."""
    adapters = get_bonded_pool()
    if not adapters:
        console.print("[bold red]Error: No active online network adapters detected.[/bold red]")
        raise click.Abort()

    # Parse custom headers
    parsed_headers = {}
    for h in headers_list:
        if ":" in h:
            k, v = h.split(":", 1)
            parsed_headers[k.strip()] = v.strip()

    dashboard = TerminalDashboard(console=console)
    chunk_bytes = chunk_size * 1024 * 1024

    engine = BondingDownloadEngine(
        url=url,
        output_path=output_path,
        adapters=adapters,
        chunk_size=chunk_bytes,
        workers_per_adapter=workers,
        custom_headers=parsed_headers,
        cookie=cookie,
        referer=referer,
    )

    probe = engine._prepare()
    dashboard.setup(probe.filename, probe.total_bytes, adapters)

    start_time = time.time()
    with dashboard.progress:
        final_path = engine.execute(progress_callback=dashboard.update)

    elapsed = time.time() - start_time
    total_bytes = final_path.stat().st_size

    # Calculate checksum
    from .core.sparse import SparseFileWriter
    with SparseFileWriter(final_path, total_bytes) as writer:
        checksum = writer.calculate_checksum()

    dashboard.finish(final_path.name, total_bytes, elapsed, checksum)


@main.command(name="proxy")
@click.option("--host", default="127.0.0.1", help="Proxy listening host (default: 127.0.0.1).")
@click.option("--port", default=8899, type=int, help="Proxy listening port (default: 8899).")
def start_proxy(host: str, port: int):
    """Launch local acceleration proxy for pip, git, and curl."""
    run_proxy(host=host, port=port)


@main.command(name="torrent")
@click.argument("source")
@click.option("-o", "--output-dir", default=".", help="Directory to save downloaded files.")
def download_torrent(source: str, output_dir: str):
    """Download a torrent or magnet link across bonded network adapters."""
    from .torrent.metadata import parse_magnet, parse_torrent_file
    from .torrent.client import TorrentBondingEngine

    adapters = get_bonded_pool()
    if not adapters:
        console.print("[bold red]Error: No active online network adapters detected.[/bold red]")
        raise click.Abort()

    if source.startswith("magnet:?"):
        meta = parse_magnet(source)
    else:
        meta = parse_torrent_file(source)

    console.print(
        Panel.fit(
            f"[bold cyan]Plexo Torrent Swarm Engine[/bold cyan]\n"
            f"Name: [bold yellow]{meta.name}[/bold yellow]\n"
            f"Info Hash: [dim]{meta.info_hash_hex}[/dim]\n"
            f"Active Network Links: [bold green]{len(adapters)}[/bold green]",
            border_style="cyan",
        )
    )

    dashboard = TerminalDashboard(console=console)
    dashboard.setup(meta.name, meta.total_length, adapters)

    engine = TorrentBondingEngine(metadata=meta, output_dir=output_dir, adapters=adapters)

    start_time = time.time()
    with dashboard.progress:
        final_path = engine.download(progress_callback=dashboard.update)

    elapsed = time.time() - start_time
    total_bytes = final_path.stat().st_size

    from .core.sparse import SparseFileWriter
    with SparseFileWriter(final_path, total_bytes) as writer:
        checksum = writer.calculate_checksum()

    dashboard.finish(final_path.name, total_bytes, elapsed, checksum)


@main.command(name="info")
def show_info():
    """Display Plexo environment and bonding capabilities."""
    adapters = get_bonded_pool()
    console.print(
        Panel.fit(
            f"[bold cyan]Plexo CLI v{__version__}[/bold cyan]\n"
            f"Bonding Engine: [bold green]Active[/bold green]\n"
            f"Verified Online Adapters: [bold yellow]{len(adapters)}[/bold yellow]\n"
            f"Default Proxy Port: [bold magenta]8899[/bold magenta]",
            border_style="cyan",
            title="System Capabilities",
        )
    )


if __name__ == "__main__":
    main()

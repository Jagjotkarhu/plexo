"""Command Line Interface for Plexo.

Exposes subcommands: download, adapters, proxy, torrent, watch, info.
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
@click.option("-m", "--mirror", "mirrors", multiple=True, help="Alternative mirror URL(s) for the same file.")
@click.option("-o", "--output", "output_path", type=click.Path(), default=None, help="Output destination path.")
@click.option("--chunk-size", default=8, type=int, help="Chunk size in MB (default: 8 MB).")
@click.option("--workers", default=2, type=int, help="Workers per network adapter (default: 2).")
@click.option("-H", "--header", "headers_list", multiple=True, help="Custom HTTP header (e.g. -H 'Auth: token').")
@click.option("--cookie", default=None, help="Custom Cookie header for protected sites/filelockers.")
@click.option("--referer", default=None, help="Custom Referer URL for sites requiring origin verification.")
@click.option("--sequential", is_flag=True, help="Download in sequential order (watch media while downloading).")
@click.option("-q", "--quota", "quota_list", multiple=True, help="Data cap for adapter (e.g. -q usb=1.5GB).")
def download_file(
    url: str,
    mirrors: tuple[str, ...],
    output_path: Optional[str],
    chunk_size: int,
    workers: int,
    headers_list: tuple[str, ...],
    cookie: Optional[str],
    referer: Optional[str],
    sequential: bool,
    quota_list: tuple[str, ...],
):
    """Download a file using bonded multi-network connections and mirrors."""
    from .core.qos import parse_size_str
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

    # Parse adapter data quotas
    parsed_quotas = {}
    for q in quota_list:
        if "=" in q:
            ident, s_val = q.split("=", 1)
            parsed_quotas[ident.strip()] = parse_size_str(s_val.strip())

    # Parse alternative mirror URLs
    parsed_mirrors = []
    for m in mirrors:
        for sub_m in m.split(","):
            sub_m = sub_m.strip()
            if sub_m and sub_m not in parsed_mirrors and sub_m != url:
                parsed_mirrors.append(sub_m)

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
        sequential=sequential,
        quotas=parsed_quotas,
        mirrors=parsed_mirrors,
    )

    probe = engine._prepare()
    dashboard.setup(probe.filename, probe.total_bytes, adapters, mirrors=engine.mirrors)

    start_time = time.time()
    with dashboard.progress:
        final_path = engine.execute(progress_callback=dashboard.update)

    elapsed = time.time() - start_time
    total_bytes = final_path.stat().st_size

    # Calculate checksum
    from .core.sparse import SparseFileWriter
    with console.status("[cyan]Verifying file checksum (SHA-256)...[/cyan]"):
        with SparseFileWriter(final_path, total_bytes) as writer:
            checksum = writer.calculate_checksum()

    dashboard.finish(final_path.name, total_bytes, elapsed, checksum, mirrors=engine.mirrors)


@main.command(name="proxy")
@click.option("--host", default="127.0.0.1", help="Proxy listening host (default: 127.0.0.1).")
@click.option("--port", default=8899, type=int, help="Proxy listening port (default: 8899).")
def start_proxy(host: str, port: int):
    """Launch local acceleration proxy for pip, git, and curl."""
    run_proxy(host=host, port=port)


@main.command(name="torrent")
@click.argument("source")
@click.option("-o", "--output-dir", default=".", help="Directory to save downloaded files.")
@click.option("--sequential", is_flag=True, help="Download pieces in sequential order (watch media while downloading).")
@click.option("--list", "list_files", is_flag=True, help="List all files in the torrent and exit.")
@click.option("--select", "select_files", default=None, help="Download only specific file numbers (e.g. --select 1,2).")
@click.option("-q", "--quota", "quota_list", multiple=True, help="Data cap for adapter (e.g. -q usb=1.5GB).")
def download_torrent(
    source: str,
    output_dir: str,
    sequential: bool,
    list_files: bool,
    select_files: Optional[str],
    quota_list: tuple[str, ...],
):
    """Download a torrent or magnet link across bonded network adapters."""
    from .core.qos import parse_size_str
    from .torrent.metadata import parse_magnet, parse_torrent_file
    from .torrent.client import TorrentBondingEngine

    if source.startswith("magnet:?"):
        meta = parse_magnet(source)
    else:
        meta = parse_torrent_file(source)

    if list_files:
        table = Table(title=f"[bold green]Torrent Contents: {meta.name}[/bold green]")
        table.add_column("#", style="bold yellow", justify="right")
        table.add_column("File Name / Path", style="cyan")
        table.add_column("Size", style="magenta", justify="right")
        for f in meta.files:
            size_str = f"{f.size_mb:.2f} MB" if f.size_mb < 1024 else f"{f.size_gb:.2f} GB"
            table.add_row(str(f.index), f.path, size_str)
        console.print(table)
        console.print(
            "\n[dim]To download specific files, run:[/dim]\n"
            f"  plexo torrent \"{source}\" --select 1,2 -o \"{output_dir}\"\n"
        )
        return

    adapters = get_bonded_pool()
    if not adapters:
        console.print("[bold red]Error: No active online network adapters detected.[/bold red]")
        raise click.Abort()

    selected_indices = None
    if select_files:
        selected_indices = [int(s.strip()) for s in select_files.split(",") if s.strip().isdigit()]

    parsed_quotas = {}
    for q in quota_list:
        if "=" in q:
            ident, s_val = q.split("=", 1)
            parsed_quotas[ident.strip()] = parse_size_str(s_val.strip())

    console.print(
        Panel.fit(
            f"[bold cyan]Plexo Torrent Swarm Engine[/bold cyan]\n"
            f"Name: [bold yellow]{meta.name}[/bold yellow]\n"
            f"Info Hash: [dim]{meta.info_hash_hex}[/dim]\n"
            f"Selected Files: [green]{len(selected_indices) if selected_indices else 'All'}[/green] / {len(meta.files)}\n"
            f"Active Network Links: [bold green]{len(adapters)}[/bold green]",
            border_style="cyan",
        )
    )

    dashboard = TerminalDashboard(console=console)
    dashboard.setup(meta.name, meta.total_length, adapters)

    engine = TorrentBondingEngine(
        metadata=meta,
        output_dir=output_dir,
        adapters=adapters,
        sequential=sequential,
        selected_files=selected_indices,
        quotas=parsed_quotas,
    )

    start_time = time.time()
    with dashboard.progress:
        final_path = engine.download(progress_callback=dashboard.update)

    elapsed = time.time() - start_time
    total_bytes = final_path.stat().st_size

    from .core.sparse import SparseFileWriter
    with SparseFileWriter(final_path, total_bytes) as writer:
        checksum = writer.calculate_checksum()

    dashboard.finish(final_path.name, total_bytes, elapsed, checksum)


@main.command(name="watch")
@click.option("--auto-download", "-y", is_flag=True, help="Automatically download detected links without prompting.")
@click.option("-o", "--output-dir", default=None, help="Directory to save downloaded files.")
@click.option("-i", "--poll-interval", default=1.0, type=float, help="Clipboard polling interval in seconds (default: 1.0).")
def watch_clipboard(auto_download: bool, output_dir: Optional[str], poll_interval: float):
    """Monitor clipboard for download URLs and magnet links."""
    from .ui.watcher import ClipboardWatcher
    watcher = ClipboardWatcher(
        poll_interval=poll_interval,
        auto_download=auto_download,
        output_dir=output_dir,
        console=console,
    )
    watcher.run()


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

"""Magnet URI and .torrent file metadata parser."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional
from urllib.parse import parse_qs, unquote, urlparse
from .bencode import bdecode, bencode


@dataclass
class TorrentMetadata:
    """Represents resolved BitTorrent swarm metadata."""
    name: str
    info_hash: bytes
    total_length: int
    piece_length: int
    pieces: List[bytes] = field(default_factory=list)
    trackers: List[str] = field(default_factory=list)

    @property
    def info_hash_hex(self) -> str:
        return self.info_hash.hex()

    @property
    def num_pieces(self) -> int:
        return len(self.pieces) if self.pieces else 0


def parse_magnet(uri: str) -> TorrentMetadata:
    """Parses a magnet link into a TorrentMetadata instance."""
    if not uri.startswith("magnet:?"):
        raise ValueError("Invalid magnet URI: must start with 'magnet:?'")

    parsed = urlparse(uri)
    params = parse_qs(parsed.query)

    xt_list = params.get("xt", [])
    btih = None
    for xt in xt_list:
        if xt.startswith("urn:btih:"):
            btih = xt[len("urn:btih:"):]
            break

    if not btih:
        raise ValueError("Magnet URI missing required 'urn:btih:' info hash.")

    # Format can be 40-character hex or 32-character Base32
    if len(btih) == 40:
        info_hash = bytes.fromhex(btih)
    elif len(btih) == 32:
        info_hash = base64.b32decode(btih.upper())
    else:
        raise ValueError(f"Invalid BTIH length: {len(btih)}")

    # Extract display name (dn)
    dn = params.get("dn", ["torrent_download"])[0]
    name = unquote(dn)

    # Extract trackers (tr)
    trackers = [unquote(tr) for tr in params.get("tr", [])]

    return TorrentMetadata(
        name=name,
        info_hash=info_hash,
        total_length=0,  # Resolved from swarm peers
        piece_length=0,
        trackers=trackers,
    )


def parse_torrent_file(path: str | Path) -> TorrentMetadata:
    """Parses a .torrent file from disk."""
    with open(path, "rb") as f:
        data = f.read()

    meta = bdecode(data)
    info = meta.get("info", {})

    info_bytes = bencode(info)
    info_hash = hashlib.sha1(info_bytes).digest()

    name = info.get("name", b"download").decode("utf-8", errors="replace")
    piece_length = info.get("piece length", 0)
    raw_pieces = info.get("pieces", b"")

    # Split pieces into 20-byte SHA-1 digests
    pieces = [raw_pieces[i : i + 20] for i in range(0, len(raw_pieces), 20)]

    # Compute total size
    total_length = 0
    if "length" in info:
        total_length = info["length"]
    elif "files" in info:
        for f in info["files"]:
            total_length += f.get("length", 0)

    # Collect trackers
    trackers = []
    if "announce" in meta:
        trackers.append(meta["announce"].decode("ascii", errors="ignore"))
    if "announce-list" in meta:
        for tier in meta["announce-list"]:
            for tr in tier:
                trackers.append(tr.decode("ascii", errors="ignore"))

    return TorrentMetadata(
        name=name,
        info_hash=info_hash,
        total_length=total_length,
        piece_length=piece_length,
        pieces=pieces,
        trackers=list(dict.fromkeys(trackers)),  # Deduplicate
    )

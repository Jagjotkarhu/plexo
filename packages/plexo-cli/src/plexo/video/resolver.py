"""Universal media stream resolver and lexicographical quality fallback engine.

Extracts direct audio, video, and subtitle stream URLs across 1,800+ platforms using
yt-dlp in metadata-only mode without downloading bytes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class VideoTrack:
    """Represents an extracted video stream variant."""
    format_id: str
    ext: str
    resolution: str
    height: int
    width: int
    fps: int
    vcodec: str
    url: str
    filesize: int = 0
    tbr: float = 0.0
    has_audio: bool = False


@dataclass
class AudioTrack:
    """Represents an extracted audio stream variant."""
    format_id: str
    ext: str
    language: str
    abr: float  # Audio bitrate in kbps
    acodec: str
    url: str
    filesize: int = 0


@dataclass
class SubtitleTrack:
    """Represents an extracted subtitle or closed-caption track."""
    language: str
    name: str
    url: str
    ext: str = "vtt"
    is_auto_generated: bool = False


@dataclass
class VideoMetadata:
    """Complete resolved media manifest containing selected tracks and full variants."""
    title: str
    id: str
    webpage_url: str
    duration: int
    uploader: str
    thumbnail: str = ""
    selected_video: Optional[VideoTrack] = None
    selected_audio: Optional[AudioTrack] = None
    selected_subtitles: List[SubtitleTrack] = field(default_factory=list)
    all_videos: List[VideoTrack] = field(default_factory=list)
    all_audios: List[AudioTrack] = field(default_factory=list)
    all_subtitles: List[SubtitleTrack] = field(default_factory=list)


def parse_target_resolution(quality_str: str | int) -> int:
    """Normalizes quality input strings into vertical pixel heights.
    
    Examples:
        '1080p' -> 1080
        '1080'  -> 1080
        '720'   -> 720
        '4k'    -> 2160
        '2k'    -> 1440
        'best'  -> 999999
        'worst' -> 1
    """
    if isinstance(quality_str, int):
        return quality_str

    q = str(quality_str).strip().lower()
    if q in ("best", "max", "highest", ""):
        return 999999
    if q in ("worst", "min", "lowest"):
        return 1
    if q in ("4k", "2160", "2160p"):
        return 2160
    if q in ("2k", "1440", "1440p"):
        return 1440

    # Extract digits: '1080p60' -> 1080
    match = re.search(r"\d+", q)
    if match:
        return int(match.group(0))

    return 1080  # Default sensible target


def select_best_video_track(
    tracks: List[VideoTrack],
    target_height: int,
    prefer_combined: bool = False,
) -> Optional[VideoTrack]:
    """Selects the best video track using a soft-ceiling mathematical fallback.
    
    Algorithm:
    1. Filter tracks where height <= target_height.
    2. If candidates exist, select track maximizing: (height, fps, tbr).
    3. If no tracks meet height <= target_height (e.g. video is only 720p or 480p),
       gracefully select the highest available resolution without crashing.
    """
    if not tracks:
        return None

    valid_tracks = [t for t in tracks if t.url and t.height > 0]
    if not valid_tracks:
        return tracks[0] if tracks else None

    # Step 1: Candidates within target ceiling
    ceiling_candidates = [t for t in valid_tracks if t.height <= target_height]

    pool = ceiling_candidates if ceiling_candidates else valid_tracks

    # Score function: (height, fps, total_bitrate, prefer_isolated_video)
    def score(t: VideoTrack) -> Tuple[int, int, float, int]:
        combined_pref = 1 if (prefer_combined == t.has_audio) else 0
        return (t.height, t.fps or 0, t.tbr or 0.0, combined_pref)

    return max(pool, key=score)


def select_best_audio_track(
    tracks: List[AudioTrack],
    preferred_lang: Optional[str] = None,
) -> Optional[AudioTrack]:
    """Selects optimal audio track matching language and maximizing bitrate."""
    if not tracks:
        return None

    valid = [t for t in tracks if t.url]
    if not valid:
        return None

    # Language matching
    if preferred_lang:
        lang_clean = preferred_lang.strip().lower()
        lang_matches = [
            t for t in valid
            if t.language and (t.language.lower() == lang_clean or t.language.lower().startswith(lang_clean[:2]))
        ]
        if lang_matches:
            valid = lang_matches

    # Maximize bitrate and audio quality
    def score(t: AudioTrack) -> float:
        return t.abr or 0.0

    return max(valid, key=score)


def select_subtitles(
    tracks: List[SubtitleTrack],
    requested_langs: Optional[List[str]] = None,
) -> List[SubtitleTrack]:
    """Filters subtitles matching user's requested languages."""
    if not tracks or not requested_langs:
        return []

    selected: List[SubtitleTrack] = []
    seen_langs = set()

    for lang in requested_langs:
        l_clean = lang.strip().lower()
        for t in tracks:
            if t.language.lower() == l_clean or t.language.lower().startswith(l_clean[:2]):
                if t.language not in seen_langs:
                    selected.append(t)
                    seen_langs.add(t.language)
                    break

    return selected


def parse_manifest_dict(
    info: Dict[str, Any],
    quality: str | int = 1080,
    audio_only: bool = False,
    audio_lang: Optional[str] = None,
    sub_langs: Optional[List[str]] = None,
) -> VideoMetadata:
    """Parses raw yt-dlp info dictionary into typed VideoMetadata with score-based selection."""
    title = info.get("title") or "video"
    vid_id = info.get("id") or "unknown"
    webpage_url = info.get("webpage_url") or ""
    duration = int(info.get("duration") or 0)
    uploader = info.get("uploader") or info.get("channel") or "Unknown"
    thumbnail = info.get("thumbnail") or ""

    formats = info.get("formats") or []
    all_videos: List[VideoTrack] = []
    all_audios: List[AudioTrack] = []

    for f in formats:
        url = f.get("url")
        if not url:
            continue

        fid = str(f.get("format_id") or "")
        ext = str(f.get("ext") or "mp4")
        vcodec = str(f.get("vcodec") or "none")
        acodec = str(f.get("acodec") or "none")
        height = int(f.get("height") or 0)
        width = int(f.get("width") or 0)
        fps = int(f.get("fps") or 0)
        filesize = int(f.get("filesize") or f.get("filesize_approx") or 0)
        tbr = float(f.get("tbr") or 0.0)
        abr = float(f.get("abr") or 0.0)
        lang = str(f.get("language") or "")

        has_video = vcodec != "none" and height > 0
        has_audio = acodec != "none"

        if has_video:
            res_label = f"{height}p" if height else "unknown"
            all_videos.append(
                VideoTrack(
                    format_id=fid,
                    ext=ext,
                    resolution=res_label,
                    height=height,
                    width=width,
                    fps=fps,
                    vcodec=vcodec,
                    url=url,
                    filesize=filesize,
                    tbr=tbr,
                    has_audio=has_audio,
                )
            )

        if has_audio:
            all_audios.append(
                AudioTrack(
                    format_id=fid,
                    ext=ext,
                    language=lang,
                    abr=abr or tbr,
                    acodec=acodec,
                    url=url,
                    filesize=filesize,
                )
            )

    # Subtitles extraction
    all_subtitles: List[SubtitleTrack] = []
    subs_dict = info.get("subtitles") or {}
    auto_subs = info.get("automatic_captions") or {}

    for lang_code, sub_list in subs_dict.items():
        for s in sub_list:
            if s.get("url"):
                all_subtitles.append(
                    SubtitleTrack(
                        language=lang_code,
                        name=s.get("name") or lang_code,
                        url=s.get("url"),
                        ext=s.get("ext") or "vtt",
                        is_auto_generated=False,
                    )
                )

    for lang_code, sub_list in auto_subs.items():
        if lang_code not in subs_dict:  # Prefer manual over automatic
            for s in sub_list:
                if s.get("url"):
                    all_subtitles.append(
                        SubtitleTrack(
                            language=lang_code,
                            name=f"{lang_code} (auto)",
                            url=s.get("url"),
                            ext=s.get("ext") or "vtt",
                            is_auto_generated=True,
                        )
                    )

    # Selection logic
    target_height = parse_target_resolution(quality)
    selected_video: Optional[VideoTrack] = None
    if not audio_only:
        selected_video = select_best_video_track(all_videos, target_height)

    # Audio track selection
    selected_audio = select_best_audio_track(all_audios, preferred_lang=audio_lang)

    # If the chosen video track is already combined (has both video & audio),
    # and no explicit audio track was found or required, keep audio track optional
    if selected_video and selected_video.has_audio and not selected_audio:
        pass

    selected_subtitles = select_subtitles(all_subtitles, sub_langs)

    return VideoMetadata(
        title=title,
        id=vid_id,
        webpage_url=webpage_url,
        duration=duration,
        uploader=uploader,
        thumbnail=thumbnail,
        selected_video=selected_video,
        selected_audio=selected_audio,
        selected_subtitles=selected_subtitles,
        all_videos=all_videos,
        all_audios=all_audios,
        all_subtitles=all_subtitles,
    )


def resolve_video(
    url: str,
    quality: str | int = 1080,
    audio_only: bool = False,
    audio_lang: Optional[str] = None,
    sub_langs: Optional[List[str]] = None,
    custom_cookies: Optional[str] = None,
) -> VideoMetadata:
    """Resolves any platform video link (YouTube, Instagram, TikTok, Twitter) into direct stream URLs.
    
    Extracts metadata without downloading media bytes.
    """
    import yt_dlp

    ydl_opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": False,
    }

    if custom_cookies:
        ydl_opts["cookiefile"] = custom_cookies

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if info is None:
            raise ValueError(f"Failed to extract video metadata from: {url}")

        # If it's a playlist or multi-entry, pick the first entry
        if "entries" in info:
            entries = list(info["entries"])
            if not entries:
                raise ValueError("Playlist contains no playable entries.")
            info = entries[0]

    return parse_manifest_dict(
        info,
        quality=quality,
        audio_only=audio_only,
        audio_lang=audio_lang,
        sub_langs=sub_langs,
    )

import logging
from pathlib import Path
from typing import Iterator, Optional

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from .logging_setup import YtdlpLoggerAdapter

log = logging.getLogger(__name__)
_ytdlp_log = logging.getLogger("yt_dlp")
_ytdlp_adapter = YtdlpLoggerAdapter(_ytdlp_log)

METADATA_FIELDS = (
    "id",
    "title",
    "description",
    "channel",
    "channel_id",
    "channel_url",
    "uploader",
    "uploader_id",
    "uploader_url",
    "upload_date",
    "duration",
    "view_count",
    "like_count",
    "comment_count",
    "tags",
    "categories",
    "webpage_url",
    "thumbnail",
    "age_limit",
    "availability",
    "live_status",
    "was_live",
)


def _watch_url(entry: dict) -> Optional[str]:
    url = entry.get("url") or entry.get("webpage_url")
    if url and url.startswith("http"):
        return url
    vid = entry.get("id")
    if vid:
        return f"https://www.youtube.com/watch?v={vid}"
    return None


def iter_channel_videos(channel_url: str) -> Iterator[tuple[str, str]]:
    """Выдаёт (video_id, video_url) для всех видео канала.

    Использует flat-extract, чтобы не дёргать info_dict каждого видео на этом шаге.
    Канал может содержать вложенные плейлисты (tabs: Videos/Shorts/Live) —
    рекурсивно разворачиваем.
    """
    log.debug("listing channel: %s", channel_url)
    ydl_opts = {
        "extract_flat": "in_playlist",
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "logger": _ytdlp_adapter,
    }
    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(channel_url, download=False)

    if info is None:
        log.warning("yt-dlp returned no info for channel %s", channel_url)
        return
    log.debug("channel info keys: %s", list(info.keys()))

    def walk(node: dict) -> Iterator[tuple[str, str]]:
        entries = node.get("entries") or []
        for entry in entries:
            if entry is None:
                continue
            if entry.get("_type") == "playlist" or "entries" in entry:
                yield from walk(entry)
                continue
            vid = entry.get("id")
            url = _watch_url(entry)
            if vid and url:
                yield vid, url

    if info.get("_type") in ("playlist", "multi_video") or "entries" in info:
        yield from walk(info)
    else:
        vid = info.get("id")
        url = _watch_url(info)
        if vid and url:
            yield vid, url


def extract_metadata(video_url: str) -> dict:
    """Полный info_dict видео. Вызывается перед скачиванием."""
    log.debug("extract_metadata: %s", video_url)
    ydl_opts = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "logger": _ytdlp_adapter,
    }
    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(video_url, download=False)
    return _slim_metadata(info)


def _slim_metadata(info: dict) -> dict:
    return {k: info[k] for k in METADATA_FIELDS if info.get(k) is not None}


class DownloadFailed(Exception):
    pass


def download(
    video_url: str,
    dest_dir: Path,
    fmt: str,
    merge_ext: Optional[str] = None,
    audio_codec: Optional[str] = None,
    audio_quality: Optional[str] = None,
) -> tuple[Path, dict]:
    """Скачивает видео/аудио и возвращает (путь_к_файлу, метаданные).

    - merge_ext задаёт контейнер для мержа видео+аудио (video-режим).
    - audio_codec включает FFmpegExtractAudio-постпроцессор (audio-режим):
      исходный .webm/.m4a перекодируется в указанный кодек (mp3/m4a/…).
      Без этого OBSC получает .webm (аудио-only) и пытается извлекать кадры.

    Кидает DownloadFailed при ошибках yt-dlp.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    log.info(
        "download start: %s (fmt=%s, merge=%s, audio_codec=%s)",
        video_url, fmt, merge_ext, audio_codec,
    )
    ydl_opts = {
        "format": fmt,
        "outtmpl": str(dest_dir / "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "restrictfilenames": True,
        "logger": _ytdlp_adapter,
    }
    if merge_ext:
        ydl_opts["merge_output_format"] = merge_ext
    expected_ext: Optional[str] = None
    if audio_codec:
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": audio_codec,
            "preferredquality": audio_quality or "192",
        }]
        expected_ext = audio_codec
    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=True)
    except DownloadError as e:
        log.warning("yt-dlp DownloadError for %s: %s", video_url, e)
        raise DownloadFailed(str(e)) from e

    if info is None:
        raise DownloadFailed("yt-dlp returned no info")

    path = _resolve_downloaded_path(info, dest_dir, expected_ext)
    if path is None or not path.exists():
        raise DownloadFailed("downloaded file not found on disk")

    size_mb = path.stat().st_size / (1024 * 1024)
    log.info(
        "download ok: %s -> %s (%.1f MB, format_id=%s, height=%s)",
        video_url, path, size_mb, info.get("format_id"), info.get("height"),
    )
    return path, _slim_metadata(info)


def _resolve_downloaded_path(
    info: dict,
    dest_dir: Path,
    expected_ext: Optional[str] = None,
) -> Optional[Path]:
    vid = info.get("id")
    # Если ожидаем конкретное расширение (postprocessor перекодировал),
    # ищем именно его — filepath в info указывает на файл ДО постпроцессора.
    if vid and expected_ext:
        candidate = dest_dir / f"{vid}.{expected_ext}"
        if candidate.exists():
            return candidate

    requested = info.get("requested_downloads") or []
    if requested:
        fp = requested[0].get("filepath")
        if fp and Path(fp).exists():
            return Path(fp)

    if vid:
        matches = sorted(dest_dir.glob(f"{vid}.*"))
        # Предпочитаем итоговый файл, а не промежуточные .f<format>.*.
        finals = [m for m in matches if ".f" not in m.stem[len(vid):]]
        if finals:
            return finals[0]
        if matches:
            return matches[0]
    return None

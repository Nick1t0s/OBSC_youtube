import json
import logging
import sys
from pathlib import Path

import yaml

from scraper.db import Database
from scraper.logging_setup import configure_logging
from scraper.obsc_client import ObscClient
from scraper.pipeline import Pipeline
from scraper.youtube import iter_channel_videos

log = logging.getLogger("scraper.main")


def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_channels(path: Path, default_mode: str) -> list[dict]:
    """Парсит channels.json. Каждая запись — либо URL-строка,
    либо объект {"url": ..., "mode": "video"|"audio"}.
    Возвращает список dict с ключами url, mode.
    """
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a JSON list")
    channels: list[dict] = []
    for entry in data:
        if isinstance(entry, str):
            channels.append({"url": entry, "mode": default_mode})
        elif isinstance(entry, dict) and "url" in entry:
            mode = entry.get("mode", default_mode)
            if mode not in ("video", "audio"):
                raise ValueError(f"channel {entry['url']}: mode must be video|audio, got {mode!r}")
            channels.append({"url": str(entry["url"]), "mode": mode})
        else:
            raise ValueError(f"invalid channel entry: {entry!r}")
    return channels


def run(config_path: Path) -> int:
    config = load_config(config_path)

    log_cfg = config.get("logging", {})
    configure_logging(
        level=log_cfg.get("level", "INFO"),
        log_file=log_cfg.get("file", "./data/scraper.log"),
    )

    log.info("=" * 60)
    log.info("scraper starting, config=%s", config_path)
    log.debug("effective config: %s", config)

    db = Database(config["storage"]["db_path"])
    log.debug("db ready at %s", config["storage"]["db_path"])

    download_dir = Path(config["storage"]["download_dir"])
    download_dir.mkdir(parents=True, exist_ok=True)
    log.debug("download dir: %s", download_dir)

    api_cfg = config["api"]
    obsc = ObscClient(
        base_url=api_cfg["base_url"],
        request_timeout=int(api_cfg.get("request_timeout_sec", 60)),
        upload_timeout=int(api_cfg.get("upload_timeout_sec", 1800)),
    )
    log.debug("obsc client: base_url=%s", api_cfg["base_url"])

    yt_cfg = config["youtube"]
    default_mode = yt_cfg.get("default_mode", "video")
    if default_mode not in ("video", "audio"):
        raise ValueError(f"youtube.default_mode must be video|audio, got {default_mode!r}")

    cookiefile = yt_cfg.get("cookiefile") or None
    if cookiefile:
        if not Path(cookiefile).is_file():
            raise FileNotFoundError(f"youtube.cookiefile not found: {cookiefile}")
        log.info("using cookies file: %s", cookiefile)

    pipeline = Pipeline(
        db=db,
        obsc=obsc,
        download_dir=download_dir,
        source_service=yt_cfg.get("source_service", "youtube"),
        video_format=yt_cfg["video_format"],
        audio_format=yt_cfg.get("audio_format", "bestaudio/best"),
        merge_ext=yt_cfg.get("merge_output_format", "mp4"),
        audio_codec=yt_cfg.get("audio_codec", "mp3"),
        audio_quality=str(yt_cfg.get("audio_quality", "192")),
        poll_interval_sec=float(api_cfg.get("poll_interval_sec", 2)),
        cookiefile=cookiefile,
    )

    max_in_flight = int(api_cfg.get("max_in_flight", 4))
    log.info("max_in_flight=%d (OBSC processing queue depth)", max_in_flight)

    log.info("reconciling state after previous run")
    pipeline.reconcile_on_startup()
    log.info("reconcile finished")

    channels = load_channels(Path(config["channels_file"]), default_mode)
    log.info("loaded %d channel(s) from %s", len(channels), config["channels_file"])
    for idx, ch in enumerate(channels, 1):
        log.debug("  channel[%d] url=%s mode=%s", idx, ch["url"], ch["mode"])

    for ch_idx, ch in enumerate(channels, 1):
        channel_url = ch["url"]
        mode = ch["mode"]
        log.info("=" * 40)
        log.info("channel %d/%d: %s (mode=%s)", ch_idx, len(channels), channel_url, mode)
        try:
            video_iter = iter_channel_videos(channel_url, cookiefile=cookiefile)
        except Exception:
            log.exception("failed to list channel %s", channel_url)
            continue

        seen = 0
        skipped = 0
        processed = 0
        for video_id, video_url in video_iter:
            seen += 1
            if db.exists(video_id):
                row = db.get(video_id)
                log.debug(
                    "[%s] skip (already in DB, status=%s)",
                    video_id, row["status"],
                )
                skipped += 1
                continue
            pipeline.wait_for_free_slot(max_in_flight)
            log.info("[%s] new video -> %s (mode=%s)", video_id, video_url, mode)
            try:
                pipeline.process_video(video_id, video_url, channel_url, mode=mode)
                processed += 1
            except Exception:
                # Последний рубеж — чтобы один битый ролик не ронял весь прогон.
                log.exception("[%s] unhandled pipeline error", video_id)

        log.info(
            "channel %s done: seen=%d, skipped=%d, processed=%d",
            channel_url, seen, skipped, processed,
        )

    log.info("=" * 40)
    log.info("all channels enqueued, draining remaining OBSC tasks")
    pipeline.drain()
    log.info("all channels processed")
    return 0


if __name__ == "__main__":
    cfg = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("config.yaml")
    sys.exit(run(cfg))

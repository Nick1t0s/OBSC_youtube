import json
import logging
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

from scraper.db import Database
from scraper.logging_setup import configure_logging
from scraper.obsc_client import ObscClient
from scraper.pipeline import Pipeline
from scraper.youtube import iter_channel_videos

log = logging.getLogger("scraper.main")


def _start_admin(config: dict) -> None:
    admin_cfg = config.get("admin", {})
    if not admin_cfg.get("enabled", True):
        return
    try:
        from admin.app import create_app
        app = create_app(config)
        host = str(admin_cfg.get("host", "0.0.0.0"))
        port = int(admin_cfg.get("port", 8080))
        log.info("admin panel starting on %s:%d", host, port)
        # Silence werkzeug startup banner — it goes to root logger
        logging.getLogger("werkzeug").setLevel(logging.WARNING)
        app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)
    except Exception:
        log.exception("admin panel crashed")

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


def read_last_start(path: Path) -> Optional[datetime]:
    """Datetime последнего прогона из last_start_file, либо None если файла
    нет / он пуст / содержит мусор. Любой из этих случаев = "запусков ещё не было".
    """
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError:
        log.exception("failed to read %s", path)
        return None
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        log.warning("malformed datetime in %s: %r — treating as no previous run", path, text)
        return None


def write_last_start(path: Path, dt: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dt.isoformat(), encoding="utf-8")


def run_scan(
    pipeline: Pipeline,
    db: Database,
    channels_file: Path,
    default_mode: str,
    cookiefile: Optional[str],
    max_in_flight: int,
) -> None:
    """Один прогон: пройти все каналы, дождаться, пока сервер обработает все
    отправленные задачи. Setup (db, obsc, pipeline) и reconcile делаются снаружи —
    один раз на процесс.
    """
    channels = load_channels(channels_file, default_mode)
    log.info("loaded %d channel(s) from %s", len(channels), channels_file)
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
    log.info("scan finished")


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
        download_retries=int(yt_cfg.get("download_retries", 0)),
        download_retry_delay_sec=float(yt_cfg.get("download_retry_delay_sec", 5)),
    )

    max_in_flight = int(api_cfg.get("max_in_flight", 4))
    log.info("max_in_flight=%d (OBSC processing queue depth)", max_in_flight)

    t = threading.Thread(target=_start_admin, args=(config,), daemon=True, name="admin-http")
    t.start()

    log.info("reconciling state after previous run")
    pipeline.reconcile_on_startup()
    log.info("reconcile finished")

    schedule_cfg = config.get("schedule", {})
    scan_interval = float(schedule_cfg.get("scan_interval_sec", 3600))
    check_interval = float(schedule_cfg.get("check_interval_sec", 60))
    last_start_path = Path(schedule_cfg.get("last_start_file", "./data/last_start.txt"))
    channels_file = Path(config["channels_file"])

    log.info(
        "schedule: scan every %.0fs, check %s every %.0fs",
        scan_interval, last_start_path, check_interval,
    )

    while True:
        last = read_last_start(last_start_path)
        now = datetime.now()
        if last is None:
            log.info("no previous run recorded in %s — starting scan", last_start_path)
            should_run = True
        else:
            elapsed = (now - last).total_seconds()
            if elapsed >= scan_interval:
                log.info(
                    "last run %.0fs ago (>= %.0fs interval) — starting scan",
                    elapsed, scan_interval,
                )
                should_run = True
            else:
                log.debug(
                    "last run %.0fs ago (< %.0fs interval) — waiting",
                    elapsed, scan_interval,
                )
                should_run = False

        if should_run:
            write_last_start(last_start_path, now)
            try:
                run_scan(pipeline, db, channels_file, default_mode, cookiefile, max_in_flight)
            except Exception:
                log.exception("scan crashed; will retry on next scheduled cycle")

        time.sleep(check_interval)


if __name__ == "__main__":
    cfg = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("config.yaml.example")
    sys.exit(run(cfg) or 0)

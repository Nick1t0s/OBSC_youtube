# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

YouTube connector for an external OBSC content-processing server. Reads a list of channel URLs from `channels.json`, walks each channel sequentially, downloads every video via `yt-dlp`, and submits the file + metadata to OBSC (`POST /process`). Submission is non-blocking: once OBSC returns 202, the record sits in `processing` and the pipeline moves to the next video. Before each new download, the pipeline polls `GET /task/{id}` for existing `processing` records and waits until their count drops below `api.max_in_flight` (default 4), keeping a steady queue depth on the OBSC side. After the last channel, `drain()` waits for all outstanding `processing` tasks to reach a terminal state. No multithreading. Download errors are retried inline up to `youtube.download_retries` times within a single run; once exhausted (and for upload/process errors) the record sits in `*_error` and is never retried across runs — state lives in a local SQLite DB so reruns are idempotent.

The process is **long-lived**: launched once manually, it then loops forever, starting a new scan every `schedule.scan_interval_sec` seconds. A scan run is the same end-to-end pass described above (channels → downloads → drain). Between runs the loop sleeps `schedule.check_interval_sec` and re-reads `schedule.last_start_file`; if that file is missing, empty, contains a malformed datetime, or its timestamp is older than `scan_interval_sec`, a new scan starts. The current datetime is written to the file *before* a scan begins, so the interval is measured start-to-start and a fast-failing scan can't busy-loop. Deleting the file is a clean way to force an immediate scan on the next check tick.

`API.md` is the **authoritative spec** for the OBSC HTTP contract (status codes, field names, 409/413/422 behavior). Read it before touching `scraper/obsc_client.py` or any `/process`/`/task` logic.

## Commands

```bash
pip install -r requirements.txt
python3 main.py                     # uses ./config.yaml
python3 main.py /path/to/cfg.yaml   # custom config
python3 -m compileall -q main.py scraper/   # syntax check (no test suite exists)
```

There is currently no lint/format config and no tests.

## Architecture

### Data flow

`main.py` startup: setup db/obsc/pipeline → `Pipeline.reconcile_on_startup()` (once per process). Then enter the scheduler loop:

For each tick: read `last_start_file`. If a scan is due, write current datetime to the file and call `run_scan()`: for each channel in `channels.json`: `iter_channel_videos()` → for each video, if not in DB: `Pipeline.wait_for_free_slot(max_in_flight)` → `Pipeline.process_video()` → `download()` → `ObscClient.submit()` (returns after 202/409/error; no inline polling). After the channel loop: `Pipeline.drain()` polls remaining `processing` records until all are terminal. Then sleep `check_interval_sec` and tick again.

### Module boundaries

- `scraper/db.py` — SQLite wrapper. One table (`videos`), PK = YouTube `video_id`. The only source of truth for "have we seen this video?" and for resume-after-crash.
- `scraper/youtube.py` — `yt-dlp` wrapper. `iter_channel_videos()` uses `extract_flat='in_playlist'` and recurses into nested playlists (channel tabs). `download()` returns `(Path, slim_metadata_dict)`; `DownloadFailed` is the only yt-dlp error that pipeline code catches by type.
- `scraper/obsc_client.py` — thin `requests` wrapper. `submit()` returns raw `(status_code, body)` — the pipeline interprets codes, not the client. `get_task()` returns `None` for 404 (meaningful signal), raises for other non-2xx.
- `scraper/pipeline.py` — the state machine. Holds the statuses-and-transitions logic end-to-end.
- `main.py` — config loading, scheduler loop, `last_start_file` read/write, `run_scan()` (channel iteration + drain), input-level dedup (`db.exists`). `reconcile_on_startup()` is called once per process before the loop, **not** per scan.

### State machine (status column)

```
downloading → uploading → processing → done
      │           │           │
      ▼           ▼           ▼
download_error  upload_error  process_error
                │                  (also done on 409)
                └── on 409: done (content_record_id from body)
```

- `uploading` = file on disk, `POST /process` in flight. Distinguishing this from `downloading` matters for `reconcile_on_startup`.
- `processing` = OBSC returned 202 with a `task_id`; the pipeline has moved on. `wait_for_free_slot()` / `drain()` / `_poll_processing_once()` are the only places that transition out of this state.
- `409` from `/process` ⇒ `done` immediately, **no polling** (the record already exists on the server).
- Any non-202/non-409 from `/process` ⇒ `upload_error`. The response body goes into `log_error`.
- `failed` from `/task` ⇒ `process_error`. OBSC's short `error` field is copied to both `error_message` (truncated) and `log_error` (full).
- `processing` with missing `task_id` encountered during polling ⇒ `process_error` (should never happen in normal flow; belt-and-suspenders guard).
- Errored videos (`*_error`) are **never retried across runs** — input dedup (`db.exists`) skips any video already in the DB in any status. (Inline download retries inside `process_video` happen *before* the record is moved out of `downloading`, so they don't interact with this dedup.)

### Reconciliation on startup

`reconcile_on_startup()` runs before channel iteration and is the only place DB records get deleted:

- `downloading` / `uploading` / `processing`-without-`task_id` → delete (re-process as new on the next pass).
- `processing` with `task_id` → `GET /task`: 404 → delete; `completed`/`failed` → finalize; still pending/processing → **leave as-is**; `wait_for_free_slot()` / `drain()` will keep polling it as part of the normal run.

This is the contract that makes "kill -9 is safe" work — don't break it without thinking through the interaction with `db.exists` dedup in `main.py`.

### Why the `log_error` column exists

`error_message` is truncated (≤500 chars) for quick inspection; `log_error` holds the full payload (traceback, OBSC `error` field, response body on non-202). When diagnosing failures, `log_error` is usually where the answer lives.

## Config conventions

- `config.yaml` is the only runtime config. `youtube.format` is a yt-dlp format selector — current value means "720p, else highest below 720p". Don't change it to allow >720p without a reason.
- `channels.json` is a plain list of URLs. Prefer URLs ending in `/videos` so yt-dlp lands on the videos tab directly instead of walking Shorts/Streams too.
- Downloaded files live under `storage.download_dir` and are deleted in a `finally` block after each video — the dir should stay empty between runs.
- `schedule.scan_interval_sec` and `schedule.check_interval_sec` are independent: scan periodicity (start-to-start) vs. file-poll cadence. `schedule.last_start_file` holds an ISO-format datetime; missing/empty/malformed = "no previous run" (scan immediately).

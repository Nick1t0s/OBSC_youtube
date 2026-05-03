import json
import os
import sqlite3
import time
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Optional

from flask import (
    Flask,
    Response,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    stream_with_context,
    url_for,
)

ALL_STATUSES = [
    "downloading",
    "uploading",
    "processing",
    "done",
    "download_error",
    "upload_error",
    "process_error",
]

STATUS_LABELS = {
    "downloading": "Скачивается",
    "uploading": "Загружается",
    "processing": "Обрабатывается",
    "done": "Готово",
    "download_error": "Ошибка скачивания",
    "upload_error": "Ошибка загрузки",
    "process_error": "Ошибка обработки",
}

STATUS_COLORS = {
    "done": "green",
    "processing": "blue",
    "downloading": "yellow",
    "uploading": "yellow",
    "download_error": "red",
    "upload_error": "red",
    "process_error": "red",
}


def create_app(config: dict) -> Flask:
    app = Flask(__name__, template_folder="templates")

    admin_cfg = config.get("admin", {})
    raw_key = admin_cfg.get("secret_key") or ""
    app.secret_key = raw_key.encode() if raw_key else os.urandom(32)
    app.config["SESSION_COOKIE_SAMESITE"] = "Strict"
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=7)

    USERNAME = str(admin_cfg.get("username", "admin"))
    PASSWORD = str(admin_cfg.get("password", "changeme"))

    DB_PATH = config["storage"]["db_path"]
    CHANNELS_FILE = Path(config.get("channels_file", "./channels.json"))
    LAST_START_FILE = Path(
        config.get("schedule", {}).get("last_start_file", "./data/last_start.txt")
    )
    LOG_FILE = Path(config.get("logging", {}).get("file", "./data/scraper.log"))
    SCAN_INTERVAL = float(config.get("schedule", {}).get("scan_interval_sec", 3600))
    DEFAULT_MODE = config.get("youtube", {}).get("default_mode", "video")
    OBSC_BASE_URL = config.get("api", {}).get("base_url", "")
    OBSC_TIMEOUT = int(config.get("api", {}).get("request_timeout_sec", 10))

    # ------------------------------------------------------------------ helpers

    def login_required(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not session.get("authenticated"):
                return redirect(url_for("login"))
            return f(*args, **kwargs)

        return decorated

    def get_db() -> sqlite3.Connection:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def load_channels() -> list[dict]:
        try:
            with CHANNELS_FILE.open("r", encoding="utf-8") as f:
                data = json.load(f)
            result = []
            for entry in data:
                if isinstance(entry, str):
                    result.append({"url": entry, "mode": DEFAULT_MODE})
                elif isinstance(entry, dict) and "url" in entry:
                    result.append(
                        {"url": entry["url"], "mode": entry.get("mode", DEFAULT_MODE)}
                    )
            return result
        except Exception:
            return []

    def save_channels(channels: list[dict]) -> None:
        out = []
        for ch in channels:
            if ch.get("mode", DEFAULT_MODE) == DEFAULT_MODE:
                out.append(ch["url"])
            else:
                out.append({"url": ch["url"], "mode": ch["mode"]})
        CHANNELS_FILE.write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def channel_display_name(url: str) -> str:
        url = url.rstrip("/")
        for suffix in ("/videos", "/streams", "/shorts", "/playlists"):
            if url.endswith(suffix):
                url = url[: -len(suffix)]
        parts = url.split("/")
        for part in reversed(parts):
            if part and part not in (
                "www.youtube.com",
                "youtube.com",
                "https:",
                "http:",
                "",
            ):
                return part
        return url

    def get_channel_stats(conn: sqlite3.Connection, channel_url: str) -> dict:
        rows = conn.execute(
            "SELECT status, COUNT(*) as cnt FROM videos WHERE channel_url = ? GROUP BY status",
            (channel_url,),
        ).fetchall()
        by_status = {row["status"]: row["cnt"] for row in rows}
        done = by_status.get("done", 0)
        errors = sum(
            by_status.get(s, 0)
            for s in ("download_error", "upload_error", "process_error")
        )
        in_flight = sum(
            by_status.get(s, 0) for s in ("downloading", "uploading", "processing")
        )
        total = sum(by_status.values())
        return {
            "total": total,
            "done": done,
            "errors": errors,
            "in_flight": in_flight,
            "by_status": dict(by_status),
        }

    def get_global_stats(conn: sqlite3.Connection) -> dict:
        rows = conn.execute(
            "SELECT status, COUNT(*) as cnt FROM videos GROUP BY status"
        ).fetchall()
        by_status = {row["status"]: row["cnt"] for row in rows}
        done = by_status.get("done", 0)
        errors = sum(
            by_status.get(s, 0)
            for s in ("download_error", "upload_error", "process_error")
        )
        in_flight = sum(
            by_status.get(s, 0) for s in ("downloading", "uploading", "processing")
        )
        total = sum(by_status.values())
        return {
            "total": total,
            "done": done,
            "errors": errors,
            "in_flight": in_flight,
            "by_status": dict(by_status),
        }

    def read_last_start() -> Optional[datetime]:
        try:
            text = LAST_START_FILE.read_text(encoding="utf-8").strip()
            if text:
                return datetime.fromisoformat(text)
        except Exception:
            pass
        return None

    def get_sample_video_id(conn: sqlite3.Connection, channel_url: str) -> Optional[str]:
        row = conn.execute(
            "SELECT video_id FROM videos WHERE channel_url = ? AND status = 'done' LIMIT 1",
            (channel_url,),
        ).fetchone()
        if not row:
            row = conn.execute(
                "SELECT video_id FROM videos WHERE channel_url = ? LIMIT 1",
                (channel_url,),
            ).fetchone()
        return row["video_id"] if row else None

    # ------------------------------------------------------------------ context

    @app.context_processor
    def inject_globals():
        return {
            "STATUS_LABELS": STATUS_LABELS,
            "STATUS_COLORS": STATUS_COLORS,
            "ALL_STATUSES": ALL_STATUSES,
        }

    # ------------------------------------------------------------------ routes

    @app.route("/")
    def index():
        if session.get("authenticated"):
            return redirect(url_for("dashboard"))
        return redirect(url_for("login"))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            if (
                request.form.get("username") == USERNAME
                and request.form.get("password") == PASSWORD
            ):
                session["authenticated"] = True
                session.permanent = True
                return redirect(url_for("dashboard"))
            flash("Неверный логин или пароль", "error")
        return render_template("login.html")

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/dashboard")
    @login_required
    def dashboard():
        try:
            conn = get_db()
            stats = get_global_stats(conn)
            conn.close()
        except Exception:
            stats = {
                "total": 0,
                "done": 0,
                "errors": 0,
                "in_flight": 0,
                "by_status": {},
            }

        last_start = read_last_start()
        next_start = None
        if last_start:
            next_start = last_start + timedelta(seconds=SCAN_INTERVAL)

        return render_template(
            "dashboard.html",
            stats=stats,
            last_start=last_start,
            next_start=next_start,
            scan_interval=int(SCAN_INTERVAL),
            now=datetime.now(),
            obsc_base_url=OBSC_BASE_URL,
        )

    @app.route("/scan/trigger", methods=["POST"])
    @login_required
    def trigger_scan():
        try:
            LAST_START_FILE.unlink(missing_ok=True)
            flash(
                "Файл последнего запуска удалён — сканирование начнётся на следующей итерации планировщика",
                "success",
            )
        except Exception as e:
            flash(f"Ошибка при удалении файла: {e}", "error")
        return redirect(url_for("dashboard"))

    @app.route("/api/obsc-health")
    @login_required
    def obsc_health():
        import requests as req

        try:
            r = req.get(f"{OBSC_BASE_URL}/health", timeout=OBSC_TIMEOUT)
            return jsonify({"ok": r.ok, "status": r.status_code})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})

    @app.route("/channels")
    @login_required
    def channels():
        channel_list = load_channels()
        try:
            conn = get_db()
            for ch in channel_list:
                ch["stats"] = get_channel_stats(conn, ch["url"])
                ch["display_name"] = channel_display_name(ch["url"])
                ch["sample_video_id"] = get_sample_video_id(conn, ch["url"])
            conn.close()
        except Exception:
            for ch in channel_list:
                ch.setdefault(
                    "stats", {"total": 0, "done": 0, "errors": 0, "in_flight": 0}
                )
                ch.setdefault("display_name", channel_display_name(ch["url"]))
                ch.setdefault("sample_video_id", None)

        return render_template(
            "channels.html", channels=channel_list, default_mode=DEFAULT_MODE
        )

    @app.route("/channels/add", methods=["POST"])
    @login_required
    def add_channel():
        url = request.form.get("url", "").strip()
        mode = request.form.get("mode", DEFAULT_MODE).strip()
        if not url:
            flash("URL не может быть пустым", "error")
            return redirect(url_for("channels"))
        if mode not in ("video", "audio"):
            mode = DEFAULT_MODE
        channel_list = load_channels()
        if any(ch["url"] == url for ch in channel_list):
            flash("Этот канал уже добавлен", "warning")
            return redirect(url_for("channels"))
        channel_list.append({"url": url, "mode": mode})
        save_channels(channel_list)
        flash(f"Канал добавлен: {channel_display_name(url)}", "success")
        return redirect(url_for("channels"))

    @app.route("/channels/remove", methods=["POST"])
    @login_required
    def remove_channel():
        url = request.form.get("url", "").strip()
        if not url:
            abort(400)
        name = channel_display_name(url)
        channel_list = load_channels()
        channel_list = [ch for ch in channel_list if ch["url"] != url]
        save_channels(channel_list)
        flash(f"Канал удалён: {name}", "success")
        return redirect(url_for("channels"))

    @app.route("/channels/videos")
    @login_required
    def channel_videos():
        channel_url = request.args.get("url", "")
        if not channel_url:
            return redirect(url_for("channels"))

        status_filter = request.args.get("status", "")
        search = request.args.get("q", "").strip()
        page = max(1, int(request.args.get("page", 1) or 1))
        per_page = 50
        offset = (page - 1) * per_page

        try:
            conn = get_db()
            params: list = [channel_url]
            where = "channel_url = ?"
            if status_filter:
                where += " AND status = ?"
                params.append(status_filter)
            if search:
                where += " AND (title LIKE ? OR video_id LIKE ?)"
                params += [f"%{search}%", f"%{search}%"]

            total_count = conn.execute(
                f"SELECT COUNT(*) FROM videos WHERE {where}", params
            ).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM videos WHERE {where} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                params + [per_page, offset],
            ).fetchall()
            stats = get_channel_stats(conn, channel_url)
            conn.close()
        except Exception:
            rows = []
            total_count = 0
            stats = {"total": 0, "done": 0, "errors": 0, "in_flight": 0}

        videos = [dict(row) for row in rows]
        total_pages = max(1, (total_count + per_page - 1) // per_page)

        return render_template(
            "channel_videos.html",
            channel_url=channel_url,
            display_name=channel_display_name(channel_url),
            videos=videos,
            stats=stats,
            status_filter=status_filter,
            search=search,
            page=page,
            total_pages=total_pages,
            total_count=total_count,
        )

    @app.route("/videos/<video_id>/retry", methods=["POST"])
    @login_required
    def retry_video(video_id):
        channel_url = request.form.get("channel_url", "")
        try:
            conn = get_db()
            conn.execute("DELETE FROM videos WHERE video_id = ?", (video_id,))
            conn.commit()
            conn.close()
            flash(
                f"Видео {video_id} удалено — будет переобработано при следующем сканировании",
                "success",
            )
        except Exception as e:
            flash(f"Ошибка: {e}", "error")
        if channel_url:
            return redirect(url_for("channel_videos", url=channel_url))
        return redirect(url_for("dashboard"))

    @app.route("/channels/retry-errors", methods=["POST"])
    @login_required
    def retry_channel_errors():
        channel_url = request.form.get("url", "").strip()
        if not channel_url:
            abort(400)
        error_statuses = ("download_error", "upload_error", "process_error")
        try:
            conn = get_db()
            placeholders = ",".join("?" * len(error_statuses))
            result = conn.execute(
                f"DELETE FROM videos WHERE channel_url = ? AND status IN ({placeholders})",
                (channel_url, *error_statuses),
            )
            deleted = result.rowcount
            conn.commit()
            conn.close()
            flash(
                f"Удалено {deleted} ошибочных записей — видео будут переобработаны при следующем сканировании",
                "success",
            )
        except Exception as e:
            flash(f"Ошибка: {e}", "error")
        return redirect(url_for("channel_videos", url=channel_url))

    @app.route("/logs")
    @login_required
    def logs():
        return render_template("logs.html")

    @app.route("/api/log-stream")
    @login_required
    def log_stream():
        def generate():
            try:
                with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(0, 2)
                    end = f.tell()
                    f.seek(max(0, end - 65536))
                    if end > 65536:
                        f.readline()  # skip partial first line
                    for line in f:
                        yield f"data: {json.dumps(line.rstrip())}\n\n"
                    while True:
                        line = f.readline()
                        if line:
                            yield f"data: {json.dumps(line.rstrip())}\n\n"
                        else:
                            time.sleep(0.5)
                            yield ": heartbeat\n\n"
            except GeneratorExit:
                pass
            except Exception as e:
                yield f"data: {json.dumps(f'[Ошибка чтения лога: {e}]')}\n\n"

        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.route("/api/stats")
    @login_required
    def api_stats():
        try:
            conn = get_db()
            stats = get_global_stats(conn)
            conn.close()
        except Exception:
            stats = {
                "total": 0,
                "done": 0,
                "errors": 0,
                "in_flight": 0,
                "by_status": {},
            }
        last_start = read_last_start()
        return jsonify(
            {
                "stats": stats,
                "last_start": last_start.isoformat() if last_start else None,
                "now": datetime.now().isoformat(),
            }
        )

    return app

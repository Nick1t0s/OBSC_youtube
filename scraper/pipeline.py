import logging
import time
import traceback
from pathlib import Path
from typing import Optional

from .db import Database
from .obsc_client import ObscClient
from .youtube import DownloadFailed, download

log = logging.getLogger(__name__)


class Pipeline:
    def __init__(
        self,
        db: Database,
        obsc: ObscClient,
        download_dir: Path,
        source_service: str,
        video_format: str,
        audio_format: str,
        merge_ext: str,
        audio_codec: Optional[str],
        audio_quality: Optional[str],
        poll_interval_sec: float,
    ):
        self.db = db
        self.obsc = obsc
        self.download_dir = download_dir
        self.source_service = source_service
        self.video_format = video_format
        self.audio_format = audio_format
        self.merge_ext = merge_ext
        self.audio_codec = audio_codec
        self.audio_quality = audio_quality
        self.poll_interval_sec = poll_interval_sec

    def process_video(
        self,
        video_id: str,
        video_url: str,
        channel_url: str,
        mode: str = "video",
    ) -> None:
        """Полный жизненный цикл одного видео.

        mode: "video" — 720p+аудио в mp4; "audio" — только аудиодорожка.
        Предполагает, что dedup-проверка уже сделана снаружи.
        """
        if mode == "audio":
            fmt = self.audio_format
            merge: Optional[str] = None
            codec = self.audio_codec
            quality = self.audio_quality
        else:
            fmt = self.video_format
            merge = self.merge_ext
            codec = None
            quality = None

        self.db.insert(
            video_id=video_id,
            url=video_url,
            channel_url=channel_url,
            title=None,
            status="downloading",
        )
        log.info("[%s] downloading (mode=%s)", video_id, mode)

        file_path: Optional[Path] = None
        try:
            file_path, metadata = download(
                video_url,
                self.download_dir,
                fmt,
                merge_ext=merge,
                audio_codec=codec,
                audio_quality=quality,
            )
        except DownloadFailed as e:
            log.warning("[%s] download failed: %s", video_id, e)
            self.db.update(
                video_id,
                status="download_error",
                error_message=str(e)[:500],
                log_error=traceback.format_exc(),
            )
            return
        except Exception:
            log.exception("[%s] unexpected download error", video_id)
            self.db.update(
                video_id,
                status="download_error",
                error_message="unexpected error",
                log_error=traceback.format_exc(),
            )
            return

        title = metadata.get("title")
        self.db.update(video_id, status="uploading", title=title)
        log.info("[%s] uploading (%s)", video_id, title)

        try:
            self._submit_and_poll(video_id, video_url, file_path, metadata)
        finally:
            if file_path and file_path.exists():
                try:
                    file_path.unlink()
                except OSError:
                    log.warning("[%s] failed to delete %s", video_id, file_path)

    def _submit_and_poll(
        self,
        video_id: str,
        video_url: str,
        file_path: Path,
        metadata: dict,
    ) -> None:
        try:
            status_code, body = self.obsc.submit(
                source_service=self.source_service,
                source_url=video_url,
                file_path=file_path,
                metadata=metadata,
            )
        except Exception as e:
            log.exception("[%s] submit raised", video_id)
            self.db.update(
                video_id,
                status="upload_error",
                error_message=f"submit exception: {e}"[:500],
                log_error=traceback.format_exc(),
            )
            return

        if status_code == 202:
            task_id = body.get("task_id")
            if not task_id:
                self.db.update(
                    video_id,
                    status="upload_error",
                    error_message="202 without task_id",
                    log_error=str(body)[:2000],
                )
                return
            self.db.update(video_id, status="processing", task_id=task_id)
            log.info("[%s] processing (task_id=%s)", video_id, task_id)
            self._poll_until_done(video_id, task_id)
            return

        if status_code == 409:
            crid = body.get("content_record_id")
            self.db.update(
                video_id,
                status="done",
                content_record_id=crid,
                error_message="duplicate (409)",
            )
            log.info("[%s] duplicate on server (content_record_id=%s)", video_id, crid)
            return

        # Любой другой код (413/422/5xx/…) = ошибка загрузки.
        self.db.update(
            video_id,
            status="upload_error",
            error_message=f"HTTP {status_code}",
            log_error=str(body)[:4000],
        )
        log.warning("[%s] upload_error HTTP %s: %s", video_id, status_code, body)

    def _poll_until_done(self, video_id: str, task_id: str) -> None:
        start = time.monotonic()
        ticks = 0
        last_status: Optional[str] = None
        while True:
            time.sleep(self.poll_interval_sec)
            ticks += 1
            elapsed = time.monotonic() - start
            try:
                task = self.obsc.get_task(task_id)
            except Exception as e:
                log.warning(
                    "[%s] poll tick=%d elapsed=%.1fs FAILED: %s",
                    video_id, ticks, elapsed, e,
                )
                continue

            if task is None:
                # /task вернул 404 посреди поллинга — на сервере записи нет.
                log.warning(
                    "[%s] poll tick=%d elapsed=%.1fs task %s DISAPPEARED (404)",
                    video_id, ticks, elapsed, task_id,
                )
                self.db.update(
                    video_id,
                    status="process_error",
                    error_message="task disappeared from OBSC (404)",
                )
                return

            status = task.get("status")
            log.debug(
                "[%s] poll tick=%d elapsed=%.1fs task=%s status=%s",
                video_id, ticks, elapsed, task_id, status,
            )
            if status != last_status:
                log.info(
                    "[%s] OBSC status: %s -> %s (elapsed=%.1fs)",
                    video_id, last_status, status, elapsed,
                )
                last_status = status
            if status in ("pending", "processing"):
                continue

            if status == "completed":
                result = task.get("result") or {}
                self.db.update(
                    video_id,
                    status="done",
                    content_record_id=result.get("content_record_id"),
                )
                log.info("[%s] done", video_id)
                return

            if status == "failed":
                err = task.get("error") or ""
                self.db.update(
                    video_id,
                    status="process_error",
                    error_message=str(err)[:500],
                    log_error=str(err)[:8000],
                )
                log.warning("[%s] process_error: %s", video_id, err)
                return

            # Неожиданный статус.
            self.db.update(
                video_id,
                status="process_error",
                error_message=f"unknown status '{status}'",
                log_error=str(task)[:4000],
            )
            return

    def reconcile_on_startup(self) -> None:
        """Чистка состояний после перезапуска.

        - downloading/uploading: дропаем запись, обработается как новое при следующем проходе.
        - processing: спрашиваем OBSC. 404 → удаляем. Иначе допалливаем до терминала.
        """
        for row in self.db.list_by_status(["downloading", "uploading"]):
            vid = row["video_id"]
            log.info("[%s] reconcile: drop stuck %s", vid, row["status"])
            self.db.delete(vid)

        for row in self.db.list_by_status(["processing"]):
            vid = row["video_id"]
            task_id = row["task_id"]
            if not task_id:
                log.info("[%s] reconcile: processing without task_id, drop", vid)
                self.db.delete(vid)
                continue
            try:
                task = self.obsc.get_task(task_id)
            except Exception as e:
                log.warning(
                    "[%s] reconcile: get_task failed, leaving as processing: %s",
                    vid, e,
                )
                continue
            if task is None:
                log.info("[%s] reconcile: task %s not in OBSC, drop", vid, task_id)
                self.db.delete(vid)
                continue
            status = task.get("status")
            if status == "completed":
                result = task.get("result") or {}
                self.db.update(
                    vid,
                    status="done",
                    content_record_id=result.get("content_record_id"),
                )
            elif status == "failed":
                err = task.get("error") or ""
                self.db.update(
                    vid,
                    status="process_error",
                    error_message=str(err)[:500],
                    log_error=str(err)[:8000],
                )
            else:
                # Всё ещё pending/processing — допалливаем до терминала.
                log.info("[%s] reconcile: resume polling task %s", vid, task_id)
                self._poll_until_done(vid, task_id)

import json
import logging
import mimetypes
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger(__name__)


class ObscClient:
    def __init__(self, base_url: str, request_timeout: int, upload_timeout: int):
        self._base = base_url.rstrip("/")
        self._timeout = request_timeout
        self._upload_timeout = upload_timeout

    def submit(
        self,
        *,
        source_service: str,
        source_url: str,
        file_path: Path,
        metadata: dict,
    ) -> tuple[int, dict]:
        """POST /process. Возвращает (status_code, json_body).

        Коды, которые мы ожидаем увидеть: 202, 409, 413, 422, 5xx.
        """
        url = f"{self._base}/process"
        size_mb = file_path.stat().st_size / (1024 * 1024)
        log.debug(
            "POST /process source_service=%s source_url=%s file=%s (%.1f MB) meta_keys=%s",
            source_service, source_url, file_path.name, size_mb,
            sorted(metadata.keys()),
        )
        log.debug("POST /process metadata payload: %s", metadata)
        data = {
            "source_service": source_service,
            "source_url": source_url,
            "metadata": json.dumps(metadata, ensure_ascii=False, default=str),
        }
        ctype, _ = mimetypes.guess_type(str(file_path))
        ctype = ctype or "application/octet-stream"
        with file_path.open("rb") as fh:
            files = {"files": (file_path.name, fh, ctype)}
            resp = requests.post(
                url, data=data, files=files, timeout=self._upload_timeout
            )
        try:
            body = resp.json()
        except ValueError:
            body = {"raw": resp.text}
        log.debug("POST /process -> HTTP %s body=%s", resp.status_code, body)
        return resp.status_code, body

    def get_task(self, task_id: str) -> Optional[dict]:
        """GET /task/{id}. Возвращает json при 200, None при 404."""
        url = f"{self._base}/task/{task_id}"
        log.debug("GET /task/%s", task_id)
        resp = requests.get(url, timeout=self._timeout)
        if resp.status_code == 404:
            log.warning("GET /task/%s -> 404 (not found)", task_id)
            return None
        resp.raise_for_status()
        body = resp.json()
        log.debug(
            "GET /task/%s -> status=%s error=%s",
            task_id, body.get("status"), body.get("error"),
        )
        return body

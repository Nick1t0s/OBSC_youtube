# OBSC HTTP API

Справочник по REST-API сервера OBSC. Запускается через `python main.py`; по умолчанию слушает `0.0.0.0:8000` (настраивается в `config.yaml` → `server.host` / `server.port`).

## Общие свойства

- **Base URL:** `http://<host>:<port>` (конфигурируется).
- **Аутентификация:** отсутствует. Rate limiting отсутствует. Версионирования URL нет.
- **Content-Type запросов:**
  - `POST /process` — `multipart/form-data` (загрузка файлов)
  - остальные — параметры в query string
- **Content-Type ответов:** `application/json` (UTF-8).
- **Ошибки валидации FastAPI:** HTTP 422 со стандартным телом `{"detail": [...]}`.
- **Лимит загрузки:** `storage.max_upload_size_mb` из `config.yaml` (по умолчанию 2048 МБ). Превышение → HTTP 413 **до** парсинга тела.
- **Дубликаты:** определяются по паре `(source_service, source_url)`. Проверяются только если `source_url` заполнен (partial unique index).

---

## 1. `POST /process`

Создаёт задачу на обработку. Задача ставится в персистентную SQLite-очередь и обрабатывается асинхронно воркером. Эндпоинт возвращается сразу с `task_id`, за статусом нужно опрашивать `GET /task/{task_id}`.

### Параметры (multipart/form-data)

| Поле | Тип | Обяз. | Описание |
|------|-----|:---:|----------|
| `source_service` | string | **да** | Имя сервиса-источника (`telegram`, `youtube`, `email`, …). Свободная строка, индексируется в БД для фильтрации. |
| `text` | string | нет | Свободный текст (plain / HTML / markdown). HTML и markdown стрипаются на стороне сервера перед рендером. |
| `source_url` | string | нет | Ссылка на первоисточник. Используется для дедупликации. |
| `metadata` | string (JSON) | нет | Произвольные метаданные, специфичные для сервиса. **Валидный JSON**. Попадает и в БД (колонка `content_records.metadata`), и в шаблон рендера (блок `$metadata`). |
| `files` | list[UploadFile] | нет | Любые файлы поддерживаемых типов (см. `configs/object_processor_config.yaml` → `attachment_processors`). |

Минимум одно из `text` / `files` имеет смысл передавать — задача с пустым телом и пустыми вложениями отрабатывает, но `generated_text` получится пустой и эмбеддинги не создаются.

### Ответы

#### 202 Accepted

Задача принята в очередь.

```json
{
  "task_id": "3f1c02e6-9c35-4aa0-9be2-10b6c1f9e7e3"
}
```

#### 409 Conflict

Запись с таким же `(source_service, source_url)` уже существует. Возвращается **вместо** создания задачи.

```json
{
  "content_record_id": "a4f9b3c0-1111-2222-3333-444455556666"
}
```

> Проверка делается дважды: один раз в `/process` и повторно воркером непосредственно перед обработкой — чтобы отловить гонку между постановкой в очередь и фактическим стартом.

#### 413 Payload Too Large

Размер загрузки превысил `storage.max_upload_size_mb`. Проверяется пре-парсингом по `Content-Length`.

```json
{"detail": "Upload too large"}
```

Та же ошибка с другим `detail: "Upload size exceeds limit"` возвращается, если суммарный размер файлов внутри multipart превысил лимит уже после чтения.

#### 422 Unprocessable Entity

Невалидный JSON в `metadata`:

```json
{"detail": "metadata must be valid JSON"}
```

Или отсутствует `source_service` — стандартная ошибка валидации FastAPI.

### Пример

```bash
curl -X POST http://localhost:8000/process \
  -F 'source_service=youtube' \
  -F 'source_url=https://youtu.be/dQw4w9WgXcQ' \
  -F 'metadata={"title":"Never Gonna Give You Up","channel":"Rick Astley","duration":212}' \
  -F 'files=@/path/to/downloaded_video.mp4'
```

---

## 2. `GET /task/{task_id}`

Возвращает статус задачи и — для завершённых — рендер и id чанков.

### Путь

- `task_id` — UUID, полученный из `POST /process`.

### Ответы

#### 200 OK

```json
{
  "task_id": "3f1c02e6-9c35-4aa0-9be2-10b6c1f9e7e3",
  "status": "completed",
  "result": {
    "generated_text": "source_service: youtube\nsource_url: https://youtu.be/...\n...",
    "content_record_id": "a4f9b3c0-...",
    "chunk_ids": ["b1...", "b2...", "b3..."]
  },
  "error": null
}
```

Поля:

| Поле | Тип | Описание |
|------|-----|----------|
| `task_id` | UUID | Тот же, что был возвращён при постановке. |
| `status` | `pending` \| `processing` \| `completed` \| `failed` | Текущий статус. |
| `result` | object \| null | Непустой **только при `completed`**. |
| `result.generated_text` | string | Полный рендер (`$metadata` + `$text` + `$attachments`). |
| `result.content_record_id` | UUID | Ссылка на строку в `content_records`. |
| `result.chunk_ids` | list[UUID] | UUID чанков в `content_chunks` в порядке `chunk_index`. |
| `error` | string \| null | Сообщение ошибки, непустое **только при `failed`**. Полный traceback лежит в `crash.log`, в API он не попадает. |

#### 404 Not Found

Задачи с таким `task_id` нет в `task_log`.

```json
{"detail": "Task not found"}
```

### Пример

```bash
curl http://localhost:8000/task/3f1c02e6-9c35-4aa0-9be2-10b6c1f9e7e3
```

### Состояния задачи

```
pending ──► processing ──► completed
                  └────────► failed
```

- `pending` — задача в очереди, воркер до неё ещё не дошёл.
- `processing` — воркер начал обработку. `started_at` заполнен.
- `completed` — `content_records` создан, эмбеддинги сохранены, очередь почищена. `result` заполнен.
- `failed` — исключение в pipeline. Crash log записан, задача удалена из очереди, `error_message` сохранён в БД.

---

## 3. `GET /tasks`

Список задач с фильтрацией и пагинацией.

### Query-параметры

| Параметр | Тип | Default | Описание |
|----------|-----|:---:|----------|
| `status` | string | — | Фильтр: `pending` / `processing` / `completed` / `failed`. |
| `source_service` | string | — | Фильтр по `source_service`. |
| `limit` | int | 50 | Максимум записей. Значения > 200 молча зажимаются до 200. |
| `offset` | int | 0 | Смещение. |

### Ответ

#### 200 OK

```json
{
  "tasks": [
    {
      "task_id": "3f1c02e6-...",
      "status": "completed",
      "source_service": "youtube",
      "source_url": "https://youtu.be/...",
      "created_at": "2026-04-19T12:00:00Z",
      "started_at": "2026-04-19T12:00:01Z",
      "completed_at": "2026-04-19T12:00:15Z"
    }
  ],
  "total": 150,
  "limit": 50,
  "offset": 0
}
```

- `total` — общее число записей, удовлетворяющих фильтру (для расчёта страниц).
- `tasks` упорядочены по `created_at` DESC.

### Пример

```bash
curl 'http://localhost:8000/tasks?status=failed&limit=20'
curl 'http://localhost:8000/tasks?source_service=youtube&offset=100'
```

---

## 4. `GET /health`

Проверка готовности всех внешних зависимостей.

### Ответ

#### 200 OK — всё ок

```json
{
  "status": "healthy",
  "checks": {
    "postgresql": "ok",
    "ollama": "ok",
    "ffmpeg": "ok",
    "soffice": "ok"
  }
}
```

#### 503 Service Unavailable — хотя бы одна проверка провалена

```json
{
  "status": "unhealthy",
  "checks": {
    "postgresql": "ok",
    "ollama": "error: HTTPConnectionPool(...): Connection refused",
    "ffmpeg": "ok",
    "soffice": "error: soffice not found"
  }
}
```

### Проверяемые зависимости

| Ключ | Проверка |
|------|----------|
| `postgresql` | `SELECT 1` через SQLAlchemy async engine. |
| `ollama` | HTTP 200 на `http://localhost:11434/api/tags` (таймаут 3 с). |
| `cuda` | Только если `whisper.device: cuda` в `config.yaml`. Вызывает `torch.cuda.is_available()`. |
| `ffmpeg` | `shutil.which("ffmpeg")`. |
| `soffice` | `shutil.which("soffice")` (LibreOffice для `word/powerpoint/odt/…`). |

> Эта же последовательность запускается **на старте процесса**. Если хотя бы одна проверка не прошла, `python main.py` завершается с ненулевым кодом. Рантайм-версия эндпоинта позволяет опрашивать состояние живого инстанса (например, из оркестратора).

---

## 5. Полный сценарий (curl)

```bash
# 1. Проверка, что сервер жив
curl -fsS http://localhost:8000/health | jq

# 2. Отправка задачи
TASK_ID=$(curl -s -X POST http://localhost:8000/process \
  -F 'source_service=test' \
  -F 'text=Привет, мир!' \
  -F 'files=@./doc.pdf' \
  -F 'metadata={"author":"nik"}' \
  | jq -r .task_id)
echo "task_id=$TASK_ID"

# 3. Поллинг до completed
while :; do
  STATUS=$(curl -s "http://localhost:8000/task/$TASK_ID" | jq -r .status)
  echo "$STATUS"
  [[ "$STATUS" =~ ^(completed|failed)$ ]] && break
  sleep 2
done

# 4. Получение результата
curl -s "http://localhost:8000/task/$TASK_ID" | jq .result
```

---

## 6. Сводная таблица HTTP-кодов

| Код | Эндпоинт(ы) | Когда |
|:---:|-------------|-------|
| 200 | `GET /task`, `GET /tasks`, `GET /health` (healthy) | Успех. |
| 202 | `POST /process` | Задача принята в очередь. |
| 404 | `GET /task/{task_id}` | Задачи нет в `task_log`. |
| 409 | `POST /process` | Дубликат по `(source_service, source_url)`. |
| 413 | `POST /process` | Превышен `max_upload_size_mb`. |
| 422 | `POST /process` | Невалидный JSON в `metadata` / нет `source_service`. |
| 503 | `GET /health` | Хотя бы одна проверка провалена. |

---

## 7. Рендер `generated_text`

`result.generated_text` у `completed`-задачи — это вывод `ObjectProcessor`, собранный из шаблона `configs/templates/object.txt`:

```
$metadata

$text

$attachments
```

- `$metadata` — построчно `key: value`; pipeline подкладывает туда `source_service`, `source_url` и всё, что пришло в поле `metadata` запроса (скаляры → `str()`, контейнеры → `json.dumps`).
- `$text` — входной `text` после stripping HTML/markdown.
- `$attachments` — склейка `render()`-вывода каждого файла, пронумерованных `Вложение i:`. Конкретный формат зависит от подпроцессора: `PhotoProcessor` даёт описание + OCR, `VideoProcessor` — транскрипцию + описания кадров, `PDFProcessor` — поблочный текст + таблицы + картинки, и т. д.

Шаблон и маршрутизация вложений настраиваются в `configs/object_processor_config.yaml` — менять рендер можно без правки кода.

---

## 8. Примечания для клиентов-коннекторов

- **OBSC source-agnostic.** Ни один эндпоинт не знает про YouTube, email или Telegram. Коннектор сам забирает данные из своего источника (yt-dlp, IMAP, Bot API …), формирует `metadata` с нужными полями и кладёт файлы в `files`.
- **Дедупликация на клиенте не нужна.** Шлите всё, что нашли — сервер сам вернёт 409 с существующим `content_record_id`, если запись уже есть.
- **Сохранять `task_id`** имеет смысл только если нужно дождаться `completed`. Иначе статус в БД всегда доступен через `GET /tasks` с фильтром по `source_url` (не напрямую, но легко найти по `source_service` + `created_at`).
- **Большие файлы** (видео, объёмные PDF) обрабатываются минутами — не делайте короткий таймаут на поллинге.
- **Файлы на диске не хранятся.** После завершения задачи `temp/<task_id>/` удаляется; в БД остаётся только `extracted_text` каждого файла в `content_files`.

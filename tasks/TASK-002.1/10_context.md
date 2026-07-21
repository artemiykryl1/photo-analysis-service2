---
task_id: TASK-002.1
agent: context-collector
model: haiku
status: done
inputs: [specs/constitution.md, specs/feature-upload/tasks.md, tasks/TASK-002.1/00_orchestration.md]
outputs: [tasks/TASK-002.1/10_context.md]
timestamp: 2026-07-21T00:00:00Z
---

# Контекст кода для TASK-002.1 — Багфиксы по внешним ревью

Карта релевантных файлов и текущее состояние по каждому фиксу F1–F8.

## Релевантные файлы

| Путь | Назначение |
|------|-----------|
| `photo-service/app/worker/consumer.py` | Цикл consumer, обработка сообщений, commit offset |
| `photo-service/app/worker/main.py` | Инициализация worker, graceful shutdown |
| `photo-service/app/api/batches.py` | Эндпоинт POST /photos/batch (чтение файлов) |
| `photo-service/app/api/photos.py` | Эндпоинт POST /photos (чтение файла) |
| `photo-service/app/services/photo_service.py` | Валидация, create_photo, create_batch, лимиты, commit |
| `photo-service/app/services/batch_service.py` | get_batch, mark_completed, select_best_photo |
| `photo-service/app/services/analysis_processor.py` | Claim, gRPC, retry loop, terminal writes, метрики |
| `photo-service/app/integrations/metrics.py` | Все Prometheus метрики (текущий монолит) |
| `photo-service/app/integrations/analyzer_client.py` | gRPC client, insecure_channel |
| `photo-service/app/core/config.py` | Settings, env переменные |
| `photo-service/app/repositories/batch_repository.py` | mark_completed, get_by_id |
| `photo-service/app/repositories/photo_repository.py` | Методы для фото, claim_for_processing, mark_done, mark_failed |
| `photo-service/app/db/models.py` | Модели Photo, Batch, AnalysisResult; связи batch_id |
| `photo-service/docker-compose.yml` | Сервисы, restart политики |
| `photo-service/prometheus.yml` | Scrape конфиг (photo-api:8000, photo-worker:8001) |
| `photo-service/grafana/dashboards/photo-service.json` | Дашборд, запросы к метрикам |
| `photo-service/app/integrations/storage.py` | delete_file (для компенсации) |
| `photo-service/app/main.py` | /metrics эндпоинт, photos_pending gauge |
| `photo-service/app/api/metrics_middleware.py` | RED middleware, http_* метрики |
| `photo-service/app/services/outbox.py` | kafka_publish_errors_total |
| `photo-service/migrations/versions/v001_init_photos.py` | TASK-001 миграция |
| `photo-service/migrations/versions/v002_add_analysis_and_batches.py` | TASK-002 миграция (схема полная) |
| `photo-service/tests/test_worker_consumer.py` | Тесты consumer (завязаны на текущее поведение) |
| `photo-service/tests/test_batch_service.py` | Тесты batch_service (завязаны на mark_completed) |

---

## F1 — at-least-once ломается на программных ошибках

**Файлы:** `app/worker/consumer.py` (строки 52-109)

### Текущее поведение

**`consume_loop` (линии 52-66):**
```python
async def consume_loop(...) -> None:
    while not stop_event.is_set():
        try:
            message = await asyncio.wait_for(consumer.getone(), timeout=_POLL_TIMEOUT_SECONDS)
        except TimeoutError:
            continue
        
        await _handle_message(message, processor)
        await consumer.commit()  # ← БЕЗУСЛОВНЫЙ commit, даже если _handle_message упала
```

**`_handle_message` (линии 74-108):**
- Линия 76-81: JSON-ошибка → логируется, `return` без исключения (poison-pill)
- Линия 89-96: Отсутствие photo_id/object_key → логируется, `return` без исключения (poison-pill)
- Линия 98-108: `except Exception: logger.exception(...) ` — ловит ВСЕ исключения, не пробрасывает
  - Если `processor.process()` упадёт с неожиданным исключением (БД недоступна, timeout внутри процесса), оно залогируется
  - Затем `_handle_message` вернётся без исключения
  - `consume_loop` линия 66 выполнит `await consumer.commit()` → **сообщение теряется**

### Проблема

- **at-least-once гарантия нарушена**: при любом исключении в `process()` (не только гонка двух worker-ов, но и программный баг) сообщение закоммичивается и теряется
- Poison-pill (битый JSON, отсутствие полей) корректно пропускаются и коммичатся (правильное поведение)
- Нужно различать:
  - **poison-pill** (недоставляемое сообщение) → commit, пропустить
  - **неожиданное исключение** (программный баг) → NO commit, залогировать, пауза (анти-hot-loop)

### Завязанные тесты

- `tests/test_worker_consumer.py:TestConsumeLoop::test_processes_one_message_then_commits_offset` (линии 193-205) — ожидает commit после process
- `tests/test_worker_consumer.py:TestConsumeLoop::test_poison_pill_message_still_commits_and_does_not_kill_loop` (линии 259-270) — ожидает commit для poison-pill
- `tests/test_worker_consumer.py:TestHandleMessageValid::test_unhandled_exception_from_processor_is_logged_not_raised` (линии 182-189) — показывает текущее поведение (исключение логируется, но не пробрасывается)

**Потребуется переписать эти тесты** (поведение commit меняется).

---

## F2 — лимиты батча срабатывают после материализации в RAM

**Файлы:** 
- `app/api/batches.py` (строка 35)
- `app/api/photos.py` (строка 58)
- `app/services/photo_service.py` (строки 57–214)

### Текущее поведение

**`upload_batch` (app/api/batches.py:29-36):**
```python
@router.post("/photos/batch", status_code=202, response_model=BatchAcceptedResponse)
async def upload_batch(
    file: list[UploadFile] = File(...),
    service: PhotoService = Depends(get_photo_service),
    session: AsyncSession = Depends(get_session),
) -> BatchAcceptedResponse:
    files = [(f.filename, await f.read()) for f in file]  # ← ВСЕ файлы читаются целиком!
    return await service.create_batch(session, files)
```

**`upload_photo` (app/api/photos.py:52-59):**
```python
@router.post("", status_code=202, response_model=UploadPhotoResponse)
async def upload_photo(
    file: UploadFile = File(...),
    service: PhotoService = Depends(get_photo_service),
    session: AsyncSession = Depends(get_session),
) -> UploadPhotoResponse:
    data = await file.read()  # ← Весь файл в памяти
    return await service.create_photo(session, file.filename, data)
```

**`create_batch` (app/services/photo_service.py:184-267):**
- Линия 200-204: Проверка `MIN_BATCH_SIZE <= len(files) <= MAX_BATCH_SIZE` ← **ПОСЛЕ чтения всех файлов**
- Линия 209-214: Проверка `total_size > BATCH_MAX_TOTAL_BYTES` ← **ПОСЛЕ чтения всех файлов**
- Лимиты: `MIN_BATCH_SIZE=2`, `MAX_BATCH_SIZE=10`, `BATCH_MAX_TOTAL_BYTES=500MB` (50MB×10)

**Сигнатуры константы (строки 57-67):**
```python
MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB
MIN_BATCH_SIZE = 2
MAX_BATCH_SIZE = 10
BATCH_MAX_TOTAL_BYTES = MAX_BATCH_SIZE * MAX_FILE_SIZE_BYTES  # 500 MB
```

**`_validate` (линии 103-118):**
```python
def _validate(self, data: bytes) -> tuple[str, str]:
    if len(data) == 0:
        raise ValidationError("Uploaded file is empty")
    if len(data) > MAX_FILE_SIZE_BYTES:  # ← 413 Payload Too Large
        raise PayloadTooLargeError("File exceeds the maximum allowed size (50 MB)")
    if data[:3] == _JPEG_MAGIC:
        return "jpg", "image/jpeg"
    if data[:8] == _PNG_MAGIC:
        return "png", "image/png"
    raise UnsupportedMediaTypeError("Only JPEG/PNG images are supported")
```

### Проблема

1. **Одиночная загрузка** (`POST /photos`): файл читается весь перед валидацией
2. **Батч** (`POST /photos/batch`): все файлы читаются в памяти ДО проверок количества и суммарного размера
3. **Отсутствует streaming** или cappedread: если клиент пошлёт 11 файлов или 500MB, всё будет загружено в RAM
4. **Отсутствует early-exit**: если первый файл >50MB, остальное не читается; но если 10 файлов ×50MB и все читаются до проверки суммы

### Завязанные тесты

- Потребуются новые тесты F2 (нет текущих, специфичных этому)
- `tests/test_photo_service.py` — тесты create_photo могут быть связаны с моком `file.read()`
- `tests/test_photo_service_batch.py` — тесты create_batch

---

## F3 — GET /v1/batches/{id} пишет в БД

**Файлы:** 
- `app/services/batch_service.py` (строки 64-102)
- `app/services/analysis_processor.py` (строки 59-155)
- `app/repositories/batch_repository.py` (строки 38-50)

### Текущее поведение

**`get_batch` (app/services/batch_service.py:64-102):**
```python
async def get_batch(self, session: AsyncSession, batch_id: uuid.UUID) -> BatchResponse:
    batch = await self._repository.get_by_id(session, batch_id)
    if batch is None:
        raise NotFoundError("Batch not found")
    
    photos = batch.photos
    all_terminal = bool(photos) and all(p.status in _TERMINAL_STATUSES for p in photos)
    
    if all_terminal:
        status = "completed"
        best_photo_id = select_best_photo(photos)
        if batch.status != "completed":
            await self._repository.mark_completed(session, batch.batch_id, best_photo_id)  # ← ПИШЕТ!
            await session.commit()  # ← КОММИТИТ!
    else:
        status = "processing"
        best_photo_id = None
    
    return BatchResponse(...)
```

**`mark_completed` (app/repositories/batch_repository.py:38-50):**
```python
async def mark_completed(
    self, session: AsyncSession, batch_id: uuid.UUID, best_photo_id: uuid.UUID | None
) -> None:
    await session.execute(
        update(Batch)
        .where(Batch.batch_id == batch_id)
        .values(status="completed", best_photo_id=best_photo_id)
    )
```

**Проблема:** 
- `get_batch` вызывается на GET, которая должна быть read-only
- Параллельные GET на одном batch_id → конкурирующие UPDATE
- GET не идемпотентна (ретраи прокси пишут в БД)
- Батч не станет completed без GET (что если никто не запросит?)

**`select_best_photo` (линии 37-57):**
```python
def select_best_photo(photos: list[Photo]) -> uuid.UUID | None:
    candidates = [p for p in photos if p.status == PhotoStatus.done and p.analysis is not None]
    if not candidates:
        return None
    
    best = min(
        candidates,
        key=lambda p: (
            p.analysis.is_blurred,
            p.analysis.blur_score,
            -p.analysis.faces_count,
            p.created_at,
            p.photo_id,
        ),
    )
    return best.photo_id
```
→ **Эта функция чистая, без I/O, должна остаться неизменной.**

### Где должна быть логика завершения батча

**`analysis_processor.py` (линии 59-155):** После терминальной записи фото (mark_done или mark_failed):
- Линия 131-132: `await self._analysis.upsert(...)` + `await self._photos.mark_done(...)`
- Линия 142: `await self._photos.mark_failed(...)`

После этих операций нужна логика:
1. Проверить `photo.batch_id` (if not None)
2. Если есть batch_id, получить все фото батча
3. Проверить, все ли фото терминальны
4. Если да → атомарный UPDATE с WHERE status='processing'

### Завязанные тесты

**`tests/test_batch_service.py:TestGetBatch` (линии 170-265):**
- Линия 206: `repository.mark_completed.assert_awaited_once_with(...)` — ожидается call на mark_completed
- Линия 207: `session.commit.assert_awaited_once()` — ожидается commit
- Линия 192-207: `test_completed_status_with_best_photo_id_when_all_terminal` — проверяет, что get_batch вызывает mark_completed

**Потребуется полная переписать get_batch тесты** (удалить проверки mark_completed).

---

## F4 — photos_pending задвоен

**Файлы:**
- `app/integrations/metrics.py` (строки 1-101) — монолит со всеми метриками
- `app/main.py` (строка 27) — импортирует photos_pending
- `app/api/metrics_middleware.py` (строка 23) — импортирует http_*
- `app/services/photo_service.py` (строка 43) — импортирует photos_uploaded_total, storage_upload_errors_total
- `app/services/outbox.py` (строка 23) — импортирует kafka_publish_errors_total
- `app/services/analysis_processor.py` (строки 28-35) — импортирует worker метрики
- `app/worker/main.py` (строка 20) — `start_http_server(WORKER_METRICS_PORT)`
- `prometheus.yml` (строки 9-15) — scrape_configs с job_name="photo-api" (порт 8000) и "photo-worker" (порт 8001)
- `grafana/dashboards/photo-service.json` (линия 61) — запрос `photos_pending` БЕЗ фильтра по job

### Текущее поведение

**Метрики в metrics.py:**
```python
# API metrics
photos_uploaded_total = Counter(...)
http_requests_total = Counter(...)
http_request_duration_seconds = Histogram(...)
storage_upload_errors_total = Counter(...)
kafka_publish_errors_total = Counter(...)
photos_pending = Gauge(...)

# Worker metrics
photo_analysis_started_total = Counter(...)
photo_analysis_completed_total = Counter(...)
photo_analysis_failed_total = Counter(...)
photo_analysis_duration_seconds = Histogram(...)
analyzer_grpc_errors_total = Counter(...)
worker_messages_processed_total = Counter(...)
```

**Импорты по модулям:**

| Модуль | Импортирует |
|--------|-------------|
| `app/main.py` | `photos_pending` |
| `app/api/metrics_middleware.py` | `http_request_duration_seconds`, `http_requests_total` |
| `app/services/photo_service.py` | `photos_uploaded_total`, `storage_upload_errors_total` |
| `app/services/outbox.py` | `kafka_publish_errors_total` |
| `app/services/analysis_processor.py` | `photo_analysis_started_total`, `photo_analysis_completed_total`, `photo_analysis_failed_total`, `photo_analysis_duration_seconds`, `analyzer_grpc_errors_total`, `worker_messages_processed_total` |

**Проблема:**
- Все метрики в `metrics.py`, который импортируется обоими процессами
- API на python -m app.main (main process) загружает prometheus_client REGISTRY с ВСЕ метриками
- Worker на python -m app.worker.main (separate process) загружает отдельный REGISTRY со ВСЕ метриками
- API экспортирует /metrics (строка 129 app/main.py): `generate_latest(REGISTRY)` → все 11 метрик (API + worker co=0)
- Worker экспортирует /metrics на порту 8001 (app/worker/main.py:55): `start_http_server(WORKER_METRICS_PORT)` → все 11 метрик (worker + api co=0)

**Дашборд запрос (grafana/dashboards/photo-service.json:61):**
```json
{"expr": "photos_pending", "legendFormat": "pending"}
```
→ Две серии: photo-api{instance="api:8000"} с реальным значением, photo-worker{instance="worker:8001"} с нулём

### Таблица текущих метрик и их типов

| Метрика | Тип | Целевой процесс |
|---------|-----|-----------------|
| `photos_uploaded_total` | Counter | API |
| `http_requests_total` | Counter | API |
| `http_request_duration_seconds` | Histogram | API |
| `storage_upload_errors_total` | Counter | API |
| `kafka_publish_errors_total` | Counter | API |
| `photos_pending` | Gauge | API |
| `photo_analysis_started_total` | Counter | Worker |
| `photo_analysis_completed_total` | Counter | Worker |
| `photo_analysis_failed_total` | Counter | Worker |
| `photo_analysis_duration_seconds` | Histogram | Worker |
| `analyzer_grpc_errors_total` | Counter | Worker |
| `worker_messages_processed_total` | Counter | Worker |

---

## F5 — MinIO-сирота при падении commit

**Файлы:**
- `app/services/photo_service.py` (строки 125-182 create_photo, 184-267 create_batch)
- `app/integrations/storage.py` (строки 104-115 delete_file)

### Текущее поведение

**`create_photo` (строки 125-182):**
```python
async def create_photo(
    self, session: AsyncSession, filename: str | None, data: bytes
) -> UploadPhotoResponse:
    ext, content_type = self._validate(data)
    
    photo_id = uuid.uuid4()
    object_key = f"photos/{photo_id}/original.{ext}"
    
    photo = Photo(...)
    
    await self._repository.create(session, photo)  # flush
    logger.info("upload received", ...)
    
    try:
        async with asyncio.timeout(STORAGE_TIMEOUT_SECONDS):
            await anyio.to_thread.run_sync(
                self._storage.save_file, object_key, data, len(data), content_type
            )
    except (StorageUnavailable, TimeoutError) as exc:
        await session.rollback()  # ← Откатываем, если MinIO упадёт
        storage_upload_errors_total.inc()
        logger.warning("MinIO unavailable on upload, rolled back", ...)
        raise StorageUnavailable(...) from exc
    
    logger.info("stored in MinIO", ...)
    
    await session.commit()  # ← НЕ обёрнут в try/except!
    photos_uploaded_total.inc()
    logger.info("photo row committed", ...)
    
    return UploadPhotoResponse(...)
```

**`create_batch` (строки 184-267):**
```python
async def create_batch(self, session: AsyncSession, files: list[tuple[str | None, bytes]]) -> BatchAcceptedResponse:
    # ... валидация и создание batch
    
    photos: list[Photo] = []
    saved_object_keys: list[str] = []
    
    try:
        for filename, data, ext, content_type in validated:
            # ... создание photo, flush
            await self._repository.create(session, photo)
            
            async with asyncio.timeout(STORAGE_TIMEOUT_SECONDS):
                await anyio.to_thread.run_sync(
                    self._storage.save_file, object_key, data, len(data), content_type
                )
            saved_object_keys.append(object_key)
            photos.append(photo)
    except (StorageUnavailable, TimeoutError) as exc:
        await session.rollback()
        storage_upload_errors_total.inc()
        logger.warning("MinIO unavailable during batch upload, rolled back", ...)
        for object_key in saved_object_keys:
            await anyio.to_thread.run_sync(self._storage.delete_file, object_key)  # ← best-effort delete
        raise StorageUnavailable("Storage is unreachable") from exc
    
    await session.commit()  # ← НЕ обёрнут в try/except!
    photos_uploaded_total.inc(len(photos))
    ...
```

**Проблема:**
- Файл(ы) уже в MinIO
- Если `session.commit()` упадёт (БД недоступна, constraint violation, etc.), транзакция откатывается
- Но объекты в MinIO остаются (сироты)
- Нет компенсирующего `delete_file` при commit-ошибке

**`delete_file` (app/integrations/storage.py:104-115):**
```python
def delete_file(self, object_name: str) -> None:
    """Best-effort delete, used as compensation for an orphaned object."""
    try:
        self._client.remove_object(self._bucket, object_name)
    except S3Error as exc:
        if exc.code == "NoSuchKey":
            return
        logger.warning("failed to delete orphaned object from MinIO: %s", exc)
```
→ Есть метод, который можно использовать для компенсации.

### Требуемый fix

Оба пути (`create_photo` и `create_batch`) должны обернуть commit в try/except:
- Если commit упадёт → best-effort delete всех сохранённых объектов → raise 503 StorageUnavailable

---

## F6 — нет restart-политик в compose

**Файл:** `docker-compose.yml` (строки 1-192)

### Текущее состояние

Ни один сервис НЕ имеет поля `restart:`:
```yaml
postgres:
  image: postgres:16
  # нет restart
  ...

minio:
  image: minio/minio
  # нет restart
  ...

kafka:
  image: apache/kafka:3.7.0
  # нет restart
  ...

analyzer-stub:
  build: ...
  # нет restart
  ...

api:
  build: .
  # нет restart
  ...

worker:
  build: .
  # нет restart
  ...

prometheus:
  image: prom/prometheus
  # нет restart
  ...

grafana:
  image: grafana/grafana
  # нет restart
  ...
```

### Требуемый fix

Добавить `restart: unless-stopped` ко всем сервисам.

---

## F7 — insecure gRPC-канал

**Файлы:**
- `app/integrations/analyzer_client.py` (строки 42-62)
- `app/core/config.py` (строки 36-44)

### Текущее состояние

**`AnalyzerGrpcClient.__init__` (строки 45-49):**
```python
def __init__(self, addr: str, timeout: float) -> None:
    self._addr = addr
    self._timeout = timeout
    self._channel = grpc.aio.insecure_channel(addr)  # ← insecure
    self._stub = analyzer_pb2_grpc.PhotoAnalyzerStub(self._channel)
```

**Settings (app/core/config.py):**
```python
ANALYZER_GRPC_ADDR: str = "analyzer-stub:50051"
ANALYZER_GRPC_TIMEOUT: float = 30.0
```
→ Нет переменной для TLS конфигурации.

### Контекст (F7 — решение за architect)

- Для MVP в локальном docker-compose (все сервисы в одной сети) insecure допустимо
- Для production (analyzer-stub за пределами внутренней сети) нужен TLS
- **Решение**: либо:
  1. Конфигурируемый TLS (env `ANALYZER_GRPC_TLS`, путь к root cert, по умолчанию выкл)
  2. Аргументированный дефер в TASK-003 (когда будет реальный analyzer)
- **Выбор** — за architect в 20_design.md

---

## F8 — полная загрузка файлов в ОЗУ (закрывается F2)

Замечание «file.read() — путь к нехватке памяти» закрывается капнутыми чтениями из F2.

**Отдельно документировать:** граница между стримингом в MinIO (вне скоупа TASK-002.1) и cappedread в памяти (в скоупе).

---

## БД и миграции

**Текущие миграции:**
- `migrations/versions/v001_init_photos.py` — TASK-001 схема (photos таблица)
- `migrations/versions/v002_add_analysis_and_batches.py` — TASK-002 схема (analysis_results, batches, photo.batch_id)

**Статус:** Новых миграций НЕ требуется (F1–F8 не меняют схему). v002 содержит всё необходимое.

---

## Интерфейсы и контракты

**HTTP API (не меняются):**
- POST /v1/photos → 202 UploadPhotoResponse
- POST /v1/photos/batch → 202 BatchAcceptedResponse
- GET /v1/batches/{batch_id} → BatchResponse (сигнатура не меняется, только КТО пишет changed)
- GET /metrics → Prometheus text-exposition

**gRPC:**
- AnalyzerGrpcClient.analyze(photo_id, object_key) → AnalyzePhotoResponse
- Остаётся, конфиг TLS решается в 20_design.md

**Kafka топики и сообщения (не меняются):**
- photo.analysis.requested: {photo_id, object_key, created_at, trace_id}

---

## Environment переменные

**Текущие (app/core/config.py):**
```python
DATABASE_URL: str = "postgresql+asyncpg://photo:photo@postgres:5432/photo_db"
MINIO_ENDPOINT: str = "minio:9000"
MINIO_ACCESS_KEY: str = "minioadmin"
MINIO_SECRET_KEY: str = "minioadmin"
MINIO_BUCKET: str = "photos"
MINIO_SECURE: bool = False
LOG_LEVEL: str = "INFO"
KAFKA_BOOTSTRAP_SERVERS: str = "kafka:9092"
KAFKA_TOPIC_ANALYSIS_REQUESTED: str = "photo.analysis.requested"
KAFKA_CONSUMER_GROUP: str = "photo-analysis-workers"
KAFKA_PUBLISH_TIMEOUT_SECONDS: float = 10.0
ANALYZER_GRPC_ADDR: str = "analyzer-stub:50051"
ANALYZER_GRPC_TIMEOUT: float = 30.0
WORKER_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_BASE_SECONDS: float = 1.0
WORKER_METRICS_PORT: int = 8001
OUTBOX_POLL_INTERVAL_SECONDS: float = 2.0
OUTBOX_BATCH_SIZE: int = 100
```

**Новые (может добавить architect в F7):**
- `ANALYZER_GRPC_TLS` (опционально)
- `ANALYZER_GRPC_ROOT_CERT_PATH` (опционально)

---

## Зависимости

**Главные зависимости проекта (из pyproject.toml, не читал полностью, но видно из импортов):**
- `fastapi` — веб-фреймворк
- `sqlalchemy` — ORM
- `alembic` — миграции
- `aiokafka` — async Kafka producer/consumer
- `grpcio`, `grpcio-tools` — gRPC
- `prometheus-client` — метрики
- `minio` — S3-compatible storage
- `pytest`, `pytest-asyncio` — тестирование
- `pydantic`, `pydantic-settings` — конфиг, валидация

---

## Ключевые сигнатуры и точки изменения

### Ключевые методы

| Метод | Сигнатура | Где меняется |
|-------|-----------|-------------|
| `consume_loop` | `async def(consumer, processor, stop_event)` | F1: логика commit |
| `_handle_message` | `async def(message, processor)` | F1: логика commit |
| `upload_batch` (API) | `async def(file: list[UploadFile], ...) → BatchAcceptedResponse` | F2: cappedread |
| `upload_photo` (API) | `async def(file: UploadFile, ...) → UploadPhotoResponse` | F2: cappedread |
| `create_batch` | `async def(session, files: list[tuple]) → BatchAcceptedResponse` | F2: cappedread, F5: commit компенсация |
| `create_photo` | `async def(session, filename, data: bytes) → UploadPhotoResponse` | F5: commit компенсация |
| `get_batch` | `async def(session, batch_id) → BatchResponse` | F3: удалить mark_completed + commit |
| `process` (AnalysisProcessor) | `async def(session, photo_id, object_key)` | F3: добавить логику завершения batch |
| `mark_completed` (BatchRepository) | `async def(session, batch_id, best_photo_id)` | F3: сигнатура не меняется |

### Новые методы

- **F3:** Возможно, новый метод в BatchRepository для получения batch_id фото или проверки всех фото батча по batch_id
- **F4:** Раскол metrics.py на metrics_api.py и metrics_worker.py + импорты

---

## Риски и смежные места

### Риск 1: F1 — infinite retry loop
Если неожиданное исключение повторяется (например, БД навсегда недоступна), worker будет в infinite retry loop. Решение: пауза 1c между ретраями → anti-hot-loop.

### Риск 2: F2 — streaming
Cappedread в памяти — не решает полной проблемы памяти (все 10×50MB всё ещё в RAM одновременно). Streaming в MinIO (вне скоупа) нужен для >> 500MB батчей. Документировать граничный случай.

### Риск 3: F3 — гонка завершения батча
Если два worker-а одновременно обрабатывают последние два фото батча, оба могут попытаться завершить batch. Защита: `UPDATE ... WHERE status='processing'` → только один UPDATE изменит строку, второй получит rowcount=0.

### Риск 4: F4 — cardinality в Prometheus
После раскола metrics на два, нужно убедиться, что prometheus.yml корректно скрейпит оба эндпоинта с разными job_name → разные серии в дашборде.

### Риск 5: F5 — race condition delete
Если MinIO delete_file упадёт (например, сеть временно вниз), объект остаётся сиротой. Решение: best-effort, логировать, вне скоупа — manual cleanup.

### Риск 6: F6 — restart unless-stopped
После перезагрузки контейнера он перезапустится автоматически. Нужно убедиться, что app/worker graceful shutdown не ломается при SIGTERM от Docker.

### Риск 7: F7 — TLS позже
Если выбрано отложить TLS на TASK-003, нужна ясная документация в 20_design.md о том, когда и как это будет реализовано.

---

## Тесты и coverage

**Тесты, которые потребуют обновления:**
- `tests/test_worker_consumer.py` — переписать логику commit (F1)
- `tests/test_batch_service.py` — удалить проверки mark_completed из get_batch (F3)

**Новые тесты (обязательно по спеке F1–F3):**
1. Consumer: неожиданное исключение → NO commit
2. Consumer: poison-pill → commit
3. Batch: 11 файлов отклоняются до чтения
4. Batch: файл >50MB прерывает на капе (413)
5. Batch: суммарный лимит срабатывает по бегущей сумме
6. Worker: завершение batch (все фото терминальны) без GET
7. Metrics: реестр/эндпоинт worker НЕ содержит photos_pending
8. Metrics: дашборд pending даёт одну серию
9. Compensation: commit-ошибка → delete_file + 503

---

## Команды и Geyts

**pytest маркеры (из pyproject.toml — не читал полностью, но видно из конвенции):**
- `pytest` — все тесты
- `pytest -v` — verbose
- `pytest -k test_worker_consumer` — specific test file

**ruff:**
- `ruff check app/` — lint
- `ruff format app/` — format

**Гейт перед coder:**
✓ spec + 10_context.md + 20_design.md (architect дал решение по F7 TLS)

---
task_id: TASK-002
agent: architect
model: opus
status: done
inputs:
  - specs/constitution.md
  - specs/feature-upload/tasks.md  (§"TASK-002")
  - tasks/TASK-002/10_context.md
  - tasks/TASK-001/20_design.md   (стиль/границы, справочно)
outputs:
  - tasks/TASK-002/20_design.md
spec_refs:
  - feature-upload/tasks.md §"TASK-002" (блоки A/B/C, таблица «Зафиксированные решения», DoD воркшопа 3, слайды 9-38)
  - constitution.md §2.2, §2.4, §3.1, §3.2, §3.4
timestamp: 2026-07-17T00:00:00Z
---

# TASK-002 — Дизайн: Асинхронный конвейер анализа + батчи + наблюдаемость

> Все пути в документе — относительно `photo-service/` (корень сервиса), если не сказано иное.
> Код лежит в пакете `app/`; проект собирается hatchling (`packages=["app"]`), тесты запускаются локально через `uv run pytest` (НЕ в Docker) — это ключевой фактор для решения по gRPC-стабам (§7).

## 0. Соответствие конституции (self-check)

- **Микросервисы, HTTP/gRPC (§2.1/§2.2):** photo-service (API, HTTP) + worker (consumer) + analyzer-stub (gRPC, external-service-заглушка). gRPC-вызов analyzer из worker — ровно то, что предусматривает §2.2 для внутреннего обмена.
- **Kafka (§2.4):** топик `photo.analysis.requested` (воркшоп переопределяет `photos-to-analyze` — зафиксировано в спеке, воркшоп главнее). Ключ партиционирования — `photo_id`. Топика результатов НЕТ (worker пишет в PostgreSQL напрямую — зафиксировано).
- **Идемпотентность/ретраи/DLQ (§2.4):** идемпотентность консьюмера — атомарный `UPDATE ... WHERE status='pending'`; offset-коммит только после обработки (at-least-once); ретраи временных ошибок с backoff, лимит 3; **DLQ упрощён до статуса `failed`** (зафиксировано в спеке — dead-letter топик вне скоупа). Явное осознанное отступление от §2.4, разрешённое спекой.
- **Наблюдаемость (§3.1):** JSON-логи с `trace_id` (сквозной upload → Kafka → worker), `photo_id`, `attempt`; Prometheus-метрики RED (http_*) + доменные; Grafana-дашборд. Jaeger/трейсинг вне скоупа воркшопа 3.
- **Отказоустойчивость (§3.2):** таймауты на gRPC/БД/Kafka; MinIO/Kafka down → upload не падает (outbox), 503 вместо 500 при недоступности хранилища; graceful shutdown worker (commit offset → закрыть consumer → gRPC → engine).
- **Acks:** producer `acks=all` (§2.4). Реплики 1 (один брокер KRaft — зафиксировано как упрощение MVP; в проде rf=3/min_isr=2 из §2.4).
- **K8s** — вне скоупа (TASK-003).

Противоречий с конституцией нет. Осознанные отступления (разрешены спекой воркшопа): нет DLQ-топика (→ `failed`), rf=1 вместо 3, нет JWT/трейсинга.

---

## 1. Обзор архитектуры «было → станет»

### Было (после TASK-001)
`compose: postgres, minio, api`. API синхронно принимает upload → MinIO + строка `photos(status=pending)`. Конвейер анализа отсутствует; `analyzer_client.py` — заглушка `NotImplementedError`; `protos/analyzer.proto` — только контракт.

### Станет
`compose: postgres, minio, kafka, api, worker, analyzer-stub, prometheus, grafana`.

Потоки данных:

```
        (1) POST /v1/photos[/batch]
Client ───────────────────────────► api ──► MinIO (bytes)
                                      │
                                      ├──► photos(status=pending, publish_status=not_sent, trace_id)  [commit]
                                      │
                (2) outbox loop       ▼
        api background task: SELECT publish_status='not_sent'
                                      │  publish {photo_id,object_key,created_at,trace_id}
                                      ▼
                             Kafka topic  photo.analysis.requested   (key=photo_id, acks=all)
                                      │  (3) consume (group=photo-analysis-workers, manual commit)
                                      ▼
                                   worker
                                      │  (3a) атомарный claim: UPDATE ...WHERE status='pending'  (0 строк → skip)
                                      │  (3b) gRPC AnalyzePhoto(photo_id, object_key) ──► analyzer-stub :50051
                                      │  (3c) success → INSERT analysis_results + status='done'
                                      │       transient err → retry(backoff, ≤3) → иначе status='failed'
                                      │       permanent err → status='failed' сразу
                                      ▼  (3d) commit offset ПОСЛЕ терминального решения
                                  PostgreSQL

api /metrics :8000  ─┐
worker /metrics :8001 ├──► Prometheus :9090 ──► Grafana :3000 (provisioned dashboard, 4 графика)
                     ─┘
```

Клиент опрашивает `GET /v1/photos/{id}` (появляется `analysis`) и `GET /v1/batches/{id}` (статус батча + `best_photo_id`).

### Границы изменений

| Файл | Действие |
|------|----------|
| `app/db/models.py` | +поля Photo (attempts, last_error_*, publish_status, published_at, trace_id, batch_id, relationship analysis); +модели `AnalysisResult`, `Batch` |
| `migrations/versions/v002_add_analysis_and_batches.py` | **Новая** ревизия (down_revision="v001") |
| `app/core/config.py` | +Kafka/analyzer/outbox/worker-metrics поля Settings |
| `app/schemas/photos.py` | +`AnalysisResultResponse`; `PhotoResponse.analysis: … \| None`; +`BatchPhotoItem`, `BatchAcceptedResponse`, `BatchResponse` |
| `app/repositories/photo_repository.py` | +claim/mark_done/mark_failed/record_attempt/outbox/get-with-analysis |
| `app/repositories/analysis_result_repository.py` | **Новый** |
| `app/repositories/batch_repository.py` | **Новый** |
| `app/services/photo_service.py` | +`create_batch`; `get_photo`/`list_photos` отдают `analysis`; upload проставляет publish_status/trace_id |
| `app/services/batch_service.py` | **Новый** (get_batch + формула лучшего кадра) |
| `app/services/analysis_processor.py` | **Новый** — доменная обработка одного сообщения (claim→gRPC→persist), переиспользуется worker-ом |
| `app/services/outbox.py` | **Новый** — фоновый publisher-цикл |
| `app/integrations/kafka_producer.py` | **Новый** — обёртка AIOKafkaProducer |
| `app/integrations/analyzer_client.py` | Переписать: реальный `grpc.aio`-клиент + классификация ошибок |
| `app/integrations/metrics.py` | **Новый** — реестр метрик (общий модуль имён) |
| `app/grpc_gen/` | **Новый** пакет: закоммиченные `analyzer_pb2.py`, `analyzer_pb2_grpc.py`, `__init__.py` |
| `app/api/photos.py` | GET/{id},list возвращают analysis; +/metrics wiring не здесь |
| `app/api/batches.py` | **Новый** роутер (POST /v1/photos/batch, GET /v1/batches/{id}) |
| `app/api/metrics_middleware.py` | **Новый** — RED-метрики HTTP |
| `app/worker/__init__.py`, `app/worker/main.py`, `app/worker/consumer.py` | **Новый** пакет worker |
| `app/main.py` | lifespan: producer + outbox task; mount /metrics; metrics middleware; (опц.) kafka в /readyz |
| `analyzer-stub/` (server.py, Dockerfile, requirements) | **Новый** внешний сервис-заглушка |
| `docker-compose.yml` | +kafka, worker, analyzer-stub, prometheus, grafana |
| `prometheus.yml`, `grafana/provisioning/**`, `grafana/dashboards/photo-service.json` | **Новые** конфиги наблюдаемости |
| `pyproject.toml`, `.env.example` | +зависимости и переменные |
| `tests/` | Правки существующих + новые юнит-тесты (§13) |

**Dockerfile НЕ меняется** для api/worker (worker — тот же образ, другой `command`; стабы закоммичены внутри `app/`). analyzer-stub — свой минимальный Dockerfile (§6).

---

## 2. Схема БД v002 (миграция, точные типы)

**Ревизия:** `v002`, `down_revision="v001"`. v001 НЕ трогаем (в проде=main).

**Enum-ы:** новых PG-enum **не вводим**. Все новые статус-поля — `VARCHAR + CHECK` (публикация/батч имеют по 2 значения). Это осознанно убирает риск `DuplicateObjectError` из TASK-000. `photos.status` (enum `photo_status`) остаётся из v001 без изменений.
> Если кодер всё же захочет enum — ОБЯЗАН применить паттерн v001: `postgresql.ENUM(..., create_type=False)` + явный `.create(bind, checkfirst=True)` в upgrade и `.drop(bind, checkfirst=True)` в downgrade. Но рекомендация — VARCHAR+CHECK.

### 2.1 `analysis_results`
| Колонка | Тип | Ограничения |
|---------|-----|-------------|
| photo_id | UUID | **PK**, FK→photos(photo_id) ON DELETE CASCADE |
| faces_count | INTEGER | NOT NULL |
| is_blurred | BOOLEAN | NOT NULL |
| blur_score | DOUBLE PRECISION | NOT NULL |
| perceptual_hash | VARCHAR(64) | NOT NULL |
| created_at | TIMESTAMPTZ | NOT NULL DEFAULT now() |

(1:1 к photo; PK=photo_id гарантирует один результат на фото → идемпотентность записи.)

### 2.2 `batches`
| Колонка | Тип | Ограничения |
|---------|-----|-------------|
| batch_id | UUID | **PK** (app-side uuid4, без server_default) |
| status | VARCHAR(20) | NOT NULL DEFAULT 'processing', CHECK IN ('processing','completed') |
| best_photo_id | UUID | NULL, FK→photos(photo_id) |
| created_at | TIMESTAMPTZ | NOT NULL DEFAULT now() |

Индекс: `ix_batches_created_at (created_at)`.

### 2.3 ALTER `photos` (новые колонки)
| Колонка | Тип | Ограничения | Назначение |
|---------|-----|-------------|-----------|
| attempts | INTEGER | NOT NULL DEFAULT 0 | число попыток анализа |
| last_error_code | VARCHAR(64) | NULL | код последней ошибки (gRPC status name/`TIMEOUT`) |
| last_error_message | TEXT | NULL | усечённое сообщение |
| publish_status | VARCHAR(20) | NOT NULL DEFAULT 'not_sent', CHECK IN ('not_sent','sent') | упрощённый outbox |
| published_at | TIMESTAMPTZ | NULL | момент успешной публикации |
| trace_id | VARCHAR(64) | NULL | request_id из middleware, для сквозной трассировки в Kafka/worker |
| batch_id | UUID | NULL, FK→batches(batch_id) | привязка к батчу (NULL для одиночной загрузки) |

> **Решение архитектора (сверх перечня оркестратора):** добавлена колонка `trace_id`. Причина: outbox-публикация выполняется в фоне, вне HTTP-запроса, поэтому `trace_id_var` там недоступен; сообщение в Kafka по контракту обязано нести `trace_id` (сквозная трассировка §3.1). Храним его на строке фото. Если NULL — publisher подставит сгенерированный uuid4 (деградация, не отказ).

Индексы:
- `ix_photos_publish_status` — **частичный**: `(publish_status) WHERE publish_status='not_sent'` (эффективный опрос outbox).
- `ix_photos_batch_id (batch_id)` — выборка фото батча.
- `ix_photos_status (status)` — для gauge `photos_pending` (`count(*) WHERE status='pending'`).

**Порядок в upgrade():** (1) create `analysis_results`; (2) create `batches` (+индекс) — её FK best_photo_id ссылается на уже существующие `photos`; (3) ALTER `photos` ADD COLUMNs (batch_id FK ссылается на уже созданные `batches`); (4) CHECK-констрейнты; (5) индексы. **downgrade():** обратный порядок — сначала drop индексы и `photos`-колонки (снимет FK batch_id и CHECK), затем drop `analysis_results`, затем `batches`.

**Гейт:** интеграционный тест миграции на реальном Postgres (testcontainers) — upgrade head + downgrade v001 + проверка DDL (см. §13).

---

## 3. Контракты

### 3.1 Kafka-сообщение (топик `photo.analysis.requested`)
- Ключ (bytes): `photo_id` (UUID-строка) — партиционирование по фото.
- Значение (JSON, UTF-8, БЕЗ файла):
```json
{
  "photo_id": "123e4567-e89b-12d3-a456-426614174000",
  "object_key": "photos/123e4567-.../original.jpg",
  "created_at": "2026-07-17T10:30:00+00:00",
  "trace_id": "req-uuid-or-generated"
}
```
- Топик: partitions=1, rf=1, retention=7d, cleanup=delete. Producer: `acks=all`, `enable_idempotence=True`.

### 3.2 gRPC (`protos/analyzer.proto` — БЕЗ изменений)
Контракт из TASK-001 полностью подходит: `PhotoAnalyzer.AnalyzePhoto(AnalyzePhotoRequest{photo_id, object_key}) → AnalyzePhotoResponse{faces_count:int32, is_blurred:bool, blur_score:double, perceptual_hash:string}`. **Proto не редактируем**, только генерируем стабы (§7).

### 3.3 HTTP-схемы (Pydantic)

```python
# app/schemas/photos.py
class AnalysisResultResponse(BaseModel):
    faces_count: int
    is_blurred: bool
    blur_score: float
    perceptual_hash: str

class PhotoResponse(BaseModel):            # РАСШИРЕНИЕ
    id: str
    filename: str
    status: Literal["pending", "processing", "done", "failed"]
    analysis: AnalysisResultResponse | None = None   # None пока не done; default → обратная совместимость

class BatchPhotoItem(BaseModel):           # элемент ответа 202
    photo_id: str
    status: Literal["pending"]

class BatchAcceptedResponse(BaseModel):    # тело POST /v1/photos/batch (202)
    batch_id: str
    photos: list[BatchPhotoItem]

class BatchPhotoDetail(BaseModel):         # элемент GET /v1/batches/{id}
    photo_id: str
    filename: str
    status: Literal["pending", "processing", "done", "failed"]
    analysis: AnalysisResultResponse | None = None

class BatchResponse(BaseModel):            # тело GET /v1/batches/{id}
    batch_id: str
    status: Literal["processing", "completed"]
    photos: list[BatchPhotoDetail]
    best_photo_id: str | None = None
```

**Обратная совместимость:** `analysis` — Optional с `default=None`, поэтому существующий контракт `GET /v1/photos` и `GET /v1/photos/{id}` не ломается (добавляется nullable-поле).

### 3.4 Эндпоинты и коды ошибок

| Метод | Путь | Успех | Ошибки |
|-------|------|-------|--------|
| POST | `/v1/photos` | 202 `UploadPhotoResponse` | 400/413/415/503/500 (как TASK-001) |
| POST | `/v1/photos/batch` | 202 `BatchAcceptedResponse` | 400 (файлов <2 или >10 / любой пустой), 413 (любой >50МБ), 415 (любой не JPEG/PNG), 503 (MinIO), 500. **Любая ошибка валидации отклоняет весь батч атомарно — ни одной строки/объекта.** |
| GET | `/v1/photos/{id}` | 200 `PhotoResponse` (+analysis когда done) | 404, 422 (не-UUID), 500 |
| GET | `/v1/photos` | 200 `list[PhotoResponse]` | 422, 500 |
| GET | `/v1/batches/{id}` | 200 `BatchResponse` | 404 (нет батча), 422, 500 |
| GET | `/metrics` (api) | 200 text/plain (Prometheus) | — |

**Новые доменные ошибки в `app/core/errors.py`:** `BatchSizeError(AppError)` → 400 `INVALID_BATCH_SIZE` (файлов <2 или >10). Прочие (пустой/большой/не-изображение) переиспользуют существующие `ValidationError`/`PayloadTooLargeError`/`UnsupportedMediaTypeError`. `KafkaUnavailable` НЕ нужен на HTTP-пути (upload не публикует синхронно — см. §4), поэтому в 503 на upload остаётся только `StorageUnavailable`.

---

## 4. Producer + Outbox (упрощённый, колонка в photos)

**Принцип:** upload НЕ обращается к Kafka. Публикация — исключительно фоновым циклом. Это даёт: (а) upload не падает и не тормозит при лежащей Kafka; (б) единственный путь публикации → нет гонки «немедленная vs фоновая публикация».

### 4.1 На стороне upload (create_photo / create_batch)
- В той же транзакции, что и строка фото: `publish_status='not_sent'`, `trace_id=trace_id_var.get()`. Коммит один.
- **Никакой публикации в Kafka здесь.** Метрика `photos_uploaded_total.inc()` — после успешного commit.

### 4.2 Фоновый publisher (asyncio-задача в lifespan API)
Модуль `app/services/outbox.py::run_outbox_publisher(sessionmaker, producer, settings, stop_event)`:
```
loop каждые OUTBOX_POLL_INTERVAL_SECONDS (деф. 2s), пока не stop_event:
  async with sessionmaker() as s:
    rows = repo.fetch_unpublished(s, limit=OUTBOX_BATCH_SIZE)   # SELECT ... WHERE publish_status='not_sent' ORDER BY created_at LIMIT n
    for r in rows:
      set trace_id_var = r.trace_id or uuid4()
      try:
        await producer.publish_analysis_requested(r.photo_id, r.object_key, r.created_at, trace_id)  # acks=all
        repo.mark_published(s, r.photo_id)   # publish_status='sent', published_at=now()
      except (KafkaError, asyncio.TimeoutError):
        kafka_publish_errors_total.inc(); log WARN; break/continue (оставить not_sent)
    await s.commit()
```
- **Гарантия:** at-least-once. Намерение зафиксировано в БД атомарно с фото → не теряется при падении Kafka/API. Дубликат публикации возможен, если `publish()` прошёл, а `mark_published`-commit — нет; это безопасно, т.к. консьюмер идемпотентен (§5).
- **Идемпотентность приёмника** покрывает дубли на стороне worker (атомарный claim, §5). Дедупликации на стороне producer не делаем (не требуется).
- **Producer** (`app/integrations/kafka_producer.py::KafkaEventProducer`): единый `AIOKafkaProducer`, `start()`/`stop()` в lifespan API, `acks='all'`, `enable_idempotence=True`, `request_timeout_ms` ≈ Kafka publish timeout 10s (§3.2). Сериализация value=JSON bytes, key=photo_id bytes.

### 4.3 lifespan API (`app/main.py`)
- startup: как сейчас (bucket, БД) + `producer = KafkaEventProducer(settings); await producer.start()` (при недоступной Kafka — не падать: лог WARN, retry внутри loop); `app.state.producer=producer`; `stop_event=asyncio.Event()`; `app.state.outbox_task = asyncio.create_task(run_outbox_publisher(...))`.
- shutdown: `stop_event.set()`; `await asyncio.wait_for(outbox_task, timeout=…)` (или cancel + await с подавлением CancelledError); `await producer.stop()`; `await engine.dispose()`.

> **Альтернатива (отклонена):** немедленная публикация в create_photo + фоновый fallback. Даёт меньшую задержку до анализа (нет ожидания poll-интервала), но вносит риск двойной публикации и тянет Kafka в HTTP-путь. Для MVP задержка ≤2s несущественна — выбран чистый транзакционный outbox.

---

## 5. Worker (consumer + gRPC + запись результата)

### 5.1 Структура `app/worker/`
- `main.py` — entrypoint (`python -m app.worker.main`): setup_logging; `start_http_server(WORKER_METRICS_PORT)` (prometheus, отдельный поток); создать `AnalyzerGrpcClient` (grpc.aio channel); создать `AIOKafkaConsumer`; запустить `consume_loop`; обработать SIGTERM/SIGINT → graceful shutdown.
- `consumer.py` — `consume_loop(consumer, processor, ...)` и разбор сообщения.
- Доменная логика — в `app/services/analysis_processor.py::AnalysisProcessor.process(session, photo_id, object_key)` (тестируется отдельно от Kafka).

Consumer: `AIOKafkaConsumer(topic, bootstrap_servers, group_id=KAFKA_CONSUMER_GROUP, enable_auto_commit=False, auto_offset_reset='earliest')`.

### 5.2 Цикл обработки одного сообщения
```
async for msg in consumer:
  payload = json.loads(msg.value)
  trace_id_var.set(payload["trace_id"])                      # сквозная трассировка
  worker_messages_processed_total.labels(result=...).inc()   # см. §8
  try:
    async with SessionLocal() as s:                          # сессия НА КАЖДОЕ сообщение (не держим открытой)
      await processor.process(s, payload["photo_id"], payload["object_key"])
  except Exception:
    log ERROR (не роняем loop)
  finally:
    await consumer.commit()                                  # offset ПОСЛЕ терминального решения (at-least-once)
```

### 5.3 `AnalysisProcessor.process`
1. **Атомарный захват** (ЗАФИКСИРОВАНО, не менять предикат): `rowcount = repo.claim_for_processing(s, photo_id)` → `UPDATE photos SET status='processing' WHERE photo_id=:id AND status='pending'`. `await s.commit()`.
   - `rowcount==0` → дубль/уже взято/терминально → лог INFO «skip duplicate», выйти (idempotent). Метрика `worker_messages_processed_total{result="skipped"}`.
   - `rowcount==1` → мы победили. `photo_analysis_started_total.inc()`; засечь `t0` для histogram.
2. **Ретрай-цикл** (in-process, backoff `1,2,4s`, `WORKER_MAX_ATTEMPTS=3`):
   ```
   for attempt in 1..MAX:
     try:
       async with asyncio.timeout(ANALYZER_GRPC_TIMEOUT):
         resp = await analyzer.analyze(photo_id, object_key)   # gRPC
       success → break
     except Exception as e:
       decision = classify(e)   # §7.2
       repo.record_attempt(s, photo_id, attempts=attempt, code, message); s.commit()   # трейл для наблюдаемости
       analyzer_grpc_errors_total.labels(code=code).inc()
       if decision == NO_RETRY: → failed, break
       if attempt == MAX:       → failed (retries exhausted), break
       await asyncio.sleep(BACKOFF[attempt-1]); continue
   ```
3. **Терминальная запись** (одна транзакция):
   - success: `analysis_repo.upsert(s, photo_id, resp)` (INSERT ... ON CONFLICT (photo_id) DO NOTHING) + `repo.mark_done(s, photo_id, attempts)` (status='done'); commit. `photo_analysis_completed_total.inc()`; `photo_analysis_duration_seconds.observe(now-t0)`.
   - failed: `repo.mark_failed(s, photo_id, code, message, attempts)` (status='failed', last_error_*); commit. `photo_analysis_failed_total.labels(reason=...).inc()`.

### 5.4 Классификация ошибок → retry/no-retry
| Источник исключения | Класс | Действие |
|---------------------|-------|----------|
| gRPC `UNAVAILABLE`, `DEADLINE_EXCEEDED`, `RESOURCE_EXHAUSTED`, `ABORTED`, `INTERNAL` | **RETRY** | backoff, ≤3 |
| `asyncio.TimeoutError` (наш таймаут вокруг вызова), обрыв канала | **RETRY** | backoff, ≤3 |
| gRPC `INVALID_ARGUMENT`, `NOT_FOUND`, `FAILED_PRECONDITION`, `UNIMPLEMENTED`, `PERMISSION_DENIED`, `UNAUTHENTICATED`, `OUT_OF_RANGE` | **NO_RETRY** | сразу `failed` |
| прочее/неожиданное | **NO_RETRY** (fail-safe, чтобы не зациклить) | сразу `failed` |

Классификатор — чистая функция `classify_grpc_error(exc) -> RetryDecision` в `analyzer_client.py`; юнит-тест по каждому коду.
`last_error_code` = имя gRPC-статуса (напр. `"UNAVAILABLE"`) или `"TIMEOUT"`/`"UNKNOWN"`; `last_error_message` — усечь до ~500 символов (без содержимого файла, §3.3).

### 5.5 Где инкрементится `attempts`
В `record_attempt` на КАЖДОЙ неуспешной попытке (persist прогресс + last_error_*). Финальное значение `attempts` совпадает с числом сделанных попыток. При success после первой попытки `attempts` остаётся 0 (можно писать фактическое число попыток в mark_done — на усмотрение кодера, но консистентно).

### 5.6 Graceful shutdown (§3.2)
SIGTERM/SIGINT → флаг остановки → выйти из `async for` → `await consumer.stop()` (закоммитит текущий offset, если обработка завершена) → `await analyzer.close()` (закрыть канал) → `await engine.dispose()`. In-flight сообщение: даём завершиться (терминальное решение + commit) до закрытия — иначе оно переобработается при рестарте (at-least-once, безопасно).

---

## 6. Analyzer-stub (внешний сервис-заглушка)

**Расположение:** `analyzer-stub/` в корне `photo-service/` (рядом с `app/`). **НЕ импортирует `app.*`** — это симулятор внешнего сервиса (constitution §2.1 «берётся как external service»).

**Решение по образу — свой минимальный Dockerfile (обоснование):**
- (а) архитектурно analyzer — внешняя зависимость, не должен тянуть зависимости photo-service (fastapi, sqlalchemy, minio и т.д.);
- (б) ему нужны только `grpcio` (+ на этапе сборки `grpcio-tools`, `protobuf`);
- (в) образ маленький и независимый; замена на реальную нейронку в TASK-003 — просто другой образ по тому же `ANALYZER_GRPC_ADDR`.

**Файлы:**
- `analyzer-stub/server.py` — grpc.aio-сервер `PhotoAnalyzer`, слушает `:50051`. Импортирует **локальные плоские** стабы (в рабочей директории образа, `import analyzer_pb2` — работает, т.к. это не пакет, а скрипт+сиблинги на sys.path). Стабы генерируются В ОБРАЗЕ на этапе сборки из `protos/analyzer.proto` (build-time codegen — здесь безопасно: leaf-сервис, без локальных тестов). Это отличается от app (там стабы закоммичены, §7) — каждый вариант локально оптимален.
- `analyzer-stub/requirements.txt` — `grpcio`, `grpcio-tools`, `protobuf`.
- `analyzer-stub/Dockerfile` — `python:3.12-slim`; `pip install -r requirements.txt`; `COPY ../protos/analyzer.proto` (контекст сборки = `photo-service/`, см. compose); `RUN python -m grpc_tools.protoc -I <dir> --python_out=. --grpc_python_out=. analyzer.proto`; `COPY server.py`; `CMD python server.py`. Healthcheck: TCP-проба порта — `CMD python -c "import socket; socket.create_connection(('localhost',50051),2)"`.

**Детерминированная генерация результата** (от `object_key` через хэш, диапазоны из указаний оркестратора):
```python
h = hashlib.sha256(object_key.encode("utf-8")).digest()
faces_count      = h[0] % 6                                   # 0..5
blur_score       = (int.from_bytes(h[1:3], "big") % 1001) / 1000.0   # 0.000..1.000
is_blurred       = blur_score > 0.6
perceptual_hash  = h.hex()[:16]                               # 16 hex-символов
```
Одинаковый `object_key` → одинаковый результат (тесты воспроизводимы). Возвращает `AnalyzePhotoResponse(faces_count, is_blurred, blur_score, perceptual_hash)`.

---

## 7. gRPC-стабы и клиент в worker

### 7.1 Стабы — ЗАКОММИТИТЬ сгенерированные (обоснование выбора)
Выбор из двух вариантов оркестратора → **закоммитить сгенерированные файлы** в `app/grpc_gen/`. Причина (риск сборки/тестов):
- Тесты идут **локально через `uv run pytest`, НЕ в Docker**. При генерации-в-образе стабов нет на локальной машине → любой импорт worker/клиента в тестах падал бы `ModuleNotFoundError`. Закоммиченные стабы импортируются одинаково и локально, и в образе, без пред-шага кодогенерации.
- Критичные юнит-тесты (формула, классификация retry, идемпотентность, outbox) стабы НЕ импортируют → блоба-радиус закоммиченного кода минимален.
- `grpcio-tools` остаётся **dev-зависимостью** (нужен только для регенерации) → runtime-образ тонкий (`grpcio`+`protobuf`).

**Регенерация (команда в шапке `app/grpc_gen/__init__.py` / README), из `photo-service/`:**
```
uv run python -m grpc_tools.protoc -I protos \
  --python_out=app/grpc_gen --grpc_python_out=app/grpc_gen \
  protos/analyzer.proto
```
grpcio-tools эмитит в `analyzer_pb2_grpc.py` плоский `import analyzer_pb2 as analyzer__pb2` — заменить одной строкой на пакетный импорт:
```
from app.grpc_gen import analyzer_pb2 as analyzer__pb2
```
Это фикс известного quirk генератора (НЕ ручное написание protobuf-логики). Закоммитить `analyzer_pb2.py`, `analyzer_pb2_grpc.py`, `__init__.py`. Пакет попадёт в wheel автоматически (`packages=["app"]`), т.к. есть `__init__.py`.

### 7.2 Клиент `app/integrations/analyzer_client.py` (переписать)
```python
class AnalyzerGrpcClient:
    def __init__(self, addr: str, timeout: float): channel=grpc.aio.insecure_channel(addr); stub=PhotoAnalyzerStub(channel)
    async def analyze(self, photo_id, object_key) -> AnalyzePhotoResponse:
        return await self._stub.AnalyzePhoto(AnalyzePhotoRequest(photo_id=..., object_key=...), timeout=self._timeout)
    async def close(self): await self._channel.close()
```
- Канал создаётся один раз при старте worker, закрывается в shutdown.
- Таймаут вызова = `ANALYZER_GRPC_TIMEOUT` (деф. 30s, §3.2) — И как gRPC-`timeout=`, И продублировать `asyncio.timeout` в процессоре (двойная защита).
- `classify_grpc_error(exc)` (§5.4) живёт здесь же. `grpc.aio.AioRpcError.code()` → маппинг.
- Старый `AnalyzerClient(NotImplementedError)` удаляется/замещается; проверить тесты, ссылающиеся на него (§13).

---

## 8. Метрики

Общий модуль имён `app/integrations/metrics.py` (объявления Counter/Gauge/Histogram; импортируется и API, и worker — но процессы разные, реестры независимые).

### 8.1 API (порт 8000, эндпоинт `/metrics`)
| Метрика | Тип | Лейблы | Где инкремент |
|---------|-----|--------|---------------|
| `photos_uploaded_total` | Counter | — | после commit в `create_photo`; в `create_batch` — `.inc(len(files))` |
| `http_requests_total` | Counter | method, endpoint, status | metrics middleware (§8.3) |
| `http_request_duration_seconds` | Histogram | method, endpoint | metrics middleware; buckets `.005,.01,.025,.05,.1,.25,.5,1,2.5,5,10` |
| `storage_upload_errors_total` | Counter | — | при `StorageUnavailable` в save (create_photo/create_batch) |
| `kafka_publish_errors_total` | Counter | — | в outbox loop при ошибке `publish()` |
| `photos_pending` | Gauge | — | обновляется при скрейпе (§8.4) |

### 8.2 Worker (порт `WORKER_METRICS_PORT`=8001, `start_http_server`)
| Метрика | Тип | Лейблы | Где инкремент |
|---------|-----|--------|---------------|
| `photo_analysis_started_total` | Counter | — | после успешного claim (rowcount=1) |
| `photo_analysis_completed_total` | Counter | — | на `done` |
| `photo_analysis_failed_total` | Counter | reason (`no_retry`/`retries_exhausted`) | на `failed` |
| `photo_analysis_duration_seconds` | Histogram | — | observe(now−t0), claim→терминал; buckets `.05,.1,.25,.5,1,2.5,5,10,30` |
| `analyzer_grpc_errors_total` | Counter | code (gRPC status name) | на каждой неуспешной gRPC-попытке |
| `worker_messages_processed_total` | Counter | result (`done`/`failed`/`skipped`) | на каждое сообщение (терминально) |

### 8.3 Metrics middleware (`app/api/metrics_middleware.py`)
Лёгкий ASGI/`@app.middleware("http")`-слой: засечь время, вызвать app, записать `http_requests_total{method,endpoint,status}` и `http_request_duration_seconds{method,endpoint}`. **endpoint = шаблон маршрута** (`request.scope["route"].path`, напр. `/v1/photos/{photo_id}`), fallback `"unmatched"` — иначе кардинальность взорвётся от `photo_id` в пути. Contextvar не нужен → допускается `BaseHTTPMiddleware`/`@app.middleware`.

### 8.4 `photos_pending` — обновление при скрейпе
`/metrics` API реализовать как **async FastAPI-роут** (не `make_asgi_app`), который: (1) `SELECT count(*) FROM photos WHERE status='pending'` через async-engine под коротким `asyncio.timeout` (при ошибке — оставить прежнее значение gauge, лог WARN), `photos_pending.set(n)`; (2) вернуть `Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)`.
> **Обоснование выбора вместо «сырого» custom collector:** приложение асинхронное. Стандартный `Collector.collect()` синхронен и вызывался бы из потока экспозиции → пришлось бы либо тянуть sync-драйвер БД, либо делать кросс-loop вызовы (хрупко). Async-роут даёт свежий счёт «при скрейпе» (как и просил оркестратор), но без sync-драйвера и без гонок с event loop. Метрики RED и counters — из глобального `REGISTRY` через `generate_latest`.

---

## 9. Prometheus / Grafana

### 9.1 `prometheus.yml`
```yaml
global: { scrape_interval: 10s }
scrape_configs:
  - job_name: photo-api
    static_configs: [{ targets: ["api:8000"] }]
  - job_name: photo-worker
    static_configs: [{ targets: ["worker:8001"] }]
```

### 9.2 Grafana provisioning
- `grafana/provisioning/datasources/prometheus.yml` — datasource `Prometheus`, `url: http://prometheus:9090`, `isDefault: true`, `access: proxy`.
- `grafana/provisioning/dashboards/provider.yml` — provider, `path: /etc/grafana/provisioning/dashboards` (или отдельная папка dashboards).
- `grafana/dashboards/photo-service.json` — дашборд, 4 панели (минимум):
  1. **Uploads per minute:** `sum(rate(photos_uploaded_total[1m])) * 60`.
  2. **Analysis duration (p95):** `histogram_quantile(0.95, sum(rate(photo_analysis_duration_seconds_bucket[5m])) by (le))`.
  3. **Failed analyses:** `sum(rate(photo_analysis_failed_total[5m]))` (+ по reason).
  4. **Pending photos:** `photos_pending`.

Монтирование в compose: datasource+provider как volume в `/etc/grafana/provisioning/...`, дашборды — в путь провайдера.

---

## 10. Конфиг, compose, Dockerfile, deps

### 10.1 `app/core/config.py` (+поля Settings, дефолты под compose)
```
KAFKA_BOOTSTRAP_SERVERS: str = "kafka:9092"
KAFKA_TOPIC_ANALYSIS_REQUESTED: str = "photo.analysis.requested"
KAFKA_CONSUMER_GROUP: str = "photo-analysis-workers"
ANALYZER_GRPC_ADDR: str = "analyzer-stub:50051"
ANALYZER_GRPC_TIMEOUT: float = 30.0
WORKER_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_BASE_SECONDS: float = 1.0     # 1,2,4
WORKER_METRICS_PORT: int = 8001
OUTBOX_POLL_INTERVAL_SECONDS: float = 2.0
OUTBOX_BATCH_SIZE: int = 100
KAFKA_PUBLISH_TIMEOUT_SECONDS: float = 10.0  # §3.2
```
(`get_settings` остаётся `@lru_cache`; worker — отдельный процесс, читает свои env — рассинхрон из риска #9 контекста неактуален, т.к. env задаётся в compose явно.)

### 10.2 `.env.example` — добавить те же ключи.

### 10.3 `docker-compose.yml` — новые сервисы
- **kafka** (KRaft, один брокер): образ `apache/kafka:3.7.0` (или `confluentinc/cp-kafka:7.6.x`); env KRaft (`KAFKA_NODE_ID=1`, `KAFKA_PROCESS_ROLES=broker,controller`, listeners PLAINTEXT `kafka:9092` + CONTROLLER `kafka:9093`, `KAFKA_CONTROLLER_QUORUM_VOTERS=1@kafka:9093`, `KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR=1`, `KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR=1`, `KAFKA_TRANSACTION_STATE_LOG_MIN_ISR=1`, `KAFKA_AUTO_CREATE_TOPICS_ENABLE=true`, `KAFKA_NUM_PARTITIONS=1`); healthcheck `kafka-broker-api-versions.sh --bootstrap-server localhost:9092`; порт 9092.
- **analyzer-stub**: `build: { context: ., dockerfile: analyzer-stub/Dockerfile }`; порт 50051; healthcheck TCP-проба; без depends_on (leaf).
- **worker**: `build: .` (тот же образ); `command: uv run python -m app.worker.main`; env = DB/MinIO/Kafka/analyzer + `WORKER_METRICS_PORT`; `depends_on: postgres(healthy), kafka(healthy), analyzer-stub(started)`; порт 8001 (для скрейпа/дебага, expose достаточно). **Миграции worker НЕ запускает** (их гоняет api — избежать гонки двух alembic; worker стартует после api по желанию, но зависимость на api не обязательна, т.к. схема появится к моменту первой обработки; для надёжности можно `depends_on: api` без condition).
- **api**: добавить в env Kafka-переменные; `depends_on` дополнить `kafka: { condition: service_started }` (upload не зависит от Kafka, поэтому не healthy). Оставляет миграции: `command: alembic upgrade head && uvicorn ...` (теперь head = v002).
- **prometheus**: `prom/prometheus`, volume `./prometheus.yml`, порт 9090.
- **grafana**: `grafana/grafana`, volumes provisioning+dashboards, `GF_SECURITY_ADMIN_PASSWORD=admin`, порт 3000, `depends_on: prometheus`.
- volumes: +`kafkadata` (при желании персистентности).

### 10.4 Dockerfile
Без структурных изменений: worker/api — тот же образ. Стабы закоммичены в `app/grpc_gen/` → попадают через `COPY app/`. Новые runtime-deps ставятся `uv sync --no-dev`. `protos/` в образ api/worker копировать НЕ нужно (стабы уже в `app/`).

### 10.5 `pyproject.toml`
- **runtime `dependencies`:** `aiokafka>=0.10`, `grpcio>=1.60`, `protobuf>=4.25`, `prometheus-client>=0.20`.
- **dev `[dependency-groups].dev`:** `grpcio-tools>=1.60` (регенерация стабов).
> Проверить совместимость `aiokafka`/`grpcio` с python 3.12 (используемые версии её поддерживают). Риск: конфликт `protobuf`-версий между `grpcio-tools` и `grpcio` — брать одинаковый мажор.

---

## 11. Риски и краевые случаи

| # | Ситуация | Решение / статус |
|---|----------|------------------|
| 1 | Kafka упала — upload | publish не на HTTP-пути; строка `not_sent` в БД; фоновый publisher дошлёт. Upload → 202. (DoD-пункт покрыт.) |
| 2 | Kafka упала после публикации, до `mark_published` commit | Дубликат публикации при следующем poll → консьюмер идемпотентен (claim). At-least-once. Приемлемо. |
| 3 | **Worker умер после claim (`processing`), до терминала** | Offset не закоммичен → Kafka переотдаст сообщение, НО строка в `processing`, claim (`WHERE status='pending'`) вернёт 0 → **фото зависнет в `processing`**. **Известное ограничение** (предикат claim зафиксирован спекой, менять нельзя). Смягчение: ретраи in-process (offset коммитим только после терминала) → окно краха мало́ (только жёсткий крах процесса посреди сообщения). **Рекомендация на TASK-003:** reaper — периодически `processing` старше порога (по `created_at`/новой колонке `processing_started_at`) → сбросить в `pending`. Зафиксировать в 20_design как ограничение MVP. |
| 4 | Дубли сообщений (rebalance) | Атомарный claim `pending→processing`: 0 строк → skip. Терминальные `done/failed` не трогаются. |
| 5 | Батч частично failed | `GET /v1/batches/{id}` → `completed`, `best_photo_id` считается ТОЛЬКО по `done`-фото; все failed → `best_photo_id=null`. Формула зафиксирована. |
| 6 | Батч: MinIO упал на N-м файле после сохранения первых | Валидация ВСЕХ файлов до любой записи в MinIO (атомарный reject 400/413/415). При сбое MinIO во время сохранения — rollback всей транзакции (нет строк) + best-effort `delete_file` уже сохранённых объектов; 503. Возможен orphan-объект (как в TASK-001) — приемлемо. |
| 7 | Кардинальность метрик | endpoint-лейбл = шаблон маршрута, не сырой путь (§8.3). |
| 8 | Бесконечный retry / retry невалидного файла | Лимит `WORKER_MAX_ATTEMPTS=3`; NO_RETRY-класс (§5.4) сразу → failed. Неожиданные исключения → NO_RETRY (fail-safe). |
| 9 | Блокирующие вызовы в event loop | Kafka — `aiokafka` (async); gRPC — `grpc.aio` (async); БД — async-engine. Синхронный MinIO — уже через `anyio.to_thread` (TASK-001). |
| 10 | Циклические FK (photos↔batches) | Порядок DDL в §2 (batches → alter photos.batch_id). |
| 11 | Гонка вычисления best_photo при параллельных GET | Считаем на чтении (детерминированная формула → одинаковый результат при одинаковых данных); write-through в `batches.best_photo_id/status='completed'` идемпотентен (§12 B). |
| 12 | Рассинхрон модель↔миграция (урок TASK-000) | Модели и v002 править вместе; интеграционный тест миграции на реальном Postgres (§13). |
| 13 | trace_id отсутствует в старых строках | Колонка nullable; publisher подставит uuid4 при NULL. |

---

## 12. Шаги для кодера (упорядоченно; блоки A/B/C сдаются поэтапно)

> Общий принцип: модель + миграция v002 правятся ВМЕСТЕ (шаги A1–A2). Предикат атомарного claim и формула лучшего кадра — ЗАФИКСИРОВАНЫ, не переизобретать.

### Блок A — асинхронный конвейер анализа

1. **`pyproject.toml`** — runtime: `aiokafka>=0.10`, `grpcio>=1.60`, `protobuf>=4.25`, `prometheus-client>=0.20`; dev: `grpcio-tools>=1.60`. (Основа для всех.)
2. **`app/core/config.py`** — добавить поля Settings из §10.1. `.env.example` — те же ключи.
3. **`app/db/models.py`** — расширить `Photo` (attempts, last_error_code, last_error_message, publish_status['not_sent'] default, published_at, trace_id, batch_id FK); relationship `analysis` (uselist=False) и (опц.) `batch`. Новые модели `AnalysisResult` (§2.1), `Batch` (§2.2). CHECK на publish_status/batch.status — через `__table_args__`. (Зависит от 1.)
4. **`migrations/versions/v002_add_analysis_and_batches.py`** — новая ревизия `v002`, `down_revision="v001"`. DDL строго по §2 (порядок upgrade/downgrade, частичный индекс outbox, CHECK-констрейнты, БЕЗ новых PG-enum). НЕ трогать v001. (Зависит от 3, синхронно.)
5. **`app/grpc_gen/`** — сгенерировать стабы командой из §7.1, применить фикс импорта, закоммитить `analyzer_pb2.py`, `analyzer_pb2_grpc.py`, `__init__.py` (с командой регенерации в докстринге). (Зависит от 1.)
6. **`app/integrations/analyzer_client.py`** — переписать: `AnalyzerGrpcClient` (grpc.aio, §7.2) + `classify_grpc_error` (§5.4). Удалить старый `NotImplementedError`-стаб. (Зависит от 5.)
7. **`app/integrations/kafka_producer.py`** (новый) — `KafkaEventProducer` (AIOKafkaProducer, acks=all, idempotence; `start/stop/publish_analysis_requested`). (Зависит от 1,2.)
8. **`app/integrations/metrics.py`** (новый) — объявить все метрики API и worker (§8.1/§8.2). (Зависит от 1.)
9. **`app/repositories/photo_repository.py`** — добавить: `claim_for_processing`(atomic UPDATE, вернуть rowcount), `record_attempt`, `mark_done`, `mark_failed`, `fetch_unpublished(limit)`, `mark_published`; `get_by_id`/`list` подгружают `analysis` (selectinload, без N+1). SQL только здесь. (Зависит от 3.)
10. **`app/repositories/analysis_result_repository.py`** (новый) — `upsert(session, photo_id, resp)` (INSERT ... ON CONFLICT DO NOTHING). (Зависит от 3.)
11. **`app/services/analysis_processor.py`** (новый) — `AnalysisProcessor.process` (§5.3: claim → retry-цикл → терминальная запись; метрики). (Зависит от 6,8,9,10.)
12. **`app/services/outbox.py`** (новый) — `run_outbox_publisher` (§4.2). (Зависит от 7,8,9.)
13. **`app/services/photo_service.py`** — в `create_photo`: проставить `publish_status='not_sent'`, `trace_id=trace_id_var.get()`; `photos_uploaded_total.inc()` после commit; `storage_upload_errors_total.inc()` при StorageUnavailable. `get_photo`/`list_photos` → маппить `analysis` в `PhotoResponse`. (Зависит от 3,8,9, схем.)
14. **`app/schemas/photos.py`** — `AnalysisResultResponse`; `PhotoResponse.analysis` (default None). (Зависит от 3.)
15. **`app/worker/`** (новый пакет) — `main.py` (entrypoint `python -m app.worker.main`, §5.1: логи, start_http_server(8001), grpc-клиент, AIOKafkaConsumer, consume_loop, SIGTERM/SIGINT graceful), `consumer.py` (цикл §5.2), `__init__.py`. (Зависит от 6,8,11.)
16. **`app/main.py`** — lifespan: старт producer + outbox-задача; корректный shutdown (§4.3). (Зависит от 7,12.)
17. **`analyzer-stub/`** (новый) — `server.py` (grpc.aio, детерминированная генерация §6), `requirements.txt`, `Dockerfile` (build-time codegen, TCP healthcheck). (Зависит от `protos/analyzer.proto` — без изменений.)
18. **`docker-compose.yml`** — добавить kafka, analyzer-stub, worker (тот же образ, command worker); api — env Kafka + depends_on kafka(started). (Зависит от 16,17.)

### Блок B — батчи и лучший кадр

19. **`app/core/errors.py`** — `BatchSizeError` (400 `INVALID_BATCH_SIZE`). (Независим.)
20. **`app/repositories/batch_repository.py`** (новый) — `create`, `get_by_id`, `list_photos(batch_id)` (photos+analysis батча), `mark_completed(batch_id, best_photo_id)`. (Зависит от 3.)
21. **`app/services/photo_service.py::create_batch`** — валидация count 2..10 (иначе BatchSizeError) и каждого файла (тот же `_validate`) ДО любой записи; создать Batch + Photo[] (publish_status='not_sent', batch_id, trace_id); сохранить объекты; один commit; best-effort cleanup при сбое; `photos_uploaded_total.inc(n)`. Вернуть `BatchAcceptedResponse`. (Зависит от 20, схем.)
22. **`app/services/batch_service.py`** (новый) — `get_batch`: собрать фото+analysis; статус `completed` если ВСЕ терминальны; `select_best_photo(rows)` — **чистая функция** по формуле (is_blurred asc → blur_score asc → faces_count desc → created_at asc; только `done`; все failed → None); write-through `mark_completed` при первом наблюдении завершённости. (Зависит от 20.)
23. **`app/schemas/photos.py`** — `BatchPhotoItem`, `BatchAcceptedResponse`, `BatchPhotoDetail`, `BatchResponse` (§3.3). (Зависит от 14.)
24. **`app/api/batches.py`** (новый) — роутер prefix `/v1`: `POST /v1/photos/batch` (202) → `create_batch`; `GET /v1/batches/{batch_id}` (200/404) → `get_batch`. Не импортирует models/select/MinIO-SDK. (Зависит от 21,22,23.)
25. **`app/main.py`** — `app.include_router(batches.router)`. (Зависит от 24.)

### Блок C — наблюдаемость

26. **`app/api/metrics_middleware.py`** (новый) — RED-метрики HTTP (§8.3), endpoint=шаблон маршрута. (Зависит от 8.)
27. **`app/main.py`** — подключить metrics middleware; добавить async-роут `GET /metrics` (§8.4, обновляет `photos_pending`, отдаёт `generate_latest`). (Зависит от 8,26.)
28. **`prometheus.yml`** (новый) — §9.1. **`grafana/provisioning/datasources/prometheus.yml`**, **`grafana/provisioning/dashboards/provider.yml`**, **`grafana/dashboards/photo-service.json`** (4 панели, §9.2). (Независимы.)
29. **`docker-compose.yml`** — добавить prometheus, grafana (volumes, порты). (Зависит от 28.)
30. **worker метрики** — убедиться, что `start_http_server(WORKER_METRICS_PORT)` поднят в `app/worker/main.py` и метрики §8.2 инкрементятся в процессоре (шаг 11). (Зависит от 8,11,15.)

### Как проверить (после реализации)
```bash
docker compose -f photo-service/docker-compose.yml down -v
docker compose -f photo-service/docker-compose.yml up --build     # alembic head=v002 без DuplicateObject
# single
curl -si -F "file=@sample.jpg" http://localhost:8000/v1/photos     # 202 {photo_id,status:pending}
sleep 5; curl -s http://localhost:8000/v1/photos/<id>              # status:done, analysis:{...}
# batch
curl -si -F "file=@a.jpg" -F "file=@b.jpg" http://localhost:8000/v1/photos/batch   # 202 {batch_id,photos:[...]}
curl -s http://localhost:8000/v1/batches/<batch_id>               # completed, best_photo_id
# метрики/графики
curl -s http://localhost:8000/metrics | grep photos_pending
curl -s http://localhost:8001/metrics | grep photo_analysis
# Grafana http://localhost:3000 (admin/admin) → дашборд 4 графика
# outbox: остановить kafka → upload всё равно 202; поднять kafka → фото дообработается
cd photo-service && uv run pytest -q
```

---

## 13. Что сломается в существующих тестах + критичные новые юнит-тесты

### 13.1 Сломается / требует правки (test-writer / фикс кодером)
- **`tests/test_migration_integration.py`** — ассерт `column_names == {6 колонок}` (L143–151) СЛОМАЕТСЯ: `upgrade head` теперь идёт до v002 → у `photos` появляются 7 новых колонок + таблицы `analysis_results`,`batches`. Обновить: ожидать расширенный набор колонок, добавить проверки новых таблиц/индексов/CHECK и **downgrade v002→v001** (набор колонок как в v001) + downgrade base.
- **`tests/test_photos_endpoints.py`** — тесты, сравнивающие тело `GET /v1/photos/{id}` и элементы списка по полному равенству, СЛОМАЮТСЯ из-за нового ключа `analysis`. Добавить `analysis: None` в ожидания (или сравнивать по подмножеству).
- **`tests/test_photo_service.py`** — если моки `repository.get_by_id` возвращают `Photo` без атрибута `analysis`, а сервис читает `photo.analysis` → поправить моки (проставить `analysis=None`). Проверить сигнатуры `get_photo/list_photos`.
- **`tests/test_stubs.py`** — если где-то ассертится `NotImplementedError` у `AnalyzerClient.analyze_photo` (переписывается на реальный клиент) — обновить/удалить. Класс/имена интеграции меняются (`AnalyzerGrpcClient`).
- **`app/integrations/analyzer_client.py`** докстринги/ссылки — привести в соответствие (Kafka + gRPC теперь реальны).

### 13.2 Критичные новые юнит-тесты (обязательны по DoD)
- **Формула лучшего кадра** (`select_best_photo`): порядок тай-брейков (is_blurred → blur_score → faces_count → created_at); только `done` учитываются; все failed → None; смешанный батч; один done.
- **Классификация retry/no-retry** (`classify_grpc_error`): каждый gRPC-код из таблицы §5.4 → верный класс; TimeoutError → RETRY; неизвестное → NO_RETRY.
- **Идемпотентность worker**: `claim_for_processing` возвращает 0 при status≠pending → процессор делает skip, НЕ вызывает gRPC, НЕ пишет результат; при 1 — полный путь.
- **Outbox-переходы**: create → `not_sent`; publisher success → `sent`+`published_at`; publish падает → остаётся `not_sent`, `kafka_publish_errors_total` инкрементнут.
- **Батч-валидация**: <2 или >10 → 400 `INVALID_BATCH_SIZE`; невалидный файл (пустой/большой/не-изображение) в батче → 413/415/400 и НИ ОДНОЙ строки/объекта (атомарность).
- **Детерминизм analyzer-stub**: один `object_key` → один результат; диапазоны faces_count∈[0,5], blur_score∈[0,1], is_blurred=(blur_score>0.6), perceptual_hash — 16 hex.
- **Ретрай-цикл процессора**: RETRY-ошибка N раз → `attempts` растёт, после лимита → `failed`+`last_error_*`; NO_RETRY → `failed` сразу без повторов; success со 2-й попытки → `done`.

Покрытие ≥85% (гейт). Интеграционный тест миграции v002 — на эфемерном Postgres (testcontainers), как в §13.1.

---

## Приложение: сводка контрактов (для кодера)

**Kafka value JSON:** `{photo_id, object_key, created_at, trace_id}`; key=photo_id.
**gRPC:** `AnalyzePhoto(photo_id, object_key) → {faces_count, is_blurred, blur_score, perceptual_hash}` (proto без изменений).
**Формула best (ЗАФИКСИРОВАНО):** сортировка done-фото `is_blurred ASC, blur_score ASC, faces_count DESC, created_at ASC` → первый; нет done → null.
**Атомарный claim (ЗАФИКСИРОВАНО):** `UPDATE photos SET status='processing' WHERE photo_id=:id AND status='pending'`; rowcount 1=взяли, 0=skip.
**Ретраи:** только transient (gRPC UNAVAILABLE/DEADLINE_EXCEEDED/RESOURCE_EXHAUSTED/ABORTED/INTERNAL, TimeoutError), backoff 1/2/4s, лимит 3; иначе/после лимита → failed.

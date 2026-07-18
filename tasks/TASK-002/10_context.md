---
task_id: TASK-002
agent: context-collector
model: haiku
status: done
inputs: [specs/constitution.md, specs/feature-upload/tasks.md]
outputs: [tasks/TASK-002/10_context.md]
timestamp: 2026-07-17T00:00:00Z
---

# Контекст кода для TASK-002: Асинхронный конвейер анализа + батчи + наблюдаемость

## 1. Релевантные файлы (текущее состояние после TASK-001)

### API слой (`app/api/`)

| Файл | Назначение | Ключевой код |
|------|-----------|----------|
| `app/api/photos.py` L1–90 | HTTP роутеры для `/v1/photos*` | Роутер с префиксом `/v1/photos` (L24); зависимости `get_photo_service`, `get_session` (L27–29); POST/GET эндпоинты (L52–89) |
| `app/api/middleware.py` L1–52 | Middleware для трассировки | RequestIdMiddleware (pure ASGI), устанавливает `trace_id_var` из заголовка или uuid4 (L37–39); пробрасывает в ответ (L44) |

### Сервисный слой (`app/services/`)

| Файл | Назначение | Ключевой код |
|------|-----------|----------|
| `app/services/photo_service.py` L1–157 | Бизнес-логика фотографий | `PhotoService` класс; метод `create_photo` (L71–118): validate → DB flush → MinIO save → commit, rollback на ошибке хранилища; методы `get_photo`, `list_photos`, `get_photo_content` (L120–156) |

### Слой доступа к данным (`app/repositories/`, `app/db/`)

| Файл | Назначение | Ключевой код |
|------|-----------|----------|
| `app/repositories/photo_repository.py` L1–56 | SQL-слой для Photo | `PhotoRepository` класс; методы `create` (L21–32), `get_by_id` (L34–37), `list` (L39–44), `update_status` (L46–55) |
| `app/db/models.py` L1–54 | SQLAlchemy модели | `Photo` класс (L35–54) с полями: photo_id (PK), filename, object_key (UNIQUE, NOT NULL), user_id (nullable), created_at, status (enum); `PhotoStatus` enum (L28–32): pending/processing/done/failed |
| `app/db/session.py` L1–33 | Конфиг БД и сессии | engine (L16–21) с pool_size=10, max_overflow=5; SessionLocal (L23); `get_session()` (L26–32) — FastAPI зависимость, не управляет commit/rollback |

### Интеграции (`app/integrations/`)

| Файл | Назначение | Ключевой код |
|------|-----------|----------|
| `app/integrations/storage.py` L1–116 | MinIO обёртка | `ObjectStorage` класс; методы: `ensure_bucket()` (L54–64), `save_file()` (L66–87), `get_file()` (L89–102), `delete_file()` (L104–116) — все синхронны, обёртываются в `anyio.to_thread` при async вызове |
| `app/integrations/analyzer_client.py` L1–38 | Заглушка анализатора (TODO TASK-002) | `AnalyzerClient` класс; метод `analyze_photo()` (L32–37) — пока `NotImplementedError`, в TASK-002 нужен Kafka producer + gRPC клиент |

### Конфиг и инфра (`app/core/`)

| Файл | Назначение | Ключевой код |
|------|-----------|----------|
| `app/core/config.py` L1–41 | Конфиг приложения | `Settings` класс (L15–34) с полями: APP_NAME, API_HOST (0.0.0.0), API_PORT (8000), LOG_LEVEL, DATABASE_URL, MINIO_* (endpoint, keys, bucket, secure) |
| `app/core/logging.py` L1–65 | Структурированное JSON-логирование | `trace_id_var` ContextVar (L27); `TraceIdFilter` (L30–38) — injector trace_id в каждый лог-запись; `setup_logging()` (L41–65) конфигурирует handler с JsonFormatter |
| `app/core/errors.py` L1–122 | Иерархия ошибок и обработчики | `AppError` базовый класс (L21–33) с error_code, http_status; подклассы `StorageUnavailable` (503), `NotFoundError` (404), `ValidationError` (400), `ConflictError` (409), `PayloadTooLargeError` (413), `UnsupportedMediaTypeError` (415); `register_exception_handlers()` (L83–121) регистрирует глобальный обработчик |

### Основная точка входа

| Файл | Назначение | Ключевой код |
|------|-----------|----------|
| `app/main.py` L1–94 | Сборка и конфигурация приложения | `lifespan()` (L30–49): инициализирует ObjectStorage в `app.state.storage`, проверяет БД; `app` (L52); middleware `RequestIdMiddleware` (L54); роутер `photos` (L56); `/healthz` (L59–62), `/readyz` (L65–93) |

### Миграции и контракты

| Файл | Назначение | Ключевой код |
|------|-----------|----------|
| `migrations/versions/v001_init_photos.py` L1–66 | Инициальная миграция | Revision ID: v001; создаёт таблицу photos и enum photo_status (L30–32 с `create_type=False` — критический фиксит из TASK-000 для избежания DuplicateObjectError) |
| `migrations/env.py` L1–76 | Alembic конфиг (async) | `run_async_migrations()` (L53–64); читает DATABASE_URL из Settings (L29) |
| `protos/analyzer.proto` L1–30 | gRPC контракт анализатора | Service `PhotoAnalyzer` (L15) с методом `AnalyzePhoto()` (L16); Request (L19–21): photo_id, object_key; Response (L24–28): faces_count, is_blurred, blur_score, perceptual_hash |

### Инфраструктура

| Файл | Назначение | Ключевой код |
|------|-----------|----------|
| `docker-compose.yml` L1–62 | Локальная разработка | Сервисы: postgres (L2–16), minio (L18–33), api (L35–57); миграция запускается в api: `uv run alembic upgrade head` (L57) |
| `Dockerfile` L1–31 | Образ приложения | Двухслойный (зависимости + код); уже скопированы pyproject.toml, app/, alembic.ini, migrations/ |
| `pyproject.toml` L1–47 | Зависимости проекта | dependencies: fastapi, uvicorn, pydantic, sqlalchemy[asyncio], asyncpg, alembic, minio, python-json-logger, anyio, python-multipart; dev: pytest, pytest-asyncio, pytest-cov, httpx, ruff, testcontainers[postgres] |
| `.env.example` L1–14 | Шаблон переменных окружения | Примеры для LOG_LEVEL, DATABASE_URL, MINIO_* |

### Схемы (`app/schemas/`)

| Файл | Назначение | Ключевой код |
|------|-----------|----------|
| `app/schemas/photos.py` L1–36 | Pydantic DTOs | `HealthResponse` (L8–12), `ErrorResponse` (L15–20), `UploadPhotoResponse` (L23–27) с photo_id и status="pending", `PhotoResponse` (L30–35) с id, filename, status (enum) |

### Тесты (`tests/`)

| Файл | Назначение | Ключевой код |
|------|-----------|----------|
| `tests/conftest.py` L1–19 | Общие fixture | `client` fixture (L15–19): ASGITransport + AsyncClient для прямого вызова приложения без real БД/MinIO |
| `tests/test_photo_service.py` L1–488 | Юнит-тесты PhotoService | Полное покрытие: валидация (TestValidate), счастливый путь (TestCreatePhotoHappyPath), ошибки (TestCreatePhotoValidationErrorsPropagate), порядок операций/rollback (TestCreatePhotoOrderAndRollback L196–291), get/list (TestGetPhoto, TestListPhotos), содержимое (TestGetPhotoContent), логирование (TestObservability) |
| `tests/test_migration_integration.py` L1–165 | Интеграционный тест миграций | Запускает реальный Postgres (testcontainers), выполняет `alembic upgrade head` и `downgrade base`, проверяет DDL (таблица, enum, колонки) |
| `tests/test_photos_endpoints.py` L1–100+ | HTTP контрактные тесты | Мокирует зависимости, проверяет HTTP статусы, тела ответов, unified error format для каждого error_code |

---

## 2. Точки интеграции для TASK-002

### 2.1 Публикация события в Kafka (Producer)

**Где ляжет:** `app/services/photo_service.py::PhotoService.create_photo()` L71–118.

**Текущее состояние:**
- L93: `await self._repository.create(session, photo)` — DB flush
- L98–109: `anyio.to_thread.run_sync(self._storage.save_file(...))` — MinIO save с timeout
- L115: `await session.commit()` — commit

**Что нужно добавить:**
- После commit (L115) нужна публикация события в Kafka топик `photo.analysis.requested` с payload `{photo_id, object_key, created_at, trace_id}`.
- **Упрощённый outbox:** если Kafka недоступна, событие должно быть сохранено в БД с статусом `not_sent`, и фоновый publisher должен дошлать (см. п. 2.3).

**Риск/смежные места:**
- Текущий `session.commit()` — граница транзакции; нужно добавить outbox статус в БД перед commit или обновить после.
- `trace_id_var.get()` (из logging) должен быть передан в Kafka-сообщение.

### 2.2 Worker (Consumer + gRPC + Result Update)

**Где ляжет:** новый модуль `app/worker/` (отдельный entrypoint в Dockerfile или в том же образе с разным CMD).

**Что нужно реализовать:**
- Consumer от Kafka топика `photo.analysis.requested`, consumer group (имя, напр., `photo-analysis-workers`).
- Цикл обработки для каждого сообщения:
  1. Прочитать сообщение: photo_id, object_key, trace_id.
  2. **Атомарный захват:** `UPDATE photos SET status='processing' WHERE photo_id=:id AND status='pending'` → 1 строка = успех, 0 = дубль/уже обработан (идемпотентность).
  3. gRPC-вызов analyzer-stub: `AnalyzePhoto(photo_id, object_key)`.
  4. На успех: сохранить результат в `analysis_results` таблицу + обновить `photos.status = 'done'`.
  5. На ошибку: классификация (retry-able: timeout/network vs no-retry: validation/not image) → обновить attempts/last_error_*, при лимите 3 попыток → status='failed'.
  6. Commit offset в Kafka ТОЛЬКО после успешной обработки (at-least-once + идемпотентность).

**Интеграции:**
- `trace_id_var.set()` (из logging) должен быть установлен из trace_id из сообщения для сквозной трассировки.
- gRPC клиент: новый в `app/integrations/` или встроено в worker.
- `anyio.to_thread` для синхронных вызовов DB (если нужно).

### 2.3 Таблицы БД (миграция v002)

**Новые таблицы:**

#### `analysis_results`
```sql
CREATE TABLE analysis_results (
  photo_id UUID NOT NULL REFERENCES photos(photo_id) UNIQUE,
  faces_count INT,
  is_blurred BOOLEAN,
  blur_score DOUBLE PRECISION,
  perceptual_hash VARCHAR(255),
  created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
  PRIMARY KEY (photo_id)
);
```

#### `batches`
```sql
CREATE TABLE batches (
  batch_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  status VARCHAR(50) NOT NULL DEFAULT 'processing',  -- processing, completed
  best_photo_id UUID REFERENCES photos(photo_id) NULLABLE,
  created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
  INDEX ix_batches_created_at (created_at)
);
```

**Изменения в `photos`:**
- `attempts INT DEFAULT 0` — счётчик попыток обработки
- `last_error_code VARCHAR(255) NULLABLE` — код ошибки при последней попытке
- `last_error_message TEXT NULLABLE` — сообщение об ошибке
- `batch_id UUID REFERENCES batches(batch_id) NULLABLE` — привязка к батчу
- `outbox_status VARCHAR(50) DEFAULT 'not_sent'` — статус публикации в Kafka (not_sent, sent)

**Миграция:**
- Файл: `migrations/versions/v002_add_analysis_and_batches.py`
- Revision ID: v002, down_revision: v001
- Создание таблиц, ALTER TABLE photos для новых колонок
- Индексы для удобства запросов

**Критично:** проверить на реальном Postgres (testcontainers тест в `test_migration_integration.py`).

### 2.4 Модели и схемы (расширение)

**Файл `app/db/models.py`:** добавить классы
- `AnalysisResults` (новая таблица)
- `Batch` (новая таблица)
- Расширить `Photo` полями: attempts, last_error_code, last_error_message, batch_id, outbox_status

**Файл `app/schemas/photos.py`:** добавить DTOs
- `AnalysisResultsResponse` с faces_count, is_blurred, blur_score, perceptual_hash
- Расширить `PhotoResponse` полем `analysis: AnalysisResultsResponse | None` (null пока не done)
- `BatchPhotoResponse` — для фото в батче
- `BatchResponse` для GET /v1/batches/{batch_id} с batch_id, status, photos: list[BatchPhotoResponse], best_photo_id

### 2.5 Репозитории (новые методы)

**`app/repositories/photo_repository.py`:**
- `update_status_atomic(session, photo_id, from_status, to_status)` → возвращает кол-во обновленных строк (0 или 1) для идемпотентной проверки.
- `update_with_analysis_results(session, photo_id, results_dict)` → обновить analysis_results + status='done'.
- `update_with_error(session, photo_id, error_code, error_message, attempts)` → обновить attempts/last_error_* и решить (retry или failed).

**Новые репозитории:**
- `AnalysisResultRepository` для CRUD analysis_results.
- `BatchRepository` для CRUD batches и вычисления best_photo_id по формуле.

### 2.6 Конфигурация (расширение)

**Файл `app/core/config.py`:** добавить в Settings класс:

```python
# Kafka
KAFKA_BOOTSTRAP_SERVERS: str = "kafka:9092"  # или составная строка "host1:port1,host2:port2"
KAFKA_TOPIC_ANALYSIS_REQUESTED: str = "photo.analysis.requested"
KAFKA_CONSUMER_GROUP: str = "photo-analysis-workers"

# Analyzer gRPC
ANALYZER_GRPC_ADDR: str = "analyzer-stub:50051"
ANALYZER_GRPC_TIMEOUT: int = 30

# Metrics
PROMETHEUS_PORT: int = 8001  # отдельный порт для /metrics в worker
```

Дефолты настроены для docker-compose (hostname kafka:9092, analyzer-stub:50051).

### 2.7 HTTP эндпоинты (новые/расширение)

**Расширить `app/api/photos.py`:**

1. **POST /v1/photos/batch** (новый)
   - Multipart с несколькими файлами (2–10).
   - Валидация каждого (magic bytes, размер).
   - Невалидный файл → атомарно отклоняет весь батч (400/413/415).
   - На успех → создать batch + фото + опубликовать события → 202 `{batch_id, photos: [{photo_id, status}]}`

2. **GET /v1/batches/{batch_id}** (новый)
   - Возвращает `{batch_id, status: processing|completed, photos: [{photo_id, filename, status, analysis}], best_photo_id}`
   - 404 если батча нет.
   - best_photo_id вычисляется по формуле (только когда completed).

3. **GET /v1/photos/{photo_id}** (расширение)
   - Текущий response: PhotoResponse с id, filename, status.
   - Новое: поле `analysis` (null или AnalysisResultsResponse) — появляется только когда status=done.

### 2.8 Prometheus метрики

**В API (`app/api/photos.py` или новый модуль `app/integrations/metrics.py`):**

```python
# Счётчики (Counter)
photos_uploaded_total = Counter("photos_uploaded_total", "Total photos uploaded")
http_requests_total = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["method", "endpoint", "status"]
)
storage_upload_errors_total = Counter(
    "storage_upload_errors_total",
    "Total storage upload errors"
)
kafka_publish_errors_total = Counter(
    "kafka_publish_errors_total",
    "Total Kafka publish errors"
)

# Gauge
photos_pending = Gauge("photos_pending", "Number of photos in pending status")

# Гистограмма (Histogram)
http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration",
    ["method", "endpoint"]
)
```

**Эндпоинт `/metrics`:** регистрируется через prometheus_client или стандартный обработчик.

**В worker (`app/worker/worker.py` или аналог):**

```python
photo_analysis_started_total = Counter(...)
photo_analysis_completed_total = Counter(...)
photo_analysis_failed_total = Counter(...)
photo_analysis_duration_seconds = Histogram(...)
analyzer_grpc_errors_total = Counter(...)
worker_messages_processed_total = Counter(...)
```

**Эндпоинт `/metrics`:** на отдельном порту (PROMETHEUS_PORT, дефолт 8001).

### 2.9 gRPC анализатор (заглушка)

**Новый сервис: `analyzer-stub` в docker-compose.**

Минимальный gRPC-сервер (в Python или другом языке), реализующий `protos/analyzer.proto::PhotoAnalyzer`:
- Слушает на `:50051`.
- Метод `AnalyzePhoto(photo_id, object_key)`:
  - Возвращает детерминированные результаты (по хэшу object_key).
  - Примеры: faces_count = hash(object_key) % 10, is_blurred = hash % 2 == 0, blur_score = (hash % 100) / 100.0, perceptual_hash = hex(hash).

**В образе:** опционально в том же образе (отдельный entrypoint) или отдельный образ в compose.

### 2.10 Outbox фоновая задача (упрощённая)

**Где ляжет:** в `app/worker/` или `app/services/`.

**Что делает:**
- Периодическая задача (напр., каждые 5 сек): `SELECT * FROM photos WHERE outbox_status = 'not_sent' LIMIT 10`.
- Для каждого фото: попытка опубликовать событие в Kafka.
- На успех: обновить `outbox_status = 'sent'`.
- На ошибку: логировать, оставить в 'not_sent' для следующей итерации.

**Гарантирует:** upload не падает при лежащей Kafka; фото дообработаются после её старта.

---

## 3. Интерфейсы/контракты

### HTTP API (расширение текущего)

**Новые/расширенные эндпоинты:**

| Метод | Путь | Статус | Запрос | Ответ | Примечание |
|-------|------|--------|--------|-------|-----------|
| POST | `/v1/photos` | 202 | multipart file | `{photo_id, status="pending"}` | Текущий; на успех публикует в Kafka |
| GET | `/v1/photos` | 200 | limit, offset | `[{id, filename, status, analysis?}]` | Расширен поле analysis |
| GET | `/v1/photos/{id}` | 200 | — | `{id, filename, status, analysis?}` | Расширен поле analysis |
| GET | `/v1/photos/{id}/content` | 200 | — | bytes | Без изменений |
| POST | `/v1/photos/batch` | 202 | multipart (2–10 files) | `{batch_id, photos: [{photo_id, status}]}` | Новый |
| GET | `/v1/batches/{batch_id}` | 200 | — | `{batch_id, status, photos: […], best_photo_id?}` | Новый |
| GET | `/metrics` | 200 | — | Prometheus text format | Новый; в обоих (API и worker) |
| GET | `/healthz` | 200 | — | `{status, service}` | Без изменений |
| GET | `/readyz` | 200/503 | — | `{status: ready/not_ready}` | Может проверять Kafka доступность |

### gRPC (analyzer.proto)

```protobuf
service PhotoAnalyzer {
  rpc AnalyzePhoto(AnalyzePhotoRequest) returns (AnalyzePhotoResponse);
}

message AnalyzePhotoRequest {
  string photo_id = 1;
  string object_key = 2;
}

message AnalyzePhotoResponse {
  int32 faces_count = 1;
  bool is_blurred = 2;
  double blur_score = 3;
  string perceptual_hash = 4;
}
```

**Вызывается из:** worker при обработке сообщения из Kafka.

---

## 4. Kafka топики и схемы

### Топик: `photo.analysis.requested`

**Назначение:** разместить задачи анализа из API в очередь для worker.

**Конфиг (KRaft, один брокер, упрощённо):**
- Partitions: 1 (упрощение для MVP)
- Replication factor: 1
- Retention: 7 дней
- Cleanup policy: delete

**Ключ сообщения:** `photo_id` (UUID строка) — партиционирование по фото.

**Значение (JSON):**
```json
{
  "photo_id": "123e4567-e89b-12d3-a456-426614174000",
  "object_key": "photos/123e4567-e89b-12d3-a456-426614174000/original.jpg",
  "created_at": "2026-07-17T10:30:00Z",
  "trace_id": "req-uuid-from-middleware"
}
```

**Producer:** в `app/services/photo_service.py` после commit (или в отдельном integrations/kafka_producer.py).

**Consumer:** worker (в `app/worker/`).

---

## 5. БД/миграции

### Текущее состояние (после v001)

**Таблица `photos`:**
- photo_id (PK, UUID)
- filename (VARCHAR 512, NOT NULL)
- object_key (VARCHAR 1024, UNIQUE, NOT NULL)
- user_id (VARCHAR 255, nullable)
- created_at (TIMESTAMP WITH TZ, DEFAULT NOW())
- status (ENUM photo_status: pending/processing/done/failed, DEFAULT pending)
- Index: ix_photos_created_at

**Enum `photo_status`:** pending, processing, done, failed

### Миграция v002 (TASK-002)

**Файл:** `migrations/versions/v002_add_analysis_and_batches.py`

**Действия:**
1. Создать таблицу `analysis_results`:
   ```sql
   CREATE TABLE analysis_results (
     photo_id UUID NOT NULL REFERENCES photos(photo_id) UNIQUE,
     faces_count INT,
     is_blurred BOOLEAN,
     blur_score DOUBLE PRECISION,
     perceptual_hash VARCHAR(255),
     created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
     PRIMARY KEY (photo_id)
   );
   ```

2. Создать таблицу `batches`:
   ```sql
   CREATE TABLE batches (
     batch_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
     status VARCHAR(50) NOT NULL DEFAULT 'processing',
     best_photo_id UUID REFERENCES photos(photo_id),
     created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL
   );
   CREATE INDEX ix_batches_created_at ON batches(created_at);
   ```

3. ALTER TABLE photos (добавить колонки):
   - `attempts INT DEFAULT 0`
   - `last_error_code VARCHAR(255)`
   - `last_error_message TEXT`
   - `batch_id UUID REFERENCES batches(batch_id)`
   - `outbox_status VARCHAR(50) DEFAULT 'not_sent'`

**Ревизия:** v002, down_revision: v001

**Критично:** 
- Тестировать `alembic upgrade head` и `downgrade v001` на реальном Postgres (testcontainers).
- Добавить проверку в `tests/test_migration_integration.py`.

---

## 6. Зависимости и переменные окружения

### Новые зависимости (`pyproject.toml`)

```toml
dependencies = [
    # ... существующие ...
    "aiokafka>=0.8.1",          # async Kafka producer/consumer
    "grpcio>=1.60",             # gRPC runtime
    "grpcio-tools>=1.60",       # для генерации Python стабов из .proto
    "protobuf>=4.24",           # Protocol Buffers runtime
    "prometheus-client>=0.19",  # Prometheus метрики
]
```

### Новые переменные окружения (`.env.example`)

```env
# Kafka
KAFKA_BOOTSTRAP_SERVERS=kafka:9092
KAFKA_TOPIC_ANALYSIS_REQUESTED=photo.analysis.requested
KAFKA_CONSUMER_GROUP=photo-analysis-workers

# Analyzer (gRPC)
ANALYZER_GRPC_ADDR=analyzer-stub:50051
ANALYZER_GRPC_TIMEOUT=30

# Prometheus metrics
PROMETHEUS_PORT=8001
```

### docker-compose.yml — новые сервисы

```yaml
kafka:
  image: confluentinc/cp-kafka:7.6.0  # или apache/kafka:latest с KRaft
  environment:
    KAFKA_NODE_ID: 1
    KRAFT_MODE: "true"
    KAFKA_PROCESS_ROLES: "broker,controller"
    KAFKA_LISTENER_SECURITY_PROTOCOL_MAP: "CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT"
    KAFKA_LISTENERS: "PLAINTEXT://kafka:9092,CONTROLLER://kafka:9093"
    KAFKA_ADVERTISED_LISTENERS: "PLAINTEXT://kafka:9092"
    KAFKA_CONTROLLER_QUORUM_VOTERS: "1@kafka:9093"
    # ... другие параметры KRaft ...
  ports:
    - "9092:9092"
  healthcheck:
    test: ["CMD", "kafka-broker-api-versions.sh", "--bootstrap-server", "localhost:9092"]
    interval: 5s
    timeout: 10s
    retries: 10

analyzer-stub:
  build:
    context: .
    dockerfile: analyzer.Dockerfile  # или embedding в основной образ
  environment:
    ANALYZER_GRPC_PORT: 50051
  ports:
    - "50051:50051"
  depends_on:
    kafka:
      condition: service_healthy

worker:
  build: .
  environment:
    # ... всё из api ...
    KAFKA_BOOTSTRAP_SERVERS: kafka:9092
    ANALYZER_GRPC_ADDR: analyzer-stub:50051
  depends_on:
    kafka:
      condition: service_healthy
    analyzer-stub:
      condition: service_started
  command: sh -c "uv run python -m app.worker"  # или свой entrypoint

prometheus:
  image: prom/prometheus:latest
  volumes:
    - ./prometheus.yml:/etc/prometheus/prometheus.yml
  ports:
    - "9090:9090"
  command:
    - "--config.file=/etc/prometheus/prometheus.yml"

grafana:
  image: grafana/grafana:latest
  environment:
    GF_SECURITY_ADMIN_PASSWORD: admin
  volumes:
    - ./grafana/dashboards:/etc/grafana/provisioning/dashboards
    - ./grafana/datasources:/etc/grafana/provisioning/datasources
  ports:
    - "3000:3000"
  depends_on:
    - prometheus
```

---

## 7. Где ляжет изменение

### Слой сервисов (`app/services/`)

1. **`photo_service.py::PhotoService.create_photo()`** (L71–118):
   - После `await session.commit()` (L115) → publish событие в Kafka или сохранить в outbox.
   - Вернуть ту же `UploadPhotoResponse` (202 поведение не меняется).

2. **Новый класс `KafkaProducerService`** или метод в `photo_service.py`:
   - Инициализация `AIOKafkaProducer`.
   - Метод `publish_photo_analysis_requested()` с retry логикой.

3. **Новый класс `OutboxPublisher`** (в services или integrations):
   - Периодическая задача по дошифровке неотправленных событий.
   - Запускается в lifespan.

### Слой интеграций (`app/integrations/`)

1. **Расширить `analyzer_client.py`**:
   - Реальный gRPC клиент (генерация стабов из protos/).
   - Метод `analyze_photo_grpc()` → вызов analyzer-stub.

2. **Новый `kafka_producer.py`**:
   - `KafkaProducerService` — обёртка над aiokafka.
   - Методы: connect(), publish(), close().

### Слой репозиториев (`app/repositories/`)

1. **`photo_repository.py`:**
   - Новый метод `update_status_atomic()` — для идемпотентного захвата (UPDATE ... WHERE status='pending').
   - Новый метод `update_with_analysis_results()`.
   - Новый метод `update_with_error()`.
   - Новые методы для outbox: `get_not_sent_photos()`, `mark_as_sent()`.

2. **Новый `analysis_result_repository.py`:**
   - CRUD для AnalysisResults таблицы.

3. **Новый `batch_repository.py`:**
   - CRUD для Batches таблицы.
   - Метод `compute_best_photo()` — вычисление по формуле.

### Слой моделей (`app/db/`)

1. **`models.py`:**
   - Расширить `Photo` новыми полями (attempts, last_error_code, last_error_message, batch_id, outbox_status).
   - Новые классы `AnalysisResults`, `Batch`.

### Слой API (`app/api/`)

1. **`photos.py`:**
   - Расширить `POST /v1/photos` → добавить логику outbox.
   - Новый `POST /v1/photos/batch` эндпоинт.
   - Новый `GET /v1/batches/{batch_id}` эндпоинт.
   - Расширить `GET /v1/photos/{id}` → добавить поле `analysis`.

2. **Новый файл `metrics.py`** (или в photos.py):
   - Регистрация Prometheus метрик.
   - Эндпоинт `/metrics`.

### Worker (новое)

1. **Новый модуль `app/worker/`:**
   - `worker.py` — main entry point.
   - `consumer.py` — цикл обработки Kafka сообщений.
   - Инициализация: Kafka consumer, gRPC клиент, сессия БД, логирование.

### Конфиг (`app/core/`)

1. **`config.py`:**
   - Добавить поля Settings: KAFKA_BOOTSTRAP_SERVERS, ANALYZER_GRPC_ADDR, PROMETHEUS_PORT, etc.

### Миграции (`migrations/`)

1. **`versions/v002_add_analysis_and_batches.py`** (новый файл).

### Главная точка входа (`app/`)

1. **`main.py`:**
   - Инициализировать KafkaProducerService и OutboxPublisher в lifespan.
   - Запустить фоновую задачу outbox publisher.
   - Может потребоваться расширить `/readyz` проверкой Kafka.

### Тесты (`tests/`)

1. **`test_photo_service.py`:**
   - Новые тесты для outbox логики.
   - Моки Kafka producer.

2. **`test_migration_integration.py`:**
   - Добавить проверку v002 (новые таблицы, колонки) в `TestV001MigrationAgainstRealPostgres`.

3. **Новые тесты:**
   - `test_worker_*.py` — юнит и интеграционные тесты worker.
   - `test_batch_endpoints.py` — тесты для POST /v1/photos/batch и GET /v1/batches/{id}.
   - `test_kafka_producer.py` — тесты Kafka producer.

---

## 8. Риски и смежные места, которые могут сломаться

### Критические риски

1. **Порядок flush-save-commit в `photo_service.py::create_photo()`:**
   - Если добавить Kafka publish ПЕРЕД commit (вместо после), то при крахе приложения событие будет опубликовано, но запись в БД может не сохраниться → расхождение состояния.
   - **Решение:** всегда publish после commit, или использовать outbox (publish в БД, затем фоновая дошифровка).

2. **Идемпотентность worker (дубли из Kafka):**
   - Если Kafka отправит одно сообщение дважды (rebalance, crash), worker должен распознать дубль и не обновить status дважды.
   - **Решение:** атомарный UPDATE ... WHERE status='pending' возвращает 0 строк на дубле → skip.
   - **Риск:** если использовать просто UPDATE без WHERE по старому status, получим гонку двух worker-ов.

3. **Offset commit timing:**
   - Если коммитить offset ПЕРЕД обработкой, потеря сообщения при краше worker-а.
   - **Решение:** коммитить только после успешной обработки (at-least-once).

4. **Trace ID сквозная трассировка:**
   - `trace_id_var` используется в API middleware; worker должен переиспользовать из Kafka сообщения.
   - **Риск:** забыть установить `trace_id_var.set()` в worker → логи потеряют связь.
   - **Решение:** явно устанавливать в worker consumer loop.

5. **Миграция v002 и down_revision:**
   - Если down_revision указан неправильно (не v001), откат сломается.
   - **Критично:** тестировать на реальном Postgres.

### Смежные места

6. **`app/db/session.py`:**
   - Текущая `SessionLocal` использует `expire_on_commit=False` (хорошо для одиночных запросов, но worker может открывать сессию на длительное время).
   - **Риск:** если worker держит session открытой между сообщениями, может быть утечка connection pool.
   - **Решение:** worker должен открывать/закрывать сессию для каждого сообщения.

7. **Exception handlers в `app/core/errors.py`:**
   - Новые типы ошибок (например, `KafkaUnavailable`, `AnalyzerGrpcError`) нужны в иерархии AppError.
   - **Риск:** если новые ошибки не регистрируются, вернётся 500 вместо 503.
   - **Решение:** добавить в hierarchy в errors.py.

8. **Prometheus metrics и thread-safety:**
   - Если метрики обновляются из worker (asyncio thread) и API (asyncio task), нужна thread-safe обёртка.
   - **Риск:** race condition в счётчике.
   - **Решение:** prometheus-client уже thread-safe; но важно инициализировать до использования.

9. **Configuration и lru_cache:**
   - `app.core.config.get_settings()` использует `@lru_cache` → изменение .env не отражается до перезагрузки.
   - **Риск:** если менять KAFKA_BOOTSTRAP_SERVERS в .env, worker может не увидеть изменение (использует кэшированное значение из инициализации API).
   - **Решение:** документировать; в PROD использовать secret manager, не .env.

10. **Lifespan и graceful shutdown:**
    - Если добавить фоновые задачи (outbox publisher, Kafka consumer) в lifespan, нужно добавить их завершение в shutdown секции.
    - **Риск:** task зависнет на завершение, приложение не выключится.
    - **Решение:** использовать `asyncio.create_task()` с явным cancel на shutdown.

11. **Outbox status в photo_service::create_photo():**
    - Нужно решить: добавить в обновление status при commit или отдельная колонка?
    - **Текущий риск:** если добавить как отдельное UPDATE после commit, есть окно, где можно потерять событие (crash между commit и update).
    - **Решение:** outbox status должна быть частью основного commit (одна транзакция).

12. **Батчевая валидация:**
    - Если один файл в батче невалиден, весь батч отклоняется (400).
    - **Риск:** клиент может ожидать, что некорректные файлы будут просто пропущены.
    - **Решение:** задокументировать в API spec, тесты должны проверить это поведение.

13. **Best photo формула:**
    - Вычисляется при GET /v1/batches/{id}; если фото обновляются после запроса, best_photo_id может измениться.
    - **Риск:** race condition при параллельных запросах.
    - **Решение:** вычислять по запросу (не кэшировать), или кэшировать в отдельной колонке и обновлять после всех фото завершены.

---

## 9. Открытые вопросы для architect

1. **Kafka producer sync vs async:** использовать `aiokafka` (async, но может быть медленнее) или fire-and-forget с фоновой task?

2. **Outbox реализация:** отдельная таблица `outbox` vs колонка `outbox_status` в `photos`?

3. **Worker в одном образе или отдельном:** переиспользовать app/ пакет (тот же образ, разные ENTRYPOINT) или отдельный?

4. **Prometheus exporter в worker:** отдельный HTTP сервер на PROMETHEUS_PORT или встроить в lifespan?

5. **Best photo computation:** вычислять в памяти (при GET) или кэшировать в DB?

6. **gRPC стабы:** где генерировать (в каком пакете), как обновлять при изменении .proto?

7. **Consumer lag monitoring:** добавить метрику для Kafka consumer lag?

8. **Retry классификация:** откуда брать информацию о retry-able ошибках (из gRPC status code)?

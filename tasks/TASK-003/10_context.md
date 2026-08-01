---
task_id: TASK-003
agent: context-collector
model: haiku
status: done
inputs: [specs/constitution.md, specs/feature-upload/tasks.md, tasks/TASK-003/00_orchestration.md, tasks/TASK-003/05_spike_analyzer.md]
outputs: [tasks/TASK-003/10_context.md]
timestamp: 2026-07-21T00:00:00Z
---

# TASK-003 — Контекст кода: Реальный анализатор, веб-интерфейс, Kubernetes, деплой

## Исходный статус

- ветка: после мёржа PR #6 (включает TASK-002.1 багфиксы)
- протокол анализатора: `photo.analyzer.v1` (НЕСОВПАДЕНИЕ с реальным `analyzer.v1`)
- лимит gRPC: 4 МБ (дефолт) vs MAX_FILE_SIZE_BYTES = 50 МБ (несовместимость выявлена спайком A3)
- заглушка анализатора: старый контракт без `image_bytes` и новых полей
- worker: не подключен к MinIO (нужен для A2)
- формула лучшего кадра: сортирует `blur_score` по возрастанию (ЛОМАЕТСЯ от реальной семантики)

---

## Блок A — Анализатор: текущее состояние и точки правки

### A1: Proto контракт — несовпадение пакета

**Файл:** `photo-service/protos/analyzer.proto` (строки 1–30)

**Текущее состояние:**
```protobuf
package photo.analyzer.v1;  // ← НЕПРАВИЛЬНО, реальный анализатор ожидает analyzer.v1

service PhotoAnalyzer {
  rpc AnalyzePhoto(AnalyzePhotoRequest) returns (AnalyzePhotoResponse);
}
message AnalyzePhotoRequest {
  string photo_id = 1;
  string object_key = 2;
  // ОТСУТСТВУЕТ: bytes image_bytes (нужен для A2)
}
message AnalyzePhotoResponse {
  int32 faces_count = 1;
  bool is_blurred = 2;
  double blur_score = 3;
  string perceptual_hash = 4;
  // ОТСУТСТВУЮТ: eyes_closed_count, dominant_color, tags[], model_version
}
```

**Что менять:**
- Строка 13: `package photo.analyzer.v1;` → `package analyzer.v1;`
- Добавить в `AnalyzePhotoRequest`: `bytes image_bytes = 3;`
- Добавить в `AnalyzePhotoResponse`:
  - `int32 eyes_closed_count = 5;`
  - `string dominant_color = 6;`
  - `repeated string tags = 7;`
  - `string model_version = 8;`

**Следствие:** перегенерировать стабы в `app/grpc_gen/analyzer_pb2{,_grpc}.py` (генерируются из .proto, не ручное редактирование).

---

### A2: Worker подключение к MinIO — новая зависимость

**Файл:** `photo-service/app/worker/main.py` (строки 52–66)

**Текущий AnalysisProcessor инициализация:**
```python
processor = AnalysisProcessor(
    photo_repository=PhotoRepository(),
    analysis_repository=AnalysisResultRepository(),
    analyzer=analyzer,
    settings=settings,
    batch_repository=BatchRepository(),
    # ОТСУТСТВУЕТ: ObjectStorage для чтения байтов
)
```

**Текущий `analyze()` вызов в `analysis_processor.py:154`:**
```python
response = await self._analyzer.analyze(photo_id, object_key)
# Отправляет только photo_id и object_key; image_bytes НЕ передаёт
```

**Что менять:**
- `AnalysisProcessor.__init__`: добавить параметр `storage: ObjectStorage | None = None`
- В `process()` перед gRPC-вызовом (строка 153): получить байты через `await anyio.to_thread.run_sync(self._storage.get_file, object_key)` под таймаутом
- Передать `image_bytes=data` в `self._analyzer.analyze(photo_id, object_key, image_bytes=data)` (новая сигнатура)
- В worker/main.py: подключить `ObjectStorage(settings)` и передать в `AnalysisProcessor`

**Ошибка чтения минио:** классифицировать как **RETRYABLE** (временная, соответствует протоколу TASK-002).

---

### A3: Лимит размера gRPC-сообщения (спайк выполнен)

**Спайк A3 (tasks/TASK-003/05_spike_analyzer.md) установил:**
- Сервер принимает максимум 4 МБ на сообщение (дефолт gRPC, не поднят на сервере)
- Текущий MAX_FILE_SIZE_BYTES = 50 МБ (строка 58 в `app/services/photo_service.py`)
- **Несовместимы**: отправка фото 50 МБ целиком в `image_bytes` вызывает `RESOURCE_EXHAUSTED`

**Решение (выбирает architect в 20_design.md):** сейчас не менять константы, architect решает, уменьшать ли лимит загрузки или реализовать ужимание копии перед отправкой в анализатор.

---

### A4: Классификация RESOURCE_EXHAUSTED — разделение ошибок

**Файл:** `photo-service/app/integrations/analyzer_client.py` (строки 26–34)

**Текущая классификация (ПРОБЛЕМНЫЙ КОД):**
```python
_RETRYABLE_GRPC_CODES = frozenset({
    grpc.StatusCode.UNAVAILABLE,
    grpc.StatusCode.DEADLINE_EXCEEDED,
    grpc.StatusCode.RESOURCE_EXHAUSTED,  # ← ВСЕ RESOURCE_EXHAUSTED = RETRY
    grpc.StatusCode.ABORTED,
    grpc.StatusCode.INTERNAL,
})
```

**Текущая функция `classify_grpc_error()` (строки 75–96):**
- Проверяет только код statusa, не различает причину `RESOURCE_EXHAUSTED`
- "Сообщение слишком большое" (постоянная ошибка) → RETRY → 3 бесполезные попытки → `failed` с невнятным кодом

**Что менять:**
- Добавить функцию `error_code_from_exception()` (уже есть, но возвращает generic код) или новую проверку в `classify_grpc_error()`
- Если `RESOURCE_EXHAUSTED` И текст содержит "larger than max" → `NO_RETRY`, `last_error_code = "MESSAGE_TOO_LARGE"`
- Если `RESOURCE_EXHAUSTED` БЕЗ этого текста (троттлинг анализатора) → `RETRY`

**Альтернатива:** предварительно проверить размер `len(image_bytes)` до gRPC-вызова и отклонить сразу, если превышение не поправимо.

---

### A5: Миграция v003 — новые поля в analysis_results

**Текущая схема `AnalysisResult` (app/db/models.py:105–129):**
```python
class AnalysisResult(Base):
    __tablename__ = "analysis_results"
    photo_id: Mapped[uuid.UUID] = ... (PK + FK)
    faces_count: Mapped[int] = ...
    is_blurred: Mapped[bool] = ...
    blur_score: Mapped[float] = ...
    perceptual_hash: Mapped[str] = ...
    created_at: Mapped[datetime] = ...
    photo: Mapped["Photo"] = relationship(...)
```

**Что добавить (A5 спека):**
- `eyes_closed_count: Mapped[int | None]` — nullable (старые записи не имеют)
- `dominant_color: Mapped[str | None]` — VARCHAR, nullable
- `tags: Mapped[...?...]` — JSONB или TEXT[] (architect решает, нужно обоснование)
- `model_version: Mapped[str | None]` — VARCHAR, nullable

**Миграция v003:** создать новый файл `photo-service/migrations/versions/v003_add_analyzer_extended_fields.py`
- Синтаксис как v002 (PostgreSQL, `op.add_column`, `server_default=None` для nullable)
- `downgrade()` дропить колонки в обратном порядке
- Не вводить новые `PostgreSQL.ENUM` (урок из TASK-000)

**Текущая последняя миграция:** `v002` (up_revision = v001, down_revision = None)

---

### A6: AnalysisResultRepository.upsert — добавить новые поля

**Файл:** `photo-service/app/repositories/analysis_result_repository.py` (строки 19–39)

**Текущая сигнатура:**
```python
async def upsert(
    self,
    session: AsyncSession,
    photo_id: uuid.UUID,
    faces_count: int,
    is_blurred: bool,
    blur_score: float,
    perceptual_hash: str,
) -> None:
    # INSERT ... ON CONFLICT (photo_id) DO NOTHING
```

**Что менять:** добавить параметры:
- `eyes_closed_count: int | None = None`
- `dominant_color: str | None = None`
- `tags: list[str] | None = None` (если JSONB) или `str | None` (если TEXT[])
- `model_version: str | None = None`

**Вызывающее место:** `app/services/analysis_processor.py:186–193`
```python
await self._analysis.upsert(
    session,
    photo_uuid,
    faces_count=response.faces_count,
    is_blurred=response.is_blurred,
    blur_score=response.blur_score,
    perceptual_hash=response.perceptual_hash,
    # НУЖНЫ НОВЫЕ ПОЛЯ:
    eyes_closed_count=response.eyes_closed_count,
    dominant_color=response.dominant_color,
    tags=response.tags,
    model_version=response.model_version,
)
```

---

### A7: Заглушка анализатора — обновить под новый контракт

**Файл:** `photo-service/analyzer-stub/server.py` (строки 40–51)

**Текущая функция `_analyze()` (ЛОМАЕТСЯ):**
```python
def _analyze(object_key: str) -> "analyzer_pb2.AnalyzePhotoResponse":
    digest = hashlib.sha256(object_key.encode("utf-8")).digest()
    faces_count = digest[0] % 6
    blur_score = (int.from_bytes(digest[1:3], "big") % 1001) / 1000.0  # 0…1
    is_blurred = blur_score > 0.6  # ЛОГИКА ЗАГЛУШКИ ≠ РЕАЛЬНОЙ СЕМАНТИКЕ
    perceptual_hash = digest.hex()[:16]
    return analyzer_pb2.AnalyzePhotoResponse(
        faces_count=faces_count,
        is_blurred=is_blurred,
        blur_score=blur_score,
        perceptual_hash=perceptual_hash,
        # ОТСУТСТВУЮТ: eyes_closed_count, dominant_color, tags[], model_version
    )
```

**Что менять:**
- Принимать `image_bytes` из запроса (может не использовать, но сигнатуру соблюдать)
- Вернуть все 8 полей новой AnalyzePhotoResponse
- Для воспроизводимости: генерировать из хэша `image_bytes` ИЛИ `object_key` (детерминированно)
- Генерировать `blur_score` как дисперсию лапласиана: **больше = резче** (а не 0…1)
  - Пример: диапазон 0…1000 вместо 0…1, где 900+ = резко, 0…100 = размыто
- Пример `tags`: список, может содержать `["landscape", "bright"]`
- `model_version`: строка, например `"mock/1.0.0"`
- `eyes_closed_count`: int 0–2
- `dominant_color`: hex цвет, например `"#ffffff"`

---

### A8: AnalyzerGrpcClient — дефер TLS закрыт (F7 из TASK-002.1)

**Файл:** `photo-service/app/integrations/analyzer_client.py` (строки 45–58)

**Текущий код (правильный для MVP):**
```python
self._channel = grpc.aio.insecure_channel(addr)
```

**Комментарий (TASK-002.1, строки 48–57):** описывает дефер, т.к. было неясно, нужен ли TLS.

**Решение спайка A3:** анализатор требует **plaintext, TLS нет**. Закрыто definitively.

**Что менять:** обновить комментарий в коде: "...Deferred to TASK-003 once a real analyzer arrives... (TASK-003 A8: real analyzer confirmed plaintext/insecure)."

---

## Блок B — Веб-интерфейс: текущее состояние

### B1: API структура и CORS

**Основной файл:** `photo-service/app/main.py` (строки 103–136)

**Текущая FastAPI сборка:**
```python
app = FastAPI(title=settings.APP_NAME, lifespan=lifespan)
app.add_middleware(RequestIdMiddleware)
app.middleware("http")(metrics_middleware)
register_exception_handlers(app)
app.include_router(photos.router)  # prefix="/v1/photos"
app.include_router(batches.router)  # prefix="/v1"

# HTTP эндпоинты:
# GET /healthz, GET /readyz, GET /metrics (swagger автоматический)
# POST /v1/photos, GET /v1/photos, GET /v1/photos/{id}, GET /v1/photos/{id}/content
# POST /v1/photos/batch, GET /v1/batches/{id}
```

**CORS:** НЕ установлен (стандартная FastAPI без CORS middleware).

**Что менять:**
- Добавить CORS middleware (перед или после RequestIdMiddleware):
  ```python
  from fastapi.middleware.cors import CORSMiddleware
  app.add_middleware(
      CORSMiddleware,
      allow_origins=settings.CORS_ALLOWED_ORIGINS,  # env var из config.py
      allow_credentials=True,
      allow_methods=["GET", "POST"],
      allow_headers=["*"],
  )
  ```
- В `app/core/config.py` добавить: `CORS_ALLOWED_ORIGINS: str = "http://localhost:3000,http://localhost:5173"`

### B2: Схемы ответов — расширить для новых полей анализа

**Файл:** `photo-service/app/schemas/photos.py` (строки 30–51)

**Текущая AnalysisResultResponse (НЕПОЛНАЯ):**
```python
class AnalysisResultResponse(BaseModel):
    faces_count: int
    is_blurred: bool
    blur_score: float
    perceptual_hash: str
    # ОТСУТСТВУЮТ: eyes_closed_count, dominant_color, tags, model_version
```

**Что менять:**
```python
class AnalysisResultResponse(BaseModel):
    faces_count: int
    is_blurred: bool
    blur_score: float
    perceptual_hash: str
    eyes_closed_count: int | None = None
    dominant_color: str | None = None
    tags: list[str] | None = None
    model_version: str | None = None
```

**Вычисления на клиенте:** `blur_score` теперь неограниченное (не 0…1), клиент должен это знать при отображении.

### B3: Маппер — обновить анализ в ответ

**Файл:** `photo-service/app/services/mappers.py` (строки 20–30)

**Текущая функция `analysis_to_response()`:**
```python
def analysis_to_response(analysis: AnalysisResult | None) -> AnalysisResultResponse | None:
    if analysis is None:
        return None
    return AnalysisResultResponse(
        faces_count=analysis.faces_count,
        is_blurred=analysis.is_blurred,
        blur_score=analysis.blur_score,
        perceptual_hash=analysis.perceptual_hash,
        # НУЖНЫ НОВЫЕ ПОЛЯ:
        eyes_closed_count=analysis.eyes_closed_count,
        dominant_color=analysis.dominant_color,
        tags=analysis.tags,
        model_version=analysis.model_version,
    )
```

---

## Блок C/D — Инфраструктура: Kubernetes и деплой

### C1: docker-compose.yml — текущая структура

**Файл:** `photo-service/docker-compose.yml` (строки 1–199)

**Сервисы (уже имеют restart: unless-stopped):**
1. `postgres:16` — DB (порт 5432, healthcheck)
2. `minio` — S3-хранилище (порт 9000, 9001 консоль, healthcheck)
3. `kafka:3.7.0` — KRaft (порт 9092, healthcheck, автосоздание топика)
4. `analyzer-stub` — gRPC-сервис (порт 50051, NO healthcheck)
5. `api` — FastAPI (порт 8000, зависит от postgres/minio/kafka, alembic migrate в command)
6. `worker` — consumer (порт 8001 metrics, зависит от postgres/kafka/api)
7. `prometheus` — сбор метрик (порт 9090)
8. `grafana` — дашборд (порт 3000, зависит от prometheus)

**Для TASK-003:**
- Версии образов сохранить, добавить новые поля env для переключения на реальный анализатор (server.yml override)
- WebUI слой нужен — еще один сервис (nginx/node)

### C2: Конфиг (app/core/config.py) — текущие настройки

**Файл:** `photo-service/app/core/config.py` (строки 15–61)

**Ключевые переменные (для Kubernetes ConfigMap):**
```python
# App
APP_NAME: str = "photo-service"
API_HOST: str = "0.0.0.0"
API_PORT: int = 8000
LOG_LEVEL: str = "INFO"

# Database
DATABASE_URL: str = "postgresql+asyncpg://photo:photo@postgres:5432/photo_db"

# MinIO
MINIO_ENDPOINT: str = "minio:9000"
MINIO_ACCESS_KEY: str = "minioadmin"  # ← SECRET
MINIO_SECRET_KEY: str = "minioadmin"  # ← SECRET
MINIO_BUCKET: str = "photos"
MINIO_SECURE: bool = False

# Kafka
KAFKA_BOOTSTRAP_SERVERS: str = "kafka:9092"
KAFKA_TOPIC_ANALYSIS_REQUESTED: str = "photo.analysis.requested"
KAFKA_CONSUMER_GROUP: str = "photo-analysis-workers"
KAFKA_PUBLISH_TIMEOUT_SECONDS: float = 10.0

# Analyzer gRPC
ANALYZER_GRPC_ADDR: str = "analyzer-stub:50051"  # ← ПЕРЕКЛЮЧАЕТСЯ на реальный в TASK-003
ANALYZER_GRPC_TIMEOUT: float = 30.0

# Worker retry
WORKER_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_BASE_SECONDS: float = 1.0

# Metrics
WORKER_METRICS_PORT: int = 8001

# Outbox
OUTBOX_POLL_INTERVAL_SECONDS: float = 2.0
OUTBOX_BATCH_SIZE: int = 100
```

**Что добавить:**
- `CORS_ALLOWED_ORIGINS: str` (для B1)
- `MINIO_SECURE_CERT_PATH: str | None` (опционально, для future TLS)

### C3: Prometheus и Grafana — текущая конфигурация

**Файл:** `photo-service/prometheus.yml` (не показан, предполагается стандартный)

**Файл:** `photo-service/grafana/provisioning/` (не показан)

**Текущие метрики (app/integrations/metrics_api.py + metrics_worker.py):**
- `photos_uploaded_total` (API)
- `http_requests_total` (API middleware)
- `http_request_duration_seconds` (API)
- `storage_upload_errors_total` (API)
- `kafka_publish_errors_total` (API)
- `photos_pending` (API, gauge)
- `photo_analysis_started_total` (worker)
- `photo_analysis_completed_total` (worker)
- `photo_analysis_failed_total` (worker)
- `photo_analysis_duration_seconds` (worker)
- `analyzer_grpc_errors_total` (worker)
- `worker_messages_processed_total` (worker)

**На TASK-003:** никаких изменений метрик не требуется.

---

## Блок D — Живой стенд на учебном сервере

### D1: Учебный сервер доступ

**Параметры (из 00_orchestration.md):**
- Адрес: `45.132.19.101`
- SSH: `ssh -i artemiy_sirius -p 22102 artemiy@45.132.19.101`
- Рабочая директория: `/home/artem/student_data/artemiy/work`
- Публичные порты: **51100–51119** (только эти)
- Compose проект: `-p artemiy` (уникальное имя)
- Внешний анализатор: `45.132.19.101:50051` (доступен из контейнера как ANALYZER_ADDR)

### D2: docker-compose.server.yml (СОЗДАТЬ)

**Создать:** `photo-service/docker-compose.server.yml` (override файл или standalone)

**Отличия от развития:**
- Порты: `api:51101`, `web:51100`, `grafana:51102`, `prometheus:51103`, `minio-console:51104`
- `ANALYZER_GRPC_ADDR=45.132.19.101:50051` (внешний, не контейнер)
- Сервис `analyzer-stub` **НЕ поднимается** (не входит в override)
- Остальное (postgres, minio, kafka, prometheus, grafana) — внутренняя сеть

### D3: Web UI сервис (СОЗДАТЬ)

**Создать:** `photo-service/web/` или `photo-service/web-ui/`

**Структура (минимальная):**
- `index.html` (главная страница)
- `app.js` (логика, fetch вызовы)
- `Dockerfile` (nginx или node)
- `docker-compose` service с портом 51100

**Функциональность (из спеки B1):**
- Загрузка одиночного файла (POST /v1/photos)
- Загрузка батча (POST /v1/photos/batch)
- Галерея (GET /v1/photos с polling)
- Карточка фото (GET /v1/photos/{id} + /content)
- Страница батча (GET /v1/batches/{id}, подсветка best_photo_id)
- Автообновление статусов (polling 2–3 сек)

---

## Формула лучшего кадра — КРИТИЧНОЕ ИЗМЕНЕНИЕ

### Спайк A3 выявил инверсию семантики

**Текущая формула (app/services/batch_service.py:44–54):**
```python
def select_best_photo(photos: list[Photo]) -> uuid.UUID | None:
    candidates = [p for p in photos if p.status == PhotoStatus.done and p.analysis is not None]
    if not candidates:
        return None
    
    best = min(
        candidates,
        key=lambda p: (
            p.analysis.is_blurred,       # False = лучше (0 < 1)
            p.analysis.blur_score,       # МЕНЬШЕ = ЛУЧШЕ (0…1 диапазон)
            -p.analysis.faces_count,     # Больше лиц = лучше
            p.created_at,
            p.photo_id,
        ),
    )
    return best.photo_id
```

**Проблема:** заглушка отдавала `blur_score` 0…1 (меньше = резче), реальный анализатор отдаёт дисперсию лапласиана (больше = резче).

**ИТОГ:** с реальным анализатором эта формула выбирает **самый размытый кадр** вместо самого резкого.

**Требуется:** architect в 20_design.md предложить новый порядок ключей, обоснованный на реальной семантике:
- `blur_score DESC` (больше = резче)
- Переоценить роль `is_blurred` (в спайке не различал резкое/размытое)
- Возможно: убрать `is_blurred`, опираться только на `blur_score`

**Тесты под удар:** `tests/test_batch_service.py` проверяет старую формулу, должны пересчитать ожидаемые результаты.

---

## Тесты — что сломается

### По блоку A (анализатор)

1. **test_analyzer_client.py** — мокирует gRPC-сервер старой сигнатурой, нужна новая
2. **test_analyzer_stub.py** — проверяет заглушку, нужно обновить под новый контракт
3. **test_analysis_processor.py** — AnalysisProcessor.process(), вызывает analyze(), нужна новая сигнатура
4. **test_analyzer_client.py::test_classify_grpc_error** — проверяет classify_grpc_error(), может потребоваться добавить case для MESSAGE_TOO_LARGE

### По блоку B (веб-интерфейс)

1. Фронтенд-тесты будут в отдельном проекте/папке (не в коде бэка)

### По блоку A + формула (критично)

1. **test_batch_service.py::test_select_best_photo** — проверяет формулу, **сломается** при смене порядка ключей
   - Должны быть новые замеры на реальной семантике `blur_score`

### По конфигу

1. Тесты могут зависеть от env vars (ANALYZER_GRPC_ADDR), убедиться в изоляции (mock по умолчанию)

---

## Депencies и версии (pyproject.toml)

**Runtime (спека TASK-003 не требует новых):**
- `grpcio>=1.82.1` — уже есть
- `protobuf>=7.35.1` — уже есть
- `minio>=7.2` — уже есть (worker будет использовать)

**Dev (для grpcio-tools):**
- `grpcio-tools>=1.82.1` — уже есть (генерировать новые стабы)

**New (может быть для веб-интерфейса):**
- `Pillow` (если будет ужимание изображений в worker)

---

## Миграции — текущее состояние

**Последняя:** `photo-service/migrations/versions/v002_add_analysis_and_batches.py` (строки 1–162)
- Создаёт `analysis_results`, `batches`, добавляет колонки в `photos`
- Стиль: `sa.Column()`, FK, CHECK constraints (БЕЗ новых ENUM)

**Требуется:** `v003_add_analyzer_extended_fields.py`
- Добавить 4 колонки в `analysis_results`
- Следовать стилю v002

**Проверка:** тест миграции на реальном Postgres (testcontainers, как в TASK-001/002)

---

## Где ляжет изменение — по компонентам

### Блок A — Анализатор (30_impl точка)

| Файл | Строки | Что менять | Тесты |
|------|--------|-----------|-------|
| `protos/analyzer.proto` | 13, 19–29 | Пакет, AddImageBytes, новые поля ответа | (не тестится прямо, через стабы) |
| `app/grpc_gen/*.py` | * | Перегенерировать (не ручное редактирование) | test_analyzer_client.py |
| `app/integrations/analyzer_client.py` | 26–34, 75–96 | Разделить RESOURCE_EXHAUSTED, обновить комментарий F7 | test_analyzer_client.py |
| `app/services/analysis_processor.py` | 61–76, 108–224 | Добавить storage в init, читать байты, новые поля в upsert | test_analysis_processor.py |
| `app/integrations/storage.py` | 89–102 | Нет изменений (уже работает) | test_storage.py |
| `app/worker/main.py` | 59–66 | Инициализировать ObjectStorage, передать в AnalysisProcessor | test_worker_main.py |
| `app/db/models.py` | 105–129 | AnalysisResult: добавить 4 колонки | (тестируется миграцией) |
| `app/repositories/analysis_result_repository.py` | 19–39 | Сигнатура upsert, вставить новые параметры | test_analysis_result_repository.py |
| `app/schemas/photos.py` | 30–37 | AnalysisResultResponse: добавить новые поля | test_schemas.py (может не быть) |
| `app/services/mappers.py` | 20–30 | analysis_to_response: маппировать новые поля | test_mappers.py (может не быть) |
| `analyzer-stub/server.py` | 40–51, 54–64 | Принять image_bytes, генерировать 8 полей с новой семантикой | test_analyzer_stub.py |
| `migrations/versions/` | — | Создать v003 для новых колонок | test_migration_integration.py |

### Блок B — Веб-интерфейс (30_impl точка)

| Компонент | Что создать | Функциональность |
|-----------|-----------|------------------|
| `web/index.html` | Новый | Галерея, загрузка, вывод анализа |
| `web/app.js` | Новый | API вызовы, polling, UI обновление |
| `web/Dockerfile` | Новый | nginx или node + express |
| `app/main.py` | +CORS middleware | Разрешить фронтенд-запросы |
| `app/core/config.py` | +CORS_ALLOWED_ORIGINS | Конфиг источников |

### Блок C — Kubernetes (30_impl точка)

| Манифест | Что создать | Что включить |
|----------|-----------|-------------|
| `k8s/namespace.yml` | Новый | Namespace для всех подов |
| `k8s/configmap.yml` | Новый | Нечувствительные настройки из config.py |
| `k8s/secret.yml` | Новый | DB/MinIO credы (БЕЗ SSH-ключей!) |
| `k8s/postgres.yml` | Новый | StatefulSet + Service + PVC |
| `k8s/minio.yml` | Новый | StatefulSet + Service + PVC |
| `k8s/kafka.yml` | Новый | StatefulSet + Service + PVC |
| `k8s/api.yml` | Новый | Deployment + Service, миграции в initContainer |
| `k8s/worker.yml` | Новый | Deployment (scalable), пробы (readiness?) |
| `k8s/prometheus.yml` | Новый | Deployment + Service + ConfigMap |
| `k8s/grafana.yml` | Новый | Deployment + Service (+ PVC для storage опционально) |
| `k8s/web.yml` | Новый | Deployment + Service |
| `k8s/ingress.yml` | Новый | NodePort или Ingress для внешнего доступа |

### Блок D — Деплой на сервер (не в impl, а в живом тестировании)

| Файл | Что создать |
|------|-----------|
| `docker-compose.server.yml` | Override для портов 51100–51119, внешний анализатор |
| `web/` | Всё веб-приложение |
| `.gitignore` | Добавить `*_sirius`, `*.pem`, `student_ssh_keys*` |
| `README.md` раздел | "Запуск на сервере" |

---

## Риски и смежные места

### Критичные

1. **Proto пакет несовпадение (A1)** — без этого все вызовы вернут UNIMPLEMENTED
   - Проверить: `full_method = /analyzer.v1.PhotoAnalyzer/AnalyzePhoto`

2. **Формула лучшего кадра (спайк A3)** — инвертированная семантика ломает главную фичу
   - Решение обязано быть в спеке 20_design.md, иначе фича поедет наоборот

3. **Лимит размера gRPC (спайк A3)** — 4 MiB vs 50 МБ
   - Если architect выберет снижение MAX_FILE_SIZE_BYTES — может побить тесты и UX
   - Если выберет ужимание — нужна зависимость Pillow + логика в worker

### Мажор

4. **Worker подключение к MinIO (A2)** — worker впервые будет читать файлы
   - Любой баг в чтении (таймаут, потеря памяти) = анализ падает
   - Обязательны тесты с большими файлами (~40 МБ)

5. **RESOURCE_EXHAUSTED разделение (A4)** — без этого размер-ошибки ретрируются впустую
   - Если забыть про разделение — продакш будет медленно падать на больших файлах

6. **Заглушка реалистичность (A6)** — если семантика заглушки ≠ реальной, тесты будут лживыми
   - Новая семантика blur_score ОБЯЗАНА быть в заглушке

7. **CORS (B3)** — без этого веб-интерфейс не сможет дёргать API
   - Проверить: `allow_origins` включает фронтенд-порт (51100)

### Минор

8. **Миграция v003 откат** — обязательно протестировать downgrade
9. **SSH-ключи в .gitignore** — критично для безопасности (5, spek-то требует это)
10. **Healthcheck для analyzer-stub** — сейчас отсутствует, может стоять задержку старта worker

---

## Файлы не найдены / вне скоупа

- Фронтенд-код (нужно создать в TASK-003)
- Kubernetes манифесты (нужно создать в TASK-003)
- `docker-compose.server.yml` (нужно создать в TASK-003)
- Промежуточные файлы трансформации изображений (если потребуется ужимание)

---

## Итоговая карта затронутых файлов (по блокам)

**Блок A — 15 файлов (критично)**
1. `protos/analyzer.proto` — пакет + поля
2. `app/grpc_gen/analyzer_pb2.py` — перегенерировать
3. `app/grpc_gen/analyzer_pb2_grpc.py` — перегенерировать
4. `app/integrations/analyzer_client.py` — классификация ошибок, комментарий
5. `app/services/analysis_processor.py` — storage, новые поля, читать байты
6. `app/worker/main.py` — инициализировать storage
7. `app/db/models.py` — AnalysisResult колонки
8. `app/repositories/analysis_result_repository.py` — сигнатура upsert
9. `app/schemas/photos.py` — AnalysisResultResponse
10. `app/services/mappers.py` — маппировать новые поля
11. `analyzer-stub/server.py` — новая реализация заглушки
12. `migrations/versions/v003_add_analyzer_extended_fields.py` — СОЗДАТЬ
13. `app/core/errors.py` — может + новый код для MESSAGE_TOO_LARGE

**Блок B — 3 файла (веб-интерфейс)**
1. `app/main.py` — CORS middleware
2. `app/core/config.py` — CORS_ALLOWED_ORIGINS
3. `web/` — СОЗДАТЬ полностью (index.html, app.js, Dockerfile)

**Блок C — 12 файлов (Kubernetes, все СОЗДАТЬ)**
1–12. `k8s/*.yml` — namespace, configmap, secret, postgres, minio, kafka, api, worker, prometheus, grafana, web, ingress

**Блок D — 2 файла (деплой на сервер)**
1. `docker-compose.server.yml` — СОЗДАТЬ override
2. `.gitignore` — добавить паттерны для SSH-ключей

**Итого: ~36 точек правки, из них ~15 СОЗДАТЬ с нуля**

---

## Ключевые тестовые сценарии (пострадают от изменений)

1. **test_analyzer_client.py::test_classify_grpc_error** — добавить case для MESSAGE_TOO_LARGE
2. **test_analyzer_stub.py** — новая реализация, новые поля в ответе
3. **test_analysis_processor.py::test_process** — новая сигнатура analyze(), storage mock
4. **test_batch_service.py::test_select_best_photo** — КРИТИЧНО: новые ожидаемые результаты при новой формуле
5. **test_migration_integration.py** — v003 upgrade/downgrade на эфемерном Postgres
6. **Живой тест D3** — загрузка реального фото через веб-интерфейс, ожидание done, проверка анализа


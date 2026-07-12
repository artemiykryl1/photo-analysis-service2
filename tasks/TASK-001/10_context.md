---
task_id: TASK-001
agent: context-collector
model: haiku
status: done
inputs:
  - specs/constitution.md
  - specs/feature-upload/tasks.md
  - specs/feature-upload/spec.md
  - tasks/TASK-000/20_design.md
  - tasks/TASK-000/30_impl.md
  - tasks/TASK-000/60_debug.md
outputs:
  - tasks/TASK-001/10_context.md
timestamp: 2026-07-12T00:00:00Z
---

# TASK-001 — Контекст: Публичный API работы с фотографиями

## 1. Требования TASK-001 (из `specs/feature-upload/tasks.md`, источник приоритета)

Закрыть DoD Воркшопа 2 (слайды 40–41):
- **Загрузка**: `POST /v1/photos` (multipart, валидация MIME/размер, 202 Accepted)
- **Получение списка**: `GET /v1/photos` (массив PhotoResponse)
- **Получение метаданных**: `GET /v1/photos/{photo_id}` (status, filename, 404 если нет)
- **Скачивание контента**: `GET /v1/photos/{photo_id}/content` (байты из MinIO, 404/503)
- **Единый формат ошибок** (`error_code`, `message`, `request_id`)
- **JSON-логи** (каждый шаг: photo_id, request_id)
- **Repository layer**: `create`, `get_by_id`, `list`, `update_status` (никакого SQL вне репозитория)
- **Миграция БД**: переименовать enum-значения и поле `s3_path`→`object_key`
- **`.proto` контракт** analyzer-а (только определение, без вызовов из кода)

**Зафиксированные решения (ОБЯЗАТЕЛЬНЫ):**
- Префикс API: `/v1/photos` (НЕ `/api/v1/photos`, как в каркасе)
- HTTP-код загрузки: **202 Accepted** (НЕ 200)
- Статусы фото: `pending → processing → done → failed` (НЕ `queued/analyzing/done/error`)
- Имя поля пути: `object_key` (НЕ `s3_path`)

---

## 2. Карта файлов под изменение

### 2.1 Роутер и главная точка входа

**`photo-service/app/api/photos.py`** (сейчас: пустой)
- Текущее состояние: `router = APIRouter(prefix="/api/v1/photos", tags=["photos"])`
- **НУЖНО ИЗМЕНИТЬ**: префикс на `/v1/photos`
- **НУЖНО ДОБАВИТЬ**: четыре маршрута:
  - `POST /` (upload_photo)
  - `GET /` (list_photos)
  - `GET /{photo_id}` (get_photo_status)
  - `GET /{photo_id}/content` (download_photo_content)

**`photo-service/app/main.py`** (готов, не менять структуру)
- Текущее состояние: `setup_logging()`, `lifespan()`, `register_exception_handlers()`, `/healthz`, `/readyz` работают
- **МОЖЕТ ПОТРЕБОВАТЬСЯ**: добавить middleware для проставления `request_id` в `trace_id_var` (TODO в core/logging.py, строка 25–27; по tasks.md слайд 35 логи должны содержать request_id)

### 2.2 Pydantic-схемы (DTOs)

**`photo-service/app/schemas/photos.py`** (сейчас: только `HealthResponse` и `ErrorResponse`)
- **НУЖНО ДОБАВИТЬ** (по tasks.md слайд 16, слайд 50):
  - `UploadPhotoResponse { photo_id: str, status: Literal["pending"] }`
  - `PhotoResponse { id: str, filename: str, status: Literal["pending","processing","done","failed"] }`
  - Возможно: `PhotoListResponse` или просто `list[PhotoResponse]`

### 2.3 Модель БД и enum

**`photo-service/app/db/models.py`** (строки 23–27)
- **ТЕКУЩИЙ enum PhotoStatus:**
  ```python
  class PhotoStatus(str, enum.Enum):
      queued = "queued"
      analyzing = "analyzing"
      done = "done"
      error = "error"
  ```
- **НУЖНО ПЕРЕИМЕНОВАТЬ на:**
  ```python
  class PhotoStatus(str, enum.Enum):
      pending = "pending"
      processing = "processing"
      done = "done"
      failed = "failed"
  ```
- **Модель Photo (строки 30–47):**
  - Поле `s3_path` (строка 37) **НУЖНО ПЕРЕИМЕНОВАТЬ на `object_key`**
  - Поле `status` default (строка 43): `default=PhotoStatus.queued` **НУЖНО ИЗМЕНИТЬ на `default=PhotoStatus.pending`**
  - Остальное: `photo_id`, `user_id`, `uploaded_at`, индекс — оставить как есть
  - По tasks.md слайд 54: добавить `created_at` (или убедиться, что `uploaded_at` используется для этого; на слайде есть оба, но в конституции §2.3 только `uploaded_at`)

### 2.4 Миграция БД v001

**`photo-service/migrations/versions/v001_init_photos.py`**
- **ТЕКУЩИЕ enum-значения (строка 22–24):**
  ```python
  photo_status_enum = postgresql.ENUM(
      "queued", "analyzing", "done", "error", name="photo_status", create_type=False
  )
  ```
- **НУЖНО ИЗМЕНИТЬ на:**
  ```python
  photo_status_enum = postgresql.ENUM(
      "pending", "processing", "done", "failed", name="photo_status", create_type=False
  )
  ```
- **Столбец `s3_path` (строка 35):**
  ```python
  sa.Column("s3_path", sa.String(length=1024), nullable=False),
  ```
- **НУЖНО ПЕРЕИМЕНОВАТЬ на:**
  ```python
  sa.Column("object_key", sa.String(length=1024), nullable=False),
  ```
- **server_default для status (строка 46):**
  ```python
  server_default="queued",
  ```
- **НУЖНО ИЗМЕНИТЬ на:**
  ```python
  server_default="pending",
  ```

**ОТКРЫТЫЙ ВОПРОС:** Правка существующей v001 vs новая ревизия v002?
- v001 уже применена в TASK-000 (фикс enum с `create_type=False` был включён)
- По tasks.md решение не зафиксировано; по фактам: если БД можно пересоздать (локальная разработка), правим v001
- Рекомендация: architect должен решить, но для Воркшопа 2 (локальная БД) скорее всего приемлема правка v001 с пересозданием БД

### 2.5 Сервис

**`photo-service/app/services/photo_service.py`**
- **Текущее состояние:** класс `PhotoService(repository, storage)`, метод `upload_photo()` → `NotImplementedError`
- **НУЖНО ДОБАВИТЬ/РЕАЛИЗОВАТЬ:**
  - `async def upload_photo(file: UploadFile, user_id: str, session: AsyncSession) -> UploadPhotoResponse`
    - Валидация: MIME по magic bytes (JPEG/PNG), размер ≤ 50 МБ, не пустой
    - Сохранить в MinIO по пути `photos/{photo_id}/original.<ext>`
    - Создать запись в БД `photos` со `status=pending` (ОДНА транзакция)
    - Ответить 202 с `photo_id` и `status="pending"`
  - `async def get_photo(photo_id: str, session: AsyncSession) -> PhotoResponse` — получить статус
  - `async def list_photos(user_id: str, skip: int, limit: int, session: AsyncSession) -> list[PhotoResponse]` — список
  - `async def download_photo(photo_id: str, session: AsyncSession) -> bytes` — скачать контент из MinIO
  - Обернуть MinIO-вызовы в `asyncio.timeout(...)` (по tasks.md слайд 23)
  - Логировать `photo_id`, `request_id` на каждом шаге (по tasks.md слайд 35)

### 2.6 Repository

**`photo-service/app/repositories/photo_repository.py`**
- **Текущее состояние:** класс `PhotoRepository`, методы `create()` и `get()` → `NotImplementedError`
- **НУЖНО РЕАЛИЗОВАТЬ:**
  - `async def create(self, session: AsyncSession, photo: Photo) -> Photo` — вставка, обработка ConflictError (UNIQUE)
  - `async def get(self, session: AsyncSession, photo_id: UUID) -> Photo | None` — по photo_id
  - `async def list(self, session: AsyncSession, user_id: str, skip: int, limit: int) -> list[Photo]` — с пагинацией
  - `async def update_status(self, session: AsyncSession, photo_id: UUID, status: PhotoStatus) -> Photo` — обновление статуса
  - Никакого SQL вне этого модуля (по tasks.md слайд 29)

### 2.7 Интеграции

**`photo-service/app/integrations/storage.py`** (готов, но нужны улучшения)
- **Текущее состояние:** `ObjectStorage` с `ensure_bucket()`, `save_file()`, `get_file()` — работают
- **НУЖНО УЛУЧШИТЬ:**
  - Обернуть блокирующие вызовы MinIO в `anyio.to_thread.run_sync()` (TODO в коде, строка 11; save_file/get_file вызываются из async-хендлеров в TASK-001)
  - Валидация пути (нет `../`, `//`) — TODO в коде, строка 59
  - Поддержка multipart-загрузки для больших файлов — может быть дополнение, проверить требования

**`photo-service/app/integrations/analyzer_client.py`** (пустой заглушка)
- **НУЖНО ДОБАВИТЬ:** `.proto` контракт analyzer-а (только файл `.proto`, без Python-реализации)
  - По tasks.md слайд 38: `service PhotoAnalyzer { rpc AnalyzePhoto(...) }`
  - `AnalyzePhotoRequest { photo_id, object_key }`
  - `AnalyzePhotoResponse { faces_count, is_blurred, blur_score, perceptual_hash }`
  - Путь: `photo-service/proto/analyzer.proto` (новая папка `proto/`)
  - Генерированные стабы (Python из grpc_tools) — опционально (по tasks.md: только контракт)

### 2.8 Ошибки и логирование (готовы)

**`photo-service/app/core/errors.py`** (работает, не менять)
- Есть: `AppError`, `StorageUnavailable(503)`, `NotFoundError(404)`, `ValidationError(400)`, `ConflictError(409)`, маппинг на HTTP
- **МОЖЕТ ПОТРЕБОВАТЬСЯ:** добавить `UnsupportedMediaTypeError(415)` для MIME (по tasks.md слайд 47)
- По tasks.md слайд 47: неправильный тип → 415 (Unsupported Media Type)

**`photo-service/app/core/logging.py`** (готово)
- JSON-логирование есть, `trace_id_var` определена (ContextVar)
- **TODO:** middleware для проставления `request_id` из запроса (по tasks.md слайд 35, по слайду 50 требуется `request_id` в логах)
  - Нужно создать middleware в TASK-001, который генерирует/берёт `request_id` из заголовка и сохраняет в `trace_id_var`

### 2.9 Конфиг (готов)

**`photo-service/app/core/config.py`**
- Все переменные для postgres, minio, логирования есть
- По convention.md §3.2 и tasks.md требуется `request_id` в логах — это middleware-задача, не конфиг

### 2.10 БД/Session (готово)

**`photo-service/app/db/session.py`**
- `engine`, `SessionLocal`, `get_session()` — готовы
- pool_size=10, max_overflow=5 — в пределах конституции (§3.4: макс 50)

### 2.11 Docker/Compose (готово, но может потребоваться проверка)

**`photo-service/Dockerfile`** — готов (python:3.12-slim, uv, uvicorn)
- После фикса B1 в TASK-000 порядок слоёв правильный

**`photo-service/docker-compose.yml`** — готов
- postgres (5432), minio (9000/9001), api (8000)
- healthcheck'и настроены
- `depends_on: service_healthy` — есть
- Команда старта: `alembic upgrade head && uvicorn` — есть

---

## 3. Зависимости: что есть, чего нет

### Установлены (в `pyproject.toml`)
- fastapi (>=0.111) ✓
- uvicorn[standard] (>=0.30) ✓
- pydantic (>=2.7) ✓
- pydantic-settings (>=2.3) ✓
- sqlalchemy[asyncio] (>=2.0) ✓
- asyncpg (>=0.29) ✓
- alembic (>=1.13) ✓
- minio (>=7.2) ✓
- python-json-logger (>=2.0) ✓
- anyio (>=4.3) ✓
- pytest, pytest-asyncio, httpx, ruff (dev) ✓

### **МОЖЕТ НЕ ХВАТАТЬ** (для TASK-001)
- **python-multipart** — для `UploadFile` в FastAPI (хотя FastAPI может использовать встроенный парсер, явно указано в TASK-000 как потребное в дизайне §4)
- **grpcio-tools** и **protobuf** — для `.proto` генерации (если нужны Python-стабы; но контракт может быть просто `.proto` файл без Python)
- **testcontainers-python** или **pytest-postgresql** — для интеграционного теста миграции (рекомендация из tasks.md слайд 76; обязателен для гейтов проекта; пока не установлен)
- **pillow** или **python-magic** — для валидации MIME по magic bytes (нет явного требования в constitution, но нужно для валидации JPEG/PNG)

**Рекомендация:** Добавить в `pyproject.toml` перед началом работы:
```toml
python-multipart = ">=0.0.7"
python-magic = ">=0.4.27"  # или pillow для magic bytes validation
# И в dev:
pytest-postgresql = ">=5.0"  # или testcontainers-python
protobuf = ">=4.0"
grpcio-tools = ">=1.60"
```

---

## 4. Что переиспользуем из TASK-000

✅ **Полностью готово:**
- `app/core/config.py` — Settings, переменные
- `app/core/errors.py` — исключения, маппинг на HTTP (может потребоваться добавить 415)
- `app/core/logging.py` — JSON-логирование, contextvar `trace_id_var`
- `app/db/session.py` — engine, sessionmaker, get_session()
- `app/db/models.py` — структура (переименовать enum и поле)
- `app/integrations/storage.py` — ObjectStorage (переделать на async)
- `app/schemas/photos.py` — ErrorResponse, добавить DTO
- `app/main.py` — структура lifespan, register_exception_handlers, include_router
- `app/services/photo_service.py` — структура класса (реализовать методы)
- `app/repositories/photo_repository.py` — структура класса (реализовать методы)
- `migrations/env.py` — async Alembic (не менять)
- `docker-compose.yml`, `Dockerfile` — готовы

❌ **НУЖНО ПЕРЕДЕЛАТЬ:**
- `app/api/photos.py` — префикс и маршруты
- `app/db/models.py` — enum-значения и имя поля
- `migrations/versions/v001_init_photos.py` — enum-значения и имя поля

⚠️ **НУЖНО ДОБАВИТЬ НОВОЕ:**
- Middleware для request_id (в app/main.py или отдельный модуль)
- `.proto` контракт analyzer (новая папка proto/)
- Обёртки `anyio.to_thread.run_sync()` для storage.py
- Интеграционный тест миграции (в tests/)

---

## 5. Открытые вопросы для architect

1. **Миграция БД (enum-значения и переименование поля):**
   - Правка существующей v001 (требует пересоздания БД `down -v base`) или новая ревизия v002?
   - По фактам: v001 уже применена в TASK-000; по воркшопу (локальная БД) скорее приемлема правка v001
   - **Решение:** architect должен указать в 20_design.md

2. **Поле `created_at`:**
   - tasks.md слайд 54 упоминает оба: `created_at` и `uploaded_at`, но конституция §2.3 говорит только про `uploaded_at`
   - `uploaded_at` уже есть в модели с `server_default=func.now()`
   - Нужно ли добавлять отдельное `created_at`? (Вероятно нет, это дублирование)

3. **JWT и user_id:**
   - По конституции §3.3 требуется JWT-аутентификация, но воркшоп её не требует
   - `user_id` уже в модели Photo, но откуда он берётся в хендлере?
   - **Решение:** architect должен указать: mock user_id (hardcode) для воркшопа или JWT-middleware?

4. **Request ID vs Trace ID:**
   - По tasks.md слайд 35, 50: логи должны содержать `request_id`
   - По constitution.md §3.1 и коду core/logging.py: это `trace_id` из contextvar
   - **Вопрос:** это одно и то же? Нужен ли middleware для их проставления?
   - **Решение:** architect должен уточнить в 20_design.md

5. **MIME-валидация:**
   - tasks.md слайд 37: валидация по magic bytes (не только расширению)
   - Нужна ли внешняя библиотека (python-magic, pillow)? Или достаточно `minio` SDK?
   - **Решение:** architect должен указать в 20_design.md

6. **HTTP 415 для неправильного типа:**
   - tasks.md слайд 47: не изображение → **415**
   - В core/errors.py нет такого; нужно ли добавлять `UnsupportedMediaTypeError`?

7. **Методы repository.list():**
   - tasks.md слайд 51 требует `list`, но нет explicit пагинации в контракте
   - Нужна ли пагинация (skip/limit) или просто get all?

---

## 6. Риски и смежные места, которые могут сломаться

| Риск | Место | Как избежать |
|------|-------|--------------|
| **Переименование enum в уже применённой миграции** | v001_init_photos.py | Ясное решение architect в 20_design.md (v001 vs v002) |
| **Забыли переименовать в модели, но переименовали в миграции** | db/models.py vs migrations/v001 | Синхронизация: если меняем v001, меняем и Photo.status enum |
| **s3_path vs object_key по всему коду** | storage.py возвращает f"{bucket}/s3_path" | Grep `s3_path` перед фиксом, заменить везде |
| **Prefix роутера меняется, но есть ссылки на старый** | api/photos.py prefix vs compose/docs | Обновить prefix в router, проверить swagger/openapi |
| **async обёртки для MinIO не добавлены** | integrations/storage.py save_file/get_file | обязательно: `anyio.to_thread.run_sync()`, иначе event loop заблокируется |
| **Request ID middleware не реализован** | logging.py trace_id_var не заполняется | middleware TASK-001: генерить/брать из заголовка, сет в trace_id_var |
| **user_id hardcode vs JWT** | api/photos.py GET-хендлер | Ясное решение architect: mock или JWT? |
| **Интеграционный тест миграции** | tests/ — нет pytest-postgresql | Добавить testcontainers в dev-deps, обязателен по tasks.md слайд 76 |
| **MIME-валидация не реализована** | services/photo_service.py upload_photo | Нужна валидация по magic bytes, ошибка → ValidationError(415) |
| **Нет multipart-upload для больших файлов** | integrations/storage.py | tasks.md не требует, но constitution §3.4 говорит о multipart для >50MB; уточнить architect |

---

## 7. Краткая карта точек изменения (для coder)

| Файл | Статус | Описание |
|------|--------|---------|
| `app/api/photos.py` | **переписать** | Префикс `/v1/photos`, 4 маршрута |
| `app/schemas/photos.py` | **добавить** | UploadPhotoResponse, PhotoResponse |
| `app/db/models.py` | **правка enum + поле** | pending/processing/done/failed, object_key |
| `migrations/versions/v001_init_photos.py` | **правка enum + столбец** | Синхронизировать с моделью |
| `app/services/photo_service.py` | **реализовать** | 4 метода (upload, get, list, download) |
| `app/repositories/photo_repository.py` | **реализовать** | 4 метода CRUD + update_status |
| `app/integrations/storage.py` | **улучшить** | anyio.to_thread для async хендлеров |
| `app/core/logging.py` | **добавить middleware** | trace_id_var из запроса (или главный files app/main.py) |
| `app/core/errors.py` | **опционально** | +UnsupportedMediaTypeError(415) если нужен |
| `proto/analyzer.proto` | **создать новый** | Контракт AnalyzePhoto RPC |
| `pyproject.toml` | **добавить зависимости** | python-multipart, python-magic, pytest-postgresql, protobuf |
| `tests/` | **добавить тесты** | Интеграционный тест миграции + тесты эндпоинтов |


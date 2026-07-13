---
task_id: TASK-000
agent: architect
model: opus
inputs:
  - specs/constitution.md
  - specs/feature-upload/spec.md
  - tasks/TASK-000/10_context.md
outputs:
  - tasks/TASK-000/20_design.md
spec_refs:
  - constitution.md §2.3 (хранение), §3.1 (наблюдаемость), §4 (стек)
  - feature-upload/spec.md §2 (форматы ответа/ошибки), §6 (лимиты)
status: done
timestamp: 2026-07-11T00:00:00Z
---

# TASK-000 — Дизайн каркаса photo-service (bootstrap)

## 1. Обзор и границы скоупа

Цель TASK-000 — собрать **вертикальный каркас** сервиса `photo-service`: рабочий скелет,
который поднимается через `docker compose up` (api + postgres + minio) и отвечает `200` на
`GET /healthz`. Никакой бизнес-логики загрузки, никаких Kafka/gRPC/analyzer.

### В скоупе
- Слоистая структура каталогов (`app/api|services|repositories|db|integrations|core|schemas`).
- `core/config.py` (pydantic-settings), `core/logging.py` (JSON), `core/errors.py` (доменные ошибки + маппинг на HTTP).
- `db/session.py` (async engine + `get_session`), `db/models.py` (DeclarativeBase + модель `Photo` как основа).
- `integrations/storage.py` (обёртка MinIO с ensure-bucket), `integrations/analyzer_client.py` (пустая заглушка).
- `api/photos.py` (роутер-заготовка), `schemas/photos.py` (место под схемы + модель ошибки).
- `main.py` (FastAPI app, `GET /healthz`, `GET /readyz`, lifespan).
- Alembic (`alembic.ini`, `migrations/env.py` под async, начальная ревизия).
- `Dockerfile`, `docker-compose.yml`, `pyproject.toml`, `.env.example`, `.gitignore`.

### Вне скоупа (осознанно отложено на TASK-001+)
- `POST /api/v1/photos` и любая логика загрузки/валидации файлов.
- Kafka producer/consumer, топик `photos-to-analyze`, DLQ, идемпотентность.
- gRPC, api-gateway, result-service, analyzer (реальный).
- JWT-аутентификация (реальная валидация токена), извлечение `user_id`.
- Метрики Prometheus, трассировка Jaeger, полноценный readiness со всеми зависимостями.
- Circuit breaker, rate limiting, Redis-кэш.

### Соответствие constitution
Constitution описывает мультисервис (api-gateway / photo-service / result-service). В спринте 2
делаем один сервис `photo-service` как вертикальный срез — это **не противоречит** конституции,
а является её первым инкрементом. Kafka/gRPC/analyzer/result-service явно вынесены заданием из
скоупа TASK-000 и будут добавлены в следующих тасках. Наблюдаемость (JSON-логи с trace_id) и
формат ошибок (`error_code`, `message`, `trace_id`) закладываются уже сейчас в соответствии с §3.1
и spec §2.

---

## 2. Слои и правила зависимостей

Направление зависимостей — **только сверху вниз**. Поперечные модули (`core`, `integrations`,
`schemas`) могут использоваться любым слоем, но сами не зависят от вышестоящих.

```
            HTTP (client)
                 │
        ┌────────▼─────────┐
        │  api/ (роутеры)  │  валидация вход/выход (Pydantic), коды ответов.
        │                  │  НЕ содержит SQL, НЕ ходит в MinIO напрямую.
        └────────┬─────────┘
                 │ вызывает
        ┌────────▼─────────┐
        │ services/        │  бизнес-логика, оркестрация repo + integrations.
        └────────┬─────────┘
                 │ вызывает
        ┌────────▼─────────┐
        │ repositories/    │  SQL/ORM, работа в рамках переданной AsyncSession.
        └────────┬─────────┘
                 │ использует
        ┌────────▼─────────┐
        │ db/ (models,     │  DeclarativeBase, engine, sessionmaker, get_session.
        │      session)    │
        └──────────────────┘

  Поперечные (доступны всем слоям, зависят только от stdlib/config):
    core/       config, logging, errors
    integrations/  storage (MinIO), analyzer_client (stub)
    schemas/    Pydantic DTO (вход/выход API, модель ошибки)
```

Жёсткие правила (для ревью):
- Хендлер в `api/` **не** импортирует SQLAlchemy-модели/`select`/`session.execute` и **не**
  импортирует `minio`/`storage` для прямых операций — только `services/`.
- `services/` не знает про FastAPI (`Request`/`Depends`), получает уже подготовленные аргументы и
  `AsyncSession`.
- `repositories/` получает `AsyncSession` аргументом, не создаёт сессию сам, не коммитит
  глобально (границу транзакции держит вызывающий слой/зависимость).
- Фото — только в MinIO (через `integrations/storage.py`), в БД хранится лишь `s3_path` (метаданные).
- `core/` и `schemas/` не импортируют `services/`/`repositories/`/`api/` (нет циклов).

> В TASK-000 `services/photo_service.py` и `repositories/photo_repository.py` создаются как
> тонкие заготовки (класс + docstring + сигнатуры на будущее), реальные методы наполняются в TASK-001.

---

## 3. Спецификация файлов

Все пути относительно `photo-service/`. Каждый пакет содержит `__init__.py` (пустой), кроме
корня приложения — `app/__init__.py` может содержать `__version__ = "0.1.0"`.

### 3.1 `app/core/config.py`
Назначение: единый источник конфигурации из env/`.env`.

```python
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # App
    APP_NAME: str = "photo-service"
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000
    LOG_LEVEL: str = "INFO"

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://photo:photo@postgres:5432/photo_db"

    # MinIO
    MINIO_ENDPOINT: str = "minio:9000"
    MINIO_ACCESS_KEY: str = "minioadmin"     # локальный дефолт; в prod — из env/секрета
    MINIO_SECRET_KEY: str = "minioadmin"     # локальный дефолт
    MINIO_BUCKET: str = "photos"
    MINIO_SECURE: bool = False               # http локально

# Кэшируем инстанс, чтобы не пересоздавать при каждом Depends.
from functools import lru_cache

@lru_cache
def get_settings() -> Settings:
    return Settings()
```

Правила:
- Дефолты рассчитаны на docker-compose (host `postgres`/`minio`). Для запуска вне compose
  значения переопределяются через `.env`.
- Секреты имеют дефолты **только** для локального MinIO (`minioadmin`) — задокументировать, что
  в prod обязательны env-переменные. Реальные секреты в репозиторий не коммитятся.

### 3.2 `app/core/logging.py`
Назначение: структурное JSON-логирование в stdout, уровень из `LOG_LEVEL`.

- Использовать `python-json-logger` (`pythonjsonlogger.jsonlogger.JsonFormatter`).
- `setup_logging(level: str) -> None`: конфигурирует root logger, один `StreamHandler(sys.stdout)`
  с JSON-форматтером; поля: `timestamp` (asctime), `level`, `name`, `message`. Добавить статическое
  поле `service="photo-service"` через `logging.LoggerAdapter` или `defaults`/фильтр.
- Заложить место под `trace_id`: определить `contextvars.ContextVar[str] trace_id_var` и `logging.Filter`,
  который дописывает `trace_id` в record из этого contextvar (если пусто — `"-"`).
  Полноценный middleware, проставляющий trace_id из входящего запроса, — TODO на TASK-001
  (оставить комментарий с указанием точки расширения).
- Не логировать содержимое файлов, полные пути MinIO, user_id в открытом виде (правило на будущее — комментарий).

### 3.3 `app/core/errors.py`
Назначение: доменные исключения и их маппинг на HTTP + единый формат тела ошибки (spec §2).

```python
class AppError(Exception):
    error_code: str = "INTERNAL_ERROR"
    http_status: int = 500
    message: str = "Internal server error"
    def __init__(self, message: str | None = None): ...

class StorageUnavailable(AppError):      # MinIO недоступен
    error_code = "SERVICE_UNAVAILABLE"; http_status = 503
class NotFoundError(AppError):
    error_code = "NOT_FOUND"; http_status = 404
class ValidationError(AppError):
    error_code = "INVALID_FILE"; http_status = 400
class ConflictError(AppError):
    error_code = "CONFLICT"; http_status = 409
```

- Функция-регистратор `register_exception_handlers(app: FastAPI) -> None`:
  - handler для `AppError` → JSON `{"error_code", "message", "trace_id"}` со статусом `exc.http_status`;
  - `trace_id` берётся из `trace_id_var` (core.logging);
  - логирует ошибку с `error_code`.
- Тело ошибки описать Pydantic-схемой `ErrorResponse` в `schemas/photos.py` (или `schemas/errors.py`) —
  см. 3.9. Формат строго по spec §2: `error_code`, `message`, `trace_id`.

### 3.4 `app/db/session.py`
Назначение: async engine + фабрика сессий + FastAPI-зависимость.

```python
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

engine = create_async_engine(get_settings().DATABASE_URL, pool_pre_ping=True, pool_size=10, max_overflow=5)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session   # commit/rollback — ответственность вызывающего слоя (services)
```

- Единственное «глобальное» состояние — `engine` и `SessionLocal` (необходимый минимум).
- Прогрев/закрытие engine — в lifespan (`await engine.dispose()` на shutdown).
- `pool_size` ≤ 50 (constitution §3.4). Для bootstrap достаточно 10.

### 3.5 `app/db/models.py`
Назначение: `DeclarativeBase` + модель `Photo` как основа под будущий upload.

**Решение: модель `Photo` заводим сейчас** (обоснование ниже). `AnalysisResult` — НЕ заводим
в TASK-000 (относится к результатам анализа, TASK-003; вне вертикального среза Client→API→PG/MinIO).

```python
class Base(DeclarativeBase): ...

class PhotoStatus(str, enum.Enum):
    queued = "queued"; analyzing = "analyzing"; done = "done"; error = "error"

class Photo(Base):
    __tablename__ = "photos"
    photo_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    s3_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    status: Mapped[PhotoStatus] = mapped_column(
        SAEnum(PhotoStatus, name="photo_status"), default=PhotoStatus.queued, nullable=False)
    __table_args__ = (Index("ix_photos_user_uploaded", "user_id", "uploaded_at"),)
```

Обоснование заведения модели сейчас:
- Alembic нужна непустая начальная ревизия; это даёт осмысленную схему `v001` и позволяет проверить,
  что миграции реально применяются к PostgreSQL (часть критерия «рабочий каркас»).
- Схема `photos` уже зафиксирована в constitution §2.3 и spec §2 — риск изменений минимален.
- Эндпоинтов, использующих модель, нет — правило «эндпоинтов загрузки нет» не нарушается.

Альтернатива (риск): оставить пустой `Base` и сделать пустую initial-ревизию Alembic. Отклонено —
меньше пользы для проверки вертикали, всё равно придётся заводить модель в TASK-001.

### 3.6 `app/integrations/storage.py`
Назначение: обёртка над MinIO SDK (`minio.Minio`), конфиг из `Settings`.

```python
class ObjectStorage:
    def __init__(self, settings: Settings): ...     # создаёт Minio(endpoint, access, secret, secure)

    def ensure_bucket(self) -> None:
        """Идемпотентно: если бакета нет — создать. Вызывается в lifespan при старте."""

    def save_file(self, object_name: str, data: BinaryIO, length: int, content_type: str) -> str:
        """Кладёт объект в MINIO_BUCKET, возвращает s3_path (bucket/object_name). Ошибки → StorageUnavailable."""

    def get_file(self, object_name: str) -> bytes:
        """Читает объект. Отсутствует → NotFoundError; сеть/недоступность → StorageUnavailable."""
```

- В TASK-000 реализовать конструктор + `ensure_bucket` (нужны для старта); `save_file`/`get_file` —
  рабочие сигнатуры с базовой реализацией через SDK и маппингом `S3Error`/сетевых ошибок на
  `StorageUnavailable`/`NotFoundError`. Полная валидация файла/путей — TASK-001.
- MinIO SDK синхронный: обернуть блокирующие вызовы в `anyio.to_thread.run_sync` при вызове из
  async-кода — оставить TODO-комментарий, в bootstrap `ensure_bucket` вызывается на старте один раз.
- Один инстанс `ObjectStorage` создаётся в lifespan и кладётся в `app.state.storage`; зависимость
  `get_storage(request)` возвращает его (без глобала).

### 3.7 `app/integrations/analyzer_client.py`
Назначение: **пустая заглушка** на будущее (реальный анализатор — external, вне скоупа).

- Только docstring модуля + `TODO`, описывающие будущий интерфейс, например:
  `async def request_analysis(photo_id, s3_path, trace_id) -> None: ...` — как комментарий/`...`,
  **без реализации** и без импорта в остальной код.

### 3.8 `app/api/photos.py`
Назначение: роутер-заготовка (эндпоинтов загрузки нет).

- `router = APIRouter(prefix="/api/v1/photos", tags=["photos"])`.
- В TASK-000 роутер пустой (без маршрутов) ЛИБО один заглушечный `GET ""`/`GET "/"`, возвращающий
  `501 Not Implemented` или простой список-заглушку. Рекомендация: **оставить пустым** (без маршрутов),
  чтобы не плодить фиктивные контракты; добавить комментарий «маршруты upload/list — TASK-001/TASK-004».
- `healthz`/`readyz` живут в `main.py` (инфраструктурные, не относятся к домену photos).

### 3.9 `app/schemas/photos.py`
Назначение: место под Pydantic DTO + общая модель ошибки.

```python
class HealthResponse(BaseModel):
    status: str            # "ok"
    service: str           # "photo-service"

class ErrorResponse(BaseModel):     # формат по spec §2
    error_code: str
    message: str
    trace_id: str
```

- DTO загрузки (`PhotoUploadResponse` и т.п.) — НЕ добавляем сейчас (TASK-001). Можно оставить
  закомментированный пример/TODO. `ErrorResponse` нужен уже сейчас для `register_exception_handlers`.

### 3.10 `app/services/photo_service.py`
Назначение: заготовка бизнес-слоя.

- Класс `PhotoService` с конструктором, принимающим `repository`, `storage` (и в будущем producer).
- Методы-сигнатуры на будущее (`async def upload_photo(...)`) — как `...`/`raise NotImplementedError`
  с TODO-ссылкой на TASK-001. Никакой рабочей логики в TASK-000.

### 3.11 `app/repositories/photo_repository.py`
Назначение: заготовка слоя доступа к БД.

- Класс `PhotoRepository`, методы принимают `session: AsyncSession`.
- Сигнатуры на будущее: `async def create(session, photo: Photo) -> Photo`, `async def get(session, photo_id) -> Photo | None`
  — как `...`/`NotImplementedError` с TODO. Реальные запросы — TASK-001.

### 3.12 `app/main.py`
Назначение: сборка приложения.

- `setup_logging(get_settings().LOG_LEVEL)` при импорте/старте.
- `lifespan(app)` (async context manager):
  - startup: создать `ObjectStorage(settings)`, `storage.ensure_bucket()`, положить в `app.state.storage`;
    прогреть БД лёгким `SELECT 1` (не блокировать старт при недоступности — залогировать warning);
  - shutdown: `await engine.dispose()` (graceful).
- `app = FastAPI(title=..., lifespan=lifespan)`.
- `register_exception_handlers(app)`.
- `app.include_router(photos.router)`.
- `GET /healthz` — liveness, всегда `200` `{"status":"ok","service":"photo-service"}` (не трогает БД/MinIO).
- `GET /readyz` — readiness, упрощённо: пробует `SELECT 1` и `storage` доступность; при ошибке `503`.
  Для bootstrap допустимо мягко: readyz не должен ломать `docker compose up` — если проверка падает,
  возвращаем 503, но контейнер живой (liveness через `/healthz`).
- Точка входа uvicorn: `uvicorn app.main:app --host ... --port ...` (запуск задаётся в Dockerfile/compose).

---

## 4. Конфигурация и переменные окружения

`.env.example` (значения-плейсхолдеры, реальный `.env` — в `.gitignore`):

```
# App
LOG_LEVEL=INFO
API_HOST=0.0.0.0
API_PORT=8000

# PostgreSQL (asyncpg драйвер обязателен)
DATABASE_URL=postgresql+asyncpg://photo:photo@postgres:5432/photo_db

# MinIO
MINIO_ENDPOINT=minio:9000
MINIO_ACCESS_KEY=minioadmin
MINIO_SECRET_KEY=minioadmin
MINIO_BUCKET=photos
MINIO_SECURE=false
```

`.gitignore` (ключевое): `.env`, `__pycache__/`, `*.pyc`, `.venv/`, `.pytest_cache/`, `.ruff_cache/`,
`*.egg-info/`, `.mypy_cache/`.

Примечание по Alembic: Alembic использует **синхронный** URL или тот же async — в `env.py`
конфигурируем async engine (см. §6). `DATABASE_URL` один и тот же (`postgresql+asyncpg://...`).

---

## 5. Docker / Compose дизайн

### 5.1 `Dockerfile` (слои, кэш-френдли)
```
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
# 1) установить uv (pip install uv или copy из ghcr.io/astral-sh/uv)
# 2) WORKDIR /app
# 3) COPY pyproject.toml uv.lock* ./        # сначала манифесты — слой кэшируется
# 4) RUN uv sync --frozen --no-dev          # (или без --frozen, если lock не коммитим)
# 5) COPY app/ ./app/  alembic.ini ./  migrations/ ./migrations/
# 6) EXPOSE 8000
# 7) CMD ["uv","run","uvicorn","app.main:app","--host","0.0.0.0","--port","8000"]
```
- Разделение слоёв: зависимости (шаги 3–4) отдельно от кода (шаг 5) для кэша.
- Опционально non-root user (`USER app`) — рекомендуется, но не блокирует bootstrap.

### 5.2 `docker-compose.yml`
Сервисы: `api`, `postgres`, `minio`.

- **postgres**: image `postgres:16`; env `POSTGRES_USER=photo`, `POSTGRES_PASSWORD=photo`,
  `POSTGRES_DB=photo_db`; volume `pgdata:/var/lib/postgresql/data`; порт `5432:5432`;
  healthcheck `pg_isready -U photo -d photo_db`.
- **minio**: image `minio/minio`; command `server /data --console-address ":9001"`;
  env `MINIO_ROOT_USER=minioadmin`, `MINIO_ROOT_PASSWORD=minioadmin`; volume `miniodata:/data`;
  порты `9000:9000` (API), `9001:9001` (console);
  healthcheck `curl -f http://localhost:9000/minio/health/live`.
- **api**: build `.`; env_file `.env` (или `environment:` с host'ами `postgres`/`minio`);
  порт `8000:8000`; `depends_on: postgres (service_healthy), minio (service_healthy)`;
  команда старта: **сначала миграции, потом сервер** — `uv run alembic upgrade head && uv run uvicorn app.main:app ...`
  (обернуть в entrypoint-скрипт или `sh -c`). Alembic должен ретраить/ждать готовности postgres —
  за это отвечает `depends_on service_healthy`.
- volumes: `pgdata`, `miniodata`.
- Единая сеть по умолчанию; сервисы адресуются по именам (`postgres`, `minio`).

Критерий готовности: `docker compose up` → все healthy → `curl http://localhost:8000/healthz` == `200`.

---

## 6. Alembic-настройка

- `alembic.ini` в корне `photo-service/`; `script_location = migrations`;
  `sqlalchemy.url` НЕ хардкодим — берём из `Settings.DATABASE_URL` в `env.py`.
- `migrations/env.py` под **async**:
  - импортировать `Base` из `app.db.models` → `target_metadata = Base.metadata`;
  - `run_migrations_online` использует `create_async_engine` + `connection.run_sync(do_run_migrations)`
    (стандартный async-шаблон Alembic);
  - URL брать из `get_settings().DATABASE_URL`.
- `migrations/script.py.mako` — стандартный шаблон.
- Инициализация: `uv run alembic init -t async migrations` даёт async-шаблон; затем правим `env.py`
  под `Settings`/`Base`. Первая ревизия — `uv run alembic revision --autogenerate -m "init photos"`
  (создаёт таблицу `photos`, enum `photo_status`, индекс `ix_photos_user_uploaded`).
- Применение в compose: `alembic upgrade head` перед стартом uvicorn (см. §5.2).

Риск: autogenerate enum в PostgreSQL иногда требует ручной правки (создание типа). Кодеру — проверить
сгенерированную ревизию, при необходимости добавить `sa.Enum(..., name="photo_status").create(bind)` /
корректный `op.create_table`. Альтернатива — написать первую ревизию вручную (детерминированнее).

---

## 7. Шаги для кодера (упорядоченно)

Работать в новом каталоге `photo-service/` в корне репозитория.

1. **Скелет каталогов**: создать дерево `photo-service/app/{api,schemas,services,repositories,db,integrations,core}`
   с `__init__.py` в каждом пакете; создать `photo-service/tests/`, `photo-service/migrations/`.
2. **`pyproject.toml`**: проект под uv, Python `>=3.12`. Зависимости:
   `fastapi`, `uvicorn[standard]`, `pydantic>=2`, `pydantic-settings`, `sqlalchemy[asyncio]>=2`,
   `asyncpg`, `alembic`, `minio`, `python-json-logger`, `anyio`.
   Dev-группа: `pytest`, `pytest-asyncio`, `httpx`, `ruff`.
3. **`.gitignore` и `.env.example`**: по §4.
4. **`app/core/config.py`**: `Settings` + `get_settings()` по §3.1.
5. **`app/core/logging.py`**: `setup_logging`, JSON-форматтер, `trace_id_var` (contextvar) + фильтр по §3.2.
6. **`app/core/errors.py`**: иерархия `AppError` + подклассы + `register_exception_handlers(app)` по §3.3.
   (Зависит от `schemas.ErrorResponse` из шага 9 — можно объявить `ErrorResponse` раньше или в этом же шаге.)
7. **`app/db/models.py`**: `Base(DeclarativeBase)`, `PhotoStatus`, модель `Photo` по §3.5.
8. **`app/db/session.py`**: `engine`, `SessionLocal`, `get_session` по §3.4.
9. **`app/schemas/photos.py`**: `HealthResponse`, `ErrorResponse` по §3.9 (DTO upload — TODO-заглушки).
10. **`app/integrations/storage.py`**: класс `ObjectStorage` (`__init__`, `ensure_bucket`, `save_file`, `get_file`)
    по §3.6, с маппингом ошибок MinIO на `StorageUnavailable`/`NotFoundError`.
11. **`app/integrations/analyzer_client.py`**: пустая заглушка (docstring + TODO) по §3.7.
12. **`app/services/photo_service.py`**: заготовка `PhotoService` по §3.10 (методы — `NotImplementedError` + TODO).
13. **`app/repositories/photo_repository.py`**: заготовка `PhotoRepository` по §3.11.
14. **`app/api/photos.py`**: пустой `APIRouter(prefix="/api/v1/photos")` по §3.8.
15. **`app/main.py`**: `setup_logging`, `lifespan` (ensure_bucket + `SELECT 1` + `engine.dispose`),
    `FastAPI(lifespan=...)`, `register_exception_handlers`, `include_router`, `GET /healthz`, `GET /readyz` по §3.12.
16. **Alembic**: `alembic.ini` + `migrations/env.py` (async, `target_metadata=Base.metadata`,
    URL из `Settings`) + `script.py.mako`; сгенерировать/написать первую ревизию (таблица `photos`,
    enum `photo_status`, индекс) по §6.
17. **`Dockerfile`**: python:3.12-slim + uv + `uv sync` + слои по §5.1; CMD uvicorn.
18. **`docker-compose.yml`**: сервисы `api`/`postgres`/`minio`, healthcheck'и, volumes, `depends_on`,
    старт api = `alembic upgrade head && uvicorn ...` по §5.2.
19. **`README.md`** (краткий): как запустить (`docker compose up`), как проверить `/healthz`, переменные env.
20. **Тестовый минимум** (заготовка для test-writer, если требуется в этой таске): `tests/conftest.py` +
    `tests/test_healthz.py` (httpx ASGI-клиент, `GET /healthz` == 200). Полноценные тесты — фаза test-writer.

### Как проверить (Definition of Done для каркаса)
```
cd photo-service
docker compose up --build       # postgres+minio → healthy, api применяет миграции и стартует
curl -i http://localhost:8000/healthz     # ожидаем HTTP/1.1 200 OK, {"status":"ok",...}
# опционально:
curl -i http://localhost:8000/readyz      # 200 если БД+MinIO доступны, иначе 503
# MinIO console: http://localhost:9001 (minioadmin/minioadmin), бакет "photos" создан на старте
# alembic: внутри контейнера `uv run alembic current` показывает head-ревизию
```

---

## 8. Отложено на будущее (вне скоупа TASK-000)

| Возможность | Куда закладываем сейчас | Когда реализуем |
|---|---|---|
| `POST /api/v1/photos` (upload) | пустой роутер + сигнатуры service/repo | TASK-001 |
| Валидация файла (magic bytes, 50MB) | комментарии в service/storage | TASK-001 |
| Kafka publish `photos-to-analyze` | — (не создаём даже конфиг) | TASK-002 |
| JWT auth, извлечение user_id | поле `user_id` в модели | TASK-001+ |
| Метрики Prometheus / трассировка | JSON-логи + trace_id_var как база | позже |
| Полноценный readiness (все зависимости) | упрощённый `/readyz` | позже |
| Circuit breaker / rate limiting / Redis | error `StorageUnavailable` (503) как база | позже |
| `AnalysisResult` модель | не заводим | TASK-003 |
| gRPC, api-gateway, result-service | — | позже |

---

## Приложение: контракты, вводимые в TASK-000

Единственные HTTP-контракты каркаса:

- `GET /healthz` → `200` `{ "status": "ok", "service": "photo-service" }` (liveness, без зависимостей).
- `GET /readyz` → `200` `{ "status": "ready" }` если БД+MinIO доступны; иначе `503` с телом `ErrorResponse`.

Единый формат ошибки (spec §2), применяется ко всем `AppError`:
```json
{ "error_code": "SERVICE_UNAVAILABLE", "message": "MinIO is unreachable", "trace_id": "..." }
```
Маппинг: `StorageUnavailable→503`, `ValidationError→400`, `NotFoundError→404`, `ConflictError→409`, `AppError→500`.

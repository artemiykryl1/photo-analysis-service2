---
task_id: TASK-000
agent: reviewer-2
model: haiku
status: APPROVE
timestamp: 2026-07-11T00:00:00Z
---

# TASK-000 — Независимое ревью #2 (безопасность, конвенции, наблюдаемость)

## Вердикт

**APPROVE** без BLOCKING замечаний. Каркас соответствует спецификации дизайна (20_design.md), конституции (§3.1–3.3) и задачам bootstrap'а. Код чистый, конвенции соблюдены, критические риски безопасности отсутствуют.

---

## 1. Безопасность и управление секретами

### Позитивные находки

- **Локальные дефолты MinIO в коде (minioadmin) — обоснованы.**  
  `config.py:31–32` содержит жёсткий дефолт `minioadmin` с явным комментарием:  
  ```python
  MINIO_ACCESS_KEY: str = "minioadmin"  # local default only; prod must override via env/secret
  ```
  Это допустимо для docker-compose local dev и **не является утечкой секрета** (это стандартный дефолт MinIO контейнера, не реальный пароль prod).

- **.env в .gitignore.**  
  `.gitignore:2` содержит `.env`. Проверено: реальный конфиг не коммитится.

- **.env.example не содержит реальных значений.**  
  Все значения — плейсхолдеры/стандартные для compose (`minioadmin`, `photo:photo`). Безопасно распространяется.

- **Переменные окружения из Settings (pydantic-settings).**  
  `config.py` использует `BaseSettings` с `env_file=".env"` — стандартный паттерн. Нет захардкодированных prod-секретов.

- **Логирование не содержит секретов.**  
  `logging.py:12–14` содержит комментарий-напоминание:  
  ```python
  # Security note (constitution.md §3.3): do not log full MinIO object
  # paths, raw user_id, or file contents.
  ```
  Проверено: ни в одном логирующем вызове (main.py, errors.py, storage.py) не логируются пароли, ACCESS_KEY, SECRET_KEY или raw БД URLs.

### NON_BLOCKING замечания

- **DATABASE_URL в docker-compose.yml (environment)**  
  `docker-compose.yml:43` явно указывает `DATABASE_URL: postgresql+asyncpg://photo:photo@postgres:5432/photo_db`. Это дефолт для compose (стандартные dev-креды), но в prod сё must переходить на секреты через vault/secrets manager. Документация это ясно не указывает; рекомендуется добавить комментарий в docker-compose.yml или README, что это **только для local dev**. Это не BLOCKING, т.к. .env/окружение может переопределить.

---

## 2. Конвенции и читаемость кода

### Соблюдено (Python/проект)

- **Именование переменных, функций, классов — консистентно.**  
  - Classes: `Settings`, `AppError`, `PhotoService`, `PhotoRepository`, `ObjectStorage` — PascalCase.  
  - Functions/methods: `setup_logging`, `get_settings`, `ensure_bucket`, `save_file` — snake_case.  
  - Constants: `SERVICE_NAME` — UPPER_SNAKE_CASE.

- **Type hints везде, где нужны.**  
  - `config.py` — полные типы для всех полей Settings.  
  - `session.py` — `AsyncGenerator[AsyncSession, None]`, return types в engine/session functions.  
  - `storage.py` — параметры и возвращаемые значения типизированы (даже if нет полного аннотирования SDK).  
  - `logging.py` — ContextVar[str], filter(record: LogRecord) -> bool.

- **Отсутствие мёртвого кода.**  
  Проверено: нет закомментированных блоков, нет неиспользуемых импортов. Каждый класс, функция и модуль имеют назначение.

- **Отсутствие дублирования.**  
  - `get_settings()` — единственная point of entry для конфига (lru_cache).  
  - `ObjectStorage` — единственный класс, взаимодействующий с MinIO.  
  - Exception handlers — единая регистрация в `register_exception_handlers`.

- **Docstrings соблюдены.**  
  Каждый модуль, класс, функция содержит docstring, который объясняет назначение и граничные условия:  
  - `config.py:1–8` — назначение модуля + примечание о секретах.  
  - `storage.py:1–13` — назначение, замечание о sync SDK и TODO о wrapping.  
  - `logging.py:1–14` — назначение, объяснение contextvar, security note.

- **Ruff-чистота (по читаемости).**  
  30_impl.md §5 указывает: "ruff check — All checks passed!" Это означает отсутствие:  
  - неиспользуемых импортов  
  - line-length нарушений (100 символов, как в pyproject.toml:35)  
  - неправильного форматирования  
  - нарушений PEP8.

### NON_BLOCKING замечания

- **Многострочные импорты в storage.py мог бы быть форматирован более читаемо.**  
  `storage.py:16–20` — импорты OK, но можно было бы группировать stdlib/third-party/local; сейчас это смешано. Это косметика, ruff не жалуется, никакого воздействия на функциональность.

---

## 3. Наблюдаемость (логирование, метрики, health)

### Логирование

- **JSON структурированный формат.**  
  `logging.py:54–56` настраивает JsonFormatter с полями:  
  ```python
  fmt="%(asctime)s %(levelname)s %(name)s %(service)s %(trace_id)s %(message)s"
  rename_fields={"asctime": "timestamp", "levelname": "level"}
  ```
  Итоговый JSON будет иметь: `timestamp`, `level` (вместо `levelname`), `name` (logger name), `service` (статическое `photo-service`), `trace_id`, `message`. Соответствует constitution §3.1.

- **trace_id как contextvar — заложена.**  
  `logging.py:28` определяет `trace_id_var: ContextVar[str]` с дефолтом `"-"`. Фильтр `TraceIdFilter:34–37` впрыскивает значение в каждый лог-рекорд.  
  **Примечание:** Сейчас trace_id всегда `"-"` (никакой middleware, который его генерирует или передаёт из заголовков, ещё не написана). Это OK для bootstrap — TODO на TASK-001 явно обозначена.

- **Логирование в stdout (не файлы).**  
  `logging.py:53` создаёт `StreamHandler(sys.stdout)` — для контейнеризации это правильно (логи собирает Docker/K8s).

- **Уровень из LOG_LEVEL env.**  
  `logging.py:48` читает `get_settings().LOG_LEVEL` и вызывает `root_logger.setLevel(level.upper())`. В compose это передаётся как env var; в .env можно переопределить.

- **Логирование ошибок с error_code.**  
  `errors.py:72–77` логирует при обработке `AppError`:  
  ```python
  logger.error(
      "request failed with %s: %s",
      exc.error_code,
      exc.message,
      extra={"error_code": exc.error_code},
  )
  ```
  Это отличная практика — error_code попадает и в структурированное поле, и в сообщение.

### Health checks

- **`GET /healthz` — liveness без зависимостей.**  
  `main.py:57–60` возвращает `200 {"status":"ok","service":"photo-service"}` без каких-либо операций ввода-вывода. Верно.

- **`GET /readyz` — readiness (упрощённо для bootstrap).**  
  `main.py:63–91` проверяет доступность БД (`SELECT 1`) и MinIO (`ensure_bucket()`). При ошибке возвращает `503` но процесс не падает. Это мягкий подход, допустимый для bootstrap; дизайн (20_design.md) это предусматривает.

### NON_BLOCKING замечания

- **Метрики Prometheus не реализованы.**  
  Вне скоупа bootstrap (spec и constitution это отмечают как будущее). Код к этому подготовлен (нет никаких помех добавить middleware позже). OK.

- **Трассировка Jaeger не реализована.**  
  Также вне скоупа. trace_id_var заложена — достаточно для bootstrap.

---

## 4. Покрытие тестами

### Текущее состояние

- **Smoke-тест /healthz.**  
  `tests/test_healthz.py` проверяет:  
  ```python
  response = await client.get("/healthz")
  assert response.status_code == 200
  assert body["status"] == "ok"
  assert body["service"] == "photo-service"
  ```
  Это минимум для bootstrap, покрывает основную вертикаль (ASGI app может быть импортирован и отвечает на запрос).

- **Заготовка conftest.py.**  
  `tests/conftest.py` предоставляет `client` fixture с httpx ASGITransport. Структура расширяемая для будущих интеграционных тестов БД/MinIO.

### Дыры на будущее (НЕ BLOCKING для bootstrap)

- Нет тестов на:  
  - `config.py` (Settings инициализируется, дефолты работают)  
  - `logging.py` (JSON formatter, trace_id injection)  
  - `db/session.py` (engine creates, SessionLocal works)  
  - `integrations/storage.py` (ensure_bucket, save_file/get_file stub'ы)  
  - `errors.py` exception handlers  
  - `main.py` lifespan (startup/shutdown)  
  
  Все эти покрываются на этапе test-writer (TASK-001+). Для bootstrap smoke-тест достаточен.

- **Coverage tool не упоминается в pyproject.toml.**  
  `pytest.ini_options` не содержит `--cov`. Это opt-in; добавляется позже при необходимости.

---

## 5. Гигиена зависимостей и конфигурации

### pyproject.toml

- **Runtime deps:**  
  - fastapi>=0.111 ✓  
  - uvicorn[standard]>=0.30 ✓  
  - pydantic>=2.7 ✓ (v2 как требуется)  
  - pydantic-settings>=2.3 ✓  
  - sqlalchemy[asyncio]>=2.0 ✓ (async mode)  
  - asyncpg>=0.29 ✓ (PostgreSQL async driver)  
  - alembic>=1.13 ✓  
  - minio>=7.2 ✓  
  - python-json-logger>=2.0 ✓ (JSON logging)  
  - anyio>=4.3 ✓ (для будущих thread wrapping)  

- **Dev deps:**  
  - pytest>=8.2, pytest-asyncio>=0.23, httpx>=0.27, ruff>=0.5 — адекватный минимум.

- **Python версия:**  
  `requires-python = ">=3.12"` ✓ Соответствует составу (constitution/spec).

- **Build backend:**  
  `hatchling` — стандартный, простой. OK.

### Dockerfile

- **Base image:**  
  `FROM python:3.12-slim` ✓ — lightweight, свежая Python версия.

- **Env переменные:**  
  ```dockerfile
  ENV PYTHONUNBUFFERED=1 \
      PYTHONDONTWRITEBYTECODE=1 \
      UV_LINK_MODE=copy
  ```
  Стандартные для контейнеризации Python.

- **uv installation:**  
  `COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv` — скопировать статический бинарь. Чистый подход, без build-time dependencies.

- **Слои:**  
  - `pyproject.toml` копируется отдельно → кэшируется.  
  - `uv sync --no-dev` (без dev deps в контейнере) ✓.  
  - `app/`, `alembic.ini`, `migrations/` копируются после ✓ (кэш-friendly).

- **CMD:**  
  `uv run uvicorn app.main:app --host 0.0.0.0 --port 8000` ✓.

- **NON_BLOCKING:** Dockerfile не использует non-root user. Это nice-to-have (защита от случайных операций в контейнере), но не обязательно для bootstrap. Можно добавить `USER app` в будущем.

### docker-compose.yml

- **postgres service:**  
  - image postgres:16 ✓  
  - Env vars для инициализации БД ✓  
  - healthcheck `pg_isready` ✓  
  - volume `pgdata` ✓  

- **minio service:**  
  - image minio/minio ✓  
  - command с server + console-address ✓  
  - healthcheck `curl .../minio/health/live` ✓  
  - volume miniodata ✓  

- **api service:**  
  - build . ✓  
  - env_file `.env` (required: false) — хорошо задокументировано (compose-friendly defaults).  
  - environment с явными переменными (дублирование для clarity) ✓  
  - depends_on с service_healthy condition ✓  
  - command: `alembic upgrade head && uvicorn` — миграции **перед** стартом сервера ✓.

### .env.example

- Содержит все переменные, используемые Settings.  
- Значения — плейсхолдеры/дефолты для compose.  
- **Хороший пример для клонирования в `.env`.**

### alembic.ini и migrations/env.py

- **alembic.ini:**  
  `script_location = migrations` — стандартная конфигурация ✓.  
  `sqlalchemy.url` НЕ хардкодирован (переопределяется в env.py из Settings) ✓.

- **env.py:**  
  - Async engine: `async_engine_from_config` + `run_async_migrations` ✓.  
  - `target_metadata = Base.metadata` из app.db.models ✓.  
  - URL из `get_settings().DATABASE_URL` ✓.  
  - Offline + online режимы ✓.

---

## 6. BLOCKING находки

**Нет.**

Все критические требования по безопасности, конвенциям и наблюдаемости из constitution (§3.1–3.3) и spec соблюдены или правильно отложены на TASK-001+.

---

## 7. NON_BLOCKING находки

### Улучшения (рекомендации, не обязательные для bootstrap)

1. **docker-compose.yml — добавить комментарий о локальных dev-кредах.**  
   Строка 43–46 содержит DATABASE_URL и MinIO-креды в открытом виде. Рекомендуется добавить комментарий:  
   ```yaml
   # NOTE: these are local development defaults only; prod must use secrets/vault
   ```

2. **Dockerfile — добавить non-root user (nice-to-have).**  
   Для дополнительной безопасности:  
   ```dockerfile
   RUN useradd -m app
   USER app
   ```
   Это не обязательно для bootstrap, но хорошая практика.

3. **README.md — уточнить, что .env local-only.**  
   Раздел "Переменные окружения" может добавить: "В prod используйте secret manager / vault, не .env файлы."

4. **storage.py — заглушка для sync SDK wrapping подробнее.**  
   Комментарий `TODO(TASK-001)` верен, но можно добавить пример:  
   ```python
   # Example (TASK-001):
   # from anyio import to_thread
   # return await to_thread.run_sync(self._client.put_object, ...)
   ```
   Это помощь для следующего кодера, но не критично.

5. **trace_id middleware — явный путь в комментариях.**  
   `logging.py:8–9` указывает TODO, но можно быть конкретнее о точке расширения:  
   ```python
   # In TASK-001, add a middleware to app.py:
   # @app.middleware("http")
   # async def inject_trace_id(request, call_next):
   #     trace_id = request.headers.get("X-Trace-ID", str(uuid4()))
   #     token = trace_id_var.set(trace_id)
   #     try:
   #         return await call_next(request)
   #     finally:
   #         trace_id_var.reset(token)
   ```
   Это поможет test-writer и следующему кодеру.

---

## 8. Проверка по скоупу TASK-000

Все критерии из 20_design.md выполнены:

- [x] Слои + правила зависимостей (api → services → repositories → db; core/integrations/schemas — поперечные).  
- [x] `core/config.py` — Settings + get_settings().  
- [x] `core/logging.py` — JSON + trace_id_var + TraceIdFilter.  
- [x] `core/errors.py` — иерархия + register_exception_handlers.  
- [x] `db/session.py` — engine + SessionLocal + get_session.  
- [x] `db/models.py` — Photo модель + PhotoStatus enum + индекс.  
- [x] `integrations/storage.py` — ObjectStorage с ensure_bucket/save_file/get_file.  
- [x] `integrations/analyzer_client.py` — пустая заглушка.  
- [x] `api/photos.py` — пустой роутер (без фиктивных контрактов).  
- [x] `schemas/photos.py` — HealthResponse, ErrorResponse.  
- [x] `services/photo_service.py` — заготовка с NotImplementedError.  
- [x] `repositories/photo_repository.py` — заготовка с NotImplementedError.  
- [x] `main.py` — FastAPI app + lifespan + /healthz + /readyz + exception handlers.  
- [x] Alembic (alembic.ini + env.py async + v001_init_photos.py).  
- [x] Dockerfile (python:3.12-slim + uv sync + slayer).  
- [x] docker-compose.yml (api + postgres + minio, healthcheck, depends_on, volumes).  
- [x] .env.example + .gitignore.  
- [x] pyproject.toml + dependencies.  
- [x] README.md.  
- [x] tests/conftest.py + test_healthz.py (smoke-тест).  

Дизайн скоуп соблюдён **полностью**.

---

## 9. Выводы

Каркас **production-grade** для bootstrap фазы. Код:
- **Безопасен:** нет утечек секретов, конфиг из env, JSON логирование без sensitive data.  
- **Чистый:** konvencje, type hints, docstrings, рuff-clean.  
- **Наблюдаемый:** JSON структурированный логи, trace_id заложен, health/readiness endpoints.  
- **Тестируемый:** ASGI client fixture, smoke-тест работает.  
- **Масштабируемый:** слои отделены, зависимости направлены, готово для TASK-001+ расширения.

Ни одно из замечаний не блокирует merge. NON_BLOCKING рекомендации — quality-of-life улучшения на будущее.

---

**Ревьювер:** reviewer-2 (haiku-4.5)  
**Статус:** ✅ APPROVE  
**Блокирующих замечаний:** 0  
**Рекомендуемых улучшений:** 5 (все NON_BLOCKING)

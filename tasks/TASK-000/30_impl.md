---
task_id: TASK-000
agent: coder
model: sonnet
inputs:
  - specs/constitution.md
  - specs/feature-upload/spec.md
  - tasks/TASK-000/10_context.md
  - tasks/TASK-000/20_design.md
  - tasks/TASK-000/40_review-1.md
outputs:
  - photo-service/ (весь каркас)
  - tasks/TASK-000/30_impl.md
status: done
timestamp: 2026-07-11T01:00:00Z
---

# TASK-000 — Реализация каркаса photo-service (bootstrap)

## 1. Что сделано (по шагам плана из 20_design.md §7)

1. Скелет каталогов `photo-service/app/{api,schemas,services,repositories,db,integrations,core}` +
   `photo-service/tests/`, `photo-service/migrations/versions/` — все пакеты с `__init__.py`.
2. `pyproject.toml` — проект под uv, `requires-python = ">=3.12"`, runtime-зависимости
   (fastapi, uvicorn[standard], pydantic>=2, pydantic-settings, sqlalchemy[asyncio]>=2, asyncpg,
   alembic, minio, python-json-logger, anyio) + dev-группа (pytest, pytest-asyncio, httpx, ruff).
3. `.gitignore` (.env, __pycache__, .venv, кэши, uv.lock) и `.env.example` со всеми переменными.
4. `app/core/config.py` — `Settings` (pydantic-settings) + `get_settings()` (lru_cache), дефолты
   под docker-compose (`postgres`/`minio` хосты), локальный дефолт MinIO-секретов с комментарием
   "prod — только из env".
5. `app/core/logging.py` — `setup_logging()` (JSON stdout, python-json-logger), `trace_id_var`
   (contextvar) + `TraceIdFilter`, статическое поле `service`. TODO на HTTP middleware — TASK-001.
6. `app/core/errors.py` — `AppError` + `StorageUnavailable(503)`, `NotFoundError(404)`,
   `ValidationError(400)`, `ConflictError(409)`; `register_exception_handlers(app)` формирует
   `{error_code, message, trace_id}`.
7. `app/db/models.py` — `Base(DeclarativeBase)`, `PhotoStatus` enum, модель `Photo` (photo_id,
   user_id, s3_path, uploaded_at, status) + индекс `ix_photos_user_uploaded`.
8. `app/db/session.py` — async `engine`, `SessionLocal` (`async_sessionmaker`), `get_session()`.
9. `app/schemas/photos.py` — `HealthResponse`, `ErrorResponse`; TODO-заглушка на upload-DTO.
10. `app/integrations/storage.py` — `ObjectStorage` (`__init__`, `ensure_bucket`, `save_file`,
    `get_file`), маппинг `S3Error` → `StorageUnavailable`/`NotFoundError`.
11. `app/integrations/analyzer_client.py` — пустая заглушка (только docstring/TODO), нигде не
    импортируется.
12. `app/services/photo_service.py` — `PhotoService(repository, storage)`, `upload_photo(...)` →
    `NotImplementedError` + TODO(TASK-001).
13. `app/repositories/photo_repository.py` — `PhotoRepository.create/get(session, ...)` →
    `NotImplementedError` + TODO(TASK-001).
14. `app/api/photos.py` — пустой `APIRouter(prefix="/api/v1/photos")`, без маршрутов, с
    docstring-правилом для ревьюеров (не импортировать SQLAlchemy/MinIO напрямую).
15. `app/main.py` — `setup_logging`, `lifespan` (ensure_bucket + `SELECT 1` с try/except-warning,
    `engine.dispose()` на shutdown), `FastAPI(lifespan=...)`, `register_exception_handlers`,
    `include_router(photos.router)`, `GET /healthz` (200 без внешних вызовов), `GET /readyz`
    (проверяет БД+MinIO, 503 при недоступности).
16. Alembic: `alembic.ini` (без хардкода URL), `migrations/env.py` (async engine,
    `target_metadata = Base.metadata`, URL из `Settings`), `migrations/script.py.mako`,
    первая ревизия `migrations/versions/v001_init_photos.py` написана вручную (детерминированно,
    как отмечено риском в дизайне §6) — создаёт enum `photo_status`, таблицу `photos`, индекс.
17. `Dockerfile` — `python:3.12-slim`, установка `uv` копированием бинаря из
    `ghcr.io/astral-sh/uv`, `uv sync --no-dev` отдельным слоем, копирование `app/`+`alembic.ini`+
    `migrations/`, `CMD uv run uvicorn app.main:app ...`.
18. `docker-compose.yml` — сервисы `postgres`(16, healthcheck `pg_isready`), `minio`
    (healthcheck `curl .../health/live`), `api` (build ., `depends_on: service_healthy`,
    команда `alembic upgrade head && uvicorn ...`); volumes `pgdata`/`miniodata`; порты
    5432/9000/9001/8000.
19. `README.md` — как запустить (`docker compose up --build`), как проверить `/healthz`/`/readyz`,
    переменные окружения, структура слоёв и правило зависимостей.
20. Тестовый минимум: `tests/conftest.py` (fixture `client` на httpx `ASGITransport`),
    `tests/test_healthz.py` (`GET /healthz` == 200, проверка тела).

## 2. Изменённые/новые файлы

Новое дерево `photo-service/` целиком (35 файлов):
```
photo-service/.env.example
photo-service/.gitignore
photo-service/Dockerfile
photo-service/README.md
photo-service/alembic.ini
photo-service/docker-compose.yml
photo-service/pyproject.toml
photo-service/app/__init__.py
photo-service/app/main.py
photo-service/app/api/__init__.py
photo-service/app/api/photos.py
photo-service/app/core/__init__.py
photo-service/app/core/config.py
photo-service/app/core/errors.py
photo-service/app/core/logging.py
photo-service/app/db/__init__.py
photo-service/app/db/models.py
photo-service/app/db/session.py
photo-service/app/integrations/__init__.py
photo-service/app/integrations/analyzer_client.py
photo-service/app/integrations/storage.py
photo-service/app/repositories/__init__.py
photo-service/app/repositories/photo_repository.py
photo-service/app/schemas/__init__.py
photo-service/app/schemas/photos.py
photo-service/app/services/__init__.py
photo-service/app/services/photo_service.py
photo-service/migrations/env.py
photo-service/migrations/script.py.mako
photo-service/migrations/versions/v001_init_photos.py
photo-service/tests/__init__.py
photo-service/tests/conftest.py
photo-service/tests/test_healthz.py
```
Корневые файлы репозитория (main.py в корне, CLAUDE.md и т.д.) не тронуты.

## 3. Принятые мелкие решения (в рамках дизайна, без выхода за скоуп)

- `uv.lock` добавлен в `.gitignore` — Dockerfile использует `uv sync --no-dev` без `--frozen`
  (в дизайне это оставлено на усмотрение кодера, §5.1: "или без `--frozen`, если lock не коммитим").
- В `docker-compose.yml` сделал `env_file: .env` **необязательным**
  (`env_file: - path: .env / required: false`), чтобы `docker compose up` работал сразу после
  клонирования без ручного создания `.env` (Settings и так содержат compose-совместимые дефолты);
  явно продублировал ключевые переменные в `environment:` для наглядности. Это не противоречит
  дизайну (§5.2 допускал оба варианта: `env_file` ИЛИ `environment` с хостами).
- `migrations/versions/v001_init_photos.py` написан вручную (не через autogenerate) — так и
  рекомендовано дизайном как более детерминированный вариант из-за риска с enum в Postgres (§6).
  Проверено генерацией offline SQL (`alembic upgrade head --sql`) — корректно создаёт enum,
  таблицу `photos`, индекс `ix_photos_user_uploaded`.
- В `core/logging.py` формат `fmt` использует `%(levelname)s` (не `%(level)s`, которого не
  существует в LogRecord), а `rename_fields` переименовывает `levelname` → `level` в итоговом JSON
  — иначе поле `level` уходило как `null`. Проверено вручную.
- `readyz` реализован по дизайну как "мягкий": ошибки БД/MinIO не роняют процесс, просто
  возвращают 503; при этом `readyz` дергает `ensure_bucket()` повторно как индикатор
  доступности MinIO (идемпотентно).
- Заглушки `services/photo_service.py` и `repositories/photo_repository.py` реализованы как
  `raise NotImplementedError` внутри реальных `async def`-методов (не `...`), чтобы сигнатуры были
  вызываемы и типобезопасны, но явно падали при попытке использования — соответствует духу
  дизайна ("сигнатуры на будущее... `.../NotImplementedError`").
- `analyzer_client.py` не содержит вообще никакого кода, только docstring — строго по дизайну
  §3.7 ("без реализации и без импорта в остальной код"); подтверждено grep — нигде не
  импортируется.

## TODO, зафиксированные в коде (не реализуются в TASK-000)

- `core/logging.py`: HTTP middleware, проставляющий `trace_id_var` из запроса — TASK-001.
- `integrations/storage.py`: обёртка блокирующих вызовов SDK в `anyio.to_thread.run_sync` при
  вызове из async-хендлеров — TASK-001 (пока используется только на старте в lifespan).
- `services/photo_service.py`, `repositories/photo_repository.py`: реальная логика — TASK-001.
- `integrations/analyzer_client.py`: Kafka producer — TASK-002.
- `schemas/photos.py`: DTO загрузки (`PhotoUploadResponse` и т.п.) — TASK-001.

Отклонений от дизайна, требующих решения архитектора, не было.

## 4. Как проверить локально

Без Docker (сделано в рамках самопроверки — venv создавался временно и удалён после проверки,
в репозиторий не коммитился):
```bash
cd photo-service
python -m venv .venv && source .venv/Scripts/activate  # или .venv/bin/activate на linux/mac
pip install -e ".[dev]"          # либо uv sync, если uv установлен
pytest tests/ -v                  # 1 passed: test_healthz_returns_200
ruff check app tests migrations   # All checks passed!
python -m alembic upgrade head --sql   # печатает корректный SQL (CREATE TYPE/TABLE/INDEX)
```

С Docker (не выполнялось в этой среде — Docker недоступен в песочнице кодера; см. §5 ниже):
```bash
cd photo-service
docker compose up --build
curl -i http://localhost:8000/healthz     # ожидаем 200 {"status":"ok","service":"photo-service"}
curl -i http://localhost:8000/readyz      # 200 если БД+MinIO доступны
# MinIO console: http://localhost:9001 (minioadmin/minioadmin), бакет "photos"
# docker compose exec api uv run alembic current   -> должен показать head-ревизию v001
```

## 5. Статус проверки в этой среде

- Python-синтаксис: `python -m py_compile` по всем `.py` файлам — OK, ошибок нет.
- Импорты: все модули (`core.*`, `db.*`, `schemas.*`, `integrations.*`, `services.*`,
  `repositories.*`, `api.*`, `main`) успешно импортируются во временном venv с реально
  установленными зависимостями (fastapi, sqlalchemy[asyncio], asyncpg, minio, alembic и т.д.).
  Реального подключения к Postgres/MinIO для этого не требовалось (engine/Minio-клиент создаются
  лениво).
- `GET /healthz` через `httpx.ASGITransport` (без поднятого сервера) — возвращает `200
  {"status":"ok","service":"photo-service"}`; проверено также через `pytest` (1 passed).
- JSON-логирование — проверено вручную, structured JSON в stdout с полями
  `timestamp/level/name/service/trace_id/message`.
- `ruff check` — "All checks passed!" без замечаний.
- Alembic offline SQL-генерация (`alembic upgrade head --sql`) — корректно генерирует
  `CREATE TYPE photo_status`, `CREATE TABLE photos`, `CREATE INDEX ix_photos_user_uploaded`.
- `docker compose up` / реальный Postgres/MinIO — **НЕ проверено**: Docker недоступен в этой
  песочнице кодера (`docker: command not found`). Это ожидаемо и допустимо по заданию
  ("Docker может быть недоступен — если запустить нельзя, не падай"). Компоуз-конфигурация
  визуально сверена с чек-листом дизайна (healthcheck'и, volumes, depends_on: service_healthy,
  порты 8000/9000/9001/5432, команда старта `alembic upgrade head && uvicorn`). Финальная
  проверка `docker compose up --build` + `curl /healthz` остаётся на этап
  test-writer/пользователя с доступным Docker.
- Временный `.venv_check` и `.ruff_cache`/`__pycache__`, созданные при самопроверке, удалены —
  в дереве `photo-service/` остались только исходники (см. итоговый листинг файлов в разделе 2).

## 6. Объяснение назначения каждого слоя (для защиты архитектуры на ревью)

**`api/`** — HTTP-роутеры (FastAPI `APIRouter`). Отвечает за парсинг запроса, Pydantic-валидацию
входа/выхода и HTTP-коды ответов. Не содержит SQL и не ходит в MinIO напрямую — только вызывает
`services/`. Разделение нужно, чтобы бизнес-логику можно было тестировать и переиспользовать без
HTTP-контекста (например, из будущего Kafka-консьюмера), а транспортный слой оставался тонким и
легко заменяемым (REST → gRPC без переписывания логики).

**`services/`** — бизнес-логика домена (`PhotoService`). Оркестрирует `repositories/` (БД) и
`integrations/` (MinIO, будущий Kafka), реализует правила («файл валиден → сохранить в MinIO →
записать в БД → опубликовать событие»). Не знает о FastAPI (`Request`/`Depends`) — получает уже
готовые аргументы и `AsyncSession`, что делает слой независимым от транспорта и легко юнит-тестируемым.

**`repositories/`** — доступ к БД (`PhotoRepository`). Инкапсулирует SQL/ORM-запросы к таблице
`photos`, получает `AsyncSession` аргументом (не создаёт сессию сама и не коммитит глобально —
границу транзакции держит вызывающий слой). Разделение с `services/` позволяет менять хранилище
метаданных (например, добавить кэш или сменить ORM-запросы) не трогая бизнес-правила.

**`db/`** — инфраструктура доступа к PostgreSQL: `models.py` (`DeclarativeBase`, модель `Photo`,
enum статуса, индекс), `session.py` (async engine, `async_sessionmaker`, `get_session`
FastAPI-зависимость). Это единственное место, где определена схема БД и создаётся
соединение/пул — контроль количества соединений (`pool_size`) и жизненного цикла engine
(dispose в lifespan) централизован здесь.

**`integrations/`** — обёртки над внешними системами: `storage.py` (MinIO — единственное место,
которое реально кладёт/читает байты фото; в БД хранится только `s3_path`, сами фото никогда не
попадают в PostgreSQL) и `analyzer_client.py` (пустая заглушка под будущий Kafka-producer к
Analyzer Service). Изоляция внешних SDK в одном слое даёт единую точку маппинга ошибок
(`S3Error` → `StorageUnavailable`/`NotFoundError`) и упрощает мокирование в тестах.

**`core/`** — сквозные (cross-cutting) модули, которые нужны всем слоям, но сами ни от кого не
зависят: `config.py` (Settings из env — единая точка конфигурации), `errors.py` (доменные
исключения + единый формат JSON-ошибки `{error_code, message, trace_id}` + маппинг на HTTP),
`logging.py` (JSON-логирование + contextvar под trace_id). Вынесены отдельно, чтобы не создавать
циклических зависимостей — `core` не импортирует `api`/`services`/`repositories`.

**`schemas/`** — Pydantic DTO для входа/выхода HTTP API (`HealthResponse`, `ErrorResponse`, в
будущем `PhotoUploadResponse`). Отделены от `db/models.py` (SQLAlchemy ORM-моделей) намеренно:
контракт API (что видит клиент) и схема БД (как хранятся данные) должны эволюционировать
независимо — иначе изменение колонки в БД сломало бы HTTP-контракт и наоборот.

**`migrations/`** — Alembic-миграции схемы PostgreSQL (async `env.py`, версии в `versions/`).
Единственный официальный способ менять схему БД; `target_metadata = Base.metadata` берётся из
`db/models.py`, так что миграции всегда синхронизированы с ORM-моделями и применяются
последовательно (v001, v002, ...) — как требует constitution §9.

**`tests/`** — модульные/интеграционные тесты (httpx ASGI-клиент против приложения без реального
сервера). В TASK-000 — только smoke-тест `/healthz`; структура (`conftest.py` с фикстурами)
заложена так, чтобы test-writer мог добавить fixtures для БД/MinIO без изменения структуры.

**Правило зависимостей**, которое соблюдено во всём коде: `api → services → repositories → db`
(строго сверху вниз, без обратных импортов); `core/`, `integrations/`, `schemas/` — поперечные,
доступны любому слою, но сами не импортируют `api/services/repositories` (проверено вручную по
всем файлам — нет циклов). Фото как бинарные данные существуют только в MinIO
(`integrations/storage.py`); в PostgreSQL (`db/models.py::Photo`) хранится исключительно
метаданные, включая `s3_path` — ссылку на объект в MinIO.

## Итерация 2 — фикс B1 (reviewer-1, BLOCKING)

**Замечание:** `photo-service/Dockerfile` выполнял `RUN uv sync --no-dev` до `COPY app/ ./app/`.
Так как `pyproject.toml` объявляет `[build-system] hatchling` с
`[tool.hatch.build.targets.wheel] packages = ["app"]`, а `uv sync` по умолчанию пытается собрать
и установить сам корневой проект как пакет — на этом слое каталога `app/` в образе ещё не было,
и сборка колеса/editable-инсталла падала бы. Это блокировало основной критерий готовности
TASK-000 (`docker compose up --build`).

**Выбранный фикс — вариант 1 из рекомендации ревьювера** (`--no-install-project` на слое
зависимостей + отдельный `uv sync --no-dev` после копирования кода). Обоснование выбора в одну
строку: сохраняет кэширование слоя зависимостей (самый частый и самый долгий шаг) отдельно от
кода, при этом гарантированно устанавливает сам проект уже после появления `app/` на диске —
в отличие от варианта 2 (`[tool.uv] package = false`), не меняет модель поставки проекта
(`uv run` продолжает работать как editable-инсталл), и в отличие от варианта 3 (перенос COPY
перед uv sync) не жертвует Docker-кэшем зависимостей при каждом изменении кода.

Итоговый `photo-service/Dockerfile`:
```dockerfile
# Dependency layer first, cached separately from the application code.
# --no-install-project: only resolve/install third-party deps here, since
# the `app/` package (required by hatchling's packages=["app"]) does not
# exist on disk yet - installing the project itself happens below, after
# the code is copied in.
COPY pyproject.toml ./
RUN uv sync --no-dev --no-install-project

# Application code.
COPY app/ ./app/
COPY alembic.ini ./
COPY migrations/ ./migrations/

# Now that app/ exists, install the project itself (fast: deps already cached).
RUN uv sync --no-dev
```

**Проверка (без реального Docker, песочница по-прежнему без него):** эмулировал точный порядок
Docker-слоёв вручную во временной scratch-директории с помощью `pip install uv` (получил рабочий
`uv` 0.11.28 локально) и копией `pyproject.toml`/`app/`/`alembic.ini`/`migrations/` в том же
порядке, в котором их видит `Dockerfile`:

1. Только `pyproject.toml` в каталоге → `uv sync --no-dev --no-install-project` — установил
   38 сторонних пакетов, **не** пытался собрать `photo-service`/`app` (подтверждено: команда
   завершилась успешно без каталога `app/` на диске — именно то, что раньше падало).
2. Скопировал `app/`, `alembic.ini`, `migrations/` (как это делает `Dockerfile` строками
   `COPY app/ ./app/` и т.д.).
3. `uv sync --no-dev` (финальный, без `--no-install-project`) → hatchling собрал и установил
   `photo-service==0.1.0` пакетом (`Building photo-service ... Built photo-service ... Installed
   1 package`) — подтверждает, что сборка проекта проходит именно тогда, когда `app/` уже
   присутствует.
4. `uv run python -c "import app.main; print(app.main.app.title)"` в этом окружении → вывод
   `app.main import OK via uv run: photo-service` — подтверждает, что итоговая команда контейнера
   (`CMD uv run uvicorn app.main:app ...`) сможет импортировать приложение после сборки.

(Локально доступен только Python 3.10, а `pyproject.toml` требует `>=3.12` — для этого теста
использовалась временная копия `pyproject.toml` с `requires-python = ">=3.10"` только чтобы обойти
интерпретатор-констрейнт при разрешении зависимостей; сам оригинальный `pyproject.toml` в
`photo-service/` **не менялся** и по-прежнему требует `>=3.12`, что удовлетворяется базовым
образом `python:3.12-slim` в реальной сборке.) Временная scratch-директория удалена после проверки.

Дополнительно перепрогнан `python -m py_compile` по всем `.py`-файлам `photo-service/` — OK
(Dockerfile — единственный изменённый файл в этой итерации, Python-код не трогался, поэтому
результаты `pytest`/`ruff` из итерации 1 — 1 passed / All checks passed! — остаются в силе).

Реальный `docker compose up --build` + `curl http://localhost:8000/healthz` == 200
**по-прежнему не выполнялся физически** в этой песочнице (Docker недоступен: `docker: command not
found`) — остаётся ручным пунктом финальной проверки на машине с Docker, как и было отмечено в
итерации 1. Эмуляция порядка Docker-слоёв через `uv`/pip выше — лучшее статическое подтверждение,
доступное в этой среде, и напрямую закрывает механизм сбоя, описанный в B1.

Non-blocking замечания (N1–N7) из `40_review-1.md` сознательно НЕ трогались — вне скоупа этой
итерации, оставлены на TASK-001 по указанию оркестратора.

## Открытые вопросы для ревью

1. `docker compose up --build` и реальный прогон против Postgres/MinIO не выполнялся в этой
   среде (нет Docker) — рекомендую проверить на машине ревьювера/пользователя перед тем, как
   считать критерий "рабочий каркас" полностью подтверждённым.
2. `env_file: .env` сделан необязательным в compose (`required: false`) — если ревьюверы считают,
   что `.env` должен быть обязательным для api-контейнера, это можно откатить одной строкой;
   решение принято, т.к. это упрощает "из коробки" `docker compose up` без предварительных шагов
   и не противоречит дизайну (§5.2 допускал оба варианта).
3. `uv.lock` не коммитится (в `.gitignore`) — если для воспроизводимости сборки понадобится
   зафиксированный lock-файл, потребуется отдельное решение (сгенерировать и закоммитить,
   переключить Dockerfile на `--frozen`).

## Чек-лист самопроверки по критериям приёмки (spec.md применим частично — TASK-000 вне скоупа
основных MUST-критериев upload; ниже — критерии, применимые к каркасу)

- [x] `docker compose up` поднимает `api`+`postgres`+`minio` — конфигурация написана по чек-листу
      дизайна; B1 (порядок COPY vs `uv sync` в Dockerfile) исправлен и статически проверен
      эмуляцией слоёв через `uv` (см. «Итерация 2 — фикс B1»); **физический прогон
      `docker compose up --build`** по-прежнему не выполнялся в этой среде (Docker недоступен), см. §5.
- [x] `GET /healthz` отвечает `200` без обращения к БД/MinIO — проверено через ASGI-клиент и pytest.
- [x] Нет эндпоинтов загрузки/бизнес-логики — `api/photos.py` не содержит маршрутов.
- [x] `services/`/`repositories/` — тонкие заготовки (`NotImplementedError` + TODO).
- [x] `integrations/analyzer_client.py` — пустая заглушка, нигде не импортируется.
- [x] Kafka/worker/analyzer/JWT/Prometheus не реализованы — подтверждено (нет соответствующего кода).
- [x] `core/config.py` — Settings со всеми требуемыми переменными, локальные дефолты под compose.
- [x] `db/session.py` — async engine + sessionmaker + `get_session`.
- [x] `db/models.py` — `Photo` модель, непустая initial-ревизия Alembic.
- [x] `integrations/storage.py` — `save_file`/`get_file`/`ensure_bucket`, маппинг ошибок.
- [x] `core/errors.py` — доменные исключения + формат `{error_code, message, trace_id}` +
      маппинг (`StorageUnavailable→503` и др.).
- [x] `core/logging.py` — JSON-логирование, уровень из `LOG_LEVEL`, contextvar под trace_id.
- [x] `main.py` — `GET /healthz` без внешних зависимостей, lifespan (ensure-bucket, dispose).
- [x] `Dockerfile` — python:3.12-slim + uv + `uv sync` + uvicorn.
- [x] `docker-compose.yml` — healthcheck'и, volumes, порты, `depends_on`.
- [x] `alembic.ini` + `migrations/env.py` (async) + initial-ревизия.
- [x] `.env.example` со всеми переменными; `.env` в `.gitignore`.

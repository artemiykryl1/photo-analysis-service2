---
task_id: TASK-000
agent: test-writer
model: sonnet
inputs:
  - specs/constitution.md
  - specs/feature-upload/spec.md
  - tasks/TASK-000/20_design.md
  - tasks/TASK-000/30_impl.md
  - tasks/TASK-000/40_review-1.md
  - tasks/TASK-000/41_review-2.md
  - photo-service/ (code + existing tests/)
outputs:
  - photo-service/tests/conftest.py (unchanged, re-verified)
  - photo-service/tests/test_healthz.py (extended)
  - photo-service/tests/test_config.py (new)
  - photo-service/tests/test_errors.py (new)
  - photo-service/tests/test_storage.py (new)
  - photo-service/tests/test_stubs.py (new)
  - photo-service/tests/test_app_boot.py (new)
  - photo-service/tests/test_lifespan.py (new)
  - photo-service/tests/test_db_session.py (new)
  - photo-service/tests/test_logging.py (new)
  - tasks/TASK-000/50_tests.md
status: green
timestamp: 2026-07-11T02:00:00Z
---

# TASK-000 — Тесты каркаса photo-service (bootstrap)

## 0. Контекст и калибровка скоупа

TASK-000 — bootstrap-каркас, оба ревьювера дали `APPROVE`. Эндпоинтов загрузки нет,
`services/`/`repositories/` — заготовки с `NotImplementedError`, `integrations/analyzer_client.py` —
пустая заглушка, нигде не импортируется. Поэтому полноценные MUST-критерии
`specs/feature-upload/spec.md` §2 (валидация файла, сохранение в MinIO с реальным путём,
регистрация в БД, публикация в Kafka, coverage ≥85% по фиче upload) **не применимы** к этой таске —
они относятся к TASK-001/TASK-002 и не имеют кода для тестирования сейчас. Ниже — тесты на то, что
реально реализовано в каркасе: конфиг, маппинг ошибок, MinIO-обёртка (замокана), заготовки-стабы,
сборка приложения, `/healthz`, `/readyz`, lifespan, логирование, DB-сессия.

Ни один тест не поднимает реальный Postgres/MinIO/Docker — везде моки (`unittest.mock`) и ASGI-транспорт
`httpx.ASGITransport`.

---

## 1. Матрица: критерий приёмки → тест

Критерии — из `tasks/TASK-000/30_impl.md` §«Чек-лист самопроверки» (применимые к каркасу пункты
`spec.md`/`20_design.md`), т.к. полные MUST-критерии upload из `feature-upload/spec.md` §2 вне
скоупа TASK-000 (см. §0 выше).

| # | Критерий (каркас) | Тест(ы) | Файл |
|---|---|---|---|
| 1 | `GET /healthz` → 200, тело `{"status":"ok","service":"photo-service"}` | `test_healthz_returns_200`, `test_healthz_body_matches_schema_exactly`, `test_healthz_content_type_is_json` | `tests/test_healthz.py` |
| 2 | `/healthz` не обращается к БД/MinIO (liveness без внешних зависимостей) | `test_healthz_does_not_touch_db_or_storage` (DB/storage замоканы так, что падают при обращении — /healthz всё равно 200) | `tests/test_healthz.py` |
| 3 | `/healthz` — только GET (остальные методы не поддерживаются как liveness-контракт) | `test_healthz_rejects_other_http_methods[post/put/delete/patch]` | `tests/test_healthz.py` |
| 4 | Нет эндпоинтов загрузки/бизнес-логики в `api/photos.py` | `test_no_upload_routes_exist_yet`, `test_photos_router_is_mounted` | `tests/test_app_boot.py` |
| 5 | `services/photo_service.py` — тонкая заготовка (`NotImplementedError`) | `test_upload_photo_raises_not_implemented`, `test_upload_photo_raises_not_implemented_with_args`, `test_photo_service_stores_repository_and_storage` | `tests/test_stubs.py` |
| 6 | `repositories/photo_repository.py` — тонкая заготовка (`NotImplementedError`) | `test_create_raises_not_implemented`, `test_get_raises_not_implemented` | `tests/test_stubs.py` |
| 7 | `core/config.py`: Settings читает env (DATABASE_URL, MINIO_*, LOG_LEVEL, API_HOST/PORT) | `TestEnvOverrides::*` (5 тестов) | `tests/test_config.py` |
| 8 | `core/config.py`: дефолты compose-совместимые, работают без `.env` | `TestDefaults::test_defaults_are_compose_friendly` | `tests/test_config.py` |
| 9 | `core/config.py`: секретные поля не имеют "боевого" дефолта (только known-local `minioadmin`) | `TestDefaults::test_minio_secret_defaults_are_local_dev_placeholders_only` | `tests/test_config.py` |
| 10 | `core/config.py`: неизвестные env-переменные не ломают Settings (`extra="ignore"`) | `test_unknown_env_vars_are_ignored_not_rejected` | `tests/test_config.py` |
| 11 | `get_settings()` — кэшированный синглтон | `test_get_settings_returns_cached_singleton` | `tests/test_config.py` |
| 12 | `core/errors.py`: маппинг `StorageUnavailable→503`, `NotFoundError→404`, `ValidationError→400`, `ConflictError→409`, `AppError→500` | `TestStatusCodeMapping::test_exception_maps_to_expected_status_and_code[...]` (5 параметров) | `tests/test_errors.py` |
| 13 | `core/errors.py`: тело ошибки строго `{error_code, message, trace_id}` (spec §2) | `test_body_has_exactly_error_code_message_trace_id` | `tests/test_errors.py` |
| 14 | `core/errors.py`: кастомное/дефолтное сообщение | `test_custom_message_overrides_default`, `test_default_message_used_when_not_provided` | `tests/test_errors.py` |
| 15 | `core/errors.py`: `trace_id` в теле ошибки берётся из `trace_id_var` | `test_trace_id_reflects_current_contextvar`, `test_trace_id_defaults_to_placeholder_when_unset` | `tests/test_errors.py` |
| 16 | `core/errors.py`: иерархия исключений (`AppError` — база) | `test_all_domain_errors_subclass_app_error[...]`, `test_app_error_message_defaults_to_class_message`, `test_app_error_message_can_be_overridden` | `tests/test_errors.py` |
| 17 | `integrations/storage.py`: `ensure_bucket` идемпотентен (`bucket_exists=True` → `make_bucket` не вызывается) | `test_bucket_exists_true_does_not_create_bucket` | `tests/test_storage.py` |
| 18 | `integrations/storage.py`: `ensure_bucket` создаёт бакет, если его нет | `test_bucket_exists_false_creates_bucket`, `test_ensure_bucket_calling_twice_is_idempotent` | `tests/test_storage.py` |
| 19 | `integrations/storage.py`: `ensure_bucket` маппит `S3Error` → `StorageUnavailable` (деградация при недоступности MinIO) | `test_ensure_bucket_raises_storage_unavailable_on_s3_error` | `tests/test_storage.py` |
| 20 | `integrations/storage.py`: `save_file` делегирует в `put_object` с ожидаемыми аргументами, возвращает `s3_path` | `test_save_file_delegates_to_client_put_object`, `test_save_file_returns_bucket_prefixed_s3_path` | `tests/test_storage.py` |
| 21 | `integrations/storage.py`: `save_file` маппит ошибку MinIO → `StorageUnavailable` | `test_save_file_raises_storage_unavailable_on_s3_error` | `tests/test_storage.py` |
| 22 | `integrations/storage.py`: `get_file` делегирует в `get_object`, читает и закрывает соединение | `test_get_file_delegates_to_client_get_object_and_reads_bytes`, `test_get_file_closes_and_releases_connection_on_success` | `tests/test_storage.py` |
| 23 | `integrations/storage.py`: `get_file` — `NoSuchKey` → `NotFoundError`, прочие S3-ошибки → `StorageUnavailable` | `test_get_file_missing_object_raises_not_found_error`, `test_get_file_other_s3_error_raises_storage_unavailable` | `tests/test_storage.py` |
| 24 | `main.py`: приложение собирается, роутер `photos` подключён, OpenAPI генерится без падения | `test_app_title_matches_service_name`, `test_photos_router_is_mounted`, `test_healthz_and_readyz_routes_registered`, `test_openapi_schema_generates_without_error` | `tests/test_app_boot.py` |
| 25 | `GET /readyz` — happy path 200 при доступных БД+MinIO (замокано) | `test_readyz_returns_200_when_db_and_storage_healthy` | `tests/test_app_boot.py` |
| 26 | `GET /readyz` — degraded → 503 при недоступности БД и/или MinIO (замокано), процесс не падает | `test_readyz_returns_503_when_db_unavailable`, `test_readyz_returns_503_when_storage_unavailable`, `test_readyz_returns_503_when_both_unavailable`, `test_readyz_degraded_response_does_not_crash_process` | `tests/test_app_boot.py` |
| 27 | `lifespan`: старт не падает при недоступности MinIO/БД (мягкая деградация, constitution) | `test_lifespan_does_not_raise_when_minio_unavailable`, `test_lifespan_does_not_raise_when_db_unavailable` | `tests/test_lifespan.py` |
| 28 | `lifespan`: happy path — `ensure_bucket` вызван, `app.state.storage` установлен, `SELECT 1` выполнен | `test_lifespan_succeeds_when_db_and_storage_healthy` | `tests/test_lifespan.py` |
| 29 | `lifespan`: shutdown освобождает engine (`engine.dispose()`) во всех случаях | `test_lifespan_disposes_engine_on_shutdown_even_after_body_runs`, + assert `dispose` во всех остальных lifespan-тестах | `tests/test_lifespan.py` |
| 30 | `db/session.py`: `get_session` — генератор, отдающий ровно одну сессию и корректно её закрывающий | `test_get_session_yields_a_session_and_closes_it` | `tests/test_db_session.py` |
| 31 | `db/session.py`: `engine`/`SessionLocal` — модульные синглтоны | `test_engine_and_session_local_are_module_level_singletons` | `tests/test_db_session.py` |
| 32 | `core/logging.py`: JSON-формат в stdout с полями `timestamp/level/name/service/trace_id/message` | `test_log_record_emits_valid_json_with_expected_fields` | `tests/test_logging.py` |
| 33 | `core/logging.py`: уровень логирования берётся из аргумента (`LOG_LEVEL`) | `test_setup_logging_sets_level_from_argument` | `tests/test_logging.py` |
| 34 | `core/logging.py`: `setup_logging` идемпотентен (без дублирования хендлеров при повторном вызове — актуально, т.к. `app.main` вызывает его на импорт) | `test_setup_logging_is_idempotent_no_duplicate_handlers` | `tests/test_logging.py` |
| 35 | `core/logging.py`: `trace_id_var` — contextvar с дефолтом `"-"`, `TraceIdFilter` прокидывает значение в запись/JSON | `test_trace_id_defaults_to_placeholder`, `test_trace_id_filter_reflects_current_contextvar_value`, `test_log_output_reflects_trace_id_var_when_set` | `tests/test_logging.py` |

---

## 2. Что покрыто / не покрыто

### Покрыто (в скоупе TASK-000)
- `/healthz` — happy path, точная форма тела, метод-контракт, независимость от БД/MinIO.
- `/readyz` — happy path и degraded (БД/MinIO недоступны, по отдельности и вместе), процесс не падает.
- `core/config.py` — дефолты, переопределение через env для всех декларированных переменных, отсутствие
  боевых секретов по умолчанию, кэширование `get_settings()`.
- `core/errors.py` — полный маппинг `AppError`-подклассов на HTTP-коды, точная форма JSON-тела
  (`error_code/message/trace_id`), поведение `trace_id` (из contextvar / дефолт `"-"`).
- `integrations/storage.py` — `ensure_bucket` идемпотентность (оба ветвления + повторный вызов),
  `save_file`/`get_file` делегирование в MinIO SDK с ожидаемыми аргументами, маппинг `S3Error` →
  `StorageUnavailable`/`NotFoundError` (аналог "деградации при недоступности внешнего сервиса" —
  здесь внешний сервис MinIO, а не analyzer, но принцип и код маппинга общие).
- `services/photo_service.py`, `repositories/photo_repository.py` — контракт-заглушка
  (`NotImplementedError`) зафиксирован тестами, чтобы случайная тихая "реализация-заглушка"
  (например, `return None`) не прошла незамеченной в TASK-001.
- `main.py` — сборка приложения, подключение роутера, генерация OpenAPI без падения, отсутствие
  фиктивных upload-роутов.
- `lifespan` — happy path и обе ветки деградации (MinIO/БД недоступны на старте — процесс не падает),
  `engine.dispose()` на shutdown во всех сценариях.
- `db/session.py` — контракт генератора `get_session` (ровно одна сессия, закрытие через `async with`).
- `core/logging.py` — JSON-формат, уровень из аргумента, идемпотентность настройки, contextvar
  `trace_id_var` и его прокидывание в лог-записи.

### Осознанно НЕ покрыто (отложено на TASK-001+)
- **Реальный HTTP-контракт `POST /api/v1/photos`** (валидация файла/MIME/размера, путь в MinIO вида
  `photos/{user_id}/{photo_id}.ext`, атомарная запись в БД, конфликт → 409, ответ 200 с
  `photo_id/user_id/status/created_at`) — эндпоинта физически не существует в TASK-000, критерии из
  `feature-upload/spec.md` §2 п.1–3, 5 неприменимы. Появятся тесты в TASK-001.
- **Kafka-поток** (публикация в `photos-to-analyze`, идемпотентность консьюмера, повторная доставка,
  DLQ) — `integrations/analyzer_client.py` пустая заглушка, Kafka-кода нет вообще (см. `20_design.md`
  §1 "Вне скоупа"). Появится в TASK-002.
- **JWT-аутентификация** (401 без токена, извлечение `user_id` из токена) — вне скоупа, `spec.md` п.7
  (SHOULD), TASK-001+.
- **Метрики Prometheus** (`upload_duration_seconds`, `upload_errors_total`) — не реализованы в коде,
  нечего тестировать.
- **Rate limiting / circuit breaker** — не реализованы, `spec.md` п.8–9 (SHOULD), позже.
- **Реальная интеграция с живым Postgres/MinIO/Docker** (`docker compose up --build` +
  `curl /healthz`) — сознательно не тестировалось test-writer'ом: по заданию тесты должны идти в
  песочнице без внешних сервисов; это остаётся ручной/CI-проверкой на машине с Docker (уже отмечено
  как открытый пункт в `30_impl.md`/`40_review-1.md`).
- **Alembic-миграция** (`v001_init_photos.py`) — не тестировалась (нет живой БД для `alembic upgrade
  head`); кодер уже проверил `alembic upgrade head --sql` статически (см. `30_impl.md` §5). Полноценный
  тест миграции на testcontainers/реальном Postgres — кандидат для TASK-001, если появится DB-фикстура.
- **`db/models.py`** — модель `Photo`/`PhotoStatus` не имеет прямых unit-тестов (нет логики, только
  декларация схемы); косвенно используется в `test_stubs.py`/`test_db_session.py`. Полноценная
  проверка (constraints, unique, index) требует живой БД — TASK-001.
- **Non-blocking замечания N1–N7 из `40_review-1.md`** (server_default рассинхрон, `readyz`
  JSON вручную строкой, `*args/**kwargs` в `upload_photo` и т.д.) — стилевые/дизайнерские, не
  затрагивают тестируемое поведение в этой таске, оставлены как есть (не баги).

---

## 3. Как запустить

Через `uv` (использовался Python 3.12, установлен автоматически через `uv python install`, т.к.
локальный интерпретатор в системе — 3.10, а `pyproject.toml` требует `>=3.12`):

```bash
cd photo-service
uv sync --python 3.12          # или полный путь к интерпретатору 3.12, см. примечание ниже
uv run pytest tests/ -v
uv run ruff check app tests migrations
# coverage (pytest-cov не в основных dev-зависимостях проекта — подключается опционально):
uv run --with pytest-cov pytest tests/ --cov=app --cov-report=term-missing
```

Примечание (Windows-специфика этой песочницы): `uv python install 3.12` иногда падает с
`Missing expected target directory for Python minor version link` из-за отсутствия прав на создание
symlink/junction в `%APPDATA%\uv\python`, хотя сам интерпретатор при этом успешно скачивается и
рабочий. В таком случае используйте прямой путь:
```bash
uv sync --python "$APPDATA/uv/python/cpython-3.12.13-windows-x86_64-none/python.exe"
```
Это особенность окружения test-writer'а, не баг каркаса — `Dockerfile` использует `python:3.12-slim`
напрямую и с этой проблемой не сталкивается.

Никакие тесты не требуют поднятого Postgres/MinIO/Docker — все внешние зависимости замоканы
(`unittest.mock.MagicMock`/`AsyncMock`, `monkeypatch`) или не задействованы (`/healthz`).

---

## 4. Результат прогона

```
70 passed, 1 warning in 0.27s
```
(warning — сторонний `DeprecationWarning` из `pythonjsonlogger` про переезд модуля, не связан с
кодом каркаса и не аффектит поведение).

Coverage (`pytest-cov`, `--cov=app`):
```
Name                                   Stmts   Miss  Cover
--------------------------------------------------------------------
app\__init__.py                            1      0   100%
app\api\photos.py                          2      0   100%
app\core\config.py                        17      0   100%
app\core\errors.py                        37      0   100%
app\core\logging.py                       21      0   100%
app\db\models.py                          21      0   100%
app\db\session.py                          9      0   100%
app\integrations\analyzer_client.py        0      0   100%
app\integrations\storage.py               37      0   100%
app\main.py                               52      0   100%
app\repositories\photo_repository.py       8      0   100%
app\schemas\photos.py                      8      0   100%
app\services\photo_service.py              9      0   100%
--------------------------------------------------------------------
TOTAL                                    222      0   100%
```
100% строчного покрытия всего каркаса `app/` (222/222 строк). Это не то же самое, что coverage ≥85%
из `feature-upload/spec.md` §7 — тот критерий про фичу upload целиком (валидация/MinIO/БД/Kafka),
которой физически ещё нет; для существующего в TASK-000 кода покрытие полное.

`ruff check app tests migrations` → `All checks passed!`.

**Детерминированность/независимость от порядка:** тесты прогонялись в исходном порядке коллекции,
в изменённом порядке (`pytest tests/test_stubs.py tests/test_app_boot.py ...` и т.д.) и повторно с
теми же результатами (58/70 — до/после добавления `test_lifespan.py`, `test_db_session.py`,
`test_logging.py`) — 0 flaky/order-dependent тестов обнаружено. Все внешние зависимости (`Settings`,
`engine`, `ObjectStorage`, `SessionLocal`, `app.state.storage`) патчатся через `monkeypatch`/
`unittest.mock.patch.object`, которые автоматически откатываются после каждого теста — нет утечки
состояния между тестами (в частности, между `test_healthz.py` и `test_app_boot.py`, которые оба
патчат `app.main.engine`).

---

## 5. Найденные при написании проблемы

**Тестовые несоответствия окружения (не баги каркаса, исправлены в тестах самим test-writer'ом):**

1. `AsyncEngine.connect` — read-only дескриптор в текущей версии SQLAlchemy 2.0.51: нельзя
   `monkeypatch.setattr("app.main.engine.connect", ...)` напрямую (падает
   `AttributeError: 'AsyncEngine' object attribute 'connect' is read-only`). Решение в тестах:
   подменять весь объект `app.main.engine` целиком через `monkeypatch.setattr("app.main.engine",
   fake_engine)`, а не его атрибут. Не блокирует прод-код — `main.py` использует `engine.connect()`
   как обычно, проблема только в способе мокирования в тестах.
2. `app.routes` в установленной версии Starlette (1.3.1, тянется свежим FastAPI 0.139.0) отдаёт
   смонтированный через `include_router` роутер как непрозрачный объект `_IncludedRouter` без
   атрибута `.path` — обход через `app.openapi()["paths"]`, который стабильно разворачивает все
   маршруты независимо от способа их подключения. Тоже не баг каркаса, только нюанс версии
   Starlette/способа интроспекции в тестах.

**Баги каркаса:** не обнаружено. Оба ревьювера дали `APPROVE`, код `photo-service/` соответствует
дизайну; в процессе написания тестов расхождений между документированным поведением
(`20_design.md`/`30_impl.md`) и фактическим кодом не найдено.

**Наблюдение (не блокер, но стоит знать оркестратору):** локальная песочница test-writer'а имела
только Python 3.10, а `pyproject.toml` требует `>=3.12` — потребовалось скачать интерпретатор 3.12
через `uv python install`/`uv sync --python`, с обходом Windows-specific ошибки symlink (см. §3).
Реальный `Dockerfile` (`python:3.12-slim`) этой проблемы не имеет; это чисто локальная особенность
машины test-writer'а, аналогичная той, что уже фиксировал `coder` в `30_impl.md` §5 про недоступность
Docker. Не требует действий от оркестратора.

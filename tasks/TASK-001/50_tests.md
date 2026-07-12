---
task_id: TASK-001
agent: test-writer
model: sonnet
status: done
inputs:
  - specs/constitution.md
  - specs/feature-upload/tasks.md
  - tasks/TASK-001/20_design.md
  - tasks/TASK-001/30_impl.md
  - tasks/TASK-001/40_review-1.md
  - tasks/TASK-001/41_review-2.md
  - photo-service/** (code as of both APPROVEs)
outputs:
  - tasks/TASK-001/50_tests.md
  - photo-service/tests/test_photo_service.py (new)
  - photo-service/tests/test_photos_endpoints.py (new)
  - photo-service/tests/test_photo_repository.py (new)
  - photo-service/tests/test_request_id_middleware.py (new)
  - photo-service/tests/test_migration_integration.py (new)
  - photo-service/tests/test_storage.py (extended: delete_file, _validate_object_name)
  - photo-service/pyproject.toml (added pytest-cov dev dep, registered `integration` marker)
timestamp: 2026-07-13T00:00:00Z
---

# TASK-001 — Тесты: Публичный API работы с фотографиями

Оба ревьювера дали **APPROVE** (`40_review-1.md`, `41_review-2.md`). Эта таска добавляет
полное функциональное покрытие 4 эндпоинтов + обязательный интеграционный тест миграции,
закрывающий пробел из `tasks/TASK-000/60_debug.md`.

## Матрица: критерий приёмки → тест

### DoD Воркшопа 2 (`specs/feature-upload/tasks.md` §"Критерии приёмки")

| Критерий приёмки | Тест(ы) |
|---|---|
| Можем загрузить фото через `POST /v1/photos` | `test_photos_endpoints.py::TestUploadPhotoHappyPath` (jpeg/png → 202); `test_photo_service.py::TestCreatePhotoHappyPath` (реальная валидация+сборка `Photo`) |
| Файл появляется в MinIO (`photos/{photo_id}/original.*`) | `test_photo_service.py::test_object_key_matches_photos_id_original_ext_contract`, `test_storage_save_file_called_with_object_key_and_bytes` (проверяют формат ключа и вызов `storage.save_file` с правильными аргументами — без Docker реальный MinIO не проверялся, см. «Что не покрыто») |
| Таблица `photos` содержит запись со `status = pending` | `test_photo_service.py::TestCreatePhotoHappyPath::test_object_key_matches_photos_id_original_ext_contract` (`photo_arg.status == PhotoStatus.pending`); `test_photo_repository.py::TestCreate` (add+flush без commit) |
| `GET /v1/photos/{id}` отдаёт статус `pending` | `test_photos_endpoints.py::TestGetPhotoStatus::test_existing_photo_returns_200_with_photo_response`; `test_photo_service.py::TestGetPhoto` |
| Ошибки и логи содержат понятные поля (`error_code`, `request_id`, `photo_id`) | `test_errors.py` (весь файл, включая `TestCatchAllHandler`); `test_photos_endpoints.py::TestErrorBodyRequestId`; `test_request_id_middleware.py`; `test_photo_service.py::TestObservability` (photo_id в логах); `test_logging.py` (request_id в логах) |
| Два APPROVE (reviewer-1 opus + reviewer-2 haiku) | Проверено оркестратором до вызова test-writer (`40_review-1.md`/`41_review-2.md` — оба `status: APPROVE`) |
| Тесты покрывают критерии приёмки; coverage ≥ 85% | Этот документ; факт — **99% по `app/`** (см. «Результат прогона») |
| Интеграционный тест миграции на эфемерном Postgres | `test_migration_integration.py` (testcontainers, `@pytest.mark.integration`, skip без Docker) |
| Живой прогон `docker compose up --build` → upload→get | **Вне скоупа test-writer** — явно поручено пользователю (Docker daemon недоступен агенту в песочнице; см. `30_impl.md` «Что осталось») |

### Детальный чек-лист по эндпоинтам (design §2, §11.2)

| # | Требование | Тест(ы) |
|---|---|---|
| 1 | `POST /v1/photos` happy path JPEG → 202 `{photo_id, status:"pending"}` | `test_photos_endpoints.py::test_jpeg_upload_returns_202_with_expected_body` |
| 2 | `POST /v1/photos` happy path PNG → 202 | `test_photos_endpoints.py::test_png_upload_returns_202` |
| 3 | Пустой файл → 400 `INVALID_FILE` | `test_photo_service.py::test_empty_file_raises_validation_error_...`; `test_photos_endpoints.py::TestUploadPhotoErrorMapping` (parametrized); `test_stubs.py::test_validate_rejects_empty_file` |
| 4 | Файл >50 МБ → 413 `PAYLOAD_TOO_LARGE` | `test_photo_service.py::test_oversized_file_raises_payload_too_large`, `TestValidate::test_validate_rejects_file_over_50mb`, граница ровно 50 МБ — `test_validate_accepts_file_exactly_at_50mb_boundary`; endpoint-уровень — `TestUploadPhotoErrorMapping` |
| 5 | Не изображение (текст/поддельное расширение) → 415 `UNSUPPORTED_MEDIA_TYPE` | `test_photo_service.py::test_non_image_raises_unsupported_media_type`, `test_validate_rejects_text_disguised_with_image_extension_by_content`; endpoint — `TestUploadPhotoErrorMapping` |
| 6 | Сбой/недоступность MinIO при upload → 503 + запись НЕ закоммичена (rollback) | `test_photo_service.py::TestCreatePhotoOrderAndRollback::test_storage_unavailable_rolls_back_and_does_not_commit`, `test_storage_timeout_is_converted_to_storage_unavailable_and_rolls_back`, `test_repository_create_still_called_before_storage_failure` |
| 7 | Порядок БД(flush)→MinIO→commit | `test_photo_service.py::test_calls_happen_in_order_flush_then_save_then_commit` (явный порядок через список side-effect'ов) |
| 8 | UNIQUE-конфликт → 409 `CONFLICT`, MinIO не трогается | `test_photo_service.py::test_conflict_error_from_repository_propagates_without_touching_storage`; `test_photo_repository.py::TestCreate::test_integrity_error_on_flush_raises_conflict_error` |
| 9 | `GET /v1/photos` — пустой список | `test_photos_endpoints.py::TestListPhotos::test_empty_list_returns_200_empty_array` |
| 10 | `GET /v1/photos` — список с элементами, формат `PhotoResponse` | `test_photos_endpoints.py::test_list_with_items_returns_photo_response_shape` |
| 11 | `GET /v1/photos` — пагинация (limit/offset, дефолты, границы, 422) | `test_photos_endpoints.py::test_default_limit_and_offset_are_50_and_0`, `test_custom_limit_and_offset_are_forwarded`, `test_out_of_range_pagination_params_return_422` (parametrized: `limit=0`, `limit=101`, `offset=-1`); `test_photo_repository.py`/`test_photo_service.py::TestListPhotos::test_forwards_limit_and_offset_to_repository` |
| 12 | `GET /v1/photos/{id}` существующий → 200 | `test_photos_endpoints.py::TestGetPhotoStatus::test_existing_photo_returns_200_with_photo_response` |
| 13 | `GET /v1/photos/{id}` несуществующий → 404 единый формат | `test_photos_endpoints.py::test_missing_photo_returns_404_unified_body` |
| 14 | `GET /v1/photos/{id}` невалидный UUID → 422 (не 500) | `test_photos_endpoints.py::test_non_uuid_path_param_returns_422_not_500` |
| 15 | `GET /v1/photos/{id}/content` существующий → 200, корректный Content-Type | `test_photos_endpoints.py::TestGetPhotoContent::test_existing_photo_returns_200_with_bytes_and_content_type`, `test_png_content_type_is_image_png` |
| 16 | `GET .../content` — Content-Disposition, кириллица/кавычки, не 500 (регрессия B1) | `test_photos_content_disposition.py` (уже существовал, 7 тестов — не дублировался) |
| 17 | `GET .../content` — нет записи → 404 | `test_photos_endpoints.py::test_missing_record_returns_404` |
| 18 | `GET .../content` — есть запись, нет объекта в MinIO (`NoSuchKey`) → 404 | `test_photos_endpoints.py::test_missing_object_in_storage_returns_404`; `test_photo_service.py::test_object_missing_in_storage_propagates_not_found` |
| 19 | `GET .../content` — MinIO недоступен → 503 | `test_photos_endpoints.py::test_storage_unavailable_returns_503`; `test_photo_service.py::test_storage_unavailable_propagates`, `test_storage_timeout_is_converted_to_storage_unavailable` |
| 20 | Единый формат ошибок `{error_code, message, request_id}` на 400/413/415/404/409/503 | `test_photos_endpoints.py::TestUploadPhotoErrorMapping` (parametrized, все 5 кодов + shape-проверка); `test_errors.py::TestStatusCodeMapping`/`TestErrorBodyShape` |
| 21 | catch-all 500 (не-`AppError`) — единый формат, детали не текут | `test_errors.py::TestCatchAllHandler` (уже существовал, не дублировался) |
| 22 | `request_id`: эхо заголовка `X-Request-ID`, генерация при отсутствии, проброс в тело ошибки | `test_request_id_middleware.py` (весь файл: echo на `/healthz`, генерация uuid4, разные id на разных запросах, проброс в error body с явным/без явного заголовка); `test_photos_endpoints.py::TestErrorBodyRequestId` |
| 23 | Наблюдаемость: логи содержат `photo_id`+`request_id` на ключевых шагах | `test_photo_service.py::TestObservability` (caplog: `create_photo` happy path ≥3 записи с `photo_id`, WARNING на rollback с `photo_id`, `content served` с `photo_id`); `test_logging.py` (request_id в JSON/LogRecord, уже существовал) |
| 24 | Repository: `create`/`get_by_id`/`list`/`update_status`, без commit, SQL только здесь | `test_photo_repository.py` (весь файл, 11 тестов) |
| 25 | Интеграционный тест миграции: `alembic upgrade head`/`downgrade base` на реальном Postgres, enum создаётся ровно 1 раз, схема 1:1 с моделью | `test_migration_integration.py::TestV001MigrationAgainstRealPostgres` (testcontainers; `@pytest.mark.integration`; skip без Docker — см. «Как запустить») |

## Что покрыто / не покрыто

### Покрыто

- Все 4 эндпоинта: happy path + все документированные коды ошибок (400/413/415/404/409/503/422/500).
- Реальная бизнес-логика `PhotoService` (не только HTTP-обвязка): валидация magic bytes (JPEG/PNG,
  порядок empty→413→415, граница ровно 50 МБ), формирование `object_key`/`filename`, порядок
  БД(flush)→MinIO(save)→commit, rollback+503 на сбой/таймаут MinIO, проброс `ConflictError`/`NotFoundError`.
  запросов
- `PhotoRepository`: `create` (add+flush, без commit, `IntegrityError`→`ConflictError`),
  `get_by_id`, `list` (реальный SQL скомпилирован и проверен на `ORDER BY ... DESC`/`LIMIT`/`OFFSET`),
  `update_status`.
- `RequestIdMiddleware`: echo, генерация, разные id на разных запросах, non-`http` scope passthrough,
  проброс в тело ошибки (с explicit/generated id).
- `ObjectStorage`: `ensure_bucket`/`save_file`/`get_file` (уже было), плюс новое — `delete_file`
  (best-effort, включая проглатывание `NoSuchKey`) и `_validate_object_name` (defense-in-depth).
- Единый формат ошибок на всех документированных кодах + catch-all 500 (уже было, не дублировано).
- Наблюдаемость: `photo_id` в логах на upload/rollback/content-served через `caplog`; `request_id`
  в логах (было) и в теле ошибки/заголовке ответа (новое, явный + сгенерированный случай).
- Интеграционный тест миграции написан полностью (testcontainers Postgres 16, `alembic upgrade
  head`/`downgrade base` в subprocess, проверка колонок/UNIQUE/enum-меток до и после).
- `get_photo_service` DI-функция — прямой юнит-тест (не только косвенно через overrides).

### Не покрыто / сознательно оставлено

- **`app/integrations/analyzer_client.py` (0%)** — документированная заглушка для TASK-002
  (`NotImplementedError`), нигде не импортируется по дизайну (§9); тестировать нечего сверх
  проверки, что она поднимает `NotImplementedError` — сочтено малоценным шумом, не добавлял.
- **Реальный живой прогон MinIO/Postgres** (объект действительно появляется в бакете, запись
  реально коммитится в таблицу) — не выполнялся: Docker daemon недоступен в песочнице агента
  (`docker version` подключается к клиенту, но `docker.from_env().ping()` падает
  `DockerException: ... npipe ...`). Единственный тест, которому это принципиально нужно
  (`test_migration_integration.py`), написан и корректно **skip**'ается по этой причине — см.
  «Как запустить» для прогона у пользователя.
- **`docker compose up --build` + curl-сценарий upload→get→content** — явно вне скоупа
  test-writer, поручено пользователю (зафиксировано в `30_impl.md`/`tasks.md`).
- **`protos/analyzer.proto`** — не Python-код, тестировать нечего (нет сгенерированных стабов
  по дизайну).

## Как запустить

```bash
cd photo-service

# Обычный прогон (без Docker) — весь функционал, кроме интеграционного теста миграции:
uv run pytest -q
# -> 173 passed, 1 skipped (test_migration_integration — skip: "Docker daemon is not reachable")

# С покрытием:
uv run pytest -q --cov=app --cov-report=term-missing

# Только интеграционный тест миграции (требует ЗАПУЩЕННЫЙ Docker Desktop/daemon):
uv run pytest -m integration -q
# -> поднимет postgres:16-alpine через testcontainers, прогонит
#    `python -m alembic upgrade head` / `downgrade base` в subprocess и провалится/пройдёт по-настоящему

# ruff:
uv run ruff check .
```

## Результат прогона

- `uv run ruff check .` → **All checks passed!**
- `uv run pytest -q` → **173 passed, 1 skipped** (174 tests total; skip = `test_migration_integration.py`,
  reason: `"Docker daemon is not reachable - testcontainers needs it to run this test"` — ожидаемо в
  этой песочнице, Docker Desktop клиент есть, но engine недоступен: `docker.errors.DockerException:
  Error while fetching server API version`).
- `uv run pytest -m integration -q` → **1 skipped, 173 deselected** (маркер зарегистрирован в
  `pyproject.toml [tool.pytest.ini_options] markers`, корректно изолирует тест).
- `uv run pytest -q --cov=app --cov-report=term-missing` → **TOTAL 99% (386/389 стейтментов)**,
  порог ≥85% выполнен с большим запасом. Непокрытые 3 строки — исключительно
  `app/integrations/analyzer_client.py:25-37` (заглушка TASK-002, см. выше). Все остальные
  модули `app/` — **100%**: `api/middleware.py`, `api/photos.py`, `core/config.py`,
  `core/errors.py`, `core/logging.py`, `db/models.py`, `db/session.py`,
  `integrations/storage.py`, `main.py`, `repositories/photo_repository.py`,
  `schemas/photos.py`, `services/photo_service.py`.

### Разбивка тестов по файлам (174 всего)

| Файл | # тестов | Новый/существующий |
|---|---|---|
| `test_photo_service.py` | 35 | **новый** |
| `test_photos_endpoints.py` | 27 | **новый** |
| `test_errors.py` | 24 | существующий (не менялся) |
| `test_storage.py` | 18 | существующий + **расширен** (delete_file, _validate_object_name: +9) |
| `test_photo_repository.py` | 11 | **новый** |
| `test_app_boot.py` | 10 | существующий |
| `test_config.py` | 8 | существующий |
| `test_healthz.py` | 8 | существующий |
| `test_photos_content_disposition.py` | 7 | существующий (не менялся) |
| `test_stubs.py` | 7 | существующий |
| `test_request_id_middleware.py` | 6 | **новый** |
| `test_logging.py` | 6 | существующий |
| `test_lifespan.py` | 4 | существующий |
| `test_db_session.py` | 2 | существующий |
| `test_migration_integration.py` | 1 | **новый** (skip без Docker) |

## Найденные при написании проблемы

Реальных багов кода не найдено — реализация точно соответствует дизайну и обоим ревью. Один
нюанс тестовой инфраструктуры (не баг продакшен-кода), стоит зафиксировать:

- При наивном мокировании `AsyncSession` через `unittest.mock.AsyncMock()` для repository-тестов
  возникал `RuntimeWarning: coroutine 'AsyncMockMixin._execute_mock_call' was never awaited` на
  `session.add(photo)` — потому что `AsyncMock()` делает *все* атрибуты асинхронными, а
  `Session.add` в реальном SQLAlchemy синхронный (репозиторий его не `await`-ит, и это корректно).
  Исправлено локально в тесте (`_mock_session()` helper в `test_photo_repository.py`, где `add` —
  обычный `MagicMock`, а `flush`/`execute`/`commit`/`rollback` — явные `AsyncMock`); продакшен-код
  не менялся, это чисто тестовая гигиена.

## Что осталось

- Живой `docker compose up --build` + curl-сценарий (пользователь, вне скоупа test-writer).
- Прогон `uv run pytest -m integration -q` на машине с запущенным Docker Desktop — тест написан
  и готов, но не был реально выполнён (только проверено, что корректно skip'ается без Docker).

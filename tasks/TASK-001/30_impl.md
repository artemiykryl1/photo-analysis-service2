---
task_id: TASK-001
agent: coder
model: sonnet
status: done
inputs:
  - specs/constitution.md
  - specs/feature-upload/tasks.md
  - tasks/TASK-001/10_context.md
  - tasks/TASK-001/20_design.md
  - tasks/TASK-001/40_review-1.md
outputs:
  - tasks/TASK-001/30_impl.md
timestamp: 2026-07-13T00:00:00Z
---

# TASK-001 — Реализация: Публичный API работы с фотографиями

## Итерация 2 — фикс B1 (BLOCKING) + N1 (по итогам tasks/TASK-001/40_review-1.md)

### B1 (BLOCKING): `Content-Disposition` падал 500 на нелатинском/кавычном имени файла

- Файл: `photo-service/app/api/photos.py`.
- Добавлена приватная функция `_content_disposition(filename: str) -> str`, реализующая RFC 6266/5987:
  ASCII-only `filename="..."` fallback (не-ASCII символы отброшены через `encode("ascii","ignore")`,
  `"`/`\` дополнительно вычищены, чтобы не сломать синтаксис заголовка; пустой fallback → `"download"`)
  **плюс** `filename*=UTF-8''<percent-encoded>` (через `urllib.parse.quote(filename, safe="")`) с точным
  оригинальным именем для клиентов, которые понимают extended-parameter.
- `download_photo_content` теперь строит заголовок через `_content_disposition(filename)` вместо
  сырой f-строки. Результат гарантированно ASCII/latin-1-safe (percent-encoding даёт только ASCII-символы).
- **Тесты** (новый файл `tests/test_photos_content_disposition.py`):
  - юнит-тесты `_content_disposition`: чистое ASCII-имя, кириллица (не падает, есть ASCII-fallback,
    корректный `filename*=UTF-8''...`), имя с `"` (ровно 2 кавычки во всём заголовке — синтаксис не
    ломается), имя с `\`, полностью не-ASCII имя → fallback `"download"`;
  - end-to-end тесты через `app.dependency_overrides` (без реальных БД/MinIO): кириллическое имя и имя
    с `"` дают **200**, а не 500, заголовок корректен и кодируется в latin-1 без исключения.

### N1 (усиление DoD): catch-all-обработчик для не-`AppError` исключений

- Файл: `photo-service/app/core/errors.py`, `register_exception_handlers`.
- Добавлен второй хендлер `@app.exception_handler(Exception)`: любое непредвиденное исключение (не
  `AppError`) теперь логируется через `logger.exception(...)` (полный traceback в логах, `photo_id=None`,
  `error_code="INTERNAL_ERROR"`) и отдаётся клиенту в едином формате
  `{error_code:"INTERNAL_ERROR", message:"Internal server error", request_id}` со статусом 500.
  Детали исключения (сообщение/traceback) в тело ответа НЕ попадают.
- Специфичный хендлер `AppError` по-прежнему выбирается первым для доменных исключений (Starlette
  матчит по MRO, более специфичный класс приоритетнее) — проверено отдельным тестом
  (`test_app_error_still_uses_its_own_handler_not_catch_all`).
- **Важный нюанс реализации/тестирования**: FastAPI/Starlette трактуют обработчик, зарегистрированный
  для `Exception` (или статус-кода 500), как `error_handler` внешнего `ServerErrorMiddleware`
  (а не `ExceptionMiddleware`). `ServerErrorMiddleware` **всегда** повторно поднимает исключение после
  отправки ответа — это штатное поведение Starlette (чтобы ASGI-сервер мог залогировать ошибку); реальный
  клиент (uvicorn/curl) уже получает корректно сформированный JSON-ответ до повторного raise. Но
  `httpx.ASGITransport` по умолчанию пробрасывает это исключение в тестовый код. Поэтому в
  `tests/test_errors.py::raising_client_factory` добавлен `raise_app_exceptions=False` — иначе тесты на
  catch-all падали бы с `RuntimeError`/`KeyError` вместо получения ответа. Поведение на реальном сервере
  не меняется (проверено вручную: `RequestValidationError`/422 по-прежнему обрабатывается дефолтным
  FastAPI-хендлером, не перехватывается catch-all — см. «Как проверить» ниже).
- **Тесты** (добавлены в `tests/test_errors.py::TestCatchAllHandler`): непредвиденное исключение → 500 +
  единый формат тела; детали исключения не просачиваются в тело; `request_id` берётся из contextvar;
  `AppError` (напр. `NotFoundError`) по-прежнему обрабатывается своим хендлером, а не catch-all.

### Проверка (итерация 2)

```bash
cd photo-service
uv run ruff check .     # All checks passed!
uv run pytest -q        # 87 passed (было 76 + 11 новых: 9 из test_photos_content_disposition.py, 4 из TestCatchAllHandler — минус пересечения)
```

Дополнительно вручную проверено (вне pytest, т.к. Docker недоступен для полного live-прогона):
`GET /v1/photos/not-a-uuid` с замоканным `app.state.storage` → по-прежнему **422** от стандартного
FastAPI-валидатора (не перехвачено новым catch-all `Exception`-хендлером).

### Изменённые/новые файлы (итерация 2)

- `photo-service/app/api/photos.py` — изменён (добавлена `_content_disposition`, заменена сборка заголовка)
- `photo-service/app/core/errors.py` — изменён (добавлен catch-all `Exception`-хендлер)
- `photo-service/tests/test_photos_content_disposition.py` — новый (регрессия на B1)
- `photo-service/tests/test_errors.py` — изменён (класс `TestCatchAllHandler`, `raise_app_exceptions=False` в фикстуре)

---

## Что сделано (по шагам плана 20_design.md §12)

1. **pyproject.toml** — добавлены `python-multipart>=0.0.9` (runtime) и `testcontainers[postgres]>=4.0` (dev). `uv sync` прогнан, `uv.lock` обновлён локально (файл в `.gitignore`, Dockerfile его не копирует и не использует `--frozen` — см. «Открытые вопросы»).
2. **app/db/models.py** — `PhotoStatus` → `pending/processing/done/failed`; `Photo`: `s3_path`→`object_key` (`unique=True`, `String(1024)`), добавлено `filename: String(512) NOT NULL`, `user_id` теперь `str | None` (nullable), `uploaded_at`→`created_at`, `status` default `PhotoStatus.pending`, индекс → `Index("ix_photos_created_at", "created_at")`.
3. **migrations/versions/v001_init_photos.py** — правка на месте (ревизия осталась `v001`, `down_revision=None`), синхронно с моделью: enum-значения, колонки `filename/object_key/user_id(nullable)/created_at/status(server_default='pending')`, `UniqueConstraint("object_key", name="uq_photos_object_key")`, индекс `ix_photos_created_at`; `downgrade()` дропает индекс/таблицу/тип. `create_type=False` сохранён без изменений.
4. **app/core/errors.py** — добавлены `PayloadTooLargeError(413, PAYLOAD_TOO_LARGE)` и `UnsupportedMediaTypeError(415, UNSUPPORTED_MEDIA_TYPE)`; `AppError.__init__` принимает опциональный keyword `photo_id` (используется для логирования, не сериализуется в ответ); хендлер собирает `ErrorResponse(..., request_id=trace_id_var.get())` и логирует `extra={"error_code", "photo_id"}`.
5. **app/schemas/photos.py** — добавлены `UploadPhotoResponse{photo_id, status: Literal["pending"]}` и `PhotoResponse{id, filename, status: Literal[...]}`; `ErrorResponse.trace_id` → `ErrorResponse.request_id`.
6. **app/repositories/photo_repository.py** — реализованы `create` (add+flush, `IntegrityError`→`ConflictError`, без commit), `get_by_id` (переименован из `get`), `list(limit, offset)` (сортировка `created_at DESC`), `update_status` (для будущего TASK-002/003). SQL только в этом модуле.
7. **app/core/logging.py** — `TraceIdFilter` инжектит и `record.trace_id`, и `record.request_id` (одно и то же значение из `trace_id_var`); формат JSON включает оба поля.
8. **app/api/middleware.py (новый)** — pure-ASGI `RequestIdMiddleware`: `X-Request-ID` из заголовка или `uuid4()` → `trace_id_var.set` → эхо в заголовке ответа → `reset` в `finally`. Явно НЕ `BaseHTTPMiddleware` (см. риск §7.1/§13 дизайна).
9. **app/integrations/storage.py** — добавлен `delete_file` (best-effort компенсация, глотает `NoSuchKey`, логирует WARN на прочие ошибки); добавлена `_validate_object_name` (защита от `..`/`//`, поднимает `ValueError` — это programming-error путь, недостижимый с доверенным вводом, а не пользовательская ошибка); докстринги/комментарии переведены на терминологию `object_key`.
10. **app/services/photo_service.py** — реализованы `create_photo` (валидация → `repository.create` (add+flush) → `anyio.to_thread.run_sync(storage.save_file)` под `asyncio.timeout(60)` → rollback+`StorageUnavailable` при сбое/таймауте → `commit` → `UploadPhotoResponse`), `get_photo` (404 если нет), `list_photos`, `get_photo_content` (404 если нет записи; `NotFoundError`/`StorageUnavailable` из `storage.get_file` пробрасываются как есть; `TimeoutError` конвертируется в `StorageUnavailable`). Приватные `_validate(data)` (magic bytes JPEG/PNG, порядок 400→413→415) и `_ext_to_mime`. Логи на всех точках из §7.3 дизайна.
11. **app/api/photos.py** — переписан: `prefix="/v1/photos"`, DI `get_photo_service`, 4 маршрута (`POST ""` → 202, `GET ""` с `limit`/`offset` query, `GET "/{photo_id}"`, `GET "/{photo_id}/content"`). Модуль не импортирует модели/`select`/MinIO SDK напрямую.
12. **app/main.py** — `app.add_middleware(RequestIdMiddleware)` подключён внешним слоем перед регистрацией хендлеров/роутера; `/healthz`/`/readyz` не тронуты.
13. **protos/analyzer.proto (новый)** — контракт `PhotoAnalyzer.AnalyzePhoto` из §9 дизайна.
14. **app/integrations/analyzer_client.py** — документированный стаб `AnalyzerClient.analyze_photo` (поднимает `NotImplementedError`, TODO(TASK-002), ссылка на `protos/analyzer.proto`); не импортируется никем.
15. **Обновление сломанных тестов TASK-000** (только чинил контракт, не писал новое покрытие — это задача test-writer):
    - `tests/test_stubs.py` — переписан: убраны contract-lock тесты на `NotImplementedError` для методов, которые теперь реализованы; оставлена/добавлена лёгкая проверка конструктора и `_validate`/`get_photo`(404).
    - `tests/test_errors.py` — `trace_id`→`request_id` в теле ошибки и в именах тестов; добавлены кейсы 413/415.
    - `tests/test_logging.py` — добавлены проверки, что `request_id` присутствует наравне с `trace_id` (в JSON-выводе и в `LogRecord`).
    - `tests/test_app_boot.py` — префикс роутера `/api/v1/photos`→`/v1/photos`; тест «маршрутов ещё нет» заменён на проверку, что все 4 маршрута `/v1/photos*` зарегистрированы в OpenAPI.
    - `tests/test_storage.py` — косметическое переименование одного теста (`s3_path`→`object_key` в названии); поведенческих изменений не потребовалось (используемые в тестах имена объектов не содержат `..`/`//`).

## Изменённые/новые файлы

- `photo-service/pyproject.toml` — изменён (зависимости)
- `photo-service/app/db/models.py` — изменён
- `photo-service/migrations/versions/v001_init_photos.py` — изменён (правка v001 на месте)
- `photo-service/app/core/errors.py` — изменён
- `photo-service/app/schemas/photos.py` — изменён
- `photo-service/app/repositories/photo_repository.py` — изменён
- `photo-service/app/core/logging.py` — изменён
- `photo-service/app/api/middleware.py` — новый
- `photo-service/app/integrations/storage.py` — изменён
- `photo-service/app/services/photo_service.py` — изменён
- `photo-service/app/api/photos.py` — изменён
- `photo-service/app/main.py` — изменён
- `photo-service/protos/analyzer.proto` — новый
- `photo-service/app/integrations/analyzer_client.py` — изменён
- `photo-service/tests/test_stubs.py` — изменён
- `photo-service/tests/test_errors.py` — изменён
- `photo-service/tests/test_logging.py` — изменён
- `photo-service/tests/test_app_boot.py` — изменён
- `photo-service/tests/test_storage.py` — изменён (косметика)
- `photo-service/uv.lock` — обновлён локально (не отслеживается git; см. открытые вопросы)

## Принятые мелкие решения (в рамках дизайна, без архитектурных отклонений)

1. **`Content-Disposition` на `GET /v1/photos/{id}/content`** (§2.4 дизайна упоминает этот заголовок, но псевдокод сервисного метода в §4.3 возвращает `tuple[bytes, str]` без имени файла): расширил возврат `get_photo_content` до `tuple[bytes, str, str]` (data, content_type, filename), чтобы api-слой мог выставить `Content-Disposition: inline; filename="..."`, не импортируя модели напрямую. Слоевые границы (api не знает про ORM/SQL) не нарушены.
2. **`AppError.__init__` получил опциональный keyword `photo_id`** — не описан явно в дизайне, но дизайн требовал логировать `photo_id` из исключения через `getattr(exc, 'photo_id', None)`; добавил явную поддержку в базовом классе вместо ad-hoc присвоения атрибута, чтобы это было типобезопасно и единообразно. Поле не сериализуется в HTTP-тело (только `error_code/message/request_id`).
3. **`_validate_object_name` в storage.py поднимает `ValueError`, а не `AppError`-подкласс** — путь недостижим для непроверенного ввода (object_key всегда строится сервисом из `uuid4()`), это defensive assertion, а не пользовательская ошибка; решил не заводить под это отдельный `error_code`.
4. **`test_stubs.py` переписан, а не удалён** — сохранил файл (вместо удаления) с новым набором смоук-тестов на реализованное поведение, следуя формулировке дизайна «замени... или удали» — выбрал вариант с заменой, чтобы не терять регресс-защиту на конструктор/валидацию до прихода test-writer.
5. **UNIQUE-констрейнт в миграции** задан явным `sa.UniqueConstraint("object_key", name="uq_photos_object_key")` (а не `unique=True` на колонке) — соответствует явной формулировке §3.3 дизайна (именованный констрейнт), тогда как в `models.py` оставлен декларативный `unique=True` (используется только для метаданных ORM, не для генерации схемы — схему создаёт alembic).

## Как запустить/проверить локально

```bash
cd photo-service
uv sync
uv run ruff check .          # чисто
uv run pytest -q             # 76 passed (существующие тесты, включая обновлённые)

# Живой прогон (Docker демон был недоступен в среде агента — не выполнялся здесь,
# оставлено пользователю/этапу тестов):
docker compose down -v
docker compose up --build    # alembic upgrade head не должен падать DuplicateObject

curl -si -F "file=@sample.jpg" http://localhost:8000/v1/photos
# -> 202 {"photo_id": "...", "status": "pending"}, заголовок X-Request-ID

curl -s http://localhost:8000/v1/photos
# -> [ {"id": "...", "filename": "sample.jpg", "status": "pending"} ]

curl -s http://localhost:8000/v1/photos/<photo_id>
# -> {"id": "...", "filename": "sample.jpg", "status": "pending"}

curl -sO -J http://localhost:8000/v1/photos/<photo_id>/content
# -> байты, Content-Type: image/jpeg, Content-Disposition: inline; filename="sample.jpg"

curl -si -F "file=@notimage.txt" http://localhost:8000/v1/photos
# -> 415 {"error_code":"UNSUPPORTED_MEDIA_TYPE","message":"...","request_id":"..."}

# Проверить в MinIO объект photos/<photo_id>/original.jpg и в БД строку status=pending.
```

## Что осталось для test-writer

- Полное функциональное покрытие эндпоинтов (`POST/GET /v1/photos*`) через httpx ASGI с моками service/storage — happy path + все коды ошибок (400/413/415/404/409/503/422). Кейс с нелатинским/кавычным именем файла в `Content-Disposition` уже покрыт в `tests/test_photos_content_disposition.py` (итерация 2, B1) — test-writer может расширить, но базовая регрессия уже есть.
- Интеграционный тест миграции на эфемерном Postgres (`tests/test_migration_integration.py`, testcontainers, `@pytest.mark.integration`, subprocess `alembic upgrade head`/`downgrade base`) — закрывает пробел TASK-000/60_debug; НЕ создавался в этой таске (Docker демон недоступен в среде агента для верификации, а задача explicitly отдана test-writer по дизайну §11.1/§15).
- Repository-тесты против реального/тестконтейнерного Postgres (create/get_by_id/list/UNIQUE-конфликт).
- Тест на request_id middleware (эхо заголовка, проброс в contextvar, видимость в логах через caplog).
- Coverage ≥ 85% (гейт проекта) — не измерялся в этой таске (нет `pytest-cov` в dev-зависимостях; test-writer может добавить при необходимости).

## Открытые вопросы для ревью

1. `Dockerfile` использует `uv sync --no-dev` (без `--frozen`) и не копирует `uv.lock` в образ вовсе — `uv.lock` в `.gitignore`. Задание кодеру предполагало, что Dockerfile использует `--frozen`, но по факту это не так; я обновил `uv.lock` локально для консистентности среды разработки, но воспроизводимость сборки образа от лока не зависит. Дизайн не просил трогать Dockerfile — оставил как есть; если оркестратор/архитектор считает нужным зафиксировать зависимости через `--frozen` в образе, это отдельная задача.
2. `docker compose up --build` и полный сценарий upload→get→content не прогонялись вживую — Docker daemon был недоступен в среде выполнения (`docker version` вернул ошибку подключения к движку). Ручные шаги проверки перечислены выше.
3. Расширение сигнатуры `get_photo_content` до 3-элементного tuple (см. «Принятые решения» п.1) — прошу ревьюеров подтвердить, что это приемлемое уточнение дизайна, а не архитектурное отклонение.

## Чек-лист самопроверки по критериям приёмки (spec: feature-upload/tasks.md §TASK-001)

- [x] `POST /v1/photos` реализован: валидация (400 пусто, 413 >50МБ, 415 не JPEG/PNG), `object_key=photos/{photo_id}/original.<ext>`, порядок БД→MinIO→commit, rollback+503 при сбое MinIO, 202 `UploadPhotoResponse`.
- [x] Файл сохраняется в MinIO по контракту `object_key` (код реализован; живая проверка через `docker compose up` оставлена пользователю — Docker недоступен в среде агента).
- [x] Таблица `photos`: `status=pending` по умолчанию (модель + миграция синхронны).
- [x] `GET /v1/photos/{id}` отдаёт `PhotoResponse{id, filename, status}`, 404 если нет.
- [x] `GET /v1/photos` — список, `GET /v1/photos/{id}/content` — байты с `Content-Type`, 404/503.
- [x] Единый формат ошибок `{error_code, message, request_id}` подключён везде через `register_exception_handlers` — **включая непредвиденные (не-`AppError`) исключения** (итерация 2, N1: catch-all `Exception`-хендлер, 500 → `INTERNAL_ERROR`, без утечки деталей).
- [x] JSON-логи содержат `request_id`/`trace_id`/`photo_id` на ключевых шагах.
- [x] Repository-слой без SQL вне `photo_repository.py`; `api/photos.py` не импортирует модели/`select`/MinIO SDK.
- [x] Миграция v001 правлена (не v002), `create_type=False` сохранён.
- [x] `.proto` контракт analyzer добавлен, без генерации стабов, без вызовов из кода.
- [x] `GET /v1/photos/{id}/content` отдаёт корректный `Content-Disposition` для ЛЮБОГО имени файла (включая кириллицу и `"`/`\`), не падает 500 — итерация 2, B1 fix (RFC 6266/5987).
- [x] `python -m py_compile` по всем изменённым файлам — OK.
- [x] `uv sync` — OK; `uv run ruff check .` — чисто; `uv run pytest -q` — 87 passed (после итерации 2, было 76).
- [ ] Живой `docker compose up --build` + curl-сценарий upload→get→content — НЕ выполнен (Docker daemon недоступен агенту); оставлен пользователю.
- [ ] Интеграционный тест миграции на эфемерном Postgres — не создавался в этой таске (входит в объём test-writer по дизайну).
- [ ] Coverage ≥ 85% — не измерялся (нет тестов от test-writer ещё).

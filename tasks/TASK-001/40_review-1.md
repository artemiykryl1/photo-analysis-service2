---
task_id: TASK-001
agent: reviewer-1
model: opus
status: APPROVE
iteration: 2
inputs:
  - specs/constitution.md
  - specs/feature-upload/tasks.md
  - tasks/TASK-001/10_context.md
  - tasks/TASK-001/20_design.md
  - tasks/TASK-001/30_impl.md
  - photo-service/** (изменённый код)
outputs:
  - tasks/TASK-001/40_review-1.md
verification:
  - "итер.1: uv run ruff check . -> All checks passed"
  - "итер.1: uv run pytest -q -> 76 passed"
  - "итер.1: repro non-ASCII Content-Disposition -> UnicodeEncodeError (BLOCKING B1)"
  - "итер.2: uv run ruff check . -> All checks passed"
  - "итер.2: uv run pytest -q -> 87 passed"
  - "итер.2: repro _content_disposition -> все кейсы latin-1-safe, синтаксис валиден, filename* сохранён"
timestamp: 2026-07-13T00:00:00Z
---

> **ИТОГОВЫЙ СТАТУС (итерация 2): APPROVE.** Ниже сохранено исходное ревью
> итерации 1 (было CHANGES_REQUESTED) для истории; раздел «Re-review итерации 2»
> в конце фиксирует закрытие блокера B1 и NON-BLOCKING N1.

# TASK-001 — Ревью-1 (корректность/архитектура)

## Вердикт (итерация 1 — исторически)

**status: CHANGES_REQUESTED** — 1 BLOCKING, несколько NON-BLOCKING.

Реализация в целом качественная и точно следует дизайну: слоистость соблюдена, порядок upload
БД→MinIO→commit с rollback+503 корректен, миграция синхронна с моделью, `create_type=False`
сохранён, request_id — pure-ASGI. Однако `GET /{id}/content` падал 500 на валидном нелатинском
имени файла — реальный сценарий для «Облако Mail».

Прогон в среде (итер.1): `ruff` чисто, `pytest` 76 passed.

## Сильные стороны

- Слоистость держится: `api/photos.py` не импортирует `select`/модели/SDK для вызовов MinIO; SQL
  только в `photo_repository.py`; commit/rollback только в сервисе; storage sync, обёртка
  `anyio.to_thread` — на стороне сервиса.
- Порядок upload и откат по §8: `create`(add+flush, без commit) → `save_file` под
  `asyncio.timeout(60)` в потоке → `rollback`+`StorageUnavailable(503)` при сбое/таймауте → `commit`.
  Дубликат ловится на flush до заливки байтов (`IntegrityError`→`ConflictError`→409), защита от
  дублей на уровне БД (PK+UNIQUE).
- Миграция v001 1:1 с моделью, downgrade корректный, `create_type=False` сохранён (баг TASK-000 не вернулся).
- request_id middleware pure-ASGI, contextvar виден downstream, эхо `X-Request-ID`, reset в finally.
- Валидация по magic bytes по содержимому, порядок пусто→400 / >50МБ→413 / не img→415.
- get_file закрывает соединение в finally — нет утечки дескриптора.

## BLOCKING (итерация 1 — ЗАКРЫТ в итерации 2)

### B1. `GET /v1/photos/{id}/content`: нелатинское имя файла → UnicodeEncodeError → 500
- Файл: `photo-service/app/api/photos.py:67` (итер.1).
- Проблема: сырое пользовательское `filename` в `Content-Disposition`; Starlette кодирует заголовки
  в latin-1 → `фото.jpg` вызывал `UnicodeEncodeError` → HTTP 500 вместо байтов; `"` ломал синтаксис.
- Рекомендация: RFC 6266/5987 — ASCII-fallback `filename="..."` + `filename*=UTF-8''<quote>`.
- **Статус: закрыт в итерации 2 (см. ниже).**

## NON-BLOCKING

### N1. Нет catch-all обработчика для не-`AppError` → единый формат теряется на 500 — ЗАКРЫТ в итер.2
- Файл: `photo-service/app/core/errors.py` (`register_exception_handlers`).
- Рекомендация: `@app.exception_handler(Exception)` с телом `INTERNAL_ERROR` + `logger.exception`.
- **Статус: реализован в итерации 2 (см. ниже).**

### N2. Полное чтение файла в память до проверки размера (DoS-вектор)
- Осознанный компромисс дизайна (§5/§13). Отложено оркестратором. Повторно не поднимается.

### N3. Отмена `asyncio.timeout` не прерывает фоновый поток `to_thread` (orphan-риск)
- Неизбежное свойство `to_thread`, покрыто «осознанным orphan-риском» §8. Отложено. Повторно не поднимается.

### N4. `PhotoRepository.list` затеняет builtin `list` — соответствует §4.2, стилистика.

### N5. Модель `status` без `server_default`, миграция — с `server_default='pending'` — безвредно (схему создаёт alembic).

## Чек-лист соответствия дизайну / DoD (итог итерации 2)

- [x] 4 эндпоинта под `/v1/photos` + `/healthz`/`/readyz`; upload = 202 `UploadPhotoResponse`.
- [x] Коды ошибок 400/413/415/404/409/503 в нужных местах.
- [x] 500: теперь единый формат и для не-`AppError` (catch-all, N1 закрыт).
- [x] Слоистость: api без SQL/прямого MinIO; SQL только в репозитории; в БД только object_key.
- [x] Транзакции: БД(flush)→MinIO→commit; сбой MinIO → rollback+503; UNIQUE→409.
- [x] Идемпотентность через PK+UNIQUE (уровень БД).
- [x] Async: нет time.sleep; MinIO в `anyio.to_thread`; `asyncio.timeout(60)`.
- [x] Миграция v001 1:1 с моделью; `create_type=False`; downgrade ок.
- [x] request_id: pure-ASGI, долетает в хендлер/сервис/логи, эхо в заголовке.
- [x] content endpoint: Content-Type верный, дескриптор не течёт, нелатинское имя больше не 500 (B1 закрыт).
- [x] Валидация magic bytes; порядок 400→413→415.
- [x] `.proto` контракт добавлен; стабы не генерятся; `AnalyzerClient` не вызывается.
- [x] `ruff` чисто; `pytest` 87 passed.

---

## Re-review итерации 2

**status: APPROVE.** Проверялись только 2 изменённых файла кода (`app/api/photos.py`,
`app/core/errors.py`) + тесты — по указанию оркестратора остальной API повторно не ревьюился.

### B1 — ЗАКРЫТ
- `app/api/photos.py:32-49` — добавлен helper `_content_disposition(filename)`:
  ASCII-fallback (`encode("ascii","ignore")`, снятие `\\` и `"`, пусто→`"download"`) плюс
  `filename*=UTF-8''<quote(filename, safe="")>`. Вызывается на `:88`.
- Репродукция моим сниппетом из ревью-1 подтверждает фикс: для кейсов
  `фото.jpg`, `a"; x="b`, `back\slash.png`, `全部.png`, `""`, `normal.jpg` результат
  **всегда `.encode("latin-1")`-безопасен** (UnicodeEncodeError больше нет → 200, не 500),
  ASCII-часть синтаксически валидна (ровно 2 кавычки, инъекции `"`/`\` вырезаны),
  оригинальное имя сохранено в `filename*`. Пустое имя → `filename="download"`.
- Тесты `tests/test_photos_content_disposition.py` (кириллица/кавычки/backslash/полностью не-ASCII
  + e2e через `dependency_overrides`) проходят.
- Слоистость не нарушена: добавлен только `from urllib.parse import quote` (stdlib), ORM/SDK в api не подтянуты.

### N1 — реализовано корректно
- `app/core/errors.py:109-121` — `@app.exception_handler(Exception)` возвращает единый
  `{error_code:"INTERNAL_ERROR", message:"Internal server error", request_id}` со статусом 500,
  логирует через `logger.exception` без утечки деталей исключения в тело ответа.
- Роутинг корректен: `AppError`-подклассы по MRO матчатся на более специфичный `_handle_app_error`
  раньше `Exception`; `RequestValidationError` (422) имеет собственный FastAPI-хендлер и не
  перехватывается catch-all. Тесты `test_errors.py` (включая `TestCatchAllHandler`) это подтверждают.
- Нюанс с `raise_app_exceptions=False` в тесте — это ограничение `httpx.ASGITransport`
  (Starlette ServerErrorMiddleware переподнимает исключение после ответа); реальные клиенты
  получают корректный JSON. Задокументировано, поведение сервиса корректно.

### Проверки итерации 2
- `uv run ruff check .` → All checks passed.
- `uv run pytest -q` → 87 passed (+11 к итерации 1).
- Репро `_content_disposition` — все кейсы latin-1-safe, синтаксис валиден, `filename*` присутствует.

### Итог
Блокер B1 закрыт, N1 закрыт корректно, новых блокеров и регрессий слоистости нет.
N2/N3 осознанно отложены оркестратором в TASK-заметку. **APPROVE.**

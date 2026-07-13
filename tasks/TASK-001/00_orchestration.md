# TASK-001 — Публичный API работы с фотографиями

**Статус:** in_progress
**Цель:** довести каркас TASK-000 до рабочего MVP, закрыть DoD Воркшопа 2.
**Спека/чеклист:** `specs/feature-upload/tasks.md` (раздел TASK-001).
**База:** код каркаса из TASK-000 (`photo-service/`), сейчас на ветке feat/task-000-bootstrap (PR #2 не смёржен).

## Зафиксированные решения (обязательны)
- Префикс: `/v1/photos` (не /api/v1). Порт: 8000. Статусы: pending→processing→done→failed.
- Поле пути MinIO: `object_key` (не s3_path). Успех upload: HTTP 202.

## Эндпоинты
POST /v1/photos · GET /v1/photos · GET /v1/photos/{id} · GET /v1/photos/{id}/content · GET /healthz (есть).
Плюс: единый формат ошибок (415/413/404/503/500), JSON-логи с photo_id+request_id, repository-слой, asyncio.timeout, UNIQUE+транзакция, .proto контракт analyzer-а.

## Правки каркаса TASK-000
Префикс роутера; enum queued/analyzing/done/error → pending/processing/done/failed (модель + миграция v001); s3_path → object_key.

## Вне скоупа
Kafka, worker, вызов analyzer, Prometheus/Grafana, K8s, gRPC-вызов (только .proto).

## Журнал вызовов

| Шаг | Агент | Артефакт | Статус |
|-----|-------|----------|--------|
| 1 | context-collector | 10_context.md | ✅ done |
| 2 | architect | 20_design.md | ✅ done (15 шагов) |
| 3 | coder | 30_impl.md + код | ✅ done — 76 passed, ruff clean, enum sync |
| 4 | reviewer-1 ∥ reviewer-2 | 40/41 | ✅ r2 APPROVE, r1 CHANGES_REQUESTED (1 BLOCKING) |
| 5 | coder (итерация 2) | 30_impl.md | ✅ B1+N1 исправлены — 87 passed, ruff clean |
| 6 | reviewer-1 (re-review) | 40_review-1.md | ✅ APPROVE — B1+N1 закрыты |
| 7 | test-writer | 50_tests.md + тесты | ✅ 173 passed / 1 skipped, cov 99% app/, ruff clean |
| 8 | pr-publisher | 70_pr.md | ✅ done — PR #3 опубликован |

**Статус TASK-001:** все проектные гейты пройдены (2×APPROVE, тесты зелёные, coverage 99% ≥85%). Осталось: (а) живой `docker compose down -v && up --build` + curl-сценарий + `pytest -m integration` у пользователя; (б) разрешение пользователя на PR.
**Гигиена для pr-publisher:** `.coverage` уже отслеживается в git с TASK-000 (по замечанию test-writer) — при коммите TASK-001 сделать `git rm --cached .coverage` и убедиться, что он в .gitignore.

**ГЕЙТ РЕВЬЮ ПРОЙДЕН:** оба APPROVE (r1 opus + r2 haiku). 87 тестов зелёные на момент передачи. → test-writer: полное функциональное покрытие эндпоинтов + интеграционный тест миграции (testcontainers, требует Docker — в песочнице skip, у пользователя запустится).

**Итерация 2:** B1 — `_content_disposition()` RFC 6266/5987 (ASCII-fallback + filename*=UTF-8''), всегда latin-1-safe; +тесты кириллица/кавычки. N1 — `@app.exception_handler(Exception)` → 500 в едином формате {error_code:INTERNAL_ERROR,message,request_id}. Затронуто только app/api/photos.py + app/core/errors.py + 2 тест-файла. 76→87 passed.

**Гейт ревью:** НЕ пройден (r1). Итерация правок №1.
**B1 (BLOCKING, app/api/photos.py:67):** Content-Disposition подставляет нелатинское filename сырьём → UnicodeEncodeError → 500. Фикс: RFC 5987 (filename*=UTF-8'') + ASCII-fallback + экранирование кавычек.
**N1 (взят в работу):** нет catch-all handler на не-AppError → непредвиденный 500 без единого тела {error_code,message,request_id}. Усиливает DoD п.5. Фикс: @app.exception_handler(Exception).
**N2/N3 → TASK-заметка (не блок):** файл читается в память до проверки 50МБ (DoS-вектор); orphan-объект MinIO при таймауте to_thread.

**Реализовано:** 4 эндпоинта /v1/photos, PhotoService/PhotoRepository, request_id ASGI-middleware (app/api/middleware.py), правка v001 in-place (enum pending/processing/done/failed, object_key, created_at, user_id nullable), .proto (protos/analyzer.proto), коды 413/415, ErrorResponse trace_id→request_id. Deps +python-multipart, +testcontainers[postgres]. Открыто (на test-writer/пользователя): живой docker compose, интеграционный тест миграции, coverage ≥85%.

**Ключевые решения дизайна:** правка v001 (enum pending/processing/done/failed, s3_path→object_key UNIQUE, uploaded_at→created_at, user_id nullable); upload порядок БД(add+flush)→MinIO→commit, rollback+503 при сбое MinIO, 409 конфликт; magic bytes (пусто→400, >50МБ→413, не img→415); request_id pure-ASGI middleware; MinIO sync в anyio.to_thread под asyncio.timeout(60); .proto без стабов; интеграционный тест миграции (testcontainers). Deps: +python-multipart, +testcontainers[postgres] (dev). Риск: ErrorResponse trace_id→request_id — обновить старые тесты TASK-000.

**Указания оркестратора по открытым вопросам (переданы architect):**
- JWT — НЕ делаем (воркшоп исключает); user_id nullable/дефолт, ручки не гейтим авторизацией.
- MIME — проверка magic bytes вручную (JPEG FFD8FF / PNG 89504E47), без системной libmagic; pillow опц.
- Миграция — правим v001 (PR #2 не смёржен, данных нет) + пользователю `down -v`; НЕ v002.
- request_id — добавить HTTP middleware, заполнять trace_id_var/request_id.
- storage save_file/get_file — обернуть блокирующий MinIO SDK в anyio.to_thread.
| 3 | coder | 30_impl.md + код | ожидает |
| 4 | reviewer-1 ∥ reviewer-2 | 40/41 | ожидает |
| 5 | test-writer | 50_tests.md | ожидает |
| 6 | pr-publisher | 70_pr.md | ожидает (разрешение пользователя) |

## Открытые решения для architect
- Править v001 vs новая ревизия v002 для смены enum-значений и s3_path→object_key (зависит от того, смёржен ли PR #2; сейчас не смёржен → вероятно правка v001 + docker compose down -v).
- Ветка/PR TASK-001: отдельная от feat/task-000-bootstrap или продолжение — решить на этапе pr-publisher.

## Следующий шаг
Дождаться 10_context.md → запустить architect.

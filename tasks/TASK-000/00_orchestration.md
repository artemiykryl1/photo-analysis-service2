# TASK-000 — Bootstrap каркаса photo-service

**Статус:** in_progress
**Скоуп:** вертикальный срез Client → API → PostgreSQL/MinIO. Kafka, worker, analyzer — НЕ реализуем (спринт 2).
**Стек:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy async, Alembic, PostgreSQL, MinIO, Docker Compose, uv.

## Требования из ТЗ пользователя (источник истины)

- Слоистая архитектура, строгое разделение: `api/` (HTTP) → `services/` (бизнес-логика) → `repositories/` (доступ к БД) → `db/` (модели, сессии). Хендлер НЕ содержит SQL и не ходит в MinIO напрямую.
- Файлы фото — ТОЛЬКО в MinIO (не в ФС контейнера, не в БД).
- Конфиг только через env/.env: `DATABASE_URL, MINIO_ENDPOINT, MINIO_ACCESS_KEY, MINIO_SECRET_KEY, MINIO_BUCKET, LOG_LEVEL`. Секреты не коммитить, `.env` в `.gitignore`, положить `.env.example`.
- Структура репозитория `photo-service/` (см. дерево в задании).
- docker-compose поднимает `api + postgres + minio`. Dockerfile: `python:3.12-slim` + `uv sync`.
- Пока БЕЗ эндпоинтов, только каркас: `docker compose up` → `GET /healthz` отвечает.
- coder обязан объяснить назначение каждого слоя (2-3 предложения) для защиты на ревью.

## Журнал вызовов

| Шаг | Агент | Артефакт | Статус |
|-----|-------|----------|--------|
| 1 | context-collector | 10_context.md | ✅ done |
| 2 | architect | 20_design.md | ✅ done (20 шагов) |
| 3 | coder | 30_impl.md + код | ✅ done (photo-service/, 32 файла) |
| 4 | reviewer-1 ∥ reviewer-2 | 40/41 | ✅ done: r2 APPROVE, r1 CHANGES_REQUESTED (1 BLOCKING) |
| 5 | coder (итерация 2) | 30_impl.md | ✅ done — B1 исправлен (вариант 1) |
| 6 | reviewer-1 (re-review) | 40_review-1.md | ✅ APPROVE — B1 закрыт |

**ГЕЙТ РЕВЬЮ ПРОЙДЕН:** оба APPROVE (r1 opus + r2 haiku). Каркас TASK-000 готов к переходу на тесты/фиксацию.
**Остаётся ручной пункт:** физический `docker compose up --build` + `curl /healthz`→200 на машине с Docker (в песочнице Docker недоступен; фикс B1 подтверждён эмуляцией слоёв).

| 7 | test-writer | 50_tests.md + тесты | ✅ done — 70 passed, 100% cov app/, ruff clean, багов нет |

**Решение пользователя:** лёгкий test-writer → затем коммит. Полное покрытие upload (coverage ≥85% из spec) отложено на TASK-001.

## Статус TASK-000: ГОТОВ (все гейты пройдены)
- context ✅ → design ✅ → impl ✅ → 2×APPROVE ✅ → tests зелёные ✅
- Ручной пункт: живой `docker compose up --build` на машине с Docker.
- Следующий шаг: коммит каркаса (ждёт решения пользователя по scope: commit-only / commit+PR).

| 8 | pr-publisher | 70_pr.md | запущен — пользователь разрешил Коммит+PR |

**Разрешение пользователя на PR: ДА** (репо artemiykryl1/photo-analysis-service2, base main, ветка feat/task-000-bootstrap, коммитим photo-service/ + tasks/).

**Итерация 2:** B1 исправлен — Dockerfile: `uv sync --no-dev --no-install-project` (слой deps) + `uv sync --no-dev` после `COPY app/`. Подтверждено эмуляцией слоёв через uv. reviewer-2 повторно НЕ гоняем (был APPROVE, Python-код не менялся, правка только в Dockerfile — вне зоны его blocking-замечаний).

**Гейт ревью:** НЕ пройден (r1 CHANGES_REQUESTED). Итерация правок №1.
**B1 (BLOCKING, Dockerfile):** `uv sync` до `COPY app/` → сборка hatchling-пакета падает → `docker compose up --build` не поднимется. Фикс: `--no-install-project` (или reorder / `[tool.uv] package=false`).
**Non-blocking (обоих ревью) → отложены на TASK-001:** рассинхрон server_default статуса модель↔миграция, ручной JSON в /readyz, ensure_bucket как readiness-проба, сигнатура upload_photo(*args,**kwargs), таймаут БД, дефолт MINIO_BUCKET, non-root Dockerfile, dev-креды коммент в compose.

**Проверка coder:** py_compile OK, импорты OK, pytest /healthz→200, ruff clean, `alembic upgrade head --sql` OK. `docker compose up` физически не запускался (Docker недоступен в песочнице) — открытый пункт для проверки на машине с Docker.

## Решения оркестратора

- Задача помечена как TASK-000 (bootstrap), т.к. это прекондиция для feature-upload (TASK-001..004), а не часть самой фичи.
- Architect вставлен несмотря на то, что пользователь назвал только context-collector+coder: гейт CLAUDE.md требует 20_design перед coder. Проход лёгкий — формализация уже данных пользователем требований.
- Kafka/gRPC/analyzer из constitution в этой таске вне скоупа — architect не проектирует их.

## Следующий шаг

Дождаться 10_context.md → запустить architect.

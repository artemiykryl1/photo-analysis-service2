# TASK-002 — Асинхронный конвейер анализа + батчи + наблюдаемость

**Статус:** ✅ DONE — PR #5 опубликован (https://github.com/artemiykryl1/photo-analysis-service2/pull/5), ветка feat/task-002-async-pipeline → main, ожидает мёржа пользователем. Все гейты пройдены: 2×APPROVE, 367 passed/98% cov, живой прогon DoD Воркшопа 3.
**Цель:** закрыть DoD Воркшопа 3 (Kafka → worker → analyzer-stub → done/failed, метрики Prometheus/Grafana) + батчевая загрузка с выбором лучшего кадра.
**Спека:** `specs/feature-upload/tasks.md`, раздел TASK-002 (зафиксированные решения обязательны).
**База:** main после мёржа PR #4 (TASK-001): рабочий API /v1/photos, PostgreSQL+MinIO, миграция v001, protos/analyzer.proto.

## Ключевые решения из спеки (напоминание для агентов)

- Топик: `photo.analysis.requested`; топика результатов НЕТ (worker пишет в PostgreSQL напрямую).
- Сообщение: {photo_id, object_key, created_at, trace_id} — без файла.
- Analyzer = заглушка (отдельный gRPC-контейнер по protos/analyzer.proto, детерминированные результаты от object_key); реальная нейронка — TASK-003, смена ANALYZer_GRPC_ADDR.
- Гонки: атомарный UPDATE pending→processing. Offset-коммит после обработки. Retry только временных ошибок, лимит 3, потом failed + last_error_*.
- Outbox: upload не падает при лежащей Kafka (not_sent → фоновый publisher → sent).
- Батч: 2–10 файлов, невалидный файл = отклонить весь батч атомарно. Лучший кадр: is_blurred asc → blur_score asc → faces_count desc → created_at asc, только done; все failed → null.
- Kafka: 1 брокер KRaft; библиотека aiokafka. Метрики: имена со слайдов 34–35 + gauge photos_pending.

## Журнал вызовов

| Шаг | Агент | Артефакт | Статус |
|-----|-------|----------|--------|
| 1 | context-collector | 10_context.md | ✅ done |
| 2 | architect | 20_design.md | ✅ done (30 шагов: A конвейер 1–18, B батчи 19–25, C метрики 26–30) |
| 3 | coder (заход 1: блок A) | 30_impl.md + код | ✅ done — 173 passed, ruff clean, alembic v002 offline OK (агент прерван на отчёте, отчёт дописан оркестратором по факту диффа + гейты) |
| 3b | coder (заход 2: блоки B+C) | 30_impl.md + код | ✅ done — 173 passed, ruff clean, compose config OK |
| 4 | reviewer-1 ∥ reviewer-2 | 40/41 | ✅ оба CHANGES_REQUESTED — r1: 1 BLOCKING (B1 outbox); r2: 3 BLOCKING (Grafana pw, payload validation, метрика) |
| 5 | coder (итерация правок 1) | 30_impl.md | ✅ done — BLK-1..4 + M-A..D, гейты зелёные (173 passed, ruff clean, compose OK) |
| 6 | reviewer-1 + reviewer-2 (re-review) | 40/41 | ✅✅ ОБА APPROVE — reviewer-1 (opus) B1 закрыт, reviewer-2 (haiku) 3 BLK закрыты. ГЕЙТ РЕВЬЮ ПРОЙДЕН |
| 7 | test-writer | 50_tests.md + тесты | ✅ done — 367 passed / 1 integration skip, coverage 98% app/, ruff clean, багов нет |
| 8 | test-debugger | 60_debug.md | ✅ done — фикс `from __future__ import annotations` в photo_repository.py; подтверждён на 3.12 (api+worker Up, /metrics 200); 367 passed |
| 9 | pr-publisher | 70_pr.md | ✅ done — PR #5 открыт (feat/task-002-async-pipeline → main), 64 файла |

## Пост-ревью раунд (живой ревьюер воркшопа, после PR #5) — коммит 3c0ccf5
- FIX-1 (M7): exec в command api/worker → SIGTERM доходит до uvicorn/worker (graceful shutdown в контейнере).
- FIX-2: docstring consumer.py приведён в соответствие с поведением передоставки (дедуп, не восстановление; stuck-processing = известный пробел, reaper в TASK-003).
- AGENTS.md добавлен (импорт CLAUDE.md + ростер агентов).
- Гейты после правок: ruff clean, 367 passed, compose config OK. Запушено в ветку PR #5.
- Осталось на пользователе: подписи на схеме ВК Доски (референс-диаграмма выдана), затем зачёт.

## Живой прогон (docker compose, 8 контейнеров) — ✅ ПРОЙДЕН
- Одиночное фото: POST → 202 pending → worker сам довёл до done с analysis (faces/blur/hash).
- Батч 3 фото: POST /v1/photos/batch → 202 → completed, best_photo_id верный по формуле (b2 blur 0.236 победил).
- Метрики: API /metrics (photos_uploaded_total 4, photos_pending 0, http_requests_total шаблонизирован), worker :8001 (photo_analysis_completed_total 4, worker_messages_processed_total{done}=4).
- Grafana дашборд (4 графика) живой. Failed analyses «No data» — корректно (0 отказов).
- Runtime-баг 3.12 (list-shadow) пойман и починен до прогона конвейера.

## RUNTIME-БАГ (найден живым прогоном, docker compose up)

api и worker Exited(1) при старте. Трейс: `class PhotoRepository` → `async def fetch_unpublished(...) -> list[Photo]` → `TypeError: 'function' object is not subscriptable` (photo_repository.py:150).
**Причина:** метод `PhotoRepository.list` (из TASK-001) затеняет встроенный `list` в аннотациях методов, объявленных ПОСЛЕ него (`fetch_unpublished` и др. из TASK-002). На Python 3.12 (Docker-образ) аннотации вычисляются жадно при импорте → краш. На Python 3.14 (локаль пользователя) аннотации ленивые (PEP 649) → тесты 367 passed НЕ ловят. Аналог бага двойного CREATE TYPE из TASK-000: только живой прогон на реальном окружении. → test-debugger.
| 5 | test-writer | 50_tests.md | ожидает |
| 6 | test-debugger | 60_debug.md | при падениях |
| 7 | pr-publisher | 70_pr.md | ожидает (разрешение пользователя) |

## Решения оркестратора

- Таска крупная (3 блока: конвейер, батчи, наблюдаемость), но идёт одним конвейером — блоки сильно связаны (worker пишет результаты, которые нужны батчам; метрики размазаны по API и worker). Если coder упрётся в объём — допустима поэтапная сдача внутри одной таски (A → B → C) с одним общим ревью.
- Спека TASK-002 в specs/feature-upload/tasks.md пока не закоммичена (правки на main локально) — уйдёт в коммит вместе с реализацией на ветке feat/task-002-async-pipeline.

## Принятые решения архитектора (сверх спеки, одобрены оркестратором)

- Колонка `trace_id` в photos — фоновому publisher-у нужен trace_id вне HTTP-контекста.
- Outbox: publish_status (not_sent/sent VARCHAR+CHECK, без нового PG-enum) + published_at; публикация ТОЛЬКО из фоновой задачи lifespan (Kafka не в HTTP-пути вообще).
- gRPC-стабы закоммичены в app/grpc_gen/ (генерация при сборке ломала бы локальные тесты); grpcio-tools в dev-deps.
- analyzer-stub: своя папка + свой Dockerfile с codegen при сборке.
- v002 без новых PG-enum (VARCHAR+CHECK) — устраняет класс бага DuplicateObjectError.
- Известное ограничение: смерть worker-а между захватом processing и терминалом оставляет фото в processing; reaper — TASK-003.

## Следующий шаг

coder блок A (шаги 1–18) → coder блоки B+C (19–30) → параллельное ревью по всему диффу.

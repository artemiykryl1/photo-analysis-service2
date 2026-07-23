---
task_id: TASK-002
agent: test-writer
model: sonnet
status: done
inputs:
  - specs/constitution.md
  - specs/feature-upload/tasks.md (§"TASK-002")
  - tasks/TASK-002/20_design.md (§12 step 22, §13.2 критичные тест-цели)
  - tasks/TASK-002/30_impl.md (блоки A/B/C + итерация правок 1)
  - tasks/TASK-002/40_review-1.md (APPROVE, раунд 2)
  - tasks/TASK-002/41_review-2.md (APPROVE, iteration 2)
  - рабочее дерево photo-service/ (весь код блоков A/B/C)
outputs:
  - photo-service/tests/*.py (15 новых файлов + правки 3 существующих)
  - tasks/TASK-002/50_tests.md (этот файл)
gates_run:
  - "uv run ruff check . -> All checks passed!"
  - "uv run pytest -q -m \"not integration\" -> 367 passed, 1 deselected"
  - "uv run pytest -q -> 367 passed, 1 skipped (интеграционный тест миграции, Docker недоступен в этой песочнице)"
  - "uv run pytest -q -m \"not integration\" --cov=app --cov-report=term-missing -> TOTAL 98% (970 stmts, 20 miss)"
timestamp: 2026-07-18
---

# TASK-002 — Тесты (50_tests.md)

## Итог одной строкой

**367 passed, 0 failed, 1 skipped/deselected** (интеграционный тест миграции — требует Docker, недоступен в песочнице, но написан и корректно скипается). **Coverage app/ = 98%** (цель ≥85% перевыполнена). `ruff check .` чист. Реального бага в коде не найдено — только одно наблюдение (не баг, см. «Найденные проблемы»).

---

## Новые тестовые файлы (15) и что покрывают

| Файл | Тестов | Покрывает |
|------|--------|-----------|
| `tests/test_batch_service.py` | 17 | `select_best_photo` (чистая функция, все тай-брейки по порядку, включая финальный `photo_id`), `BatchService.get_batch` (404, processing/completed, write-through идемпотентность, all-failed→null, пустой батч) |
| `tests/test_analyzer_client.py` | 25 | `classify_grpc_error` (все retryable/no-retry gRPC-коды + TimeoutError + неожиданный класс), `error_code_from_exception`, `truncate_error_message`, конструкция/close `AnalyzerGrpcClient` (канал/стаб мокнуты) |
| `tests/test_analysis_processor.py` | 11 | `AnalysisProcessor.process`: rowcount=0→skip; success с 1-й/2-й попытки; retry-исчерпание→failed(retries_exhausted); NO_RETRY→failed сразу; неожиданное исключение→NO_RETRY fail-safe; порядок commit'ов; backoff-паузы между попытками |
| `tests/test_kafka_producer.py` | 12 | `ensure_started` (ленивый старт, идемпотентность, race-safety под конкурентным вызовом, сброс producer при неудаче и self-heal), `stop`, `publish_analysis_requested` (JSON-payload, RuntimeError без ensure_started, проброс ошибок) |
| `tests/test_outbox.py` | 11 | `run_outbox_publisher`: not_sent→sent; Kafka/timeout-ошибка оставляет not_sent + инкремент `kafka_publish_errors_total`; одна неудачная строка не блокирует остальные; `ensure_started()`-провал не роняет цикл; CancelledError пробрасывается; неожиданный Exception логируется, не роняет цикл; TimeoutError между поллами — штатная ветка |
| `tests/test_worker_consumer.py` | 21 | `_is_missing`; poison-pill (невалидный JSON/не-UTF8/JSON-массив)→skip без исключения; отсутствие/пустота `photo_id`/`object_key`→skip без KeyError; валидное сообщение→`processor.process` с trace_id из payload и его сброс после; `consume_loop` — commit строго после process, несколько сообщений, stop_event уже установлен, poll-timeout без сообщения |
| `tests/test_worker_main.py` | 6 | `_install_shutdown_handlers` (SIGTERM/SIGINT через `add_signal_handler`, Windows-фолбэк на `signal.signal`), `_run()` — сборка (metrics-сервер, gRPC-клиент, consumer с правильным topic/group), teardown в `finally` даже при исключении из `consume_loop`, `main()`→`asyncio.run` |
| `tests/test_batches_endpoints.py` | 14 | HTTP-контракт `POST /v1/photos/batch` (202, тело, форвардинг файлов) и `GET /v1/batches/{id}` (200 processing/completed, analysis, 404, 422); маппинг ошибок (`BatchSizeError`→400 INVALID_BATCH_SIZE, 413/415/503) |
| `tests/test_photo_service_batch.py` | 18 | `PhotoService.create_batch`: размер батча 2..10 (границы + вне диапазона); невалидный файл в любой позиции батча отклоняет весь батч атомарно (0 записей в repo/storage); `BATCH_MAX_TOTAL_BYTES` (413, до любой записи); happy path (общий `batch_id`, уникальные `object_key`, один commit); откат при сбое MinIO + best-effort delete уже сохранённых объектов |
| `tests/test_photo_repository_analysis_pipeline.py` | 11 | `claim_for_processing` (rowcount 0/1, ЗАФИКСИРОВАННЫЙ предикат `WHERE status='pending'` в скомпилированном SQL, не коммитит сам), `record_attempt`, `mark_done` (чистит `last_error_*`), `mark_failed`, `fetch_unpublished`, `mark_published`, `count_pending` |
| `tests/test_batch_repository.py` | 7 | `BatchRepository.create/get_by_id/mark_completed` (компилированный SQL, `session.get` с eager-load опциями, None-safe best_photo_id) |
| `tests/test_analysis_result_repository.py` | 3 | `upsert` — `INSERT ... ON CONFLICT (photo_id) DO NOTHING` (идемпотентность записи результата) |
| `tests/test_metrics_middleware.py` | 5 | RED HTTP-метрики: инкремент `http_requests_total` с лейблом-шаблоном маршрута (не сырой `photo_id` — защита от кардинальности), 404-статус, unmatched-роут→`endpoint="unmatched"`, наблюдение в `http_request_duration_seconds` |
| `tests/test_metrics_endpoint.py` | 6 | `GET /metrics`: 200 text/plain, наличие всех заявленных имён метрик, `photos_pending` отражает `count_pending`, скрейп никогда не 500 при ошибке БД, gauge сохраняет последнее значение при сбое, таймаут-бюджет соблюдается |
| `tests/test_analyzer_stub.py` | 19 | Детерминизм `analyzer-stub/server.py::_analyze` (загружается напрямую через importlib, реальный код, не дублирование логики): один `object_key`→один результат; `faces_count∈[0,5]`; `blur_score∈[0,1]`; `is_blurred == (blur_score>0.6)`; `perceptual_hash` — 16 hex-символов |

## Правки существующих файлов

| Файл | Что добавлено |
|------|----------------|
| `tests/test_config.py` | `TestTaskO02Defaults` (5 тестов): дефолты Kafka/analyzer/worker/outbox-полей Settings, переопределение через env |
| `tests/test_errors.py` | `BatchSizeError` добавлен в параметризованные проверки маппинга статус-кода (400 `INVALID_BATCH_SIZE`) и в тест иерархии `AppError` |
| `tests/test_lifespan.py` | 2 новых теста: outbox-задача, застрявшая дольше `OUTBOX_SHUTDOWN_TIMEOUT_SECONDS`, корректно отменяется при shutdown (не виснет); падение `producer.stop()` логируется и не мешает `engine.dispose()` |

`tests/test_migration_integration.py`, `tests/test_photo_service.py`, `tests/test_photos_endpoints.py` были уже расширены кодером под TASK-002 (миграция v002, поле `analysis`, `publish_status`/`trace_id`) — не редактировались повторно, только прогнаны и проверены на зелёный статус.

---

## Матрица: критерий приёмки → тест

Источник критериев: `specs/feature-upload/tasks.md` §TASK-002 «Критерии приёмки» + design §13.2.

| # | Критерий приёмки | Тест(ы) |
|---|-------------------|---------|
| 1 | `POST /v1/photos` → фото в `pending` | `tests/test_photo_service.py::TestCreatePhotoHappyPath` (существует, не менялся) |
| 2 | Kafka получила сообщение `photo.analysis.requested` (через outbox) | `tests/test_kafka_producer.py::TestPublishAnalysisRequested::test_publishes_expected_json_payload_keyed_by_photo_id`; `tests/test_outbox.py::TestSuccessfulPublish` |
| 3 | Worker прочитал и обработал сообщение (атомарный захват, идемпотентность дублей) | `tests/test_photo_repository_analysis_pipeline.py::TestClaimForProcessing` (предикат `WHERE status='pending'`); `tests/test_analysis_processor.py::TestClaimSkip` (rowcount=0→skip, gRPC не вызывается) |
| 4 | Analyzer(-stub) вернул результат по gRPC | `tests/test_analyzer_client.py::TestAnalyzerGrpcClient` (вызов stub с timeout/полями запроса); `tests/test_analyzer_stub.py` (детерминизм реального `_analyze`) |
| 5 | PostgreSQL обновился: `analysis_results` + `done` (или `failed`+`last_error_*` после ретраев) | `tests/test_analysis_processor.py::TestSuccessPath`, `TestRetryExhaustion`, `TestNoRetryPath`; `tests/test_analysis_result_repository.py::TestUpsert`; `tests/test_photo_repository_analysis_pipeline.py::TestMarkDone/TestMarkFailed` |
| 6 | `GET /v1/photos/{id}` → `status: done` c заполненным `analysis` | `tests/test_photos_endpoints.py::TestGetPhotoStatus` (существует, `analysis` в теле); `tests/test_photo_service.py::TestGetPhoto` |
| 7 | `POST /v1/photos/batch` (2–10 фото) → 202 с `batch_id`; `GET /v1/batches/{id}` → `completed`, все фото с результатами, `best_photo_id` по формуле | `tests/test_batches_endpoints.py::TestUploadBatchHappyPath/TestGetBatch`; `tests/test_photo_service_batch.py`; `tests/test_batch_service.py::TestSelectBestPhotoTieBreakOrder/TestGetBatch` |
| 8 | `/metrics` отдаёт метрики в API и worker | `tests/test_metrics_endpoint.py` (API); worker-метрики инкрементируются в `tests/test_analysis_processor.py` косвенно (импортируются реальные Counter/Histogram, не мокнуты) + `tests/test_worker_main.py::test_run_starts_metrics_server...` (`start_http_server` вызывается с портом) |
| 9 | Grafana показывает дашборд (≥4 графика) | Вне юнит/интеграционного контура тестов (визуальная проверка) — `grafana/dashboards/photo-service.json` валидность JSON и PromQL-строки не тестировались автоматически (см. «Что не покрыто») |
| 10 | Upload не падает при остановленной Kafka; после её старта фото дообрабатываются (outbox) | `tests/test_outbox.py::TestProducerNotStarted` (`ensure_started()`-провал не роняет цикл); `tests/test_kafka_producer.py::TestEnsureStarted` (self-heal после сбоя); `tests/test_photo_service.py` (upload не обращается к Kafka вовсе — существующий тест) |
| 11 (доп. гейт) | Два APPROVE | Подтверждено — `40_review-1.md`/`41_review-2.md` оба `status: APPROVE` |
| 12 (доп. гейт) | Тесты покрывают критерии; coverage ≥85% | Этот файл; **98%** фактически |
| 13 (доп. гейт) | Юнит-тесты формулы best/классификации retry/идемпотентности/outbox | Все покрыты выше (см. п.3, п.5, п.7, п.10) |
| 14 (доп. гейт) | Интеграционный тест миграции v002 (testcontainers) | `tests/test_migration_integration.py` (расширен кодером; прогнан здесь — **skipped**, Docker недоступен в песочнице; тест написан и валиден, содержит проверку upgrade head + downgrade v002→v001→base + все новые таблицы/колонки/индексы/CHECK) |
| 15 (доп. гейт) | Живой прогон `docker compose up --build` | Вне зоны ответственности test-writer — ручная проверка пользователем |

Дополнительно из §13.2 design, сверх таблицы выше:
- Формула лучшего кадра — **все ветки**: is_blurred→blur_score→faces_count→created_at→photo_id, все done, все failed→None, пусто, смешанный батч, один done — `tests/test_batch_service.py`.
- Классификация retry — **каждый** gRPC-код таблицы §5.4 отдельным параметром — `tests/test_analyzer_client.py`.
- Батч-валидация — <2/>10, невалидный файл на любой позиции, суммарный размер >`BATCH_MAX_TOTAL_BYTES` — `tests/test_photo_service_batch.py`.
- Детерминизм analyzer-stub — `tests/test_analyzer_stub.py`.
- Ретрай-цикл процессора — RETRY N раз→attempts растёт→failed; NO_RETRY сразу; success со 2-й попытки — `tests/test_analysis_processor.py`.

---

## Что покрыто / не покрыто

**Покрыто (юнит + функционально, без реального Postgres/Kafka/MinIO/gRPC-сервера):**
- Вся бизнес-логика блоков A/B/C: claim/retry/persist, outbox-переходы, producer self-heal, consumer payload-валидация, формула best, батч-атомарность, метрики (RED + доменные), `/metrics`-эндпоинт, graceful shutdown API и worker.
- Реальный код `analyzer-stub/server.py::_analyze` (не дублирование формулы в тесте — импорт настоящего файла).
- HTTP-контракты для всех новых/изменённых эндпоинтов (`/v1/photos/batch`, `/v1/batches/{id}`, `/v1/photos/{id}` с `analysis`, `/metrics`).

**Не покрыто (осознанно, вне зоны юнит/функциональных тестов):**
- **Реальный Kafka-брокер / реальный Postgres / реальный gRPC-сервер / реальный MinIO** — по условию задачи ("моки/фикстуры для внешних зависимостей; реальный анализатор не зови") и по общему стилю существующих тестов проекта (TASK-001). Живой end-to-end сценарий (`docker compose up --build` → батч → done → best_photo_id → графики Grafana) остаётся ручной проверкой пользователя, как зафиксировано в spec.md.
- **Интеграционный тест миграции v002** (`tests/test_migration_integration.py`) — написан кодером, прогнан здесь: **skipped**, т.к. Docker-демон недоступен в этой песочнице (`docker ps` → `failed to connect to the Docker API`). Тест корректно самоскипается через `pytest.mark.skipif` (тот же паттерн, что и в TASK-001) — не красный, а осознанно пропущенный. Нужно прогнать отдельно с доступным Docker перед мержем (команда — в шапке файла).
- **Grafana-дашборд** (`grafana/dashboards/photo-service.json`) — валидность JSON и текстовое совпадение PromQL со design §9.2 проверялись кодером вручную (`30_impl.md`), не автоматизированным тестом; визуальный рендеринг в реальной Grafana не проверялся никем (это уже отмечено кодером как открытый вопрос reviewer'ам).
- Сгенерированный protobuf-код `app/grpc_gen/analyzer_pb2*.py` (63%/60% coverage) — не тестируется напрямую (это `DO NOT EDIT` генерируемый код, исключён из `ruff` тем же способом в `pyproject.toml`); он транзитивно упражняется через `test_analyzer_client.py` и `test_analyzer_stub.py`, непокрытые строки — версионные проверки/сервисные хелперы grpc-плагина, не бизнес-логика.
- `app/worker/main.py:99` (`if __name__ == "__main__": main()`) — тривиальный entrypoint-guard, не несёт логики.

---

## Как запустить

```bash
cd photo-service

# Юнит + функциональные тесты (без Docker):
uv run pytest -q -m "not integration"

# Линт:
uv run ruff check .

# Покрытие:
uv run pytest -q -m "not integration" --cov=app --cov-report=term-missing

# Интеграционный тест миграции v002 (нужен запущенный Docker daemon):
uv run pytest -m integration -q
```

## Результат прогона

```
uv run ruff check .
  -> All checks passed!

uv run pytest -q -m "not integration"
  -> 367 passed, 1 deselected, 1 warning in ~13s

uv run pytest -q   (без фильтра — интеграционный тест сам решает скипаться)
  -> 367 passed, 1 skipped, 1 warning in ~13s
     (docker ps подтверждён недоступным в этой песочнице -> легитимный skip)

uv run pytest -q -m "not integration" --cov=app --cov-report=term-missing
  -> TOTAL: 970 stmts, 20 miss, 98% covered
     (все app/*.py файлы бизнес-логики — 100%; неполное покрытие только у
      сгенерированного grpc_gen/*_pb2*.py и тривиального worker/main.py:99)
```

Стабильность: полный набор `-m "not integration"` прогнан 3 раза подряд — 367 passed каждый раз (без флейков), включая новый тест на гонку `ensure_started()` (`tests/test_kafka_producer.py`), прогнанный отдельно 5 раз подряд для проверки детерминизма конкурентного сценария под pytest-asyncio.

---

## Найденные при написании проблемы

Не найдено настоящих багов, требующих возврата кодеру. Одно наблюдение, зафиксированное для полноты картины (не блокер, не входит в критерии приёмки):

- **Медленные существующие тесты `tests/test_lifespan.py`** (не мной введено — уже было в базовой линии): 4 теста в этом файле не мокают `KafkaEventProducer`, поэтому реальный `producer.start()` пытается резолвить хост `kafka:9092` и падает по таймауту DNS/connect ~2.7s на тест (итого ~11s из ~13s общего времени прогона всего набора тестов уходит на этот файл). Не влияет на корректность (тест по-прежнему ассертит "не падает"), только на скорость CI. Два новых теста, добавленных здесь (`test_lifespan_cancels_outbox_task_when_shutdown_exceeds_timeout`, `test_lifespan_swallows_producer_stop_failure_and_still_disposes_engine`), сознательно мокают `KafkaEventProducer`, поэтому выполняются за <0.05s каждый — можно было бы так же поправить 4 существующих теста, но это не входит в мандат test-writer (не трогать чужой существующий код без необходимости) и не критично для гейтов.

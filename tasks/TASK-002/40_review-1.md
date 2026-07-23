---
task_id: TASK-002
agent: reviewer-1
model: opus
status: APPROVE
review_round: 2
inputs:
  - specs/constitution.md
  - specs/feature-upload/tasks.md (§"TASK-002")
  - tasks/TASK-002/20_design.md
  - tasks/TASK-002/30_impl.md
  - рабочее дерево photo-service/ (весь дифф блоков A/B/C)
gates_run:
  - "uv run ruff check . -> All checks passed! (раунды 1 и 2)"
  - "uv run pytest -q -m \"not integration\" -> 173 passed, 1 deselected (раунды 1 и 2)"
blocking_count: 0
timestamp: 2026-07-18
---

# TASK-002 — Ревью 1 (корректность и архитектура)

> **Раунд 2 (после итерации правок №1): status APPROVE.** Блокер B1 закрыт, майноры M1–M3 закрыты.
> Верификация — в разделе «Re-review (раунд 2)» в конце документа. Ниже сохранён исходный отчёт
> раунда 1 (когда был выставлен CHANGES_REQUESTED) для истории.

## Резюме (раунд 1)

Реализация в целом добротная и близко следует дизайну: атомарный claim, at-least-once с
commit offset после обработки, идемпотентный upsert результата (`ON CONFLICT DO NOTHING`),
классификация retry/no-retry, транзакционный outbox, атомарность батча, метрики и наблюдаемость —
всё на месте и архитектурные границы (api без SQL/SDK, SQL только в repositories, processor не
знает про Kafka/ASGI) соблюдены. Гейты (ruff + 173 юнита) зелёные.

Блокирующая проблема раунда 1: фоновый outbox-publisher не умел (пере)поднять Kafka-producer,
если `producer.start()` упал на старте API. При штатном `docker compose up` это было достижимо
(api зависел от `kafka: service_started`, не `healthy`) и приводило к тому, что фото НИКОГДА не
публикуются, без самовосстановления. В раунде 2 исправлено (см. Re-review).

---

## BLOCKING раунда 1 (ЗАКРЫТ в раунде 2 — см. Re-review)

### B1. Outbox не восстанавливается, если `producer.start()` упал на старте API
- Файлы: `app/main.py:69-75`, `app/services/outbox.py:45-71`, `app/integrations/kafka_producer.py:33-34`
- Суть: в `lifespan` `producer.start()` обёрнут в `try/except`, ошибка логируется и глотается.
  Но `run_outbox_publisher` НИКОГДА не вызывал `producer.start()` повторно — только `send_and_wait`.
  Если на момент старта API брокер ещё не готов (`api.depends_on.kafka.condition = service_started`),
  producer оставался незапущенным, все публикации падали, строки навсегда `not_sent`, worker не
  получал сообщений. Самовосстановления не было.
- Рекомендация (раунда 1): ленивый идемпотентный `ensure_started()` в producer с вызовом в каждой
  итерации outbox; либо `api.depends_on.kafka.condition: service_healthy`.

---

## MAJOR / MINOR раунда 1

### M1. Формула лучшего кадра: `created_at` как тай-брейк не работает внутри батча (ЗАКРЫТ)
- Файл: `app/services/batch_service.py:40-49`, `app/db/models.py:57-59,149-151`
- Суть: `created_at` = `func.now()` = время начала транзакции (Postgres), константа. Все фото батча
  создаются в одной транзакции -> одинаковый `created_at` -> тай-брейк `created_at ASC` в месте, где
  считается best, no-op; при полном равенстве метрик победитель зависел от порядка `batch.photos`
  без `order_by`. Практически ничьи маловероятны (blur_score — float из хэша), но детерминизм
  ослаблен.
- Рекомендация: финальный тай-брейк по `photo_id` + `order_by` на relationship.

### M2. `Batch.photos` без `order_by` -> нестабильный порядок в ответе (ЗАКРЫТ)
- Файл: `app/db/models.py:153-155`, `app/repositories/batch_repository.py:31-34`

### M3. `mark_done` не очищает `last_error_*` после успешной повторной попытки (ЗАКРЫТ)
- Файл: `app/services/analysis_processor.py:107-131`, `app/repositories/photo_repository.py:108-114`

### M4. 500 от непойманных исключений не учитываются в `http_requests_total` (MINOR, оставлено)
- Файл: `app/api/metrics_middleware.py:26-43`, `app/main.py:105-107`, `app/core/errors.py:118-130`
- catch-all `Exception` обслуживается `ServerErrorMiddleware` (снаружи metrics middleware); при 500
  `call_next` бросает, метрика не пишется. AppError (4xx/5xx через `ExceptionMiddleware`) считаются
  корректно. Рекомендация: try/finally в middleware. Не блокер.

### M5. Outbox держит открытую БД-транзакцию на всё время публикаций батча (MINOR/perf, оставлено)
- Файл: `app/services/outbox.py:47-71`

### M6. Батч читает все файлы целиком в память (NOTE/perf, оставлено)
- Файл: `app/api/batches.py:35`, `app/services/photo_service.py:199`

### M7. Graceful shutdown воркера под `sh -c` может не получить SIGTERM (NOTE, оставлено)
- Файл: `docker-compose.yml` (`command: sh -c "uv run python -m app.worker.main"`)

---

## Проверено и признано корректным (раунд 1, для протокола)

- **Атомарный claim** (`photo_repository.claim_for_processing`): `UPDATE ... WHERE status='pending'`,
  возврат `rowcount`; дубли/rebalance -> 0 строк -> skip; row-lock Postgres сериализует конкурентные
  claim. Идемпотентно, предикат не менялся.
- **rowcount==0** обработан верно: skip + метрика `skipped`, gRPC не вызывается, результат не пишется.
- **Границы транзакций** в процессоре: claim->commit; каждая неудача->record_attempt+commit;
  терминал (upsert+mark_done ИЛИ mark_failed)->commit. Частичных терминальных состояний нет.
- **Commit offset строго после обработки** (`consumer.py`): `getone` c таймаутом, `commit` после
  `_handle_message`; ошибки внутри сообщения ловятся, loop жив; poison-pill (битый JSON, KeyError,
  невалидный UUID) -> лог + пропуск, без блокировки партиции.
- **Известное ограничение «зависание в processing»** при жёстком краше между claim и терминалом —
  зафиксировано в design §11 #3, осознанное для MVP.
- **Outbox at-least-once**: намерение фиксируется в БД атомарно со вставкой; дубликат публикации
  безопасен (идемпотентный consumer); единственный путь публикации — фоновый цикл.
- **Классификация retry/no-retry**: retryable набор + `TimeoutError` -> RETRY; прочее -> NO_RETRY
  (fail-safe); лимит 3, backoff; `attempts`/`last_error_*` пишутся на каждой неудаче.
- **Идемпотентная запись результата**: `ON CONFLICT (photo_id) DO NOTHING`; PK=photo_id.
- **Батчи**: валидация ВСЕХ файлов до любой записи -> атомарный reject; сбой MinIO -> rollback +
  best-effort delete; размер 2..10 -> `BatchSizeError` (400).
- **Формула best**: `min` по `(is_blurred, blur_score, -faces_count, created_at)` соответствует
  зафиксированной сортировке; только done; нет done -> None; `completed` только когда все терминальны;
  write-through идемпотентен. (Тай-брейк — см. M1, закрыт в раунде 2.)
- **Миграция v002**: `down_revision='v001'`, корректный порядок upgrade/downgrade для циклической
  связки photos<->batches; НЕТ новых PG-enum (VARCHAR+CHECK); интеграционный тест миграции покрывает
  upgrade head + downgrade v002->v001->base.
- **Обратная совместимость**: `PhotoResponse.analysis` Optional default None; single upload/list/get
  не сломаны; 173 теста зелёные.
- **expire_on_commit=False** — `get_batch` строит ответ после commit, обращаясь к eager-загруженным
  полям без MissingGreenlet.
- **`session.get(Batch, id, options=[selectinload(...)])`** — валидно в SQLAlchemy 2.0 (свежая
  сессия на запрос, опции применяются). Ок.
- **Нет блокирующих вызовов в event loop**: aiokafka/grpc.aio async, БД async, sync MinIO через
  `anyio.to_thread`; `trace_id_var` переиспользуется в consumer и outbox.
- **gRPC-стабы**: фикс импорта применён, стабы закоммичены, grpcio-tools в dev.

---

## Re-review (раунд 2) — проверка закрытия находок

Правки кодера прочитаны в рабочем дереве; гейты перепрогнаны: `ruff check .` -> clean;
`pytest -q -m "not integration"` -> **173 passed, 1 deselected**. Регрессий нет.

### B1 — ЗАКРЫТ (блокер снят)
- `app/integrations/kafka_producer.py`: добавлен `ensure_started()` — идемпотентный (fast-path
  `if self._started: return`), race-safe (`asyncio.Lock` + double-check внутри лока), при неудаче
  `producer.start()` сбрасывает `self._producer = None` и пробрасывает исключение, поэтому СЛЕДУЮЩИЙ
  вызов строит и стартует свежий `AIOKafkaProducer` (а не долбит объект в сломанном состоянии).
  `start()` теперь делегирует в `ensure_started()`; `stop()` корректно учитывает `_started`/None;
  `publish_analysis_requested` защищён guard-ом.
- `app/services/outbox.py`: `await producer.ensure_started()` вызывается в НАЧАЛЕ КАЖДОЙ итерации
  (строка 54); при неудаче — WARN и переход к следующему poll (публикации в этой итерации не
  выполняются). Это и есть само-восстановление: как только Kafka становится доступна, ближайший poll
  успешно стартует producer и разгребает накопившиеся `not_sent`. Гонка «producer не стартовал ->
  навсегда not_sent» устранена.
- `docker-compose.yml`: `api` и `worker` теперь зависят от `kafka` по `condition: service_healthy`
  (api строки 96/108, worker 136-137) — устраняет и сам холодный старт до готовности брокера.
  Двойная защита (healthcheck-депенденси + ленивый ensure_started) полностью закрывает сценарий.
- Замечание (не блокер): при повторных падениях `start()` частично сконструированный producer
  сбрасывается в None — возможный мелкий ресурс-лик фоновых задач aiokafka на каждой неудачной
  попытке; на практике aiokafka чистит за собой при сбое `start()`. Оставляю как NOTE.

### M1 (M-A) — ЗАКРЫТ
- `app/services/batch_service.py::select_best_photo`: ключ `min()` теперь
  `(is_blurred, blur_score, -faces_count, created_at, photo_id)` — финальный детерминированный
  тай-брейк по `photo_id` (UUID поддерживает сравнение) даёт однозначного победителя даже при полном
  равенстве метрик и одинаковом `created_at` внутри батча. Порядок точно соответствует
  зафиксированной формуле (is_blurred asc -> blur_score asc -> faces_count desc -> created_at asc ->
  photo_id asc).

### M2 (M-C) — ЗАКРЫТ
- `app/db/models.py`: `Batch.photos = relationship(..., order_by="Photo.created_at, Photo.photo_id")`
  -> стабильный, детерминированный порядок `photos[]` в `GET /v1/batches/{id}` и согласованный
  вход для формулы best.

### M3 (M-B) — ЗАКРЫТ
- `app/repositories/photo_repository.py::mark_done`: `.values(status=done, attempts=...,
  last_error_code=None, last_error_message=None)` — стейл-ошибка от ранней retryable-попытки больше
  не остаётся на `done`-фото.

### Оставлены как не-блокирующие замечания (на усмотрение)
M4 (метрика 500), M5 (длинная транзакция outbox), M6 (память батча), M7 (SIGTERM под `sh -c`) —
не блокируют, зафиксированы выше для последующих итераций / TASK-003.

---

## Вердикт

**status: APPROVE** — 0 BLOCKING. Блокер B1 закрыт корректно (ленивый race-safe `ensure_started` +
вызов каждую итерацию outbox + `service_healthy`-зависимости — самовосстановление реально работает).
Майноры M1–M3 закрыты. Оставшиеся M4–M7 — не-блокирующие замечания. Гейты зелёные. Готово к
следующему гейту (тесты).

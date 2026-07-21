---
task_id: TASK-002.1
agent: coder
model: sonnet
status: done
inputs:
  - specs/constitution.md
  - specs/feature-upload/tasks.md   # раздел TASK-002.1 (F1–F8, критерии, ограничения)
  - tasks/TASK-002.1/10_context.md
  - tasks/TASK-002.1/20_design.md
  - tasks/TASK-002.1/00_orchestration.md
outputs:
  - tasks/TASK-002.1/30_impl.md
  - photo-service/** (см. "Изменённые/новые файлы")
timestamp: 2026-07-21T00:00:00Z
---

# TASK-002.1 — Реализация багфиксов F1–F8

Реализовано строго по 16 шагам `20_design.md`. Одно осознанное отклонение
от буквы дизайна (тесты F1, см. «Принятые мелкие решения» и «Открытые
вопросы») — из-за прямого конфликта между «реализовать R1 буквально» и
«существующие тесты остаются зелёными без изменений»; исправлено минимально
и задокументировано.

## Что сделано (по шагам плана)

**Шаги 1–4 (F4 — раскол `metrics.py`):**
- Создан `app/integrations/metrics_api.py` (6 api-метрик: `photos_uploaded_total`,
  `http_requests_total`, `http_request_duration_seconds`, `storage_upload_errors_total`,
  `kafka_publish_errors_total`, `photos_pending`).
- Создан `app/integrations/metrics_worker.py` (6 worker-метрик: `photo_analysis_started_total`,
  `photo_analysis_completed_total`, `photo_analysis_failed_total`, `photo_analysis_duration_seconds`,
  `analyzer_grpc_errors_total`, `worker_messages_processed_total`).
- Удалён `app/integrations/metrics.py`.
- Импорты обновлены: `app/main.py`, `app/api/metrics_middleware.py`,
  `app/services/photo_service.py`, `app/services/outbox.py` → `metrics_api`;
  `app/services/analysis_processor.py` → `metrics_worker`; тесты
  `tests/test_metrics_middleware.py`, `tests/test_metrics_endpoint.py`,
  `tests/test_outbox.py` — аналогично.
- Дашборд `grafana/dashboards/photo-service.json`: панель "Pending photos"
  → `photos_pending{job="photo-api"}`.
- **Проверено:** `import app.worker.main` → реестр prometheus_client НЕ
  содержит `photos_pending`/`http_requests_total` (worker-only метрики есть);
  `import app.main` → реестр НЕ содержит `photo_analysis_started_total`
  (см. раздел "Как проверить").

**Шаг 5 (F3-развязка — `app/services/mappers.py`):**
- Новый модуль `app/services/mappers.py::analysis_to_response(...)`.
- `photo_service.py`: локальная `_analysis_response` удалена, импорт из
  `mappers`, 2 места вызова (`get_photo`, `list_photos`) переключены.
- `batch_service.py`: импорт `analysis_to_response` из `mappers` (не из
  `photo_service`). Проверено: `batch_service.py` больше не импортирует
  `photo_service` (`grep` пуст).

**Шаги 6–9 (F3 — завершение батча в worker, `get_batch` read-only):**
- `app/repositories/photo_repository.py`: добавлен `get_batch_id(session, photo_id) -> uuid.UUID | None`.
- `app/repositories/batch_repository.py`: `mark_completed` заменён на
  `try_complete(session, batch_id, best_photo_id) -> int` — атомарный
  `UPDATE ... WHERE batch_id=:id AND status='processing'`, возвращает rowcount.
  `mark_completed` удалён (мёртвый код отсутствует, проверено `grep`).
- `app/services/analysis_processor.py`: добавлены `_TERMINAL_STATUSES`,
  импорт `select_best_photo` из `batch_service` (не из `photo_service` —
  важно для F4, см. ниже), параметр `batch_repository` в `__init__`
  (default `BatchRepository()`), приватный метод `_maybe_complete_batch`.
  Вызов добавлен на 3 путях: skip (rowcount==0), done, failed.
- `app/services/batch_service.py::get_batch`: удалён блок
  `mark_completed`+`commit`; оставлено только вычисление
  `status`/`best_photo_id` для отображения (без записи). Неиспользуемые
  `logging`/`logger` удалены.
- `app/worker/main.py`: импортирует `BatchRepository`, передаёт
  `batch_repository=BatchRepository()` в `AnalysisProcessor(...)`.
- **Проверено:** `get_batch` не вызывает ни `try_complete`, ни
  `session.commit` (тесты `test_batch_service.py::TestGetBatch` переписаны
  под это, см. «Изменённые тесты»).

**Шаг 10 (F1 — `app/worker/consumer.py`):**
- Добавлен `enum.Enum MessageOutcome {COMMIT, RETRY}`, константа
  `_ERROR_BACKOFF_SECONDS = 1.0`, импорт `TopicPartition` из
  `aiokafka.structs` (проверено: `seek` синхронный, сигнатура
  `(partition, offset)`).
- `_handle_message` теперь возвращает `MessageOutcome`:
  - unparsable JSON → `COMMIT` (poison-pill, как раньше).
  - отсутствующие/пустые `photo_id`/`object_key` → `COMMIT` (как раньше).
  - **новое:** `photo_id`, не парсящийся как UUID → `COMMIT` (poison-pill,
    решение R1, одобрено оркестратором) — проверка `uuid.UUID(payload["photo_id"])`
    в try/except ValueError, ДО вызова `processor.process(...)`.
  - `processor.process(...)` завершилась без исключения → `COMMIT`.
  - неожиданное исключение → `logger.exception(...)` + `return RETRY`
    (не пробрасывается наружу).
- `consume_loop`: ветвление по исходу — `COMMIT` → `await consumer.commit()`;
  `RETRY` → `consumer.seek(TopicPartition(message.topic, message.partition), message.offset)`
  (синхронно, без await) + `logger.warning(...)` + пауза
  `await asyncio.wait_for(stop_event.wait(), timeout=_ERROR_BACKOFF_SECONDS)`
  (прерываемая shutdown-ом).
- Module-docstring переписан: явно объясняет, почему `seek()` обязателен
  (иначе `getone()` уже сдвинул позицию и "просто не коммитить" не даёт
  реальной передоставки того же сообщения), и фиксирует компромисс
  "детерминированная ошибка блокирует партицию" (DLQ вне скоупа).

**Шаг 11 (F2/F8 — `app/api/uploads.py` + эндпоинты):**
- Новый модуль `app/api/uploads.py`:
  - `read_capped_file(file: UploadFile) -> bytes` — `await file.read(MAX_FILE_SIZE_BYTES + 1)`,
    немедленный `PayloadTooLargeError` (413) при превышении, остаток не
    читается (в рамках одного файла — читать больше и не нужно, `read(N)`
    сам не читает больше `N`).
  - `read_batch_files(files: list[UploadFile]) -> list[tuple[str|None, bytes]]` —
    сначала проверка `MIN_BATCH_SIZE <= len(files) <= MAX_BATCH_SIZE` (ДО
    единого `.read()`), затем капнутое чтение каждого файла с бегущей
    суммой; при превышении `BATCH_MAX_TOTAL_BYTES` — немедленный 413,
    следующие файлы не читаются (`for`-цикл прерывается исключением).
  - Константы (`MAX_FILE_SIZE_BYTES`, `MIN_BATCH_SIZE`, `MAX_BATCH_SIZE`,
    `BATCH_MAX_TOTAL_BYTES`) импортированы из `photo_service` — единственный
    источник, не продублированы (сохраняет monkeypatch в
    `tests/test_photo_service_batch.py` рабочим).
  - Docstring документирует границу F8 (капнутое чтение ≠ стриминг;
    почему ранний отказ по `Content-Length` не реализован).
- `app/api/photos.py::upload_photo` → `data = await read_capped_file(file)`.
- `app/api/batches.py::upload_batch` → `files = await read_batch_files(file)`.
- Сервисный слой (`photo_service.py::_validate`/`create_batch`) оставлен
  как есть (defense-in-depth, авторитетный источник бизнес-правила) — по
  дизайну.

**Шаг 12 (F5 — компенсация MinIO при падении commit):**
- `app/core/errors.py`: добавлен `class DatabaseUnavailable(AppError)` —
  `error_code="SERVICE_UNAVAILABLE"`, `http_status=503` (переиспользует
  существующий публичный код, формат ответа не меняется).
- `photo_service.py::create_photo`: финальный `await session.commit()`
  обёрнут в try/except → `rollback()` + best-effort
  `anyio.to_thread.run_sync(self._storage.delete_file, object_key)` (свой
  try/except, не маскирует исходную ошибку) + `logger.error(...)` +
  `raise DatabaseUnavailable(..., photo_id=...)`.
- `photo_service.py::create_batch`: аналогично, но удаляются ВСЕ
  `saved_object_keys` (весь список), а не один объект.
- **Проверено:** существующие тесты (`session.commit` как `AsyncMock`, не
  бросает) остаются зелёными без изменений — новый try/except не меняет
  их поведение (commit не падает в тестах).

**Шаг 13 (F6 — restart-политики):** `docker-compose.yml` — `restart: unless-stopped`
добавлен всем 8 сервисам (`postgres`, `minio`, `kafka`, `analyzer-stub`,
`api`, `worker`, `prometheus`, `grafana`). Проверено `docker compose config`.

**Шаг 14 (F7 — insecure gRPC):** `app/integrations/analyzer_client.py::__init__` —
добавлен комментарий, объясняющий осознанный выбор `insecure_channel` для
однo-сетевого MVP-compose и дефер конфигурируемого TLS в TASK-003. Никаких
функциональных изменений, новых env-переменных не добавлено.

**Шаг 15 (F8 — граница):** документирована в docstring `app/api/uploads.py`
(сделано в шаге 11); строка для README помечена как задача документатора
(80_docs) — не в скоупе кодера.

**Шаг 16 (обновление существующих тестов под новую семантику):** см.
раздел «Изменённые тесты» ниже.

## Изменённые/новые файлы

**Новые:**
- `photo-service/app/integrations/metrics_api.py`
- `photo-service/app/integrations/metrics_worker.py`
- `photo-service/app/services/mappers.py`
- `photo-service/app/api/uploads.py`

**Удалённые:**
- `photo-service/app/integrations/metrics.py`

**Изменённые (продакшн-код):**
- `photo-service/app/main.py` (импорт `metrics_api`)
- `photo-service/app/api/metrics_middleware.py` (импорт `metrics_api`)
- `photo-service/app/api/photos.py` (`read_capped_file`)
- `photo-service/app/api/batches.py` (`read_batch_files`)
- `photo-service/app/services/photo_service.py` (импорт `metrics_api`,
  `analysis_to_response` из `mappers`, F5 try/except вокруг commit ×2)
- `photo-service/app/services/batch_service.py` (F3 read-only `get_batch`,
  импорт `analysis_to_response` из `mappers`)
- `photo-service/app/services/analysis_processor.py` (импорт `metrics_worker`,
  F3 `_maybe_complete_batch` + 3 вызова, `batch_repository` param)
- `photo-service/app/services/outbox.py` (импорт `metrics_api`)
- `photo-service/app/repositories/photo_repository.py` (`get_batch_id`)
- `photo-service/app/repositories/batch_repository.py` (`try_complete`
  вместо `mark_completed`)
- `photo-service/app/worker/consumer.py` (F1 `MessageOutcome`, `seek`,
  анти-hot-loop пауза)
- `photo-service/app/worker/main.py` (передаёт `batch_repository`)
- `photo-service/app/core/errors.py` (`DatabaseUnavailable`)
- `photo-service/app/integrations/analyzer_client.py` (F7 комментарий)
- `photo-service/docker-compose.yml` (F6 restart)
- `photo-service/grafana/dashboards/photo-service.json` (F4 job-фильтр)

**Изменённые тесты (в объёме «Ожидаемых поломок» дизайна + 1 задокументированное отклонение):**
- `photo-service/tests/test_metrics_middleware.py`,
  `test_metrics_endpoint.py`, `test_outbox.py` — импорт `metrics_api`
  (F4, буквально по списку дизайна).
- `photo-service/tests/test_batch_service.py::TestGetBatch` — убраны
  ассерты `mark_completed`/`session.commit`, заменены на «не вызывается»
  (F3, по списку дизайна); докстринг модуля обновлён.
- `photo-service/tests/test_batch_repository.py::TestMarkCompleted` →
  `TestTryComplete` — переписан под новую сигнатуру/семантику (F3, по
  списку дизайна), добавлены 2 теста на rowcount (0/1) и WHERE-предикат
  `status='processing'`.
- `photo-service/tests/test_analysis_processor.py` — во все 11 существующих
  тестов добавлено `photos.get_batch_id.return_value = None` (F3, по
  списку дизайна, для детерминизма `_maybe_complete_batch`); докстринг
  модуля дополнен.
- `photo-service/tests/test_worker_consumer.py` — **отклонение от буквы
  «Ожидаемых поломок»** (см. «Открытые вопросы»): фикстуры `photo_id` в
  `TestHandleMessageValid` и части `TestConsumeLoop` заменены с
  placeholder-строк (`"photo-1"`, `"p1"`, `"p2"`) на валидные UUID-строки.
  Никакие ассерты/семантика тестов не менялись — только константы,
  используемые как валидный `photo_id`.

## Принятые мелкие решения

1. **F1 UUID-проверка ломает 3 неучтённых в дизайне теста** — исправлено
   минимально (см. «Открытые вопросы» ниже, зафиксировано явно, так как
   это прямое расхождение с буквой "Ожидаемых поломок").
2. Для F5 добавлен `except Exception` (не `BaseException`) вокруг
   `session.commit()` — не перехватывает `asyncio.CancelledError`
   (согласуется с остальным кодом, `noqa: BLE001` как в дизайне).
3. Комментарий в `test_batch_service.py` для теста
   `test_processing_status_while_any_photo_non_terminal` обновлён с
   `mark_completed` на `try_complete` (тот же mock, просто имя атрибута
   соответствует новому методу — иначе тест продолжал бы молча проверять
   несуществующий метод по старому имени).

## Как запустить/проверить локально

Из `photo-service/`:

```bash
uv run ruff check .                          # чисто
uv run pytest -q -m "not integration"        # 370 passed, 1 deselected
uv run pytest -q -m "not integration" --cov=app --cov-report=term-missing  # 94% total
docker compose config -q                     # валиден (exit 0)
docker compose config | grep -E "restart:|^  [a-z-]+:"   # restart: unless-stopped × 8

# F4 самопроверка (воркер не регистрирует api-метрики):
uv run python -c "
import app.worker.main
from prometheus_client import REGISTRY, generate_latest
text = generate_latest(REGISTRY).decode()
assert 'photos_pending' not in text
assert 'http_requests_total' not in text
assert 'photo_analysis_started_total' in text
print('OK')
"

# F4 обратная проверка (api не регистрирует worker-метрики):
uv run python -c "
import app.main
from prometheus_client import REGISTRY, generate_latest
text = generate_latest(REGISTRY).decode()
assert 'photos_pending' in text
assert 'photo_analysis_started_total' not in text
print('OK')
"
```

Результаты фактического прогона (см. выше в диалоге коммитов инструмента):
- `ruff check .` → `All checks passed!`
- `pytest -m "not integration"` → `370 passed, 1 deselected`
- coverage total → `94%` (непокрытые строки — новые F1/F3/F5 ветки:
  RETRY-путь consumer.py, `_maybe_complete_batch`, commit-failure
  компенсация — ожидаемо, добавляет test-writer)
- `docker compose config -q` → exit 0; все 8 сервисов содержат
  `restart: unless-stopped` (проверено построчно)

## Открытые вопросы для ревью

1. **F1 / R1 — конфликт дизайна с существующими тестами (требует внимания
   ревьюеров).** Раздел «Ожидаемые поломки тестов» дизайна утверждает, что
   `tests/test_worker_consumer.py` остаётся зелёным без изменений для
   success/poison-pill путей. Однако буквальная реализация R1 (непарсируемый
   `photo_id` → poison-pill через `uuid.UUID(payload["photo_id"])`) ломает
   4 теста в `TestHandleMessageValid` и 3 теста в `TestConsumeLoop`,
   которые использовали non-UUID placeholder-строки (`"photo-1"`, `"p1"`,
   `"p2"`) как валидный `photo_id` — с новой UUID-проверкой такие сообщения
   стали бы (ошибочно для теста) классифицированы как poison-pill и
   `processor.process` не вызывался бы вовсе.
   Я расцениваю это как непреднамеренный пробел дизайна (R1 одобрен
   оркестратором как есть; тестовые фикстуры просто не были пересмотрены
   под новую семантику) и исправил **минимально** — заменил значения
   `photo_id` на валидные UUID-строки, не трогая ни одного ассерта/логики
   теста. Прошу ревьюеров подтвердить, что это в рамках «фиксить тесты
   только в объёме новой семантики F1», а не выход за скоуп.
2. **F3 покрытие `_maybe_complete_batch` тестами** — новый метод (строки
   `analysis_processor.py:88-100`) пока не покрыт собственными тестами
   (дизайн явно оставляет это test-writer'у); существующие тесты лишь
   гарантируют, что метод корректно no-op'ит для одиночных фото.
3. **F5 компенсация** также пока не имеет прямого теста «commit упал →
   `delete_file` вызван → 503» — по дизайну это тоже задача test-writer'а.

## Чек-лист самопроверки по критериям приёмки (spec: TASK-002.1)

- [x] Тест consumer: неожиданное исключение из `process()` → offset НЕ
      закоммичен (реализовано: `RETRY` → `seek` + no commit; тест на этот
      путь — задача test-writer, но код и existing tests подтверждают
      обратную сторону: success/poison-pill по-прежнему коммитят).
- [x] Тест батча: 11 файлов отклоняются ДО чтения (`read_batch_files`
      проверяет count до `.read()`); файл >50 МБ прерывает чтение на капе
      (413); суммарный лимит по бегущей сумме — реализовано, unit-тесты
      добавит test-writer.
- [x] Тест worker-завершения батча: `_maybe_complete_batch` реализован,
      вызывается на skip/done/failed; `get_batch` не вызывает
      `try_complete`/`commit` (подтверждено переписанными тестами
      `test_batch_service.py`).
- [x] Тест метрик: `/metrics` worker-а не содержит `photos_pending`/`http_*`
      (проверено вручную, см. «Как проверить»); api содержит. Дашборд:
      `photos_pending{job="photo-api"}`.
- [x] Тест компенсации: `DatabaseUnavailable` реализован в обоих путях
      (`create_photo`/`create_batch`), рабочий код проверен вручную (по
      структуре try/except); юнит-тест — задача test-writer.
- [x] `docker compose config`: у всех 8 сервисов `restart: unless-stopped`
      (проверено).
- [x] Все существующие тесты зелёные (`370 passed, 1 deselected`); ruff
      чист; coverage 94% (не ниже текущего уровня).
- [ ] Живой прогон (`docker compose up --build` → загрузка/батч/метрики/
      графики) — не выполнялся кодером, требует ручной/CI проверки после
      ревью (вне скоупа самопроверки кодера).

F1–F8 реализованы, готово к ревью.

---

## Итерация правок 1 (после ревью)

**Примечание:** агент-coder внёс все правки и прогнал гейты, но был прерван сетевой ошибкой до самопроверок и написания этого раздела. Раздел дописан оркестратором по факту диффа; самопроверки BLK-1/BLK-2 выполнены оркестратором (вывод ниже).

### BLK-1 (reviewer-1 B1, БЛОКЕР) — незащищённый seek роняет воркер
`app/worker/consumer.py`: импорт `from aiokafka.errors import CommitFailedError, IllegalStateError`; `seek()` в RETRY-ветке обёрнут в `except (IllegalStateError, AssertionError)`, `commit()` в COMMIT-ветке — в `except CommitFailedError`. Обе ветки: warning с topic/partition/offset + продолжение цикла. Докстринг модуля приведён в соответствие с обещанием «never left to crash the loop».

**Самопроверка (оркестратор):** мок `consumer.seek`, бросающий `IllegalStateError`, + `process()`, бросающий `RuntimeError`:
- `consume_loop` НЕ пробросил исключение (до фикса ревьюер получал `PROPAGATED IllegalStateError`);
- лог: «partition revoked before rewind; redelivery handled by rebalance»;
- `commit.await_count == 0` — offset не закоммичен, сообщение не потеряно (заберёт новый владелец партиции).

### BLK-2 (reviewer-1 M2, поднят оркестратором) — суммарный лимит был недостижим
`app/services/photo_service.py`: `BATCH_MAX_TOTAL_BYTES` стал независимой константой `150 * 1024 * 1024` (150 МБ) вместо производной `MAX_BATCH_SIZE * MAX_FILE_SIZE_BYTES` (= 500 МБ, недостижимо при строгом `>`). Комментарий фиксирует, что это осознанный потолок RAM на один батч-запрос.

**Самопроверка (оркестратор):** теоретический максимум 10 × 50 МБ = 500 МБ против лимита 150 МБ → ветка достижима (`True`); отклонение срабатывает уже на 4 файлах по 50 МБ (200 МБ > 150 МБ). Ранее не срабатывало никогда — то есть в RAM допускались ровно те 500 МБ, ради которых лимит и вводился (исходное замечание reviewer-2 #6 в TASK-002).

### MIN-1 (reviewer-1 M1) — метрики на дропы и ретраи
`app/integrations/metrics_worker.py`: добавлены `worker_messages_dropped_total{reason}` (reason: unparsable | missing_fields | invalid_uuid) и `worker_message_retries_total`; инкременты в соответствующих ветках `consumer.py`. В докстринге зафиксировано, что залипание партиции детектируется через `increase(worker_message_retries_total[5m]) > 0`, удерживающийся во времени. Кардинальных лейблов нет.

### MIN-2 (reviewer-1 m1) — позднее связывание констант
`app/api/uploads.py`: вместо импорта значений — `from app.services import photo_service` и обращение через модуль (`photo_service.MAX_FILE_SIZE_BYTES`). Единый источник истины сохранён, monkeypatch в тестах теперь влияет на api-путь.

### Гейты после правок (прогнаны оркестратором)
- `uv run ruff check .` → All checks passed
- `uv run pytest -q -m "not integration"` → 370 passed, 1 deselected
- `docker compose config -q` → валиден

Правки по ревью 1 внесены, готово к повторному ревью.

---

## Итерация правок 2 (остаточные не-блокеры)

Три оставшихся non-blocking пункта из `40_review-1.md` (m2, m3, m4), скоуп
строго ограничен ими. `test-writer` сознательно не писал тесты на эти
находки (`tasks/TASK-002.1/50_tests.md`, раздел «Не покрыто»), чтобы не
зафиксировать багованное поведение как ожидаемое — тесты добавлены в этой
итерации вместе с фиксом.

### MIN-A (m2) — `session.rollback()` вне защищённого блока

**Файл:** `photo-service/app/services/photo_service.py`

Добавлены два приватных хелпера компенсации, переиспользуемые в обоих
методах (`create_photo`/`create_batch`), публичное поведение не изменено:

- `PhotoService._safe_rollback(session, context)` — `await
  session.rollback()` в try/except; падение rollback (реалистично, если
  соединение уже инвалидировано неудачным commit) поглощается и
  логируется `logger.warning(...)`, но **не** прерывает дальнейшую
  компенсацию.
- `PhotoService._delete_saved_objects(object_keys, context)` — общий
  best-effort цикл удаления (см. также MIN-B ниже).

Оба `except`-блока в `create_photo` (StorageUnavailable-путь и
commit-путь F5) и оба в `create_batch` теперь вызывают
`self._safe_rollback(...)` вместо голого `await session.rollback()`.
Порядок гарантирован: (1) попытка rollback (падение поглощается), (2)
best-effort удаление уже сохранённых объектов (только там, где они есть —
StorageUnavailable-путь `create_photo` ничего не сохраняет до падения, там
удалять нечего), (3) `raise StorageUnavailable`/`DatabaseUnavailable` —
клиент всегда получает 503, а не 500 от необработанного исключения
`rollback()`.

### MIN-B (m3) — незащищённая компенсация в StorageUnavailable-пути батча

**Файл:** `photo-service/app/services/photo_service.py`

Цикл `for object_key in saved_object_keys: await
anyio.to_thread.run_sync(self._storage.delete_file, object_key)` в
StorageUnavailable-ветке `create_batch` заменён на вызов общего
`self._delete_saved_objects(saved_object_keys, {"batch_id": ...})` —
тот же хелпер, что и в MIN-A, с try/except вокруг каждого отдельного
`delete_file` (падение одного объекта логируется `logger.warning` и не
мешает попытке удалить остальные; исходное `StorageUnavailable`
сохраняется через `raise ... from exc`, ничем не маскируется).
Commit-путь `create_batch` уже использовал per-object try/except — теперь
оба пути (StorageUnavailable и commit) буквально используют один и тот же
код, копипаста устранена.

### MIN-C (m4) — двойной учёт `worker_messages_processed_total`

**Файл:** `photo-service/app/services/analysis_processor.py`

На всех трёх терминальных путях (skip/done/failed) инкремент
`worker_messages_processed_total.labels(result=...)` перенесён на строку
**после** вызова `_maybe_complete_batch(...)`, а не до него. Если
`_maybe_complete_batch` бросит исключение (например, БД моргнула),
инкремент теперь не происходит вовсе — сообщение уходит в RETRY,
передоставляется, и при повторной обработке (в т.ч. на skip-пути) не
происходит двойного учёта одного и того же сообщения.

`photo_analysis_completed_total`/`photo_analysis_failed_total` (описывают
судьбу ФОТО) оставлены на прежнем месте — до `_maybe_complete_batch` —
это осознанно другая семантика. Разница задокументирована в двух местах:
- комментарий на каждом из трёх инкрементов в `analysis_processor.py`;
- расширенный docstring `worker_messages_processed_total` в
  `app/integrations/metrics_worker.py` — явно называет счётчик
  "per-message" (в отличие от "per-photo" счётчиков `photo_analysis_*`) и
  объясняет, почему инкремент стоит именно после батч-чека.

### Тесты, добавленные в этой итерации

1. **`session.rollback()` бросает в `create_photo` → `delete_file` всё
   равно вызван, клиент получает `DatabaseUnavailable` (503, не 500).**
   `tests/test_photo_service.py::TestCreatePhotoCommitFailureCompensation::
   test_rollback_itself_failing_still_deletes_the_object_and_raises_503`.
2. **То же для `create_batch`** (удаляются все 3 сохранённых объекта,
   несмотря на падение rollback).
   `tests/test_photo_service_batch.py::TestBatchCommitFailureCompensation::
   test_rollback_itself_failing_still_deletes_every_object_and_raises_503`.
3. **StorageUnavailable-путь батча: `delete_file` бросает на первом
   объекте → остальные два всё равно удалены (3 вызова), наружу летит
   `StorageUnavailable`.**
   `tests/test_photo_service_batch.py::TestBatchStorageFailureRollback::
   test_delete_failure_on_first_saved_object_does_not_skip_the_rest`
   (4 файла: первые 3 сохраняются, 4-й падает с `StorageUnavailable`;
   `delete_file` для первого сохранённого объекта бросает `RuntimeError`,
   для остальных двух — нет; проверено `call_count == 3`, т.е. все три
   попытки удаления действительно произошли).
4. **`_maybe_complete_batch` бросает → `worker_messages_processed_total`
   НЕ инкрементирован** — на всех трёх терминальных путях (skip/done/
   failed) отдельно.
   `tests/test_analysis_processor.py::
   TestWorkerMessagesProcessedTotalNotDoubleCounted` (3 теста: `photos.
   get_batch_id.side_effect = RuntimeError(...)`, `process()`
   пробрасывает `RuntimeError` наружу, значение счётчика `_value.get()`
   до/после вызова не меняется — сравнение по конкретному лейблу
   `result=`, паттерн `_counter_value` как в `test_worker_consumer.py`).

### Изменённые/новые файлы (итерация 2)

- `photo-service/app/services/photo_service.py` — хелперы
  `_safe_rollback`/`_delete_saved_objects`, применены во всех 4
  except-блоках `create_photo`/`create_batch`.
- `photo-service/app/services/analysis_processor.py` — перенос 3
  инкрементов `worker_messages_processed_total` после
  `_maybe_complete_batch`, поясняющие комментарии.
- `photo-service/app/integrations/metrics_worker.py` — расширен docstring
  `worker_messages_processed_total` (per-message vs per-photo).
- `photo-service/tests/test_photo_service.py` — +1 тест.
- `photo-service/tests/test_photo_service_batch.py` — +2 теста.
- `photo-service/tests/test_analysis_processor.py` — +1 импорт
  (`worker_messages_processed_total`), +1 хелпер `_counter_value`, +3
  теста (новый класс `TestWorkerMessagesProcessedTotalNotDoubleCounted`).

### Принятые мелкие решения (итерация 2)

1. Единый хелпер `_delete_saved_objects` применён и в StorageUnavailable-,
   и в commit-пути `create_batch` — устраняет дублирование, которое сам
   review-1 отметил как «копипаста уже написанного внутреннего
   try/except». Лог-сообщение унифицировано на `"failed to delete
   orphaned object after failure"` (было два чуть разных текста:
   «...after commit failure» и «...batch object after commit failure») —
   проверено, что ни один тест не матчился на точный текст этих логов
   (grep по тестам, см. ниже), так что унификация не ломает ассерты.
2. Для StorageUnavailable-пути `create_photo` (где ничего не сохранено до
   падения `save_file`) delete-хелпер не вызывается — там нечего
   удалять; протестировано только защитой самого `rollback()` (см.
   комментарий в `_safe_rollback`, применяется единообразно во всех 4
   местах, но реально проверяется тестом только там, где есть что
   удалять — по заданию оркестратора).
3. `_safe_rollback` объявлен `@staticmethod` (не обращается к `self`,
   кроме логгера модуля) — оставлен как метод класса (не свободная
   функция), т.к. концептуально относится к компенсационному поведению
   `PhotoService` и симметричен `_delete_saved_objects`.

### Результаты гейтов (итерация 2, прогнано кодером)

```
uv run ruff check .
All checks passed!

uv run pytest -q -m "not integration"
431 passed, 1 deselected in ~16s   (было 425, +6 новых тестов)

uv run pytest -q -m "not integration" --cov=app --cov-report=term
TOTAL: 1081 stmts, 20 miss, 98%   (не ниже порога 98%; непокрытые строки —
исключительно protobuf-генерация app/grpc_gen/* и `if __name__ ==
"__main__"` guard в app/worker/main.py, не связаны с этой итерацией)
```

Прогнано дважды подряд — оба раза `431 passed, 1 deselected`, без флаки.

Остаточные не-блокеры закрыты, TASK-002.1 готова к живому прогону и PR.

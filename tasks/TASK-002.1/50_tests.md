---
task_id: TASK-002.1
agent: test-writer
model: sonnet
status: done
inputs:
  - specs/constitution.md
  - specs/feature-upload/tasks.md   # раздел TASK-002.1 (F1-F8, критерии приёмки)
  - tasks/TASK-002.1/20_design.md
  - tasks/TASK-002.1/30_impl.md
  - tasks/TASK-002.1/40_review-1.md   # раздел M4 (пробелы покрытия) + Re-review
  - tasks/TASK-002.1/41_review-2.md   # раздел "Покрытие тестами"
  - photo-service/** (код F1-F8, существующие тесты)
outputs:
  - photo-service/tests/test_worker_consumer.py (изменён, +16 тестов)
  - photo-service/tests/test_analysis_processor.py (изменён, +11 тестов)
  - photo-service/tests/test_photo_service.py (изменён, +4 теста)
  - photo-service/tests/test_photo_service_batch.py (изменён, +3 теста)
  - photo-service/tests/test_photo_repository.py (изменён, +3 теста)
  - photo-service/tests/test_photos_endpoints.py (изменён, +1 параметр в матрице ошибок)
  - photo-service/tests/test_batches_endpoints.py (изменён, +1 параметр в матрице ошибок)
  - photo-service/tests/test_uploads.py (новый, 14 тестов)
  - photo-service/tests/test_metrics_isolation.py (новый, 2 теста)
  - tasks/TASK-002.1/50_tests.md
timestamp: 2026-07-21T00:00:00Z
---

# TASK-002.1 — Тесты на багфиксы F1–F8

Написаны после двух APPROVE (reviewer-1 opus, reviewer-2 haiku, оба раунда
2). Список тестов закрывает M4 (`40_review-1.md`, обязательный минимум
из 10 пунктов + Re-review-дополнение из 3 пунктов) и раздел «Покрытие
тестами» (`41_review-2.md`), а также 17 пунктов задания оркестратора.
Реальный analyzer нигде не вызывается (все внешние зависимости —
`AsyncMock`/`MagicMock`/фейки).

## Матрица: критерий приёмки → тест

### Критерии из `specs/feature-upload/tasks.md` (TASK-002.1 «Критерии приёмки»)

| # | Критерий | Тест(ы) |
|---|---|---|
| 1 | Consumer: неожиданное исключение из `process()` → offset НЕ закоммичен | `test_worker_consumer.py::TestConsumeLoopRetryPath::test_unexpected_exception_does_not_commit_the_offset` |
| 2 | Consumer: `seek()` вызван с правильными `(TopicPartition(topic, partition), offset)` | `TestConsumeLoopRetryPath::test_unexpected_exception_seeks_to_this_messages_own_topic_partition_offset` |
| 3 | Consumer: poison-pill (битый JSON / нет полей / невалидный UUID) → закоммичен, `process` не вызван | `TestHandleMessagePoisonPill` (существовал), `TestHandleMessageMissingFields` (существовал), `TestHandleMessageInvalidUuid` (новый) |
| 4 | Consumer: обработанное сообщение → закоммичен, `seek` НЕ вызван (happy path не деградировал) | `TestConsumeLoopHappyPathDoesNotSeek::test_successful_processing_never_calls_seek` |
| 5 | Consumer: ребаланс — `seek()`/`commit()` бросают `IllegalStateError`/`AssertionError`/`CommitFailedError` → цикл выживает | `TestConsumeLoopRebalanceGuard` (4 теста) |
| 6 | Consumer: анти-hot-loop пауза прерывается `stop_event` (graceful shutdown) и корректно завершается по таймауту в обычном случае | `TestConsumeLoopRetryBackoffInterruptible`, `TestConsumeLoopRetryBackoffElapsesNormally` |
| 7 | Consumer: `worker_message_retries_total` / `worker_messages_dropped_total{reason}` инкрементируются | `TestConsumeLoopRetryPath::test_worker_message_retries_total_increments_on_retry`, `TestDroppedMessageMetrics` (3 теста: unparsable/missing_fields/invalid_uuid) |
| 8 | Батч: 11 файлов отклоняются ДО чтения содержимого (0 вызовов `.read()`) | `test_uploads.py::TestReadBatchFilesCountCheckedBeforeAnyRead` (11/1/0 файлов) |
| 9 | Батч: файл >лимита прерывает чтение на капе (413), не дочитывая остаток; остальные файлы батча не читаются | `TestReadCappedFile::test_oversized_file_raises_payload_too_large_without_reading_more`, `TestReadBatchFilesPerFileCap` (2 теста) |
| 10 | Батч: суммарный лимit (150 МБ) срабатывает по бегущей сумме, достижим на боевых константах | `TestReadBatchFilesRunningTotal` (monkeypatched-лимит + `test_production_constants_reject_four_50mb_files` — регрессионный тест на BLK-2/M2 без monkeypatch) |
| 11 | Worker-завершение батча: все фото терминальны → батч `completed` + `best_photo_id` БЕЗ единого GET | `test_analysis_processor.py::TestMaybeCompleteBatchDirect::test_all_terminal_completes_the_batch_with_the_computed_best_photo_id` + `TestMaybeCompleteBatchCalledOnAllTerminalPaths` (вызов через `process()`, GET нигде не участвует) |
| 12 | `get_batch` НЕ вызывает `try_complete`/UPDATE | pre-existing `test_batch_service.py::TestGetBatch` (переписан кодером под F3 в рамках 30_impl, уже проверяет `repository.try_complete.assert_not_called()` + `session.commit.assert_not_called()` на всех ветках) — не менялся мной, уже достаточен |
| 13 | Метрики: `/metrics`/реестр worker-а не содержит `photos_pending`/`http_*`; api не содержит worker-метрик | `test_metrics_isolation.py` (2 теста, subprocess-изоляция) |
| 14 | Компенсация: падение `commit` в `create_photo` → `delete_file` вызван, клиенту 503 | `test_photo_service.py::TestCreatePhotoCommitFailureCompensation` (4 теста) + `test_photos_endpoints.py` (параметр `DatabaseUnavailable` в матрице ошибок → 503 `SERVICE_UNAVAILABLE`, unified body) |
| 15 | Компенсация: то же для `create_batch`, удалены ВСЕ сохранённые object_key | `test_photo_service_batch.py::TestBatchCommitFailureCompensation` (3 теста) + `test_batches_endpoints.py` (параметр `DatabaseUnavailable`) |
| 16 | Все существующие тесты зелёные, ruff чист, coverage не ниже текущего | см. «Результат прогона» ниже |
| 17 | `docker compose config`: `restart: unless-stopped` у всех сервисов | статическая конфигурация, не код — уже верифицирована исполнением реviewer-1/reviewer-2 (`40_review-1.md`/`41_review-2.md`, таблица гейтов); юнит-тест не добавлялся (вне списка оркестратора, нет бизнес-логики для покрытия) |
| 18 | Живой прогон (`docker compose up --build`) | вне скоупа test-writer — ручная/CI проверка после мёржа |

### Детализация по 17 пунктам задания оркестратора

| Пункт задания | Тест(ы) |
|---|---|
| 1. Неожиданное исключение → offset НЕ коммитится + `seek(TopicPartition(...), offset)` с правильными аргументами | `TestConsumeLoopRetryPath` (2 теста) |
| 2. Poison-pill → COMMIT + инкремент `worker_messages_dropped_total{reason}` (unparsable/missing_fields/invalid_uuid) | `TestDroppedMessageMetrics` (3 теста), `TestHandleMessageInvalidUuid` |
| 3. Успешная обработка → COMMIT, `seek` НЕ вызывается | `TestConsumeLoopHappyPathDoesNotSeek` |
| 4. Ребаланс: `seek()`→`IllegalStateError`/`AssertionError`, `commit()`→`CommitFailedError` → цикл выживает | `TestConsumeLoopRebalanceGuard` (4 теста, включая «цикл способен обработать следующее сообщение после сбоя») |
| 5. RETRY-пауза прерывается по `stop_event` | `TestConsumeLoopRetryBackoffInterruptible` (+ зеркальный `TestConsumeLoopRetryBackoffElapsesNormally` на обычный таймаут) |
| 6. `worker_message_retries_total` инкрементируется на RETRY | `TestConsumeLoopRetryPath::test_worker_message_retries_total_increments_on_retry` |
| 7. Количество файлов вне 2–10 → 0 вызовов `.read()` | `TestReadBatchFilesCountCheckedBeforeAnyRead` |
| 8. Файл больше лимита → ровно один `read(MAX+1)`, остальные не читаются, 413 | `TestReadCappedFile`, `TestReadBatchFilesPerFileCap` |
| 9. Суммарный лимит (150 МБ) достижим: 4×50 МБ → 413, оставшиеся не читаются | `TestReadBatchFilesRunningTotal` (в т.ч. на боевых константах без monkeypatch) |
| 9b. Ловушка m1: константы связаны поздно через модуль (`photo_service.<CONST>`, не импортированное значение) | `TestReadCappedFile::test_late_binding_honors_a_patch_on_the_photo_service_module` |
| 10. `_maybe_complete_batch` вызывается на всех 3 путях (done/failed/skip-дубль) | `TestMaybeCompleteBatchCalledOnAllTerminalPaths` (3 теста) |
| 11. Батч становится completed БЕЗ единого GET | `TestMaybeCompleteBatchDirect` (вызов напрямую через `AnalysisProcessor`, GET не участвует) |
| 12. `try_complete` атомарен: повторный вызов на completed батче — no-op | `TestMaybeCompleteBatchDirect::test_lost_race_rowcount_zero_does_not_raise_and_still_commits` (+ pre-existing `test_batch_repository.py::TestTryComplete::test_returns_zero_rowcount_when_nothing_matched`) |
| 13. `BatchService.get_batch` не пишет (ни commit, ни try_complete) | pre-existing `test_batch_service.py::TestGetBatch` (не менялся мной — уже покрывает) |
| 14. Публичный ответ GET не изменился (completed+best даже если БД ещё processing) | pre-existing `test_batch_service.py::TestGetBatch::test_completed_status_with_best_photo_id_when_all_terminal` |
| 15. Реестр worker-процесса без api-метрик и наоборот (импорт-тест) | `test_metrics_isolation.py` (subprocess на реальный `app.main`/`app.worker.main`) |
| 16. Падение `commit()` в `create_photo` → rollback + `delete_file` + 503 `DatabaseUnavailable`, формат ошибки сохранён | `test_photo_service.py::TestCreatePhotoCommitFailureCompensation` + `test_photos_endpoints.py` |
| 17. То же для `create_batch`, удалены ВСЕ `saved_object_keys` | `test_photo_service_batch.py::TestBatchCommitFailureCompensation` + `test_batches_endpoints.py` |

## Что покрыто / не покрыто

**Покрыто (новое):**
- Всё F1: RETRY-путь (не-коммит + `seek` с точными аргументами), обе ветки ребаланса (`seek`/`commit`), обе стороны анти-hot-loop паузы (прервана `stop_event` / обычный таймаут `except TimeoutError: pass`), новая poison-pill ветка (невалидный UUID), метрики дропов/ретраев, живучесть цикла после сбоя (следующее сообщение всё равно обрабатывается).
- Всё F2/F8: ранний отказ по количеству (0 файлов до чтения), капнутое чтение на файл (ровно один `read(MAX+1)`, соседние файлы не тронуты), бегущая сумма (включая позиционирование "оверсайз в середине списка"), регрессионный тест на боевых константах (150 МБ, без monkeypatch — фиксирует закрытие BLK-2/M2), ловушка m1 (позднее связывание через модуль).
- Всё F3: `_maybe_complete_batch` изолированно (no-op ветки: нет batch_id / batch отсутствует / уже completed / есть нетерминальные / пустой список фото; полный путь: completed + best_photo_id; проигранная гонка rowcount=0); вызов на всех трёх терминальных путях `process()` (skip/done/failed) доказан через мок `batches.get_by_id`.
- Всё F4: полная двухсторонняя изоляция реестров через subprocess-импорт реальных `app.main`/`app.worker.main` (не только «одна сторона», как раньше).
- Всё F5: компенсация `create_photo`/`create_batch` (удаление объекта(ов), пробрасывание `DatabaseUnavailable`, не-маскирование при падении самого `delete_file`, «нет компенсации на happy path»), плюс HTTP-уровень (503 + `{error_code, message, request_id}` не изменился).
- Побочно, для закрытия пробелов coverage: `PhotoRepository.get_batch_id` (репозиторный уровень, ранее исполнялся только через моки) и `except TimeoutError: pass` в consumer (обычное завершение паузы без сигнала останова).

**Не покрыто (осознанно, с обоснованием):**
- **F6 (`restart: unless-stopped` в compose)** — статическая конфигурация YAML, не код; уже верифицирована исполнением `docker compose config` обоими ревьюерами (см. таблицы гейтов в `40_review-1.md`/`41_review-2.md`). Не входила в список оркестратора; добавление отдельного YAML-парсинг-теста возможно, но не даёт покрытия бизнес-логики — оставлено на усмотрение оркестратора/documenter.
- **F7 (insecure gRPC, дефер в TASK-003)** — не функциональное изменение (только комментарий), тестировать нечего.
- **Дашборд Grafana (`photos_pending{job="photo-api"}` → одна серия)** — верифицируется живым Prometheus/Grafana-стеком, не юнит-тестом; вне скоупа test-writer.
- **m2/m3 (review-1, MINOR, ещё не исправлены кодером):** `session.rollback()` в F5-компенсации сам не защищён try/except (`photo_service.py:169,264`), и `StorageUnavailable`-ветка `create_batch` (строки 257-258, СОСЕДНЯЯ с F5, но не входящая в F5) не оборачивает `delete_file` в try/except. Тест на «rollback тоже падает» я сознательно НЕ писал: код сейчас пробросил бы исходное исключение как есть (не `DatabaseUnavailable`), то есть тест либо документировал бы текущий баг как «ожидаемое поведение» (плохо — заморозит баг), либо сразу упал бы (это не входит в мой мандат «не чинить код»). Рекомендация оркестратору: вернуть коду коду фикс m2/m3, затем добавить регрессионный тест на «rollback тоже падает → всё равно 503, не 500» и «StorageUnavailable-путь батча: delete_file падает → всё равно 503, не голое исключение».
- **m4 (двойной учёт `worker_messages_processed_total` при падении `_maybe_complete_batch`)** — тоже открытый MINOR у review-1, не код-фикс в этой итерации; тест не писал по той же причине (задокументировал бы известный, ещё не исправленный изъян).
- **Живой прогон `docker compose up --build`** — вне мандата test-writer (ручная/CI проверка).

**Реальных багов при написании тестов НЕ найдено.** Все проверенные ветки (F1–F5) вели себя ровно так, как описано в `20_design.md`/`30_impl.md`/review-раундах; открытые non-blocking находки (m2/m3/m4) — это уже известные, задокументированные ревьюером-1 пункты, не новые находки этой итерации.

## Как запустить

Из `photo-service/`:

```bash
uv run ruff check .
uv run pytest -q -m "not integration"
uv run pytest -q -m "not integration" --cov=app --cov-report=term-missing
```

Новые тесты по отдельности:

```bash
uv run pytest -q tests/test_worker_consumer.py tests/test_uploads.py \
  tests/test_analysis_processor.py tests/test_metrics_isolation.py \
  tests/test_photo_service.py tests/test_photo_service_batch.py \
  tests/test_photo_repository.py tests/test_photos_endpoints.py \
  tests/test_batches_endpoints.py
```

`test_metrics_isolation.py` спавнит 2 отдельных subprocess'а (`python -c "import app.main/app.worker.main; ..."`) — не требует запущенных Kafka/Postgres/MinIO (импорт модулей ленивый, engine/consumer не подключаются при импорте), но чуть медленнее обычных unit-тестов (~1 c на процесс).

Детерминированность/независимость от порядка: все новые тесты либо используют локальные фейки/моки (без общего состояния), либо для глобальных Prometheus-счётчиков (`worker_message_retries_total`, `worker_messages_dropped_total`) сравнивают значение ДО/ПОСЛЕ (дельта), а не абсолютное число — не ломаются от порядка запуска или повторного прогона. Прогнан дважды подряд, результат идентичен (425 passed оба раза).

## Результат прогона

```
uv run ruff check .
All checks passed!

uv run pytest -q -m "not integration"
425 passed, 1 deselected in ~15.5s
```

Coverage (`--cov=app --cov-report=term-missing`), до/после:

| Файл | До (review-1, раунд 2) | После |
|---|---|---|
| `app/api/uploads.py` | 90% (69, 93 непокрыты) | **100%** |
| `app/services/analysis_processor.py` | 88% (88-100 непокрыты, весь `_maybe_complete_batch`) | **100%** |
| `app/services/photo_service.py` | 87% (182-195, 277-291 непокрыты — обе компенсации F5) | **100%** |
| `app/worker/consumer.py` | 76% (147-199, 243-249 непокрыты — RETRY, ребаланс, UUID-poison) | **100%** |
| `app/repositories/photo_repository.py` | (не отмечался ревьюером, но `get_batch_id` не был вызван вживую) | **100%** |
| **TOTAL** | 94% | **98%** |

Непокрытые 20 строк в TOTAL — исключительно сгенерированный protobuf-код
(`app/grpc_gen/analyzer_pb2*.py`, вне скоупа, исключён из ruff по тем же
причинам) и строка `if __name__ == "__main__": main()` в `app/worker/main.py`
(стандартный guard, не тестируется нигде в проекте, pre-existing).

Итого тестов: **425 passed, 1 deselected** (интеграционный тест на
эфемерном Postgres, требует Docker — не запускался, как и раньше).
Было до этой итерации: 370 passed. Добавлено чистых **55 тестов**
(53 новых `def test_...` + 2 новых параметра в существующих
`@pytest.mark.parametrize` матрицах ошибок в `test_photos_endpoints.py`/
`test_batches_endpoints.py`).

Разбивка по файлам:
- `tests/test_worker_consumer.py`: +16 (F1)
- `tests/test_analysis_processor.py`: +11 (F3)
- `tests/test_uploads.py` (новый): +14 (F2/F8)
- `tests/test_photo_service.py`: +4 (F5, create_photo)
- `tests/test_photo_service_batch.py`: +3 (F5, create_batch)
- `tests/test_photo_repository.py`: +3 (`get_batch_id`, закрытие coverage)
- `tests/test_metrics_isolation.py` (новый): +2 (F4)
- `tests/test_photos_endpoints.py` / `tests/test_batches_endpoints.py`: +1/+1 параметр (F5, HTTP-контракт)

Прогнан дважды подряд — оба раза `425 passed, 1 deselected`, без флаки.

## Найденные при написании проблемы

Новых багов не найдено. Подтверждены (не переоткрыты, только зафиксированы
тестами) уже известные из `40_review-1.md` открытые non-blocking находки:
**m2** (`session.rollback()` в F5-компенсации не защищён), **m3**
(`StorageUnavailable`-ветка `create_batch` не оборачивает `delete_file`),
**m4** (двойной учёт `worker_messages_processed_total` при падении
`_maybe_complete_batch`) — тесты на них сознательно не писались (см. «Что
не покрыто»); передаю оркестратору как готовый список для следующей
итерации кодера, после которой соответствующие регрессионные тесты стоит
добавить.

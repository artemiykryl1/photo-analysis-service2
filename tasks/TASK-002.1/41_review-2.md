---
task_id: TASK-002.1
reviewer: reviewer-2
model: haiku
status: APPROVE
inputs:
  - specs/feature-upload/tasks.md (TASK-002.1 F1–F8)
  - tasks/TASK-002.1/20_design.md
  - tasks/TASK-002.1/30_impl.md
  - photo-service/** (code review)
timestamp: 2026-07-21T00:00:00Z
---

# TASK-002.1 — Ревью-2 (безопасность, конвенции, наблюдаемость, покрытие)

## Статус
**APPROVE** — все фиксы F1–F8 реализованы по существу; критических уязвимостей не обнаружено.

---

## F2 — DoS-вектор: проверка ПО СУЩЕСТВУ

**Вердикт:** ✅ Решена полностью.

**Проверка механики:**
1. **Файлов до чтения** (app/api/uploads.py:81): `if not (MIN_BATCH_SIZE <= len(files) <= MAX_BATCH_SIZE)` срабатывает ДО единого `.read()` — 11 файлов отклоняются на валидации, прежде чем код войдёт в цикл.
2. **Per-file cap** (uploads.py:67, 90): `await file.read(MAX_FILE_SIZE_BYTES + 1)` читает максимум 50 МБ+1 байт; превышение → `PayloadTooLargeError` (413) немедленно, без дочитывания остатка.
3. **Running total** (uploads.py:88-95): переменная `total` накапливает размеры файлов; при превышении `BATCH_MAX_TOTAL_BYTES` → 413, цикл прерывается, последующие файлы не читаются.
4. **Одиночный upload** (photos.py:59): использует `read_capped_file`, то же капнутое чтение.
5. **Defense-in-depth** (photo_service.py:206-220): сервис сохраняет свои проверки count/total-size — вторая линия валидации (unit-тесты на сервис остаются зелёными, monkeypatch работает).

**Остаточные риски:**
- Starlette уже спулит multipart-тело в памяти (SpooledTemporaryFile) ДО входа в эндпоинт — это не в скоупе (F8, задокументировано в docstring uploads.py).
- Стриминг в MinIO (chunked put_object) — вне скоупа (F8).
- Ранний отказ по Content-Length — вне скоупа (F8, обоснован в docstring).

**Итог:** Проблема F2 решена по существу (лимиты действуют ДО материализации), не только по наличию кода.

---

## F1 — Poison-pill и at-least-once семантика

**Вердикт:** ✅ Корректно реализовано.

**Механика:**
1. **Enum MessageOutcome** (consumer.py:86-93): читаемая типизация (COMMIT | RETRY).
2. **Poison-pill классификация** (consumer.py:140-179):
   - Unparsable JSON → `logger.exception(...)` + `COMMIT` (строка 149).
   - Missing/blank `photo_id`/`object_key` → `logger.warning(...)` + `COMMIT` (строка 160).
   - **R1 (одобрено оркестратором):** non-UUID `photo_id` → `logger.warning(...)` + `COMMIT` (строка 175) — скопидально: непарсируемый UUID — это заведомо-неисполнимое сообщение, логирование содержит сам `photo_id` для анализа.
   - Неожиданное исключение из `process()` → `logger.exception(...)` + `return RETRY` (строка 190) — логируется стек, photo_id из payload.
3. **consume_loop :: offset commit logic** (consumer.py:112-132):
   - `COMMIT` → `await consumer.commit()` (строка 113).
   - `RETRY` → `consumer.seek(tp, message.offset)` (строка 120, синхронный вызов — правильно для aiokafka) + `logger.warning(...)` (строка 121) + пауза `asyncio.wait_for(stop_event.wait(), timeout=_ERROR_BACKOFF_SECONDS)` (строка 130, прерывается shutdown-ом).
4. **Анти-hot-loop пауза** (_ERROR_BACKOFF_SECONDS = 1.0, строка 83) — обоснована.

**Логирование детали:**
- При poison-pill: достаточно информации (payload, photo_id) для post-mortem, без целиком файла (который мог бы быть большим).
- При RETRY: включены координаты (topic, partition, offset) для отладки и трассировки.
- trace_id пробрасывается и устанавливается в contextvars (строки 181, 196).

**Документация модуля** (consumer.py:1-61): подробно объясняет почему нужен `seek()` (иначе getone() уже сдвинул позицию), фиксирует осознанный компромисс (детерминированная ошибка блокирует партицию, DLQ вне скоупа).

**Обновление тестов:** Фикстуры в test_worker_consumer.py обновлены на валидные UUID-строки (e.g. "11111111-1111-1111-1111-111111111111") — это правомерная правка, так как F1 вводит требование парсируемости photo_id; non-UUID плейсхолдеры ("photo-1", "p1") теперь отклоняются как poison-pill, что нарушило бы семантику тестов.

**Итог:** F1 решена, семантика at-least-once с seek() и классификацией poison-pill — правильная.

---

## F3 — Завершение батча в worker; get_batch read-only

**Вердикт:** ✅ Реализовано корректно, избегает race-условий.

**Механика:**
1. **`_maybe_complete_batch` в analysis_processor.py:77-106:**
   - Получает `batch_id` через `get_batch_id` (новый метод photo_repository, строка 84).
   - Fetches batch with eager-loaded photos + analysis (selectinload в get_by_id).
   - Проверяет `batch.status == "processing"` и все фото терминальны (done/failed).
   - Вычисляет `best_photo_id` через `select_best_photo` (чистая функция, без изменений).
   - Вызывает `try_complete` (атомарный UPDATE с `WHERE status='processing'`).
   - Коммитит и логирует (строка 100).
2. **Вызовы на трёх путях:**
   - skip (rowcount==0, строка 127): дублевая переставка → self-healing.
   - done (успешный анализ, строка 193): терминальная запись.
   - failed (после лимита ретраев, строка 210): терминальная запись.
3. **`try_complete` в batch_repository.py:38-59:**
   - `UPDATE batches SET status='completed', best_photo_id=:b WHERE batch_id=:id AND status='processing'`.
   - Возвращает rowcount (0 или 1).
   - Не коммитит сам (caller's ответственность) — строка 52.
4. **`get_batch` в batch_service.py:61-107:**
   - Strictly read-only (нет `mark_completed`, нет `commit`).
   - Вычисляет `status`/`best_photo_id` для отображения (с вызовом `select_best_photo` только для ответа, не для персистента).
   - Клиент видит completed+best, как только worker его персистит, или вычислено на лету если еще не персистено — поведение неотличимо.

**Race-условия предотвращены:**
- Два worker-а на последних фото батча: оба вычисляют детерминированный `best`, оба вызывают `try_complete`, первый получает rowcount=1, второй rowcount=0 → no-op.
- Крэш между terminal-commit и `_maybe_complete_batch`: реставление через skip-путь (строка 127) при переставке.
- GET параллельно с worker-завершением: read-only, никаких racing UPDATE.

**Итог:** F3 корректна, архитектура цельна.

---

## F4 — Раскол metrics.py на API/worker

**Вердикт:** ✅ Гигиена импортов соблюдена.

**Проверка:**
1. **Новые модули:**
   - `metrics_api.py`: 6 метрик (photos_uploaded_total, http_requests_total, http_request_duration_seconds, storage_upload_errors_total, kafka_publish_errors_total, photos_pending).
   - `metrics_worker.py`: 6 метрик (photo_analysis_started_total, photo_analysis_completed_total, photo_analysis_failed_total, photo_analysis_duration_seconds, analyzer_grpc_errors_total, worker_messages_processed_total).
   - Старый `metrics.py` удалён (grep не находит импортов `from app.integrations.metrics`).
2. **Импорты (полная таблица соответствия из дизайна F4):**
   - `app/main.py:27` → `metrics_api` (photos_pending).
   - `app/api/metrics_middleware.py:23` → `metrics_api` (http_*).
   - `app/services/photo_service.py:44` → `metrics_api` (photos_uploaded_total, storage_upload_errors_total).
   - `app/services/outbox.py:23` → `metrics_api` (kafka_publish_errors_total).
   - `app/services/analysis_processor.py:40` → `metrics_worker` (6 worker-метрик).
   - Тесты обновлены соответственно.
3. **worker/main.py (граф импорта):** не импортирует `metrics_api`, только `metrics_worker` (транзитивно через analysis_processor) — гигиена соблюдена.
4. **API/main.py:** не импортирует `metrics_worker`.
5. **Дашборд:** Grafana query обновлён (photos_pending{job="photo-api"}) → явная защита от дублирования серий.

**Инвариант F4 соблюдён:** worker-процесс не затянул api-метрики; api не затянул worker-метрики.

**Итог:** F4 выполнена полностью.

---

## F5 — Компенсация MinIO при падении commit

**Вердикт:** ✅ Best-effort удаление с логированием.

**Механика:**
1. **DatabaseUnavailable** (errors.py:43-58): новый класс (не переиспользует StorageUnavailable), но переиспользует публичный код `SERVICE_UNAVAILABLE` (503) — контракт HTTP не меняется, семантика логирования точная.
2. **create_photo** (photo_service.py:166-183):
   - `try: await session.commit()`.
   - `except Exception: await session.rollback()` + best-effort `delete_file` + `logger.error(...)` + `raise DatabaseUnavailable`.
   - Комментарий `noqa: BLE001` (почему ловим все Exception, а не BaseException) обоснован.
3. **create_batch** (photo_service.py:261-277):
   - Тот же паттерн, но цикл по всем `saved_object_keys` (批量удаление).
   - Best-effort delete для каждого (свой try/except, не маскирует исходную ошибку commit).
   - Логирование содержит batch_id и photo_count.

**Риск-профиль:**
- Delete-fail (e.g. MinIO тоже вниз): логируется warning, объект может остаться сиротой (ручная чистка вне скоупа) — приемлемо для MVP.
- Commit-fail редок (БД обычно либо доступна, либо нет) — логирование достаточно для post-mortem.

**Итог:** F5 приемлема, обработка ошибок разумна.

---

## F6 — Restart-политики

**Вердикт:** ✅ Все 8 сервисов имеют `restart: unless-stopped`.

Проверено `docker-compose.yml`:
- postgres:4
- minio:21
- kafka:42
- analyzer-stub:73
- api:79
- worker:126
- prometheus:169
- grafana:179

**Итог:** F6 выполнена.

---

## F7 — gRPC TLS (дефер в TASK-003)

**Вердикт:** ✅ Дефер обоснован и задокументирован.

**Комментарий в analyzer_client.py:** явно объясняет, что `insecure_channel` — осознанный выбор для одно-сетевого MVP-compose; конфигурируемый TLS вводится в TASK-003. Никаких мёртвых env-переменных не добавлено.

**Итог:** F7 корректна.

---

## Конвенции и читаемость

**Типизация:**
- Правильное использование `uuid.UUID | None`, `list[tuple[str | None, bytes]]`, enum.
- Type hints в method signatures полные.

**Docstrings:**
- `uploads.py`: подробный, объясняет границу F8 (стриминг вне скоупа).
- `consumer.py`: обширный модульный docstring, объясняет зачем нужен `seek()`.
- `batch_service.py`: объясняет переход с write-on-GET на read-only.
- `analysis_processor.py`: документирует F3, F4 гигиену.

**Комментарии:**
- Inline комментарии объясняют нетривиальные моменты (e.g. почему `seek()` синхронный, почему poison-pill для non-UUID).
- `noqa` комментарии обоснованы (BLE001 для `except Exception`).

**Мёртвый код:**
- `mark_completed` в batch_repository удалён (заменён на `try_complete`) — проверено `grep`.
- Старый `metrics.py` удалён.
- Нет неиспользуемых импортов.

**Ruff:**
- Кодер доложил: `All checks passed!`
- Проведена выборочная проверка импортов — чистые.

**Итог:** Конвенции соблюдены.

---

## Наблюдаемость

**Логирование (F1):**
- Poison-pill: EXCEPTION или WARNING с деталями (photo_id, payload, topic/partition/offset).
- RETRY: WARNING с координатами.
- Успех: INFO.
- trace_id пробрасывается в contextvars (trace_id_var).

**Метрики:**
- Разделены по процессам (api vs worker).
- Дашборд обновлён на явный job-фильтр.
- Лейблы без кардинальных значений (photo_id/object_key не используются; `reason` только на photo_analysis_failed_total с 2 значениями).

**Health/readiness:**
- Не меняются, остаются из TASK-002.

**Итог:** Наблюдаемость адекватна.

---

## Покрытие тестами — пробелы для test-writer

Критерии F1–F8 спеки и дизайн. Какие остались НЕ покрыты unit-тестами (требуют test-writer):

1. **F1 — RETRY-ветка (offset НЕ коммичен, seek вызван):**
   - Тест: mock consumer.seek, проверить что он вызван с правильными (topic, partition, offset).
   - Тест: verify что consumer.commit НЕ вызывается в этом пути.

2. **F1 — poison-pill (non-UUID photo_id):**
   - Тест: message с `photo_id="abc-xyz"` → COMMIT (poison-pill), processor.process НЕ вызывается.

3. **F2 — ранний отказ по count:**
   - Тест: `read_batch_files([file]*11)` → BatchSizeError, ни один `.read()` не вызывается.

4. **F2 — per-file cap:**
   - Тест: 60 МБ файл → PayloadTooLargeError, next files НЕ читаются.

5. **F2 — running total:**
   - Тест: 5 файлов по 120 МБ каждый (совокупно 600 МБ > 500 МБ) → прерывается на 5-м файле или раньше, остаток НЕ читается.

6. **F3 — batch completion в worker:**
   - Тест: обновить `_maybe_complete_batch` — все фото терминальны → `try_complete` вызван с правильным rowcount=1.
   - Тест: уже терминальный batch → `try_complete` вызван но rowcount=0 (no-op).

7. **F3 — self-healing через skip-путь:**
   - Тест: переставка сообщения для терминального photo (skip rowcount=0) → `_maybe_complete_batch` завершает batch при необходимости.

8. **F3 — get_batch read-only:**
   - Тест: mock repository/session → verify что `get_batch` НЕ вызывает `try_complete` и НЕ вызывает session.commit.

9. **F4 — метрики не задвоены:**
   - Тест: `import app.worker.main` → registry НЕ содержит `photos_pending` или http_*; `import app.main` → registry НЕ содержит `photo_analysis_*`.

10. **F5 — компенсация MinIO:**
    - Тест: mock session.commit падает → `delete_file` вызван для каждого файла, 503 отправлена.
    - Тест: delete_file сам падает → логируется warning, исходная DatabaseUnavailable пробрасывается.

11. **F6 — restart-политики:**
    - Тест: `docker compose config` → все 8 сервисов содержат `restart: unless-stopped`.

---

## Открытые вопросы / нотабене для последующих фаз

- **TASK-003 (реальный analyzer):** TLS конфигурация, реальный error handling.
- **DLQ/reaper:** "залипшие" партиции (детерминированная ошибка) требуют manual intervention или автоматический reaper — вне скоупа TASK-002.1.
- **Стриминг:** полнофункциональное потоковое чтение (chunked multipart → chunked MinIO put) — F8, будущая оптимизация.

---

## Суммарный вывод

**Статус:** ✅ **APPROVE**

Все 8 багфиксов (F1–F8) реализованы:
- **F2 ПО СУЩЕСТВУ** (лимиты действуют ДО материализации, не только по наличию кода).
- **F1**: at-least-once с seek(), классификация poison-pill, логирование достаточно.
- **F3**: завершение батча в worker, атомарный try_complete, get_batch read-only.
- **F4**: раскол метрик соблюдает гигиену импортов.
- **F5**: компенсация MinIO с best-effort delete и правильным кодом ошибки.
- **F6/F7**: restart-политики, дефер TLS обоснован.

**Критических уязвимостей не обнаружено.** Конвенции соблюдены (типизация, docstrings, комментарии). Наблюдаемость адекватна (логирование с trace_id, метрики разделены, дашборд обновлён).

**Пробелы в покрытии** (для test-writer): RETRY-путь consumer.py, ранний отказ в uploads.py, self-healing в analysis_processor, read-only проверка get_batch, метрики не задвоены, компенсация MinIO — список в разделе «Покрытие тестами».

**Без замечаний для пересмотра.**

---

**Рекомендация:** → test-writer для покрытия пробелов → CI/зелёные тесты → публикация PR.

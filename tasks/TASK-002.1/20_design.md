---
task_id: TASK-002.1
agent: architect
model: opus
status: done
inputs:
  - specs/constitution.md
  - specs/feature-upload/tasks.md   # раздел TASK-002.1 (F1–F8, критерии, ограничения)
  - tasks/TASK-002.1/10_context.md
  - tasks/TASK-002.1/00_orchestration.md
outputs:
  - tasks/TASK-002.1/20_design.md
spec_refs:
  - "TASK-002.1 F1 (at-least-once / commit offset)"
  - "TASK-002.1 F2/F8 (батч-лимиты до материализации)"
  - "TASK-002.1 F3 (завершение батча в worker, get_batch read-only)"
  - "TASK-002.1 F4 (раскол metrics.py)"
  - "TASK-002.1 F5 (компенсация MinIO при падении commit)"
  - "TASK-002.1 F6 (restart-политики compose)"
  - "TASK-002.1 F7 (gRPC TLS — решение architect)"
timestamp: 2026-07-21T00:00:00Z
---

# TASK-002.1 — Дизайн багфиксов F1–F8

## Соответствие constitution

Ни один фикс не вводит нового сервиса/контракта и не нарушает принципы:
микросервисы (api/worker/analyzer-stub), HTTP+gRPC, Kafka, идемпотентность
(атомарный claim), ретраи (уже есть), наблюдаемость (Prometheus), K8s (compose
для MVP). Отдельно:

- **F1** усиливает at-least-once (constitution §2.4 «commit offset только после
  обработки»). DLQ (constitution §2.4) осознанно вне скоупа — TASK-002 зафиксировал
  «после лимита ретраев → failed, без dead-letter топика»; компромисс F1 (детерминированный
  poison-код блокирует партицию) — прямое следствие этого решения, документируется.
- **F5** возвращает 503, а не 500, при недоступности зависимости (constitution §3.2
  «MinIO/зависимость недоступна → 503»).
- **F7**: constitution §2.2 объявляет gRPC опциональным для внутренних путей и не требует
  TLS внутри mesh; §3.3 требует «нет хардкода секретов» — TLS откладываем в TASK-003
  (обоснование ниже), секретов при этом не появляется.

Схема БД **не меняется**, миграции **не нужны**, публичные HTTP-контракты
(пути/коды/тела `{error_code,message,request_id}`) **не меняются**.

---

## F1 — at-least-once: новая семантика commit offset

### Проблема (подтверждена по коду)
`consume_loop` (`app/worker/consumer.py:59-66`) вызывает `await consumer.commit()`
**безусловно** после `_handle_message`, а `_handle_message` (строки 99-108) ловит
все `Exception` из `process()` и молча возвращается. Итог: программная ошибка
(БД недоступна до claim, любой баг в `process()`) → offset коммитится → сообщение
теряется навсегда.

### Решение

Ввести явный «исход обработки» и отдать решение о коммите консюмеру.

1. **Enum исхода** (в `consumer.py`):
   ```
   class MessageOutcome(enum.Enum):
       COMMIT = "commit"   # обработано ИЛИ подтверждённый poison-pill -> двигаем offset
       RETRY  = "retry"    # неожиданная ошибка -> НЕ коммитим, Kafka передоставит
   ```
   (Выбран enum, а не bool, — читается яснее и оставляет место под третий исход
   DLQ в будущем; но семантика ровно «коммитить / не коммитить».)

2. **`_handle_message` возвращает `MessageOutcome`:**
   - Небитый/битый JSON нельзя распарсить → лог + `return COMMIT` (poison-pill).
   - `payload` не dict / `photo_id`|`object_key` отсутствуют или пустые → лог + `return COMMIT`
     (poison-pill; логика `_is_missing` **без изменений**).
   - **Дополнение к poison-pill (решение architect, см. риск R1):** `photo_id` не парсится
     как UUID → это тоже «недоставляемо навсегда» (claim никогда не выполнится) →
     трактуем как poison-pill: лог + `return COMMIT`. Реализовать проверкой
     `uuid.UUID(payload["photo_id"])` в блоке валидации `_handle_message` (не в `process()`),
     оборачивая в try/except ValueError. Это честнее к цели F1, чем блокировать партицию
     на заведомо-неисполнимом сообщении.
   - `await processor.process(...)` завершилась без исключения (в т.ч. терминальные
     `done`/`failed` и skip-дубль rowcount==0) → `return COMMIT`.
   - `process()` бросила любое **неожиданное** исключение → `logger.exception(...)` +
     `return RETRY`. (Ничего наружу не пробрасываем — цикл не должен умирать.)

3. **`consume_loop` решает по исходу:**
   ```
   outcome = await _handle_message(message, processor)
   if outcome is MessageOutcome.COMMIT:
       await consumer.commit()
   else:  # RETRY
       tp = TopicPartition(message.topic, message.partition)
       consumer.seek(tp, message.offset)     # ВАЖНО: перемотать назад
       logger.warning("offset not committed; message will be redelivered", extra={...})
       # анти-hot-loop, прерывается shutdown-ом:
       try:
           await asyncio.wait_for(stop_event.wait(), timeout=_ERROR_BACKOFF_SECONDS)
       except TimeoutError:
           pass
   ```

### Почему нужен `consumer.seek(...)` (ключевой момент, проверять на ревью)
`aiokafka` при `getone()` **двигает in-memory позицию** партиции. Если просто «не
коммитить» и продолжить цикл, следующий `getone()` вернёт **следующее** сообщение,
а не то же самое; а когда оно успешно обработается, `commit()` закоммитит позицию
**за** упавшим сообщением → упавшее сообщение всё равно потеряется. Спека F1 требует
«Kafka передоставит **то же** сообщение» и «продолжить цикл» — это выполнимо только
если перемотать позицию на `message.offset` (`seek`). Именно поэтому наивное «убрать
`commit()`» — фикс, НЕ решающий заявленную проблему (тот самый процессный урок из
00_orchestration.md). `seek` + пауза + continue = сообщение перечитывается на следующей
итерации; при детерминированной ошибке партиция «залипает» на нём — **осознанный
компромисс** (DLQ вне скоупа), фиксируется в docstring.

`consumer.seek(...)` в aiokafka **синхронный** (не корутина) — вызывать без `await`.

### Константа
`_ERROR_BACKOFF_SECONDS = 1.0` — модульная константа рядом с `_POLL_TIMEOUT_SECONDS`.
(Можно позже вынести в Settings; сейчас достаточно константы.)

### Graceful shutdown
`stop_event` проверяется в начале каждой итерации `while`; пауза после RETRY
прерывается через `asyncio.wait_for(stop_event.wait(), ...)` — то же, что в
`outbox.py`. Итого задержка остановки ≤ `_ERROR_BACKOFF_SECONDS`.

---

## F2 / F8 — лимиты батча ДО материализации в RAM

### Проблема
`app/api/batches.py:35` читает **все** файлы целиком (`await f.read()`) до любых
проверок; лимиты 2–10 и суммарный размер проверяются в `create_batch`, когда всё
уже в памяти. Одиночный `app/api/photos.py:58` тоже читает весь файл.

### Решение — чистое разделение слоёв

Каппинг чтения физически требует `UploadFile` (FastAPI-объект), а сервис не должен
знать FastAPI. Значит **механика капнутого чтения живёт в api**, а **лимиты (значения)
остаются бизнес-правилом сервиса** — единственный источник истины. Связка: тонкий
api-хелпер импортирует константы из `photo_service` и бросает доменные ошибки из
`app/core/errors.py` (они уже маппятся в HTTP-хендлерах).

**Новый модуль `app/api/uploads.py`** (api-слой, ему МОЖНО знать про `UploadFile`):
```
from fastapi import UploadFile
from app.core.errors import BatchSizeError, PayloadTooLargeError
from app.services.photo_service import (
    MAX_FILE_SIZE_BYTES, MIN_BATCH_SIZE, MAX_BATCH_SIZE, BATCH_MAX_TOTAL_BYTES,
)

async def read_capped_file(file: UploadFile) -> bytes:
    data = await file.read(MAX_FILE_SIZE_BYTES + 1)   # читаем максимум cap+1
    if len(data) > MAX_FILE_SIZE_BYTES:
        raise PayloadTooLargeError("File exceeds the maximum allowed size (50 MB)")
    return data

async def read_batch_files(files: list[UploadFile]) -> list[tuple[str | None, bytes]]:
    if not (MIN_BATCH_SIZE <= len(files) <= MAX_BATCH_SIZE):   # ДО любого чтения
        raise BatchSizeError(
            f"Batch must contain between {MIN_BATCH_SIZE} and {MAX_BATCH_SIZE} files "
            f"(got {len(files)})"
        )
    result: list[tuple[str | None, bytes]] = []
    total = 0
    for f in files:
        data = await read_capped_file(f)      # per-file cap; при переполнении — 413, остаток не читаем
        total += len(data)
        if total > BATCH_MAX_TOTAL_BYTES:      # бегущая сумма; остаток не читаем
            raise PayloadTooLargeError(
                f"Batch total size exceeds the maximum allowed {BATCH_MAX_TOTAL_BYTES} bytes"
            )
        result.append((f.filename, data))
    return result
```

Ключевые свойства (то, что будут проверять ревьюеры «по существу»):
- **Count-check раньше чтения:** 11 файлов → `BatchSizeError` до единого `.read()`.
- **Per-file cap:** `read(MAX+1)` не втягивает 2 ГБ в память — максимум 50 МБ+1;
  превышение → 413 сразу, следующие файлы не читаются.
- **Running total:** переполнение суммарного лимита → 413 по бегущей сумме, остаток
  не читается.
- Пустой файл проходит хелпер (len 0 ≤ cap) — проверку «пустой → 400» оставляет
  сервис (`_validate`), контракт не меняется.

**Правки эндпоинтов:**
- `app/api/batches.py::upload_batch`: `files = await read_batch_files(file)` вместо
  списочного включения; далее как было `return await service.create_batch(session, files)`.
- `app/api/photos.py::upload_photo`: `data = await read_capped_file(file)` вместо
  `await file.read()`; далее `return await service.create_photo(session, file.filename, data)`.

**Сервис оставляем как есть (defense-in-depth, авторитетный слой):** `create_batch`
сохраняет свои проверки count/total-size, `_validate` — проверку размера. Api-хелпер —
только ранняя защита памяти; сервис остаётся источником истины бизнес-правила
(и это сохраняет зелёными сервисные тесты, которые monkeypatch-ят
`photo_service_module.BATCH_MAX_TOTAL_BYTES` и зовут `create_batch` напрямую).

**Константы НЕ переносим** из `photo_service.py` (иначе ломается monkeypatch в
`tests/test_photo_service*.py`). Api-хелпер импортирует их оттуда. Это единственный
источник значений.

### F8 — граница (задокументировать)
В docstring `app/api/uploads.py` явно зафиксировать: капнутое чтение ограничивает RAM
на файл (≤50 МБ+1) и на батч (≤`BATCH_MAX_TOTAL_BYTES`), но **потоковая загрузка в
MinIO (chunked put_object) и ранний отказ по Content-Length до парсинга multipart —
вне скоупа**. Причина: Starlette уже спулит тело multipart в `SpooledTemporaryFile`
(на диск при переполнении буфера), а стриминг в MinIO — существенно большая переработка
слоя storage; отложено. (Ранний отказ по Content-Length — опционально, НЕ реализуем:
для multipart это размер всего тела, и на момент входа в эндпоинт тело уже распарсено;
корректно это делается middleware до парсинга — отдельная задача.) Продублировать
одну строку про границу в README (шаг документатора, 80_docs).

---

## F3 — завершение батча в worker; `get_batch` строго read-only

### Проблема
`app/services/batch_service.py::get_batch` (строки 75-84) на GET делает
`mark_completed` + `commit`. Следствия: батч не станет completed без GET; параллельные
GET — конкурирующие UPDATE; GET небезопасен (ретраи прокси пишут в БД).

### Решение — перенести завершение в `analysis_processor`

**Важное архитектурное ограничение (связка с F4):** `analysis_processor` работает в
worker-процессе. Чтобы переиспользовать `select_best_photo`, он импортирует
`batch_service`. Но `batch_service` сейчас импортирует `_analysis_response` из
`photo_service`, а `photo_service` импортирует `metrics_api` — то есть worker
транзитивно затянет api-метрики и **вернёт баг F4**. Поэтому сначала расцепляем:

**Шаг-развязка: вынести `_analysis_response` в нейтральный модуль
`app/services/mappers.py`:**
```
# app/services/mappers.py
from app.db.models import AnalysisResult
from app.schemas.photos import AnalysisResultResponse
def analysis_to_response(analysis: AnalysisResult | None) -> AnalysisResultResponse | None: ...
```
- `photo_service.py`: удалить локальный `_analysis_response`, импортировать
  `analysis_to_response` из mappers, заменить вызовы.
- `batch_service.py`: импортировать `analysis_to_response` из mappers (не из
  photo_service). После этого `batch_service` **не импортирует** `photo_service` →
  цепочка `analysis_processor → batch_service → {mappers, batch_repository, errors,
  schemas, models}` не содержит `metrics_api`. (Ни один тест не импортирует
  `_analysis_response` напрямую — правки тестов не требуются.)

**Логика завершения в `analysis_processor.AnalysisProcessor`:**

1. Добавить коллаборатор `batch_repository: BatchRepository | None = None` в `__init__`
   (default `BatchRepository()`, как в `PhotoService`) → `self._batches`.
   Добавить локально `_TERMINAL_STATUSES = frozenset({PhotoStatus.done, PhotoStatus.failed})`
   (`from app.db.models import PhotoStatus`) и `from app.services.batch_service import select_best_photo`.

2. Приватный метод:
   ```
   async def _maybe_complete_batch(self, session, photo_uuid) -> None:
       batch_id = await self._photos.get_batch_id(session, photo_uuid)
       if batch_id is None:
           return
       batch = await self._batches.get_by_id(session, batch_id)   # eager photos+analysis
       if batch is None or batch.status != "processing":
           return
       photos = batch.photos
       if not (photos and all(p.status in _TERMINAL_STATUSES for p in photos)):
           return
       best = select_best_photo(photos)                            # ЧИСТАЯ функция, БЕЗ изменений
       rowcount = await self._batches.try_complete(session, batch_id, best)
       await session.commit()
       if rowcount:
           logger.info("batch completed", extra={"batch_id": str(batch_id),
                       "best_photo_id": str(best) if best else None})
   ```

3. Вызвать `await self._maybe_complete_batch(session, photo_uuid)`:
   - на skip-пути (`rowcount == 0`) перед `return` (восстановление после падения между
     терминальной записью и завершением батча — см. ниже);
   - на done-пути (после `worker_messages_processed_total...done` перед `return`);
   - на failed-пути (в конце метода, после логирования).

**Новые методы репозиториев:**
- `PhotoRepository.get_batch_id(session, photo_id) -> uuid.UUID | None`
  = `SELECT batch_id FROM photos WHERE photo_id=:id` (`scalar_one_or_none`). Для
  одиночных фото вернёт `None` → ноль лишних запросов в обычном (не-батч) пути.
- `BatchRepository.try_complete(session, batch_id, best_photo_id) -> int`
  = **атомарный** `UPDATE batches SET status='completed', best_photo_id=:b
  WHERE batch_id=:id AND status='processing'`, вернуть `result.rowcount`.
  **Заменяет** `mark_completed` (безусловный UPDATE) — `mark_completed` после F3 нигде
  не используется, удалить его (мёртвый код).

**`get_batch` → read-only (batch_service.py):** удалить блок
`if batch.status != "completed": mark_completed + commit`. Оставить вычисление
`status`/`best_photo_id` на лету для отображения:
```
if all_terminal:
    status = "completed"
    best_photo_id = select_best_photo(photos)   # только для ответа, БЕЗ записи
else:
    status = "processing"
    best_photo_id = None
```
Это **сохраняет текущее поведение отображения** (клиент видит completed+best, как
только все фото терминальны) и не пишет в БД. После удаления блока `logger`/`logging`
в `batch_service.py` становятся неиспользуемыми — удалить их импорт/объявление (ruff).

**worker/main.py:** передать `batch_repository=BatchRepository()` в `AnalysisProcessor(...)`
(импортировать `BatchRepository`) — явности ради (default тоже сработал бы).

### Консистентность и гонки (документировать)
- **Гонка двух воркеров** на последних фото батча: оба могут вычислить один и тот же
  `best` (формула детерминирована на одних и тех же терминальных данных) и вызвать
  `try_complete`. Атомарный `WHERE status='processing'` гарантирует rowcount=1 у первого
  и rowcount=0 у второго → второй no-op. Результат идентичен.
- **Нет «зависшего» батча:** завершение читается **после** собственного terminal-commit
  (read-after-commit). Последний по времени коммитящий терминал воркер при своём чтении
  увидит все фото терминальными (все коммиты уже видны, READ COMMITTED) → завершит батч.
  Ни один интерливинг не приводит к «оба пропустили».
- **Крэш между terminal-commit и `_maybe_complete_batch`:** terminal-commit прошёл, но
  offset ещё НЕ закоммичен (F1: `process()` не вернулась) → сообщение передоставится →
  claim вернёт 0 (фото уже терминально) → skip-путь → `_maybe_complete_batch` завершит
  батч. Поэтому вызов на skip-пути обязателен для самовосстановления.

---

## F4 — раскол `metrics.py` на api/worker

### Проблема
`app/integrations/metrics.py` импортируется обоими процессами → в реестре worker-а
регистрируются api-метрики (`photos_pending`, `http_*`, ...) и экспортируются нулями;
в Grafana `photos_pending` без фильтра даёт две серии (одна всегда 0).

### Решение
1. **Создать `app/integrations/metrics_api.py`** (метрики API):
   `photos_uploaded_total`, `http_requests_total`, `http_request_duration_seconds`,
   `storage_upload_errors_total`, `kafka_publish_errors_total`, `photos_pending`.
2. **Создать `app/integrations/metrics_worker.py`** (метрики worker):
   `photo_analysis_started_total`, `photo_analysis_completed_total`,
   `photo_analysis_failed_total`, `photo_analysis_duration_seconds`,
   `analyzer_grpc_errors_total`, `worker_messages_processed_total`.
   (Перенести определения дословно, включая комментарии про `reason`/cardinality.)
3. **Удалить `app/integrations/metrics.py`.**
4. **Обновить импорты (полный список):**
   | Файл | Было `app.integrations.metrics` → стало |
   |------|-----------------------------------------|
   | `app/main.py` (стр.27) | `metrics_api` (`photos_pending`) |
   | `app/api/metrics_middleware.py` (стр.23) | `metrics_api` (`http_*`) |
   | `app/services/photo_service.py` (стр.43) | `metrics_api` (`photos_uploaded_total`, `storage_upload_errors_total`) |
   | `app/services/outbox.py` (стр.23) | `metrics_api` (`kafka_publish_errors_total`) |
   | `app/services/analysis_processor.py` (стр.28) | `metrics_worker` (6 worker-метрик) |
   | `tests/test_metrics_middleware.py` (стр.20) | `metrics_api` |
   | `tests/test_metrics_endpoint.py` (стр.17) | `metrics_api` |
   | `tests/test_outbox.py` (стр.25) | `from app.integrations import metrics_api as metrics_module` |

### Инвариант импорт-гигиены (критично — иначе F4 не выполнен)
- **worker-процесс не должен транзитивно импортировать `metrics_api`.** Проверить, что
  граф импорта `app.worker.main` не содержит `metrics_api` (обеспечено развязкой F3:
  `batch_service` больше не тянет `photo_service`).
- **api-процесс не должен импортировать `metrics_worker`** (обеспечено: api нигде не
  импортирует `analysis_processor`).
Done-criterion: `/metrics` worker-а не содержит `photos_pending`/`http_*`; `/metrics`
API не содержит `photo_analysis_*`.

### Дашборд (`grafana/dashboards/photo-service.json`)
Корневой фикс — раскол (после него `photos_pending` экспортируется только job
`photo-api` → одна серия). Панель 4 (`"expr": "photos_pending"`) → изменить на
`photos_pending{job="photo-api"}` как явную защиту (низкий риск, фиксирует намерение).
Остальные панели используют `sum(...)`/`histogram_quantile(sum ... by(le))` — job
схлопывается агрегацией, дублей серий нет, править не нужно. Done-criterion: панель
«Pending photos» даёт ровно одну серию.

---

## F5 — компенсация MinIO при падении `session.commit()`

### Проблема
В `create_photo`/`create_batch` файл(ы) уже в MinIO, а `session.commit()` не обёрнут:
упавший commit откатывает транзакцию, но объекты в MinIO остаются сиротами.

### Решение

**Новый класс ошибки** в `app/core/errors.py`:
```
class DatabaseUnavailable(AppError):
    error_code = "SERVICE_UNAVAILABLE"   # публичный код НЕ меняется (как у StorageUnavailable)
    http_status = 503
    message = "Service temporarily unavailable"
```
Обоснование выбора: падение commit — это отказ **БД**, а не storage, поэтому переиспользовать
`StorageUnavailable` семантически неверно (введёт в заблуждение логи и `photo_id`-тег
в хендлере). Новый класс даёт корректную семантику/сообщение, но **тот же публичный
`error_code="SERVICE_UNAVAILABLE"` и статус 503** → формат ответа
`{error_code,message,request_id}` и набор публичных кодов не меняются. (Альтернатива —
просто переиспользовать `StorageUnavailable`; допустима, но менее точна. Выбран новый
класс.)

**`create_photo` (photo_service.py):** обернуть финальный commit:
```
try:
    await session.commit()
except Exception as exc:  # noqa: BLE001 - commit провалился -> компенсируем и отдаём 503
    await session.rollback()
    try:
        await anyio.to_thread.run_sync(self._storage.delete_file, object_key)
    except Exception:  # noqa: BLE001 - best-effort, не маскируем исходную ошибку
        logger.warning("failed to delete orphaned object after commit failure",
                       extra={"photo_id": str(photo_id), "object_key": object_key})
    logger.error("DB commit failed after MinIO save; compensated by deleting object",
                 extra={"photo_id": str(photo_id)})
    raise DatabaseUnavailable("Service temporarily unavailable", photo_id=str(photo_id)) from exc
photos_uploaded_total.inc()
```

**`create_batch` (photo_service.py):** тот же паттерн вокруг финального commit, но
удалить **все** `saved_object_keys`:
```
try:
    await session.commit()
except Exception as exc:  # noqa: BLE001
    await session.rollback()
    for object_key in saved_object_keys:
        try:
            await anyio.to_thread.run_sync(self._storage.delete_file, object_key)
        except Exception:  # noqa: BLE001
            logger.warning("failed to delete orphaned batch object after commit failure",
                           extra={"object_key": object_key})
    logger.error("DB commit failed after batch MinIO save; compensated",
                 extra={"batch_id": str(batch.batch_id), "photo_count": len(saved_object_keys)})
    raise DatabaseUnavailable("Service temporarily unavailable") from exc
photos_uploaded_total.inc(len(photos))
```
Примечания: `except Exception` не ловит `CancelledError` (она `BaseException`).
`delete_file` уже глотает `NoSuchKey`; внешний try — на случай сетевой ошибки MinIO,
чтобы компенсация не перекрыла исходную причину. Метрику не добавляем (вне скоупа);
наблюдаемость — через логи компенсации.

---

## F6 — restart-политики compose

`docker-compose.yml`: добавить `restart: unless-stopped` **всем 8 сервисам**:
`postgres`, `minio`, `kafka`, `analyzer-stub`, `api`, `worker`, `prometheus`, `grafana`.
Done-criterion: `docker compose config` показывает `restart: unless-stopped` у каждого.
Замечание: graceful shutdown воркера/uvicorn уже настроен (exec + SIGTERM, коммит 3c0ccf5),
F1-пауза прерывается shutdown-ом — авто-рестарт корректен.

---

## F7 — gRPC TLS: РЕШЕНИЕ — дефер в TASK-003 (вариант «б»)

**Выбор: отложить конфигурируемый TLS в TASK-003, зафиксировав решение письменно;
в TASK-002.1 — только уточняющий комментарий в коде, без функциональных изменений и
без новых env.**

Обоснование:
1. **Скоуп/цель.** Для MVP `analyzer-stub` и `worker` живут в одной docker-сети без
   недоверенных участников; insecure gRPC внутри mesh допустим (constitution §3.3 про
   недоверенный ВВОД и секреты, а не про транспорт внутри самодостаточного compose).
2. **Правильность модели.** Реальный анализатор придёт в TASK-003 заменой
   `ANALYZER_GRPC_ADDR`; именно там определится настоящая граница безопасности (внешняя
   сеть, выдача сертификатов, возможно mTLS, K8s-secrets). Строить TLS сейчас — угадывать
   модель сертификатов и плодить непокрытый тестами код (стаб — plaintext, реального
   TLS-эндпоинта для проверки нет).
3. **Тестируемость/объём.** Конфигурируемый TLS сейчас тестировался бы только «флаг
   выключен → insecure» + мок «secure_channel сконструирован» — низкая ценность при
   риске мёртвой конфигурации.

Действие кодера (минимальное, не функциональное): в
`app/integrations/analyzer_client.py::__init__` добавить комментарий, что
`grpc.aio.insecure_channel` выбран осознанно для одно-сетевого compose MVP, а
конфигурируемый TLS (env `ANALYZER_GRPC_TLS` + путь к root-cert) вводится в TASK-003
вместе с реальным анализатором. Env-переменную сейчас **не** заводим (не плодим мёртвую
конфигурацию). Зафиксировать этот выбор также в 80_docs (документатор).

---

## Что НЕ трогать

- `select_best_photo` — формула и сигнатура **без изменений** (только переиспользуется
  из `analysis_processor`).
- Публичные HTTP-контракты: пути, коды, тела. `GET /v1/batches/{id}` меняет только
  «кто пишет completed», форма ответа `BatchResponse` — прежняя.
- Формат ошибок `{error_code, message, request_id}`; набор публичных `error_code`
  (новый класс `DatabaseUnavailable` использует существующий код `SERVICE_UNAVAILABLE`).
- Схема БД, модели, миграции v001/v002 — без изменений.
- Kafka: топик `photo.analysis.requested`, формат сообщения, ключ, consumer group —
  без изменений. Атомарный claim `WHERE status='pending'` — без изменений.
- `_is_missing` (F1) — логика без изменений (UUID-проверка добавляется отдельным
  блоком в `_handle_message`, не внутри `_is_missing`).
- Значения лимитов (50 МБ, 2–10, 500 МБ) и их местоположение в `photo_service.py`.

---

## Шаги для кодера (исполнимый порядок)

Порядок выбран так, чтобы развязка (F4/F3) шла до потребителей.

1. **F4a — `app/integrations/metrics_api.py` (новый).** Перенести 6 api-метрик
   (`photos_uploaded_total`, `http_requests_total`, `http_request_duration_seconds`,
   `storage_upload_errors_total`, `kafka_publish_errors_total`, `photos_pending`) с их
   docstring/комментариями. Готово: модуль импортируется, метрики объявлены один раз.

2. **F4b — `app/integrations/metrics_worker.py` (новый).** Перенести 6 worker-метрик
   (см. список F4). Готово: модуль импортируется.

3. **F4c — удалить `app/integrations/metrics.py`.** Готово: файла нет, `grep`
   `app.integrations.metrics\b` по `app/` пуст.

4. **F4d — обновить импорты в приложении:** `main.py`, `api/metrics_middleware.py`,
   `services/photo_service.py`, `services/outbox.py` → `metrics_api`;
   `services/analysis_processor.py` → `metrics_worker` (по таблице F4). Готово: приложение
   и worker импортируются без ошибок.

5. **F3-развязка — `app/services/mappers.py` (новый) + перенос `_analysis_response`.**
   Создать `analysis_to_response(...)`; в `photo_service.py` удалить локальную функцию,
   импортировать из mappers, заменить 2 вызова (`get_photo`, `list_photos`); в
   `batch_service.py` импортировать из mappers вместо photo_service, заменить вызов.
   Готово: `batch_service` не импортирует `photo_service`; `grep` подтверждает.

6. **F3a — репозитории.** В `photo_repository.py` добавить `get_batch_id(...)`. В
   `batch_repository.py` заменить `mark_completed` на `try_complete(...) -> int`
   (атомарный `WHERE status='processing'`, вернуть rowcount), обновить docstring.
   Готово: `mark_completed` больше не определён и нигде не вызывается.

7. **F3b — `analysis_processor.py`.** Добавить импорт `PhotoStatus`, локальный
   `_TERMINAL_STATUSES`, `from app.services.batch_service import select_best_photo`,
   параметр `batch_repository` в `__init__` (default `BatchRepository()`), метод
   `_maybe_complete_batch`, и его вызовы на skip/done/failed-путях. Готово: unit-логика
   завершения на месте.

8. **F3c — `batch_service.py::get_batch` → read-only.** Удалить блок
   `mark_completed`+`commit`; оставить вычисление status/best на лету; убрать
   неиспользуемые `logging`/`logger`. Готово: `get_batch` не вызывает никаких write/commit.

9. **F3d — `worker/main.py`.** Импортировать `BatchRepository`, передать
   `batch_repository=BatchRepository()` в `AnalysisProcessor(...)`. Готово: worker
   стартует, батчи завершаются без GET.

10. **F1 — `app/worker/consumer.py`.** Добавить `enum`-импорт, `MessageOutcome`,
    `_ERROR_BACKOFF_SECONDS`, `from aiokafka.structs import ConsumerRecord, TopicPartition`.
    `_handle_message` → возвращает `MessageOutcome` (poison-pill/UUID-непарс → COMMIT;
    success → COMMIT; неожиданное исключение → RETRY). `consume_loop` → ветвление
    COMMIT→commit / RETRY→seek+лог+интеррап-пауза+continue. Обновить module-docstring
    (новая семантика + компромисс «залипание партиции»). Готово: критерий F1 выполним.

11. **F2 — `app/api/uploads.py` (новый) + эндпоинты.** Создать `read_capped_file`,
    `read_batch_files` (импорт констант из `photo_service`, ошибок из `core.errors`),
    module-docstring с границей F8. `api/photos.py::upload_photo` → `read_capped_file`;
    `api/batches.py::upload_batch` → `read_batch_files`. Готово: 11 файлов/оверсайз
    отсекаются до/на капе.

12. **F5 — `app/core/errors.py` + `photo_service.py`.** Добавить `DatabaseUnavailable`.
    Обернуть финальные `commit` в `create_photo`/`create_batch` в try/except с rollback,
    best-effort delete (в батче — всех `saved_object_keys`), логами и `raise
    DatabaseUnavailable`. Готово: падение commit → delete_file + 503.

13. **F6 — `docker-compose.yml`.** `restart: unless-stopped` всем 8 сервисам. Готово:
    `docker compose config` подтверждает.

14. **F7 — `app/integrations/analyzer_client.py`.** Комментарий об осознанном insecure
    + дефере TLS в TASK-003. Без функциональных изменений. Готово.

15. **F8 — документация границы.** Docstring в `app/api/uploads.py` (сделано в шаге 11);
    отметить для 80_docs строку в README. Готово.

16. **Обновить существующие тесты под новую семантику (в объёме F1/F3):** см. раздел ниже.
    Готово: `pytest` зелёный, `ruff check app/` чистый.

---

## Ожидаемые поломки/правки существующих тестов

Менять **только** в объёме новой семантики (F1/F3/F4). Правит кодер (шаг 16),
дополнительное покрытие добавляет test-writer.

**F3 — `tests/test_batch_service.py::TestGetBatch`:**
- `test_completed_status_with_best_photo_id_when_all_terminal` (стр.192-207): убрать
  ассерты `mark_completed.assert_awaited_once_with(...)` и `session.commit.assert_awaited_once()`;
  заменить на «read-only»: `repository.mark_completed`/`try_complete` **не вызывается**,
  `session.commit` **не вызывается**; ответ по-прежнему `completed` + верный `best_photo_id`.
- `test_completed_with_all_failed_yields_null_best_photo_id` (стр.209-220): убрать
  `mark_completed.assert_awaited_once()`; ассертить, что запись не производится,
  `best_photo_id is None`, статус `completed`.
- Остальные тесты класса (processing, already-completed idempotency, empty, analysis) —
  остаются зелёными (write и так не ожидался).

**F3 — `tests/test_batch_repository.py::TestMarkCompleted`:** переписать под
`try_complete`: проверить, что SQL содержит `WHERE ... status='processing'`,
`SET status='completed'`, корректные `batch_id`/`best_photo_id` (в т.ч. `NULL`), метод
не коммитит сам и возвращает rowcount. (`mark_completed` удалён.)

**F3 — `tests/test_analysis_processor.py`:** во всех существующих тестах (одиночные фото,
без батча) добавить `photos.get_batch_id.return_value = None`, чтобы `_maybe_complete_batch`
детерминированно завершался сразу (иначе полагались бы на мок-квирки). Ассерты числа
`session.commit` остаются прежними (2 на success, 3 на failure, 1 на skip), т.к.
`get_batch_id → None` не добавляет commit.

**F4 — импорты в тестах:** `tests/test_metrics_middleware.py`,
`tests/test_metrics_endpoint.py`, `tests/test_outbox.py` → импортировать из `metrics_api`
(см. таблицу F4). Поведение тестов не меняется.

**F1 — `tests/test_worker_consumer.py`:** существующие тесты остаются зелёными —
success/poison-pill по-прежнему коммитят; неожиданное-исключение на уровне
`_handle_message` по-прежнему не пробрасывается. Новое поведение (RETRY → seek + не-commit)
покрывается **новыми** тестами (test-writer). Если test-writer/кодер добавляет тест на
RETRY-путь `consume_loop`, задать `consumer.seek = MagicMock()` (в aiokafka `seek`
синхронный; на `AsyncMock` он вернул бы неожиданную корутину).

**F2/F5 — существующие эндпоинт/сервис-тесты остаются зелёными:** капнутое чтение малых
файлов возвращает те же байты (`test_service_receives_*_raw_bytes`, empty-file тесты);
сервисные лимит-тесты (`test_photo_service_batch.py`) зовут `create_batch` напрямую,
константы на месте. Новые тесты F2/F5 добавляет test-writer.

---

## Риски и альтернативы

- **R1 — F1: непарсируемый `photo_id` как poison-pill.** Без UUID-проверки в
  `_handle_message` сообщение с `photo_id="abc"` бросало бы `ValueError` из `process()`
  → RETRY → вечная блокировка партиции на заведомо-неисполнимом сообщении.
  **Решение:** трактовать непарсируемый UUID как poison-pill (COMMIT/skip). Это выходит
  за буквальный список спеки (JSON/пустые поля), но соответствует её ЦЕЛИ (отличать
  «недоставляемо навсегда» от «временной ошибки»). Если ревьюер настаивает на букве —
  убрать этот подпункт; тогда malformed-UUID попадает под задокументированный компромисс
  «детерминированный баг блокирует партицию».
- **R2 — F1: залипание партиции.** При детерминированной ошибке на конкретном сообщении
  партиция перечитывает его бесконечно (DLQ вне скоупа). Осознанно; зафиксировано в
  docstring. Смягчение (reaper/DLQ) — TASK-003.
- **R3 — F2: RAM всё ещё до 500 МБ на батч.** Капнутое чтение не делает стриминга: 10×50 МБ
  могут одновременно быть в памяти. Это в рамках заявленного лимита; стриминг в MinIO —
  вне скоупа (F8, документируется). Заявленную проблему (лимиты ДО материализации
  произвольно больших тел) фикс решает.
- **R4 — F3: лишние запросы на терминал.** Для батч-фото добавляются `get_batch_id` +
  `get_by_id` (selectinload ≤10 строк) на каждую терминальную запись. Приемлемо для MVP;
  одиночные фото не затронуты (`get_batch_id → None`).
- **R5 — F4: импорт-гигиена.** Главный риск — снова затянуть `metrics_api` в worker через
  транзитивный импорт. Снят развязкой F3 (mappers). Обязательна проверка графа импорта
  `app.worker.main` (шаг F4 done-criterion) и тест «worker-реестр без photos_pending».
- **R6 — F5: класс ошибки.** Введён `DatabaseUnavailable` с публичным кодом
  `SERVICE_UNAVAILABLE` (контракт не меняется). Альтернатива — переиспользовать
  `StorageUnavailable` (проще, но семантически неточно). Выбран новый класс.
- **R7 — F5: delete_file может упасть при недоступности MinIO.** Компенсация best-effort;
  падение логируется, объект может остаться сиротой (ручная чистка — вне скоупа).

## Приложение — сводка изменяемых/новых контрактов (внутренние, не публичные)

- `app/integrations/metrics_api.py` (new), `app/integrations/metrics_worker.py` (new),
  `app/integrations/metrics.py` (deleted).
- `app/services/mappers.py` (new): `analysis_to_response(AnalysisResult|None) -> AnalysisResultResponse|None`.
- `app/api/uploads.py` (new): `read_capped_file(UploadFile) -> bytes`,
  `read_batch_files(list[UploadFile]) -> list[tuple[str|None, bytes]]`.
- `app/core/errors.py`: `+ class DatabaseUnavailable(AppError)` (`SERVICE_UNAVAILABLE`, 503).
- `PhotoRepository.get_batch_id(session, photo_id) -> uuid.UUID | None` (new).
- `BatchRepository.try_complete(session, batch_id, best_photo_id) -> int` (replaces `mark_completed`).
- `AnalysisProcessor.__init__(..., batch_repository: BatchRepository | None = None)`;
  `+ _maybe_complete_batch(session, photo_uuid)`.
- `consumer.MessageOutcome` (enum), `_handle_message(...) -> MessageOutcome`,
  `consume_loop` ветвление commit/seek.
- HTTP/gRPC/Kafka wire-контракты — **без изменений**.

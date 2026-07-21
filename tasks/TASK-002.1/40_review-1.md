---
task_id: TASK-002.1
agent: reviewer-1
model: opus
status: APPROVE
inputs:
  - specs/constitution.md
  - specs/feature-upload/tasks.md   # раздел TASK-002.1 (F1-F8)
  - tasks/TASK-002.1/20_design.md
  - tasks/TASK-002.1/30_impl.md
  - tasks/TASK-002.1/00_orchestration.md
  - git diff (рабочее дерево) + новые файлы
outputs:
  - tasks/TASK-002.1/40_review-1.md
blocking_findings: 0
blocking_findings_initial: 1
review_rounds: 2
timestamp: 2026-07-21T00:00:00Z
---

# TASK-002.1 — Ревью-1 (корректность и архитектура)

**status: APPROVE** (раунд 2; в раунде 1 были запрошены правки)
**BLOCKING: 0 (был 1, закрыт) · открытыми остаются: MAJOR 2 · MINOR 3 · NOTE 4**

## Гейты (прогнано мной, не со слов кодера)

| Гейт | Результат |
|---|---|
| `uv run ruff check .` | `All checks passed!` |
| `uv run pytest -q -m "not integration"` | `370 passed, 1 deselected` |
| coverage | `TOTAL 94%` |
| `docker-compose.yml` | 8 сервисов, `restart: unless-stopped` × 8 |
| Импорт-гигиена F4 | `import app.worker.main` → реестр содержит только 6 worker-метрик; `photos_pending`/`http_requests_total`/`photos_uploaded_total`/`kafka_publish_errors_total`/`storage_upload_errors_total` — **отсутствуют** ✅ |

Всё, что заявлено кодером в 30_impl «Как проверить», воспроизведено. Ложных
утверждений в 30_impl не обнаружено.

---

## Проверка фиксов ПО СУЩЕСТВУ (главное требование ревью)

Проверял не «есть ли код», а «решает ли он заявленную проблему». Где возможно —
исполнением (скрипты во временной папке, не в репозитории).

| Fix | Решает заявленную проблему? | Обоснование (доказательство) |
|---|---|---|
| **F1** at-least-once | **Да** (с оговоркой B1) | Прогон: `_handle_message` при `RuntimeError` из `process()` → `MessageOutcome.RETRY`; `consume_loop` → `consumer.commit.await_count == 0`; `consumer.seek` вызван с `call(TopicPartition(topic='photo.analysis.requested', partition=3), 4242)` — **правильный TP из `message.topic`/`message.partition` и правильный offset самого сообщения**. Подтверждено, что `enable_auto_commit=False` (`worker/main.py:72`) — авто-коммит не обходит решение. Подтверждено по исходникам aiokafka 0.14.0, что `seek()` синхронный и что `Fetcher.seek_to` сбрасывает буфер партиции (`del self._records[tp]`) — то есть следующий `getone()` реально перечитает то же сообщение. Многопартиционный случай безопасен: `commit()` без аргументов коммитит `position()` каждой партиции, а после `seek` `position == message.offset`, так что успех на соседней партиции не «перепрыгивает» упавшее сообщение. Пауза прерываема `stop_event` — graceful shutdown задерживается ≤1 с. **Оговорка:** новая ветка вводит собственный краш-путь (B1). |
| **F1** poison-pill | **Да** | Прогон: битый JSON → COMMIT, отсутствие полей → COMMIT, `photo_id="abc"` → COMMIT; `processor.process` не вызывается ни разу. Ветка `not isinstance(payload, dict)` тоже покрыта. Риск залипания партиции зафиксирован в module-docstring (`consumer.py:35-39`), как требовала спека. |
| **F2** лимиты до материализации | **Да, частично** (см. M2) | Прогон с fake-`UploadFile`, регистрирующим каждый `read(size)`: 11 файлов → `BatchSizeError` при **`len(READS) == 0`** — ни одного байта не прочитано ✅. Оверсайз-файл → ровно один `read(52428801)` = `MAX+1`, остальные файлы **не читаются** ✅. Бегущая сумма (с патчем константы до 25 Б): прочитаны `['a','b','c']`, `d` не читался ✅. Одиночный upload закапан ✅. Безлимитных `await f.read()` в `app/` не осталось (grep). Слои не нарушены: `app/api/uploads.py` → `app/services/photo_service.py` (направление api→services), сервис по-прежнему FastAPI-агностичен. **Оговорка:** сама константа `BATCH_MAX_TOTAL_BYTES` недостижима (M2). |
| **F3** GET read-only | **Да** | В `batch_service.py` не осталось ни `try_complete`, ни `commit`, ни любого `session.execute` — только `get_by_id` + вычисление. Публичный ответ не изменился: `all_terminal → "completed" + select_best_photo(...)`, иначе `"processing"`/`None` — ровно та же формула, что была до фикса (сверено по диффу строк 86-92). `select_best_photo` не тронут (диффа нет). Пустой батч по-прежнему `processing`. |
| **F3** завершение в worker | **Да** | `_maybe_complete_batch` вызывается на **всех трёх** терминальных путях: skip/`rowcount==0` (`analysis_processor.py:127`), done (`:193`), failed (`:210`). Вызов на skip-пути корректен и обязателен — он и есть самовосстановление после краха между terminal-commit и завершением батча. Атомарность: `UPDATE ... WHERE batch_id=:id AND status='processing'` + возврат `rowcount` (`batch_repository.py:54-59`). Гонка двух воркеров: доказательство архитектора верно и я его перепроверил — пусть T1<T3 моменты terminal-commit, T2/T4 моменты чтения; «оба пропустили» требует T4<T1 при T4>T3>T2>T1 — противоречие. Свежесть чтения обеспечена READ COMMITTED (изоляция нигде не переопределена) и тем, что все worker-записи — Core-`update()`/`pg_insert`, поэтому identity map сессии пуст и `session.get(Batch, ...)` реально ходит в БД (см. N2 про хрупкость). |
| **F4** раскол метрик | **Да** | 12 метрик → 6+6, определения перенесены дословно (сверено с `git show HEAD:...metrics.py`), дублей/пропаж нет. `metrics.py` удалён. Циклов импорта нет. Развязка `batch_service → mappers` реально разрывает цепочку: `batch_service` больше не импортирует `photo_service` (grep пуст), поэтому `analysis_processor → batch_service` не тянет `metrics_api`. Инструментально подтверждено (см. таблицу гейтов). Дашборд: `photos_pending{job="photo-api"}`, и `job_name: photo-api` действительно есть в `prometheus.yml` — фильтр рабочий, не опечатка. |
| **F5** компенсация commit | **Да** (с оговорками M3/m4) | try/except вокруг `commit()` в обоих путях; в батче удаляются **все** `saved_object_keys`; внутренний try/except вокруг `delete_file` не маскирует исходную ошибку; `raise ... from exc` сохраняет причину. `DatabaseUnavailable` реиспользует публичный `error_code="SERVICE_UNAVAILABLE"`/503 → формат `{error_code,message,request_id}` и набор кодов не меняются, хендлер `AppError` обрабатывает его штатно. Проверил отдельно опасение «`batch.batch_id` после `rollback()` бросит `DetachedInstanceError`» — **не бросает** (атрибут задан в Python, объект возвращается в transient), прогон на живой SQLAlchemy-сессии. |
| **F6** restart | **Да** | 8 сервисов в compose, 8 × `restart: unless-stopped`. `pgdata/miniodata/kafkadata` — тома, не сервисы. |
| **F7** gRPC TLS | **Да** (дефер) | Комментарий в `analyzer_client.py:48-57`, функциональных изменений и мёртвых env нет — ровно как решил architect и одобрил оркестратор. |
| **F8** граница | **Да** | Docstring `app/api/uploads.py:31-45` честно фиксирует: капнутое чтение ≠ стриминг; Starlette уже спулит тело; ранний отказ по `Content-Length` требует ASGI-middleware до роутинга. Это соответствует букве спеки («приветствуется», не «требуется»). |

**Итог по существу:** формальных фиксов, не решающих заявленную проблему, я не нашёл.
Процессный урок TASK-002 не повторён. Блокер ниже — не «фикс не работает», а «фикс
вводит новый краш-путь в обработчике, который обязан не падать».

---

## BLOCKING

### B1 — `consumer.seek()` в RETRY-ветке не защищён: ребаланс убивает worker-процесс
**Файл:** `photo-service/app/worker/consumer.py:119-120`

```python
tp = TopicPartition(message.topic, message.partition)
consumer.seek(tp, message.offset)  # aiokafka: synchronous, no await
```

`AIOKafkaConsumer.seek()` (aiokafka 0.14.0) → `Fetcher.seek_to` → `SubscriptionState.seek`
→ `_assigned_state(tp)`, который бросает `IllegalStateError("No current assignment for
partition ...")`, если партиция уже не назначена, и `AssertionError`, если
`self._subscription.assignment is None`. В `consume_loop` вокруг `seek` нет `try/except`;
`worker/main.py::_run` имеет только `try/finally` без `except` → исключение доходит до
`asyncio.run` и процесс падает.

**Окно реально:** между `getone()` и `seek()` выполняется весь `process()` — до
`WORKER_MAX_ATTEMPTS × ANALYZER_GRPC_TIMEOUT` плюс backoff 1+2+4 с. Ребаланс в этом окне —
рядовое событие (масштабирование реплик worker в K8s, что constitution §4 прямо
предполагает; failover координатора; таймаут сессии соседнего консюмера). А RETRY-ветка
входится ровно тогда, когда «всё плохо» — то есть корреляция с ребалансом положительная.

**Доказано исполнением:** мок-консюмер с `seek`, бросающим `IllegalStateError`, →
`consume_loop` пропагирует исключение наружу (`RESULT: consume_loop PROPAGATED
IllegalStateError`).

**Почему BLOCKING:** F1 по спеке обязан «залогировать, пауза, Kafka передоставит»
и продолжить цикл; module-docstring сам декларирует «never left to crash the loop»
(`consumer.py:185`). Сейчас на этом пути loop умирает. Потери данных нет (offset не
закоммичен), но это регресс доступности в новом коде, и он лечится тремя строками.
Семантически при ребалансе `seek` не только опасен, но и не нужен: партиция уходит
другому консюмеру, который сам стартует с последнего закоммиченного offset — то есть
пропуск `seek` в этом случае и есть правильное поведение.

**Фикс (исполним как есть):**
```python
from aiokafka.errors import IllegalStateError
...
        else:
            tp = TopicPartition(message.topic, message.partition)
            try:
                consumer.seek(tp, message.offset)  # aiokafka: synchronous, no await
            except (IllegalStateError, AssertionError):
                # Партиция отозвана ребалансом, пока шёл process(). Перематывать
                # нечего: новый владелец партиции начнёт с последнего
                # закоммиченного offset, то есть с этого же сообщения.
                logger.warning(
                    "partition revoked before rewind; redelivery handled by rebalance",
                    extra={"topic": message.topic, "partition": message.partition,
                           "offset": message.offset},
                )
            else:
                logger.warning(
                    "offset not committed after unexpected error; message will be redelivered",
                    extra={...},  # как сейчас
                )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=_ERROR_BACKOFF_SECONDS)
            except TimeoutError:
                pass
```
Дополнительно (в том же фиксе, симметрично): `await consumer.commit()` на строке 113
может бросить `CommitFailedError` при ребалансе и так же убить цикл. Обернуть:
```python
        if outcome is MessageOutcome.COMMIT:
            try:
                await consumer.commit()
            except CommitFailedError:
                logger.warning("commit failed (rebalance); message will be redelivered",
                               extra={"topic": message.topic, "partition": message.partition,
                                      "offset": message.offset})
```
(это pre-existing, но чинится в одну строку рядом и относится к тому же инварианту
«цикл не умирает»).

**Тест, который должен появиться:** `seek` бросает `IllegalStateError` → `consume_loop`
не падает, `commit` не вызван, цикл делает следующую итерацию.

---

## NON-BLOCKING

### M1 (MAJOR) — poison-pill и RETRY невидимы в метриках; залипшую партицию нечем алертить
**Файлы:** `photo-service/app/worker/consumer.py:149, 160-164, 175-179, 121-128`

F1 осознанно принял компромисс «детерминированная ошибка блокирует партицию»
(20_design R2) и одновременно **расширил** класс молча дропаемых сообщений
(добавился не-UUID `photo_id`). При этом:
- дроп poison-pill инкрементирует **ноль** метрик — только `logger.warning`;
- RETRY (не-коммит) инкрементирует **ноль** метрик.

Значит ни «мы теряем сообщения», ни «партиция залипла и лаг растёт по одной
партиции» не алертятся. Это прямо противоречит constitution §3.1 (наблюдаемость,
Kafka lag в USE) и делает принятый компромисс неуправляемым в проде.

**Фикс:** переиспользовать существующий `worker_messages_processed_total{result}`
(лейбл уже есть, кардинальность не растёт), добавив два значения:
```python
# metrics_worker уже импортируется только worker-ом; consumer.py импортирует
# worker_messages_processed_total из app.integrations.metrics_worker
worker_messages_processed_total.labels(result="poison").inc()   # во всех трёх poison-ветках
worker_messages_processed_total.labels(result="retry").inc()    # в RETRY-ветке
```
и панель/алерт `increase(worker_messages_processed_total{result="retry"}[5m]) > 0`.
Импорт `metrics_worker` в `consumer.py` инвариант F4 не нарушает (это worker-модуль).

### M2 (MAJOR) — «суммарный лимит батча» недостижим при боевых константах: ветка мертва
**Файлы:** `photo-service/app/api/uploads.py:92-95`, `photo-service/app/services/photo_service.py:68, 216-220`

`BATCH_MAX_TOTAL_BYTES = MAX_BATCH_SIZE * MAX_FILE_SIZE_BYTES = 10 × 50 МБ = 500 МБ`.
Каждый файл уже закапан 50 МБ, файлов максимум 10 → максимально возможная сумма ровно
500 МБ, а условие — строгое `total > 500 МБ`. **Прогон подтверждает: ветка
недостижима** (`F2.deadcode: MAX possible total = 524288000, BATCH_MAX_TOTAL_BYTES =
524288000 → REACHABLE? False`). То же в `create_batch:216`.

Последствия:
1. Критерий приёмки «суммарный лимит срабатывает по бегущей сумме» проверяем **только**
   monkeypatch-ем константы — на боевых значениях он не срабатывает никогда.
2. Исходная претензия внешнего ревью TASK-002 («10 × 50 МБ = 500 МБ в RAM за запрос»)
   для **легитимного** батча по-прежнему в силе: 500 МБ Python-`bytes` одновременно.
   20_design R3 это признаёт, поэтому это не блокер, но принимать это молча нельзя.

**Фикс (выбрать один, оба исполнимы):**
- (предпочтительно) снизить константу до осмысленного значения, например
  `BATCH_MAX_TOTAL_BYTES = 200 * 1024 * 1024  # 200 МБ, ниже суммы per-file лимитов`
  — тогда лимит становится реальным ограничением памяти на запрос и ветка живой;
- либо оставить как есть, но заменить комментарий на строке 64-68 `photo_service.py`
  честной формулировкой: «guard-инвариант; при текущих значениях недостижим и
  срабатывает только если per-file/count лимиты будут ослаблены», и продублировать
  это в docstring `read_batch_files`, чтобы следующий ревьюер не считал его рабочим.

Дополнительно: пик RAM в момент проверки = (сумма уже прочитанных) + (текущий файл),
т.е. до `BATCH_MAX_TOTAL_BYTES + 50 МБ`. Стоит зафиксировать в docstring.

### M3 (MAJOR) — F5: конкурентных загрузок × 500 МБ ничем не ограничено
**Файл:** `photo-service/app/api/batches.py:36`, `photo-service/app/api/uploads.py:73-97`

Constitution §3.4 требует «per-user: макс 100 одновременных uploads» и 1000 RPS на
инстанс. Ни F2, ни что-либо ещё не ограничивает **число одновременных** батч-запросов,
каждый из которых легально держит до 500 МБ. 10 параллельных батчей = 5 ГБ → OOM пода.
Это не регресс TASK-002.1, но F2 объявлен фиксом проблемы «нехватка памяти», и без
ограничения конкурентности проблема закрыта лишь наполовину.

**Фикс:** завести модульный `asyncio.Semaphore` вокруг чтения батча в
`app/api/uploads.py` (значение из `Settings`, дефолт, например, 4) либо зафиксировать
в 80_docs/README требование к `--limit-concurrency` uvicorn и лимитам памяти пода.
Достаточно письменного решения + строки в README, если реализацию решено отложить.

### M4 (MAJOR) — не покрыто тестами НИ ОДНО новое поведение F1/F2/F3/F5
**Coverage (мой прогон):**
```
app\api\uploads.py                20   2   90%   69, 93      # оба raise
app\services\analysis_processor.py 92  11   88%   88-100      # весь _maybe_complete_batch
app\services\photo_service.py     134  17   87%   168-181, 263-277  # обе компенсации F5
app\worker\consumer.py             58  10   83%   119-132, 174-179  # RETRY + UUID-poison
```
То есть исполняются только «старые» пути; все пять тестовых критериев приёмки спеки
(TASK-002.1 «Критерии приёмки») сейчас не выполнены. По конвейеру это зона test-writer,
но гейт «оба APPROVE → тесты» не должен пропустить это без явного списка. **Передать
test-writer как обязательный минимум:**
1. `consume_loop`: `process()` бросает → `commit` НЕ вызван, `seek(TopicPartition(topic, partition), offset)` вызван ровно с этими аргументами (`consumer.seek = MagicMock()`, не `AsyncMock` — иначе создастся неожидаемая корутина), пауза прерывается `stop_event`.
2. `consume_loop`: `seek` бросает `IllegalStateError` → цикл не падает (тест на B1).
3. `_handle_message`: `photo_id="not-a-uuid"` → `MessageOutcome.COMMIT`, `process` не вызван.
4. `read_batch_files`: 11 файлов → ни одного `.read()` (счётчик вызовов на фейке); файл >cap → ровно один `read(MAX+1)` и следующие файлы не читаны; бегущая сумма — с патчем **`app.api.uploads.BATCH_MAX_TOTAL_BYTES`** (см. m1!).
5. `_maybe_complete_batch`: (a) `get_batch_id → None` → ни одного запроса к batch-репозиторию; (b) все фото терминальны → `try_complete` + `commit`; (c) один фото `processing` → `try_complete` не вызван; (d) `rowcount == 0` (проигранная гонка) → не падает, лог не пишется; (e) вызывается на всех трёх путях (skip/done/failed).
6. `create_photo`/`create_batch`: `session.commit` бросает → `delete_file` вызван (в батче — на **всех** ключах) → наружу `DatabaseUnavailable`, ответ 503 c телом `{error_code:"SERVICE_UNAVAILABLE", ...}`.
7. Реестр метрик: `app.worker.main` без `photos_pending`/`http_*`; `app.main` без `photo_analysis_*` (сделать регрессионным тестом, а не ручной проверкой из 30_impl).

### m1 (MINOR) — константы в `uploads.py` — копии значений, а не «единственный источник»
**Файл:** `photo-service/app/api/uploads.py:51-56`

`from app.services.photo_service import BATCH_MAX_TOTAL_BYTES, ...` связывает **значение**
в момент импорта. Утверждение 20_design «Api-хелпер импортирует их оттуда. Это
единственный источник значений» верно для чтения исходника, но неверно для рантайма:
`monkeypatch.setattr(photo_service_module, "BATCH_MAX_TOTAL_BYTES", X)` (как делают
`tests/test_photo_service_batch.py`) **не влияет** на api-слой. Тест, который патчит
сервисную константу и стучится в эндпоинт, молча проверит не то, что думает.

**Фикс:** одна строка в docstring `app/api/uploads.py`: «константы связаны по значению
на импорте; тесты, меняющие лимит для api-пути, должны патчить
`app.api.uploads.<CONST>`, а не `app.services.photo_service.<CONST>`». Плюс явно
сказать это test-writer.

### m2 (MINOR) — F5: `session.rollback()` вне защищённого блока
**Файлы:** `photo-service/app/services/photo_service.py:169` и `:264`

`await session.rollback()` стоит первым в `except` и сам не обёрнут. Если он бросит,
компенсирующий `delete_file` не выполнится, а клиент получит 500 (`INTERNAL_ERROR`)
вместо 503 — то есть ровно те два симптома, которые F5 и лечит. На практике при
disconnect-коммите SQLAlchemy уже инвалидировала соединение и `rollback()` становится
no-op, поэтому это MINOR, а не блокер.

**Фикс:**
```python
        except Exception as exc:  # noqa: BLE001
            try:
                await session.rollback()
            except Exception:  # noqa: BLE001 - соединение уже мертво; компенсация важнее
                logger.warning("rollback after failed commit also failed",
                               extra={"photo_id": str(photo_id)})
            try:
                await anyio.to_thread.run_sync(self._storage.delete_file, object_key)
            ...
```

### m3 (MINOR) — тот же класс бага рядом, в StorageUnavailable-пути батча, остался незакрытым
**Файл:** `photo-service/app/services/photo_service.py:257-258`

```python
            for object_key in saved_object_keys:
                await anyio.to_thread.run_sync(self._storage.delete_file, object_key)
            raise StorageUnavailable("Storage is unreachable") from exc
```
Компенсация без `try/except` в ветке, куда попадают **именно потому, что MinIO
недоступен** → `delete_file` с высокой вероятностью бросит и подменит `StorageUnavailable`
(503) на голое исключение → 500. F5 закрыл этот паттерн в commit-ветках, но пропустил
соседнюю. Pre-existing, но чинится копипастой уже написанного внутреннего `try/except`
и логически принадлежит F5.

### m4 (MINOR) — двойной учёт `worker_messages_processed_total` при падении `_maybe_complete_batch`
**Файлы:** `photo-service/app/services/analysis_processor.py:188, 193` (и `199, 210`)

`worker_messages_processed_total{result="done"}` инкрементируется **до**
`_maybe_complete_batch`. Если тот бросит (БД моргнула), сообщение уйдёт в RETRY,
передоставится, и на skip-пути инкрементируется ещё и `{result="skipped"}`. Сумма по
`result` перестаёт быть числом сообщений. Не критично, но искажает дашборд именно в
инцидентах.

**Фикс:** либо перенести инкременты `worker_messages_processed_total` в
`consumer._handle_message` (единственное место, знающее исход сообщения), либо принять
и добавить строку в docstring метрики: «счётчик доставок, не уникальных фото».

### m5 (MINOR) — устаревший docstring `worker/main.py` вводит в заблуждение по F1
**Файл:** `photo-service/app/worker/main.py:11`

> `consumer.stop()` (commits the last offset)

При `enable_auto_commit=False` (строка 72) `stop()` **не** коммитит. После F1 это
особенно важно: читатель может решить, что при shutdown во время RETRY offset всё-таки
уедет вперёд. Заменить на: «`consumer.stop()` (auto-commit выключен — offset не
коммитится; незакоммиченное сообщение будет передоставлено после рестарта)».

### N1 (NOTE) — фиксированный backoff 1 с + `logger.exception` на каждой итерации
`_ERROR_BACKOFF_SECONDS = 1.0` без экспоненты: при часовой недоступности БД это
~3600 трейсбеков на воркер (constitution §3.1: ротация 1 ГБ/день). Разумно: экспонента
с потолком (1→2→4→…→30 с) со сбросом после успеха, либо rate-limit лога. Не блокер,
спека просила «например 1с».

### N2 (NOTE) — `_maybe_complete_batch` неявно зависит от того, что identity map пуст
**Файл:** `photo-service/app/services/analysis_processor.py:88` → `batch_repository.py:31`

`SessionLocal` создан с `expire_on_commit=False`, а `BatchRepository.get_by_id` использует
`session.get(...)`, который при попадании в identity map **возвращает объект без запроса и
без применения `options=[selectinload(...)]`** → был бы lazy-load из async-кода
(`MissingGreenlet`) и/или устаревшие данные. Сегодня безопасно: все worker-записи —
Core-`update()`/`pg_insert`, ORM-инстансы `Batch`/`Photo` в сессию не попадают, а
`_maybe_complete_batch` вызывается один раз на сессию. Но связь неявная и сломается от
безобидного рефакторинга репозитория. Достаточно комментария у вызова
(`# session identity map пуст: все worker-записи Core-style, поэтому get() реально ходит в БД`)
или перехода на `select(Batch).options(...).where(...)`.

### N3 (NOTE) — `batches.status`/`best_photo_id` теперь write-only
После F3 воркер пишет эти колонки, а `get_batch` их не читает (считает на лету).
Расхождение невозможно (формула детерминирована на терминальных данных), поведение
клиента не меняется — но стоит зафиксировать в 80_docs, что источник истины для ответа
API — вычисление, а колонка нужна для отчётности/будущих потребителей.

---

## Ответ на открытый вопрос оркестратора (правка 7 тестов в `test_worker_consumer.py`)

**Правомерно. Подмены данных, маскирующей проблему, здесь нет.**

Обоснование по коду, а не по намерению:
1. Единственный продьюсер топика — `app/services/outbox.py:68`, который публикует
   `str(row.photo_id)`, где `Photo.photo_id` — колонка типа UUID (PK). Значит **реальное
   сообщение физически не может содержать не-UUID `photo_id`**. Плейсхолдеры
   `"photo-1"/"p1"/"p2"` описывали контракт, которого никогда не существовало.
2. Правка затронула только литералы-фикстуры; ни один ассерт и ни одна ветка логики не
   ослаблены (сверено по диффу построчно). Тесты по-прежнему проверяют ровно то же:
   `process` вызван с теми же аргументами, `trace_id_var` выставлен, `commit` вызван,
   исключение не пробрасывается.
3. Риск «реальные сообщения теперь молча дропаются» = риск появления **стороннего**
   продьюсера с не-UUID id. Он не нулевой, и именно поэтому M1 (метрика на poison-pill)
   — не косметика: сейчас такой дроп виден только в `logger.warning`.

Единственная претензия к процессу: пункт отсутствовал в «Ожидаемых поломках» 20_design —
это пробел дизайна (R1 одобрен, а фикстуры под него не пересмотрены), а не выход кодера
за скоуп. Кодер честно вынес это в «Открытые вопросы». Замечаний нет.

Требуется добавить недостающий тест на саму новую ветку (`photo_id="not-a-uuid"` →
COMMIT, `process` не вызван) — сейчас строки `consumer.py:174-179` не покрыты (см. M4.3).

---

## Резюме

Реализация точно следует 20_design, все 16 шагов выполнены, ни один фикс не оказался
«формальным». F1 и F2 я проверил исполнением, а не чтением: `seek` получает правильный
`TopicPartition` и offset, `commit` в RETRY-ветке не вызывается; счётчик `.read()`
подтверждает, что count-check предшествует чтению, а per-file cap и бегущая сумма
реально прерывают обход. F3 читается корректно на всех терминальных путях, гонка двух
воркеров закрыта атомарным предикатом, публичный контракт GET не изменился. F4
подтверждён инструментально по реестру Prometheus.

Блокер один и узкий: RETRY-ветка F1 может убить процесс воркера при ребалансе, потому
что `consumer.seek()` не защищён — это ровно тот обработчик, который по спеке обязан
«залогировать и продолжить». Фикс — три строки + тест (B1, и заодно `commit()` на
строке 113).

Из не-блокеров два стоит закрыть в этой же итерации, а не «потом»: M1 (poison-pill и
RETRY не видны ни в одной метрике — принятый компромисс «партиция залипает» иначе
неуправляем) и M2 (лимит суммарного размера батча недостижим при боевых константах —
либо снизить значение, либо честно назвать его guard-инвариантом). M4 — обязательный
исполнимый список для test-writer.

После B1 (+ желательно M1, M2) — APPROVE.

---

# Re-review (раунд 2) — проверка закрытия находок

Проверял **исполнением**, а не по описанию правок. Гейты прогнаны заново мной:
`ruff check .` → `All checks passed!`; `pytest -q -m "not integration"` → `370 passed,
1 deselected`; coverage `TOTAL 94%`.

## B1 (BLOCKING) — ЗАКРЫТ

**Файл:** `app/worker/consumer.py:97, 144-161, 168-186`

Импорт `from aiokafka.errors import CommitFailedError, IllegalStateError` на месте;
`seek()` обёрнут в `except (IllegalStateError, AssertionError)`, `commit()` — в
`except CommitFailedError`; обе ветки логируют warning и цикл продолжается. Пауза
backoff вынесена за `try/except/else` и выполняется в обоих случаях.

Прогон (мок-консюмер, 4 сценария):

```
B1a seek/IllegalStateError:   loop SURVIVED | commit awaits = 0 | retries metric + 1.0
B1b seek/AssertionError:      loop SURVIVED | commit awaits = 0
B1c commit/CommitFailedError: loop SURVIVED | seek calls = 0
B1d happy path:               commit awaits = 1 | seek calls = 0
```

Ключевое: ни один из трёх сценариев отказа не пробрасывает исключение наружу
(в раунде 1 `consume_loop` пропагировал `IllegalStateError` и убивал процесс), при
этом **счастливый путь не деградировал** — `commit` по-прежнему вызывается ровно один
раз и `seek` не дёргается. Инвариант at-least-once сохранён: в B1a/B1b `commit` не
вызван ни разу, то есть перемотать не удалось → сообщение остаётся незакоммиченным и
будет передоставлено новым владельцем партиции. Комментарии в обеих ветках корректно
объясняют, почему отсутствие `seek` при ребалансе — не ошибка, а правильное поведение.

## M2 (MAJOR) — ЗАКРЫТ

**Файл:** `app/services/photo_service.py:68-82`

`BATCH_MAX_TOTAL_BYTES` перестал быть производной `MAX_BATCH_SIZE * MAX_FILE_SIZE_BYTES`
и стал независимым потолком `150 * 1024 * 1024`. Прогон:

```
M2 BATCH_MAX_TOTAL_BYTES = 157286400 = 150 MB
M2 reachable? max legit total 524288000 > 157286400 -> True
M2 4x50MB -> 413
```

Ветка стала достижимой на боевых константах. Отдельно проверил границу: 3 файла × 50 МБ
= 150 МБ проходят (не `>` лимита), 4-й файл переводит сумму в 200 МБ → 413. То, что
4-й файл всё же читается, — корректно и неизбежно: сумму нельзя узнать, не прочитав
файл; пик RAM при этом ровно `BATCH_MAX_TOTAL_BYTES + MAX_FILE_SIZE_BYTES` = 200 МБ,
что теперь честно задокументировано в docstring `read_batch_files`. Комментарий на
строках 69-81 внятно фиксирует, почему производная константа была мёртвым кодом.

**Требует протокола (не блокер, но нельзя терять):** 20_design в разделе «Что НЕ
трогать» прямо перечислял «Значения лимитов (50 МБ, 2–10, **500 МБ**)». Снижение до
150 МБ — сознательное отклонение от дизайна по моей рекомендации M2, эскалированное
оркестратором до обязательного. Это **видимое клиенту изменение поведения**: батч на
200–500 МБ, который раньше принимался, теперь получает 413. Обязательно отразить в
80_docs/README и в `specs/feature-upload/tasks.md` — иначе следующий ревьюер увидит
расхождение спеки и кода. Внутренних потребителей старого значения нет, тесты зелёные.

## M1 (MAJOR) — ЗАКРЫТ

**Файлы:** `app/integrations/metrics_worker.py:61-95`, `app/worker/consumer.py:102-105,
163, 217, 232, 248`

Добавлены `worker_messages_dropped_total{reason}` (фиксированный набор
`unparsable|missing_fields|invalid_uuid` — кардинальность не растёт) и
`worker_message_retries_total`. Прогон подтверждает инкремент во всех четырёх точках:

```
M1 dropped[unparsable]     + 1.0
M1 dropped[missing_fields] + 1.0
M1 dropped[invalid_uuid]   + 1.0
worker_message_retries_total + 1.0   (в сценарии B1a)
```

В docstring `metrics_worker.py` и `consumer.py` описано, как детектировать залипшую
партицию (`increase(worker_message_retries_total[5m]) > 0`, удерживающийся несколько
окон). Принятый компромисс «партиция залипает» стал управляемым.

**Инвариант F4 перепроверен после новых импортов** (`consumer.py` теперь тянет
`metrics_worker`) — обе стороны чисты:

```
API процесс:    metrics_worker imported = False; worker_messages_dropped_total = False
WORKER процесс: metrics_api    imported = False; photos_pending / http_requests_total = False
```

## m1 (MINOR) — ЗАКРЫТ

**Файл:** `app/api/uploads.py:51-65, 76-77, 96-100`

Переход на позднее связывание через модуль (`from app.services import photo_service`,
обращение `photo_service.MAX_FILE_SIZE_BYTES` в момент вызова). Прогон:

```
m1 late-binding: api SEES patched app.services.photo_service const -> PASS
```

То есть `monkeypatch.setattr(photo_service, "BATCH_MAX_TOTAL_BYTES", X)` теперь влияет
и на api-слой — ловушка для test-writer устранена, и утверждение дизайна «единственный
источник значений» стало правдой в рантайме, а не только в исходнике. Сообщение
`BatchSizeError` при переносе строк не изменилось (склейка даёт прежний текст) —
контракт ошибки цел.

## Что осталось открытым (не блокирует, переносится дальше)

| ID | Severity | Суть | Кому |
|---|---|---|---|
| M3 | MAJOR | Конкурентность батч-загрузок не ограничена: N параллельных запросов × 150 МБ. Стало легче после M2 (было ×500 МБ), но семафор / `--limit-concurrency` + строка в README всё ещё нужны | architect / documenter |
| M4 | MAJOR | Новые ветки по-прежнему без тестов; список из 7 пунктов в разделе M4 остаётся обязательным | test-writer |
| m2 | MINOR | `session.rollback()` вне защищённого блока в обеих компенсациях F5 | coder |
| m3 | MINOR | Незащищённый `delete_file` в StorageUnavailable-ветке `create_batch` | coder |
| m4 | MINOR | Двойной учёт `worker_messages_processed_total` при падении `_maybe_complete_batch` | coder / documenter |
| m5 | MINOR | Устаревший docstring `worker/main.py` про «stop() commits the last offset» | coder |
| N1–N3 | NOTE | Фиксированный backoff и объём логов; хрупкость identity map в `_maybe_complete_batch`; `batches.status` стал write-only | documenter / TASK-003 |
| — | NOTE | **Новое:** снижение лимита 500 → 150 МБ отразить в спеке и README (см. M2) | documenter |

**Дополнение к M4 после раунда 2** — покрытие новых веток не изменилось, плюс появились
две новые непокрытые (диапазон `consumer.py:147-199` включает оба `except` ребаланса):

```
app/api/uploads.py                  20   2   90%   78, 108
app/services/analysis_processor.py  92  11   88%   88-100
app/services/photo_service.py      134  17   87%   182-195, 277-291
app/worker/consumer.py              70  17   76%   147-199, 243-249
```

К списку M4 добавить:
(8) `commit()` бросает `CommitFailedError` → цикл жив, `seek` не вызван;
(9) `seek()` бросает `IllegalStateError`/`AssertionError` → цикл жив, `commit` не вызван;
(10) инкременты `worker_messages_dropped_total{reason}` по всем трём причинам и
`worker_message_retries_total` на RETRY-пути.

## Итог раунда 2

Блокер B1 закрыт по существу, а не формально: проверено, что цикл выживает во всех трёх
сценариях отказа, при этом счастливый путь и гарантия at-least-once не пострадали.
M2 закрыт содержательно — лимит стал реально достижимым, а не переименованным мёртвым
кодом. M1 и m1 закрыты и подтверждены прогоном. Регрессий не внесено: 370 тестов
зелёные, ruff чист, инвариант импорт-гигиены F4 держится в обе стороны после появления
новых импортов метрик в `consumer.py`.

**APPROVE.** Оставшиеся находки не блокируют. До PR обязательны: M4 (тесты) —
test-writer; отражение нового лимита 150 МБ в спеке и README — documenter.

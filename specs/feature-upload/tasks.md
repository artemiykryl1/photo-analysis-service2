# Реестр задач фичи feature-upload

> Скоуп синхронизирован с **Воркшопом 2** («От архитектуры к первому backend MVP на Python»).
> TASK-000 (bootstrap каркаса) выполнен — см. `tasks/TASK-000/`. Ниже — TASK-001, закрывающий DoD воркшопа.

## Зафиксированные решения (обязательны для всех агентов)

| Решение | Значение | Источник |
|---------|----------|----------|
| Префикс путей API | **`/v1/photos`** (НЕ `/api/v1/photos`) | воркшоп, слайд 18 |
| Порт api (uvicorn + docker) | **8000** (оставляем как в каркасе; в воркшопе показан 8080 — игнорируем) | решение пользователя |
| Значения статуса фото | **`pending → processing → done → failed`** | воркшоп, слайд 16 |
| Имя поля пути в MinIO | `object_key` (НЕ `s3_path`) | воркшоп, слайды 28, 30 |
| HTTP-код успешной загрузки | **202 Accepted** | воркшоп, слайд 15 |

Эти решения означают правки существующего каркаса TASK-000: переименовать enum-значения в модели и миграции v001, сменить префикс роутера, переименовать `s3_path`→`object_key`.

## Реестр

| ID | Задача | Статус |
|----|--------|--------|
| TASK-000 | Bootstrap каркаса (API→PostgreSQL/MinIO, Docker, /healthz) | ✅ done (PR #2) |
| TASK-001 | **Публичный API работы с фотографиями — закрывает DoD Воркшопа 2** | ✅ done (PR #4) |
| TASK-002 | **Асинхронный конвейер анализа + батчи + наблюдаемость — закрывает DoD Воркшопа 3** | ✅ done (PR #5) |
| TASK-003 | Веб-интерфейс, Kubernetes, подключение реального analyzer-а, развёртывание на сервер (Воркшоп 4) | todo (позже) |

TASK-004 из прежнего реестра (GET /{id}) поглощён TASK-001. Прежние TASK-002 (консьюмер) и TASK-003 (сохранение результатов) объединены в новую TASK-002 — по Воркшопу 3 worker сам сохраняет результат в PostgreSQL, отдельный топик результатов не нужен.

---

## TASK-001 — Публичный API работы с фотографиями

**Цель:** довести каркас до рабочего MVP, при котором можно загрузить фото, увидеть его в MinIO и в БД, получить статус и скачать содержимое. Закрывает практическое задание (слайд 40, пп. 4–9) и DoD (слайд 41) Воркшопа 2.

### В скоупе (реализуем)

1. **`POST /v1/photos`** — multipart-загрузка (поле `file`).
   - Валидация: MIME по magic bytes (JPEG/PNG); размер ≤ 50 МБ; пустой файл → ошибка.
   - Сохранить байты в MinIO по `object_key = photos/{photo_id}/original.<ext>`.
   - Создать запись в `photos` со `status = pending` (одна транзакция).
   - Ответ **202** `UploadPhotoResponse { photo_id, status: "pending" }`.
2. **`GET /v1/photos`** — список фотографий (пагинация опционально), массив `PhotoResponse`.
3. **`GET /v1/photos/{photo_id}`** — метаданные/статус: `PhotoResponse { id, filename, status }`; 404 если нет.
4. **`GET /v1/photos/{photo_id}/content`** — отдача байтов из MinIO с корректным `Content-Type`; 404 если объекта/записи нет; 503 если MinIO недоступен.
5. **`GET /healthz`** — уже есть (TASK-000), сохранить.
6. **Единый формат ошибок**, подключённый к ручкам (слайд 36):
   - не изображение → **415**; слишком большой → **413**; не найдено → **404**; MinIO недоступен → **503**; непредвиденное → **500**.
   - Тело: `{ error_code, message, request_id }`.
7. **Структурные JSON-логи** на каждом важном шаге с `photo_id` и `request_id` (слайд 35). Middleware генерации/проброса `request_id`.
8. **Pydantic-схемы** (слайд 16): `UploadPhotoResponse { photo_id: str, status: Literal["pending"] }`; `PhotoResponse { id: str, filename: str, status: Literal["pending","processing","done","failed"] }`.
9. **Repository layer** (слайд 29): `create`, `get_by_id`, `list`, `update_status`. Никакого SQL вне репозитория.
10. **Слой services**: бизнес-сценарий upload (сохранить в MinIO → создать запись; при сбое MinIO — корректная ошибка/откат). `asyncio.timeout(...)` вокруг вызовов MinIO/БД (слайд 23).
11. **БД / миграция** (слайды 26, 28):
    - Поля `photos`: `id` (PK, UNIQUE), `filename`, `object_key` (NOT NULL), `status` (enum/CHECK IN pending/processing/done/failed), `created_at`.
    - Enum `photo_status` привести к `pending/processing/done/failed` (правка модели + миграции; если PR #2 ещё не смёржен — редактируем v001, иначе новая ревизия v002 — решает architect).
    - Защита от дубликатов: UNIQUE + транзакция (слайды 24, 28), не только проверка в Python.
12. **`.proto` контракт analyzer-а** (слайд 38): `service PhotoAnalyzer { rpc AnalyzePhoto(...) }`, `AnalyzePhotoRequest { photo_id, object_key }`, `AnalyzePhotoResponse { faces_count, is_blurred, blur_score, perceptual_hash }`. Только контракт (+ опц. сгенерированные стабы); вызова из кода нет.
13. **Правки каркаса под зафиксированные решения**: префикс `/v1/photos`, порт 8000 (без изменений), `s3_path`→`object_key`.

### Вне скоупа (готовим/откладываем — по воркшопу)

Kafka, worker, вызов analyzer, Prometheus/Grafana, Kubernetes, gRPC-вызов (готовим только `.proto`), JWT (если не требуется воркшопом — не делаем), rate limiting.

### Критерии приёмки (DoD Воркшопа 2, слайд 41 — все 5 обязательны)

- [ ] Можем загрузить фото через `POST /v1/photos`.
- [ ] Файл появляется в MinIO (`photos/{photo_id}/original.*`).
- [ ] Таблица `photos` содержит запись со `status = pending`.
- [ ] `GET /v1/photos/{id}` отдаёт статус `pending`.
- [ ] Ошибки и логи содержат понятные поля (`error_code`, `request_id`, `photo_id`).

### Дополнительные гейты проекта (CLAUDE.md / constitution)

- [ ] Два APPROVE (reviewer-1 opus + reviewer-2 haiku).
- [ ] Тесты покрывают критерии приёмки; coverage ≥ 85%.
- [ ] **Интеграционный тест миграции на эфемерном Postgres** (testcontainers/pytest-postgresql) — закрывает пробел из `tasks/TASK-000/60_debug.md`, из-за которого проскочил баг enum.
- [ ] Живой прогон: `docker compose up --build` → полный сценарий upload→get отрабатывает (проверяет пользователь).

### Типичные ошибки, которых избегаем (слайд 39)

`async def` + `time.sleep()`; всё в `main.py`; нет миграций; SQL внутри endpoint-а; пароли в git; нет `request_id` в логах; файлы внутри контейнера.

---

## TASK-002 — Асинхронный конвейер анализа + батчи + наблюдаемость

**Цель:** фото после загрузки автоматически проходит полный жизненный цикл `pending → processing → done/failed` через Kafka и worker; появляется батчевая загрузка с выбором лучшего кадра; система наблюдаема через Prometheus/Grafana. Закрывает задание (слайд 40) и DoD (слайд 41) Воркшопа 3 + пользовательскую фичу батчей.

**Источники:** Воркшоп 3 «Асинхронная обработка и наблюдаемость» (Kafka, worker, retry, метрики); решения пользователя (батчи, анализатор-заглушка).

### Зафиксированные решения (обязательны для всех агентов)

| Решение | Значение | Источник |
|---------|----------|----------|
| Kafka-топик задач | **`photo.analysis.requested`** (НЕ `photos-to-analyze` из constitution — воркшоп главнее) | воркшоп 3, слайды 9–10 |
| Топик результатов | **НЕТ** — worker пишет результат напрямую в PostgreSQL | воркшоп 3, слайды 13, 18 |
| Сообщение в Kafka | `{photo_id, object_key, created_at, trace_id}` — БЕЗ самого файла | воркшоп 3, слайд 15 |
| Анализатор | **Заглушка (analyzer-stub)**: отдельный gRPC-сервис в compose, реализует существующий `protos/analyzer.proto`, возвращает детерминированные правдоподобные результаты (по хэшу object_key). Реальная нейронка в разработке — подключится в TASK-003 заменой адреса `ANALYZER_GRPC_ADDR` | решение пользователя |
| Защита от гонок | Атомарный `UPDATE photos SET status='processing' WHERE photo_id=:id AND status='pending'` — 1 строка = worker победил, 0 = задачу уже взяли | воркшоп 3, слайд 22 |
| Kafka недоступна при upload | Упрощённый **outbox**: upload НЕ падает, намерение сохраняется в БД (`not_sent`), фоновый publisher дошлёт (`not_sent → sent`) | воркшоп 3, слайды 23–24 |
| Retry | Ретраим ТОЛЬКО временные ошибки (timeout, unavailable, сеть) с лимитом попыток (3); постоянные (не изображение, ошибка валидации) → сразу `failed`. Поля `attempts`, `last_error_code`, `last_error_message` в БД | воркшоп 3, слайды 20–21, 25 |
| Offset-коммит | Коммит offset ТОЛЬКО после успешной обработки сообщения (at-least-once + идемпотентность через атомарный UPDATE) | воркшоп 3, слайды 17–18 |
| Батч | 2–10 файлов за один запрос, каждый файл проходит ту же валидацию, что одиночный (magic bytes, ≤50 МБ) | решение пользователя |
| Лучший кадр батча | Детерминированная формула по результатам анализа: сортировка `is_blurred` (false лучше) → `blur_score` (меньше лучше) → `faces_count` (больше лучше) → `created_at` (раньше лучше). Учитываются только фото со статусом `done`; все failed → `best_photo_id = null` | решение пользователя |
| Kafka в compose | Один брокер, режим KRaft (без ZooKeeper) | упрощение для MVP |
| Kafka-библиотека | `aiokafka` (async, совместима с нашим стеком) | стек проекта |

### В скоупе (реализуем)

**Блок A — асинхронный конвейер анализа:**

1. **Kafka в docker-compose** (KRaft, healthcheck, автосоздание топика `photo.analysis.requested`).
2. **Producer в API**: после успешного commit загрузки публикуется событие `{photo_id, object_key, created_at, trace_id}`. Реализация через упрощённый outbox: статус публикации в БД, фоновая дослав-задача для `not_sent`. Upload работает даже при лежащей Kafka.
3. **Worker** (отдельный контейнер, тот же образ, свой entrypoint `app/worker/`): consumer group, цикл: прочитал сообщение → атомарно `pending→processing` (0 строк = пропустить, дубль) → gRPC-вызов analyzer-stub → сохранил результат → `done`; при ошибке — классификация retry/no-retry, лимит 3 попытки, затем `failed` + `last_error_*`. Commit offset после обработки.
4. **Analyzer-stub**: минимальный gRPC-сервер (отдельный контейнер) по `protos/analyzer.proto`; результаты детерминированы от `object_key` (тесты воспроизводимы). Генерация Python-стабов из .proto (grpcio-tools) — теперь уже нужна и worker-у, и заглушке.
5. **БД/миграция v002**: таблица `analysis_results` (photo_id FK UNIQUE, faces_count, is_blurred, blur_score, perceptual_hash, created_at); поля `attempts`, `last_error_code`, `last_error_message`, статус публикации outbox в `photos`; таблица `batches` (batch_id PK, status, best_photo_id nullable FK, created_at) + `photos.batch_id` (nullable FK).
6. **`GET /v1/photos/{id}` расширяется**: поле `analysis` — `null`, пока не `done`; объект с результатами анализа, когда `done` (слайды 26–27).

**Блок B — батчи и лучший кадр:**

7. **`POST /v1/photos/batch`** — multipart с несколькими файлами (2–10): создаёт batch + фото, каждое уходит в конвейер анализа. Ответ 202 `{batch_id, photos: [{photo_id, status}]}`. Валидация каждого файла как в одиночной загрузке; невалидный файл в батче → весь батч отклоняется (400/413/415, атомарно — ничего не сохранено).
8. **`GET /v1/batches/{batch_id}`** — статус батча: `{batch_id, status: processing|completed, photos: [{photo_id, filename, status, analysis}], best_photo_id}`. Батч `completed`, когда ВСЕ его фото в терминальном статусе (done/failed); `best_photo_id` вычисляется по зафиксированной формуле (только когда completed, иначе null). 404 если батча нет.
9. Одиночный `POST /v1/photos` продолжает работать и тоже отправляет фото в конвейер анализа.

**Блок C — наблюдаемость:**

10. **`/metrics`** (prometheus_client) в API: `photos_uploaded_total`, `http_requests_total`, `http_request_duration_seconds`, `storage_upload_errors_total`, `kafka_publish_errors_total` (слайд 34) + gauge `photos_pending` (слайд 37).
11. **`/metrics`** в worker (свой HTTP-порт): `photo_analysis_started_total`, `photo_analysis_completed_total`, `photo_analysis_failed_total`, `photo_analysis_duration_seconds`, `analyzer_grpc_errors_total`, `worker_messages_processed_total` (слайд 35).
12. **Prometheus + Grafana в docker-compose**: Prometheus скрейпит API и worker; Grafana с provisioned-дашбордом минимум из 4 графиков (слайд 38): uploads per minute, analysis duration, failed analyses, pending photos.
13. Структурные JSON-логи в worker с `photo_id`, `trace_id` (сквозной из Kafka-сообщения), `attempt`.

### Вне скоупа (TASK-003, Воркшоп 4)

Веб-интерфейс, Kubernetes, реальная нейронка-анализатор (заглушка меняется на неё конфигом), развёртывание на сервер, JWT, rate limiting, DLQ (упрощаем: после лимита ретраев — `failed`, без dead-letter топика).

### Критерии приёмки (DoD Воркшопа 3, слайд 41 + батчи)

- [ ] `POST /v1/photos` → фото в `pending`.
- [ ] Kafka получила сообщение `photo.analysis.requested`.
- [ ] Worker прочитал и обработал сообщение (атомарный захват, идемпотентность дублей).
- [ ] Analyzer(-stub) вернул результат по gRPC.
- [ ] PostgreSQL обновился: `analysis_results` + статус `done` (или `failed` + `last_error_*` после ретраев).
- [ ] `GET /v1/photos/{id}` → `status: done` c заполненным `analysis`.
- [ ] `POST /v1/photos/batch` (2–10 фото) → 202 с `batch_id`; `GET /v1/batches/{id}` в конце → `completed`, все фото с результатами, `best_photo_id` по формуле.
- [ ] `/metrics` отдаёт метрики в API и worker.
- [ ] Grafana показывает дашборд (минимум 4 графика).
- [ ] Upload не падает при остановленной Kafka; после её старта фото дообрабатываются (outbox).

### Дополнительные гейты проекта (CLAUDE.md / constitution)

- [ ] Два APPROVE (reviewer-1 opus + reviewer-2 haiku).
- [ ] Тесты покрывают критерии приёмки; coverage ≥ 85%. Обязательно: юнит-тесты формулы лучшего кадра, классификации retry/no-retry, идемпотентности worker (0 строк от UPDATE), outbox-переходов; интеграционный тест миграции v002 на эфемерном Postgres (testcontainers).
- [ ] Живой прогон пользователем: `docker compose up --build` → загрузка батча → done → лучший кадр → графики в Grafana.

### Типичные ошибки, которых избегаем (слайды 17, 20–22 + опыт TASK-000/001)

Commit offset до обработки (потеря задач); бесконечный retry; retry невалидного файла; обновление статуса без атомарного условия (гонка двух worker-ов); файл внутри Kafka-сообщения; блокирующие вызовы (grpc sync, kafka sync) в event loop без to_thread/async-клиентов; миграция, не проверенная на реальном Postgres.

---

## TASK-002.1 — Багфиксы по внешним ревью TASK-002

**Статус:** 📋 готова к запуску (конвейер не стартовал).
**Основание:** два внешних ревью после мёржа PR #5 (ревьюер курса + второе независимое ревью). Все 8 находок проверены оркестратором по коду 2026-07-21 — все подтверждены. Схема БД НЕ меняется, публичные контракты API НЕ меняются.

### Зафиксированные находки и требуемые фиксы (приоритет по убыванию)

**F1 (КРИТИЧНО) — at-least-once ломается на программных ошибках.**
`app/worker/consumer.py`: `_handle_message` ловит все `Exception`, после чего `consume_loop` безусловно коммитит offset → при сбое внутри `process()` (например, БД недоступна до захвата) сообщение теряется навсегда.
Фикс: коммитить offset ТОЛЬКО если сообщение обработано или это подтверждённый poison-pill (нечитаемый JSON / нет photo_id/object_key). При неожиданном исключении — НЕ коммитить, залогировать, пауза (анти-hot-loop, например 1с), Kafka передоставит. Компромисс задокументировать: детерминированный баг на конкретном сообщении заблокирует партицию (DLQ вне скоупа — осознанно).

**F2 (КРИТИЧНО) — лимиты батча срабатывают после материализации в RAM.**
`app/api/batches.py:35` читает ВСЕ файлы целиком до проверок; лимиты количества (2–10) и суммарного размера (500 МБ) в `create_batch` проверяются, когда всё уже в памяти.
Фикс: проверка количества файлов ДО любого чтения; чтение каждого файла капнутое (`await f.read(MAX_FILE_SIZE_BYTES + 1)` → при превышении 413 немедленно, остаток не читать); бегущий суммарный размер с прерыванием при превышении BATCH_MAX_TOTAL_BYTES. Одиночный upload (`app/api/photos.py`) — тоже капнутое чтение. Ранний отказ по Content-Length (если передан) приветствуется.

**ИЗМЕНЕНИЕ КОНТРАКТА (внесено по итогам ревью, решение оркестратора).** В ходе ревью reviewer-1 доказал прогоном, что `BATCH_MAX_TOTAL_BYTES = MAX_BATCH_SIZE × MAX_FILE_SIZE_BYTES` (= 500 МБ) **недостижим в принципе**: per-file кап 50 МБ × 10 файлов даёт ровно 500 МБ, а условие строгое `>` — то есть проверка не срабатывала никогда и в RAM допускались ровно те 500 МБ, ради предотвращения которых лимит и вводился. Лимит заменён на независимую константу **150 МБ** (осознанный потолок RAM на один батч-запрос, не производная от других констант).

Видимое клиентам следствие: батч суммарным размером 150–500 МБ, который ранее принимался, теперь отклоняется с **413**. Ограничения «2–10 файлов» и «≤50 МБ на файл» не изменились. Это единственное изменение публичного поведения в TASK-002.1.

**F3 (MAJOR) — GET /v1/batches/{id} пишет в базу.**
`app/services/batch_service.py::get_batch` на GET выполняет `mark_completed` + commit. Следствия: батч не станет completed без GET; параллельные GET — конкурирующие UPDATE; GET небезопасен (ретраи прокси пишут в БД).
Фикс: перенести завершение батча в worker — после терминальной записи фото с `batch_id` проверять «все фото батча терминальны» и атомарно завершать (`UPDATE batches SET status='completed', best_photo_id=:b WHERE batch_id=:id AND status='processing'`; формула `select_best_photo` остаётся чистой и без изменений). `get_batch` становится строго read-only (статус для ответа можно вычислять на лету, но БД не трогать).

**F4 (MAJOR) — photos_pending задвоен: worker экспортирует чужой Gauge как 0.**
Единый `app/integrations/metrics.py` импортируется обоими процессами → в реестре worker-а регистрируются api-метрики (photos_pending, http_*, kafka_publish_errors_total и др.) и экспортируются нулями; в дашборде запрос `photos_pending` без фильтра → две серии, одна всегда 0 (видно на живом скрине Grafana).
Фикс: разделить на `metrics_api.py` (http_*, photos_uploaded_total, storage_upload_errors_total, kafka_publish_errors_total, photos_pending) и `metrics_worker.py` (photo_analysis_*, analyzer_grpc_errors_total, worker_messages_processed_total); импорты поправить (api-код и middleware → api-модуль; analysis_processor/worker → worker-модуль). Проверить, что /metrics worker-а больше не содержит photos_pending, а дашборд показывает одну серию pending.

**F5 (MAJOR) — MinIO-сирота при падении commit.**
`app/services/photo_service.py`: в `create_photo` и `create_batch` файл(ы) уже в MinIO, а `session.commit()` не обёрнут — упавший commit оставляет объекты-сироты.
Фикс: try/except вокруг commit в обоих путях → best-effort `delete_file` сохранённых объектов (в батче — всех сохранённых) + проброс 503 StorageUnavailable/DependencyUnavailable. Логировать компенсацию.

**F6 (MINOR) — нет restart-политик в compose.**
Ни у одного сервиса нет `restart:`. Фикс: `restart: unless-stopped` всем сервисам (api, worker, kafka, postgres, minio, analyzer-stub, prometheus, grafana).

**F7 (MINOR, решение за architect) — insecure gRPC-канал.**
`analyzer_client.py` использует `grpc.aio.insecure_channel`. Внутри одной docker-сети для MVP допустимо, но решение фиксируем явно: либо конфигурируемый TLS (env `ANALYZER_GRPC_TLS` + путь к root-cert, по умолчанию выкл в локальном compose), либо аргументированный дефер в TASK-003 (реальный analyzer через внешнюю сеть) с записью в design и docs. Выбор — за architect в 20_design.md.

**F8 (закрывается F2) — полная загрузка файлов в ОЗУ.**
Замечание «file.read() — путь к нехватке памяти» закрывается капнутыми чтениями из F2; отдельно задокументировать границу (стриминг в MinIO — вне скоупа, зафиксировать почему).

### Критерии приёмки

- [ ] Тест consumer: неожиданное исключение из `process()` → offset НЕ закоммичен; poison-pill (битый JSON / нет полей) → закоммичен; обработанное сообщение → закоммичен.
- [ ] Тест батча: 11 файлов отклоняются ДО чтения содержимого; файл >50 МБ прерывает чтение на капе (413), не дочитывая остаток; суммарный лимит срабатывает по бегущей сумме.
- [ ] Тест worker-завершения батча: все фото терминальны → батч completed + best_photo_id БЕЗ единого GET; `get_batch` не вызывает UPDATE (проверить отсутствием вызова mark_completed из сервиса GET-пути).
- [ ] Тест метрик: реестр/эндпоинт worker-а не содержит `photos_pending` и http_*-метрик; api-эндпоинт содержит. Дашборд: запрос pending даёт одну серию.
- [ ] Тест компенсации: падение commit в `create_photo` → вызван `delete_file`, клиенту 503; аналогично для батча (удалены все сохранённые объекты).
- [ ] `docker compose config`: у всех сервисов `restart: unless-stopped`.
- [ ] Все существующие тесты зелёные (обновить только те, чьё поведение осознанно изменено — F1/F3), ruff чистый, coverage не ниже текущего.
- [ ] Живой прогон: одиночное фото и батч до done/completed; `/metrics` обоих процессов; графики.

### Ограничения

Формулу лучшего кадра не менять. Миграций не требуется. Публичные HTTP-контракты (пути, коды, тела) не менять — F3 меняет только то, КТО пишет completed, не форму ответа.

# TASK-002 — Отчёт кодера (30_impl.md)

**Составлен оркестратором по факту диффа** (агент-coder был прерван на этапе финального отчёта; весь код блока A написан им, гейты прогнаны оркестратором). Блоки B/C — следующий заход.

## Статус: Блок A завершён. Блоки B/C не начаты.

## Гейты (прогнаны из photo-service/)
- `uv run ruff check .` → All checks passed!
- `uv run pytest -q -m "not integration"` → **173 passed, 1 deselected**
- `uv run alembic upgrade head --sql` (offline) → валиден; `CREATE TYPE photo_status` ровно 1 раз; созданы `photos`, `analysis_results`, `batches` (+ alembic_version). Бага двойного CREATE TYPE нет (в v002 новых enum нет — VARCHAR+CHECK).

## Что сделано по шагам 1–18 (блок A)

**Инфраструктура и зависимости**
- `pyproject.toml`: +aiokafka, +grpcio, +protobuf, +prometheus-client (runtime); +grpcio-tools (dev, для генерации стабов).
- `docker-compose.yml`: +kafka (KRaft, single-broker, healthcheck), +analyzer-stub (свой build), +worker (тот же образ api, command `python -m app.worker.main`); api получил зависимость от kafka; env для новых сервисов.
- `.env.example` / `app/core/config.py`: +KAFKA_BOOTSTRAP_SERVERS, KAFKA_TOPIC, ANALYZER_GRPC_ADDR, ANALYZER_GRPC_TIMEOUT, WORKER_MAX_ATTEMPTS, RETRY_BACKOFF_BASE_SECONDS, OUTBOX_POLL_INTERVAL_SECONDS, OUTBOX_BATCH_SIZE, METRICS_PORT.

**Схема БД (миграция v002, down_revision=v001)**
- `analysis_results` (photo_id PK+FK→photos ON DELETE CASCADE, faces_count, is_blurred, blur_score, perceptual_hash, created_at).
- `batches` (batch_id PK, status VARCHAR+CHECK IN (processing,completed), best_photo_id nullable FK→photos, created_at; индекс по created_at).
- `photos` ALTER: attempts (default 0), last_error_code, last_error_message, publish_status (VARCHAR+CHECK IN (not_sent,sent), default not_sent), published_at, trace_id, batch_id (FK→batches). Индексы: ix_photos_batch_id, ix_photos_status, частичный ix_photos_publish_status WHERE not_sent (ускоряет опрос outbox).
- `app/db/models.py`: модели AnalysisResult, Batch; расширен Photo новыми колонками.
- Порядок в upgrade() задокументирован (analysis_results → batches → ALTER photos), т.к. FK ссылаются на уже созданные таблицы.

**gRPC и analyzer-stub**
- `app/grpc_gen/` — сгенерированные стабы (analyzer_pb2, analyzer_pb2_grpc) закоммичены (не генерятся в образе — иначе ломались бы локальные тесты).
- `analyzer-stub/` — отдельный gRPC-сервер (server.py), свой Dockerfile + requirements.txt, codegen при сборке. Результаты детерминированы от object_key (hash): faces_count 0–5, blur_score 0.0–1.0, is_blurred = blur_score>0.6, perceptual_hash — 16-символьный hex.
- `app/integrations/analyzer_client.py`: AnalyzerGrpcClient (grpc.aio async-канал, таймаут), + классификация ошибок classify_grpc_error → RetryDecision (RETRY: timeout/unavailable/сеть; NO_RETRY: invalid argument/валидация), error_code_from_exception, truncate_error_message.

**Producer + Outbox (upload не зависит от Kafka)**
- `app/services/photo_service.py::create_photo`: в той же транзакции, что и вставка, ставит publish_status=not_sent + trace_id (из trace_id_var). Kafka в HTTP-пути НЕТ.
- `app/integrations/kafka_producer.py`: KafkaEventProducer (aiokafka), publish_analysis_requested → JSON {photo_id, object_key, created_at, trace_id}.
- `app/services/outbox.py`: run_outbox_publisher — фоновая asyncio-задача (стартует в lifespan), опрашивает not_sent, публикует, помечает sent; при недоступной Kafka строка остаётся not_sent (не падает). Graceful по stop_event.
- `app/repositories/photo_repository.py`: +fetch_unpublished, +mark_published.
- `app/main.py`: lifespan поднимает producer + фоновую задачу outbox, глушит по stop_event.

**Worker (consumer)**
- `app/worker/main.py`: entrypoint (python -m app.worker.main), сборка зависимостей, graceful shutdown (SIGTERM/SIGINT → stop_event).
- `app/worker/consumer.py`: consume_loop (getone с таймаутом, проверка stop_event между сообщениями), commit offset ТОЛЬКО после обработки (at-least-once); poison-pill (нечитаемый JSON) логируется и пропускается; trace_id из сообщения → trace_id_var.
- `app/services/analysis_processor.py`: ядро (тестируется без Kafka). Атомарный захват pending→processing (claim_for_processing; rowcount=0 → дубль, skip), retry-loop (backoff 1,2,4с, лимит WORKER_MAX_ATTEMPTS), запись attempts/last_error_* на каждой неудаче, терминал done (analysis_results.upsert + mark_done) или failed (mark_failed + reason no_retry/retries_exhausted).
- `app/repositories/photo_repository.py`: +claim_for_processing (атомарный UPDATE ... WHERE status='pending'), +record_attempt, +mark_done, +mark_failed.
- `app/repositories/analysis_result_repository.py`: upsert результата (1:1 к photos).

**API-расширение**
- `app/schemas/photos.py`: +AnalysisResultResponse; PhotoResponse.analysis (nullable, default None — обратная совместимость списка сохранена).
- GET /v1/photos/{id}: подтягивает analysis (null пока не done; объект при done).

**Метрики (хук блока C, заложен уже сейчас — его импортирует processor)**
- `app/integrations/metrics.py`: определены counters/histogram (photo_analysis_started/completed/failed_total, duration, analyzer_grpc_errors_total, worker_messages_processed_total, kafka_publish_errors_total). Экспорт endpoint /metrics и Prometheus/Grafana — блок C.

**Починка существующих тестов (ожидаемые поломки, §13 дизайна)**
- `tests/test_migration_integration.py`: ассерты набора колонок/таблиц расширены под v002.
- `tests/test_photo_service.py`, `tests/test_photos_endpoints.py`: подправлены под publish_status/trace_id и nullable analysis.

## Открытые вопросы для блока B/C
- Батч-эндпоинты: отдельный `app/api/batches.py` (по дизайну) — POST /v1/photos/batch, GET /v1/batches/{id}, формула лучшего кадра, атомарность батча.
- Метрики блока C: endpoint /metrics в API (+ gauge photos_pending через SELECT count при скрейпе), start_http_server в worker, prometheus.yml, Grafana provisioning (datasource + dashboard 4 графика), новые сервисы prometheus/grafana в compose.
- Тесты формулы/идемпотентности/retry/outbox — на этап test-writer.

---

## Блоки B/C — отчёт кодера (заход 2)

**agent:** coder · **model:** sonnet · **status:** done
**inputs:** specs/constitution.md, tasks/TASK-002/20_design.md (§12 блоки B/C), tasks/TASK-002/30_impl.md (блок A, выше), specs/feature-upload/tasks.md §TASK-002
**outputs:** код блоков B/C (см. «Изменённые/новые файлы» ниже), этот раздел

### Что сделано по шагам плана (§12)

**Блок B — батчи (шаги 19–25)**
- **19.** `app/core/errors.py`: `BatchSizeError` (400, `INVALID_BATCH_SIZE`) — сообщение включает фактическое число файлов.
- **20.** `app/repositories/batch_repository.py` (новый): `create` (add+flush, без commit), `get_by_id` (через `session.get(Batch, id, options=[selectinload(Batch.photos).selectinload(Photo.analysis)])` — фото и их analysis батча одним запросом, без N+1/лишних lazy-load из async-кода), `mark_completed` (UPDATE status='completed'+best_photo_id, идемпотентно — детерминированная формула, повторный вызов с теми же входами не меняет результат).
- **21.** `app/services/photo_service.py::create_batch`: двухфазно — (1) валидация ВСЕХ файлов тем же `_validate`, что и одиночная загрузка, ДО единой записи в БД/MinIO (список-комprehension, падает на первом невалидном файле — на этот момент ничего ещё не создано); (2) создание `Batch` + `Photo[]` (publish_status='not_sent', trace_id, batch_id) с сохранением каждого файла в MinIO; один `session.commit()` в конце. При сбое MinIO на N-м файле — rollback (batch/photos ещё не закоммичены) + best-effort удаление уже сохранённых объектов (`delete_file` не бросает — см. `app/integrations/storage.py`, повторно использована та же семантика, что и в одиночной загрузке). `photos_uploaded_total.inc(len(photos))` после коммита. `PhotoService.__init__` получил опциональный параметр `batch_repository: BatchRepository | None = None` (дефолт — `BatchRepository()`), чтобы существующие тесты/вызовы `PhotoService(repository=..., storage=...)` не сломались.
- **22.** `app/services/batch_service.py` (новый): `select_best_photo(photos: list[Photo]) -> uuid.UUID | None` — чистая функция без I/O (работает с уже загруженными ORM-инстансами `Photo`/`AnalysisResult`, легко конструируется в юнит-тестах без БД). Сортировка `min()` по ключу `(is_blurred, blur_score, -faces_count, created_at)` — ЗАФИКСИРОВАННАЯ формула (is_blurred ASC, blur_score ASC, faces_count DESC через отрицание, created_at ASC); только `status==done` с непустым `analysis`; пусто → `None`. `BatchService.get_batch`: 404 если батча нет; `status='completed'`, если все фото терминальны (done/failed) — иначе `processing` и `best_photo_id=None`; при первом наблюдении завершённости — write-through `mark_completed` + commit (идемпотентно при гонке параллельных GET, design §11 риск #11); переиспользует `_analysis_response` из `photo_service.py` (модуль-level функция, без дублирования маппинга ORM→DTO).
- **23.** `app/schemas/photos.py`: `BatchPhotoItem`, `BatchAcceptedResponse`, `BatchPhotoDetail`, `BatchResponse` — точно по контрактам §3.3 дизайна.
- **24.** `app/api/batches.py` (новый): роутер `prefix="/v1"`. `POST /photos/batch` — параметр `file: list[UploadFile] = File(...)` (имя поля `file`, повторяемое — под curl-пример дизайна `-F "file=@a.jpg" -F "file=@b.jpg"`), читает байты каждого файла и передаёт `list[tuple[filename, bytes]]` в `PhotoService.create_batch`. `GET /batches/{batch_id}` → `BatchService.get_batch`. Переиспользует `get_photo_service` из `app/api/photos.py` (та же DI-обвязка storage, что и для одиночной загрузки) + новый `get_batch_service`. Ни SQL, ни MinIO SDK здесь не импортируются (соблюдено правило слоистости).
- **25.** `app/main.py`: `app.include_router(batches.router)`.

**Блок C — наблюдаемость (шаги 26–30)**
- **26.** `app/api/metrics_middleware.py` (новый): `metrics_middleware(request, call_next)` — RED-метрики `http_requests_total{method,endpoint,status}` / `http_request_duration_seconds{method,endpoint}`; `endpoint` — шаблон маршрута (`request.scope["route"].path`), fallback `"unmatched"` (защита от взрыва кардинальности сырыми `photo_id` в пути, design §8.3/§11 риск #7). Использует `@app.middleware("http")`-стиль (не чистый ASGI) — обоснованно допущено дизайном, т.к. middleware не работает с `ContextVar`.
- **27.** `app/main.py`: `app.middleware("http")(metrics_middleware)` подключён ПОСЛЕ `app.add_middleware(RequestIdMiddleware)` (порядок добавления даёт metrics_middleware более внутреннее положение в стеке относительно `ExceptionMiddleware` — `call_next` внутри него видит уже готовый `Response` с корректным `status_code` от `register_exception_handlers`, включая 404/503/etc.). Новый async-роут `GET /metrics`: под `asyncio.timeout(METRICS_DB_TIMEOUT_SECONDS=3s)` обновляет gauge `photos_pending` через `PhotoRepository.count_pending(session)` (новый метод репозитория — SQL остался только в repositories/, не в main.py напрямую); при таймауте/ошибке — лог WARNING и отдача метрик со старым значением gauge (скрейп никогда не 500-ит). Возвращает `generate_latest(REGISTRY)` с `CONTENT_TYPE_LATEST`.
- **28.** `prometheus.yml` (корень photo-service/) — job'ы `photo-api` (api:8000) и `photo-worker` (worker:8001), scrape_interval 10s. `grafana/provisioning/datasources/prometheus.yml` — datasource Prometheus (`http://prometheus:9090`, isDefault). `grafana/provisioning/dashboards/provider.yml` — provider, `options.path=/var/lib/grafana/dashboards` (дашборды монтируются отдельно от provisioning-yml, чтобы не путать конфиг провайдера с самими JSON). `grafana/dashboards/photo-service.json` — 4 панели: uploads/min, p95 analysis duration, failed analyses (by reason), pending photos — запросы Prometheus взяты дословно из дизайна §9.2.
- **29.** `docker-compose.yml`: сервисы `prometheus` (образ `prom/prometheus`, volume `./prometheus.yml:/etc/prometheus/prometheus.yml:ro`, порт 9090) и `grafana` (образ `grafana/grafana`, `GF_SECURITY_ADMIN_PASSWORD=admin`, volumes provisioning+dashboards, порт 3000, `depends_on: prometheus`).
- **30.** Метрики воркера уже подняты в блоке A (`start_http_server(WORKER_METRICS_PORT)` в `app/worker/main.py`, инкременты в `analysis_processor.py`) — в этом заходе только подтверждено (не изменялось) и прописана scrape-цель `worker:8001` в `prometheus.yml`.

### Изменённые/новые файлы

Новые:
- `app/repositories/batch_repository.py`
- `app/services/batch_service.py`
- `app/api/batches.py`
- `app/api/metrics_middleware.py`
- `prometheus.yml`
- `grafana/provisioning/datasources/prometheus.yml`
- `grafana/provisioning/dashboards/provider.yml`
- `grafana/dashboards/photo-service.json`

Изменены:
- `app/core/errors.py` (+`BatchSizeError`)
- `app/repositories/photo_repository.py` (+`count_pending`)
- `app/services/photo_service.py` (+`create_batch`, конструктор +`batch_repository`)
- `app/schemas/photos.py` (+`BatchPhotoItem`, `BatchAcceptedResponse`, `BatchPhotoDetail`, `BatchResponse`)
- `app/main.py` (+metrics middleware, +`GET /metrics`, +`include_router(batches.router)`)
- `docker-compose.yml` (+`prometheus`, +`grafana`)

### Принятые мелкие решения (в рамках плана, не архитектурные)

1. **Поле формы для батча — `file` (не `files`).** Дизайн явно приводит curl-пример `-F "file=@a.jpg" -F "file=@b.jpg"` — назвал FastAPI-параметр `file: list[UploadFile]`, чтобы имя параметра совпало с именем поля формы (иначе потребовался бы `alias`/другое имя поля не по примеру дизайна).
2. **`get_photo_service` переиспользован из `app/api/photos.py`** для batch-upload вместо копии — единая точка DI для `ObjectStorage` из `app.state.storage`; `get_batch_service` — новый, без storage (батчам он не нужен, только `BatchRepository`).
3. **`StorageUnavailable` в `create_batch` без `photo_id=` kwarg** (в отличие от `create_photo`) — на момент ошибки контекст батчевый, а не единичное фото; передавать `batch_id` под именем kwarg `photo_id` показалось вводящим в заблуждение для логов ошибок, поэтому оставлен только `message`.
4. **`grafana/dashboards/*.json` монтируется в `/var/lib/grafana/dashboards`**, а не в ту же директорию, что и `provider.yml` (`/etc/grafana/provisioning/dashboards`) — чтобы не смешивать в одном volume-маунте YAML-конфиг провайдера и JSON дашбордов (два независимых docker volume mount из двух разных host-директорий). Design допускал оба варианта («path: ... или отдельная папка dashboards»).
5. **`select_best_photo` принимает ORM `Photo`-инстансы**, а не отдельный dataclass — тест-райтер может конструировать `Photo(...)`/`AnalysisResult(...)` в памяти (без БД) так же, как уже делает `test_photo_service.py` для `PhotoResponse`-маппинга; лишней прослойки DTO не вводилось.

### Как запустить/проверить локально

```bash
cd photo-service
uv run ruff check .                        # чисто
uv run pytest -q -m "not integration"      # 173 passed, 1 deselected (без новых тестов — задача test-writer)
uv run alembic upgrade head --sql          # валиден, миграция v002 не менялась
docker compose config -q                   # синтаксис compose валиден
```

Полный e2e-сценарий (см. §12 дизайна «Как проверить»):
```bash
docker compose -f photo-service/docker-compose.yml up --build
curl -si -F "file=@a.jpg" -F "file=@b.jpg" http://localhost:8000/v1/photos/batch   # 202 {batch_id,photos:[...]}
curl -s http://localhost:8000/v1/batches/<batch_id>                                 # completed + best_photo_id после анализа
curl -s http://localhost:8000/metrics | grep photos_pending
curl -s http://localhost:8001/metrics | grep photo_analysis
# Grafana http://localhost:3000 (admin/admin) → дашборд "Photo Service", 4 панели
```

Дополнительно (в рамках самопроверки, не коммитится) кодер прогнал ad-hoc скрипты через `uv run python`, вне тестового дерева, чтобы проверить формулу `select_best_photo` (тай-брейки, все-failed→None), атомарность `create_batch` (невалидный файл → `repository.create`/`batch_repository.create` не вызваны ни разу) и HTTP-маршрутизацию `/v1/photos/batch`, `/v1/batches/{id}`, `/metrics` через ASGI-транспорт — все сценарии прошли ожидаемо.

### Открытые вопросы для ревьюеров/test-writer

- Формула `select_best_photo` и атомарность `create_batch` покрыты только ad-hoc проверкой кодера (см. выше), не постоянными тестами — юнит-тесты по §13.2 дизайна (формула/батч-валидация/идемпотентность) остаются за `test-writer`.
- `metrics_middleware` не покрыт тестами на кардинальность/корректность `status` для 4xx/5xx через реальный `AppError`-путь — стоит добавить функциональный тест, аналогичный `test_request_id_middleware.py`.
- `/metrics` эндпоинт не покрыт тестом на «gauge обновляется реальным SELECT» (сейчас есть только happy-path ad-hoc прогон) — предлагается юнит-тест на `PhotoRepository.count_pending` + функциональный тест на `/metrics` с моком сессии, возвращающим конкретное число pending.
- `BatchRepository.get_by_id` использует `session.get(..., options=[...])` — паттерн, отличный от `select(...).options(...)` в `photo_repository.py`; оставлено осознанно (единственный способ получить PK-lookup с eager-load без ручного `select(Batch).where(...)`), но стоит, чтобы reviewer-1 подтвердил, что это не создаёт расхождение конвенций.
- Grafana-дашборд (`photo-service.json`) не проверялся визуально в реальном Grafana (только валидность JSON и текст PromQL-запросов, дословно скопированных из дизайна §9.2) — рекомендуется первичный ручной прогон `docker compose up` перед мержем.

### Чек-лист самопроверки по критериям приёмки (spec.md / feature-upload/tasks.md §TASK-002, блоки B/C)

- [x] `POST /v1/photos/batch` принимает 2–10 файлов, каждый валидируется как при одиночной загрузке.
- [x] Батч из <2 или >10 файлов → 400 `INVALID_BATCH_SIZE`, ничего не сохранено.
- [x] Невалидный файл в батче (пустой/большой/не-изображение) → соответствующий 400/413/415, весь батч атомарно отклонён (ни `Batch`, ни `Photo`, ни объектов в MinIO).
- [x] `GET /v1/batches/{id}` → 404 если батча нет; иначе `batch_id`, `status`, `photos[]` (с `analysis`), `best_photo_id`.
- [x] Формула лучшего кадра — чистая функция, реализована строго по зафиксированной сортировке; все-failed → `best_photo_id=null`.
- [x] Одиночная загрузка/список/get не сломаны (173 существующих теста зелёные без изменений).
- [x] `/metrics` (API, порт 8000) отдаёт Prometheus text-format; `photos_pending` обновляется на каждый скрейп через SELECT count(*); RED-метрики HTTP собираются middleware с лейблом-шаблоном маршрута.
- [x] Worker `/metrics` (порт 8001) — поднят в блоке A, подтверждён; добавлен в `prometheus.yml` как scrape-target.
- [x] Grafana provisioned datasource + dashboard с ≥4 панелями (uploads/min, p95 duration, failed analyses, pending photos).
- [x] `ruff check .` чисто; `pytest -q -m "not integration"` — все существующие тесты зелёные; `alembic upgrade head --sql` валиден (миграция v002 не тронута).

## Блоки A+B+C завершены, готово к ревью.

---

## Итерация правок 1

**agent:** coder · **model:** sonnet · **status:** done
**inputs:** tasks/TASK-002/40_review-1.md (reviewer-1, opus), tasks/TASK-002/41_review-2.md (reviewer-2, haiku), tasks/TASK-002/20_design.md, этот файл (блоки A/B/C выше)

Оба ревью дали `CHANGES_REQUESTED`. Ниже — что и где исправлено, строго по перечню оркестратора (без расширения скоупа).

### БЛОКЕРЫ

**BLK-1 (r1 B1) — самовосстановление outbox-producer**
- `app/integrations/kafka_producer.py`: `KafkaEventProducer` теперь ленивый и идемпотентный. Добавлены `self._producer: AIOKafkaProducer | None`, `self._started: bool`, `self._start_lock: asyncio.Lock()`, метод `_build_producer()`. Новый метод `ensure_started()`: быстрый выход, если уже запущен (без лока); под локом — повторная проверка (double-checked locking, безопасно при гонках между вызовами outbox-цикла и lifespan), попытка `producer.start()`; при исключении — `self._producer = None` (сбрасывает частично сконструированный объект, чтобы следующий вызов строил producer заново, а не долбился в тот же сломанный) и исключение пробрасывается вызывающему. `start()` теперь делегирует в `ensure_started()` (сохранена публичная сигнатура для `app.main.lifespan`). `stop()` — обнуляет `_started`/`_producer`. `publish_analysis_requested` — защитная проверка `if self._producer is None or not self._started: raise RuntimeError(...)` (защита от неправильного использования — caller обязан звать `ensure_started()` перед публикацией).
- `app/services/outbox.py::run_outbox_publisher`: в начале КАЖДОЙ итерации цикла (до открытия сессии/чтения outbox-строк) теперь `await producer.ensure_started()`. При исключении — `logger.warning(...)`, публикация в этой итерации пропускается (строки остаются `not_sent`), но цикл не падает и не блокируется — следующая итерация повторит попытку через `OUTBOX_POLL_INTERVAL_SECONDS`. При успехе — прежняя логика (fetch_unpublished → publish → mark_published → commit) без изменений, обёрнута в `else`-ветку.
- `docker-compose.yml`: `api.depends_on.kafka.condition` изменено с `service_started` на `service_healthy` (второе, дополнительное усиление поверх самовосстановления — оркестратор попросил оба изменения вместе). `worker.depends_on.kafka` уже был `service_healthy` — не трогалось. Комментарий у `api` обновлён: явно объясняет, что upload по-прежнему не зависит от Kafka в рантайме (outbox), а `service_healthy` здесь только устраняет гонку старта producer при `docker compose up` с нуля; настоящая защита от долгой недоступности Kafka — именно `ensure_started()`.
- Проверено: `docker compose config -q` — валиден; рендер `depends_on.kafka.condition: service_healthy` для `api` подтверждён через `docker compose config`.

**BLK-2 (r2-1) — пароль Grafana не хардкодить**
- `docker-compose.yml`, сервис `grafana`: `GF_SECURITY_ADMIN_PASSWORD: admin` заменено на `GF_SECURITY_ADMIN_PASSWORD: ${GRAFANA_ADMIN_PASSWORD:-admin_dev_only}`; добавлен `GF_SECURITY_ADMIN_USER: ${GRAFANA_ADMIN_USER:-admin}` (аналогично, т.к. в compose можно явно переопределить и логин).
- `.env.example`: добавлены `GRAFANA_ADMIN_USER=admin` и `GRAFANA_ADMIN_PASSWORD=admin_dev_only` с комментарием «локальный дефолт, MUST быть переопределён в любом не-локальном окружении».
- Проверено: `docker compose config` без `.env` в photo-service/ рендерит дефолты `admin` / `admin_dev_only` (нет хардкода в самом yaml).

**BLK-3 (r2-2) — валидация Kafka payload в consumer**
- `app/worker/consumer.py`: добавлена вспомогательная функция `_is_missing(payload, key)` (ключ отсутствует, не строка или пустая строка — считается отсутствующим). В `_handle_message`, сразу после успешного `json.loads`, если `payload` не dict или `photo_id`/`object_key` отсутствуют/пустые — `logger.warning("dropping Kafka message missing required fields", extra={"payload": ...})` и `return` (без исключения). Offset коммитится как обычно в `consume_loop` (ничего не менялось в цикле commit'а) — сообщение просто пропускается, как и поведение для битого JSON выше по коду. `trace_id` не трогается для отброшенных сообщений (contextvar не устанавливается на невалидном payload — как и раньше не было бы установлен, если бы упал на `payload["photo_id"]`).
- Проверено: `uv run ruff check .` чисто; ручной прогон через `python -c` (`json.loads('{"trace_id":"x"}')` → payload без `photo_id`/`object_key` → функция `_is_missing` возвращает `True` для обоих ключей) — логика проверена вручную (постоянный тест — задача test-writer).

**BLK-4 (r2-3) — согласовать лейблы метрики failed**
- `app/integrations/metrics.py`: у `worker_messages_processed_total` лейблы НЕ менялись (остался `result`: done/failed/skipped) — по рекомендации оркестратора это самый дешёвый и правильный вариант (не плодить кардинальность photo_id/object_key, не дублировать разбивку по причине). Добавлены два развёрнутых комментария прямо у объявлений метрик: (1) у `photo_analysis_failed_total` — что `reason` осознанно НЕ дублируется на `worker_messages_processed_total`, что у processor'а (`analysis_processor.py`) ровно два значения `reason`: `no_retry`/`retries_exhausted`, и что дашборд для разбора причин должен смотреть сюда; (2) у `worker_messages_processed_total` — явное «макро-уровень, без reason, без photo_id/object_key».
- Проверено: `analysis_processor.py` не менялся (уже писал `reason=no_retry|retries_exhausted` в `photo_analysis_failed_total.labels(reason=...)` — сверено чтением файла, соответствие полное, никаких других значений `reason` в кодовой базе нет).

### МАЙНОРЫ

**M-A (r1 M1) — финальный тай-брейк по photo_id**
- `app/services/batch_service.py::select_best_photo`: ключ `min()` дополнен последним элементом `p.photo_id` — итоговый порядок `(is_blurred, blur_score, -faces_count, created_at, photo_id)`. `uuid.UUID` в Python поддерживает упорядочивание из коробки (сравнение по `.int`), доп. приведение типов не требуется. Докстринг модуля дополнен объяснением, почему `created_at` — no-op тай-брейк внутри батча (все фото батча создаются в одной транзакции → одинаковый `now()`).

**M-B (r1 M3) — чистить last_error_* при успехе**
- `app/repositories/photo_repository.py::mark_done`: `UPDATE` теперь дополнительно выставляет `last_error_code=None, last_error_message=None` вместе с `status=done, attempts=attempts`. Сигнатура метода не менялась (те же параметры), проверено — в тестах нет мест, где мокается/ассертится `mark_done` (grep по `tests/` — 0 совпадений), поломок не ожидается.

**M-C (r1 M2) — стабильный порядок фото батча**
- `app/db/models.py::Batch.photos`: у relationship добавлен `order_by="Photo.created_at, Photo.photo_id"` (тот же двухуровневый порядок, что и финальный тай-брейк M-A — согласовано). `BatchRepository.get_by_id` не менялся — сортировка теперь гарантируется на уровне relationship, а не отдельным запросом.

**M-D (r2 major) — лимит суммарного размера батча**
- `app/services/photo_service.py`: добавлена константа `BATCH_MAX_TOTAL_BYTES = MAX_BATCH_SIZE * MAX_FILE_SIZE_BYTES` (= 10 × 50 МБ = 500 МБ, ровно как предложил оркестратор). В `create_batch`, сразу после фазы 1 (валидация каждого файла), добавлена проверка суммарного размера (`sum(len(data) for ...)`); при превышении — `PayloadTooLargeError` (413) с сообщением, включающим фактический и предельный размер в байтах. Проверка выполняется ДО создания `Batch`/`Photo` строк и ДО записи в MinIO (ничего не сохраняется при превышении, как и остальные фазы-1 валидации). Докстринг `create_batch` дополнен упоминанием этой проверки.

### Изменённые файлы (итерация правок 1)

- `app/integrations/kafka_producer.py` — BLK-1 (ленивый `ensure_started`)
- `app/services/outbox.py` — BLK-1 (вызов `ensure_started()` в начале каждой итерации)
- `docker-compose.yml` — BLK-1 (api зависит от kafka `service_healthy`) + BLK-2 (Grafana пароль/логин из env)
- `.env.example` — BLK-2 (`GRAFANA_ADMIN_USER`/`GRAFANA_ADMIN_PASSWORD`)
- `app/worker/consumer.py` — BLK-3 (валидация payload)
- `app/integrations/metrics.py` — BLK-4 (документирующие комментарии, без изменения лейблов)
- `app/services/batch_service.py` — M-A (тай-брейк по `photo_id`)
- `app/repositories/photo_repository.py` — M-B (`mark_done` чистит `last_error_*`)
- `app/db/models.py` — M-C (`Batch.photos` `order_by`)
- `app/services/photo_service.py` — M-D (`BATCH_MAX_TOTAL_BYTES` + проверка в `create_batch`)

### Гейты (прогнаны из photo-service/, после всех правок)

- `uv run ruff check .` → **All checks passed!**
- `uv run pytest -q -m "not integration"` → **173 passed, 1 deselected** (существующие тесты не тронуты, новых тестов на исправления не добавлялось — по контуру задачи это работа test-writer после ре-ревью).
- `uv run alembic upgrade head --sql` → валиден, DDL не изменился (миграция v002 не трогалась, как и просил оркестратор).
- `docker compose config -q` → валиден; вручную сверено, что `api.depends_on.kafka.condition == service_healthy` и `GF_SECURITY_ADMIN_PASSWORD`/`GF_SECURITY_ADMIN_USER` рендерятся из дефолтов env-переменных, а не хардкод-строкой в yaml.

### Открытые вопросы для ревью

- `ensure_started()` использует `asyncio.Lock()`, создаваемый в `__init__` (до старта event loop, если `KafkaEventProducer` конструируется в синхронном контексте) — в `app.main.lifespan` и в тестах `KafkaEventProducer(settings)` всегда создаётся уже внутри работающего event loop (лямбда лайфспана — корутина), так что риска «lock создан не в том loop» нет; отмечаю для ревью, т.к. это единственное более тонкое место в фиксe BLK-1.
- Комментарий про BLK-4 сознательно НЕ меняет лейблы метрики (как и разрешил оркестратор «либо задокументировать соответствие») — если ревьюеры всё же настаивают на лейбле `reason` у `worker_messages_processed_total`, это уже расширение скоупа за пределы того, что было явно разрешено в задании на итерацию, и требует отдельного захода.
- Тестов на новые ветки (poison-pill без `photo_id`/`object_key`, `ensure_started` retry-семантику, `BATCH_MAX_TOTAL_BYTES`, тай-брейк по `photo_id`) в этом заходе не добавлено — по вводным оркестратора «правки без расширения скоупа» и по общему конвейеру (test-writer вызывается после APPROVE обоих ревьюеров).

## Правки по ревью 1 внесены, готово к повторному ревью.

---

## Пост-ревью правки (живой ревьюер воркшопа, после PR #5)

Две точечные правки по замечаниям ревьюера (agent-coder внёс код, гейты прогнаны оркестратором — агент прерван на этапе отчёта инфраструктурным сбоем API).

**FIX-1 (docker-compose.yml, замечание M7 reviewer-1) — проброс SIGTERM.**
`sh -c "..."` делает шелл PID 1, который не пробрасывает SIGTERM дочернему процессу → graceful shutdown не срабатывает, контейнер убивается по таймауту (SIGKILL). Фикс: `exec` перед целевой командой, чтобы процесс стал эффективным PID 1.
- worker: `command: sh -c "exec uv run python -m app.worker.main"`
- api: `command: sh -c "uv run alembic upgrade head && exec uv run uvicorn app.main:app --host 0.0.0.0 --port 8000"`

**FIX-2 (app/worker/consumer.py) — комментарий приведён в соответствие с поведением.**
Прежний docstring обещал, что передоставка Kafka «безопасно всё починит». Уточнено: offset коммитится после process() (at-least-once на доставке); передоставка безопасна как дедуп (уже terminal/processing фото повторно не обрабатывается, claim=0 → skip), но НЕ воскрешает фото, чей воркер умер между захватом processing и терминалом — оно остаётся в processing (известный пробел, reaper в TASK-003). Логика кода не менялась.

**Гейты:** ruff clean, 367 passed / 1 integration skip, `docker compose config -q` валиден.

## Пост-ревью правки внесены.

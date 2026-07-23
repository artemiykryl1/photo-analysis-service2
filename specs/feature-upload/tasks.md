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

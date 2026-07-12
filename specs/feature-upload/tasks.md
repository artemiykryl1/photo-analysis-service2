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
| TASK-001 | **Публичный API работы с фотографиями — закрывает DoD Воркшопа 2** | todo |
| TASK-002 | Kafka-консьюмер анализа (подписка на photos-to-analyze) — вне воркшопа 2 | todo (позже) |
| TASK-003 | Сохранение результатов анализа (подписка на analysis-results) — вне воркшопа 2 | todo (позже) |

TASK-004 из прежнего реестра (GET /{id}) поглощён TASK-001.

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

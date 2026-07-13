---
task_id: TASK-001
agent: architect
model: opus
status: done
inputs:
  - specs/constitution.md
  - specs/feature-upload/tasks.md
  - tasks/TASK-001/10_context.md
  - tasks/TASK-000/20_design.md
  - tasks/TASK-000/60_debug.md
outputs:
  - tasks/TASK-001/20_design.md
spec_refs:
  - feature-upload/tasks.md §"TASK-001" (слайды 15,16,18,23,24,28,29,35,36,37,38,47)
  - constitution.md §2.3, §3.1, §3.2, §3.3
timestamp: 2026-07-12T00:00:00Z
---

# TASK-001 — Дизайн: Публичный API работы с фотографиями

## 0. Соответствие конституции (self-check)

- Микросервис photo-service, HTTP/REST — да (constitution §2.1/§2.2).
- Асинхрон/Kafka — вне скоупа воркшопа 2 (готовим только `.proto` контракт analyzer, §2.2/§2.4 — future TASK-002/003).
- Идемпотентность/ретраи/DLQ — относятся к Kafka-консьюмеру (TASK-002), здесь не требуется. Для upload: защита от дублей через PK + UNIQUE (§2.3, слайды 24/28).
- Наблюдаемость (§3.1): структурные JSON-логи с `request_id`/`trace_id` + `photo_id`, request_id middleware. Метрики Prometheus — вне воркшопа (отмечено как TODO).
- Отказоустойчивость (§3.2): MinIO недоступен → 503 (не 500); `asyncio.timeout` вокруг внешних вызовов; rollback БД при сбое MinIO.
- Безопасность (§3.3): MIME по magic bytes (не по расширению), лимит 50 МБ, санитизация object_key, секреты только из env. JWT — **сознательно отключён** решением оркестратора (воркшоп исключает авторизацию); зафиксировано в §1.
- K8s — вне скоупа воркшопа 2.

Противоречий с конституцией нет. Единственное осознанное отступление — отсутствие JWT (§3.3): допущено решением оркестратора для рамок воркшопа; `user_id` сохраняется nullable, чтобы не ломать будущее включение авторизации.

---

## 1. Обзор и границы

**Что делаем:** доводим каркас TASK-000 до рабочего MVP: 4 эндпоинта под `/v1/photos`, слои service/repository с реальной логикой, правка модели/миграции под зафиксированные статусы и `object_key`, request_id middleware, единый формат ошибок с `request_id`, `.proto` контракт analyzer, интеграционный тест миграции.

**Границы изменений (только сервис `photo-service`):**

| Файл | Действие |
|------|----------|
| `app/api/photos.py` | Переписать: prefix `/v1/photos`, 4 маршрута, DI сервиса |
| `app/schemas/photos.py` | +`UploadPhotoResponse`, +`PhotoResponse`; `ErrorResponse.trace_id`→`request_id` |
| `app/db/models.py` | enum → pending/processing/done/failed; `s3_path`→`object_key` (UNIQUE); +`filename`; `uploaded_at`→`created_at`; `user_id` nullable; правка индекса |
| `migrations/versions/v001_init_photos.py` | Синхронная правка v001 (НЕ v002) |
| `app/services/photo_service.py` | Реализовать 4 метода + валидацию |
| `app/repositories/photo_repository.py` | Реализовать create/get_by_id/list/update_status |
| `app/integrations/storage.py` | Оставить sync SDK; вызовы обернуть в `anyio.to_thread` на стороне сервиса; +`delete_file` (компенсация); косметика s3_path→object_key |
| `app/core/errors.py` | +`PayloadTooLargeError(413)`, +`UnsupportedMediaTypeError(415)`; хендлер отдаёт `request_id`; лог с `photo_id` |
| `app/core/logging.py` | `TraceIdFilter` инжектит `request_id` (= trace_id) в записи; helper для set/reset |
| `app/api/middleware.py` (новый) | Pure-ASGI request_id middleware |
| `app/main.py` | Подключить middleware |
| `protos/analyzer.proto` (новый) | Контракт PhotoAnalyzer |
| `app/integrations/analyzer_client.py` | Оставить документированным стабом (TODO), ссылка на proto |
| `pyproject.toml` | +`python-multipart` (runtime); +`testcontainers[postgres]` (dev) |
| `tests/` | Новые тесты эндпоинтов/сервиса/валидации + интеграционный тест миграции; обновить `test_stubs.py`, `test_errors.py`, `test_logging.py` |

**Вне скоупа:** Kafka/worker/вызов analyzer, метрики Prometheus, JWT, rate limiting, multipart-upload больших файлов в MinIO (файлы ≤50 МБ читаем в память).

---

## 2. Контракты эндпоинтов

Все под `router = APIRouter(prefix="/v1/photos", tags=["photos"])`. `/healthz` и `/readyz` остаются в `main.py` без изменений.

### 2.1 POST /v1/photos — загрузка

- **Вход:** `multipart/form-data`, поле `file: UploadFile = File(...)`.
- **Выход (успех):** **HTTP 202**, `UploadPhotoResponse { photo_id: str, status: "pending" }`.
- **Поток (порядок «БД→MinIO→commit», обоснование в §3.4):**
  1. `data = await file.read()`; `filename = file.filename`.
  2. Валидация (см. §5): пусто→400; >50 МБ→413; не JPEG/PNG по magic bytes→415. Возвращает `(ext, content_type)`.
  3. `photo_id = uuid4()`; `object_key = f"photos/{photo_id}/original.{ext}"`.
  4. В одной транзакции сессии: `repository.create(...)` (add+flush) → `storage.save_file(...)` через `anyio.to_thread` под `asyncio.timeout(60)` → `session.commit()`.
  5. Ответ 202.
- **Коды ошибок:** 400 (пусто), 413 (>50 МБ), 415 (не изображение), 409 (UNIQUE-конфликт), 503 (MinIO недоступен), 500 (прочее).

### 2.2 GET /v1/photos — список

- **Вход:** query `limit: int = 50` (1..100), `offset: int = 0` (≥0). Фильтра по пользователю нет (JWT off).
- **Выход:** `list[PhotoResponse]` (может быть пустым), сортировка `created_at DESC`.
- **Коды:** 200; 422 (невалидные limit/offset — стандартная валидация FastAPI); 500.

### 2.3 GET /v1/photos/{photo_id} — метаданные

- **Вход:** path `photo_id: uuid.UUID`.
- **Выход:** `PhotoResponse { id, filename, status }`.
- **Коды:** 200; 404 (нет записи); 422 (не-UUID); 500.

### 2.4 GET /v1/photos/{photo_id}/content — контент

- **Вход:** path `photo_id: uuid.UUID`.
- **Выход:** `Response(content=bytes, media_type=<из ext object_key>)`, заголовок `Content-Disposition: inline; filename="<filename>"`.
- **Поток:** `repository.get_by_id` (404 если нет записи) → `storage.get_file(object_key)` через `anyio.to_thread` под `asyncio.timeout(60)` → отдать байты. `content_type` = маппинг ext→MIME (`jpg/jpeg→image/jpeg`, `png→image/png`).
- **Коды:** 200; 404 (нет записи ИЛИ нет объекта в MinIO — `NoSuchKey`); 503 (MinIO недоступен); 500.
- **Примечание:** для воркшопа отдаём `Response` целиком (файлы ≤50 МБ в память допустимы). `StreamingResponse` — возможная оптимизация, но требует стриминга из sync SDK через thread-генератор — усложнение, откладываем.

---

## 3. Модель данных и миграция (правка v001)

### 3.1 Enum

```python
class PhotoStatus(str, enum.Enum):
    pending = "pending"
    processing = "processing"
    done = "done"
    failed = "failed"
```

### 3.2 Модель Photo (итог)

| Колонка | Тип | Ограничения | Комментарий |
|---------|-----|-------------|-------------|
| `photo_id` | UUID | PK, default uuid4 | публичный `id` в DTO = str(photo_id) |
| `filename` | String(512) | NOT NULL | оригинальное имя файла (для PhotoResponse) |
| `object_key` | String(1024) | NOT NULL, **UNIQUE** | путь в MinIO `photos/{photo_id}/original.<ext>` |
| `user_id` | String(255) | **NULLABLE** | JWT off; заполняется в будущем |
| `status` | Enum photo_status | NOT NULL, default pending, server_default 'pending' | |
| `created_at` | DateTime(tz) | NOT NULL, server_default now() | переименовано из `uploaded_at` |

- Индекс: заменить `ix_photos_user_uploaded (user_id, uploaded_at)` на `ix_photos_created_at (created_at)` — используется для сортировки списка (user-фильтра нет). UNIQUE на `object_key` даёт отдельный индекс автоматически.
- **filename** — не обязателен по конституции, но требуется публичным контрактом воркшопа (`PhotoResponse.filename`). Добавляем как отдельную колонку.

### 3.3 Правка миграции v001 (НЕ создавать v002)

Причина: PR #2 каркаса не смёржен, продовых данных нет, пользователь пересоздаёт БД `docker compose down -v`. Ревизия остаётся `v001`, `down_revision=None`.

Правки в `v001_init_photos.py`:
1. `photo_status_enum = postgresql.ENUM("pending","processing","done","failed", name="photo_status", create_type=False)` — **сохранить `create_type=False`** (фикс из TASK-000/60_debug, иначе вернётся DuplicateObjectError).
2. Колонки `create_table("photos", ...)`:
   - `photo_id UUID primary_key`
   - `filename String(512) NOT NULL`
   - `object_key String(1024) NOT NULL` (переименование `s3_path`→`object_key`)
   - `user_id String(255) nullable=True`
   - `created_at DateTime(tz) server_default=func.now() NOT NULL` (переименование `uploaded_at`)
   - `status photo_status_enum NOT NULL server_default='pending'`
3. `op.create_unique_constraint("uq_photos_object_key", "photos", ["object_key"])` (или `sa.UniqueConstraint` в create_table).
4. Индекс: `op.create_index("ix_photos_created_at", "photos", ["created_at"])`.
5. `downgrade()`: drop index `ix_photos_created_at`, drop table, `photo_status_enum.drop(bind, checkfirst=True)`. UNIQUE-констрейнт уйдёт вместе с таблицей.

**Требование консистентности:** значения enum и имена колонок в модели и в миграции обязаны совпадать 1:1 (урок TASK-000). Проверяется интеграционным тестом миграции (§8).

---

## 4. Слои и сигнатуры

### 4.1 schemas/photos.py

```python
class UploadPhotoResponse(BaseModel):
    photo_id: str
    status: Literal["pending"]

class PhotoResponse(BaseModel):
    id: str
    filename: str
    status: Literal["pending", "processing", "done", "failed"]

class ErrorResponse(BaseModel):        # правка существующего
    error_code: str
    message: str
    request_id: str                    # было trace_id
```
DTO строятся из ORM вручную (не `from_orm`): `PhotoResponse(id=str(p.photo_id), filename=p.filename, status=p.status.value)`.

### 4.2 repositories/photo_repository.py (SQL только здесь; commit — снаружи)

```python
async def create(self, session: AsyncSession, photo: Photo) -> Photo
    # session.add(photo); await session.flush()
    # except IntegrityError -> raise ConflictError("photo already exists")
    # return photo   (НЕ commit)

async def get_by_id(self, session: AsyncSession, photo_id: uuid.UUID) -> Photo | None
    # select(Photo).where(Photo.photo_id == photo_id); scalar_one_or_none

async def list(self, session: AsyncSession, limit: int, offset: int) -> list[Photo]
    # select(Photo).order_by(Photo.created_at.desc()).limit(limit).offset(offset)

async def update_status(self, session: AsyncSession, photo_id: uuid.UUID,
                        status: PhotoStatus) -> None
    # update(Photo).where(...).values(status=status)  (для будущего TASK-002; commit снаружи)
```
Переименование: текущий `get` → `get_by_id`. Транзакцию/commit НЕ делает — граница у сервиса.

### 4.3 services/photo_service.py (FastAPI-agnostic; получает bytes, не UploadFile)

```python
async def create_photo(self, session: AsyncSession, filename: str | None,
                       data: bytes) -> UploadPhotoResponse
    # 1. ext, content_type = self._validate(data)           # §5
    # 2. photo_id = uuid4(); object_key = f"photos/{photo_id}/original.{ext}"
    # 3. photo = Photo(photo_id=..., filename=filename or f"{photo_id}.{ext}",
    #                  object_key=object_key, status=PhotoStatus.pending)
    # 4. await self._repository.create(session, photo)       # add+flush
    # 5. try: async with asyncio.timeout(60):
    #        await anyio.to_thread.run_sync(self._storage.save_file,
    #             object_key, data, len(data), content_type)
    #    except (StorageUnavailable, TimeoutError):
    #        await session.rollback(); raise StorageUnavailable(...)
    # 6. await session.commit()
    # 7. log INFO {photo_id, object_key(only key, not full), size}
    # 8. return UploadPhotoResponse(photo_id=str(photo_id), status="pending")

async def get_photo(self, session, photo_id: uuid.UUID) -> PhotoResponse
    # photo = get_by_id; if None -> raise NotFoundError; map -> PhotoResponse

async def list_photos(self, session, limit: int, offset: int) -> list[PhotoResponse]

async def get_photo_content(self, session, photo_id: uuid.UUID) -> tuple[bytes, str]
    # photo = get_by_id; if None -> NotFoundError
    # async with asyncio.timeout(60):
    #     data = await anyio.to_thread.run_sync(self._storage.get_file, photo.object_key)
    # content_type = _ext_to_mime(photo.object_key)
    # return data, content_type
```
- `_validate(data) -> tuple[str, str]` — приватный helper (§5).
- `asyncio.timeout` только вокруг MinIO. TimeoutError маппится в StorageUnavailable(503).
- Логи на каждом важном шаге: `logger.info(..., extra={"photo_id": str(photo_id)})`. `request_id` подставляет фильтр логгера автоматически из contextvar.

### 4.4 integrations/storage.py

- Методы `save_file`/`get_file` остаются **синхронными** (SDK sync); оборачивание в `anyio.to_thread.run_sync(...)` — на стороне сервиса (чтобы storage не тянул anyio и оставался тонким).
- `get_file` уже кидает `NotFoundError` на `NoSuchKey` и `StorageUnavailable` на прочих S3Error — оставить.
- Добавить `delete_file(object_name: str) -> None` (best-effort, для компенсации orphan-объекта; глотает NoSuchKey). Пригодится, если решим чистить объект при сбое commit (см. §3.4, опционально).
- Косметика: в докстринге/возврате `save_file` заменить упоминание `s3_path` → `object_key` (возвращаемое значение `bucket/object_name` — информационное; сервис в БД пишет переданный `object_name`, НЕ возврат).
- Санитизация пути (нет `../`, `//`) — object_key формируется сервисом из uuid, инъекция невозможна; лёгкую проверку в `save_file` добавить как защиту (§3.3 конституции).

### 4.5 api/photos.py — DI

```python
def get_photo_service(request: Request) -> PhotoService:
    return PhotoService(repository=PhotoRepository(), storage=request.app.state.storage)
```
Эндпоинты берут `service: PhotoService = Depends(get_photo_service)`, `session: AsyncSession = Depends(get_session)`. Правило слоя сохраняется: api НЕ импортирует models/select/MinIO-SDK; передача экземпляра storage в сервис допустима (api сам MinIO не вызывает). commit/rollback инициирует сервис.

---

## 5. Валидация файла (magic bytes, вручную)

Порядок проверок в `_validate(data)`:
1. `len(data) == 0` → `ValidationError("empty file")` → **400**.
2. `len(data) > 50*1024*1024` → `PayloadTooLargeError` → **413**.
3. Сигнатуры:
   - JPEG: `data[:3] == b"\xFF\xD8\xFF"` → ext `jpg`, mime `image/jpeg`.
   - PNG: `data[:8] == b"\x89PNG\r\n\x1a\n"` (`89 50 4E 47 0D 0A 1A 0A`) → ext `png`, mime `image/png`.
   - иначе → `UnsupportedMediaTypeError` → **415**.
- Тип определяется ТОЛЬКО по содержимому, не по `Content-Type`/расширению (§3.3 конституции).
- Библиотеки libmagic/pillow НЕ используем (не тянем системную зависимость в slim-образ).

---

## 6. Обработка ошибок

`core/errors.py` — добавить классы, дополнить хендлер.

| HTTP | Класс | error_code | Ситуация |
|------|-------|-----------|----------|
| 400 | `ValidationError` (есть) | `INVALID_FILE` | пустой файл / прочий невалидный ввод |
| 413 | `PayloadTooLargeError` (новый) | `PAYLOAD_TOO_LARGE` | файл > 50 МБ |
| 415 | `UnsupportedMediaTypeError` (новый) | `UNSUPPORTED_MEDIA_TYPE` | не JPEG/PNG по magic bytes |
| 404 | `NotFoundError` (есть) | `NOT_FOUND` | нет записи/объекта |
| 409 | `ConflictError` (есть) | `CONFLICT` | нарушение UNIQUE (object_key/PK) |
| 503 | `StorageUnavailable` (есть) | `SERVICE_UNAVAILABLE` | MinIO недоступен/таймаут |
| 500 | `AppError` (база) | `INTERNAL_ERROR` | непредвиденное |

- Тело ответа: `ErrorResponse { error_code, message, request_id }`. `request_id = trace_id_var.get()`.
- Хендлер логирует `extra={"error_code": ..., "photo_id": <если доступно>}`. `request_id` подставит фильтр логгера.
- Для FastAPI `RequestValidationError` (422, битый UUID/limit) — оставляем дефолтный хендлер FastAPI; при желании обернуть в общий формат (опционально, не обязательно для DoD).

---

## 7. Наблюдаемость (request_id, логи)

### 7.1 request_id middleware — pure ASGI (НЕ BaseHTTPMiddleware)

Новый `app/api/middleware.py`:
- Реализовать как pure-ASGI middleware (`class RequestIdMiddleware` с `async def __call__(self, scope, receive, send)`), а НЕ `BaseHTTPMiddleware`.
- **Причина (риск):** `BaseHTTPMiddleware` выполняет downstream-приложение в отдельной задаче anyio, из-за чего `ContextVar`, выставленный в `dispatch()` до `call_next`, НЕ гарантированно виден в обработчике эндпоинта. Pure-ASGI middleware работает в той же задаче — contextvar виден и в сервисе, и в логах эндпоинта.
- Логика:
  1. Только для `scope["type"] == "http"`, иначе прокинуть дальше.
  2. `request_id = headers.get("x-request-id") or str(uuid4())`.
  3. `token = trace_id_var.set(request_id)`.
  4. Обернуть `send`: в событии `http.response.start` добавить заголовок `x-request-id: <request_id>`.
  5. `try: await app(scope, receive, wrapped_send) finally: trace_id_var.reset(token)`.
- Подключение: `app.add_middleware(RequestIdMiddleware)` в `main.py` (ставить внешним слоем, до роутера).

### 7.2 logging.py

- `TraceIdFilter.filter`: добавить `record.request_id = trace_id_var.get()` РЯДОМ с существующим `record.trace_id` (одно значение под двумя именами: `trace_id` — конституция §3.1, `request_id` — DoD воркшопа).
- Формат JSON: добавить `%(request_id)s` в fmt (или переименовать поле через rename_fields) — итог: в каждой строке лога есть `request_id`, `trace_id`, `service`; в бизнес-логах дополнительно `photo_id` (передаётся через `extra`).
- Оставить security-правило §3.3: не логировать полный object_key целиком/содержимое файла (логировать только `photo_id` и размер; object_key — опционально, но без user_id в plaintext).

### 7.3 Обязательные точки логирования

INFO: `upload received`, `stored in MinIO` (photo_id, size), `photo row committed` (photo_id); `content served` (photo_id). WARN: `MinIO unavailable on upload, rolled back` (photo_id). ERROR: через хендлер ошибок. Каждая строка автоматически несёт `request_id`.

---

## 8. Порядок upload и отказоустойчивость (обоснование)

**Выбранный порядок: БД(add+flush) → MinIO(save) → commit; rollback при сбое MinIO.**

Рассмотренные варианты:

| Вариант | Плюс | Минус |
|---------|------|-------|
| **A. БД flush → MinIO → commit (выбран)** | При сбое/таймауте MinIO `session.rollback()` убирает ещё не закоммиченную строку → нет «висячей» записи, указывающей на несуществующий объект. Точно соответствует указанию конституции «MinIO упал после БД → rollback». UNIQUE-конфликт ловится на flush до обращения к MinIO. | При падении самого `commit` уже после успешного save → orphan-объект в MinIO (мусор). Смягчение: best-effort `storage.delete_file` в except commit + WARN-лог; либо периодический GC (отложено). |
| B. MinIO → БД insert+commit | Нет незакоммиченных строк | При сбое БД — orphan-объект; строки нет, но объект есть. Тот же orphan-риск, но без гарантии «rollback при сбое MinIO» из ТЗ. |

Итог: вариант A. Он даёт консистентность «нет записи без объекта» в самом вероятном сбойном сценарии (MinIO down), а редкий orphan (сбой commit после save) — некритичен и не ломает контракт (GET по такому объекту без записи → 404). Явно фиксируем: **на `StorageUnavailable`/`TimeoutError` из save_file — `await session.rollback()` и 503**.

**Защита от дубликатов (слайды 24/28):** PK `photo_id` (uuid4) + UNIQUE `object_key`; конфликт ловится в `repository.create` на flush внутри транзакции и маппится в `ConflictError`→409 — не Python-проверкой, а средствами БД. Практически конфликт недостижим (свежий UUID), но constraint фиксирует инвариант на уровне схемы.

---

## 9. .proto контракт analyzer

Новый файл `photo-service/protos/analyzer.proto`:

```proto
syntax = "proto3";

package photo.analyzer.v1;

service PhotoAnalyzer {
  rpc AnalyzePhoto(AnalyzePhotoRequest) returns (AnalyzePhotoResponse);
}

message AnalyzePhotoRequest {
  string photo_id = 1;
  string object_key = 2;
}

message AnalyzePhotoResponse {
  int32 faces_count = 1;
  bool is_blurred = 2;
  double blur_score = 3;
  string perceptual_hash = 4;
}
```

**Решение:** только `.proto` файл, БЕЗ генерации Python-стабов (из кода вызовов нет — избегаем `grpcio-tools`/`protobuf` в зависимостях). `app/integrations/analyzer_client.py` остаётся документированным стабом с TODO, ссылающимся на `protos/analyzer.proto` (класс `AnalyzerClient` с методом-заглушкой, поднимающим `NotImplementedError` / помеченным TODO(TASK-002)). Генерация стабов — future TASK-002, тогда же добавятся dev-deps `grpcio-tools`+`protobuf`.

---

## 10. Зависимости к добавлению

`pyproject.toml`:
- **runtime:** `python-multipart>=0.0.9` — обязателен для `UploadFile`/`File(...)` в FastAPI.
- **dev:** `testcontainers[postgres]>=4.0` — эфемерный PostgreSQL для интеграционного теста миграции.

НЕ добавляем: `pillow`/`python-magic` (валидация вручную), `grpcio-tools`/`protobuf` (стабы не генерим), `python-magic` (системная libmagic несовместима с slim). Альтернатива testcontainers — `pytest-postgresql>=6.0` (требует локальный бинарь postgres; менее переносимо в Docker-CI) — запасной вариант, если Docker-in-Docker недоступен.

---

## 11. Тест-стратегия

### 11.1 Интеграционный тест миграции (закрывает пробел TASK-000/60_debug)

Файл `tests/test_migration_integration.py`, маркер `@pytest.mark.integration`, `skip` если Docker недоступен.

Подход (устойчивый к `lru_cache` в `get_settings`):
1. Поднять `PostgresContainer("postgres:16-alpine")` (testcontainers).
2. Сформировать `DATABASE_URL` в asyncpg-формате из параметров контейнера (`postgresql+asyncpg://user:pass@host:port/db`).
3. Запустить alembic **в subprocess** с `env={**os.environ, "DATABASE_URL": <url>}`, cwd=`photo-service/`:
   - `alembic upgrade head` → assert exit 0 (реальный `CREATE TYPE`/`CREATE TABLE` исполняется — именно это не ловилось offline-режимом).
4. Подключиться к контейнеру (sync psycopg/asyncpg) и проверить:
   - таблица `photos` существует; колонки `photo_id, filename, object_key, user_id, status, created_at` присутствуют;
   - `object_key` — UNIQUE (`information_schema.table_constraints`);
   - enum `photo_status` содержит ровно `{pending, processing, done, failed}` (`pg_enum`);
   - `CREATE TYPE` не падает дублем (косвенно — upgrade exit 0).
5. `alembic downgrade base` → assert exit 0; проверить, что таблицы `photos` и типа `photo_status` больше нет (чистый downgrade).

Subprocess выбран, чтобы каждый прогон alembic стартовал со «холодным» `get_settings` (свежий процесс), без ручной инвалидции `lru_cache`.

### 11.2 Функциональные тесты (httpx ASGI, с моками storage/repo или реальным контейнером)

- POST: валидный JPEG (magic bytes) → 202 + `{photo_id, status:"pending"}`; PNG → 202; пустой → 400; >50 МБ → 413; текст/пустая сигнатура → 415; MinIO бросает `StorageUnavailable` → 503 + проверка отката (repo.create вызван, commit НЕ вызван / rollback вызван).
- GET list → 200, массив; пагинация limit/offset.
- GET {id} → 200 для существующего; 404 для отсутствующего; невалидный UUID → 422.
- GET {id}/content → 200 c корректным `Content-Type`; 404 (нет записи); `NoSuchKey`→404; `StorageUnavailable`→503.
- Формат ошибок: тело содержит `error_code`, `message`, `request_id`.
- request_id middleware: ответ содержит заголовок `X-Request-ID`; переданный `X-Request-ID` эхо-возвращается; логи содержат его (можно проверить через caplog).
- Юнит валидации `_validate`: сигнатуры JPEG/PNG/мусор/пусто/размер.
- Repository (против контейнерного Postgres или sqlite-совместимого — предпочтителен Postgres из-за enum/UUID): create+get_by_id round-trip; UNIQUE→ConflictError; list ordering.

### 11.3 Обновление существующих тестов (контракт меняется — это осознанно)

- `tests/test_stubs.py` — методы service/repository теперь реализованы: заменить «raises NotImplementedError» на позитивные проверки поведения (или удалить контракт-локи для реализованных методов).
- `tests/test_errors.py` — поле `trace_id`→`request_id` в `ErrorResponse`; +кейсы 413/415.
- `tests/test_logging.py` — фильтр инжектит `request_id`.

Coverage ≥ 85% (гейт проекта).

---

## 12. Шаги для кодера (упорядоченно, с зависимостями)

> Правило: enum/переименования затрагивают модель+миграцию+сервис+storage одновременно — держать синхронно. Проверка «CREATE TYPE ровно один раз» уже обеспечена `create_type=False` — НЕ трогать этот флаг.

1. **pyproject.toml** — добавить в `dependencies` `python-multipart>=0.0.9`; в `dev` `testcontainers[postgres]>=4.0`. (Нет зависимостей от других шагов.)
2. **app/db/models.py** — enum → `pending/processing/done/failed`; `s3_path`→`object_key` + `unique=True`; добавить `filename: Mapped[str] String(512) NOT NULL`; `user_id` → `nullable=True`; `uploaded_at`→`created_at`; `status` default `PhotoStatus.pending`; индекс → `Index("ix_photos_created_at", "created_at")`. (Основа для 3,5,6.)
3. **migrations/versions/v001_init_photos.py** — синхронизировать с шагом 2: enum-значения, колонки (filename, object_key, user_id nullable, created_at, status server_default='pending'), UNIQUE `uq_photos_object_key`, индекс `ix_photos_created_at`; downgrade drop index/table/type. **Сохранить `create_type=False`.** НЕ создавать v002. (Зависит от 2.)
4. **app/core/errors.py** — добавить `PayloadTooLargeError(413, PAYLOAD_TOO_LARGE)`, `UnsupportedMediaTypeError(415, UNSUPPORTED_MEDIA_TYPE)`; в хендлере: собирать тело из `ErrorResponse(..., request_id=trace_id_var.get())`, логировать `extra={"error_code":..., "photo_id": getattr(exc,'photo_id',None)}` (photo_id опционален). (Зависит от 8 по полю request_id — можно параллельно, согласовать с 7.)
5. **app/schemas/photos.py** — `UploadPhotoResponse`, `PhotoResponse` (см. §4.1); `ErrorResponse.trace_id`→`request_id`. (Зависит от 2 семантически.)
6. **app/repositories/photo_repository.py** — реализовать `create` (add+flush, IntegrityError→ConflictError), `get_by_id` (переименовать из `get`), `list(limit, offset)`, `update_status`. SQL только здесь; без commit. (Зависит от 2.)
7. **app/core/logging.py** — `TraceIdFilter` инжектит `request_id` (=trace_id_var); добавить `%(request_id)s` в fmt; (опц.) helper контекст-менеджер set/reset. (Основа для 8,4.)
8. **app/api/middleware.py (новый)** — pure-ASGI `RequestIdMiddleware` (§7.1): X-Request-ID из заголовка или uuid4 → `trace_id_var.set` → эхо в ответе → reset в finally. (Зависит от 7.)
9. **app/integrations/storage.py** — добавить `delete_file(object_name)` (best-effort, глотает NoSuchKey); лёгкая проверка object_name (нет `..`, `//`); косметика s3_path→object_key в докстринге/возврате. Методы остаются sync. (Зависит от 2 по терминологии.)
10. **app/services/photo_service.py** — реализовать `create_photo`, `get_photo`, `list_photos`, `get_photo_content` + приватные `_validate(data)->(ext,mime)` и `_ext_to_mime(object_key)->mime` (§4.3, §5). MinIO-вызовы через `anyio.to_thread.run_sync` под `asyncio.timeout(60)`; на StorageUnavailable/TimeoutError при upload — `await session.rollback()` и 503; commit после успешного save. Логи с `photo_id`. (Зависит от 2,5,6,9.)
11. **app/api/photos.py** — prefix `/v1/photos`; `get_photo_service` DI; 4 маршрута (§2): POST(202, UploadFile), GET list (limit/offset), GET {photo_id:UUID}, GET {photo_id}/content (Response + media_type). api не импортирует models/select/MinIO-SDK. (Зависит от 5,10.)
12. **app/main.py** — `app.add_middleware(RequestIdMiddleware)`; роутер уже подключается. Проверить, что `/healthz`,`/readyz` не тронуты. (Зависит от 8.)
13. **protos/analyzer.proto (новый)** — контракт из §9. (Независим.)
14. **app/integrations/analyzer_client.py** — документированный стаб `AnalyzerClient` с TODO(TASK-002), ссылка на `protos/analyzer.proto`; без реальных вызовов. (Зависит от 13 по ссылке.)
15. **Обновить тесты-контракты каркаса** (иначе упадут): `tests/test_stubs.py` (методы реализованы), `test_errors.py` (request_id, 413/415), `test_logging.py` (request_id). Полные новые тесты пишет test-writer после ревью — здесь только чиним существующие красные. (Зависит от 4,5,6,7,10.)

### Как проверить (после реализации)

```bash
# 1. чистый прогон (пользователь пересоздаёт БД)
docker compose -f photo-service/docker-compose.yml down -v
docker compose -f photo-service/docker-compose.yml up --build   # alembic upgrade head должен пройти без DuplicateObject

# 2. upload -> get -> content (curl)
curl -si -F "file=@sample.jpg" http://localhost:8000/v1/photos      # -> 202 {photo_id, status:"pending"}, заголовок X-Request-ID
curl -s http://localhost:8000/v1/photos                             # -> [ {id, filename, status:"pending"} ]
curl -s http://localhost:8000/v1/photos/<photo_id>                  # -> {id, filename, status:"pending"}
curl -sO http://localhost:8000/v1/photos/<photo_id>/content         # -> байты, Content-Type: image/jpeg
curl -si -F "file=@notimage.txt" http://localhost:8000/v1/photos    # -> 415 {error_code:UNSUPPORTED_MEDIA_TYPE, request_id}
# проверить MinIO: объект photos/<photo_id>/original.jpg; таблица photos: строка status=pending

# 3. тесты
cd photo-service && uv run pytest -q          # включая test_migration_integration (нужен Docker)
```

---

## 13. Риски

| Риск | Митигция |
|------|----------|
| `BaseHTTPMiddleware` не пробрасывает contextvar в эндпоинт → request_id пустой в логах сервиса | Использовать **pure-ASGI** middleware (§7.1) — обязательное решение, не BaseHTTPMiddleware. |
| Рассинхрон enum/колонок модель↔миграция (повтор урока TASK-000) | Шаги 2 и 3 делать вместе; интеграционный тест миграции (§11.1) проверяет реальную БД. |
| `get_settings` закэширован (`lru_cache`) → тест миграции бьёт не в тот URL | Запускать alembic в subprocess с `DATABASE_URL` в env (§11.1). |
| Забыть `python-multipart` → 500/ошибка парсинга multipart | Шаг 1 первым; smoke-тест POST. |
| Orphan-объект при сбое commit после save | Осознанно принято (§8); опц. best-effort `delete_file` + WARN. |
| testcontainers требует Docker в тест-раннере | Маркер `@pytest.mark.integration` + skip без Docker; альтернатива pytest-postgresql. |
| Чтение всего файла в память (≤50 МБ) | В рамках воркшопа приемлемо; стриминг/multipart — future. |
| Смена контракта `ErrorResponse` (trace_id→request_id) ломает старые тесты | Шаг 15 обновляет их синхронно. |

---
task_id: TASK-000
agent: reviewer-1
model: opus
role: Глубокое ревью (корректность/архитектура)
inputs:
  - tasks/TASK-000/10_context.md
  - tasks/TASK-000/20_design.md
  - tasks/TASK-000/30_impl.md
  - specs/constitution.md
  - specs/feature-upload/spec.md
  - photo-service/ (весь каркас)
status: APPROVE
blocking_count: 0
iteration: 2
note: >-
  Итерация 1 — CHANGES_REQUESTED (1 BLOCKING: B1, порядок слоёв в Dockerfile).
  Итерация 2 — кодер закрыл B1 (вариант 1: split uv sync + --no-install-project).
  Итоговый статус APPROVE. См. раздел «Re-review итерации 2».
timestamp: 2026-07-11T00:00:00Z
---

# TASK-000 — Ревью-1 каркаса photo-service (bootstrap)

## Вердикт (итоговый, итерация 2)

**status: APPROVE** — единственный BLOCKING (B1) закрыт в итерации 2, новых блокеров нет.
Архитектура каркаса корректна и строго по дизайну: слоистость, изоляция MinIO, конфиг из env,
async-Alembic, `get_session` без утечек, `/healthz` без внешних зависимостей. NON-BLOCKING
N1–N7 остаются отложенными на TASK-001.

> История: итерация 1 была CHANGES_REQUESTED из-за B1 (см. ниже, раздел сохранён как есть).
> Физический `docker compose up --build` остаётся ручным пунктом для машины с Docker — это
> ожидаемо и само по себе не блокер.

Скоуп калиброван как bootstrap: отсутствие upload/Kafka/JWT/метрик/worker — НЕ считается замечанием.

---

## Re-review итерации 2 (фикс B1)

**Проверенный файл:** `photo-service/Dockerfile` (единственное изменение итерации 2).

**Что стало:**
```
COPY pyproject.toml ./
RUN uv sync --no-dev --no-install-project   # ставит только сторонние deps, app/ не нужен
COPY app/ ./app/
COPY alembic.ini ./
COPY migrations/ ./migrations/
RUN uv sync --no-dev                        # app/ уже на диске → hatchling собирает проект
```

**Вердикт по B1: ЗАКРЫТ.** Механизм сбоя устранён:
- Слой зависимостей (`--no-install-project`) больше не запускает hatchling-сборку пакета `app`,
  которого на этом слое ещё нет — именно это раньше валило `docker compose up --build`.
- Финальный `uv sync --no-dev` выполняется уже после `COPY app/`, `alembic.ini`, `migrations/`,
  когда каталог `app/` присутствует, поэтому `packages=["app"]` резолвится и `photo-service==0.1.0`
  собирается корректно. Соответствует предложенному варианту 1 из B1.

**Проверка на отсутствие новых проблем:**
- Dev-зависимости в prod-слой не тянутся: оба вызова с `--no-dev` (pytest/ruff/httpx исключены).
- Кэш-слои консистентны: слой deps зависит только от `pyproject.toml`; изменение кода в `app/`
  не инвалидирует установку зависимостей (кэш-френдли порядок сохранён).
- COPY-пути существуют: `app/`, `alembic.ini`, `migrations/` присутствуют в дереве.
- `CMD ["uv","run","uvicorn","app.main:app",...]` — импорт `app.main` подтверждён кодером
  эмуляцией слоёв через реальный `uv` (`app.title == "photo-service"`).
- Команда compose `alembic upgrade head && uvicorn ...` исполнима: `alembic.ini` и `migrations/`
  скопированы в образ; env.py берёт URL из Settings.
- `python -m py_compile` по всем модулям — OK (Python-код в этой итерации не менялся).

**Остаточный пункт (не блокер):** физический `docker compose up --build` + `curl /healthz`==200
не прогонялся (Docker недоступен в песочнице) — статическая эмуляция слоёв через `uv` напрямую
закрывает описанный в B1 механизм сбоя; живой прогон остаётся ручной финальной проверкой на
машине с Docker.

---

## Сильные стороны

- Строгая слоистость соблюдена фактически, не только на словах: `api/photos.py` не импортирует
  SQLAlchemy/minio; `services/` не знает про FastAPI; `repositories/` принимает `AsyncSession`
  аргументом и не коммитит; `core/` не импортирует вышестоящие слои. Циклов нет.
- Фото только в MinIO, в БД только `s3_path` — правило ТЗ соблюдено (`integrations/storage.py`
  единственная точка работы с байтами).
- `get_session` корректен: `async with SessionLocal()` гарантирует закрытие сессии, commit/rollback
  явно делегирован вызывающему слою — без утечек.
- `lifespan` корректен: `ensure_bucket` и `SELECT 1` обёрнуты try/except-warning (старт не падает
  при недоступности внешних систем), `engine.dispose()` на shutdown.
- `/healthz` действительно не трогает внешние зависимости (liveness), `/readyz` мягко возвращает 503.
- Alembic env.py — корректный async-шаблон (`async_engine_from_config` + `run_sync`), URL из
  Settings, `NullPool`. Ревизия v001 консистентна с моделью (таблица photos, enum photo_status,
  индекс ix_photos_user_uploaded), upgrade/downgrade симметричны, enum создаётся/удаляется явно
  с `checkfirst=True`.
- Конфиг — только env через pydantic-settings; секреты не захардкожены (дефолт `minioadmin` только
  локальный, помечен комментарием). `.env` в `.gitignore`, `.env.example` присутствует.
- Маппинг ошибок MinIO (`S3Error` → `StorageUnavailable`/`NotFoundError`) и единый формат
  `{error_code, message, trace_id}` соответствуют spec §2 и constitution §3.2.

---

## BLOCKING замечания

### B1 [РЕШЕНО в итерации 2]. Dockerfile: `uv sync` выполнялся ДО копирования `app/` — сборка проекта (hatchling) падала
**Файл (итерация 1):** `photo-service/Dockerfile:13-17`
```
COPY pyproject.toml ./
RUN uv sync --no-dev        # <-- app/ ещё НЕ скопирован
COPY app/ ./app/
```
**Проблема (была):** `pyproject.toml` объявляет `[build-system] hatchling` и
`[tool.hatch.build.targets.wheel] packages = ["app"]`. По умолчанию `uv sync` устанавливает сам
проект (root package), запуская build backend. На шаге `RUN uv sync` каталога `app/` в образе ещё
нет → hatchling не находит пакет `app` и сборка колеса/editable-инсталла падает («Unable to
determine which files to ship» / отсутствует директория пакета). Это ломало основной критерий
готовности TASK-000 — `docker compose up --build`.

**Статус:** ЗАКРЫТ в итерации 2 (вариант 1: `--no-install-project` на слое deps + второй
`uv sync --no-dev` после `COPY app/`). Детали — см. раздел «Re-review итерации 2».

---

## NON-BLOCKING замечания (остаются на TASK-001)

### N1. Рассинхрон default статуса между моделью и миграцией
**Файл:** `photo-service/app/db/models.py:41-45` vs `migrations/versions/v001_init_photos.py:42-47`
Модель задаёт только Python-side `default=PhotoStatus.queued` (без `server_default`), а миграция
ставит `server_default="queued"`. Функционально для bootstrap безвредно, но при будущем
`--autogenerate` Alembic может увидеть расхождение и предложить дроп server_default.
**Рекомендация:** в модели добавить `server_default="queued"` в `mapped_column` для `status`,
чтобы схема ORM и миграция совпадали (пригодится уже в TASK-001).

### N2. `MINIO_BUCKET` имеет дефолт, хотя context помечал его как «обязательный»
**Файл:** `photo-service/app/core/config.py:33`
`10_context.md` перечисляет `MINIO_BUCKET` среди обязательных переменных. Дефолт `"photos"` —
несекретное значение и допустим для bootstrap (совпадает с constitution §2.3). Оставляю
non-blocking, но зафиксируйте решение: дефолт осознанный, не забыт.

### N3. `pool_size`/`max_overflow` заданы, таймаут БД (5s, constitution §3.2) — нет
**Файл:** `photo-service/app/db/session.py:16-21`
Пул настроен (10+5 ≤ 50 — ок), но `pool_timeout`/`connect_args` с `command_timeout` не заданы.
Для bootstrap приемлемо, но constitution §3.2 требует 5s на запрос БД — заложить в TASK-001.

### N4. `readyz` дёргает `ensure_bucket()` как проверку доступности MinIO
**Файл:** `photo-service/app/main.py:80`
`ensure_bucket()` идемпотентен, но это операция записи (make_bucket при отсутствии), а не
lightweight health-probe. На каждый вызов `/readyz` идёт `bucket_exists` (сетевой round-trip,
синхронный, блокирует event loop — в bootstrap допустимо). Для будущего readiness лучше лёгкая
проверка (`list_buckets`/`bucket_exists` без создания) и вынос в `to_thread`. TODO уже отмечен в
impl — просто зафиксировать.

### N5. `readyz` формирует JSON вручную строкой
**Файл:** `photo-service/app/main.py:87-91`
`Response(content='{"status":"not_ready"}', media_type="application/json")` работает, но
несогласовано с остальным кодом (везде Pydantic/JSONResponse). Мелочь стиля — лучше
`JSONResponse(status_code=503, content={"status": "not_ready"})`.

### N6. `PhotoService.upload_photo(*args, **kwargs)` — размытая сигнатура
**Файл:** `photo-service/app/services/photo_service.py:24`
Дизайн (§3.10) допускал заглушку с NotImplementedError, но `*args/**kwargs` не документирует
будущий контракт (в отличие от `repositories`, где сигнатуры конкретные). Не блокирует, но
конкретная сигнатура (даже закомментированная) полезнее для TASK-001.

### N7. `analyzer_client.py` — полностью пустой модуль (только docstring)
**Файл:** `photo-service/app/integrations/analyzer_client.py`
Строго по дизайну §3.7 (нигде не импортируется — подтверждено). Замечаний нет; фиксирую
соответствие. `ruff` может ругаться на пустой модуль при некоторых правилах — сейчас проходит.

---

## Проверка соответствия дизайну / ТЗ (чеклист)

| Пункт (дизайн/ТЗ) | Статус | Комментарий |
|---|---|---|
| Слоистость api→services→repositories→db, без обратных импортов | OK | проверено по всем файлам, циклов нет |
| Хендлер без SQL и без прямого MinIO | OK | `api/photos.py` пустой роутер, правило в docstring |
| Фото только в MinIO, в БД только s3_path | OK | `storage.py` изолирован, модель хранит s3_path |
| Конфиг только через env, секреты не в коде | OK | pydantic-settings, дефолты несекретные/локальные |
| async engine + sessionmaker + get_session без утечек | OK | `async with SessionLocal()` |
| lifespan: ensure_bucket + SELECT 1 + dispose | OK | старт не падает при недоступности |
| /healthz без внешних зависимостей → 200 | OK | подтверждено pytest |
| /readyz мягкий 503 | OK | N4/N5 — стилевые |
| Alembic env.py async, target_metadata=Base.metadata, URL из Settings | OK | корректный async-шаблон |
| Ревизия v001 консистентна с моделью (photos, enum, индекс), upgrade/downgrade | OK | N1 — минорный рассинхрон server_default |
| MinIO save_file/get_file/ensure_bucket + маппинг ошибок | OK | синхронный SDK помечен TODO(to_thread) |
| ensure_bucket идемпотентен | OK | bucket_exists перед make_bucket |
| core/errors формат {error_code, message, trace_id} + маппинг статусов | OK | соответствует spec §2 |
| core/logging JSON + LOG_LEVEL + contextvar trace_id | OK | rename_fields level корректен |
| Заготовки services/repositories = NotImplementedError | OK | N6 — размытая сигнатура upload_photo |
| analyzer_client — пустая заглушка, не импортируется | OK | подтверждено |
| Dockerfile python:3.12-slim + uv + uvicorn | OK (итер.2) | B1 закрыт: split uv sync + --no-install-project |
| docker-compose: healthcheck'и, volumes, depends_on service_healthy, миграции перед uvicorn | OK | конфигурация корректна |
| .env.example + .env в .gitignore | OK | присутствуют |
| docker compose up физически проверен | НЕТ (ожидаемо) | Docker недоступен; ручной пункт, не блокер |

## Статические проверки, выполненные при ревью
- `python -m py_compile` по всем модулям и миграциям — OK (локально Python 3.10, синтаксис 3.10+
  совместим; целевой рантайм 3.12).
- Ручная трассировка импортов — циклов между слоями нет; `core`/`schemas` не тянут вышестоящие.
- Итерация 2: перечитан `Dockerfile`; порядок слоёв и `--no-install-project`/финальный `uv sync`
  верифицированы — механизм сбоя B1 закрыт. Docker физически недоступен — эмуляция слоёв через
  `uv` (кодером) закрывает механизм сбоя статически.

---

## Итог

Каркас архитектурно корректен и соответствует дизайну. Блокер B1 (порядок слоёв в Dockerfile)
закрыт в итерации 2 корректным способом (вариант 1), новых блокеров не внесено. Итоговый статус —
**APPROVE**. NON-BLOCKING N1–N7 вынесены в TASK-001. Живой `docker compose up --build` —
ручная финальная проверка на машине с Docker (не блокер).

# TASK-000 — 60_debug.md

- task_id: TASK-000
- agent: test-debugger
- status: FIXED
- date: 2026-07-11

## Симптом

При `docker compose up` (реальный PostgreSQL) контейнер `api` падает на этапе `alembic upgrade head`:

```
sqlalchemy.exc.ProgrammingError: (asyncpg.exceptions.DuplicateObjectError) <class 'asyncpg.exceptions.DuplicateObjectError'>: type "photo_status" already exists
[SQL: CREATE TYPE photo_status AS ENUM ('queued', 'analyzing', 'done', 'error')]
```

Traceback: дубль-`CREATE TYPE` эмитится изнутри `op.create_table(...)` (`visit_enum` → `CreateEnumType`), после того как тип уже был создан явно строкой выше.

Воспроизводится на чистой БД (не проблема "застрявшего" тома). Существующие тесты его не ловили, т.к. использовался `alembic upgrade head --sql` (offline-режим, DDL не исполняется реальной БД) и unit-тесты с моками (реальный `CREATE TYPE` не выполнялся ни разу).

## Root cause (с доказательством)

Файл `photo-service/migrations/versions/v001_init_photos.py`:

```python
photo_status_enum = postgresql.ENUM(
    "queued", "analyzing", "done", "error", name="photo_status"
)

def upgrade() -> None:
    bind = op.get_bind()
    photo_status_enum.create(bind, checkfirst=True)   # (1) явное создание типа

    op.create_table(
        "photos",
        ...
        sa.Column("status", photo_status_enum, ...),  # (2) create_table сам эмитит CREATE TYPE
    )
```

`postgresql.ENUM` по умолчанию имеет `create_type=True`. Alembic/SQLAlchemy DDL-компилятор при компиляции `CREATE TABLE`, содержащей столбец типа `ENUM` с `create_type=True`, сам эмитит `CREATE TYPE <name> AS ENUM (...)` **без** `checkfirst` — это заложенная логика `visit_enum`/`CreateEnumType` в диалекте PostgreSQL. Поскольку шаг (1) уже создал тип явно с `checkfirst=True`, шаг (2) пытается создать его повторно без проверки существования → `DuplicateObjectError` на живой БД.

Доказательство до фикса (offline SQL, который генерируется без учёта checkfirst — DDL "как есть"):
`CREATE TYPE photo_status AS ENUM (...)` встречался бы дважды логически (одна инструкция сгенерирована compile-time дважды за счёт двух источников), но т.к. offline-режим просто выводит SQL без выполнения — тесты этого не замечали. На реальной БД первая инструкция (checkfirst) молча проходит (тип ещё не существует → создаёт), вторая (без checkfirst, встроенная в create_table) падает, т.к. тип уже есть.

## Классификация

**Несерьёзно.** Локальный дефект написанной вручную миграции (неверная конфигурация `postgresql.ENUM.create_type`), не задевает контракты API, модель данных или архитектуру. Исправляется точечно одним параметром.

## Что сделано

Добавлен `create_type=False` в конструктор `postgresql.ENUM(...)`, чтобы `op.create_table` больше не пытался повторно создавать тип. Тип теперь создаётся/удаляется исключительно явными вызовами `photo_status_enum.create(bind, checkfirst=True)` / `photo_status_enum.drop(bind, checkfirst=True)` в `upgrade()`/`downgrade()` (оба уже присутствовали в коде и не менялись).

Diff:
```diff
 photo_status_enum = postgresql.ENUM(
-    "queued", "analyzing", "done", "error", name="photo_status"
+    "queued", "analyzing", "done", "error", name="photo_status", create_type=False
 )
```

### Консистентность с моделью

`photo-service/app/db/models.py`: `status` объявлен как `SAEnum(PhotoStatus, name="photo_status")`, где `PhotoStatus(str, enum.Enum)` имеет члены `queued/analyzing/done/error` с value == name. Значения enum совпадают со значениями в миграции 1:1, расхождений нет. Модель не участвует в создании схемы на runtime (приложение полагается на Alembic, не на `Base.metadata.create_all`), поэтому менять её не требовалось — только зафиксировано отсутствие расхождений.

## Изменённые файлы

- `photo-service/migrations/versions/v001_init_photos.py` — добавлен `create_type=False`.

## Результат повторного прогона

1. `python -m py_compile migrations/versions/v001_init_photos.py` → OK.
2. `uv run pytest -q` в `photo-service/` → **70 passed**, 1 warning (не связано с фиксом — deprecation в `pythonjsonlogger`).
3. Offline SQL-проверка (ключевая, без живой БД):
   - `uv run alembic upgrade head --sql`: `CREATE TYPE photo_status AS ENUM (...)` встречается **ровно 1 раз**.
   - `uv run alembic downgrade v001:base --sql`: `DROP TYPE photo_status;` встречается **ровно 1 раз**.

Это статически подтверждает, что дубль-`CREATE TYPE` устранён: единственный источник DDL для типа — явный `photo_status_enum.create/drop`, `create_table`/`drop_table` в тип больше не вмешиваются (эффект `create_type=False`).

## Тестовый пробел (рекомендация, не реализовано)

Ни один текущий тест не выполняет `alembic upgrade head` против реального PostgreSQL — только offline-режим (`--sql`, DDL не исполняется) и unit-тесты с моками. Такой класс багов (дубли DDL, ловящиеся только реальным движком БД) в принципе не мог быть пойман текущим набором тестов.

Рекомендация для TASK-001 (не блокирует текущий фикс, не реализовывал — требует внешней зависимости): добавить интеграционный тест, поднимающий эфемерный PostgreSQL (`testcontainers-python` или `pytest-postgresql`) и реально прогоняющий `alembic upgrade head` / `downgrade base` на чистой БД в CI.

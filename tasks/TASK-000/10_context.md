---
task_id: TASK-000
agent: context-collector
model: haiku
status: done
inputs: [specs/constitution.md, specs/feature-upload/spec.md, specs/feature-upload/tasks.md]
outputs: [tasks/TASK-000/10_context.md]
timestamp: 2026-07-11T00:00:00Z
---

# TASK-000 Контекст: Bootstrap каркаса photo-service

## Скоуп и вне-скоупа

### В скоупе TASK-000
- Инициализация структуры репозитория (каталоги слоёв: api, services, repositories, db, integrations, core).
- FastAPI приложение с минимальным конфигом (main.py, config.py).
- PostgreSQL + MinIO + docker-compose для локальной разработки.
- Эндпоинт `GET /healthz` (health check).
- Структурное логирование (JSON output) с trace_id.
- Модели SQLAlchemy для таблиц photos и analysis_results (основа для будущих TASK-001..004).
- Миграции Alembic (v001_init).
- Заглушки интеграций (integrations/storage.py, integrations/analyzer_client.py).
- .env конфигурация, .gitignore для секретов, .env.example.
- Dockerfile на python:3.12-slim, сборка через uv.

### Вне скоупа TASK-000
- **Kafka топики и консьюмеры** (photo-service будет публиковать события, но обработка — TASK-002).
- **gRPC сервисы** (описаны в constitution как опционально для внутренних путей).
- **Фактический анализ фотографий** (внешняя зависимость).
- **API для загрузки фотографий** `POST /api/v1/photos` (это TASK-001).
- **Получение результатов анализа** `GET /api/v1/photos/{id}` (TASK-004).
- **Middleware JWT аутентификации** (минимальная заглушка на будущее).
- **Circuit breaker, rate limiting** (описаны в spec, но реализуются поэтапно).

## Технологический стек (зафиксирован)

| Компонент | Выбор | Версия/примечание |
|-----------|-------|-------------------|
| **Язык** | Python | 3.12 (в Dockerfile: python:3.12-slim) |
| **HTTP Framework** | FastAPI | Встроена Pydantic v2, асинхрон |
| **ORM** | SQLAlchemy | async mode (async_engine, AsyncSession) |
| **Миграции БД** | Alembic | Инициализация v001_init.sql |
| **СУБД** | PostgreSQL | Контейнер в docker-compose |
| **Object Storage** | MinIO | S3-совместимое, контейнер в docker-compose |
| **Package Manager** | uv | Замена pip; pyproject.toml |
| **Контейнеризация** | Docker | docker-compose для локальной разработки |
| **Логирование** | Python logging (JSON) | Структурные логи с trace_id |

## Требования к архитектуре и слоям

Из задания и constitution.md зафиксированы требования к слоистой архитектуре:

1. **api/** (HTTP-эндпоинты)
   - Функция: маршрутизация запросов, валидация через Pydantic, формирование ответов.
   - Ограничения: **НЕ содержит SQL**, не обращается к MinIO напрямую, не вызывает БД непосредственно.
   - Пример: `api/photos.py` — хендлеры `POST /api/v1/photos`, `GET /healthz`.

2. **services/** (бизнес-логика)
   - Функция: оркестрирует repositories и integrations, реализует правила домена.
   - Пример: `services/photo_service.py` — валидация файла, координация сохранения в MinIO + запись в БД + публикация в Kafka.

3. **repositories/** (доступ к БД)
   - Функция: SQL-запросы, работа с ORM, трансакции.
   - Пример: `repositories/photo_repository.py` — insert/select/update в таблицу photos.

4. **db/** (модели и сессии)
   - Функция: модели SQLAlchemy, создание engine, управление AsyncSession.
   - Файлы: `db/models.py` (Photo, AnalysisResult), `db/session.py` (engine, SessionLocal).

5. **integrations/** (внешние системы)
   - Функция: wrapper'ы для MinIO, Kafka, external analyzer.
   - Файлы: `integrations/storage.py` (upload_to_minio), `integrations/analyzer_client.py` (stub).

6. **core/** (кросс-слойные concerns)
   - Функция: config, error models, logging.
   - Файлы: `core/config.py` (Settings), `core/errors.py` (AppError), `core/logging.py` (json_formatter).

## Требования к хранению и конфигурации

### MinIO
- **Путь объекта:** `photos/{user_id}/{photo_id}.jpg/png` (расширение соответствует MIME-типу).
- **Bucket:** переменная окружения `MINIO_BUCKET` (по умолчанию? или обязательно?).
- **Лимиты файла:** макс 50 МБ, только JPEG/PNG по magic bytes.
- **Доступ:** через переменные окружения `MINIO_ENDPOINT`, `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY`.

### PostgreSQL
- **Подключение:** через `DATABASE_URL` (e.g., `postgresql+asyncpg://user:pass@localhost/photo_db`).
- **Таблицы (базовые модели):**
  - `photos` (для TASK-001): photo_id (UUID, PK), user_id, s3_path, uploaded_at (timestamp UTC), status (enum: queued/analyzing/done/error).
  - `analysis_results` (для TASK-003): photo_id (FK), blur_score, face_count, duplicate_id, quality_issues (JSON), analyzed_at.
  - Индексы: (user_id, uploaded_at), (photo_id) на photos.
- **Миграции:** Alembic инициализация, v001 создаёт таблицы и индексы.

### Конфигурация (только env/.env)
**Обязательные переменные:**
- `DATABASE_URL` — PostgreSQL connection string.
- `MINIO_ENDPOINT` — e.g., `localhost:9000`.
- `MINIO_ACCESS_KEY` — access key для MinIO.
- `MINIO_SECRET_KEY` — secret key для MinIO.
- `MINIO_BUCKET` — имя bucket'а.
- `LOG_LEVEL` — DEBUG/INFO/WARN/ERROR/FATAL (по умолчанию INFO).

**Опционально:**
- `API_HOST`, `API_PORT` (по умолчанию 0.0.0.0:8000?).
- `WORKERS` (число worker'ов для uvicorn).

**Ограничения:**
- Секреты (keys, DATABASE_URL) **НЕ коммитятся**: `.env` в `.gitignore`.
- Создать `.env.example` с плейсхолдерами.
- CI/CD и Docker Compose должны использовать `.env` или переменные окружения контейнера.

### Структурное логирование
- **Формат:** JSON (не текстовые логи).
- **Обязательные поля:** timestamp, level, service (photo-service), trace_id, span_id (опционально), message, error_code (при ошибке).
- **trace_id:** уникальный идентификатор запроса (UUID), пробрасывается во все операции.
- **Ротация:** 1 ГБ или 1 день (конфиг Python logging).

## Целевая структура файлов (из задания)

```
photo-service/
├── app/
│   ├── __init__.py
│   ├── main.py                    # Entry point, FastAPI app
│   ├── api/
│   │   ├── __init__.py
│   │   └── photos.py              # Router для photos (GET /healthz и др.)
│   ├── schemas/
│   │   ├── __init__.py
│   │   └── photos.py              # Pydantic models (input/output)
│   ├── services/
│   │   ├── __init__.py
│   │   └── photo_service.py       # Бизнес-логика
│   ├── repositories/
│   │   ├── __init__.py
│   │   └── photo_repository.py    # Доступ к БД
│   ├── db/
│   │   ├── __init__.py
│   │   ├── models.py              # SQLAlchemy models (Photo, AnalysisResult)
│   │   └── session.py             # engine, SessionLocal, dependency
│   ├── integrations/
│   │   ├── __init__.py
│   │   ├── storage.py             # MinIO wrapper
│   │   └── analyzer_client.py     # Stub (заглушка на будущее)
│   └── core/
│       ├── __init__.py
│       ├── config.py              # Settings (Pydantic model)
│       ├── errors.py              # AppError, custom exceptions
│       └── logging.py             # JSON formatter, setup_logging()
│
├── migrations/
│   ├── env.py                     # Alembic env
│   ├── script.py.mako
│   └── versions/
│       └── v001_init.py           # Initial schema (photos, analysis_results)
│
├── tests/
│   ├── __init__.py
│   ├── conftest.py
│   ├── unit/
│   │   └── test_*.py
│   └── integration/
│       └── test_*.py
│
├── Dockerfile                     # python:3.12-slim + uv sync
├── docker-compose.yml             # api, postgres, minio сервисы
├── .env.example                   # шаблон конфига
├── .gitignore                     # .env, __pycache__, etc.
├── pyproject.toml                 # зависимости, метаданные
└── README.md                      # документация
```

## Текущее состояние репозитория (факты)

Сканирование показывает:
- **Корень:** только CLAUDE.md, SETUP-CLAUDE-CODE.md, main.py (пустой, 1 строка), specs/ (constitution, feature-upload/), .claude/agents/ (инструкции для субагентов), tasks/TASK-000/00_orchestration.md.
- **Отсутствует:** каталог photo-service/ (целиком), pyproject.toml, requirements.txt, Dockerfile, docker-compose.yml, .env/.env.example, migrации.
- **Git статус:** ветка main, есть коммиты с recent commit `aea474d setup multiagent`.
- **Заключение:** репозиторий инициализирован только мультиагентной обвязкой; backend-каркас подлежит созданию с нуля в TASK-000.

## Релевантные ограничения из constitution.md

1. **Микросервисная архитектура:**
   - Constitution описывает многосервисную структуру: API Gateway → Photo Service, Result Service, Analyzer Service.
   - **РАСХОЖДЕНИЕ:** TASK-000 требует единый `photo-service/app/` (вертикальный срез), не отдельные микросервисы.
   - Решение: photo-service в TASK-000 интегрирует API + основная логика; маршрутизация/gRPC и Result Service — будущие задачи.

2. **Kafka топики (для TASK-001+):**
   - `photos-to-analyze`: ключ photo_id, значение JSON { photo_id, s3_path, user_id, uploaded_at, trace_id }.
   - Retention: 7 дней. Replicas: 3, min_insync_replicas: 2.
   - **В TASK-000:** конфиг и заглушки для публикации, но сама очередь не поднимается (вне docker-compose).

3. **Health checks:**
   - `GET /health` — liveness (сервис поднялся).
   - `GET /ready` — readiness (БД доступна, Kafka доступна).
   - **В TASK-000:** реализовать `/healthz` (объединённый health check для simple start); `/health` и `/ready` — будущие детали.

4. **Таймауты:**
   - HTTP: 30s по умолчанию, MinIO: 60s.
   - БД: 5s на один запрос.
   - Kafka: 10s publish, 30s consume.
   - **В TASK-000:** зафиксировать в config.py, не реализовать в полном объёме.

5. **Логирование:**
   - Структурные логи (JSON) с trace_id.
   - Уровни: DEBUG, INFO, WARN, ERROR, FATAL.
   - **В TASK-000:** настроить JSON formatter и trace_id injection в middleware (заглушка).

6. **Безопасность:**
   - Валидация MIME-type (magic bytes), размер макс 50 МБ.
   - JWT Bearer token (опционально на начальном этапе, но архитектура должна поддерживать).
   - Не логировать пути MinIO, user_id в plain text.
   - **В TASK-000:** placeholder для JWT middleware, основная валидация логики закладывается.

7. **Таблицы БД:**
   - `photos`: photo_id, user_id, s3_path, uploaded_at, status.
   - `analysis_results`: photo_id (FK), blur_score, face_count, duplicate_id, quality_issues.
   - Индексы на (user_id, uploaded_at), (photo_id).
   - **В TASK-000:** модели SQLAlchemy закладываются в db/models.py и миграция v001.

8. **Отказоустойчивость:**
   - Circuit breaker для MinIO, Kafka.
   - Graceful shutdown (15s).
   - **В TASK-000:** основная структура, детали — позже.

## Открытые вопросы / расхождения

1. **Микросервисная архитектура vs. монолит на старт:**
   - Constitution описывает api-gateway/photo-service/result-service как разные контейнеры.
   - TASK-000 требует единый photo-service/app/main.py.
   - **Факт:** это вертикальный срез для спринта 2; gateway и result-service появятся в TASK-002+ (не уточнено в specs/feature-upload/tasks.md).

2. **Обязательность Kafka в TASK-000:**
   - Spec и constitution упоминают `photos-to-analyze` топик.
   - TASK-000 исключает Kafka из docker-compose.
   - **Факт:** заглушка для интеграции (код готов к публикации), но сама queue не поднимается. Решение — conditional flag или mock.

3. **Аутентификация JWT:**
   - Constitution требует Bearer token, spec предполагает user_id из токена.
   - TASK-000 не уточняет реализацию middleware.
   - **Факт:** архитектура должна поддерживать injection user_id; реальная валидация JWT — будущая задача.

4. **Версия FastAPI/Pydantic:**
   - Задание указывает "Pydantic v2", но конкретных версий в pyproject.toml нет.
   - **Факт:** использовать последние стабильные версии (FastAPI 0.100+, Pydantic 2.x).

5. **Rate limiting и per-user лимиты:**
   - Spec описывает "макс 100 одновременных uploads per user".
   - TASK-000 не включает middleware для ограничения.
   - **Факт:** заглушка в архитектуре, реализация — после bootstrap.

6. **HTTP vs. gRPC для internal сервисов:**
   - Constitution упоминает gRPC как опционально для высоконагруженных путей.
   - TASK-000 использует только HTTP.
   - **Факт:** расширяемость — REST в photo-service/app/api, gRPC добавится при необходимости.

7. **Кэширование (Redis):**
   - Constitution упоминает Redis для кэширования результатов анализа.
   - TASK-000 не упоминает Redis.
   - **Факт:** вне скоупа bootstrap; рассмотрится в TASK-003+.

## Ключевые точки для architecture/design (TASK-000)

- **config.py:** всё из env, структурированный Settings (Pydantic model).
- **main.py:** создание FastAPI app, инициализация БД engine, setup логирования, регистрация маршрутов.
- **db/session.py:** async engine, AsyncSession factory, dependency для FastAPI.
- **db/models.py:** Photo и AnalysisResult моделью с нужными полями и индексами.
- **api/photos.py:** router с `GET /healthz`.
- **core/logging.py:** JSON formatter + trace_id injection middleware.
- **integrations/storage.py:** placeholder для MinIO (реализация — TASK-001).
- **Dockerfile и docker-compose:** postgres и minio сервисы, volume'ы для данных.
- **Миграции:** v001_init.py создаёт схему.
- **.env.example:** шаблон для конфига.

---

## Резюме контекста

TASK-000 инициализирует вертикальный срез backend-сервиса анализа фотографий: от HTTP-эндпоинта до PostgreSQL и MinIO. Текущий репозиторий содержит только спецификации и мультиагентную обвязку; backend-каркас подлежит созданию с нуля. Стек: Python 3.12, FastAPI, Pydantic v2, SQLAlchemy async, Alembic, PostgreSQL, MinIO, Docker Compose, uv.

Архитектура строго слоистая: api (маршруты) → services (бизнес-логика) → repositories (БД) → db (модели, engine). Конфигурация только через env/.env (DATABASE_URL, MINIO_*, LOG_LEVEL), секреты не коммитятся. Логирование структурированное (JSON с trace_id). Модели SQLAlchemy для photos и analysis_results закладываются в db/models.py. Docker-compose поднимает api + postgres + minio (Kafka вне скоупа). Сервис отвечает на GET /healthz.

Ключевые расхождения: Constitution описывает многосервисную архитектуру (api-gateway/photo-service/result-service), но TASK-000 требует единый photo-service/app — это временный срез для спринта 2. Kafka и gRPC исключены из bootstrap'а, хотя архитектура их предусматривает. Аутентификация JWT и rate limiting — заглушки для будущих задач.

# photo-service

Backend-сервис анализа фотографий: вертикальный каркас Client -> API -> PostgreSQL/MinIO.
Спринт 2 (TASK-000): рабочий скелет без бизнес-логики загрузки.

## Запуск

```bash
cd photo-service
docker compose up --build
```

Поднимутся `postgres`, `minio` и `api`. При старте `api` контейнер сначала
применяет миграции Alembic (`alembic upgrade head`), затем запускает
uvicorn.

## Проверка

```bash
curl -i http://localhost:8000/healthz    # 200 {"status":"ok","service":"photo-service"}
curl -i http://localhost:8000/readyz     # 200 если БД и MinIO доступны, иначе 503
```

MinIO console: http://localhost:9001 (minioadmin/minioadmin), бакет `photos`
создаётся автоматически при старте.

## Переменные окружения

См. `.env.example`. Для локального запуска вне Docker Compose скопируйте
файл в `.env` и подставьте свои значения (`.env` в `.gitignore`, секреты
никогда не коммитятся).

## Локальная разработка без Docker

```bash
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
uv run pytest
```

## Структура

```
app/
  api/            HTTP-роутеры (валидация, коды ответов; без SQL/MinIO)
  services/       бизнес-логика (оркестрация repositories + integrations)
  repositories/   доступ к БД (SQL/ORM в рамках переданной сессии)
  db/             модели SQLAlchemy, engine, sessionmaker
  integrations/   внешние системы (MinIO, будущий Kafka/analyzer)
  core/           конфиг, ошибки, логирование (поперечные модули)
  schemas/        Pydantic DTO вход/выход API
migrations/       Alembic (async env.py + versions/)
tests/            pytest (httpx ASGI client)
```

Правило зависимостей: `api -> services -> repositories -> db`; `core`/`integrations`/`schemas` — поперечные, доступны всем слоям, сами не зависят от вышестоящих.

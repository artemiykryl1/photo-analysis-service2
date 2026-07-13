# Constitution проекта — Сервис анализа фотографий

**Версия:** 1.0  
**Дата:** 2024-07-01  
**Статус:** Актуально

---

## 1. Видение

Разработать **масштабируемый, отказоустойчивый backend** для облачного хранилища фотографий с асинхронным анализом изображений. Система похожа на облако Mail.ru, но без клиента — чистый бэкенд с микросервисной архитектурой.

**Не в скоупе:**
- Разработка моделей анализа фотографий (берутся как external service).
- Веб-интерфейс или мобильное приложение (опционально в конце).

---

## 2. Архитектурные принципы

### 2.1 Микросервисная архитектура

- **API Gateway** — единая точка входа (HTTP).
- **Photo Service** — управление фотографиями, MinIO, БД.
- **Analyzer Service** — асинхронная обработка через Kafka (external dependency).
- **Result Service** — хранение результатов анализа.
- **Notification Service** — опционально: уведомления о готовности.

Границы ясны; каждый сервис отвечает за свой домен.

### 2.2 Взаимодействие между сервисами

**HTTP/REST:**
- API Gateway → Photo Service, Result Service (синхрон запросы).
- Клиент → API Gateway (синхрон).

**gRPC (опционально для высоконагруженных путей):**
- Внутри сервисов для быстрого обмена метаданными (если нужна низкая latency).

**Kafka (асинхрон):**
- Photo Service публикует события: `photos-to-analyze` (когда загружена фотография).
- Analyzer Service подписывается, обрабатывает, публикует `analysis-results`.
- Result Service слушает `analysis-results` и сохраняет в БД.

### 2.3 Хранение данных

- **СУБД** (PostgreSQL):
  - `photos` таблица (photo_id, user_id, s3_path, uploaded_at, status).
  - `analysis_results` таблица (photo_id, blur_score, face_count, duplicate_id, quality_issues).
  - Индексы на (user_id, uploaded_at), (photo_id).
  
- **MinIO (S3-совместимое объектное хранилище)**:
  - Путь: `photos/{user_id}/{photo_id}.jpg`.
  - Лимиты: не более 50 МБ, только JPEG/PNG.

- **Кэш (Redis опционально)**: результаты анализа для быстрого доступа.

### 2.4 Асинхронная обработка через Kafka

**Топики:**
- `photos-to-analyze` — новые фотографии.
  - Ключ: `photo_id` (партиционирование по фото).
  - Значение: JSON `{ photo_id, s3_path, user_id, uploaded_at, trace_id }`.
  - Retention: 7 дней.
  - Replicas: 3; min_insync_replicas: 2.

- `analysis-results` — готовые результаты.
  - Ключ: `photo_id`.
  - Значение: JSON `{ photo_id, blur_score, face_count, duplicate_id, quality_issues, analyzed_at, trace_id }`.
  - Retention: 30 дней.

- `analysis-dlq` — мёртвые письма (failed messages).
  - Сообщение попадает сюда после 3 ретраев.

**Гарантии:**
- `acks=all` —写入топик только при подтверждении всех replicas.
- Идемпотентность консьюмера: сохраняем `offset` в БД, не обновляем результат дважды.
- Ретраи: exponential backoff (1s, 2s, 4s), max 3 раза.
- DLQ: неудачные сообщения в отдельный топик с логированием.

---

## 3. Нефункциональные требования

### 3.1 Наблюдаемость

**Логирование:**
- Структурные логи (JSON): `timestamp`, `level`, `service`, `trace_id`, `span_id`, `message`, `error_code`.
- Все запросы имеют `trace_id` (генерируется в API Gateway, пробрасывается во все сервисы).
- Уровни: DEBUG, INFO, WARN, ERROR, FATAL.
- Лог-ротация: 1 ГБ или 1 день.

**Метрики (Prometheus/OpenTelemetry):**
- RED (Request/Error/Duration): для HTTP-эндпоинтов.
- USE (Utilization/Saturation/Errors): для ресурсов (CPU, mem, disk, Kafka lag).
- Кастомные: `upload_duration_seconds`, `kafka_publish_latency_ms`, `minio_operation_duration_seconds`.

**Трассировка (Jaeger):**
- Все сервисы отправляют spans в Jaeger.
- Трассировка по `trace_id`.

**Health checks:**
- `GET /health` — liveness (сервис поднялся).
- `GET /ready` — readiness (БД доступна, Kafka доступна).

### 3.2 Отказоустойчивость

**Таймауты:**
- HTTP запросы: 30s (по умолчанию); для MinIO — 60s.
- Kafka: 10s на publish, 30s на consume.
- БД: 5s на один запрос.

**Circuit Breaker:**
- Если MinIO недоступен → возвращаем 503 (Unavailable), а не 500.
- Если Kafka недоступна → возвращаем 503, но файл сохранён в локальной очереди (для ретрая).

**Graceful Shutdown:**
- 15s на завершение in-flight запросов.
- Закрываем Kafka консьюмер (commit offset), затем БД, затем HTTP сервер.

**Ретраи:**
- Kafka publish: 3 раза с exponential backoff.
- HTTP запросы между сервисами: 2 раза.
- БД транзакции: не ретрим, поднимаем ошибку.

### 3.3 Безопасность

**Входные данные:**
- Валидация типа загруженного файла (MIME-type по magic bytes, не только расширению).
- Валидация размера (макс 50 МБ).
- Санитизация пути MinIO (нет `../`, `//`).

**Авторизация:**
- API требует Bearer token (JWT).
- Пользователь видит только свои фотографии.
- Нет хардкода секретов в коде; используются переменные окружения или vault.

**Логирование:**
- Не логируем целиком пути MinIO, ID пользователей в plain text.
- Не логируем содержимое файлов.

### 3.4 Производительность

**Лимиты:**
- Per-user: макс 100 одновременных uploads.
- Per-instance: макс 1000 RPS на фото-сервис.
- Размер сообщения Kafka: макс 10 МБ.

**Оптимизация:**
- Параллельная загрузка в MinIO (multipart upload для больших файлов).
- Connection pooling для БД (макс 50 connection в пуле).
- Кэширование результатов анализа (Redis, TTL 1 день).

---

## 4. Технологический стек

| Компонент | Выбор | Обоснование |
|-----------|-------|-------------|
| **Язык backend** | Python | Быстрая разработка, богатая экосистема (FastAPI, asyncio), достаточна производительность для I/O-bound сервиса. |
| **Framework HTTP** | FastAPI | Асинхронный, быстрый, встроена валидация (Pydantic), автогенерация OpenAPI. |
| **СУБД** | PostgreSQL | ACID, надёжна, поддержка сложных запросов. |
| **Кэш** | Redis | Быстрый KV store для результатов. |
| **Message queue** | Apache Kafka | Асинхрон обработка, масштабируемость, гарантии доставки. |
| **Object storage** | MinIO | S3-совместимое объектное хранилище, self-hosted, без зависимости от AWS. |
| **Логирование** | ELK или Loki + Grafana | Структурные логи, полнотекстовый поиск. |
| **Метрики** | Prometheus | Стандарт в K8s. |
| **Трассировка** | Jaeger | Open-source, хорошая интеграция. |
| **Container** | Docker | Стандарт; образы минимальные (alpine). |
| **Оркестрация** | Kubernetes | Автоскейлинг, self-healing, сетевые политики. |
| **CI/CD** | GitHub Actions или GitLab CI | Встроены, просто настраиваются. |

---

## 5. Гейты и процесс разработки

### 5.1 SDD (Spec-Driven Development)

Каждая фича идёт через фазы:
1. **Specify** (`spec.md`): что строим, критерии приёмки.
2. **Plan** (`plan.md`): как строим (архитектура, контракты).
3. **Tasks** (`tasks.md`): задачи в трекере.
4. **Implement** (`*.md` артефакты на каждый шаг).

### 5.2 Ревью и гейты

- **Кодер** → код.
- **Ревьювер_1 (Opus)** → проверка корректности и архитектуры → APPROVE или CHANGES_REQUESTED.
- **Ревьювер_2 (Haiku)** → проверка безопасности, конвенций → APPROVE или CHANGES_REQUESTED.
- **Оба** дали APPROVE → идём на тесты.
- **Тесты зелёные** → готовы к PR.
- **Пользователь дал разрешение** → публикуем PR/MR.

### 5.3 Документация

- API docs (OpenAPI/Swagger).
- Архитектурные диаграммы в ВК Доске (C4, sequence).
- Руководство по развёртыванию (K8s, конфиг, миграции).
- README в репозитории.

---

## 6. Структура репозитория

```
photo-analysis-service/
├── .github/workflows/           # CI/CD (GitHub Actions)
├── api-gateway/
│   ├── main.py
│   ├── handler/
│   │   ├── upload.py            # POST /api/v1/photos
│   │   ├── status.py            # GET /api/v1/photos/{id}
│   │   └── list.py              # GET /api/v1/photos (with pagination)
│   ├── middleware/
│   │   ├── auth.py              # JWT validation
│   │   ├── logging.py           # trace_id injection
│   │   └── error.py             # error formatting
│   └── Dockerfile
├── photo-service/
│   ├── main.py
│   ├── service/
│   │   ├── photo.py             # бизнес-логика
│   │   ├── minio_uploader.py    # MinIO integration
│   │   └── kafka_producer.py    # publish events
│   ├── repository/
│   │   └── photo_repo.py        # DB queries
│   ├── models/
│   │   └── photo.py
│   └── Dockerfile
├── result-service/
│   ├── main.py
│   ├── service/
│   │   └── result.py            # сохранение результатов
│   ├── models/
│   │   └── result.py
│   └── Dockerfile
├── migrations/
│   ├── v001_init.sql
│   ├── v002_add_indexes.sql
│   └── ...
├── config/
│   ├── docker-compose.yml       # локальная разработка
│   ├── kubernetes/              # K8s manifests
│   │   ├── namespace.yml
│   │   ├── api-gateway.yml
│   │   ├── photo-service.yml
│   │   ├── result-service.yml
│   │   ├── postgres.yml
│   │   ├── kafka.yml
│   │   └── ...
│   └── .env.example
├── docs/
│   ├── API.md
│   ├── ARCHITECTURE.md
│   ├── DEPLOYMENT.md
│   └── diagrams/
│       ├── services.png
│       ├── sequence.png
│       └── data-flow.png
├── tests/
│   ├── integration/
│   │   ├── test_upload.py
│   │   └── test_analysis.py
│   └── contract/              # consumer-driven contract tests
├── specs/
│   ├── constitution.md        # этот файл
│   ├── feature-upload/
│   │   ├── spec.md
│   │   ├── plan.md
│   │   └── tasks.md
│   ├── feature-analysis/
│   │   ├── spec.md
│   │   ├── plan.md
│   │   └── tasks.md
│   └── ...
├── tasks/
│   ├── TASK-001/
│   │   ├── 00_orchestration.md
│   │   ├── 10_context.md
│   │   ├── 20_design.md
│   │   ├── 30_impl.md
│   │   ├── 40_review-1.md
│   │   ├── 41_review-2.md
│   │   ├── 50_tests.md
│   │   ├── 60_debug.md        # только если были баги
│   │   ├── 70_pr.md
│   │   ├── 80_docs.md
│   │   └── diagrams/
│   ├── TASK-002/
│   └── ...
├── README.md
└── pyproject.toml, requirements.txt
```

---

## 7. Контрольный список на начало

Перед первой таской проверьте:

- [ ] Репозиторий создан на GitHub/GitLab.
- [ ] БД инициализирована (миграция v001).
- [ ] Kafka локально поднята (docker-compose).
- [ ] MinIO доступен.
- [ ] CI/CD pipeline сконфигурирован (запуск тестов, build).
- [ ] Трекер (Jira/Trello) подготовлен для задач.
- [ ] ВК Доска создана для диаграмм.
- [ ] Логирование (stdout в JSON) настроено.
- [ ] Документация API (Swagger/OpenAPI) инициирована.

---

## 8. Регулярные check-ups

**Еженедельно:**
- Ревью метрик: latency, error rate, Kafka lag.
- Проверка лога на FATAL.

**Ежемесячно:**
- Ревью document аccess patterns; нужны ли индексы?
- Проверка MinIO/БД usage.

**Перед release:**
- Load testing (150% expected peak).
- Security audit (inputs, secrets, auth).
- Disaster recovery drill (восстановление из бэкапа).

---

## 9. Версионирование

- **API**: семантическое (v1, v2, ...). Breaking change → новая версия.
- **DB migrations**: последовательные номера (v001, v002, ...).
- **Kubernetes manifests**: теги образов по git commit hash.

---

**Подписи:**
- **Архитектор:** [имя]
- **Лид разработки:** [имя]
- **Дата принятия:** 2024-07-01

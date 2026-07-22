# photo-service

Backend-сервис анализа фотографий: асинхронный конвейер API -> Kafka ->
worker -> реальный gRPC-анализатор -> PostgreSQL/MinIO, батчи с выбором
лучшего кадра, веб-интерфейс, Prometheus/Grafana, развёртывание в
Kubernetes (локальный кластер).

TASK-003 (`tasks/TASK-003/20_design.md`): заглушка анализатора заменена на
подключение к реальному общему сервису (`45.132.19.101:50051`), добавлены
подготовка изображения перед отправкой (лимит сообщения gRPC — 4 МиБ,
раздел §2 дизайна) и новая формула выбора лучшего кадра батча (резкость по
`blur_score`, а не по сломанному `is_blurred` реального анализатора,
раздел §3 дизайна).

## Запуск в Kubernetes локально (демонстрация проекта)

> Полное описание манифестов, ConfigMap/Secret, миграций и валидации — в
> `docs/DEPLOYMENT.md`. Здесь — рабочая последовательность команд.

### Предусловия

- Установлен Docker (Docker Desktop на Windows) с работающим движком/WSL2.
- Установлены `kind` и `kubectl` (оба — один статический бинарник, без
  админ-прав):
  ```powershell
  # kind (см. актуальную ссылку на https://kind.sigs.k8s.io/docs/user/quick-start/)
  curl.exe -Lo kind-windows-amd64.exe https://kind.sigs.k8s.io/dl/latest/kind-windows-amd64
  move kind-windows-amd64.exe C:\Users\<you>\bin\kind.exe

  # kubectl (см. актуальную ссылку на https://kubernetes.io/docs/tasks/tools/)
  curl.exe -LO "https://dl.k8s.io/release/v1.31.0/bin/windows/amd64/kubectl.exe"
  move kubectl.exe C:\Users\<you>\bin\kubectl.exe
  ```
  Добавьте выбранную папку (`C:\Users\<you>\bin` в примере выше) в `PATH`.
- **Лимит памяти Docker Desktop поднят минимум до ~6 ГБ.** Суммарные
  `requests` всех подов — около 1.3 CPU / 3.4 ГБ (таблица ресурсов в
  дизайне §9.8); если поды зависают в `Pending` с `Insufficient memory` —
  это первое, что нужно проверить.

### Последовательность (всё — из `photo-service/`, PowerShell)

```powershell
# 1. Создать локальный кластер.
kind create cluster --name photo --config k8s/kind-cluster.yaml

# 2. Собрать образы.
docker build -t photo-service:local .
docker build -t photo-web:local ./web

# 3. Загрузить образы в узлы кластера (без внешнего registry).
kind load docker-image photo-service:local --name photo
kind load docker-image photo-web:local     --name photo

# 4. Namespace, конфиг и секреты (dev-значения для локального демо).
kubectl apply -f k8s/00-namespace.yaml
kubectl apply -f k8s/10-configmap.yaml
kubectl apply -f k8s/11-secret.yaml

# 5. Инфраструктура (StatefulSet-ы с постоянными томами).
kubectl apply -f k8s/20-postgres.yaml -f k8s/21-minio.yaml -f k8s/22-kafka.yaml
kubectl -n photo-service rollout status statefulset/postgres --timeout=180s

# 6. Миграции (Job) — один прогон, до api/worker.
kubectl apply -f k8s/30-migrate-job.yaml
kubectl -n photo-service wait --for=condition=complete job/alembic-upgrade --timeout=180s

# 7. Приложение и наблюдаемость.
kubectl apply -f k8s/31-api.yaml -f k8s/32-worker.yaml -f k8s/42-web.yaml
kubectl apply -f k8s/40-prometheus.yaml -f k8s/41-grafana.yaml
kubectl -n photo-service rollout status deploy/api deploy/worker deploy/web

# 8. Доступ к UI (в отдельных терминалах, оставить запущенными;
#    либо одной командой: pwsh k8s/port-forward.ps1 — см. ниже).
kubectl -n photo-service port-forward svc/web     8080:80
kubectl -n photo-service port-forward svc/api     8000:8000
kubectl -n photo-service port-forward svc/grafana 3000:3000
```

Шаги 4-7 можно свернуть в одну команду, но **не** `kubectl apply -f k8s/`:
в этой папке лежат `kind-cluster.yaml` (конфиг `kind`, а не манифест
Kubernetes) и `port-forward.ps1`, поэтому такая команда завершится ошибкой
`no matches for kind "Cluster"`. Применяйте только пронумерованные файлы:

```powershell
Get-ChildItem k8s\[0-9]*.yaml | Sort-Object Name | ForEach-Object { kubectl apply -f $_.FullName }
```

Числовые префиксы задают порядок применения, но порядок между Job миграций
и api/worker в любом случае гарантирует initContainer `wait-for-schema`
(см. `docs/DEPLOYMENT.md`), а не порядок `apply`.

### Открыть в браузере

- Веб-интерфейс: http://localhost:8080
- API/Swagger: http://localhost:8000/docs
- Grafana: http://localhost:3000 (логин `admin`, пароль — из Secret
  `photo-secrets`, dev-значение `admin_dev_only`)

Оба port-forward (`web` и `api`) должны быть запущены одновременно —
фронт обращается к API по `http://localhost:8000`, а CORS на API разрешён
именно для `http://localhost:8080`.

Свернуть все 4 port-forward в один скрипт (опционально, не обязательный
шаг):

```powershell
pwsh k8s/port-forward.ps1
```

**Если скрипт не запускается.** На чистой Windows он, скорее всего, не
запустится: `pwsh` может отсутствовать (PowerShell 7 ставится отдельно), а
Windows PowerShell 5.1 откажется выполнять неподписанный файл с ошибкой
`UnauthorizedAccess` / `about_Execution_Policies`. Скрипт **не обязателен** —
просто откройте по окну на каждый проброс и оставьте их запущенными:

```powershell
kubectl -n photo-service port-forward svc/web        8080:80
kubectl -n photo-service port-forward svc/api        8000:8000
kubectl -n photo-service port-forward svc/grafana    3000:3000
kubectl -n photo-service port-forward svc/prometheus 9090:9090
```

Каждое окно должно вывести `Forwarding from 127.0.0.1:<порт> -> ...` и
остаться открытым: `kubectl port-forward` живёт ровно столько, сколько
открыто его окно. Туннель также может оборваться сам — при перезапуске
пода или после долгого простоя; тогда достаточно повторить команду.
Для демонстрации обязательны только `web` (8080) и `api` (8000).

### Логи

```powershell
kubectl -n photo-service logs -f deploy/worker
kubectl -n photo-service logs -f deploy/api
kubectl -n photo-service logs job/alembic-upgrade
```

### Пересборка после правок кода

Тег образов фиксированный (`:local`) — `kind load` обновляет слои в узлах,
но НЕ перезапускает уже работающие поды сам по себе:

```powershell
docker build -t photo-service:local .
kind load docker-image photo-service:local --name photo
kubectl -n photo-service rollout restart deploy/api deploy/worker
```

(аналогично для `photo-web:local` + `deploy/web`, если менялся `web/`).

### Снос

```powershell
kind delete cluster --name photo   # убирает кластер, поды, PVC — всё
```

### Сервер Sirius

Сервер `45.132.19.101` используется только как хост внешнего общего
анализатора (`ANALYZER_GRPC_ADDR` в `k8s/10-configmap.yaml`); SSH-доступ,
диапазон портов 51100-51119 и деплой на сам сервер — вне скоупа. SSH-ключи
(`*_sirius`, `*.pem`) не коммитятся (см. `.gitignore`).

## Запуск через docker compose (разработка и тесты)

```bash
cd photo-service
docker compose up --build
```

Поднимутся `postgres`, `minio`, `kafka`, `analyzer-stub` (заглушка,
детерминированные результаты по хэшу `object_key`), `api`, `worker`,
`prometheus`, `grafana` и `web`. Веб-интерфейс — http://localhost:8080,
API — http://localhost:8000. Заглушка анализатора поднимается
автоматически и остаётся дефолтом для локальной разработки и тестов —
переключение на реальный анализатор делается одной переменной
`ANALYZER_GRPC_ADDR`.

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

# Развёртывание в Kubernetes (локальный кластер)

> TASK-003, блоки C/D (`tasks/TASK-003/20_design.md` §9-10). Демонстрация
> проекта проводится в ЛОКАЛЬНОМ кластере на машине разработчика (Windows +
> Docker) — развёртывание на учебном сервере Sirius из скоупа исключено;
> сервер используется только как хост внешнего анализатора
> (`45.132.19.101:50051`).

## Состав манифестов (`photo-service/k8s/`)

Файлы применяются в порядке, заданном числовыми префиксами.

| Файл | Содержимое |
|---|---|
| `kind-cluster.yaml` | конфиг `kind` (вход для `kind create cluster --config`, НЕ применяется через `kubectl apply`) |
| `00-namespace.yaml` | `Namespace: photo-service` |
| `10-configmap.yaml` | `ConfigMap photo-config` — нечувствительные настройки, включая внешний `ANALYZER_GRPC_ADDR` |
| `11-secret.yaml` | `Secret photo-secrets` — dev-плейсхолдеры кредов Postgres/MinIO/Grafana |
| `20-postgres.yaml` | `StatefulSet(1)` + headless `Service` + PVC 5Gi |
| `21-minio.yaml` | `StatefulSet(1)` + `Service` (9000/9001) + PVC 10Gi |
| `22-kafka.yaml` | `StatefulSet(1, KRaft)` + headless `Service` + PVC 5Gi, `KAFKA_NUM_PARTITIONS=3` |
| `30-migrate-job.yaml` | `Job alembic-upgrade`, `backoffLimit: 10`, `ttlSecondsAfterFinished: 600`, initContainer `wait-for-postgres` |
| `31-api.yaml` | `Deployment(2)` + `Service` ClusterIP 8000, initContainer `wait-for-schema` |
| `32-worker.yaml` | `Deployment(2)` + `Service` ClusterIP 8001 (для scrape), initContainer `wait-for-schema` |
| `40-prometheus.yaml` | `ConfigMap` (scrape-конфиг + алерты) + `Deployment(1)` + `Service` |
| `41-grafana.yaml` | `ConfigMap` (provisioning + дашборд) + `Deployment(1)` + `Service` |
| `42-web.yaml` | `Deployment(2)` + `Service` ClusterIP 80 |
| `port-forward.ps1` | опциональный скрипт, поднимающий все 4 port-forward сразу (не обязателен) |

**Чего в этой директории нет и почему:**
- **`analyzer-stub`** — анализатор в кластере внешний (`ANALYZER_GRPC_ADDR` в ConfigMap указывает на `45.132.19.101:50051`); заглушка остаётся только в `docker-compose.yml` для локальной разработки и тестов.
- **`Ingress`/`NodePort`** — доступ к UI/API/Grafana решён через `kubectl port-forward` (см. ниже), что не требует ingress-контроллера и не резервирует порты хоста в конфиге кластера.

## ConfigMap против Secret

**ConfigMap `photo-config`** — всё, что не является секретом: имена хостов сервисов кластера (`postgres`, `minio`, `kafka`, `api`, `worker`), таймауты, лимиты подготовки изображения, порт метрик worker'а, и, что важнее всего, **`ANALYZER_GRPC_ADDR: "45.132.19.101:50051"`** — адрес внешнего реального анализатора.

**Secret `photo-secrets`** — всё, что содержит креды: `DATABASE_URL` (целиком, с паролем), `POSTGRES_USER`/`PASSWORD`/`DB`, `MINIO_ROOT_USER`/`PASSWORD`, `MINIO_ACCESS_KEY`/`SECRET_KEY`, `GF_SECURITY_ADMIN_PASSWORD`.

Оба подключены в `api`/`worker`/`migrate-job` через `envFrom: [configMapRef, secretRef]` — пересечений ключей между ConfigMap и Secret нет.

`11-secret.yaml` содержит только очевидные dev-плейсхолдеры (те же значения, что и в `docker-compose.yml`) и явный комментарий-предупреждение: это допустимо ТОЛЬКО для полностью локального, эфемерного кластера без внешнего доступа. Вне этого сценария секрет создаётся `kubectl create secret` / внешним менеджером секретов и в git не коммитится.

## Как применяются миграции

Миграция — **`Job`, не `initContainer`**. Если бы `alembic upgrade head` был `initContainer`-ом `Deployment`-а с `replicas: 2`, на пустой БД стартовали бы ДВА параллельных прогона миграции — именно то, что запрещено требованием C4. `Job` гарантированно создаёт ровно один под.

Порядок между `Job` и `api`/`worker` не гарантирован `kubectl apply` — вместо этого у `api`/`worker` есть свой **initContainer `wait-for-schema`** (`app/db/wait_for_schema.py`, тот же образ `photo-service:local`), который опрашивает `SELECT 1 FROM alembic_version` с бэкоффом и не даёт основному контейнеру стартовать, пока `Job` не отработал.

```powershell
kubectl apply -f k8s/30-migrate-job.yaml
kubectl -n photo-service wait --for=condition=complete job/alembic-upgrade --timeout=180s
kubectl -n photo-service logs job/alembic-upgrade
```

## Валидация манифестов (C5)

Основной путь (если `kubectl` установлен и кластер поднят):

```powershell
# ВНИМАНИЕ: не `-f k8s/` — в папке лежат kind-cluster.yaml (конфиг kind, не
# манифест) и port-forward.ps1, из-за них команда упадёт с
# `no matches for kind "Cluster"`. Применяем только пронумерованные файлы:
Get-ChildItem k8s\[0-9]*.yaml | Sort-Object Name | ForEach-Object { kubectl apply -f $_.FullName }
kubectl -n photo-service get all
```

**Про `--dry-run`.** Он не заменяет живое применение: `--dry-run=server`
не создаёт namespace, поэтому все ресурсы внутри `photo-service` в этом
режиме отваливаются с `namespaces "photo-service" not found` — это
артефакт режима, а не дефект манифестов (проверено на живом кластере
22.07.2026).

**Как валидировалось в этой сессии.** `kubectl` в песочнице кодера установлен (входит в Docker Desktop), но `kubectl config current-context` не задан и ни один кластер не поднят — начиная с этой версии kubectl (`v1.36.1`) `--dry-run=client` тоже требует discovery-запрос к API-серверу даже для встроенных типов (`Namespace`/`ConfigMap`/`Deployment`/...), поэтому команда выше упирается в `dial tcp ... connectex: No connection could be made`. Это ожидаемое ограничение среды кодера (нет запущенного кластера/`kind`), а не проблема манифестов.

Вместо этого манифесты провалидированы **YAML-парсером Python** (гейт задания это прямо предусматривает как запасной путь):
1. Каждый файл в `k8s/*.yaml` разобран `yaml.safe_load_all` — синтаксис YAML корректен во всех 13 файлах, включая многодокументные (`---`).
2. Для каждого k8s-документа (23 штуки) проверено: есть `apiVersion`, `kind`, `metadata.name`; для всех ресурсов, кроме `Namespace`, `metadata.namespace == "photo-service"`.
3. Для `Deployment`/`StatefulSet` — есть `replicas`, хотя бы один контейнер, у каждого контейнера есть `image` и `resources`; для трёх наших образов (`photo-service:local`, `photo-web:local`) — обязательно `imagePullPolicy: IfNotPresent` и тег `:local` (design §9.3, риск R10 — иначе `ErrImagePull`).
4. Для `Job` — есть `backoffLimit`, `ttlSecondsAfterFinished`, `restartPolicy: OnFailure`.
5. Отдельно — вложенные строковые YAML/JSON внутри `ConfigMap.data` (конфиг и правила Prometheus, датасорс и дашборд Grafana) распарсены как YAML/JSON и **побайтово сверены** с исходными `prometheus/rules.yml` и `grafana/dashboards/photo-service.json` — расхождений нет.
6. `kind-cluster.yaml` — отдельно: `kind: Cluster`, `apiVersion: kind.x-k8s.io/v1alpha4`, есть хотя бы один узел с `image`.

Все проверки прошли (`All structural checks passed.`). Дополнительно оркестратор
провалидировал манифесты **строгой офлайн-схемой Kubernetes 1.31**
(`kubernetes-validate`, strict): 26 объектов, 0 ошибок.

**Живой прогон выполнен 22–23.07.2026** на `kind v0.32.0` (узел
`kindest/node:v1.31.2`): все 10 подов `Running`, `Job alembic-upgrade`
`Completed`, фото и батч прошли конвейер до `done` с реальным анализатором
(`model_version = opencv-dnn-res10-ssd+laplacian+phash/1.1.0`), Prometheus
видит 4 таргета (по реплике api и worker), Grafana и веб-интерфейс работают.
Прогон выявил три дефекта, не обнаружимых статически — см. раздел «Ловушки»
в конце документа.

## Схема доступа (port-forward)

Кластер `kind` не публикует порты наружу (`kind-cluster.yaml` намеренно без `extraPortMappings`) — доступ к любому сервису осуществляется через `kubectl port-forward`, запущенный на той же машине, где показывается демо:

```powershell
kubectl -n photo-service port-forward svc/web        8080:80
kubectl -n photo-service port-forward svc/api         8000:8000
kubectl -n photo-service port-forward svc/grafana     3000:3000
kubectl -n photo-service port-forward svc/prometheus  9090:9090
```

Или одной командой (опционально, `photo-service/k8s/port-forward.ps1`):

```powershell
pwsh k8s/port-forward.ps1
```

**Скрипт может не запуститься — это нормально, он не обязателен.** На
чистой Windows `pwsh` (PowerShell 7) часто не установлен, а Windows
PowerShell 5.1 откажется выполнять неподписанный `.ps1`
(`UnauthorizedAccess`, см. `about_Execution_Policies`). В этом случае
просто запускайте четыре команды выше, каждую в своём окне.

Особенности `port-forward`, важные для демонстрации:
- туннель живёт ровно столько, сколько открыто его окно; закрыли окно —
  сервис стал недоступен из браузера;
- туннель рвётся при перезапуске пода и может отвалиться после простоя —
  лечится повторным запуском той же команды;
- `Start-Job` для фоновых пробросов привязан к сессии PowerShell: в новом
  окне `Get-Job` покажет пусто, хотя процессы ещё живы и порт занят
  (`Only one usage of each socket address`).

URL: веб-интерфейс `http://localhost:8080`, API/Swagger `http://localhost:8000/docs`, Grafana `http://localhost:3000` (`admin` / пароль из Secret), Prometheus `http://localhost:9090`.

**Важно:** оба порта — `web` (8080) и `api` (8000) — должны быть проброшены ОДНОВРЕМЕННО. Фронт обращается к API по `http://localhost:8000` (значение `API_BASE_URL` в `42-web.yaml`), а API разрешает CORS только для `http://localhost:8080` (`CORS_ALLOWED_ORIGINS` в `10-configmap.yaml`) — расхождение портов даст CORS-отказ в браузере.

## Снос кластера

```powershell
kind delete cluster --name photo
```

Убирает кластер целиком: все поды, PVC, namespace — одним действием, не затрагивая остальной Docker на машине.

## Что манифесты НЕ требуют

- Внешнего registry (образы собираются локально и загружаются `kind load docker-image`, см. `README.md`).
- Доступа к серверу Sirius — кроме сетевой достижимости внешнего анализатора при живом прогоне (проверено спайком, TCP ~40 мс).

## Ловушки, найденные живым прогоном (22–23.07.2026)

Все перечисленное уже исправлено в манифестах — раздел оставлен как справка
на случай похожих симптомов и как объяснение, почему параметры именно такие.

**`kafka-0` в `CrashLoopBackOff`, в логах `UnknownHostException: kafka-0.kafka`.**
Headless-сервис по умолчанию публикует DNS-записи только для *готовых* подов,
а KRaft-брокеру нужно разрешить собственное имя из
`KAFKA_CONTROLLER_QUORUM_VOTERS` ещё до того, как он соберёт кворум и станет
готовым — замкнутый круг. Решается `publishNotReadyAddresses: true` в Service
(`k8s/22-kafka.yaml`).

**`kafka-0` навсегда `0/1`, `Startup probe failed: command timed out after 1s`.**
Пробы запускают `kafka-broker-api-versions.sh`, который поднимает целую JVM, а
`timeoutSeconds` по умолчанию равен 1 с — проба не успевает никогда. При этом
брокер работает (топик создаётся, консьюмер-группа собирается), но kubelet его
перезапускает. Решается `timeoutSeconds: 10` на readiness/startup. В
`docker-compose.yml` у того же healthcheck изначально стоял `timeout: 10s` —
при переносе в манифест это легко потерять.

**Grafana открывается, потом перестаёт отвечать; в `port-forward` —
`connection refused inside namespace` и `lost connection to pod`.**
Не туннель: процесс Grafana внутри пода убивается по памяти. Она на старте
ставит бандл-плагины и строит поисковые индексы, чего не хватает 256 МБ.
Решается `limits.memory: 768Mi` (`k8s/41-grafana.yaml`). Симптом легко принять
за проблему `port-forward` — проверяйте `RESTARTS` у пода.

**Общее наблюдение.** Ни одна из трёх проблем не выявляется статически:
манифесты проходят строгую валидацию по схеме Kubernetes 1.31, тесты зелёные,
код-ревью пройдено. Ресурсные лимиты в принципе не проверяются без нагрузки, а
дедлок DNS виден только в момент старта настоящего кластера.

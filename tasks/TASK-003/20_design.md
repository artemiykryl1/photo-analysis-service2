---
task_id: TASK-003
agent: architect
model: opus
status: done
inputs:
  - specs/constitution.md
  - specs/feature-upload/tasks.md (раздел TASK-003, блоки A–D; C5/D переписаны под локальный k8s 21.07.2026)
  - tasks/TASK-003/00_orchestration.md
  - tasks/TASK-003/05_spike_analyzer.md
  - tasks/TASK-003/10_context.md
outputs:
  - tasks/TASK-003/20_design.md
spec_refs:
  - TASK-003 A1–A8 (реальный анализатор)
  - TASK-003 B1–B4 (веб-интерфейс)
  - TASK-003 C1–C5 (Kubernetes, локально)
  - TASK-003 D1–D5 (локальный запуск и демонстрация)
  - constitution.md §2.3, §2.4, §3.1, §3.2, §3.3, §3.4
timestamp: 2026-07-22T00:00:00Z
revision: "D8/D9 переработаны под ЛОКАЛЬНЫЙ кластер (уточнение пользователя 21.07.2026); блоки A/B не изменялись"
---

# TASK-003 — Дизайн: реальный анализатор, веб-интерфейс, Kubernetes, деплой

## 0. Резюме решения

Спайк A3 сломал три предпосылки: сервер режет сообщения на 4 MiB, `RESOURCE_EXHAUSTED`
классифицирован как RETRY, а `blur_score` имеет инвертированную относительно нашей
семантику. Дизайн отвечает так:

1. **Лимит 50 МБ для пользователя сохраняем.** Worker готовит уменьшенную копию для
   анализатора (гибрид: влезает — шлём как есть, не влезает — уменьшаем лесенкой). Новая
   зависимость — Pillow.
2. **Формула лучшего кадра переписывается.** `is_blurred` выбрасывается из формулы
   (поле бесполезно — 5/5 замеров True), первым ключом становится наш собственный
   порог резкости по `blur_score`, дальше композиция (лица), затем открытые глаза,
   затем точная резкость, затем детерминированные тай-брейки.
3. **Ошибки разделяются на двух уровнях** — превентивная проверка размера до отправки
   плюс распознавание текста detail на случай, если сервер поменяет лимит.
4. **Worker получает ObjectStorage**, читает байты один раз за сообщение и переиспользует
   подготовленные байты между ретраями.
5. Схема БД растёт на 4 nullable-поля (`tags` — JSONB), появляется статический
   веб-интерфейс под nginx.
6. **Демонстрация — в ЛОКАЛЬНОМ кластере Kubernetes** (уточнение 21.07.2026). Дистрибутив —
   **kind**, доставка образов — `kind load`, доступ к UI — `kubectl port-forward`. Сервер
   Sirius остаётся только хостом внешнего анализатора; `docker-compose.server.yml`,
   диапазон портов 51100–51119 и SSH-деплой из скоупа исключены.

---

## 1. Границы изменений по сервисам

| Компонент | Что меняется | Что НЕ меняется |
|---|---|---|
| `photo-service` API | +CORS, +4 поля в `AnalysisResultResponse`, +маппер | пути, коды, формат ошибок, лимиты загрузки |
| `photo-service` worker | +ObjectStorage, +подготовка изображения, новая классификация ошибок, новый throttle при UNAVAILABLE | at-least-once consumer, атомарный захват, порядок терминальной записи |
| `analyzer-stub` | новый контракт, 8 полей, реалистичная семантика blur_score | остаётся дефолтом только для compose и тестов |
| БД | миграция v003, 4 nullable-колонки | v001/v002, PG-enum не добавляем |
| `batch_service` | новая формула `select_best_photo` | `get_batch` остаётся read-only |
| `web/` (новый) | статический фронт под nginx | — |
| `k8s/` (новый) | манифесты всех компонентов под локальный кластер | — |

---

## 2. D1 — Размер изображения против лимита 4 MiB

### Решение: вариант (в) — гибрид с подготовкой копии в worker

**Что отвергнуто и почему.**

- **(а) Снизить `MAX_FILE_SIZE_BYTES` до ~3.5 МБ.** Формально проще всего, но:
  фотография с современного телефона (12–48 Мп JPEG) — это 4–12 МБ, то есть продукт
  перестанет принимать типичный вход. Кроме того это ломает F2 из TASK-002.1 (ранние
  капнутые чтения рассчитаны на 50 МБ) и меняет публично заявленный лимит из
  constitution.md §2.3 — регресс продукта ради ограничения чужого сервиса.
- **(б) Всегда уменьшать.** Хорошо для сопоставимости результатов внутри батча, но
  добавляет обязательный decode/encode даже для 200-килобайтной картинки, где он не нужен,
  и безусловно теряет детали для детекции лиц на и без того маленьких кадрах.

**Принято (в).** Правило подготовки:

```
prepare_for_analysis(data) -> (bytes_to_send, downscaled: bool)

1. если len(data) <= ANALYZER_MAX_IMAGE_BYTES и
   max(width, height) <= ANALYZER_MAX_IMAGE_SIDE  -> вернуть data как есть, downscaled=False
2. иначе — уменьшить лесенкой (первая ступень, которая влезла в бюджет):
      (2048, q85) -> (1600, q80) -> (1280, q75) -> (1024, q70) -> (800, q60)
   формат выхода — всегда JPEG, RGB (альфа PNG кладётся на белый фон)
3. если ни одна ступень не влезла -> AnalyzerMessageTooLarge (постоянная ошибка)
```

**Бюджет.** Потолок сервера — 4 194 304 байта на всё сообщение. Служебная часть:
`photo_id` (36 байт) + `object_key` (~60 байт) + теги и varint-длины полей (< 20 байт),
то есть меньше 150 байт. Тем не менее берём запас на порядок больше, потому что размер
JPEG после перекодирования неточно предсказуем:

```
ANALYZER_MAX_MESSAGE_BYTES = 4 * 1024 * 1024   # 4 194 304 — потолок сервера, только для проверки
ANALYZER_MAX_IMAGE_BYTES   = 3_500_000          # бюджет на image_bytes (~83% потолка)
ANALYZER_MAX_IMAGE_SIDE    = 2048               # px по длинной стороне
ANALYZER_MAX_IMAGE_PIXELS  = 50_000_000         # защита от decompression bomb
```

**Где происходит.** Новый модуль `app/services/image_prep.py` — **чистая функция**
`prepare_for_analysis(data: bytes, settings) -> PreparedImage`, без I/O, без сети, без БД.
Вызывается из `AnalysisProcessor` через `anyio.to_thread.run_sync` (decode/encode — CPU-bound,
блокировать event loop нельзя). Чистота критична: вся ветка «лесенка ступеней» тестируется
юнит-тестами на сгенерированных Pillow-ом картинках, без MinIO и без gRPC.

**Библиотека.** Pillow. Вес: `pillow` wheel manylinux ~4.5 МБ, разворачивается в ~12 МБ в
образе. Это единственная новая runtime-зависимость; альтернативы (opencv-python ~90 МБ,
pyvips + системный libvips) заметно тяжелее. Версию **пинуем минорно**
(`pillow>=11.0,<12`), потому что от версии кодировщика зависят выходные байты и,
следовательно, детерминизм (см. ниже). Образ общий для api и worker — api Pillow не
использует, лишние 12 МБ принимаем ради одного Dockerfile.

**Память.** Пик на одно сообщение: оригинал (до 50 МБ) + декодированный растр + результат.
Растр 50-мегапиксельного JPEG в RGB — это ~150 МБ, что опасно для worker-а. Меры:
1. `ANALYZER_MAX_IMAGE_PIXELS = 50_000_000` — превышение даёт постоянную ошибку
   `IMAGE_TOO_LARGE` без попытки декодирования (размеры читаются из заголовка, `Image.open`
   ленив и растр не поднимает);
2. для JPEG вызываем `img.draft("RGB", (side, side))` **до** `load()` — Pillow уменьшает
   прямо при DCT-декодировании, экономя до 16x памяти и времени;
3. оригинальные байты освобождаются сразу после подготовки (`data = None` перед gRPC-вызовом),
   в памяти остаётся только подготовленная копия;
4. лимит памяти worker-а в k8s — 1Gi (см. D8).

**Если даже после уменьшения не влезает.** Практически недостижимо (800 px @ q60 — это
40–120 КБ), но обработано явно: `AnalyzerMessageTooLarge` → NO_RETRY →
`last_error_code = MESSAGE_TOO_LARGE`, фото в `failed`. gRPC-вызов не делается вообще —
общий анализатор не тревожим.

**Детерминизм.** Одинаковые входные байты + одинаковая версия Pillow + фиксированная
лесенка = одинаковые выходные байты = одинаковый ответ анализатора. Тесты подготовки
проверяют свойства (влезло в бюджет, сторона не превышена, формат JPEG, повторный вызов даёт
байт-в-байт то же), а не конкретные хеши — иначе апгрейд Pillow будет ломать CI.

**Принятое ограничение (записать в docs).** `blur_score` — дисперсия лапласиана — зависит
от разрешения. Кадр, отправленный в оригинале (2 МБ, 4000 px), и кадр, уменьшенный до
2048 px, сравниваются формулой не вполне честно. Смягчение: (1) кадры одного батча почти
всегда с одной камеры, то есть одного разрешения, и попадают в одну ветку правила;
(2) первый ключ формулы — грубое ведро «резкое/размытое», а не точное значение, поэтому
скос влияет только на тай-брейк внутри ведра. Альтернатива с полной нормализацией
(вариант (б), всегда 1600 px) вынесена в раздел «Риски».

---

## 3. D2 — Новая формула лучшего кадра (изменение зафиксированного решения)

### 3.1 Почему старая формула нерабочая

Старый ключ: `is_blurred ASC → blur_score ASC → faces_count DESC → created_at ASC → photo_id ASC`.

- `blur_score ASC` при реальной семантике (дисперсия лапласиана, больше = резче) выбирает
  **самый размытый** кадр. Замеры: резкий 930.47 против размытого 1.10.
- `is_blurred` первым ключом: во всех пяти замерах спайка поле вернуло `True`, включая кадр
  с `blur_score = 99 774`. Как разделитель поле не работает.

### 3.2 Новый порядок ключей

```
1. sharp_enough DESC   — вычисляем сами: blur_score >= SHARPNESS_THRESHOLD (100.0)
2. faces_count DESC    — больше людей в кадре
3. eyes_closed_count ASC — меньше закрытых глаз (NULL трактуем как 0)
4. blur_score DESC     — точная резкость внутри ведра
5. created_at ASC      — раньше снятый
6. photo_id ASC        — финальный детерминированный тай-брейк
```

Реализация — тот же `min()` с кортежем:
`(not sharp_enough, -faces_count, eyes_closed, -blur_score, created_at, photo_id)`.

### 3.3 Обоснование каждого ключа

**1. `sharp_enough` (ведро, а не сырое значение) — первым.** Если сделать первым ключом
сырой `blur_score DESC`, все последующие ключи становятся мёртвым кодом: непрерывная
величина практически никогда не даёт ничью, и «лучший кадр» превращается в «самый резкий
кадр любой ценой» — кадр с закрытыми глазами и одним лицом победит групповой портрет,
отличаясь на 3% резкости. Ведро возвращает смысл остальным ключам: сначала отсекаем
объективно негодные кадры, потом выбираем среди годных по содержанию.

**Порог 100.0.** По замерам спайка: размытые кадры дали 0.375 / 1.10 / 2.926, резкие —
930.47 / 99 774. Порог 100 лежит примерно посередине логарифмической шкалы и с запасом
отделяет обе группы. Значение — модульная константа `SHARPNESS_THRESHOLD` в
`app/services/batch_service.py` (не env-переменная: `select_best_photo` обязана остаться
чистой функцией без Settings, это её главное тестовое свойство). То же число дублируется
на фронте через `config.js` — зафиксировать в комментарии обоих мест, что значения должны
совпадать.

**2. `faces_count DESC` перед глазами.** Количество лиц — свойство композиции («все ли попали
в кадр»), а не качества съёмки. Кадр, где один человек оказался за границей кадра, хуже
кадра, где он в кадре, но моргнул, — недостающего человека нельзя вернуть, а моргание
портит один кадр из серии.

**3. `eyes_closed_count ASC` — новое поле.** При равной композиции меньше закрытых глаз
однозначно лучше. `NULL` (строки до v003 и любой анализатор, не заполнивший поле)
трактуется как `0` — то есть «нет данных о закрытых глазах» не штрафуется. Смешение
NULL-строк и заполненных внутри одного батча на практике невозможно: все фото батча
анализируются одним прогоном одной версии кода.

**4. `blur_score DESC`.** Внутри ведра «резкие» предпочитаем более резкий. Направление
исправлено с ASC на DESC — это и есть центральная правка спайка. Диапазон значения
не 0…1, а неограниченный положительный, никаких нормировок мы не делаем.

**5–6. `created_at ASC`, `photo_id ASC`.** Сохраняем без изменений. `created_at` в пределах
батча практически всегда одинаков (одна транзакция `create_batch`), поэтому `photo_id`
остаётся обязательным финальным тай-брейком — иначе результат зависит от порядка
итерации `batch.photos` (это правка M1 из TASK-002/40_review-1.md, откатывать её нельзя).

### 3.4 Судьба `is_blurred`

**В формуле не участвует.** Обоснование:
- эмпирически поле не различает резкое и размытое (5/5 True, включая 99 774);
- как первый ключ оно кладёт всех кандидатов в одно ведро и обнуляет сортировку;
- хуже того, если автор анализатора однажды починит поле, поведение нашей главной фичи
  скачком изменится без единой правки у нас — недопустимая зависимость от чужого бага
  в обе стороны.

**В БД и в API-ответе поле остаётся** — мы честно отдаём то, что вернул анализатор.
В UI показываем его как сырое поле анализатора, а вердикт «резкое/размытое» рисуем по
нашему порогу `SHARPNESS_THRESHOLD` (см. D7). Отдельным действием: сообщить автору
анализатора о подозрении на баг (`is_blurred` всегда True, тег `blurry` всегда присутствует).

### 3.5 Влияние на тесты и заглушку

- `tests/test_batch_service.py`: `test_is_blurred_false_beats_true_regardless_of_other_fields`
  — **удалить** (поведение осознанно исчезло); `test_lower_blur_score_wins_when_is_blurred_tied`
  — **переписать** в `test_higher_blur_score_wins_within_same_sharpness_bucket`;
  `test_more_faces_wins_when_is_blurred_and_blur_score_tied` — переписать под новый набор
  зафиксированных полей. Добавить: ведро (`blur_score=101, faces=0` бьёт
  `blur_score=99, faces=5`), `eyes_closed_count` как третий ключ, NULL-глаза = 0.
- Заглушка обязана отдавать `blur_score` в реальной семантике, иначе тесты пайплайна
  закрепят инверсию (см. D6).

### 3.6 Готовая формулировка для спеки

> Заменить строку таблицы «Зафиксированные решения» в `specs/feature-upload/tasks.md`
> (строка 105, «Лучший кадр батча») на текст ниже.

**Лучший кадр батча (редакция TASK-003, заменяет редакцию TASK-002).**
Детерминированная формула по результатам анализа. Учитываются только фото со статусом
`done` и с непустым результатом анализа; если таких нет — `best_photo_id = null`.
Порядок ключей:

1. **резкость-ведро** (убывание): `blur_score >= 100.0` считается резким. `blur_score` —
   дисперсия лапласиана от реального анализатора, **больше = резче**, диапазон
   неограниченный положительный (замеры: размытые 0.4–2.9, резкие 930–99 774);
2. **`faces_count`** (убывание) — больше лиц в кадре лучше;
3. **`eyes_closed_count`** (возрастание) — меньше закрытых глаз лучше; `NULL` = 0;
4. **`blur_score`** (убывание) — точная резкость внутри ведра;
5. **`created_at`** (возрастание);
6. **`photo_id`** (возрастание) — финальный детерминированный тай-брейк.

Поле `is_blurred` в формуле **не используется**: эмпирически (спайк A3,
`tasks/TASK-003/05_spike_analyzer.md`) реальный анализатор возвращает `True` для всех
кадров, включая заведомо резкие, поэтому как ключ сортировки оно бесполезно и создаёт
зависимость нашей главной фичи от чужого дефекта. Поле сохраняется в БД и в HTTP-ответе как
сырые данные анализатора; вердикт «резкое/размытое» в UI считается по нашему порогу.
Порог `SHARPNESS_THRESHOLD = 100.0` — модульная константа `app/services/batch_service.py`,
дублируется во фронтовом `config.js`; при расхождении источником истины считается бэкенд.

---

## 4. D3 — Классификация ошибок

### 4.1 Двухуровневая защита (оба механизма, не «или»)

**Уровень 1 — превентивная проверка (основной).** В `AnalysisProcessor` после подготовки
изображения:

```
if len(prepared) + MESSAGE_OVERHEAD_BYTES > settings.ANALYZER_MAX_MESSAGE_BYTES:
    raise AnalyzerMessageTooLarge(...)
```

где `MESSAGE_OVERHEAD_BYTES = 1024` — заведомо избыточная оценка служебной части
protobuf (реально < 150 байт). Почему это основной механизм: мы не тратим сетевой раунд,
не нагружаем общий анализатор заведомо мёртвым запросом и получаем внятный код без разбора
чужих строк.

**Уровень 2 — распознавание detail (страховка).** Реальный сервер может изменить лимит,
а мы можем ошибиться в оценке overhead. Поэтому `RESOURCE_EXHAUSTED` дополнительно
разбирается:

```
_MESSAGE_TOO_LARGE_MARKER = "larger than max"   # регистронезависимо

def _is_message_too_large(exc) -> bool:
    return (isinstance(exc, grpc.aio.AioRpcError)
            and exc.code() is grpc.StatusCode.RESOURCE_EXHAUSTED
            and _MESSAGE_TOO_LARGE_MARKER in (exc.details() or "").lower())
```

Маркер подобран так, чтобы покрывать обе формулировки gRPC: серверную
`Received message larger than max (6687802 vs. 4194304)` и клиентскую
`Sent message larger than max`. Одна общая функция используется и в `classify_grpc_error`,
и в `error_code_from_exception` — иначе решение и код ошибки разъедутся.

### 4.2 Итоговая таблица классификации

| Условие | Решение | `last_error_code` |
|---|---|---|
| `AnalyzerMessageTooLarge` (превентивно) | NO_RETRY | `MESSAGE_TOO_LARGE` |
| `RESOURCE_EXHAUSTED` + detail содержит `larger than max` | NO_RETRY | `MESSAGE_TOO_LARGE` |
| `RESOURCE_EXHAUSTED` без маркера (троттлинг общего анализатора) | **RETRY** | `RESOURCE_EXHAUSTED` |
| `UNAVAILABLE`, `DEADLINE_EXCEEDED`, `ABORTED`, `INTERNAL`, `TimeoutError` | RETRY | как раньше |
| `INVALID_ARGUMENT`, `NOT_FOUND`, `UNIMPLEMENTED`, `PERMISSION_DENIED`, ... | NO_RETRY | имя кода |
| `StorageUnavailable` (MinIO недоступен) | RETRY | `STORAGE_UNAVAILABLE` |
| `NotFoundError` из MinIO (NoSuchKey) | NO_RETRY | `OBJECT_NOT_FOUND` |
| `ImageDecodeError` (Pillow не распознал) | NO_RETRY | `IMAGE_DECODE_FAILED` |
| `ImageTooLargeError` (> ANALYZER_MAX_IMAGE_PIXELS) | NO_RETRY | `IMAGE_TOO_LARGE` |
| всё прочее | NO_RETRY (fail-safe) | `UNKNOWN` |

### 4.3 Где живёт классификация

`classify_grpc_error` / `error_code_from_exception` остаются в
`app/integrations/analyzer_client.py` и отвечают только за gRPC и таймауты — их
существующие тесты продолжают проходить без правок, кроме двух новых кейсов
RESOURCE_EXHAUSTED.

Новый модуль **`app/services/analysis_errors.py`** с единственной чистой функцией:

```
def classify_error(exc: Exception) -> tuple[RetryDecision, str]
```

Она знает про storage- и image-ошибки, а всё остальное делегирует в analyzer_client.
`AnalysisProcessor` вызывает **только** её. Обоснование границы: MinIO и Pillow не имеют
отношения к gRPC-клиенту, и складывать их в `analyzer_client` — размывание слоя.

---

## 5. D4 — Worker ↔ MinIO

### 5.1 Проводка

`ObjectStorage(settings)` создаётся в `app/worker/main.py::_run` рядом с `AnalyzerGrpcClient`
и передаётся в `AnalysisProcessor(storage=...)`. Параметр **обязательный keyword-only**
(не `| None = None`): наполовину сконфигурированный процессор, молча пропускающий чтение
байтов, — это тихий продовый отказ. Существующие тестовые фикстуры сломаются с явным
`TypeError`, что нам и нужно.

`ensure_bucket()` в worker **не вызываем** — бакет создаёт API при старте; worker только
читает и не должен требовать прав на создание.

### 5.2 Порядок шагов в `process()`

```
1. claim_for_processing + commit          (без изменений)
   rowcount == 0 -> skip-ветка            (без изменений, F1/F3 не трогаем)
2. photo_analysis_started_total, t0       (без изменений)
3. цикл попыток 1..WORKER_MAX_ATTEMPTS:
     3.1 если prepared is None:
           raw = await to_thread(storage.get_file, object_key) под таймаутом
           prepared = await to_thread(prepare_for_analysis, raw)
           raw = None
           превентивная проверка размера
     3.2 response = await analyzer.analyze(photo_id, object_key, prepared.data)
     3.3 break
   except: classify_error -> record_attempt -> commit -> backoff/выход
4. терминальная запись (без изменений по структуре, +4 поля в upsert)
5. _maybe_complete_batch, метрика сообщения (без изменений)
```

**Почему качаем ПОСЛЕ захвата, а не до.** Захват — одна дешёвая `UPDATE ... WHERE
status='pending'`; при at-least-once повторной доставке и при нескольких репликах worker-а
её проигрывает большинство обработчиков. Скачать до захвата означало бы тянуть из MinIO до
50 МБ, чтобы тут же их выбросить в skip-ветке. Кроме того, байты держатся в памяти строго
в промежутке «мы владеем фото», что делает пик памяти предсказуемым.

**Почему внутри цикла, но с кешированием (`if prepared is None`).** Три альтернативы:
- скачать один раз ДО цикла — тогда временная ошибка MinIO валит фото с первой попытки,
  тогда как gRPC получает три; несимметрично и нарушает A2 («ошибка чтения — временная»);
- скачивать на каждой попытке — лишняя нагрузка на MinIO и повторный CPU-дорогой ресайз;
- **кеш внутри цикла (принято)** — временная ошибка чтения ретраится тем же самым
  существующим механизмом (backoff, `record_attempt`, метрики, классификация), а успешная
  подготовка выполняется ровно один раз.

### 5.3 Обёртка синхронного SDK и таймауты

```
async with asyncio.timeout(settings.STORAGE_READ_TIMEOUT_SECONDS):   # новая, default 60.0
    raw = await anyio.to_thread.run_sync(self._storage.get_file, object_key)

async with asyncio.timeout(settings.IMAGE_PREP_TIMEOUT_SECONDS):     # новая, default 30.0
    prepared = await anyio.to_thread.run_sync(prepare_for_analysis, raw, settings)
```

60 с на MinIO — из constitution.md §3.2. Отдельный таймаут на подготовку нужен, потому что
патологический вход может съесть минуты CPU; 30 с с запасом покрывает 50 Мп кадр.
`asyncio.timeout` вокруг `to_thread` отменяет ожидание, но не сам поток — это известное
ограничение; поток завершится сам, память освободится. Зафиксировать комментарием.

### 5.4 Влияние на память

Один консьюмер обрабатывает по одному сообщению за раз (последовательный цикл), поэтому пик =
оригинал (≤50 МБ) + растр после `draft()` (≤ ~30 МБ при стороне 2048) + результат (≤3.5 МБ).
Расчётный потолок ~100 МБ на реплику сверх базового рантайма. Лимит памяти пода worker —
**1Gi**, request — 512Mi. Метрика `analyzer_image_prepared_bytes` (гистограмма) даёт
фактическое распределение для последующей подстройки.

---

## 6. D5 — Схема БД и миграция v003

### 6.1 Колонки

| Колонка | Тип | Null | Обоснование |
|---|---|---|---|
| `eyes_closed_count` | `INTEGER` | да | счётчик; старые строки не имеют |
| `dominant_color` | `VARCHAR(32)` | да | `#feffff` — 7 символов; 32 даёт запас на `rgb(...)`/именованные формы |
| `tags` | `JSONB` | да | см. ниже |
| `model_version` | `VARCHAR(128)` | да | `opencv-dnn-res10-ssd+laplacian+phash/1.1.0` — 42 символа; 128 с запасом |

### 6.2 `tags`: JSONB против TEXT[]

**Выбран JSONB.** Обоснование:
1. SQLAlchemy 2.x + asyncpg отдают `JSONB` как готовый `list[str]` без дополнительных
   конвертеров; `postgresql.ARRAY(String)` требует диалект-специфичного импорта и в
   ORM-модели навсегда привязывает `analysis_results` к PostgreSQL на уровне типов Python.
2. Мы не делаем поиск по тегам (дедупликация и фильтры явно вне скоупа TASK-003), значит
   главное преимущество `TEXT[]` — дешёвый GIN-индекс на membership — нам сейчас не нужно.
   Если понадобится, JSONB тоже индексируется GIN (`jsonb_path_ops`).
3. `tags` — непрозрачный список от внешнего сервиса, форма которого нам не подконтрольна
   (в замерах туда попал служебный `dominant:#feffff`, дублирующий отдельное поле). Хранить
   такое как документ честнее, чем как реляционный массив, который мы делаем вид, что понимаем.

Альтернатива `TEXT[]` (строже типизировано, компактнее, дешевле для будущего поиска)
зафиксирована в разделе «Риски». Переход между ними — одна миграция, решение обратимо.

### 6.3 Миграция

Файл `photo-service/migrations/versions/v003_add_analyzer_extended_fields.py`,
`revision = "v003"`, `down_revision = "v002"`. Стиль — как v002.

```
upgrade():   add_column eyes_closed_count -> dominant_color -> tags -> model_version
downgrade(): drop_column model_version -> tags -> dominant_color -> eyes_closed_count
```

Никаких `postgresql.ENUM` (урок TASK-000/60_debug.md), никаких `server_default` (все
nullable), никакого бэкфилла — старые строки остаются с NULL, это семантически верно
(«анализатор этой версии таких данных не давал»).

### 6.4 Границы транзакций и консистентность

Не меняются. `upsert` остаётся `INSERT ... ON CONFLICT (photo_id) DO NOTHING` внутри той же
терминальной транзакции.

**Явное решение: НЕ менять на `DO UPDATE`.** Соблазн «дозаполнить новые поля при
переобработке» надо отвергнуть: `DO NOTHING` — это и есть ключ идемпотентности из TASK-002,
и переход на `DO UPDATE` позволил бы повторной доставке перезаписать результат другим
(например, полученным с другой степени сжатия). Следствие принимаем: строка, записанная до
v003, новые поля не получит никогда — это допустимо, поле nullable.

---

## 7. D6 — Заглушка анализатора

`analyzer-stub/server.py`, новая `_analyze(object_key: str, image_bytes: bytes)`:

```
digest = sha256(object_key)                     # источник детерминизма НЕ меняется
faces_count        = digest[0] % 6
blur_score         = round(10 ** (digest[1] / 255 * 5), 3)   # 1.0 … 100 000, лог-шкала
is_blurred         = blur_score < 100.0
perceptual_hash    = digest.hex()[:16]
eyes_closed_count  = digest[4] % (faces_count + 1)           # не больше числа лиц
dominant_color     = "#" + digest[5:8].hex()
tags               = ["no_face"|"face", "bright"|"dark", "blurry"?, f"dominant:{color}"]
model_version      = "stub/2.0.0"
```

**Семантика `blur_score`.** Логарифмическая шкала 1…100 000 воспроизводит наблюдаемый
диапазон реального анализатора (0.375 … 99 774) и главное — **больше = резче**,
неограниченный положительный. Линейный 0…1000 из предложения context-collector-а хуже:
он не даёт кадров с blur_score в единицах, а именно такие значения (0.375, 1.10) отделяют
размытые кадры в реальности.

**`is_blurred`.** Заглушка считает поле **правильно** (`blur_score < 100`), а не копирует
наблюдаемый дефект «всегда True». Это безопасно ровно потому, что формула лучшего кадра
поле больше не читает (D2) — тесты не смогут случайно опереться ни на дефект, ни на его
отсутствие. Записать это обоснование комментарием в заглушке.

**`image_bytes`.** Принимаем, валидируем непустоту, логируем длину — и **не используем как
источник результата**. Детерминизм остаётся от `object_key`: тестам и демо нужен стабильный
результат на одно фото, а фикстурить конкретные байты дороже, чем ключ. Если `image_bytes`
пуст — возвращаем `INVALID_ARGUMENT`, чтобы заглушка ловила регресс «забыли приложить байты».

`analyzer-stub/Dockerfile` уже генерирует стабы из `protos/analyzer.proto` при сборке —
правок не требует, новый пакет подхватится автоматически. Healthcheck там тоже уже есть.

---

## 8. D7 — Веб-интерфейс

### 8.1 Стек: статический HTML + ES-модули под `nginx:alpine`

**Обоснование против Vite:** для четырёх экранов без роутера и стейт-менеджера сборка даёт
только издержки — node_modules и lockfile в репозитории, отдельный build-stage в Dockerfile,
риск несовпадения версий Node, и лишний шаг перед каждым деплоем. Статика даёт образ ~25 МБ,
сборку за секунды и **один и тот же образ в compose и в k8s** (конфиг подставляется
env-переменной на старте контейнера, а не вшивается на этапе сборки). Vite оправдан от
момента, когда появится сборка компонентов или TypeScript — сейчас нет.

### 8.2 Структура

```
photo-service/web/
  Dockerfile                 # nginx:alpine + entrypoint
  nginx.conf                 # listen 0.0.0.0:80, gzip, no-cache на config.js
  docker-entrypoint.sh       # envsubst config.js.template -> config.js, затем nginx -g daemon off
  html/
    index.html               # галерея + формы загрузки
    photo.html               # карточка фото (?id=...)
    batch.html               # страница батча (?id=...)
    config.js.template       # window.APP_CONFIG = {apiBaseUrl, sharpnessThreshold, pollIntervalMs}
    css/app.css
    js/api.js                # fetch-обёртка + разбор ошибок
    js/format.js             # бейджи статусов, шкала резкости, чипы тегов
    js/gallery.js
    js/photo.js
    js/batch.js
```

`config.js` генерируется на старте контейнера из `API_BASE_URL`, `SHARPNESS_THRESHOLD`,
`POLL_INTERVAL_MS` — один образ работает и локально в compose (`http://localhost:8000`), и
в k8s (значение из ConfigMap, при port-forward — `http://localhost:8000`).

### 8.3 Экраны и вызываемые эндпоинты

| Экран | Эндпоинты |
|---|---|
| Галерея (`index.html`) | `GET /v1/photos?limit=50&offset=0`, превью `GET /v1/photos/{id}/content` |
| Загрузка одиночная | `POST /v1/photos`, multipart, поле **`file`**, → 202 `{photo_id, status}` |
| Загрузка батчем | `POST /v1/photos/batch`, multipart, поле **`file`** повторяется 2–10 раз (не `files` — см. `app/api/batches.py`), → 202 `{batch_id, photos[]}` |
| Карточка фото (`photo.html`) | `GET /v1/photos/{id}`, изображение `GET /v1/photos/{id}/content` |
| Страница батча (`batch.html`) | `GET /v1/batches/{id}` |

**Поллинг.** Интервал 3 с. Запускается, только если в текущем ответе есть фото в
`pending`/`processing` (галерея, карточка) или `status == "processing"` (батч).
Останавливается при достижении терминального состояния, при `document.hidden` и по потолку
100 итераций (~5 минут) — после чего показываем кнопку «Обновить». Реализация —
`setTimeout`-цепочка, не `setInterval` (иначе запросы наслаиваются при медленном API).

**Отображение анализа в карточке.** `faces_count`, `eyes_closed_count`, `blur_score`
(число + вердикт «резкое/размытое» по `APP_CONFIG.sharpnessThreshold`), `dominant_color`
(цветной кружок + hex), `tags` (чипы), `model_version` (подпись мелким шрифтом),
`perceptual_hash` (моноширинно), `is_blurred` — показывается **как сырое поле анализатора**
с пояснением, что вердикт считается по `blur_score`, а не по нему. Для `failed` показываем
`status` и подсказку смотреть логи (наружу `last_error_code` API не отдаёт — контракт
ответа не меняем).

**Батч.** Лучший кадр — рамка + бейдж «Лучший кадр»; если `best_photo_id == null` при
`completed` — плашка «Ни один кадр не проанализирован успешно».

### 8.4 Обработка ошибок (B4)

`api.js` на любой не-2xx пытается разобрать тело как `{error_code, message, request_id}`.
Таблица человекочитаемых сообщений:

```
INVALID_FILE            -> «Файл пустой или повреждён»
PAYLOAD_TOO_LARGE       -> «Файл больше 50 МБ»
UNSUPPORTED_MEDIA_TYPE  -> «Поддерживаются только JPEG и PNG»
INVALID_BATCH_SIZE      -> «В батче должно быть от 2 до 10 файлов»
NOT_FOUND               -> «Не найдено»
SERVICE_UNAVAILABLE     -> «Сервис временно недоступен, попробуйте позже»
INTERNAL_ERROR          -> «Внутренняя ошибка сервиса»
```

Под сообщением — `request_id` мелким шрифтом (для сопоставления с логами по `trace_id`).
Неизвестный `error_code` → показываем `message` от сервера. Сетевой сбой/CORS-отказ
(`fetch` бросил `TypeError`) → «Не удалось связаться с сервисом» + подсказка проверить, что
API поднят, — никаких «Failed to fetch».

### 8.5 CORS (B3)

В `app/core/config.py`:

```
CORS_ALLOWED_ORIGINS: str = "http://localhost:8080,http://localhost:5173"
@property
def cors_allowed_origins_list(self) -> list[str]:  # split по запятой, strip, отбросить пустые
```

В `app/main.py`, **после** `RequestIdMiddleware` в коде (Starlette применяет middleware в
обратном порядке добавления, поэтому CORS окажется внешним и корректно ответит на preflight
даже при ошибке ниже):

```
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins_list,   # явный список, НЕ "*"
    allow_credentials=False,                            # cookie/Authorization не используем
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-Request-ID"],
    expose_headers=["X-Request-ID"],
)
```

`allow_credentials=False` — сознательно: аутентификации нет (вне скоупа), а `True` вместе
со списком origin создаёт ложное ощущение защищённости и мешает будущему переходу на
токены. `expose_headers` нужен, чтобы фронт мог показать `request_id` даже для ответов без
тела.

**Важно для k8s-демо (port-forward).** Origin фронта при port-forward — `http://localhost:8080`,
а API — `http://localhost:8000`; это разные origin, CORS обязателен. Значение
`CORS_ALLOWED_ORIGINS=http://localhost:8080` кладём в ConfigMap (см. D8). Если демонстратор
пробросит другой локальный порт — обновить одну строку ConfigMap.

---

## 9. D8 — Kubernetes (РЕДАКЦИЯ: локальный кластер)

> Заменяет прежнюю редакцию под учебный сервер. Демонстрация проекта проводится в ЛОКАЛЬНОМ
> кластере на машине разработчика (Windows + Docker). Внешний анализатор
> `45.132.19.101:50051` достижим из кластера напрямую (спайк подтвердил TCP ~40 мс).

### 9.1 Выбор дистрибутива: kind

**Рекомендованный путь — kind (Kubernetes in Docker). Один, без альтернатив в демо.**

Обоснование именно под эту машину (Windows, установленный Docker, живой опыт `docker compose`):
- **Переиспользует уже установленный Docker.** kind поднимает узлы кластера как обычные
  Docker-контейнеры — никакого второго гипервизора и отдельной ВМ (в отличие от minikube с
  драйверами hyperv/virtualbox). Пользователь уже гонял `docker compose`, значит движок и
  бэкенд WSL2 у него настроены и проверены.
- **Одна CLI-утилита, скриптуемо и воспроизводимо.** `kind create cluster` — одна команда;
  конфиг кластера версионируется файлом. Docker Desktop Kubernetes включается галочкой в GUI
  (не скриптуется, состояние «включён/выключен» неявное, тяжелее переносится между машинами
  ревьюеров), а его образ живёт в том же daemon-е, что усложняет чистый снос кластера.
- **Явная и предсказуемая доставка образов** через `kind load docker-image` (см. §9.3) —
  в отличие от Docker Desktop, где «общий daemon» работает как побочный эффект, который легко
  сломать неверным `imagePullPolicy`.
- **Дёшево сносится.** `kind delete cluster` убирает всё одним действием, не трогая остальной
  Docker пользователя.

Отвергнуто: **Docker Desktop Kubernetes** — привязка к GUI, неявное состояние, разделяемый с
хостом daemon (риск конфликтов и грязного снятия). **minikube** — отдельная ВМ/драйвер,
лишний слой поверх уже работающего Docker, медленнее стартует.

**Предусловие для кодера/демонстратора:** установленные `kind` и `kubectl` (обе — один
статический бинарник, ставятся без админ-прав; команды установки — в README). Версию
Kubernetes фиксируем образом узла (`kindest/node:v1.31.x`) для воспроизводимости.

**Конфиг кластера** `photo-service/k8s/kind-cluster.yaml`: один control-plane узел,
`extraPortMappings` не используем (доступ к UI — через port-forward, см. §9.5), что делает
кластер полностью портативным между машинами.

### 9.2 Состав манифестов (`photo-service/k8s/`)

Префиксы-номера задают порядок `kubectl apply -f k8s/`. **`analyzer-stub` в k8s
отсутствует** — анализатор внешний, его адрес в ConfigMap.

| Файл | Содержимое |
|---|---|
| `kind-cluster.yaml` | конфиг kind (не применяется через apply; вход для `kind create cluster --config`) |
| `00-namespace.yaml` | `Namespace: photo-service` |
| `10-configmap.yaml` | `ConfigMap: photo-config` — нечувствительные настройки, вкл. внешний `ANALYZER_GRPC_ADDR` |
| `11-secret.yaml` | `Secret: photo-secrets` — `stringData` с dev-плейсхолдерами |
| `20-postgres.yaml` | `StatefulSet(1)` + headless `Service` + `volumeClaimTemplates` 5Gi |
| `21-minio.yaml` | `StatefulSet(1)` + `Service` (9000 api, 9001 console) + PVC 10Gi |
| `22-kafka.yaml` | `StatefulSet(1, KRaft)` + headless `Service` + PVC 5Gi |
| `30-migrate-job.yaml` | `Job: alembic-upgrade`, `backoffLimit: 3`, `ttlSecondsAfterFinished: 600` |
| `31-api.yaml` | `Deployment(2)` + `Service` ClusterIP 8000 |
| `32-worker.yaml` | `Deployment(2)` + `Service` ClusterIP 8001 (для scrape) |
| `40-prometheus.yaml` | `ConfigMap` (scrape-конфиг + rules) + `Deployment(1)` + `Service` |
| `41-grafana.yaml` | `ConfigMap` (provisioning) + `Deployment(1)` + `Service` |
| `42-web.yaml` | `Deployment(2)` + `Service` ClusterIP 80 |

Ingress/NodePort-манифестов **нет** — доступ к UI решён через port-forward (§9.5), что
портативнее (не требует ingress-контроллера в kind) и проще для показа с той же машины.

### 9.3 Доставка образов в кластер (без внешнего registry)

Три наших образа собираются локально из репозитория и **загружаются прямо в узлы kind** —
пуш в Docker Hub/GHCR не нужен и явно не требуется (демо офлайн-дружелюбно).

```powershell
# 1. Сборка (контекст photo-service/). Один Dockerfile обслуживает api и worker.
docker build -t photo-service:local .
docker build -t photo-web:local ./web

# 2. Загрузка образов в узлы kind (кладёт слои в containerd внутри узла-контейнера).
kind load docker-image photo-service:local --name photo
kind load docker-image photo-web:local     --name photo
```

Официальные образы (`postgres:16`, `minio/minio`, `apache/kafka:3.7.0`, `prom/prometheus`,
`grafana/grafana`) kind тянет сам из публичных реестров при первом применении — их грузить
через `kind load` не нужно.

**Критично для манифестов наших образов:** тег **не** `latest` и
**`imagePullPolicy: IfNotPresent`** (или `Never`). При `Always` (дефолт для `latest`) kubelet
попытается сходить в несуществующий registry за `photo-service:local` и упадёт в
`ErrImagePull`. Локальный тег `:local` + `IfNotPresent` заставляет использовать загруженный
через `kind load` образ. Зафиксировать комментарием в `31-api.yaml`/`32-worker.yaml`/`42-web.yaml`.

**При изменении кода** образ надо пересобрать, снова `kind load` и перезапустить деплой
(`kubectl -n photo-service rollout restart deploy/api deploy/worker`) — потому что тег
неизменный, а `kind load` обновляет слои, но не перезапускает поды. Записать в README.

### 9.4 ConfigMap против Secret

**ConfigMap `photo-config`:** `APP_NAME`, `LOG_LEVEL`, `API_PORT`, `MINIO_ENDPOINT`,
`MINIO_BUCKET`, `MINIO_SECURE`, `KAFKA_BOOTSTRAP_SERVERS`, `KAFKA_TOPIC_ANALYSIS_REQUESTED`,
`KAFKA_CONSUMER_GROUP`, `KAFKA_PUBLISH_TIMEOUT_SECONDS`,
**`ANALYZER_GRPC_ADDR: "45.132.19.101:50051"`** (внешний анализатор),
`ANALYZER_GRPC_TIMEOUT`, `ANALYZER_MAX_MESSAGE_BYTES`, `ANALYZER_MAX_IMAGE_BYTES`,
`ANALYZER_MAX_IMAGE_SIDE`, `ANALYZER_MAX_IMAGE_PIXELS`, `STORAGE_READ_TIMEOUT_SECONDS`,
`IMAGE_PREP_TIMEOUT_SECONDS`, `WORKER_MAX_ATTEMPTS`, `RETRY_BACKOFF_BASE_SECONDS`,
`ANALYZER_UNAVAILABLE_COOLDOWN_SECONDS`, `WORKER_METRICS_PORT`, `OUTBOX_*`,
`CORS_ALLOWED_ORIGINS: "http://localhost:8080"` (origin фронта при port-forward, §9.5).

**Secret `photo-secrets`:** `DATABASE_URL` (содержит пароль целиком),
`POSTGRES_USER`/`POSTGRES_PASSWORD`/`POSTGRES_DB`, `MINIO_ROOT_USER`/`MINIO_ROOT_PASSWORD`,
`MINIO_ACCESS_KEY`/`MINIO_SECRET_KEY`, `GF_SECURITY_ADMIN_PASSWORD`.

В контейнеры подаются через `envFrom: [configMapRef, secretRef]`. В `11-secret.yaml` — только
очевидные dev-плейсхолдеры плюс комментарий-предупреждение, что вне учебного демо секрет
создаётся `kubectl create secret` / внешним менеджером и в git не коммитится. Для локального
демо dev-значения в `stringData` допустимы (кластер эфемерный, наружу не смотрит).

### 9.5 Доступ из браузера: `kubectl port-forward`

**Выбран port-forward. Показ идёт с той же машины, где кластер, — это решает всё.**

Обоснование против альтернатив:
- **NodePort** в kind не публикуется на хост автоматически: узел — это контейнер, и без
  `extraPortMappings` в конфиге кластера NodePort доступен только внутри Docker-сети kind.
  Пришлось бы заранее прошивать порты в `kind-cluster.yaml`, теряя портативность и жёстко
  занимая порты хоста.
- **Ingress** требует установки и настройки ingress-контроллера (ingress-nginx) плюс правки
  `extraPortMappings` — лишняя движущаяся часть ради четырёх экранов на localhost.
- **port-forward** не требует ничего, кроме `kubectl`, работает поверх API-сервера, и его
  тривиально свернуть (Ctrl+C). Идеален для «показать с этой же машины».

Команды демонстрации (каждая — в своём терминале, работают, пока команда запущена):

```powershell
kubectl -n photo-service port-forward svc/web        8080:80     # веб-интерфейс
kubectl -n photo-service port-forward svc/api        8000:8000   # API (фронт ходит сюда)
kubectl -n photo-service port-forward svc/grafana    3000:3000   # Grafana
kubectl -n photo-service port-forward svc/prometheus 9090:9090   # Prometheus (опционально)
```

URL в браузере:
- **Веб-интерфейс:** `http://localhost:8080`
- **API/Swagger:** `http://localhost:8000/docs`
- **Grafana:** `http://localhost:3000` (логин `admin`, пароль — из Secret)

Ключевая связка: фронт собран с `API_BASE_URL=http://localhost:8000` (значение из ConfigMap
web-деплоя), а `CORS_ALLOWED_ORIGINS=http://localhost:8080` в ConfigMap api. Оба порта
пробрасываются на localhost — origin и target совпадают с тем, что разрешено CORS. Записать
в README, что оба port-forward (web и api) должны быть активны одновременно.

Опционально можно свернуть в один скрипт `k8s/port-forward.ps1`/`.sh`, поднимающий все
форварды фоново, — приложить в README как удобство, не как обязательный шаг.

### 9.6 Миграции: Job, а не initContainer

**Подтверждено: `Job`.** initContainer выполняется **в каждой реплике** Deployment-а: при
`replicas: 2` два `alembic upgrade head` стартуют параллельно на пустой БД — ровно та
ситуация, которую запрещает C4. Alembic держит advisory-lock не во всех конфигурациях, и это
классический источник «duplicate table» на холодном старте. `Job` создаёт ровно один Pod,
`backoffLimit: 3` даёт ретраи при ещё не поднявшейся БД, `ttlSecondsAfterFinished: 600`
убирает завершённый Pod.

**Как Job применяется до api/worker.** Гарантий порядка между манифестами `kubectl apply` не
даёт, поэтому у `api` и `worker` — **initContainer `wait-for-schema`** на том же образе
`photo-service:local`, выполняющий новый модуль `app/db/wait_for_schema.py` (опрос
`SELECT 1 FROM alembic_version` с бэкоффом и потолком по времени, ~25 строк). Пока Job не
создал таблицу `alembic_version` со штампом head, initContainer не завершается и основной
контейнер не стартует. Это дешевле, чем тянуть образ с `psql`, и не требует внешних
инструментов.

Команда `alembic upgrade head` **убирается** из `command` пода api в k8s — там её делает Job.
В `docker-compose.yml` она **остаётся** как есть (одна реплика api, гонки нет).

### 9.7 Пробы

| Компонент | liveness | readiness | startup |
|---|---|---|---|
| api | `GET /healthz:8000`, period 10, failure 3 | `GET /readyz:8000`, period 5, failure 3 | `GET /healthz`, period 2, failure 30 |
| worker | `GET /metrics:8001`, period 15, failure 3 | `GET /metrics:8001`, period 10, failure 3 | — |
| web | `GET /:80` | `GET /:80` | — |
| postgres | `exec pg_isready -U $POSTGRES_USER` | то же | — |
| minio | `GET /minio/health/live:9000` | `GET /minio/health/ready:9000` | — |
| kafka | `tcpSocket:9092` | `exec kafka-broker-api-versions.sh` | period 5, failure 30 |

**api переиспользует существующие эндпоинты** `/healthz` (liveness, всегда 200) и `/readyz`
(readiness, проверяет БД и MinIO) — они уже правильно разделены в `app/main.py`, новых
эндпоинтов не добавляем.

**Про worker (C2).** Отдельного HTTP-приложения у него нет, но
`prometheus_client.start_http_server` уже слушает `WORKER_METRICS_PORT` — используем его
httpGet вместо exec-пробы: дешевле, не требует утилит в образе, не порождает процесс на
каждой проверке. **Честное ограничение, записать в манифест комментарием:** метрик-сервер
живёт в отдельном потоке и переживёт зависание asyncio-цикла, то есть проба доказывает
«процесс жив», а не «консьюмер работает». Настоящий детектор зависания — алерт на
отсутствие роста `worker_messages_processed_total` при `photos_pending > 0` (§11.3).

### 9.8 Ресурсы и рестарты

| Под | requests (cpu/mem) | limits (cpu/mem) |
|---|---|---|
| api | 100m / 256Mi | 500m / 512Mi |
| worker | 200m / 512Mi | 1000m / **1Gi** (Pillow + до 50 МБ байтов, §5.4) |
| web | 10m / 32Mi | 100m / 64Mi |
| postgres | 250m / 512Mi | 1000m / 1Gi |
| minio | 100m / 256Mi | 500m / 1Gi |
| kafka | 250m / 512Mi | 1000m / 1Gi (`KAFKA_HEAP_OPTS: -Xmx512m`) |
| prometheus | 100m / 256Mi | 500m / 512Mi |
| grafana | 50m / 128Mi | 200m / 256Mi |
| migrate Job | 100m / 256Mi | 500m / 512Mi |

`restartPolicy: Always` для Deployment/StatefulSet, `OnFailure` для Job.
**Суммарно requests ~1.3 CPU / ~3.4Gi** — умещается в дефолтный лимит Docker Desktop на
Windows (обычно 4+ ядра, 6–8Gi выделено WSL2). В README указать это предусловием: если поды
висят в `Pending` по `Insufficient memory` — поднять лимит памяти Docker Desktop.

### 9.9 `replicas: 2` для worker — почему безопасно (C3)

Три независимых механизма:
1. **Атомарный захват.** `UPDATE photos SET status='processing' WHERE photo_id=:id AND
   status='pending'` — ровно одна реплика получает `rowcount = 1`, остальные видят `0` и
   уходят в skip-ветку, не вызывая анализатор. Двойной анализ невозможен по построению.
2. **Идемпотентная запись результата.** `INSERT ... ON CONFLICT (photo_id) DO NOTHING`.
3. **Атомарное завершение батча.** `UPDATE batches ... WHERE status='processing'` — гонка
   двух реплик на последнем фото батча разрешается rowcount-ом, повторное завершение
   не происходит.

Порядок обработки одного фото сохраняется, потому что ключ Kafka-сообщения — `photo_id`,
все сообщения по одному фото идут в одну партицию.

**Важная оговорка.** В compose `KAFKA_NUM_PARTITIONS: 1`, поэтому вторая реплика при том же
`group_id` простаивала бы. Чтобы `replicas: 2` имели смысл, в k8s-манифесте Kafka ставим
`KAFKA_NUM_PARTITIONS: "3"`. В compose оставляем 1 (одна реплика worker-а).

### 9.10 Валидация манифестов (C5)

```powershell
kubectl apply --dry-run=client -f k8s/          # синтаксис/схема, без кластера
# после подъёма кластера — серверная валидация и живой прогон:
kubectl apply --dry-run=server -f k8s/
kubectl -n photo-service get all                # вывод приложить к 30_impl.md и к D3
```

Записать в `docs/DEPLOYMENT.md`. Внешнего registry и доступа к Sirius (кроме сетевой
достижимости анализатора при живом прогоне) манифесты не требуют.

---

## 10. D9 — Локальный запуск и демонстрация (РЕДАКЦИЯ)

> Заменяет прежний раздел про `docker-compose.server.yml`. Сервер Sirius — только хост
> внешнего анализатора; SSH-деплой, порты 51100–51119 и server-compose из скоупа исключены
> (спека D5). `docker-compose.server.yml` **не создаётся**.

### 10.1 Последовательность «поднять локально с нуля»

Все команды — из каталога `photo-service/` (PowerShell на Windows).

```powershell
# 0. Предусловия (один раз): установлены docker, kind, kubectl. Команды установки — в README.

# 1. Создать локальный кластер.
kind create cluster --name photo --config k8s/kind-cluster.yaml

# 2. Собрать наши образы.
docker build -t photo-service:local .
docker build -t photo-web:local ./web

# 3. Загрузить образы в узлы кластера (без внешнего registry).
kind load docker-image photo-service:local --name photo
kind load docker-image photo-web:local     --name photo

# 4. Namespace, конфиг и секреты (dev-значения для локального демо).
kubectl apply -f k8s/00-namespace.yaml
kubectl apply -f k8s/10-configmap.yaml
kubectl apply -f k8s/11-secret.yaml

# 5. Инфраструктура (StatefulSet-ы с PVC).
kubectl apply -f k8s/20-postgres.yaml -f k8s/21-minio.yaml -f k8s/22-kafka.yaml
kubectl -n photo-service rollout status statefulset/postgres --timeout=180s

# 6. Миграции (Job) — один прогон, до api/worker.
kubectl apply -f k8s/30-migrate-job.yaml
kubectl -n photo-service wait --for=condition=complete job/alembic-upgrade --timeout=180s

# 7. Приложение и наблюдаемость.
kubectl apply -f k8s/31-api.yaml -f k8s/32-worker.yaml -f k8s/42-web.yaml
kubectl apply -f k8s/40-prometheus.yaml -f k8s/41-grafana.yaml
kubectl -n photo-service rollout status deploy/api deploy/worker deploy/web

# 8. Доступ к UI (в отдельных терминалах, оставить запущенными).
kubectl -n photo-service port-forward svc/web     8080:80
kubectl -n photo-service port-forward svc/api      8000:8000
kubectl -n photo-service port-forward svc/grafana  3000:3000
```

Шаги 4–7 можно свернуть в один `kubectl apply -f k8s/` (нумерация файлов задаёт порядок), но
Job миграций всё равно защищён initContainer-ом `wait-for-schema`, так что порядок между
Job и api/worker соблюдается автоматически (§9.6). Явная последовательность выше — для
наглядности демонстрации и для отладки.

### 10.2 Внешний анализатор (D2)

В ConfigMap `photo-config`: `ANALYZER_GRPC_ADDR: "45.132.19.101:50051"`. Контейнер
`analyzer-stub` в кластере **не разворачивается** (нет манифеста). Заглушка остаётся только в
`docker-compose.yml` для локальной разработки и для тестов. Проверка достижимости анализатора
из пода при отладке:

```powershell
kubectl -n photo-service run netcheck --rm -it --image=busybox --restart=Never -- sh -c "nc -zv 45.132.19.101 50051"
```

### 10.3 Живой прогон (D3, обязательный)

1. Открыть `http://localhost:8080`, загрузить одиночное фото → статус `pending` →
   через несколько секунд `done`.
2. В карточке фото убедиться, что **`model_version` не пустой** (значит ответ пришёл от
   реального анализатора, а не от заглушки) и заполнены `dominant_color`, `tags`,
   `eyes_closed_count`.
3. Загрузить батч 2–10 фото → дождаться `completed` → увидеть подсвеченный **лучший кадр**.
4. Открыть Grafana (`http://localhost:3000`) → показать графики
   (`photo_analysis_*`, `analyzer_image_prepared_bytes`, доля уменьшенных).
5. Приложить вывод `kubectl -n photo-service get all` к артефакту прогона.

### 10.4 Снос

```powershell
kind delete cluster --name photo   # убирает кластер, поды, PVC — всё
```

### 10.5 README (D4)

Раздел **«Запуск в Kubernetes локально»**:
- предусловия (docker, kind, kubectl; команды установки под Windows; лимит памяти Docker
  Desktop, §9.8);
- полная последовательность §10.1;
- как открыть UI и Grafana (§9.5, точные URL);
- как посмотреть логи: `kubectl -n photo-service logs -f deploy/worker`,
  `kubectl -n photo-service logs job/alembic-upgrade`;
- как пересобрать после правок кода (§9.3: rebuild → `kind load` → `rollout restart`);
- как всё снести (§10.4).

Раздел **«Запуск через docker compose»** (для разработки/тестов) — коротко: `docker compose
up --build`, фронт на `http://localhost:8080`, заглушка анализатора поднимается автоматически.

Раздел про сервер Sirius: одна фраза, что сервер используется только как хост внешнего
анализатора, SSH-ключи не коммитятся.

---

## 11. Наблюдаемость

### 11.1 Новые метрики (`app/integrations/metrics_worker.py`)

```
analyzer_image_prepared_bytes   Histogram  buckets: 64Ki,256Ki,1Mi,2Mi,3Mi,3.5Mi
analyzer_image_downscaled_total Counter
storage_read_errors_total       Counter    labels: code
```

Существующие `photo_analysis_failed_total{reason}` и `analyzer_grpc_errors_total{code}`
получат новые значения меток без правок кода. **Раскол метрик (F4) не трогаем**: все новые
метрики регистрируются только в `metrics_worker.py`, в `metrics_api.py` ничего не добавляем.

### 11.2 Логи (все с `trace_id` из существующего контекста)

- `INFO "image prepared"` — `photo_id`, `original_bytes`, `sent_bytes`, `downscaled`,
  `width`, `height`. **Содержимое файла и полный object_key не логируем** (constitution §3.3).
- `WARNING "analyzer message too large"` — `photo_id`, `sent_bytes`, `limit` — при
  превентивном отказе.
- `WARNING "analyzer unavailable, cooling down"` — `photo_id`, `cooldown_seconds`.
- Существующие `analysis claim won` / `analysis completed` / `analysis failed` не меняем.

### 11.3 Алерты (`prometheus/rules.yml`, новый файл, подключить в `prometheus.yml`)

| Алерт | Условие | Смысл |
|---|---|---|
| `AnalysisFailureRateHigh` | `rate(photo_analysis_failed_total[5m]) > 0.1` 10m | пайплайн деградирует |
| `AnalyzerMessageTooLarge` | `increase(analyzer_grpc_errors_total{code="MESSAGE_TOO_LARGE"}[15m]) > 0` | бюджет размера подобран неверно |
| `AnalyzerUnavailable` | `increase(analyzer_grpc_errors_total{code="UNAVAILABLE"}[5m]) > 10` | внешний анализатор лежит |
| `WorkerStalled` | `increase(worker_messages_processed_total[10m]) == 0 and photos_pending > 0` | консьюмер завис (§9.7) |

Дашборд Grafana дополнить панелями: `analyzer_image_prepared_bytes` (p50/p95) и доля
уменьшенных (`rate(analyzer_image_downscaled_total[5m])`).

---

## 12. Отказоустойчивость и деградация

- **Таймауты:** MinIO 60 с, подготовка изображения 30 с, gRPC 30 с (существующий
  `ANALYZER_GRPC_TIMEOUT`), БД без изменений.
- **Вежливость к общему анализатору (A7).** Backoff 1-2-4 с и `WORKER_MAX_ATTEMPTS=3`
  сохраняются. Дополнительно: после исчерпания попыток, когда последняя ошибка была
  `UNAVAILABLE` или `DEADLINE_EXCEEDED`, worker перед возвратом ждёт
  `ANALYZER_UNAVAILABLE_COOLDOWN_SECONDS` (новая настройка, default **5.0**). Это простой
  дроссель: при лежащем анализаторе поток сообщений в его адрес падает примерно на порядок,
  а очередь остаётся в Kafka и разберётся сама, когда сервис вернётся. Полноценный
  circuit breaker сознательно **вне скоупа** — он требует разделяемого состояния и заметного
  тестового бюджета ради учебного демо с одним внешним сервисом; зафиксировать как дефер.
- **Деградация при недоступности анализатора.** Загрузка фото продолжает работать (она
  зависит только от MinIO + БД + outbox), фото копятся в `pending`, `photos_pending`
  растёт, алерт срабатывает. Потери данных нет: сообщения лежат в Kafka, offset не
  коммитится для необработанных.
- **DLQ** остаётся вне скоупа (явно указано в спеке TASK-003); фиксируем, что роль
  «мёртвого письма» сейчас играет статус `failed` + `last_error_code` в БД.

---

## 13. Шаги для кодера

Порядок обязателен: блок A целиком должен быть зелёным до старта B/C/D.

> Примечание к редакции: блоки A (шаги A-1…A-17) и B (шаги B-1…B-7) уже реализованы и в этой
> ревизии не меняются — приведены для полноты. Переработаны только блок C и блок D под
> локальный кластер.

### Блок A — реальный анализатор

**A-1. `photo-service/protos/analyzer.proto`.**
Сменить `package photo.analyzer.v1;` → `package analyzer.v1;`. В `AnalyzePhotoRequest`
добавить `bytes image_bytes = 3;`. В `AnalyzePhotoResponse` добавить
`int32 eyes_closed_count = 5; string dominant_color = 6; repeated string tags = 7;
string model_version = 8;`. Обновить шапку-комментарий.
*Готово:* файл соответствует контракту из спеки TASK-003 дословно.

**A-2. `app/grpc_gen/analyzer_pb2.py`, `analyzer_pb2_grpc.py`.** Перегенерировать
(`grpc_tools.protoc`), применить задокументированную правку импорта
(`from app.grpc_gen import analyzer_pb2 as analyzer__pb2`).
*Готово:* полное имя метода — `/analyzer.v1.PhotoAnalyzer/AnalyzePhoto`.

**A-3. `pyproject.toml`.** Добавить `"pillow>=11.0,<12"`.
*Готово:* `uv sync` проходит, `ruff` чист.

**A-4. `app/core/config.py`.** Добавить `ANALYZER_MAX_MESSAGE_BYTES`,
`ANALYZER_MAX_IMAGE_BYTES`, `ANALYZER_MAX_IMAGE_SIDE`, `ANALYZER_MAX_IMAGE_PIXELS`,
`STORAGE_READ_TIMEOUT_SECONDS`, `IMAGE_PREP_TIMEOUT_SECONDS`,
`ANALYZER_UNAVAILABLE_COOLDOWN_SECONDS`, `CORS_ALLOWED_ORIGINS` + property
`cors_allowed_origins_list`.
*Готово:* `tests/test_config.py` дополнен.

**A-5. `app/services/image_prep.py` (СОЗДАТЬ).** Чистый модуль по §2.
*Готово:* без БД/сети; ранний возврат/лесенка/детерминизм.

**A-6. `app/integrations/analyzer_client.py`.** `analyze(..., image_bytes)`;
`AnalyzerMessageTooLarge`; `_is_message_too_large`; разделение RESOURCE_EXHAUSTED; обновить
TLS-комментарий F7.
*Готово:* `tests/test_analyzer_client.py` зелёный.

**A-7. `app/services/analysis_errors.py` (СОЗДАТЬ).** `classify_error` по §4.2.
*Готово:* чистая функция, покрыта.

**A-8. `app/services/analysis_processor.py`.** `storage` keyword-only; чтение+подготовка в
цикле с кешем; превентивная проверка размера; `classify_error`; cooldown; 4 новых поля в
`upsert`. Не менять порядок claim/цикл/write, skip-ветку, расстановку
`worker_messages_processed_total`, `_maybe_complete_batch`.
*Готово:* `tests/test_analysis_processor.py` зелёный.

**A-9. `app/worker/main.py`.** Создать `ObjectStorage`, передать `storage=` в процессор;
`ensure_bucket()` не вызывать.
*Готово:* `tests/test_worker_main.py` подтверждает передачу storage.

**A-10. `app/db/models.py`.** 4 новых nullable-колонки в `AnalysisResult` (`tags` — JSONB).
**A-11. `migrations/versions/v003_add_analyzer_extended_fields.py` (СОЗДАТЬ).** По §6.3.
**A-12. `app/repositories/analysis_result_repository.py`.** 4 kwargs в `upsert`,
`ON CONFLICT DO NOTHING` не менять.
**A-13. `app/schemas/photos.py`.** 4 новых поля в `AnalysisResultResponse` (дефолт `None`).
**A-14. `app/services/mappers.py`.** Прокинуть 4 поля.
**A-15. `app/services/batch_service.py`.** Новая формула §3, `SHARPNESS_THRESHOLD = 100.0`,
чистая функция, `get_batch` не трогать.
**A-16. `analyzer-stub/server.py`.** Новый контракт по §7.
**A-17. `app/integrations/metrics_worker.py`.** 3 новых метрики (§11.1), в `metrics_api.py`
ничего не добавлять.

### Блок B — веб-интерфейс

**B-1. `app/main.py`.** CORS по §8.5.
**B-2. `web/` инфраструктура (СОЗДАТЬ):** `Dockerfile`, `nginx.conf`, `docker-entrypoint.sh`,
`config.js.template` (envsubst `API_BASE_URL`/`SHARPNESS_THRESHOLD`/`POLL_INTERVAL_MS`).
**B-3. `web/html/js/api.js` (СОЗДАТЬ).** Обёртка fetch + разбор ошибок §8.4.
**B-4. `web/html/index.html` + `js/gallery.js` + `css/app.css` (СОЗДАТЬ).** Галерея + формы.
**B-5. `web/html/photo.html` + `js/photo.js` + `js/format.js` (СОЗДАТЬ).** Карточка §8.3.
**B-6. `web/html/batch.html` + `js/batch.js` (СОЗДАТЬ).** Страница батча, подсветка лучшего.
**B-7. `docker-compose.yml`.** Сервис `web` (`8080:80`, `API_BASE_URL=http://localhost:8000`,
`depends_on: api`), в `api` добавить `CORS_ALLOWED_ORIGINS: http://localhost:8080`.

### Блок C — Kubernetes (локальный кластер)

**C-1. `app/db/wait_for_schema.py` (СОЗДАТЬ).** Опрос `SELECT 1 FROM alembic_version` с
бэкоффом; exit 0 при успехе, 1 по таймауту (`WAIT_FOR_SCHEMA_TIMEOUT_SECONDS`, default 120).
Используется как initContainer в api и worker (§9.6).
*Готово:* `python -m app.db.wait_for_schema` завершается 0 на поднятой БД с миграциями.

**C-2. `photo-service/k8s/kind-cluster.yaml` (СОЗДАТЬ).** Конфиг kind: один control-plane
узел, образ `kindest/node:v1.31.x`, без `extraPortMappings` (доступ — port-forward).
*Готово:* `kind create cluster --name photo --config k8s/kind-cluster.yaml` поднимает кластер.

**C-3. `photo-service/k8s/*.yaml` (СОЗДАТЬ).** Все манифесты по составу §9.2:
- ConfigMap/Secret по §9.4 (внешний `ANALYZER_GRPC_ADDR`, `CORS_ALLOWED_ORIGINS=http://localhost:8080`);
- наши образы: тег `:local`, **`imagePullPolicy: IfNotPresent`** (§9.3) — обязательно, иначе
  `ErrImagePull`;
- `30-migrate-job.yaml` — Job, `backoffLimit: 3`, `ttlSecondsAfterFinished: 600` (§9.6);
- `31-api.yaml` — `replicas: 2`, initContainer `wait-for-schema`, команда **без**
  `alembic upgrade head`, пробы `/healthz`+`/readyz` (§9.7);
- `32-worker.yaml` — `replicas: 2` (комментарий-обоснование §9.9), initContainer
  `wait-for-schema`, проба `/metrics:8001`;
- `22-kafka.yaml` — `KAFKA_NUM_PARTITIONS: "3"`;
- `42-web.yaml` — образ `photo-web:local`, `API_BASE_URL=http://localhost:8000`;
- **нет** манифеста `analyzer-stub`, **нет** Ingress/NodePort;
- ресурсы/пробы/restart по §9.7–§9.8.
*Готово:* `kubectl apply --dry-run=client -f k8s/` без ошибок; на поднятом kind после
последовательности §10.1 `kubectl -n photo-service get all` — все поды Running/Completed,
вывод приложен к `30_impl.md`.

**C-4. `prometheus/rules.yml` (СОЗДАТЬ) + `prometheus.yml`.** Правила §11.3; дашборд
Grafana +2 панели §11.1. Прометей-конфиг в k8s берётся из ConfigMap `40-prometheus.yaml`
и должен указывать на `api:8000` и `worker:8001` внутри кластера (ClusterIP-сервисы).
*Готово:* правила видны в Prometheus UI → Alerts.

### Блок D — локальный запуск и демонстрация

**D-1. `.gitignore` (корень репозитория).** Добавить `*_sirius`, `*.pem`, `*.key`,
`student_ssh_keys*`, `.env`. (Server-compose и `.env.server` больше не нужны — из скоупа
исключены.)
*Готово:* `git status` не показывает ключей и `.env`.

**D-2. `photo-service/README.md`.** Разделы по §10.5: «Запуск в Kubernetes локально» (полная
последовательность §10.1, доступ к UI §9.5, логи, пересборка, снос), «Запуск через docker
compose», короткая заметка про сервер как хост анализатора. Плюс заметка про новую подготовку
изображения и новую формулу лучшего кадра со ссылкой на этот дизайн.
*Готово:* по README демонстратор поднимает локальный кластер с нуля и открывает UI.

**D-3. `docs/DEPLOYMENT.md` (СОЗДАТЬ или дополнить).** Валидация манифестов §9.10, состав
компонентов, схема доступа через port-forward, снос кластера.
*Готово:* документ самодостаточен.

**D-4. `specs/feature-upload/tasks.md`.** Заменить строку 105 таблицы «Зафиксированные
решения» текстом §3.6, с пометкой про редакцию TASK-003 и ссылкой на спайк.
*Готово:* спека и код говорят одно и то же.

> Прежние шаги «создать `docker-compose.server.yml`» и «`.env.server.example`» **отменены**
> (сервер вне скоупа).

---

## 14. Ожидаемые поломки тестов

| Тест | Что происходит | Что делать |
|---|---|---|
| `test_batch_service.py::test_is_blurred_false_beats_true_regardless_of_other_fields` | поведение удалено осознанно | **удалить**, заменить тестом «is_blurred не влияет на выбор» |
| `test_batch_service.py::test_lower_blur_score_wins_when_is_blurred_tied` | направление инвертировано | переписать в `test_higher_blur_score_wins_within_same_bucket` |
| `test_batch_service.py::test_more_faces_wins_when_is_blurred_and_blur_score_tied` | изменился набор фиксируемых полей | переписать: равные ведро и blur_score → больше лиц |
| `test_batch_service.py::test_earlier_created_at_wins_...`, `test_photo_id_is_final_deterministic_tiebreak_...` | ключи сохранены, фикстурам нужен `eyes_closed_count` | обновить фикстуры, логику не менять |
| `test_batch_service.py` (все) | фабрика `AnalysisResult` без новых полей | добавить `eyes_closed_count` в хелпер фикстур |
| **новые** в `test_batch_service.py` | — | ведро резкости (101 бьёт 99), `eyes_closed_count` третьим ключом, `NULL` глаза = 0 |
| `test_analyzer_client.py::...::test_transient_grpc_codes_are_retryable` | `RESOURCE_EXHAUSTED` без detail остаётся RETRY | мок отдаёт пустой `details()` |
| `test_analyzer_client.py::test_analyze_calls_stub_with_timeout_and_request_fields` | новая сигнатура + `image_bytes` | обновить вызов и ассерты |
| **новые** в `test_analyzer_client.py` | — | RESOURCE_EXHAUSTED с маркером → NO_RETRY/`MESSAGE_TOO_LARGE`; без маркера → RETRY; `AnalyzerMessageTooLarge` → NO_RETRY |
| `test_analysis_processor.py` (**весь файл**) | `storage` обязателен, `analyze()` +аргумент | fake-storage в фикстуру, замокать `prepare_for_analysis`, обновить ассерты |
| `test_analysis_processor.py::test_success_on_first_attempt_...` | в `upsert` 4 новых kwargs | обновить ожидаемые аргументы |
| `test_analysis_processor.py::test_backoff_sleep_called_between_retries_...` | добавился cooldown-`sleep` при UNAVAILABLE | считать только sleep-ы в цикле, отдельный тест на cooldown |
| `test_analysis_result_repository.py::test_executes_insert_with_all_fields` | расширился набор values | обновить ожидаемые поля |
| `test_analyzer_stub.py::test_blur_score_is_within_documented_range` | диапазон 0…1 → 1…100 000 | переписать под лог-шкалу |
| `test_analyzer_stub.py::test_is_blurred_matches_blur_score_threshold` | порог `>0.6` → `<100.0` | переписать |
| `test_analyzer_stub.py` (все) | `_analyze` принимает два аргумента | обновить вызовы |
| **новые** в `test_analyzer_stub.py` | — | все 8 полей, `eyes_closed_count <= faces_count`, валидный hex, пустые `image_bytes` → INVALID_ARGUMENT |
| `test_worker_main.py::test_run_starts_metrics_server_consumer_...` | в `_run` появился `ObjectStorage` | замокать `ObjectStorage`, проверить передачу |
| `test_migration_integration.py::test_upgrade_head_..._downgrade_chain_...` | `head` теперь v003 | проверка 4 колонок, `downgrade v002` и обратно |
| `test_app_boot.py` / `test_lifespan.py` | добавился CORS middleware | проверить, что порядок middleware не сломал ассерты |
| `test_config.py` | новые настройки | проверки дефолтов и парсинга `CORS_ALLOWED_ORIGINS` |
| **новый** `test_image_prep.py` | — | ранний возврат, лесенка, детерминизм, `ImageDecodeError`, `ImageTooLargeError`, альфа PNG |
| **новый** `test_analysis_errors.py` | — | все ветки таблицы §4.2 |
| **новый** `test_cors.py` | — | preflight с разрешённым и с чужим Origin |
| **новый** `test_wait_for_schema.py` | — | exit 0 при наличии `alembic_version`, таймаут при отсутствии |

Покрытие ≥95% (спека). Новый Python-код (`image_prep`, `analysis_errors`,
`wait_for_schema`) покрывается юнит-тестами; фронт и манифесты в покрытие не входят.
**Манифесты k8s и локальный деплой в CI/тестах не участвуют** — тесты не зависят ни от
кластера, ни от внешнего анализатора (заглушка — дефолт).

---

## 15. Что НЕ трогать

Свежие фиксы TASK-002.1 приняты двумя ревью — откат любого будет считаться регрессом.

1. **F1 — at-least-once consumer.** Неожиданное исключение из `process()` НЕ коммитит offset;
   poison-pill коммитит. Новые исключения (`ImageDecodeError`, `AnalyzerMessageTooLarge`)
   наружу из `process()` **не выходят** — классифицируются внутри → штатный `failed`.
2. **F2 — ранние лимиты загрузки.** `MAX_FILE_SIZE_BYTES = 50 МБ`,
   `BATCH_MAX_TOTAL_BYTES = 150 МБ`, капнутые чтения в `app/api/uploads.py` — без изменений.
3. **F3 — `GET /v1/batches/{id}` строго read-only.** `get_batch` не пишет в БД; завершение
   батча делает worker. Новая формула меняет только тело чистой `select_best_photo`.
4. **F4 — раскол метрик.** Новые метрики только в `metrics_worker.py`; ни один worker-модуль
   не импортирует `metrics_api`; `analysis_processor` импортирует `select_best_photo` из
   `batch_service`, а не из `photo_service`.
5. **F5 — компенсация MinIO-сироты** при падении commit.
6. **F6 — `restart: unless-stopped`** у всех сервисов compose (включая `web`).
7. **Публичный формат ошибок** `{error_code, message, request_id}` и набор кодов. Новые коды
   (`MESSAGE_TOO_LARGE`, `OBJECT_NOT_FOUND`, `IMAGE_DECODE_FAILED`, `IMAGE_TOO_LARGE`) —
   **внутренние** `photos.last_error_code`, наружу в HTTP не выходят.
8. **Идемпотентность `upsert`** (`ON CONFLICT DO NOTHING`) и атомарный захват
   `WHERE status='pending'`.
9. **Заглушка — дефолт compose и тестов.** Реальный анализатор включается только через
   `ANALYZER_GRPC_ADDR` (в k8s — ConfigMap). Тесты не ходят в сеть до `45.132.19.101`.
10. **Публичные HTTP-контракты**: пути, коды, поля существующих ответов. Новые поля анализа —
    совместимое расширение (дефолт `None`).
11. **Реализованные блоки A и B** (эта редакция их не трогает) — только Kubernetes/деплой.

---

## 16. Риски и альтернативы

| # | Риск | Вероятность / влияние | Смягчение и альтернатива |
|---|---|---|---|
| R1 | `blur_score` зависит от разрешения → уменьшенный и неуменьшенный кадры сравниваются не вполне честно | средняя / среднее | Первый ключ — грубое ведро, кадры батча обычно одного разрешения. **Альтернатива:** нормализовать ВСЕ кадры к 1600 px (вариант (б) D1); переключается константой. |
| R2 | Порог `SHARPNESS_THRESHOLD = 100.0` подобран по 5 замерам | средняя / среднее | Замеры уверенно разделяют группы. Порог — одна константа. **Альтернатива:** относительный порог по медиане батча — отвергнута (результат фото не должен зависеть от соседей). |
| R3 | Pillow меняет байты между версиями → недетерминизм | низкая / низкое | Минорный пин `>=11.0,<12`; тесты проверяют свойства, не хеши. |
| R4 | Детект «too large» по тексту detail — чужая строка может измениться | низкая / среднее | Второй эшелон; основной — превентивная проверка размера. Деградация до «3 ретрая» не хуже текущего. |
| R5 | `tags` как JSONB неудобен при будущем поиске | низкая / низкое | Переход на `TEXT[]` — одна миграция; GIN по JSONB доступен. |
| R6 | Память worker-а на 50-Мп входе | средняя / высокое | `ANALYZER_MAX_IMAGE_PIXELS`, `draft()`, освобождение оригинала, limit 1Gi. При OOM — снизить лимит пикселей до 25 Мп. |
| R7 | Проба worker-а по `/metrics` не ловит зависание asyncio-цикла | средняя / среднее | Алерт `WorkerStalled` (§11.3). **Альтернатива:** heartbeat-файл + exec-проба. |
| R8 | **kind не установлен / Docker Desktop с малым лимитом памяти** → поды `Pending` | средняя / среднее | README: команды установки kind/kubectl под Windows и требование поднять память Docker Desktop до ≥6Gi (суммарные requests ~3.4Gi, §9.8). **Альтернатива на крайний случай:** снизить `replicas` api/worker до 1 — уменьшает requests, ценой потери демонстрации масштабируемости. |
| R9 | Общий анализатор лежит/троттлит во время демо | средняя / высокое | Cooldown при UNAVAILABLE, backoff 1-2-4, `WORKER_MAX_ATTEMPTS=3`. **Запасной план демо:** временно завести в кластере заглушку (образ `analyzer-stub`, `kind load`, минимальный Deployment+Service) и переключить `ANALYZER_GRPC_ADDR` на неё — pipeline и UI работают полностью, отличается только `model_version`. Манифест заглушки держать под рукой, по умолчанию не применять. |
| R10 | **`imagePullPolicy`/тег заданы неверно** → `ErrImagePull` для локальных образов | средняя / высокое | Явный тег `:local` + `IfNotPresent` во всех трёх наших Deployment-ах, зафиксировано комментарием; README предупреждает про пересборку → `kind load` → `rollout restart`. |
| R11 | CORS-origin при port-forward не совпал (другой проброшенный порт) | средняя / низкое | `CORS_ALLOWED_ORIGINS` в ConfigMap = `http://localhost:8080`; README требует пробрасывать web на 8080, api на 8000. UI на CORS-отказ показывает внятное сообщение. |
| R12 | При правке кода демонстратор забыл пересобрать образ и `kind load` → в кластере старый код | средняя / среднее | README: явная памятка rebuild → `kind load` → `kubectl rollout restart`. Тег неизменный, поэтому без rollout-restart под не подхватит новые слои. |

---

## 17. Открытые вопросы к оркестратору

1. **Сообщить автору анализатора** о дефекте `is_blurred` (всегда `True`, включая
   `blur_score = 99 774`) и о служебном теге `dominant:#hex`, дублирующем отдельное поле.
   Наш дизайн от этого не зависит, но исправление полезно всем.
2. **Порог `SHARPNESS_THRESHOLD`** перепроверить на живом прогоне с реальным батчем 5–10
   кадров: если все кадры окажутся по одну сторону порога, формула выродится в
   `faces_count → eyes_closed → blur_score` (тоже допустимо, но стоит знать).
3. **Версия узла kind** (`kindest/node:v1.31.x`) — зафиксировать конкретный патч под
   установленную у демонстратора версию `kind`, чтобы образ узла точно скачался.

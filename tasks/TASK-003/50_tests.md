---
task_id: TASK-003
agent: test-writer
model: sonnet
status: done
inputs:
  - specs/constitution.md
  - specs/feature-upload/tasks.md (раздел TASK-003, «Лучший кадр батча» — редакция TASK-003)
  - tasks/TASK-003/05_spike_analyzer.md
  - tasks/TASK-003/20_design.md
  - tasks/TASK-003/30_impl.md
  - tasks/TASK-003/40_review-1.md (раздел «Что нужно для APPROVE», гейты, NB-4…NB-13)
  - tasks/TASK-003/41_review-2.md (раздел «Пробелы покрытия для test-writer»)
  - существующие tests/ (стиль pytest, asyncio_mode=auto, httpx ASGITransport, unittest.mock)
outputs:
  - tasks/TASK-003/50_tests.md
  - photo-service/tests/test_mappers.py (новый)
  - photo-service/tests/test_analyzer_stub_grpc.py (новый)
  - photo-service/tests/test_web_xss_regression.py (новый)
  - photo-service/tests/test_k8s_manifest_consistency.py (новый)
  - photo-service/tests/test_analysis_processor.py (дополнен)
  - photo-service/tests/test_photo_service.py (дополнен)
  - photo-service/tests/test_image_prep.py (дополнен)
timestamp: 2026-07-22T06:00:00Z
---

# TASK-003 · Тесты: отчёт test-writer

## Резюме

Оба ревью дали APPROVE (0 открытых блокеров). Основной объём критериев
приёмки уже был закрыт тестами кодера в рамках блоков A/B/C/D (формула
лучшего кадра, `image_prep`, классификация ошибок, CORS, `wait_for_schema` —
см. таблицу ниже, колонка «кем закрыт»). Моя работа — точечно закрыть
оставшиеся пробелы, явно перечисленные в задании оркестратора и в разделах
«Пробелы покрытия» обоих ревью:

1. `GET /v1/photos/{id}` — новые поля v003 и nullable-старые-строки (пункты 21–22) —
   **не было ни одного теста** ни на маппер, ни на сервис (только косвенное
   line-coverage через `test_batch_service.py`).
2. Реальный (не смоканный) таймаут вокруг `anyio.to_thread.run_sync` (пункт 20) —
   ревьюер-1 проверял это вручную прогоном, автотеста не было.
3. Скачивание из MinIO строго ПОСЛЕ атомарного захвата (пункт 18) — код это
   гарантирует структурой, но явного `storage.get_file.assert_not_called()`
   в skip-ветке не было.
4. `INVALID_ARGUMENT` при пустых `image_bytes` в analyzer-stub на уровне
   реального gRPC-вызова (пункт, отмеченный кодером как открытый вопрос и
   reviewer-2 как «для test-writer если понадобится»).
5. XSS-регрессия (был BLOCKING-3) — фикс подтверждён построчным чтением
   кода, но не был закреплён автотестом (пункт 24, JS-раннера в проекте нет).
6. Эквивалентность `prometheus/rules.yml` ↔ встроенной копии в
   `k8s/40-prometheus.yaml` (пункт 26) — заявлена в 30_impl.md как «сверено
   скриптом» разово, но не была закреплена как регрессионный тест.
7. Одна недостающая ветка `image_prep._to_rgb` (grayscale/не-RGB без альфы) —
   найдена по `--cov-report=term-missing` (была единственной непокрытой
   строкой в блоке A, `image_prep.py:86`).

Настоящих багов новым тестированием **не найдено** — код прошёл все
добавленные проверки с первого прогона (кроме одной ошибки в самом тесте —
исправлена, см. «Найденные при написании проблемы»).

---

## Матрица: критерий приёмки → тест

| № | Критерий (из задания оркестратора) | Тест(ы) | Кем закрыт |
|---|---|---|---|
| 1 | Резкий кадр (930.47) побеждает размытый (1.10) даже с бо́льшим числом лиц | `test_batch_service.py::TestSelectBestPhotoTieBreakOrder::test_sharp_bucket_beats_blurry_bucket_even_with_worse_other_fields` (на близких к спайку значениях) | кодер |
| 2 | Ведро резкости первично: `blur=101,faces=0` бьёт `blur=99,faces=5`; порог 100.0 включающий | тот же тест + `test_higher_blur_score_wins_within_same_sharpness_bucket` (порог `SHARPNESS_THRESHOLD + 1` включён в «резкие») | кодер |
| 3 | `is_blurred=False` у размытого кадра не спасает его (поле не в формуле) | `test_is_blurred_does_not_affect_the_outcome` | кодер |
| 4 | Внутри ведра: faces DESC → eyes ASC → blur DESC | `test_more_faces_wins_within_the_same_sharpness_bucket`, `test_fewer_closed_eyes_wins_when_bucket_and_faces_tied`, `test_higher_blur_score_wins_within_same_sharpness_bucket` | кодер |
| 5 | `NULL` в `eyes_closed_count` не роняет сортировку (= 0) | `test_null_eyes_closed_count_is_treated_as_zero` | кодер |
| 6 | Все failed → `None`; смешанный батч; один done | `TestSelectBestPhotoEmptyAndAllFailed.*`, `TestSelectBestPhotoSingleDone.test_only_done_photos_considered_in_mixed_batch` | кодер |
| 7 | Финальный тай-брейк по `photo_id` детерминирован | `test_photo_id_is_final_deterministic_tiebreak_on_complete_tie` (порядок-независимо) | кодер |
| 8 | Маленький файл проходит без изменений | `test_small_image_within_side_budget_is_returned_unmodified`, `test_small_rgba_png_within_budget_passes_through_without_flattening` | кодер |
| 9 | Большой файл проходит лесенку, укладывается в бюджет | `test_oversized_image_is_downscaled_to_fit_the_byte_and_side_budget`, `test_oversized_image_over_the_side_budget_but_under_byte_budget_is_still_downscaled` | кодер |
| 10 | Бюджет учитывает служебные байты (не ровно 4 MiB) | `test_analysis_processor.py::TestImagePreparation::test_message_too_large_after_preparation_fails_no_retry` (`_max_message_bytes` меньше `ANALYZER_MAX_MESSAGE_BYTES` на `_MESSAGE_OVERHEAD_BYTES`) | кодер |
| 11 | Decompression bomb отклоняется ДО полного decode | `test_pixel_count_over_the_limit_is_rejected_without_decoding` | кодер |
| 12 | Битый файл → доменная ошибка, не сырой `OSError` | `test_truncated_oversized_jpeg_raises_image_decode_error`, `TestImageDecodeError.*` | кодер |
| 13 | Детерминизм: одинаковый вход → байт-в-байт одинаковый выход | `test_repeated_calls_on_the_same_input_are_byte_identical` | кодер |
| 13b | Ветка `_to_rgb` для НЕ-RGB без альфы (grayscale) — единственная непокрытая строка блока A | **новый** `test_image_prep.py::TestDownscaleLadder::test_grayscale_image_downscale_path_is_converted_to_rgb` | **test-writer** |
| 14 | `MESSAGE_TOO_LARGE` (по detail и превентивно) → NO_RETRY | `test_analyzer_client.py::TestClassifyGrpcErrorMessageTooLarge.*`, `test_analysis_errors.py::TestDelegatedToAnalyzerClient.test_resource_exhausted_with_size_marker_is_no_retry`, `test_analysis_processor.py::TestImagePreparation::test_message_too_large_after_preparation_fails_no_retry` | кодер |
| 15 | Обычный `RESOURCE_EXHAUSTED` (троттлинг) → RETRY | `test_analysis_errors.py::test_resource_exhausted_without_size_marker_is_retryable` | кодер |
| 16 | Сетевой отказ MinIO (`MaxRetryError`, `ConnectionError`) → RETRY/`STORAGE_UNAVAILABLE`, `storage_read_errors_total` инкрементируется | `test_storage.py::test_get_file_transport_error_raises_storage_unavailable` (+ save_file/ensure_bucket), `test_analysis_errors.py::test_raw_urllib3_transport_error_is_retryable`/`test_raw_connection_error_is_retryable`, `test_analysis_processor.py::TestImagePreparation::test_storage_error_increments_storage_metric_not_analyzer_metric` (метрика) | кодер (регрессия BLOCKING-1) |
| 17 | `NoSuchKey` → постоянная ошибка | `test_storage.py::test_get_file_missing_object_raises_not_found_error`, `test_analysis_errors.py::test_not_found_error_is_no_retry_object_not_found` | кодер |
| 18 | Скачивание из MinIO ПОСЛЕ атомарного захвата | **новый** `test_analysis_processor.py::TestClaimSkip::test_rowcount_zero_never_downloads_from_storage` | **test-writer** |
| 19 | Подготовленные байты кешируются между попытками (один вызов storage) | `test_analysis_processor.py::TestSuccessPath::test_image_is_downloaded_and_prepared_only_once_across_retries` | кодер |
| 20 | Таймаут вокруг чтения реально прерывает ожидание | **новый** `test_analysis_processor.py::TestTimeoutsActuallyInterruptWaiting` (2 теста: MinIO-чтение и `prepare_for_analysis`, реальный `time.sleep` в потоке + реальный `asyncio.timeout`, проверка по wall-clock) | **test-writer** |
| 21 | `GET /v1/photos/{id}` у done-фото отдаёт `eyes_closed_count`, `dominant_color`, `tags`, `model_version` | **новый** `test_mappers.py::TestAnalysisToResponseNewFields`, **новый** `test_photo_service.py::TestGetPhoto::test_done_photo_response_includes_all_task_003_analysis_fields` | **test-writer** |
| 22 | Старые записи без новых полей не ломают ответ (nullable) | **новый** `test_mappers.py::TestAnalysisToResponseNullableOldRows` (включая реальную JSON-сериализацию Pydantic-модели), **новый** `test_photo_service.py::test_pre_v003_done_photo_with_null_new_fields_does_not_break_the_response` | **test-writer** |
| 23 | CORS preflight: разрешённый origin → успех; чужой → отказ; `allow_credentials` выключен | `test_cors.py` (4 теста) | кодер |
| 24 | XSS-регрессия (BLOCKING-3): `escapeHtml` экранирует все 5 символов; payload не вырывается из атрибута | **новый** `test_web_xss_regression.py` (9 тестов: анализ реального `.replace()`-чейна из исходника + повтор той же логики на конкретных пейлоадах + проверка отсутствия `dominant_color` в `style="..."` + проверка, что все интерполяции `filename` идут через `escapeHtml`) | **test-writer** |
| 25 | `wait_for_schema` — успех/таймаут/повтор | `test_wait_for_schema.py` (6 тестов) | кодер |
| 26 | `prometheus/rules.yml` ≡ встроенная копия в `k8s/40-prometheus.yaml` | **новый** `test_k8s_manifest_consistency.py::TestPrometheusRulesConsistency` (+ бонус: то же для дашборда/датасорса Grafana, `TestGrafanaProvisioningConsistency`) | **test-writer** |
| bonus | Реальный gRPC-раунд-трип к analyzer-stub, включая `INVALID_ARGUMENT` при пустых `image_bytes` (открытый вопрос кодера/reviewer-2) | **новый** `test_analyzer_stub_grpc.py` (4 теста: полный ответ, детерминизм по object_key, `INVALID_ARGUMENT`, конкурентные вызовы) — реальный `grpc.aio.server()` на эфемерном loopback-порту, без внешней сети | **test-writer** |

---

## Что покрыто / не покрыто

### Покрыто

- Формула лучшего кадра (`select_best_photo`) — построчно все 6 ключей сортировки,
  включая точные значения спайка A3 и все граничные/ничейные случаи.
- `image_prep.prepare_for_analysis` — 100% строк (было 98%, недостающая ветка
  `_to_rgb` для grayscale закрыта).
- Классификация ошибок (`analysis_errors.classify_error`,
  `analyzer_client.classify_grpc_error`/`error_code_from_exception`) — 100%,
  включая регрессию BLOCKING-1 (транспортные ошибки MinIO).
- `AnalysisProcessor` — 100%, включая реальные (не смоканные) таймауты и
  строгий порядок «захват → скачивание» через явную проверку отсутствия вызова.
- Мапперы новых полей v003 (`mappers.analysis_to_response`) — прямое
  покрытие, включая полную сериализацию Pydantic-модели с `NULL`-полями.
- Веб-интерфейс: CORS (существующий), XSS-регрессия escapeHtml (новый,
  на уровне анализа исходника — см. ограничения ниже).
- Инфраструктура: `wait_for_schema` (существующий), консистентность
  Prometheus/Grafana манифестов между compose и k8s (новый).
- analyzer-stub — и чистая функция `_analyze()` (существующий), и теперь
  реальный gRPC-контракт через loopback-сервер (новый), включая
  `INVALID_ARGUMENT`-ветку, которая раньше была написана, но не покрыта.

### Не покрыто (и почему)

- **`test_migration_integration.py`** — требует Docker/testcontainers (эфемерный
  Postgres), недоступен в этой песочнице. Штатно `skip`/`deselected`
  (`1 deselected` в каждом прогоне). Гейт проекта остаётся открытым до
  прогона на машине с Docker — не в моей власти закрыть это здесь.
- **Живой `kubectl apply`/`kind create cluster`** — блок C/D целиком
  вне скоупа юнит/интеграционного тестирования; `test_k8s_manifest_consistency.py`
  проверяет только текстовую эквивалентность встроенных копий конфигов,
  не поведение реального кластера (валидность через `--dry-run=server`,
  пробы, RBAC — за живым прогоном пользователя, D3).
- **Реальный браузерный/JS-раннер** — в проекте нет Node.js/JS-тестраннера,
  и задание явно просило не тащить новый без необходимости. XSS-тест
  вместо этого разбирает реальный `.replace()`-чейн из `format.js` регэкспом
  и повторяет ТУ ЖЕ логику в Python против конкретных пейлоадов — это
  надёжнее хардкод-ожидания (лечит регресс, если чейн переставят/уберут
  символ), но не заменяет полноценный DOM-тест (например, не проверяет
  фактическое поведение браузера при парсинге получившегося HTML).
- **Живой `docker compose up --build` / реальный gRPC до 45.132.19.101** —
  сознательно не делается нигде (требование задания): весь новый тест на
  gRPC (`test_analyzer_stub_grpc.py`) поднимает сервер на `127.0.0.1:0`
  (эфемерный порт), реального анализатора не касается.
- **Генерированный код** (`app/grpc_gen/analyzer_pb2*.py`) — частично не
  покрыт (63–77%), это protoc-сгенерированные файлы вне скоупа
  содержательного тестирования; не относится к блоку A бизнес-логики.
- **`if __name__ == "__main__":` guard-и** (`wait_for_schema.py:94`,
  `worker/main.py:110`) — по одной строке, тривиальный вызов `main()`,
  сам `main()` уже покрыт отдельными тестами (`TestMain`,
  `test_main_delegates_to_asyncio_run`).

---

## Найденные при написании проблемы

1. **Не баг, а ошибка в собственном тесте при первом прогоне** —
   `TestEscapeHtmlNoLongerUsesTheVulnerableTrick` изначально падал:
   регэксп искал строки `innerHTML`/`textContent` по всей функции, включая
   docstring-комментарий, который сам ОБЪЯСНЯЕТ, почему эти API больше не
   используются (упоминает их в прозе). Это ложное срабатывание в тесте,
   не в продовом коде — исправлено фильтрацией строк-комментариев перед
   поиском. Зафиксировано на случай, если кто-то будет чинить похожий тест
   в будущем: искать по коду, не по докстрингу.
2. **Настоящих багов в реализации блока A/B/C новыми тестами не найдено.**
   Все добавленные тесты (включая реальный gRPC-раунд-трип и реальные
   таймауты) прошли с первого прогона после исправления пункта 1 выше.
   Это ожидаемо: TASK-003 уже прошла два полных раунда ревью (opus нашёл
   и закрыл 3 BLOCKING, включая прогон против закрытого порта) — оставшиеся
   пробелы были именно «не покрыто автотестом», а не «код неверен».

---

## Как запустить

Все команды из `photo-service/`:

```bash
uv run ruff check .                                                   # чисто
uv run pytest -q -m "not integration"                                 # 533 passed, 1 deselected
uv run pytest -q -m "not integration" --cov=app --cov-report=term-missing
                                                                        # 99% (было 98% до этой сессии)
```

Отдельно новые файлы:

```bash
uv run pytest -q tests/test_mappers.py
uv run pytest -q tests/test_analyzer_stub_grpc.py
uv run pytest -q tests/test_web_xss_regression.py
uv run pytest -q tests/test_k8s_manifest_consistency.py
```

Интеграционный тест миграции (требует Docker, не запускался здесь, как и
у кодера/ревьюеров):

```bash
uv run pytest -m integration -q
```

Стабильность (детерминизм, независимость от порядка): полный набор
`-m "not integration"` прогнан 3 раза подряд — во всех трёх `533 passed,
1 deselected`, без флейков (включая новые тесты на реальные таймауты и
реальный gRPC-сервер на эфемерном порту).

---

## Результат прогона

| Метрика | До этой сессии | После |
|---|---|---|
| Тесты (`-m "not integration"`) | 503 passed, 1 deselected | **533 passed, 1 deselected** (+30) |
| `ruff check .` | чисто | чисто |
| Coverage (`--cov=app`) | 98% | **99%** |
| `app/services/image_prep.py` | 98% (1 строка) | **100%** |
| `app/services/analysis_errors.py` | 100% | 100% |
| `app/services/analysis_processor.py` | 100% | 100% |
| `app/services/batch_service.py` | 100% | 100% |
| `app/services/mappers.py` | 100% (косвенно) | 100% (прямое поведенческое покрытие) |

Новые тест-файлы (4) + точечные дополнения в 3 существующих (30 новых
тестов суммарно): `test_mappers.py` (5), `test_analyzer_stub_grpc.py` (4),
`test_web_xss_regression.py` (9), `test_k8s_manifest_consistency.py` (6),
`test_analysis_processor.py` (+3: скип без скачивания, 2×реальный таймаут),
`test_photo_service.py` (+2: done-фото с новыми полями, pre-v003 nullable),
`test_image_prep.py` (+1: grayscale-ветка `_to_rgb`).

**Статус: все тесты зелёные.** Красных тестов нет и не было ни на одном
этапе (после исправления собственной ошибки в тесте, см. выше).

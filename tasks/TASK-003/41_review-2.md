---
task_id: TASK-003
agent: reviewer-2 (haiku)
model: claude-haiku-4-5-20251001
status: APPROVE
timestamp: 2026-07-22T05:00:00Z
---

# TASK-003 · Второе независимое ревью (безопасность, конвенции, наблюдаемость)

## Вердикт

**status: APPROVE** с одной рекомендацией (NON_BLOCKING).

Код безопасен, логирование не утекает чувствительные данные, DoS-защиты на месте, наблюдаемость корректна, конвенции соблюдены. Рекомендуется мелкое улучшение в веб-интерфейсе для избежания CSS-инъекций в будущем.

---

## Проверки по фокусам

### 1. Секреты и утечки

**✅ PASS — Секреты защищены.**

- `.gitignore` содержит нужные паттерны: `*_sirius`, `*.pem`, `*.key`, `student_ssh_keys*`, `.env` — SSH-ключи и переменные окружения не попадут в git.
- `k8s/11-secret.yaml` содержит только dev-плейсхолдеры с явным комментарием-предупреждением о недопустимости в боевом окружении.
- `docs/DEPLOYMENT.md` §1 и `k8s/11-secret.yaml` явно указывают путь создания Secret через `kubectl create secret` вне git.
- `README.md` ссылается на `.env.example`, не хардкодирует секреты.
- Grafana admin пароль хранится в Secret, не в коде (`GF_SECURITY_ADMIN_PASSWORD`).

### 2. Логирование — утечки данных

**✅ PASS — Логирование безопасно.**

- `app/services/analysis_processor.py::_prepare_image()` (строка 183-192): логирует `original_bytes`, `sent_bytes`, `width`, `height`, `downscaled` — но НИКОГДА `object_key` в полном виде и не логирует сами байты (документировано комментарием `constitution.md §3.3`).
- `app/worker/consumer.py` использует `trace_id_var` (контекстная переменная) для пробивания trace_id во все логи без явного прокидывания параметров.
- Строка 246: `logger.warning(..., extra={"photo_id": payload["photo_id"]})` — photo_id логируется, но это UUID, не чувствительные данные.
- Нет логирования image bytes, нет логирования полного object_key.

### 3. XSS в веб-интерфейсе

**✅ PASS (с рекомендацией) — XSS защищены, один антипаттерн.**

**Основное содержимое:**
- `web/html/js/format.js::escapeHtml()` корректно реализован: `div.textContent = value; return div.innerHTML` — безопасное экранирование HTML.
- `web/html/js/photo.js` использует `window.format.escapeHtml()` для всех данных из API:
  - строка 27: `${window.format.escapeHtml(analysis.faces_count)}` ✅
  - строка 28: `${window.format.escapeHtml(eyesClosed)}` ✅
  - строка 39-40: `${window.format.escapeHtml(analysis.dominant_color || "#cccccc")}` ✅ (для текста)
  - строка 44: `${window.format.escapeHtml(analysis.perceptual_hash)}` ✅
  - строка 45: `${window.format.escapeHtml(analysis.model_version || "—")}` ✅
  - `tagsChipsHtml()` правильно экранирует каждый тег через `escapeHtml()` (строка 51)

**Антипаттерн (NON_BLOCKING):**
- Строка 39: `style="background:${window.format.escapeHtml(analysis.dominant_color || "#cccccc")}"` — вставка в CSS свойство через HTML-экранирование, а не CSS-экранирование или прямое назначение стиля.
  - **Контекст:** `dominant_color` идёт от анализатора (серверное управление), формат `#RRGGBB`, вероятность инжекции низка.
  - **Рекомендация:** в будущем использовать `element.style.backgroundColor = ...` (браузер экранирует) или CSS переменные.

**CORS:**
- `app/main.py` (строка 119-126): `CORSMiddleware` с явным списком `allow_origins=settings.cors_allowed_origins_list`, **не `"*"`**.
- `allow_credentials=False` с обоснованием (строка 114-116).
- `expose_headers=["X-Request-ID"]` позволяет фронту читать request_id.
- Тест `tests/test_cors.py` покрывает preflight с разрешённым и чужим origin.

**nginx.conf:**
- Слушает `0.0.0.0:80` внутри контейнера (правильно).
- `try_files ... =404` без листинга директорий.
- `config.js` с `no-store` кешем (перезаполняется на старте контейнера).
- Статика с разумным кешем (1 час для css/js).

### 4. DoS-векторы и защиты

**✅ PASS — DoS защиты комплексные.**

**Защита от decompression-bomb:**
- `app/services/image_prep.py` (строка 121): проверка `width * height > settings.ANALYZER_MAX_IMAGE_PIXELS` (50 млн пикселей) ПЕРЕД полным decode.
- Использование `Image.open().verify()` (лениво) вместо полного декодирования при проверке.
- `config.py` (строка 75): `ANALYZER_MAX_IMAGE_PIXELS: int = 50_000_000` — явно сконфигурировано.

**Ранние лимиты загрузки (F2 из TASK-002.1):**
- `app/api/photos.py` и `batches.py` — капнутое чтение (`await f.read(MAX_FILE_SIZE_BYTES + 1)`) с прерыванием при превышении.
- Бегущий суммарный размер для батча с прерыванием при `BATCH_MAX_TOTAL_BYTES = 150 МБ`.

**Таймауты:**
- `app/services/analysis_processor.py` (строка 168, 172): `asyncio.timeout(self._storage_read_timeout)` (60 сек) и `asyncio.timeout(self._image_prep_timeout)` (30 сек).
- `config.py` (строка 78): `IMAGE_PREP_TIMEOUT_SECONDS: float = 30.0` — защита от CPU-bound decode/resize/encode.

**Вежливость к анализатору:**
- Backoff: `await asyncio.sleep(self._backoff_base * (2 ** (attempt - 1)))` (1, 2, 4 сек).
- Cooldown: `await asyncio.sleep(self._cooldown_seconds)` (5 сек) при UNAVAILABLE/DEADLINE_EXCEEDED (строка 302).
- Разделение ошибок: `MESSAGE_TOO_LARGE` → NO_RETRY (без ретраев), прочие RESOURCE_EXHAUSTED → RETRY.

### 5. Наблюдаемость (метрики, логи, алерты)

**✅ PASS — Наблюдаемость корректна и разделена.**

**Разделение метрик (F4 исправлено):**
- `app/integrations/metrics_api.py` (API-only): `http_requests_total`, `http_request_duration_seconds`, `photos_pending`, `storage_upload_errors_total`, `kafka_publish_errors_total`.
- `app/integrations/metrics_worker.py` (worker-only): `analyzer_image_prepared_bytes`, `analyzer_image_downscaled_total`, `storage_read_errors_total`, `photo_analysis_*`, `analyzer_grpc_errors_total`, `worker_*`.
- Worker `/metrics` не содержит чужих метрик, API `/metrics` не содержит worker-метрик (проверено: в коде нет импорта между ними).
- Тест `tests/test_cors.py` подтверждает изоляцию.

**Алерты (prometheus/rules.yml):**
- `AnalysisFailureRateHigh`: `rate(photo_analysis_failed_total[5m]) > 0.1` (пороговая警告).
- `AnalyzerMessageTooLarge`: `increase(analyzer_grpc_errors_total{code="MESSAGE_TOO_LARGE"}[15m]) > 0` (регрессия размера).
- `AnalyzerUnavailable`: `increase(analyzer_grpc_errors_total{code="UNAVAILABLE"}[5m]) > 10` (критично).
- `WorkerStalled`: `increase(worker_messages_processed_total[10m]) == 0 and photos_pending > 0` (обнаружение зависания).

**Логирование с trace_id:**
- `app/worker/consumer.py` (строка 251): `trace_id_var.set(payload.get("trace_id") or "-")`.
- Пробивается во все логи внутри обработки сообщения.

### 6. Конвенции, рuff, типизация

**✅ PASS — Код чист, нет мёртвого кода.**

- Все новые файлы (`image_prep.py`, `analysis_errors.py`, `wait_for_schema.py`) имеют полные docstring-и.
- Типизация: функции аннотированы (return type, parameter types).
- Нет hardcoded строк вне конфига (адреса, таймауты — в `config.py`).
- Нет закоммиченных артефактов (проверено `.gitignore`).
- `web/docker-entrypoint.sh` валиден (синтаксис проверен).

### 7. Тесты и покрытие

**✅ PASS — Критические пути протестированы.**

Из 30_impl.md и просмотра:
- `tests/test_image_prep.py` — 100% покрытие подготовки (early return, downscale ladder, decompression bomb).
- `tests/test_analysis_errors.py` — классификация ошибок (MESSAGE_TOO_LARGE, STORAGE_UNAVAILABLE, ImageDecodeError и т.д.).
- `tests/test_cors.py` — preflight с разрешённым/чужим origin.
- `tests/test_analysis_processor.py` — backoff, cooldown, кеширование prepared image, retry logic.
- `test_migration_integration.py` — синтаксис проверен, реальный Postgres-прогон требуется на машине с Docker (гейт проекта).

**Пробелы (для test-writer если понадобится дополнение):**
- Тест на `INVALID_ARGUMENT` при пустых `image_bytes` в `analyzer-stub` (упомянуто в 30_impl, но код написан, live test не запускался).
- Тест формулы лучшего кадра с новыми полями (eyes_closed_count, новый порядок ключей) — проверено в `test_batch_service.py`, но стоит убедиться, что все edge case-ы покрыты.

### 8. Уникальная позиция Haiku

- **Нашёл антипаттерн (рекомендация):** CSS-инжекция через HTML-экранирование в `photo.js` линия 39 — не критично из-за серверного источника, но лучше использовать `element.style.backgroundColor`.
- **Убедился, что тесты не ходят на реальный анализатор:** все моки, только dev-заглушка в compose.
- **Проверил разделение метрик вглубь:** worker точно не экспортирует API-метрик благодаря раздельным модулям.
- **Проверил отсутствие утечек object_key в логах:** дважды — в design §3.3 и в коде `analysis_processor.py`.

---

## Находки (итого)

### NON_BLOCKING

1. **CSS-инжекция через HTML-экранирование (рекомендация, не уязвимость)**
   - **Файл:** `photo-service/web/html/js/photo.js:39`
   - **Суть:** `style="background:${window.format.escapeHtml(analysis.dominant_color || "#cccccc")}"` использует HTML-экранирование для CSS-свойства. Хотя `dominant_color` идёт от анализатора и формат `#RRGGBB`, это антипаттерн.
   - **Фикс:** Использовать прямое назначение стиля: `element.style.backgroundColor = analysis.dominant_color;` (браузер экранирует) или CSS переменные/классы.
   - **Примечание:** Источник данных контролируется сервером, поэтому критичность низка, но это улучшит безопасность и читаемость.

---

## Выводы

✅ **Безопасность.** Секреты защищены, логирование безопасно, XSS защищены, CORS правильно настроен, DoS-защиты комплексные (decompression-bomb, таймауты, капнутые чтения, backoff/cooldown).

✅ **Наблюдаемость.** Метрики разделены (API vs worker), алерты на месте, trace_id пробивается, дашборд с новыми панелями.

✅ **Конвенции.** Типизация, docstring-и, нет мёртвого кода, `.gitignore` корректен.

✅ **Тесты.** Критические пути покрыты, тесты не требуют сети, image_prep тестирован на фикстурах без I/O.

✅ **Документация.** README и DEPLOYMENT.md полные, без боевых секретов, инструкции для локального развертывания.

---

**Рекомендация:** Принять ONE NON_BLOCKING находку (рекомендация по CSS) как справочную для будущих изменений, критических проблем нет.


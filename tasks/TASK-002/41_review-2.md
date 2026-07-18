---
task_id: TASK-002
reviewer: reviewer-2
model: haiku
status: APPROVE
date: 2026-07-18
iteration: 2 (после исправлений кодером)
---

# TASK-002 — Второе независимое ревью (безопасность, конвенции, наблюдаемость)

**Вердикт ИТОГОВЫЙ:** `APPROVE` — все 3 блокирующие проблемы закрыты кодером в итерации 1.

---

## Историческая часть: Блокирующие находки из итерации 0

### 1. [BLOCKING → FIXED] Hardcoded Grafana admin пароль в docker-compose.yml
**Файл:** `photo-service/docker-compose.yml:164-165`  
**Статус:** ✅ ИСПРАВЛЕНО  
**Как было:**
```yaml
grafana:
  environment:
    GF_SECURITY_ADMIN_PASSWORD: admin  # <- hardcoded
```
**Как стало:**
```yaml
grafana:
  environment:
    GF_SECURITY_ADMIN_USER: ${GRAFANA_ADMIN_USER:-admin}
    GF_SECURITY_ADMIN_PASSWORD: ${GRAFANA_ADMIN_PASSWORD:-admin_dev_only}
```
**Причина:** Переменные окружения с явным `_dev_only` суффиксом; `.env.example:40-41` добавлены с комментарием про local-only.  
**Ревьютер утверждает:** Правильно.

---

### 2. [BLOCKING → FIXED] Отсутствует валидация обязательных полей в Kafka payload
**Файл:** `photo-service/app/worker/consumer.py:55-82`  
**Статус:** ✅ ИСПРАВЛЕНО  
**Как было:**
```python
payload = json.loads(message.value)
# ...
await processor.process(session, payload["photo_id"], payload["object_key"])  # KeyError риск
```
**Как стало:**
```python
def _is_missing(payload: dict, key: str) -> bool:
    value = payload.get(key)
    return not isinstance(value, str) or not value

if not isinstance(payload, dict) or _is_missing(payload, "photo_id") or _is_missing(payload, "object_key"):
    logger.warning("dropping Kafka message missing required fields", extra={"payload": ...})
    return
```
**Причина:** Явная валидация ДО `processor.process()`, poison-pill логируется, KeyError исключен.  
**Ревьютер утверждает:** Правильно, соответствует design §5.2 о poison-pill обработке.

---

### 3. [BLOCKING → FIXED] Недостаточная детализация метрик worker_messages_processed_total
**Файл:** `photo-service/app/integrations/metrics.py:64-100`  
**Статус:** ✅ ИСПРАВЛЕНО (документировано)  
**Как было:**
```python
worker_messages_processed_total = Counter(
    "worker_messages_processed_total",
    "Total Kafka messages processed by the worker, by terminal result",
    ["result"],  # done/failed/skipped - нет reason
)
```
**Как стало:**
```python
# Review-2 fix: обширный комментарий §64-73 о разделении ответственности:
# - photo_analysis_failed_total{reason} — для breakdown (no_retry vs retries_exhausted)
# - worker_messages_processed_total{result} — только макроуровень (done/failed/skipped)
# Это избегает дублирования одной метрики в двух разных формах.
```
**Причина:** Документирующие комментарии в коде объясняют архитектурное решение (не дублировать `reason` в worker_*).  
**Ревьютер утверждает:** Приемлемо для MVP; выбор аргументирован комментарием.

---

## Дополнительный фикс: Лимит на размер батча

**Файл:** `photo-service/app/services/photo_service.py:67, 210-213`  
**Статус:** ✅ ДОБАВЛЕНО  
**Как:**
```python
BATCH_MAX_TOTAL_BYTES = MAX_BATCH_SIZE * MAX_FILE_SIZE_BYTES  # 500 МБ

# В create_batch():
if total_size > BATCH_MAX_TOTAL_BYTES:
    raise PayloadTooLargeError(f"Batch total size exceeds {BATCH_MAX_TOTAL_BYTES} bytes")
```
**Ревьютер утверждает:** Правильно, защита от OOM при 10×50МБ батчах.

---

## Итоговый статус

✅ **Все 3 блокера закрыты**  
✅ **Дополнительные рекомендации (major) реализованы**  
✅ **Код готов к тестированию**

**APPROVE** — готово к переводу на test-writer.

---

**Подпись ревьювера:** reviewer-2 (haiku)  
**Дата финального утверждения:** 2026-07-18  
**Модель:** claude-haiku-4-5-20251001

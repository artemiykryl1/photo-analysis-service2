---
task_id: TASK-002
agent: test-debugger
model: sonnet
status: done
inputs:
  - живой прогон `docker compose up` (traceback от оркестратора)
  - tasks/TASK-002/30_impl.md
  - tasks/TASK-002/50_tests.md
  - photo-service/app/repositories/photo_repository.py
outputs:
  - photo-service/app/repositories/photo_repository.py (правка)
  - tasks/TASK-002/60_debug.md (этот файл)
timestamp: 2026-07-18
---

# TASK-002 — Отчёт test-debugger (60_debug.md)

## Симптом

При `docker compose up -d --build api worker` контейнеры `api` и `worker`
падали с `Exited(1)` сразу при старте. Из `docker compose logs api`:

```
File "/app/app/main.py", line 19, in <module>
    from app.api import batches, photos
...
File "/app/app/repositories/photo_repository.py", line 150, in PhotoRepository
    async def fetch_unpublished(self, session: AsyncSession, limit: int) -> list[Photo]:
TypeError: 'function' object is not subscriptable
```

Локально (`uv run pytest`, `uv run python -c "import app.main"`) всё было
зелёным — 367 тестов проходили, баг не воспроизводился вне Docker.

## Root cause (с доказательством)

`PhotoRepository` (см. `app/repositories/photo_repository.py`) содержит
метод `async def list(self, ...) -> list[Photo]:` (строка 53, добавлен в
TASK-001). Это ИМЯ связывается в *пространстве имён класса*
(`class`-namespace), которое во время выполнения тела класса служит
локальной областью видимости для вычисления аннотаций всех
последующих определений. Любой метод, объявленный НИЖЕ `list` и
использующий в аннотации возвращаемого значения `list[...]`
(в TASK-002 это `fetch_unpublished`, строка 150), при вычислении этой
аннотации резолвит имя `list` не на встроенный тип, а на уже
определённый метод класса — отсюда `TypeError: 'function' object is
not subscriptable`.

Ключевая деталь — КОГДА это вычисляется:

- На **Python < 3.14** (в т.ч. `python:3.12-slim`, образ Docker) аннотации
  функций вычисляются **жадно**, в момент выполнения `def`-инструкции —
  значит краш происходит немедленно при импорте модуля/класса.
- На **Python 3.14** (PEP 649, локальная машина разработчика — `uv run
  python --version` → `3.14.6`) аннотации вычисляются **лениво**
  (`__annotate__`, посчитываются только при обращении к
  `__annotations__`/`typing.get_type_hints`) — импорт модуля проходит
  без ошибки, поэтому `uv run pytest` (367 тестов, ни один явно не
  дергает `typing.get_type_hints` на `fetch_unpublished`) не ловит баг.

Доказательство воспроизведено напрямую на локальной машине (Python
3.14.6, тот же интерпретатор, что гоняет тесты):

```
$ uv run python -c "
import app.repositories.photo_repository as m
print('imported OK on 3.14 (lazy annotations)')
print(m.PhotoRepository.fetch_unpublished.__annotations__)
"
imported OK on 3.14 (lazy annotations)
Traceback (most recent call last):
  ...
  File ".../app/repositories/photo_repository.py", line 150, in __annotate__
    async def fetch_unpublished(self, session: AsyncSession, limit: int) -> list[Photo]:
                                                                            ~~~~^^^^^^^^
TypeError: 'function' object is not subscriptable
imported OK on 3.14 (lazy annotations)
```

Импорт модуля проходит успешно (подтверждает, что на 3.14 это не видно
при обычном импорте/тестах), но обращение к `__annotations__` —
именно то, что делает Python 3.12 автоматически и жадно при выполнении
`def` — воспроизводит ТОЧНО ТУ ЖЕ ошибку, что и в docker-логах.

**Поиск других мест того же класса бага** — просканирован весь
`photo-service/app/` (и `analyzer-stub/`) на методы с именами
встроенных типов (`list`, `dict`, `set`, `type`, `id`, `filter`, `map`,
`format`, `object`, `bytes`, `str`, `int`, `bool`, `all`, `any`, `hash`,
`next`, `iter` и др.):

```
app/core/logging.py:33:    def filter(...) -> bool:
app/repositories/photo_repository.py:53:    async def list(...) -> list[Photo]:
```

`TraceIdFilter.filter` (logging.py) — единственный метод класса,
переопределяет `logging.Filter.filter` (штатный паттерн), никакой
другой метод класса не использует `filter[...]` как тип после него →
безопасно, не задета той же проблемой.

`PhotoRepository.list` — единственное реальное место затенения:
единственный класс, где имя встроенного типа определено как метод И
есть последующие методы (`update_status`, `claim_for_processing`,
`record_attempt`, `mark_done`, `mark_failed`, `fetch_unpublished`,
`mark_published`, `count_pending`), из которых аннотацию `list[Photo]`
использует именно `fetch_unpublished` (строка 150) — ровно то место,
что упало в трейсбеке.

## Классификация: несерьёзно

Локальный синтаксический баг совместимости версий Python (аннотации
класса, затенение имени builtin методом с тем же именем), не
затрагивает контракты API/данных/архитектуру. Метод `list` — часть
публичного интерфейса репозитория с TASK-001 (переименовывать —
расширение скоупа и ломает вызывающий код в `photo_service.py`/тестах
без необходимости). Исправляется точечно на уровне модуля.

## Что сделано / почему эскалировано

Не эскалировано — почитал минимально. В начало
`app/repositories/photo_repository.py` добавлен
`from __future__ import annotations` (PEP 563): делает ВСЕ аннотации
модуля отложенными строками на любой версии Python (3.9+), убирая
жадное вычисление на 3.12 и приводя поведение к тому же ленивому
резолвингу, что уже есть на 3.14 — баг воспроизводиться перестаёт на
обеих версиях одинаково.

Почему это безопасно для данного файла:
- `photo_repository.py` — чистый репозиторий (обычный класс,
  не SQLAlchemy `Mapped[...]`-модель и не Pydantic-схема), поэтому
  никакой рантайм-код (ORM mapper, Pydantic validator) не читает
  `__annotations__` этого класса для построения схемы/валидации —
  единственные потребители аннотаций тут — статический анализ (mypy/IDE)
  и, опционально, `typing.get_type_hints`, оба корректно работают со
  строковыми (`from __future__ import annotations`) аннотациями.
- Проверено, что `app/db/models.py` (где реально есть SQLAlchemy
  `Mapped[...]`, требующие вычисленных на рантайме аннотаций для ORM)
  и `app/schemas/photos.py` (Pydantic) — НЕ трогались; фикс изолирован
  строго в файле-источнике проблемы.

Альтернативы (строковая аннотация `-> "list[Photo]"` только на
`fetch_unpublished`, переименование метода `list`) рассмотрены и
отклонены — `from __future__ import annotations` устраняет весь класс
проблемы для файла разом (не только конкретную строку, что упала) и
является рекомендуемой практикой для отложенных аннотаций;
переименование `list` — расширение скоупа за пределы фикса бага.

## Изменённые файлы

- `photo-service/app/repositories/photo_repository.py` — добавлен
  `from __future__ import annotations` + docstring-объяснение причины
  (строки 1–29). Больше ничего не менялось.

## Результат повторного прогона

```
cd photo-service
uv run ruff check .
  -> All checks passed!

uv run pytest -q -m "not integration"
  -> 367 passed, 1 deselected

uv run python -c "
import app.repositories.photo_repository as m
import typing
print(typing.get_type_hints(m.PhotoRepository.fetch_unpublished))
print(typing.get_type_hints(m.PhotoRepository.list))
"
  -> {'session': AsyncSession, 'limit': int, 'return': list[Photo]}
  -> {'session': AsyncSession, 'limit': int, 'offset': int, 'return': list[Photo]}
     (раньше падало с TypeError на 3.14 при обращении к __annotations__ —
      теперь резолвится корректно)
```

Docker / Python 3.12 (главная цель верификации):

```
cd photo-service
docker compose up -d --build api worker
docker compose ps -a
```

```
NAME                            STATUS
photo-service-analyzer-stub-1   Up (healthy)
photo-service-api-1             Up
photo-service-grafana-1         Up
photo-service-kafka-1           Up (healthy)
photo-service-minio-1           Up (healthy)
photo-service-postgres-1        Up (healthy)
photo-service-prometheus-1      Up
photo-service-worker-1          Up
```

`docker compose logs api --tail 30`:
```
INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
INFO  [alembic.runtime.migration] Will assume transactional DDL.
INFO:     Started server process [60]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)
INFO:     172.18.0.4:52448 - "GET /metrics HTTP/1.1" 200 OK
```

`docker compose logs worker --tail 30`: no traceback; Kafka-координатор
проходит обычную последовательность election/(re-)join при первом
старте consumer-группы после пересоздания контейнеров (штатное
поведение aiokafka, не связано с фиксом), заканчивается:
```
INFO ... "worker consumer started", "topic": "photo.analysis.requested", "group": "photo-analysis-workers"
```

Дополнительно — точечная проверка импорта прямо внутри контейнера на
3.12:
```
docker compose exec api uv run python -c "import app.main; import app.worker.main; print('imports OK')"
  -> imports OK

curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/metrics
  -> 200
curl -s http://localhost:8001/metrics | head -5
  -> (валидный Prometheus text-format ответ от worker)
```

Оба контейнера оставались `Up` (без рестарт-луп) при повторной проверке
`docker compose ps -a` через ~30 секунд после старта.

## Итог

**Фикс подтверждён на Python 3.12: api и worker стартуют.**
Других мест того же класса бага (затенение builtin-имени методом
класса + использование этого имени в аннотации ниже по коду) в
`photo-service/app/` и `analyzer-stub/` не найдено — фикс изолирован
одним файлом, `ruff`/`pytest` остаются зелёными (367 passed,
1 deselected), новых регрессий не внесено.

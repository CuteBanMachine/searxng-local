# searxng-local

Встраиваемый локальный [SearXNG](https://github.com/searxng/searxng) для Python-проектов.
Без Docker: класс сам устанавливает SearXNG (при первом запуске), запускает процесс
в конструкторе, даёт асинхронный поиск через JSON API и гарантированно останавливает
процесс при выходе.

- **Платформа:** Windows 10/11 x64
- **Python:** 3.11+
- **Зависимости пакета:** [`httpx`](https://www.python-httpx.org/) — единственная
- **Системные требования:** `git` в PATH (для загрузки исходников SearXNG), доступ в интернет при первой установке

## Быстрый старт

```powershell
# из каталога проекта, куда хотите встроить поиск
pip install -e C:\Proj\searxng-local
```

```python
import asyncio
from searxng_local import SearXNG

# Первый запуск: git clone upstream SearXNG -> ./searxng-local/repo,
# venv -> ./searxng-local/.venv, pip, Windows-патч, settings.yml (минуты, сеть).
# Дальнейшие запуски мгновенны.
with SearXNG() as sx:                      # запуск процесса + ожидание готовности
    data = asyncio.run(sx.search("python 3.11 what's new", categories="it"))
    for item in data["results"][:5]:
        print(item["title"], item["url"])
# выход из with -> процесс SearXNG остановлен
```

Полностью async-вариант:

```python
async def main():
    async with SearXNG() as sx:
        data = await sx.search("hello", engines=["bing", "qwant"], safesearch=1)
        return data["results"]

asyncio.run(main())
```

## API

### `SearXNG(install_dir=None, *, port=8888, settings_path=None, start_timeout=60.0, auto_install=True)`

| Параметр | Значение |
|---|---|
| `install_dir` | каталог установки; по умолчанию `<cwd>/searxng-local` |
| `port` | порт; **фиксируется в `settings.yml` при первой установке** (один инстанс на установку). Несовпадение при следующих запусках → `ConfigMismatchError` |
| `settings_path` | свой `settings.yml`; порт тогда читается из файла, параметр `port` игнорируется |
| `start_timeout` | сколько секунд ждать готовности SearXNG |
| `auto_install` | `False` → `NotInstalledError` вместо автоматической установки |

Конструктор синхронно: устанавливает (если нужно), проверяет, что порт свободен
(иначе `PortInUseError`), запускает процесс и ждёт `GET /healthz`.

### `await sx.search(query, *, categories, engines, language, pageno, safesearch, time_range, timeout=30.0)`

`GET /search?format=json` → **сырой dict из ответа SearXNG** (без нормализации полей).

- `categories`: `"general"`, `"it"`, `"news"` … или список
- `engines`: `"bing"` или `["bing", "qwant"]`
- `language`: `"ru-RU"`, `"en"` …
- `pageno`: номер страницы (с 1)
- `safesearch`: 0/1/2
- `time_range`: `day` / `week` / `month` / `year`

Ошибки сети/HTTP → `SearchError`.

### Прочее

- `sx.base_url`, `sx.port`, `sx.install_dir`, `sx.pid`, `sx.is_running` — свойства
- `sx.stop()` / `await sx.aclose()` — явная остановка
- `sx.log_path`, `sx.err_log_path` — логи процесса SearXNG
- `update(install_dir)` / `python -m searxng_local update` — обновление upstream SearXNG (только вручную; свежий клон + pip + повторный патч)

## Жизненный цикл и остановка процесса

Процесс SearXNG (`python.exe searx/webapp.py` из venv установки) — прямой потомок
вашего скрипта, запущенный без промежуточного PowerShell-лаунчера. Остановка —
`terminate()` → ожидание до 10 с → `kill()` ровно этого процесса.

Гарантии остановки, по убыванию надёжности:

1. `with SearXNG() as sx:` / `async with` — выход из блока, включая исключение;
2. явный `sx.stop()` / `await sx.aclose()`;
3. `atexit`-хук — нормальное завершение интерпретатора;
4. `__del__` — последняя линия при сборке мусора.

> ⚠️ **Ограничение (осознанное):** при жёстком убийстве родителя (`taskkill /F`,
> смерть ОС/сессии) ни один из механизмов не сработает — процесс SearXNG останется
> висеть. Обнаружить: `netstat -ano | findstr :8888`, остановить:
> `taskkill /PID <pid> /F`. Следующий запуск упрётся в `PortInUseError` с той же
> подсказкой. (Отказаться от Job Object — решение проектирования, см. историю обсуждения.)

## CLI

```powershell
python -m searxng_local install [--dir PATH] [--port N]   # установка
python -m searxng_local status  [--dir PATH]              # состояние
python -m searxng_local update  [--dir PATH]              # обновление upstream
```

## Каталог установки

```
<install_dir>/
  repo/                    # git clone --depth 1 searxng/searxng
  .venv/                   # venv от вашего Python 3.11
  settings.yml             # генерируется: secret_key, 127.0.0.1, порт, json-формат
  logs/searxng.log         # stdout процесса
  logs/searxng.err.log     # stderr процесса
  .searxng-local.json      # маркер установки (commit, порт, шаги)
```

## Устранение неполадок

- **`ModuleNotFoundError: No module named 'tomllib'` в логах SearXNG** — venv установки
  создан Python <3.11 (обычно дефолтным 3.10), а upstream импортирует stdlib-модуль
  `tomllib`. Устанавливать `tomllib` из pip **не нужно**: на 3.11+ он уже встроен,
  а бэктпорт с PyPI рассчитан только на <3.11. Пакет теперь проверяет версию venv
  и пересоздаёт её автоматически; вручную достаточно удалить каталог
  `<install_dir>/.venv` и запустить установку с Python 3.11+
  (например `py -3.12 -m searxng_local install`).
- **Не запускается / падает при старте** — смотрите `logs/searxng.err.log`;
  `ProcessCrashedError`/`StartTimeoutError` содержат путь.
- **`GitNotFoundError`** — установите Git: `winget install Git.Git`.
- **`PortInUseError`** — либо висит прошлый процесс (см. выше), либо на порту
  чужой сервис; правило пакета: чужой процесс никогда не убивается.
- **`ConfigMismatchError`** — установка уже настроена на другой порт: используйте
  его или укажите другой `install_dir`.
- **Мало результатов / CAPTCHA у отдельных движков** — upstream SearXNG работает
  с движками напрямую; при необходимости настройте `settings_path` со своим
  списком движков или прокси (см. `outgoing:` в документации SearXNG).

## Отличия от [Kerberos255/searxng-windows](https://github.com/Kerberos255/searxng-windows)

Пакет не зависит от того репозитория, но воспроизводит его ключевые приёмы:
запуск `searx/webapp.py` в venv c `SEARXNG_SETTINGS_PATH`/`PYTHONUTF8`,
Windows-патч `searx/valkeydb.py` (опциональный `import pwd`). Отличия: установка
в каталог проекта, запуск напрямую через `subprocess` (без PowerShell-лаунчера,
PID-файлов и остановки по маске командной строки), библиотечный lifecycle-API
и асинхронный JSON-поиск. API Pool (порт 8890) и настройка прокси — вне скоупа.

Ещё одно отличие, критичное для Windows: в дереве upstream SearXNG есть файлы,
невалидные для NTFS (например `.../searxng.conf:socket` — двоеточие в имени),
из-за которых обычный `git clone` падает на checkout ("invalid path"), а
`read-tree`/checkout не могут даже наполнить индекс. Пакет обходит это
plumbing-цепочкой: `clone --no-checkout` → `ls-tree` с фильтрацией
невалидных путей → `update-index --index-info` → `checkout-index -a`
(отбрасываются только шаблоны конфигураций apache/nginx/uwsgi — для работы
SearXNG они не нужны). По той же причине `update()` делает свежий клон
вместо `git pull`. Если клонируете upstream вручную — ждите ту же ошибку.

## Лицензии

Код пакета — MIT. SearXNG скачивается во время установки и остаётся под своей
лицензией AGPL-3.0-or-later на вашей машине; пакет его не бандлит и не изменяет
кроме одного файла совместимости (`searx/valkeydb.py`).

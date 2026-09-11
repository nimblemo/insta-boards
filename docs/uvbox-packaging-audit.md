# Аудит упаковки `insta-boards` через uvbox

Инженерный отчёт: что именно производит uvbox во время выполнения, какие
переменные `IG_*` из `.env` реально работали, какие — нет, и что было
исправлено.

Область: упаковка в self-bootstrapping бинарники (`uvbox` 1.0.9),
загрузка конфигурации из `.env`, разрешение «корня репозитория».

---

## 1. Что uvbox производит в рантайме

uvbox **не замораживает Python** (в отличие от PyInstaller). Он собирает
небольшой Go-лаунчер, который:

1. **Первый запуск** — распаковывает встроенный `uv` в
   `$XDG_DATA_HOME/uvbox/<hash>/uv/` (по умолчанию
   `~/.local/share/uvbox/<hash>/uv/`; в Windows —
   `%LOCALAPPDATA%\uvbox\<hash>\uv\`).
2. Скачивает интерпретатор Python и зависимости — интерпретатором управляет
   встроенный `uv`, отдельного подкаталога `python/` **нет**.
3. Устанавливает пакет через `uv tool install` в
   `.../uvbox/<hash>/tools/` (и `tools-bin/`).
4. Запускает точку входа (`insta-boards`).

Каталог установки (`<hash>` — хеш от имени пакета, имени скрипта и
конфигурации; он обеспечивает изоляцию между разными приложениями)
содержит подкаталоги: `uv/`, `tools/`, `tools-bin/`, `cache/`,
`configuration/`. Базовый путь:

| Платформа | Базовый каталог |
| --- | --- |
| Linux / macOS | `$XDG_DATA_HOME/uvbox/<hash>/` (по умолчанию `~/.local/share/uvbox/<hash>/`) |
| Windows | `%LOCALAPPDATA%\uvbox\<hash>\` |

**Последствия, которые важно понимать:**

| Свойство | Значение |
| --- | --- |
| Замороженный Python | ❌ нет — это обычный Python-процесс из `site-packages` |
| Офлайн-работа | ❌ первый запуск требует сети (скачивание uv + Python + зависимостей) |
| Рабочий каталог (CWD) | ✅ наследуется от shell, из которого запущен бинарник |
| `Path(__file__)` | указывает в `.../site-packages/src/paths.py` — в предках **нет** `pyproject.toml` |
| Встроенные подкоманды | `self update`, `self remove`, `self path`, `self cache`, `self uv` |

Ключевой вывод для нашего кода: в упакованном бинарнике «корня
репозитория» не существует, поэтому пути `data/`, `secrets/`,
`sync-collection-list.txt` должны разрешаться относительно **рабочего
каталога**, а не относительно исходников.

---

## 2. Аудит `.env`: какие переменные работали, а какие — нет

### 2.1. Причина бага

`src/cli/app.py` строит парсер и импортирует `src.cli._common`, который
импортирует `src.instagram_sync`. При этом `load_env()` вызывался **только
внутри** `client.init_client()`, то есть значительно позже. В результате
любое чтение `os.environ` **на этапе импорта модуля** фиксировало значения
до того, как `.env` успевал загрузиться.

### 2.2. Таблица по переменным

| Переменная | Где читалась (исходный код) | Работал ли `.env` | Причина |
| --- | --- | --- | --- |
| `IG_USERNAME` | `src/client.py:74` (`get_config`) | ✅ | читается после `load_env()` |
| `IG_PASSWORD` | `src/client.py:75` | ✅ | то же |
| `IG_SESSIONID` | `src/client.py:78` | ✅ | то же |
| `IG_PROXY` | `src/client.py:76` | ✅ | то же |
| `IG_2FA_CODE` | `src/client.py:79` | ✅ | то же |
| `IG_SETTINGS_PATH` | `src/client.py:67` | ✅ | то же |
| `IG_USER_AGENT_ROTATE` | `src/client.py:146` | ✅ | то же |
| `IG_HUMANIZE*` | `src/humanizer.py:80-98` (`field(default_factory=…)`) | ✅ | ленивое чтение, объект создаётся после `init_client()` |
| `IG_DOWNLOAD_CONCURRENCY` | `src/parallel.py:60` (`default_factory`) | ✅ | ленивое чтение |
| **`IG_DOWNLOAD_POOL_REUSE`** | `src/parallel.py:64` (`default_factory`) | ❌ **нет** | **мёртвая ручка**: значение читалось в `ParallelConfig.reuse_pool`, но не передавалось в `DownloadPool`, поэтому ни на что не влияло → исправлено (см. §3.5) |
| **`IG_DOWNLOAD_TIMEOUT`** | `src/instagram_sync.py:60` | ❌ **нет** | константа уровня модуля — фиксируется при импорте |
| **`IG_DOWNLOAD_RETRIES`** | `src/instagram_sync.py:62` | ❌ **нет** | то же |
| **`IG_DOWNLOAD_BACKOFF`** | `src/instagram_sync.py:63` | ❌ **нет** | то же |
| **`IG_DOWNLOAD_DELAY`** | `src/instagram_sync.py:61` (`_throttle`) | ⚠️ **частично** | в `_throttle` — фиксировалась при импорте; в `HumanizerConfig` — работала |
| **`IG_STATE_PATH`** | `src/cli/_common.py:108` | ⚠️ **частично** | для `sync` читалась **до** `load_env()`; для `download` — после |

### 2.3. Исходный (багованный) код

`src/instagram_sync.py`, строки 60-63 — вычисление при импорте:

```python
DEFAULT_DOWNLOAD_TIMEOUT: int = _env_int("IG_DOWNLOAD_TIMEOUT", 120)
DEFAULT_DOWNLOAD_DELAY: float = _env_float("IG_DOWNLOAD_DELAY", 1.0)
DEFAULT_DOWNLOAD_RETRIES: int = _env_int("IG_DOWNLOAD_RETRIES", 5)
DEFAULT_DOWNLOAD_BACKOFF: float = _env_float("IG_DOWNLOAD_BACKOFF", 0.5)
```

`src/cli/app.py`, `main()` — `load_env()` не вызывался вовсе:

```python
def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    handler = args._handler
    ...
```

`src/cli/commands/sync.py`, строки 64-68 — `default_state_path()`
вычисляется **до** `init_client()` (а значит, до `load_env()`):

```python
state_path = (
    Path(args.state_path).expanduser().resolve()
    if args.state_path
    else default_state_path()
)
```

`src/instagram_sync.py`, `get_pool()` — мёртвая ручка `IG_DOWNLOAD_POOL_REUSE`
(значение конфигурации не доходило до пула):

```python
def get_pool(pacer: SessionPacer | None = None) -> DownloadPool:
    cfg = ParallelConfig()
    return DownloadPool(
        pacer=pacer or get_pacer(),
        max_workers=cfg.max_workers,
        # reuse_pool=cfg.reuse_pool,   <-- отсутствовало
    )
```

---

## 3. Внесённые исправления

### 3.1. Ленивые аксессоры вместо констант (`src/instagram_sync.py`)

**Было:**

```python
DEFAULT_DOWNLOAD_TIMEOUT: int = _env_int("IG_DOWNLOAD_TIMEOUT", 120)
DEFAULT_DOWNLOAD_DELAY: float = _env_float("IG_DOWNLOAD_DELAY", 1.0)
DEFAULT_DOWNLOAD_RETRIES: int = _env_int("IG_DOWNLOAD_RETRIES", 5)
DEFAULT_DOWNLOAD_BACKOFF: float = _env_float("IG_DOWNLOAD_BACKOFF", 0.5)
```

**Стало** (значения читаются в момент вызова):

```python
def download_timeout() -> int:   return _env_int("IG_DOWNLOAD_TIMEOUT", 120)
def download_delay() -> float:   return _env_float("IG_DOWNLOAD_DELAY", 1.0)
def download_retries() -> int:   return _env_int("IG_DOWNLOAD_RETRIES", 5)
def download_backoff() -> float: return _env_float("IG_DOWNLOAD_BACKOFF", 0.5)
```

Обновлены все три места использования: `_build_session()`
(retries/backoff), `_throttle()` (delay), `download_to_file()` (timeout).
Старые константы удалены.

### 3.2. `load_env()` первым шагом (`src/cli/app.py`)

`main()` теперь вызывает `client.load_env()` **до** построения парсера и
диспетчеризации, поэтому все обработчики видят значения из `.env`.
Вызов внутри `init_client()` оставлен как страховка (функция
идемпотентна: заполняет только отсутствующие/пустые ключи; реальное
окружение не перезаписывается, а `.env` корня репозитория имеет
приоритет над `.env` рабочего каталога).

Дополнительно: при заданном `--workdir` процесс делает `chdir` в этот
каталог **до** диспетчеризации и повторно вызывает `load_env()`, поэтому
`.env` из `--workdir` тоже подхватывается — **но только для ключей,
которые к этому моменту ещё не заданы**. Непустые значения из `.env`
корня репозитория и из реального окружения имеют приоритет и не
перезаписываются (см. §3.6 и §4).

### 3.3. Детерминированный `repo_root()` (`src/paths.py`)

**Было:**

```python
def repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in (here, *here.parents):
        if (parent / "pyproject.toml").is_file():
            return parent
    return Path.cwd()
```

**Стало** — порядок разрешения: `IG_REPO_ROOT` → ближайший предок,
являющийся **настоящим** чекаутом `insta-boards` (содержит и
`pyproject.toml`, и `src/paths.py`) → `Path.cwd()`:

```python
def repo_root() -> Path:
    explicit = _explicit_root()          # IG_REPO_ROOT (abs или ~)
    if explicit is not None:
        return explicit
    here = Path(__file__).resolve()
    for parent in (here, *here.parents):
        if _looks_like_checkout(parent):  # pyproject.toml + src/paths.py
            return parent
    return Path.cwd()
```

Это закрывает две проблемы:

* чужой `pyproject.toml` где-то выше `site-packages` больше не может
  «перехватить» каталог данных;
* появляется явный «аварийный выход» `IG_REPO_ROOT` для пользователей,
  которым нужен фиксированный каталог данных/состояния/`.env`.

`resolve_from_repo_root()` продолжает работать без изменений.

### 3.4. Проверка остальных чтений окружения

Проверено: иных чтений `os.environ` на этапе импорта нет.

* `HumanizerConfig` и `ParallelConfig` используют
  `field(default_factory=…)` — чтение ленивое; объекты создаются в
  `get_pacer()` / `get_pool()`, то есть после `init_client()`.
* `client.get_config()` и `client.init_client()` читают окружение после
  `load_env()`.
* `_common.default_state_path()` — единственное «раннее» чтение, и оно
  теперь покрыто вызовом `load_env()` в `main()`.

Для каждой подкоманды:

| Подкоманда | Читает `IG_*` после `load_env()`? |
| --- | --- |
| `sync` | ✅ (`load_env()` в `main()` до `default_state_path()`) |
| `download` | ✅ |
| `list boards` | ✅ |
| `list items` | ✅ |

### 3.5. `IG_DOWNLOAD_POOL_REUSE` — мёртвая ручка (`src/instagram_sync.py`, `src/cli/_common.py`, `src/parallel.py`)

`ParallelConfig.reuse_pool` читал `IG_DOWNLOAD_POOL_REUSE` корректно
(лениво), но это значение **нигде не передавалось** в `DownloadPool`:
`DownloadPool.__init__` по умолчанию использовал `reuse_pool=True`, а
`get_pool()` не пробрасывал поле конфигурации. В итоге установка
`IG_DOWNLOAD_POOL_REUSE=0` не меняла поведение — ручка была мёртвой.

**Стало:**

* у `DownloadPool` появилось публичное свойство `reuse_pool`
  (`src/parallel.py`);
* `get_pool()` пробрасывает значение из конфигурации
  (`src/instagram_sync.py`):

```python
def get_pool(pacer: SessionPacer | None = None) -> DownloadPool:
    cfg = ParallelConfig()
    return DownloadPool(
        pacer=pacer or get_pacer(),
        max_workers=cfg.max_workers,
        reuse_pool=cfg.reuse_pool,
    )
```

* `make_pacer_and_pool()` в `src/cli/_common.py` (ветка `--concurrency`)
  тоже передаёт `reuse_pool=cfg.reuse_pool` при создании «свежего» пула.

Теперь `IG_DOWNLOAD_POOL_REUSE=0` реально заставляет каждый
`download_many()` создавать собственный пул и закрывать его после работы.

### 3.6. Пустое значение в `.env` больше не «затеняет» (`src/client.py`)

`load_env()` использовал два вызова `load_dotenv(override=False)`.
python-dotenv считает присваивание `KEY=` **присутствующим** ключом,
поэтому пустое значение в `.env` более высокого приоритета «затеняло»
реальное значение из файла более низкого приоритета.

**Стало** — читаем файлы через `dotenv_values()` и применяем значения
вручную, трактуя пустую строку/пробелы как «не задано»:

```python
for env_path in (repo_env, cwd_env):
    for key, value in dotenv_values(env_path).items():
        if value is None:
            continue  # «голый» KEY без значения — пропускаем
        current = os.environ.get(key)
        if current is None or current.strip() == "":
            os.environ[key] = value
```

Порядок применения: `<repo_root>/.env` → `<cwd>/.env` (то есть
репо-корень имеет более высокий приоритет); реальное окружение не
перезаписывается никогда.

---

## 4. Остаточные ограничения (что важно знать пользователю)

| Ограничение | Детали |
| --- | --- |
| **Сеть при первом запуске** | Бинарник не офлайн: первый запуск скачивает `uv`, Python и зависимости в `$XDG_DATA_HOME/uvbox/<hash>/` (по умолчанию `~/.local/share/uvbox/<hash>/`) или `%LOCALAPPDATA%\uvbox\<hash>\`. |
| **Относительные пути** | В упакованном бинарнике нет «корня репозитория»: `data/`, `secrets/`, `sync-collection-list.txt` разрешаются относительно **рабочего каталога**. Запускайте бинарник из нужного каталога. |
| **`--workdir`** | Меняет рабочий каталог процесса (и, значит, расположение `data/`, `secrets/`, файла состояния). `.env` из `--workdir` тоже загружается, но **только для ключей, ещё не заданных** непустым значением из `.env` корня репозитория или из реального окружения. |
| **`IG_REPO_ROOT`** | Явно задаёт корень (абсолютный путь или `~`). Переопределяет автоопределение и фиксирует расположение данных/состояния/`.env`. |
| **Приоритет `.env`** | Реальное окружение → `.env` корня репозитория → `.env` рабочего каталога (`--workdir`). Пустое значение (`KEY=`) считается «не задано» и не затеняет значение из источника с более низким приоритетом. |
| **Кэш установки при локальном тестировании** | `uv tool install` не переустанавливает **ту же самую версию**, поэтому после пересборки бинарника из исправленного исходника запуск может подхватить **старую** установку из `%LOCALAPPDATA%\uvbox\<hash>\tools\` и показать доисправочное поведение. Перед проверкой локального артефакта удалите подкаталоги `tools/` и `tools-bin/` (каталоги `uv/` и `cache/` можно оставить — бутстрап останется быстрым). В реальных релизах проблема не возникает: версия меняется, и установка обновляется. |

---

## 5. Регрессионные тесты

Добавлены тесты, которые ловят именно эти баги (`tests/`):

| Файл | Что проверяет |
| --- | --- |
| `tests/test_download_env.py` | Ленивые аксессоры отражают значения, выставленные **после** импорта; старые константы удалены; `_throttle` читает задержку лениво; `IG_DOWNLOAD_POOL_REUSE` реально доходит до пула (регрессия мёртвой ручки). |
| `tests/test_env_loading.py` | `.env` в рабочем каталоге подхватывается сквозным путём (`sync --dry-run`); `.env` не переопределяет реальные переменные окружения; пустое значение в `.env` более высокого приоритета не затеняет реальное значение из файла более низкого приоритета; `.env` корня репозитория приоритетнее `.env` рабочего каталога. |
| `tests/test_paths.py` | `IG_REPO_ROOT`; откат к CWD без чекаута; игнорирование чужого `pyproject.toml`; принятие настоящего чекаута. |

Конкретные тесты, закрывающие дефекты F1 и F2:

* **F1 (`IG_DOWNLOAD_POOL_REUSE` — мёртвая ручка):**
  `test_get_pool_respects_reuse_pool_env`,
  `test_get_pool_reuse_pool_default`,
  `test_throttle_reads_delay_at_call_time`.
* **F2 (пустое значение в `.env` затеняет):**
  `test_empty_higher_priority_dotenv_does_not_mask_lower`,
  `test_repo_dotenv_beats_workdir_dotenv`,
  `test_real_env_beats_both_dotenv_files`.

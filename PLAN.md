# План: обратимая корзина для DSH (`gio_trash`)

**Статус:** план готов, на ревью  
**Дата:** 2026-11-25  
**Репозиторий:** `gio_trash` (отдельный от монорепозитория DSH)  
**Платформа v1:** Linux, Python 3.11+ (stdlib), Node.js только для DSH-плагина

## Кратко

- **Что:** CLI `gio-trash` перемещает файлы workspace в системную freedesktop-корзину, хранит расширенные метаданные в SQLite-индексе и восстанавливает записи строго в пределах своего workspace.
- **Почему так:** payload остаётся в стандартной корзине (виден файловым менеджерам), а sidecar-индекс даёт workspace, хеши, даты и манифесты, которых нет в `.trashinfo`.
- **Почему без `gio` CLI:** он не изолируется через `XDG_DATA_HOME`, зависит от gvfs-демона, локализован и отказывается работать на tmpfs. Спецификацию реализуем сами (stdlib), совместимость проверяем через `gio trash --list`.
- **Интеграция:** отдельный DSH bundle с инструментами `trash`, `trash_list`, `trash_restore`; плагин запускает CLI доверенным хост-процессом и сам проверяет sandbox-политику. Дополнительные writable roots не нужны.
- **План:** 5 этапов — ядро/`put`, просмотр/`restore`, обслуживание, DSH-плагин, документация и выпуск.

## Содержание

1. [Контекст и цель](#1-контекст-и-цель)
2. [Ключевые решения](#2-ключевые-решения)
3. [Архитектура](#3-архитектура)
4. [Модель данных](#4-модель-данных)
5. [Контракт CLI](#5-контракт-cli)
6. [Интеграция с DSH](#6-интеграция-с-dsh)
7. [Безопасность](#7-безопасность)
8. [Тестирование](#8-тестирование)
9. [Поэтапный план работ](#9-поэтапный-план-работ)
10. [Риски и открытые вопросы](#10-риски-и-открытые-вопросы)
11. [Definition of Done](#11-definition-of-done-v1)

---

## 1. Контекст и цель

### 1.1. Проблема

DSH в режиме `workspace-write` разрешает изменения только внутри корня workspace и платформенных временных каталогов. Это относится и к файловым инструментам (`ctx.fs`), и к командам, запускаемым через bash. При этом:

- `rm внутри workspace` технически разрешён, но необратим: агент может уничтожить нужный файл без возможности отката;
- перенос в системную корзину — это запись в `~/.local/share/Trash`, то есть **за пределами** workspace, поэтому sandbox её блокирует;
- у DSH нет способа сказать «удали обратимо»: `rm` необратим, а корзина недоступна.

`gio_trash` даёт доверенный путь для этой операции: CLI перемещает файл в freedesktop-корзину, sidecar-индекс хранит метаданные, а DSH-плагин предоставляет модели типизированные инструменты `trash`, `trash_list`, `trash_restore`.

### 1.2. Пользовательские сценарии

1. **Агент убирает мусор.** «Удали `dist/`» → файлы и каталоги уходят в корзину, в ответе — id записи, при необходимости всё возвращается одним вызовом.
2. **Агент ошибся.** Модель удалила не тот файл; пользователь или агент вызывает `trash_list` и `trash_restore` по id, файл возвращается на прежнее относительное место.
3. **Человек работает с CLI.** Посмотреть, что удалялось в этом workspace, восстановить файл, очистить старые записи — без запуска DSH.
4. **Два workspace не пересекаются.** Записи workspace A не видны и не восстанавливаются из workspace B, даже если имена файлов совпадают.

### 1.3. Не цели (v1)

- замена git, системы бэкапов или версионирования;
- глобальный «undo» для любых изменений и слежение за файловой системой;
- очистка чужих записей системной корзины и интеграция с файловыми менеджерами сверх формата freedesktop;
- Windows и macOS (архитектура не запрещает, но v1 — Linux);
- сетевой/удалённый доступ, синхронизация между машинами;
- UI в DSH Web (в v1 — только инструменты и текст результата).

---

## 2. Ключевые решения

| # | Решение | Обоснование | Следствия |
|---|---------|-------------|-----------|
| 1 | **Системная freedesktop-корзина как хранилище payload'а** (`$XDG_DATA_HOME/Trash/files`, `.../info/*.trashinfo`) | Файлы видны в файловых менеджерах и `gio trash --list`; стандартный формат; содержимое не дублируется | Записи может удалить/восстановить сторонний инструмент; индекс должен уметь сверяться с реальностью |
| 2 | **Sidecar-индекс SQLite** в `$XDG_DATA_HOME/gio-trash/` | `.trashinfo` хранит только путь и дату; нужны workspace, размер, хеш, ctime/mtime, манифест, состояние | Индекс — истина для метаданных; payload — истина для содержимого; `doctor` мирит их |
| 3 | **Свой движок по спецификации freedesktop, без `gio` и PyGObject** | Пробы показали: CLI `gio trash` не изолируется через `XDG_DATA_HOME` (gvfs-демон), печатает локализованный текст, `--restore` требует `trash:///` URI, а `Gio.File.trash` отказывается работать на tmpfs («Trashing on system internal mounts is not supported») | Минимум зависимостей, предсказуемость, тестируемость; same-device — атомарный `rename`, cross-device — copy+fsync+unlink |
| 4 | **CLI `gio-trash` + отдельный DSH bundle** | CLI полезен человеку и тестам; плагин даёт модели типизированный контракт, не заставляя её собирать shell-команды | Два артефакта, но общий формат JSON и единая логика в CLI |
| 5 | **Workspace идентифицируется по `(st_dev, st_ino)` корня**, id = `sha256("dev:ino")[:16]` | Переживает переименование каталога в пределах ФС; не зависит от `cd`; одинаков для fs- и shell-sandbox | `list`/`restore` фильтруют по `workspace_id`; переезд на другую ФС = новый workspace (старые записи не видны) |
| 6 | **Плагин выполняет CLI как доверенный хост-процесс**, не через `ctx.fs` | Запись в корзину — вне workspace, `ctx.fs` её запретит; плагин сам проверяет режим sandbox и границы workspace | Sandbox не ослабляется: дополнительные writable roots не нужны; bash-маршрут в корзину остаётся закрытым |
| 7 | **Содержимое не копируется в индекс** | Payload уже содержит байты; дублирование удваивает место и создаёт рассинхрон | В индексе: SHA-256, размер, текстовое превью (ограниченное), манифест каталога |
| 8 | **Восстановление по относительному пути** от текущего корня workspace | Защищает от побега за пределы workspace и переживает переименование корня | `original_path` хранится для аудита, но цель по умолчанию — `workspace_root / relpath` |

### 2.1. Что именно проверено экспериментально

- `XDG_DATA_HOME=/tmp/... gio trash file` всё равно показывает реальную корзину пользователя: CLI общается с gvfs-демоном, который не наследует переменную; вывод локализован.
- `gio trash --restore <file:///...>` завершается ошибкой «Указанное местоположение не начинается с `trash:///`» — для скриптов нужен парсинг URI и отдельная семантика.
- `Gio.File.trash()` из PyGObject **уважает** `XDG_DATA_HOME` и создаёт корректный `.trashinfo` на домашней ФС, но на tmpfs падает с `Trashing on system internal mounts is not supported`.
- `gio trash` на tmpfs падает с той же формулировкой (по-русски).

Вывод: для надёжного и тестируемого поведения реализуем спецификацию сами. Совместимость проверяем чтением `gio trash --list` на домашней ФС (ручной/опциональный тест), а не через вызовы `gio` в рабочем коде.

### 2.2. Границы доверия

- **CLI** не знает про DSH. Он доверяет переданному `--workspace` только после канонизации и всегда проверяет, что цель лежит внутри него. Это защита от ошибок, а не от злонамеренного вызывающего.
- **Плагин DSH** — доверенный код в хост-процессе. Он не использует `ctx.fs` для перемещения (иначе sandbox заблокирует), но обязан:
  1. определить режим sandbox через `ctx.sandboxPolicy.resolve({ session })`;
  2. запретить операцию в `read-only`;
  3. канонизировать путь и проверить вложенность в `workspaceRoot`;
  4. передать CLI абсолютный путь и корень workspace, а не сырые аргументы модели.
- **Bash-маршрут** (`gio-trash put ...` из-под sandbox) по умолчанию не работает: sandbox не даёт писать в `Trash` и индекс. Это осознанно; включение `additionalWritableRoots` для корзины описано в разделе 6 как нерекомендуемая опция.

---

## 3. Архитектура

### 3.1. Компоненты

```
┌──────────────┐   tool call    ┌──────────────────┐   spawn (argv)   ┌─────────────────┐
│ модель / DSH │ ─────────────> │ dsh-plugin-      │ ───────────────> │ gio-trash CLI   │
│ (агент)      │ <───────────── │ gio-trash (host) │ <─────────────── │ (Python)        │
└──────────────┘   JSON result  └──────────────────┘   JSON stdout    └────────┬────────┘
                                                                               │
                                       ┌───────────────────────────────────────┤
                                       ▼                                       ▼
                            $XDG_DATA_HOME/Trash                    $XDG_DATA_HOME/gio-trash/
                            files/ + info/*.trashinfo               index.sqlite3, manifests/
```

- **CLI** — единственное место, где реализованы правила корзины, индекс и восстановление. Он не зависит от DSH и работает вручную.
- **Плагин** — тонкий адаптер: схемы инструментов, проверка sandbox-политики, запуск CLI, форматирование результата. Не дублирует перемещение файлов.
- **Индекс** — SQLite в WAL-режиме. Payload и `.trashinfo` остаются в системной корзине, чтобы файл был виден файловому менеджеру.

### 3.2. Раскладка репозитория

```
gio_trash/
├── PLAN.md
├── README.md
├── pyproject.toml                 # console_scripts: gio-trash
├── src/gio_trash/
│   ├── __main__.py                # python -m gio_trash
│   ├── cli.py                     # argparse, JSON/human output, exit codes
│   ├── workspace.py               # канонизация, containment, protected paths
│   ├── metadata.py                # statx/ctime/mtime, sha256, превью, манифест
│   ├── trashdir.py                # путь Trash, коллизии имён, .trashinfo
│   ├── engine.py                  # put / restore / empty / doctor
│   ├── index.py                   # схема, миграции, запросы, locking
│   └── errors.py                  # коды ошибок CLI
├── tests/                         # pytest + временный XDG_DATA_HOME
└── dsh-plugin/
    ├── package.json               # dsh.bundle + engines.dsh
    ├── cordis.patch.yml
    ├── src/index.ts               # apply(), defineTool(), ctx.subprocess
    └── test/trash.test.ts         # фейковый CLI, проверка argv/политики
```

### 3.3. Жизненный цикл `put`

1. `--workspace` канонизируется (`realpath`), проверяется, что это существующий каталог.
2. `PATH` разрешается относительно cwd и канонизируется; запрещены: сам корень workspace, `Trash`, каталог индекса, `/`, `$HOME`, защищённые пути (по умолчанию `.git`).
3. `lstat` без перехода по финальной symlink; поддерживаются `file`, `dir`, `symlink`. Спецфайлы (socket, fifo, device) отклоняются.
4. Собираются метаданные **до** перемещения: размер, режим, uid/gid, mtime/ctime/birthtime, SHA-256 (для файла), превью, манифест (для каталога).
5. Берётся эксклюзивная блокировка (`flock`) на `gio-trash/lock`.
6. В индекс пишется строка `state=pending` с полными метаданными и намерением.
7. В `Trash/files` резервируется имя с учётом коллизий; файл перемещается:
   - та же ФС — атомарный `os.rename`;
   - разные ФС — копирование во временное имя `.partial`, `fsync` файла и каталога, `rename` в финальное имя, затем удаление источника.
8. `.trashinfo` пишется во временный файл, `fsync`, затем атомарно переименовывается в `Trash/info/<name>.trashinfo`.
9. Строка переводится в `state=active`; COMMIT; блокировка снимается.
10. В stdout — JSON с `id`, `relpath`, `size`, `sha256`, `trashed_at`.

Сбой на любом шаге: источник не считается удалённым, пока payload не перемещён и `.trashinfo` не зафиксирован. Частично созданные данные откатываются; строка помечается `failed` с текстом ошибки, `doctor --fix` доводит или чистит её.

### 3.4. Жизненный цикл `restore`

1. Запись ищется по `id` и `workspace_id` (или `--all` только для человека); чужая запись — `not_found`, без утечки деталей.
2. Проверяется `state=active` и наличие payload; если записан SHA-256 — сверяется.
3. Цель: по умолчанию `workspace_root / relpath`; `--to` разрешён только внутри workspace. Родительские каталоги создаются только внутри workspace.
4. Если цель уже существует: без `--force` — конфликт; с `--force` существующий файл сначала уходит в корзину отдельной записью (`reason="replaced-by-restore"`), затем восстановление повторяется.
5. Под блокировкой выполняется обратное перемещение (rename или copy+fsync+unlink), `.trashinfo` удаляется.
6. Строка переводится в `state=restored`, фиксируются `restored_at`, `restore_target`, `restored_sha256`.
7. Если SHA-256 не совпал после копирования — ошибка, цель удаляется (только что созданная нами), запись остаётся `active`.

Отдельно: `restore` **никогда** не пишет по абсолютному `original_path`; только относительно текущего корня workspace. Это одновременно решает переименование workspace и исключает побег за его границы.

### 3.5. Согласованность с внешним миром

Корзина — общий ресурс: пользователь может очистить её файловым менеджером, `gio trash --empty` или восстановить файл вручную. Поэтому:

- `list` по умолчанию скрывает записи, у которых payload исчез, но не удаляет их;
- `doctor` сообщает: `pending` старше 10 минут, отсутствующий payload, несовпадение размера/хеша, `.trashinfo` без строки индекса (orphan), строку без `.trashinfo`;
- `doctor --fix` завершает `pending` по факту наличия payload и помечает `missing`; `.trashinfo` без строки индекса только показываются в отчёте и автоматически не удаляются;
- записи, созданные не через `gio_trash`, в индекс не попадают и инструментами DSH не видны (это ожидаемо).

### 3.6. Конкурентность и блокировки

- Один `flock` на все мутации (`put`, `restore`, `empty`, `doctor --fix`); `list`/`show` читают WAL без блокировки.
- Имя payload'а резервируется под блокировкой, поэтому два параллельных `put` не выбирают одно имя.
- SQLite: `journal_mode=WAL`, `busy_timeout=5000`, `foreign_keys=ON`; миграции — по `PRAGMA user_version`.

---

## 4. Модель данных

### 4.1. Схема индекса (v1)

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE workspaces (
  id           TEXT PRIMARY KEY,          -- sha256("<st_dev>:<st_ino>")[:16]
  root         TEXT NOT NULL,             -- canonical absolute path (обновляется при переименовании)
  root_dev     INTEGER NOT NULL,
  root_ino     INTEGER NOT NULL,
  created_at   TEXT NOT NULL,             -- ISO-8601 UTC
  last_seen_at TEXT NOT NULL,
  UNIQUE (root_dev, root_ino)
);

CREATE TABLE entries (
  id             TEXT PRIMARY KEY,        -- t_<base32(time+random)>, монотонно сортируется
  workspace_id   TEXT NOT NULL REFERENCES workspaces(id),
  original_path  TEXT NOT NULL,           -- абсолютный путь на момент удаления (аудит)
  relpath        TEXT NOT NULL,           -- путь относительно корня workspace
  filename       TEXT NOT NULL,
  kind           TEXT NOT NULL CHECK (kind IN ('file', 'dir', 'symlink')),
  size           INTEGER NOT NULL,        -- файл: байты; каталог: сумма размеров файлов; symlink: длина цели
  mode           INTEGER NOT NULL,
  uid            INTEGER,
  gid            INTEGER,
  mtime_ns       INTEGER NOT NULL,
  ctime_ns       INTEGER NOT NULL,
  birthtime_ns   INTEGER,                 -- NULL, если платформа не отдаёт creation time
  link_target    TEXT,                    -- для symlink
  sha256         TEXT,                    -- для file; NULL для dir/symlink
  preview        TEXT,                    -- первые <= 4096 символов, только текст
  manifest_path  TEXT,                    -- для dir: относительный путь к JSON-манифесту
  trash_dir      TEXT NOT NULL,           -- например /home/u/.local/share/Trash
  payload_name   TEXT NOT NULL,           -- имя внутри Trash/files
  payload_path   TEXT NOT NULL,
  trashinfo_path TEXT NOT NULL,
  trashed_at     TEXT NOT NULL,
  reason         TEXT,
  state          TEXT NOT NULL CHECK (state IN ('pending', 'active', 'restored', 'missing', 'failed')),
  restored_at    TEXT,
  restore_target TEXT,
  restored_sha256 TEXT,
  error          TEXT
);

CREATE INDEX entries_workspace_time ON entries(workspace_id, trashed_at DESC);
CREATE INDEX entries_workspace_path ON entries(workspace_id, original_path);
CREATE INDEX entries_state ON entries(state);
```

`PRAGMA user_version = 1`. Миграции — последовательные функции `v1→v2` с транзакцией и проверкой в тестах.

**Регистрация workspace.** CLI делает `stat` канонического корня и ищет строку по `(root_dev, root_ino)`. Если строка найдена, а `root` отличается — путь обновляется (каталог переименован/перемещён в пределах ФС). Если строка не найдена — создаётся новая. Переезд workspace на другую ФС даёт новый inode и, следовательно, новый workspace: старые записи остаются в корзине, но DSH их не видит (ограничение v1). Переиспользование inode после удаления каталога теоретически возможно; это редкий риск, принятый для v1 (см. раздел 10.1).

### 4.2. Метаданные времени

- `mtime_ns` — время изменения содержимого (то, что обычно называют edit time).
- `ctime_ns` — inode change time; **не** creation time. Хранится отдельно, чтобы не выдавать его за дату создания.
- `birthtime_ns` — берётся через `statx(2)` (ctypes на Linux) или `st_birthtime`, если ОС его отдаёт; иначе `NULL` и `birthtime_source="unavailable"` в JSON.
- `trashed_at` — UTC ISO-8601 с миллисекундами; в `.trashinfo` дополнительно пишется локальное `DeletionDate` без зоны (требование формата).

### 4.3. Содержимое и манифест

Payload — единственная копия содержимого. В индексе:

- `sha256` для обычного файла (потоково, блоками 1 МиБ);
- `preview` для текстовых файлов: не более 4096 символов, только если данные декодируются как UTF-8 и не содержат NUL; флаг `preview_truncated`;
- для каталога — JSON-манифест в `gio-trash/manifests/<id>.json`:

```json
{
  "version": 1,
  "truncated": false,
  "totals": { "files": 12, "dirs": 3, "symlinks": 1, "bytes": 84213 },
  "entries": [
    { "path": "a/b.txt", "kind": "file", "size": 12, "mode": 420, "mtime_ns": 1795593609000000000, "sha256": "…" },
    { "path": "a/link", "kind": "symlink", "link_target": "../b.txt" }
  ]
}
```

Ограничения хеширования каталога (защита от гигантских деревьев): не более 100 000 записей и 2 ГиБ суммарно; при превышении `truncated=true`, у пропущенных файлов `sha256=null`. Сам перенос в корзину при этом не блокируется.

### 4.4. Идентификаторы

`id` — `t_` + Crockford base32 от `(unix_ms << 40) | random40`, 16 символов. Сортируется лексикографически по времени, не требует зависимостей, не угадывается последовательно. Полный id показывается в JSON; человеку можно показывать короткий суффикс только в `list` (с предупреждением о неоднозначности), но `restore` принимает полный id.

---

## 5. Контракт CLI

### 5.1. Команды

| Команда | Назначение | Ключевые флаги |
|---------|------------|----------------|
| `gio-trash put PATH` | Переместить файл/каталог в корзину и записать метаданные | `--workspace DIR`, `--reason TEXT`, `--dry-run`, `--no-preview`, `--json` |
| `gio-trash list` | Список записей workspace'а | `--workspace DIR`, `--all`, `--limit N`, `--state active\|restored\|missing`, `--json` |
| `gio-trash show ID` | Полные метаданные записи | `--json` |
| `gio-trash restore ID` | Вернуть запись в workspace | `--workspace DIR`, `--to PATH`, `--force`, `--json` |
| `gio-trash empty` | Удалить записи (payload, `.trashinfo`, манифест) | `--workspace DIR`, `--older-than 30d`, `--yes`, `--json` |
| `gio-trash doctor` | Сверка индекса и корзины | `--fix`, `--json` |
| `gio-trash version` | Версия и пути хранилища | `--json` |

Правила по умолчанию:

- `put` без `--workspace` использует текущий каталог как корень workspace; путь вне него — ошибка. Запускать из корня workspace (или передавать `--workspace` явно).
- `list`/`restore`/`empty` без `--workspace` используют текущий каталог; `--all` снимает фильтр (только для человека; плагин его не использует).
- `empty` без `--yes` только показывает, что будет удалено; никогда не трогает записи вне выбранного workspace.

### 5.2. JSON-контракт

`put`:

```json
{
  "id": "t_00JD3K9Q4M2V8S6R",
  "workspace": "/home/u/proj",
  "path": "/home/u/proj/dist/app.js",
  "relpath": "dist/app.js",
  "kind": "file",
  "size": 18432,
  "sha256": "9f2c…",
  "trashed_at": "2026-11-25T08:12:33.412Z"
}
```

`list` — массив объектов `{id, relpath, kind, size, trashed_at, state, sha256}`; `show` — полная строка, включая `preview`, `birthtime_ns`, `manifest_path`, `trash_dir`. `restore` — `{id, path, relpath, sha256, restored_at}`.

### 5.3. Ошибки и коды возврата

| Код | Значение |
|-----|----------|
| 0 | успех |
| 2 | неверные аргументы |
| 3 | запись не найдена / чужая workspace |
| 4 | конфликт (цель существует без `--force`, имя занято) |
| 5 | отказ containment (путь вне workspace, защищённый путь) |
| 6 | ошибка ФС/корзины (ENOSPC, EACCES, сбой копирования) |
| 7 | повреждён или недоступен индекс |

При `--json` ошибки тоже JSON: `{"error": {"code": "OUTSIDE_WORKSPACE", "message": "…", "details": {…}}}`. Человеческий режим печатает сообщение в stderr; stdout остаётся чистым для машинного парсинга.

### 5.4. Конфигурация

- `XDG_DATA_HOME` — база для `Trash/` и `gio-trash/` (по умолчанию `~/.local/share`). Этого достаточно для изоляции в тестах.
- `GIO_TRASH_LOCK_TIMEOUT` — секунды ожидания flock (по умолчанию 10).
- Никаких сетевых вызовов и никаких зависимостей вне stdlib.

---

## 6. Интеграция с DSH

### 6.1. Формат поставки

Отдельный npm-пакет `dsh-plugin-gio-trash` в каталоге `dsh-plugin/`, устанавливаемый как bundle:

```json
{
  "name": "dsh-plugin-gio-trash",
  "version": "0.1.0",
  "type": "module",
  "main": "lib/index.js",
  "files": ["lib/index.js", "cordis.patch.yml"],
  "dsh": { "bundle": { "patch": "./cordis.patch.yml" } },
  "engines": { "dsh": ">=0.2.0 <0.3.0" },
  "peerDependencies": {
    "@deepseek-ai/cordis": "^0.2.0",
    "@deepseek-ai/dsh-tools": "^0.2.0",
    "@deepseek-ai/dsh-sandbox-policy": "^0.2.0",
    "@deepseek-ai/dsh-subprocess": "^0.2.0",
    "@deepseek-ai/dsh-agent": "^0.2.0",
    "@deepseek-ai/dsh-working-directory": "^0.2.0"
  }
}
```

`cordis.patch.yml`:

```yaml
- insert:
    - id: tool-gio-trash
      name: dsh-plugin-gio-trash
      config:
        cliPath: gio-trash
        timeoutMs: 20000
```

Установка: `dsh plugin --profile <name> add ./dsh-plugin`.

### 6.2. Инструменты

| Инструмент | Параметры | Результат |
|------------|-----------|-----------|
| `trash` | `path` (string, required; относительный к cwd сессии или абсолютный внутри workspace), `reason` (string, optional, ≤ 200 символов) | `{id, path, relpath, kind, size, sha256, trashed_at}` |
| `trash_list` | `limit` (integer, 1..100, default 20) | `{entries: [{id, relpath, kind, size, trashed_at, state}], total}` |
| `trash_restore` | `id` (string, required), `force` (boolean, default false) | `{id, path, relpath, sha256, restored_at}` |

`empty` намеренно **не** даётся модели: очистка корзины — решение человека. `trash` принимает один путь за вызов; массовая уборка — несколько вызовов (позже можно добавить массив, не ломая контракт).

### 6.3. Проверки перед вызовом CLI

```ts
// псевдокод execute()
if (!exec.agent) throw new TrashError('trash requires an owning agent session')
const policy = ctx.sandboxPolicy.resolve({ session: exec.agent.session })
if (policy.mode === 'read-only') {
  throw new TrashError('[sandbox: file access denied under read-only mode] trash is unavailable')
}
const cwd = await ctx.workingDirectory.ensure(exec.agent, exec.signal)
const workspace = policy.workspaceRoot            // фиксированный корень, не cwd
const target = await canonicalizeForTrash(args.path, cwd, workspace) // без перехода по финальной symlink
assertInside(workspace, target)                   // lexical + realpath родителя
assertNotProtected(target)                        // .git и т.п. — как в CLI
```

Затем:

```ts
const result = await ctx.subprocess.spawn({
  command: config.cliPath,
  args: ['put', target, '--workspace', workspace, '--reason', reason ?? '', '--json'],
  env: { HOME: process.env.HOME, XDG_DATA_HOME: process.env.XDG_DATA_HOME, PATH: process.env.PATH },
  timeoutMs: config.timeoutMs,
})
```

и разбор строгого JSON-контракта (раздел 5.2). Никакого `shell: true`, никакой интерполяции пользовательских строк в argv.

Почему не `ctx.fs.moveFile`: `ctx.fs` — sandboxed-провайдер, перемещение в `Trash` будет отклонено как запись вне workspace. Инструмент — доверенный код хоста, который сам реализует политику. Это не лазейка: путь модели всё равно ограничен workspace, а режим `read-only` запрещает вызов.

### 6.4. Отображение ошибок

| Ситуация | Что видит модель |
|----------|------------------|
| `read-only` | `[sandbox: file access denied under read-only mode]` + подсказка, что trash недоступен |
| путь вне workspace | `path is outside the session workspace: <path>` (без stack trace) |
| защищённый путь | `refusing to trash protected path: .git`; обхода из инструмента нет |
| CLI не найден | `gio-trash CLI not found; install it or set cliPath in the plugin config` |
| конфликт restore | `target already exists: <relpath>; pass force=true to replace it (the existing file is moved to trash)` |
| таймаут | `gio-trash timed out after 20000 ms` |
| ошибка индекса | `trash index is unavailable: <code>` + рекомендация `gio-trash doctor` |

Все ошибки — структурированные, без вывода stderr как есть (stderr может содержать пути и локализованный текст).

### 6.5. Подсказка модели

Плагин добавляет в системный промпт короткий блок:

> Удаляй файлы workspace через `trash`, а не через `rm`: удаление обратимо, возврат — `trash_list` + `trash_restore`. Инструмент работает только внутри текущего workspace и только в режимах `workspace-write` / `danger-full-access`.

### 6.6. Регистрация при старте

- На `apply()` плагин проверяет `cliPath` (`--version`, короткий таймаут). Если CLI нет — пишет предупреждение и **не** регистрирует инструменты (модель не должна видеть заведомо нерабочие tools).
- При `ctx.subprocess` недоступном (минимальные композиции) — тоже предупреждение и пропуск.
- Версия CLI и минимально совместимая версия проверяются по `version --json`; несовпадение major — предупреждение.

### 6.7. Ослабление sandbox (не рекомендуется)

Если пользователю нужно вызывать `gio-trash` из bash внутри sandbox, придётся добавить в `sandboxPolicy.additionalWritableRoots`:

- `$XDG_DATA_HOME/Trash`,
- `$XDG_DATA_HOME/gio-trash`.

Это даёт модели право писать (и удалять) **всю** пользовательскую корзину и индекс, включая чужие записи, поэтому по умолчанию не делается и не входит в поставку. Документируем как явный, осознанный риск; альтернатива на будущее — привилегированный helper-демон с unix-socket и проверкой workspace на своей стороне.

---

## 7. Безопасность

### 7.1. Модель угроз

| Угроза | Защита |
|--------|--------|
| `path` с `..` или абсолютный путь вне workspace | `realpath` + containment; отказ `OUTSIDE_WORKSPACE` |
| Симлинк в конце пути | `lstat`; в корзину уходит сам симлинк, цель не читается и не перемещается |
| Подмена родительского симлинка между проверкой и `rename` | повторная канонизация непосредственно перед операцией; по возможности `renameat` с `dir_fd` и `O_NOFOLLOW`; остаточный TOCTOU принят, как в `fs-sandbox` |
| Восстановление поверх чужого файла | без `--force` — конфликт; с `--force` существующий файл сначала уходит в корзину |
| Восстановление в чужой workspace | фильтр по `workspace_id`; цель всегда внутри текущего корня |
| Подмена payload'а в корзине | SHA-256 при `restore`; расхождение — отказ и `doctor` |
| Огромный/бинарный файл в `preview` | превью только для UTF-8 без NUL, ≤ 4096 символов; индекс 0600 |
| Секреты в метаданных | превью можно отключить (`--no-preview` / конфиг плагина); payload и так лежит в пользовательской корзине |
| Мусорные `.trashinfo` / инъекция в `Path` | URL-кодирование, запрет переводов строк, абсолютный путь, запись через временный файл |
| Заполнение диска | `empty --older-than`; автоматического удаления нет; `doctor` показывает объём |
| Гонки двух `put` | `flock` + резервирование имени под блокировкой |
| Чтение чужой корзины | per-user пути (`$XDG_DATA_HOME`), права 0700/0600; v1 не поддерживает multi-user |

### 7.2. Защищённые пути

По умолчанию запрещено перемещать:

- сам корень workspace;
- `Trash` и `gio-trash` (включая попытку «удалить корзину»);
- `.git` (каталог/файл) внутри workspace;
- `/`, `$HOME`, родительские каталоги workspace.

Обход — только явный флаг CLI `--allow-protected`, которого нет у DSH-инструмента.

### 7.3. Права

- `Trash/` — 0700, `Trash/files` — 0700, `Trash/info` — 0700.
- `.trashinfo` — 0600.
- `gio-trash/` — 0700; `index.sqlite3` и `manifests/` — 0600/0700.
- При копировании сохраняются mode и mtime (best effort; xattr — по возможности).

### 7.4. Что будет, если индекс потерян

`.trashinfo` содержит абсолютный путь и дату удаления — этого достаточно, чтобы вручную восстановить файл из `Trash/files`. `gio-trash doctor` находит «осиротевшие» `.trashinfo` и показывает их в отчёте; импорт таких записей в индекс (с назначением workspace) — кандидат на v1.1, в v1 автоматического импорта нет. Payload при потере индекса не страдает.

---

## 8. Тестирование

### 8.1. Unit (pytest)

- `workspace.py`: canonicalize, containment, `..`, префиксы (`/ws` vs `/ws2`), symlink-родитель, запрещённые пути.
- `metadata.py`: размер каталога, SHA-256, превью (UTF-8/NUL/лимит), манифест и truncated-режим.
- `trashdir.py`: коллизии имён (`a.txt`, `a.1.txt`, `a.2.txt`), URL-кодирование `Path`, формат `DeletionDate`, права.
- `index.py`: миграции, WAL, состояния, фильтрация по workspace, сортировка.
- `engine.py`: `pending → active`, откат при сбое, `restored`, `missing`, `failed`.

### 8.2. Интеграция CLI

Фикстура: `XDG_DATA_HOME=tmp_path/data`, workspace в `tmp_path/ws`. Собственный движок не отказывается от tmpfs (в отличие от GIO), поэтому тесты идут в обычном `tmp_path`.

Сценарии:

1. `put` файла → `list` → `restore`: содержимое, mode, mtime, sha256 совпадают.
2. `put` каталога с вложенными файлами и симлинком → манифест → `restore` целиком.
3. Два workspace: `list` не показывает чужое, `restore` чужого id → exit 3.
4. Коллизии: три файла с одинаковым именем → три payload'а, три `.trashinfo`.
5. Конфликт восстановления: без `--force` exit 4, с `--force` старый файл уходит в корзину.
6. Внешнее удаление payload'а → `list` скрывает, `doctor` находит, `--fix` помечает `missing`.
7. Обрыв после `pending`: `doctor --fix` завершает запись.
8. Параллельные `put` (N процессов) → все записи `active`, имена уникальны.
9. Отключённый `preview`, защищённый `.git`, отказ для socket/fifo.
10. Cross-device путь: monkeypatch «другая ФС» → copy+fsync+unlink, исходник исчезает только после успеха.
11. Переименование workspace: `put` в `/ws`, затем `mv /ws /ws2`, `list --workspace /ws2` показывает запись, `restore` возвращает её в `/ws2/relpath`.

### 8.3. Плагин (vitest)

- Фейковый CLI (node-скрипт) пишет полученный argv в файл и возвращает JSON; проверяем: путь абсолютный, `--workspace` = policy root, нет shell-инъекций.
- `read-only` → отказ без вызова CLI.
- путь вне workspace → отказ без вызова CLI.
- невалидный JSON / ненулевой exit / таймаут → понятная ошибка.
- отсутствие CLI при старте → инструменты не регистрируются.
- `trash_restore` пробрасывает `force`.

### 8.4. E2E в DSH (ручной чек-лист)

1. `dsh plugin --profile trash add ./dsh-plugin`, запустить `dsh --profile trash` в тестовом workspace с политикой `workspace-write`.
2. Попросить агента удалить файл → в ответе id; `ls "$XDG_DATA_HOME/Trash/files"` содержит payload.
3. `gio trash --list | grep <имя>` (на домашней ФС) показывает запись.
4. `trash_list` → `trash_restore` возвращает файл.
5. `bash`: `rm ../outside.txt` → sandbox по-прежнему запрещает; прямой `gio-trash put` из bash → отказ записи в Trash.
6. Второй workspace не видит запись первого.

### 8.5. Совместимость

- Формат `.trashinfo` сверяется с эталоном, созданным `Gio.File.trash` (ручной тест, не CI): те же ключи, URL-кодирование, дата.
- `gio trash --list` на домашней ФС видит наши записи.
- `gio trash --restore` (вручную) возвращает файл — значит, формат корректен.

---

## 9. Поэтапный план работ

Каждый этап заканчивается зелёными тестами и 1–2 небольшими коммитами. Коммиты — по завершённым срезам, а не по файлам.

### Этап 0. План и каркас — сделано

- `README.md`, `PLAN.md`, `.gitignore`.
- Коммиты: `docs: add project overview and initial trash design`, `docs: specify storage architecture, data model, and CLI contract`.

### Этап 1. Ядро хранилища и `put`

- `pyproject.toml` (`console_scripts=gio-trash`), `errors.py`, `workspace.py`, `metadata.py`, `trashdir.py`, `index.py`.
- `engine.put`: pending → rename/copy → `.trashinfo` → active.
- Команды `put`, `version`; `--json`, `--dry-run`.
- Тесты: unit, коллизии имён, права, откат при сбое, формат `.trashinfo`.
- Критерий: файл и каталог попадают в `Trash/files`, `.trashinfo` валиден, запись `active`, источник исчез.

### Этап 2. Просмотр и восстановление

- `list`, `show`, `restore`; `--workspace`, `--all`, `--force`, `--to`.
- Изоляция workspace, сверка sha256, восстановление относительного пути.
- Тесты: round-trip, конфликт, чужой workspace, исчезнувший payload.
- Критерий: `put → list → restore` возвращает идентичное содержимое; чужой workspace недоступен.

### Этап 3. Обслуживание

- `empty --older-than`, `doctor`, `doctor --fix`, reconcile с `.trashinfo`.
- Защищённые пути, `--no-preview`, лимиты манифеста.
- Тесты: обрыв `pending`, внешнее удаление, конкурентность, cross-device.
- Критерий: индекс и корзина сходятся после сбоев; `empty` не выходит за workspace.

### Этап 4. DSH-плагин

- `dsh-plugin/`: bundle-манифест, patch, `apply()`.
- Инструменты `trash`, `trash_list`, `trash_restore`; проверка `sandboxPolicy`, `ctx.subprocess`, JSON-валидация.
- Тесты vitest с фейковым CLI.
- Критерий: в `workspace-write` инструмент работает, в `read-only` — отказ, bash-маршрут остаётся закрытым.

### Этап 5. Документация и выпуск

- README: установка CLI, установка bundle, примеры, ограничения (Linux, topdir trash отложен).
- `SECURITY.md` с моделью угроз и нерекомендуемым `additionalWritableRoots`.
- CI: pytest + vitest + линтеры; сборка wheel.
- Критерий: чистая установка с нуля по README; E2E-чек-лист пройден.

---

## 10. Риски и открытые вопросы

### 10.1. Риски

| Риск | Вероятность | Смягчение |
|------|-------------|-----------|
| Расхождение с freedesktop-форматом | низкая | сверка с `Gio.File.trash` и `gio trash --list`; тест на `.trashinfo` |
| Нет birthtime на Linux | высокая | `NULL` + `birthtime_source`; не подменять ctime |
| Дорогое копирование на другой диск | средняя | v1: copy+fsync; далее topdir-корзина на той же ФС |
| Пользователь/менеджер опустошил корзину | средняя | `doctor`, состояние `missing`, понятные ошибки |
| Превью хранит секреты | средняя | лимит, режим 0600, отключаемо |
| Изменения API DSH 0.x | средняя | узкая зависимость: `tools`, `sandboxPolicy`, `subprocess`, `workingDirectory` |
| Переиспользование inode после удаления workspace | низкая | id по `(dev, ino)`; при совпадении старый путь обновляется; задокументировано, сброс — пересоздать индекс |
| Пользователь включил `additionalWritableRoots` | низкая | явная документация риска, не включаем по умолчанию |

### 10.2. Открытые вопросы

1. Имя CLI: `gio-trash` (совпадает с репозиторием, но подразумевает GNOME GIO) или `dsh-trash` (точнее по смыслу)? Предлагаю `gio-trash`, зависимостей от GIO нет.
2. Делать ли topdir-корзину (`<mount>/.Trash-$uid`) уже в v1 или отложить до v1.1? Предлагаю отложить: v1 — домашняя корзина + copy-fallback.
3. Превью содержимого по умолчанию включено или выключено? Предлагаю включено с лимитом 4 КиБ и отключением флагом.
4. Нужен ли `trash` для нескольких путей за вызов? Предлагаю v1 — один путь.
5. Нужна ли retention-политика (автоочистка старше N дней)? Предлагаю только ручной `empty`.
6. Поддержка macOS/Windows: формат `.trashinfo` — Linux/freedesktop; для других ОС нужен отдельный бэкенд. Вне v1.

---

## 11. Definition of Done (v1)

- [ ] `gio-trash put/list/show/restore/empty/doctor` работают по контракту раздела 5, exit codes соблюдены.
- [ ] Файл, каталог и симлинк переживают round-trip; sha256, mode и mtime сохраняются (где позволяет ФС).
- [ ] `.trashinfo` совместим с freedesktop; запись видна `gio trash --list`.
- [ ] `list`/`restore` изолированы по workspace; восстановление никогда не выходит за корень.
- [ ] `read-only` запрещает trash; `workspace-write` разрешает только внутри workspace; bash-маршрут в корзину закрыт.
- [ ] Сбои (обрыв, внешняя очистка, коллизии, конкурентность) не теряют данные и диагностируются `doctor`.
- [ ] Плагин устанавливается как bundle, регистрирует ровно три инструмента, не ломает композиции без CLI.
- [ ] README + SECURITY + ручной E2E-чек-лист; CI зелёный.



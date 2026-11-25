# План: обратимая корзина для DSH (`gio_trash`)

**Статус:** черновик, на ревью
**Дата:** 2026-11-25
**Репозиторий:** `gio_trash` (отдельный от монорепозитория DSH)
**Платформа v1:** Linux, Python 3.11+ (stdlib), Node.js только для DSH-плагина

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
| 5 | **Workspace — канонический корень сессии**, id = `sha256(realpath)[:16]` | Корень не меняется при `cd`; одинаковый для fs- и shell-sandbox | `list`/`restore` фильтруют по `workspace_id`; запись «переезжает» вместе с workspace только по относительному пути |
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
- `doctor --fix` завершает `pending` по факту наличия payload, помечает `missing`, удаляет осиротевшие `.trashinfo` **только** если payload отсутствует; payload никогда не удаляется автоматически;
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
  id           TEXT PRIMARY KEY,          -- sha256(canonical root)[:16]
  root         TEXT NOT NULL UNIQUE,      -- canonical absolute path
  created_at   TEXT NOT NULL,             -- ISO-8601 UTC
  last_seen_at TEXT NOT NULL
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
| `gio-trash put PATH` | Переместить файл/каталог в корзину и записать метаданные | `--workspace DIR`, `--reason TEXT`, `--dry-run`, `--json` |
| `gio-trash list` | Список записей workspace'а | `--workspace DIR`, `--all`, `--limit N`, `--state active\|restored\|missing`, `--json` |
| `gio-trash show ID` | Полные метаданные записи | `--json` |
| `gio-trash restore ID` | Вернуть запись в workspace | `--workspace DIR`, `--to PATH`, `--force`, `--json` |
| `gio-trash empty` | Удалить записи (payload + `.trashinfo`) | `--workspace DIR`, `--older-than 30d`, `--yes`, `--json` |
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

<!-- PLAN-CONTINUES -->


# dsh-trash

Обратимая корзина для файлов workspace в [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) (DSH).

`dsh-trash` — это CLI и DSH-плагин, которые перемещают файлы в системную freedesktop-корзину, сохраняют расширенные метаданные в SQLite-индексе и возвращают файлы обратно, не смешивая разные workspace'ы. Каталог репозитория исторически называется `gio_trash`; проект и CLI — `dsh-trash`.

## Зачем

В режиме `workspace-write` DSH разрешает агенту менять только файлы внутри workspace. `rm` внутри workspace работает, но необратим, а перенос в `~/.local/share/Trash` — это запись за пределами workspace, которую sandbox блокирует.

`dsh-trash` закрывает разрыв: удаление становится обратимым, а sandbox не ослабляется. Плагин запускает CLI как доверенный хост-процесс и сам проверяет режим sandbox и границы workspace.

## Возможности

- **Файлы, каталоги и симлинки** целиком попадают в корзину; симлинк не разыменовывается.
- **Freedesktop-совместимость**: payload лежит в `Trash/files`, рядом пишется `Trash/info/*.trashinfo` с исходным путём и датой удаления.
- **Метаданные**: workspace, абсолютный и относительный путь, имя, размер, SHA-256, mtime/ctime, режим, владелец, текстовая выдержка, манифест каталога.
- **Восстановление с изоляцией**: `list`/`restore` видят только записи своего workspace; цель всегда остаётся внутри его корня.
- **Конфликты**: существующий файл не перезаписывается молча — нужен `--force`, и тогда старый файл тоже уходит в корзину.
- **Обслуживание**: `doctor` сверяет индекс с корзиной, `empty` удаляет старые записи.
- **Кросс-ФС**: если корзина на другом устройстве, используется copy + fsync + unlink.
- **DSH-инструменты**: `trash`, `trash_list`, `trash_restore` с проверкой `sandboxPolicy`.

## Требования

- Linux (v1).
- Python 3.11+.
- Node.js 20+ — только для DSH-плагина.

## Установка

```sh
python3 -m pip install -e .
dsh-trash version
```

Команда `dsh-trash` должна быть в `PATH` — плагин запускает её по имени. Другой путь можно указать в конфиге плагина через `cliPath`.

Запуск без установки:

```sh
PYTHONPATH=src python3 -m dsh_trash version
```

## CLI

Все команды принимают `--json` и печатают стабильный машинный контракт. Workspace по умолчанию — текущий каталог; запускайте команды из корня workspace или передавайте `--workspace` явно.

```sh
# в корзину
dsh-trash put ./dist --workspace . --reason "cleanup" --json

# что лежит в корзине этого workspace
dsh-trash list --workspace .
dsh-trash show <id> --json

# вернуть запись
dsh-trash restore <id> --workspace .

# вернуть в другое место внутри workspace
dsh-trash restore <id> --workspace . --to build/dist

# заменить существующий файл (старый уйдёт в корзину)
dsh-trash restore <id> --workspace . --force

# сверка и очистка
dsh-trash doctor --fix
dsh-trash empty --workspace . --older-than 30d --yes
```

### Команды

| Команда | Что делает |
|---------|------------|
| `put PATH` | Перемещает путь в корзину и записывает метаданные |
| `list` | Показывает записи workspace (по умолчанию только `active`) |
| `show ID` | Печатает полные метаданные записи |
| `restore ID` | Возвращает запись в workspace |
| `empty` | Удаляет записи; без `--yes` работает как dry-run |
| `doctor` | Сверяет индекс, payload'ы и `.trashinfo` |
| `version` | Версия и пути хранилища |

Полезные флаги: `put --dry-run`, `put --no-preview`, `list --all`, `list --state`, `doctor --deep`, `empty --older-than`.

### Коды возврата

| Код | Значение |
|-----|----------|
| 0 | успех |
| 2 | неверные аргументы |
| 3 | запись не найдена / чужой workspace |
| 4 | конфликт |
| 5 | путь вне workspace или защищённый путь |
| 6 | ошибка файловой системы или корзины |
| 7 | ошибка индекса |

При `--json` ошибки печатаются как `{"error": {"code": "...", "message": "..."}}`.

## DSH-плагин

```sh
dsh plugin --profile trash add ./dsh-plugin
dsh --profile trash
```

| Инструмент | Назначение |
|------------|------------|
| `trash` | Переместить путь workspace в корзину, вернуть id |
| `trash_list` | Показать активные записи текущего workspace |
| `trash_restore` | Вернуть запись по id; `force` заменяет существующий файл |

Поведение:

- в `read-only` инструменты отказывают;
- в `workspace-write` принимаются только пути внутри `workspaceRoot`;
- `.git` защищён;
- запись в `Trash` выполняет CLI как доверенный хост-процесс, а не `ctx.fs`, поэтому sandbox не ослабляется;
- `empty` намеренно не даётся модели — очистка корзины остаётся решением человека.

Конфиг по умолчанию (`dsh-plugin/cordis.patch.yml`):

```yaml
cliPath: dsh-trash
timeoutMs: 20000
preview: true
```

## Как это работает

```
модель → DSH-плагин → dsh-trash CLI → Trash/files + Trash/info
                                            │
                                     SQLite-индекс и манифесты
```

- **Корзина**: `$XDG_DATA_HOME/Trash` (по умолчанию `~/.local/share/Trash`).
- **Индекс**: `$XDG_DATA_HOME/dsh-trash/index.sqlite3`, права 0600; каталог — 0700.
- **Манифесты каталогов**: `$XDG_DATA_HOME/dsh-trash/manifests/<id>.json`.
- **Workspace** идентифицируется по `(st_dev, st_ino)` корня, поэтому переименование каталога не меняет его id.
- **Восстановление** идёт по относительному пути от текущего корня workspace, а не по устаревшему абсолютному пути.
- **Состояния записи**: `pending → active → restored`, а также `missing` и `failed` для диагностики.

Подробная архитектура, модель данных и обоснование решений — в [PLAN.md](PLAN.md).

## Ограничения

- v1 — только Linux. Topdir-корзина для отдельных разделов, Windows и macOS запланированы на v1.1.
- `birthtime` на Linux сейчас `NULL`: `os.stat` не отдаёт statx btime.
- Корзина общая с другими приложениями: если её очистить извне, `doctor` покажет `missing`, payload восстановить нельзя.
- Одна команда `trash` — один путь; массовая уборка — несколько вызовов.
- Автоматической retention-политики нет, только ручной `empty`.
- Текстовая выдержка включена по умолчанию; для чувствительных файлов используйте `--no-preview`.
- DSH-bundle написан и покрыт тестами helpers, но ещё не прогнан end-to-end в живом DSH.

## Тесты

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
cd dsh-plugin && node --test test/*.test.js
```

Сейчас: 60 Python-тестов и 6 тестов helpers плагина.

## Структура

```
src/dsh_trash/     Python CLI: workspace, metadata, trashdir, index, engine, cli
tests/             unittest: put, restore, обслуживание, изоляция workspace
dsh-plugin/        DSH bundle: index.js, helpers.js, cordis.patch.yml
PLAN.md            архитектура, контракты, безопасность и этапы v1.1
```

## Лицензия

MIT — см. [LICENSE](LICENSE).

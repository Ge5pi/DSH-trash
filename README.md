# dsh-trash

Обратимая, ограниченная workspace'ом корзина для [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) (DSH). Каталог репозитория исторически называется `gio_trash`; проект и CLI — `dsh-trash`.

DSH в режиме `workspace-write` разрешает агенту менять только файлы внутри workspace. Удалить файл через `rm` внутри workspace можно, но это необратимо; перенести его в системную корзину (`~/.local/share/Trash`) нельзя — это запись за пределами workspace, которую sandbox блокирует. `dsh-trash` закрывает этот разрыв: отдельный CLI перемещает файлы в freedesktop-корзину, хранит расширенные метаданные в sidecar-индексе и умеет возвращать файлы, не смешивая разные workspace'ы. DSH подключает CLI как типизированные инструменты `trash`, `trash_list`, `trash_restore`.

## Ключевые свойства

- **Обратимость**: файл или каталог целиком попадает в системную корзину, а не удаляется.
- **Метаданные**: workspace, исходный и относительный путь, имя, размер, SHA-256, даты создания/изменения/удаления, режим, владелец, текстовое превью (включено по умолчанию), манифест каталога.
- **Изоляция**: `list`/`restore` видят только записи своего workspace; восстановление всегда остаётся внутри текущего корня workspace.
- **Совместимость**: корзина freedesktop (`Trash/files`, `Trash/info/*.trashinfo`), записи видны файловым менеджерам и `gio trash --list`.
- **Без ослабления sandbox**: инструмент DSH выполняет перемещение доверенным хост-кодом, а не через `ctx.fs`, и сам проверяет режим sandbox и границы workspace.

## Статус

Реализованы этапы 1–3: `put`, `list`, `show`, `restore`, `empty`, `doctor`, SQLite-sidecar, манифесты каталогов, copy-fallback между ФС и 60 тестов. Следующий этап — DSH-плагин (`trash`, `trash_list`, `trash_restore`).

План и ограничения: [PLAN.md](PLAN.md). Превью включено по умолчанию, topdir-корзина и Windows/macOS — v1.1.

## Установка

```sh
python3 -m pip install -e .
dsh-trash version
```

Без установки, из исходников:

```sh
PYTHONPATH=src python3 -m dsh_trash version
```

## Использование

```sh
# переместить файл/каталог в корзину (workspace по умолчанию — текущий каталог)
dsh-trash put ./dist --workspace . --reason "cleanup" --json

# что лежит в корзине этого workspace
dsh-trash list --workspace .
dsh-trash show <id> --json

# вернуть запись (конфликт с существующим файлом — только с --force)
dsh-trash restore <id> --workspace .

# сверка индекса и корзины, очистка старых записей
dsh-trash doctor --fix
dsh-trash empty --workspace . --older-than 30d --yes
```

`--json` печатает стабильный контракт для плагина и скриптов; коды возврата описаны в [PLAN.md](PLAN.md#5-контракт-cli).

## Тесты

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## Планируемая структура

```
src/dsh_trash/        Python CLI (stdlib), freedesktop-корзина + SQLite-индекс
tests/                pytest: CLI, индекс, восстановление, изоляция workspace
dsh-plugin/           DSH bundle: инструменты trash, trash_list, trash_restore
```

## Платформы

- **v1:** Linux.
- **v1.1:** topdir-корзина для отдельных разделов, Windows и macOS.

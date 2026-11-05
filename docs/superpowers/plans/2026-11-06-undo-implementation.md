# Откат записанных переименований: план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Дать откат отдельных строк `rename_log.csv` из CLI (`video-renamer undo`) и из TUI, с теми же проверками, что у `apply`.

**Architecture:** Журнал дописывается: строки `rename` и `undo`, состояние вычисляется при чтении. Модуль `core/rename_log.py` отвечает за формат, идентификаторы и миграцию старого файла. Модуль `core/undo.py` выбирает строки и откатывает их через `TitleService.apply_rows`. CLI и TUI только вызывают эти функции.

**Tech Stack:** Python 3.14 stdlib (`csv`, `hashlib`, `os`), `pytest`; Textual для TUI (extra `tui`).

**Spec:** `docs/superpowers/specs/2026-11-06-undo-design.md`

## Global Constraints

- Без новых обязательных зависимостей. Ядро (`rename_log`, `undo`) — только stdlib.
- Windows 10/11, Python 3.14. Подпроцессов в этой фиче нет.
- Формат журнала: заголовок `timestamp;action;undoes;root;old;new;oshash;size;scene_id;source`. Метка времени: UTC, `%Y-%m-%dT%H:%M:%S.%fZ`.
- Старый формат — ровно `timestamp;old;new;oshash;scene_id;source`. Файл старого формата переименовывается в `rename_log.legacy*.csv` рядом с ним; смешанный формат в одном файле не пишется.
- Идентификатор строки: первые 10 hex SHA-1 от `timestamp|action|root|old|new`, в нижнем регистре.
- Откат строки без OSHASH запрещён. Размер проверяется, если записан.
- Строки старого формата (`root` пуст) откатываются только с `--root` и `--confirm-legacy` (CLI) или явной отметкой в диалоге (TUI).
- `apply` для строк с размером ведёт себя как раньше. `preview.csv` читается как раньше (`_parse_int`, пустое → `0`).
- Журнал не переписывается и не удаляет строки. Запись в журнал — только дописыванием.
- Значения, попадающие в CSV, проходят `_guard_formula`, как и в `append_rename_log` сейчас.
- Ключи API и значения `.env` не попадают в журнал и вывод.

## Review Focus

1. Файл на новом пути заменён другим файлом (OSHASH не совпадает) — откат не выполняется, файл не трогается. Тест: `test_undo_refuses_when_oshash_changed` (Task 3).
2. Цепочка: строка A→B и строка B→C в одном запуске — оба откатываются в правильном порядке. Тест: `test_undo_chain_restores_both` (Task 3).
3. Строка уже откатена — повторный откат отказан, журнал не получает второй `undo`. Тест: `test_undo_rejects_already_undone_row` (Task 1 и 3).
4. Строка старого формата без `--confirm-legacy` — отказ, ничего не переименовано. Тест: `test_cli_undo_legacy_requires_confirm` (Task 4).
5. Откат прошёл, запись в журнал не удалась — строка помечена «журнал не записан», код 1, автоматического повтора нет. Тест: `test_undo_reports_unwritten_journal` (Task 3).
6. Миграция старого журнала невозможна (файл занят) — запись прервана с ошибкой, файл не смешивается. Тест: `test_append_aborts_when_legacy_rename_fails` (Task 1).
7. Два запуска с одинаковыми путями в одну секунду получают разные идентификаторы. Тест: `test_ids_differ_for_same_paths_in_same_second` (Task 1).

---

## File Structure

- Create: `video_renamer/core/rename_log.py` — формат, `LogEntry`, `entry_id`, `format_log_timestamp`, `read_log`, `append_log`, `active_renames`, `LogMigrationError`.
- Create: `video_renamer/core/undo.py` — `select_for_undo`, `run_undo`, `UndoSelection`, `UndoReport`.
- Modify: `video_renamer/core/title_service.py` — `PreviewRow.size: int | None`, пропуск проверки размера при `None`.
- Modify: `video_renamer/cli.py` — `append_rename_log` через `rename_log`, `write_preview_csv` пишет пустой размер для `None`, подкоманда `undo`, `_run_apply` передаёт корень.
- Modify: `video_renamer/tui.py`, `video_renamer/tui_model.py` — экран журнала, диалог отката, единый формат времени, `_apply_worker_impl` передаёт корень.
- Modify: `README.md`, `CHANGELOG.md`, `docs/architecture.md` — описание команды, формата и отката.
- Test: `tests/test_rename_log.py` (новый), `tests/test_undo.py` (новый), изменения в `tests/test_fix_video_titles.py`, `tests/test_title_service.py`, `tests/test_tui.py`.

Порядок задач: 1 → 2 → 3 → 4 → 5 → 6. Задачи 4 и 5 зависят от 3; 6 — от всех.

---

### Task 1: Модуль журнала `rename_log`

**Files:**
- Create: `video_renamer/core/rename_log.py`
- Test: `tests/test_rename_log.py`

**Interfaces:**
- Consumes: —
- Produces:
  - `format_log_timestamp(moment: datetime | None = None) -> str` — UTC, `%Y-%m-%dT%H:%M:%S.%fZ`.
  - `entry_id(timestamp: str, action: str, root: str, old: str, new: str) -> str`.
  - `@dataclass(frozen=True) class LogEntry`: `timestamp, action, undoes, root, old, new, oshash, size: int | None, scene_id, source, legacy: bool`, свойство `id -> str` = `entry_id(timestamp, action, root, old, new)`.
  - `read_log(path: str) -> list[LogEntry]` — читает `path` и все файлы `rename_log.legacy*.csv` той же папки; отсутствующий файл — пустой список.
  - `active_renames(entries: Sequence[LogEntry]) -> list[LogEntry]` — `rename`, на которые нет `undo` с `undoes == id`.
  - `append_log(path: str, entries: Sequence[LogEntry]) -> None` — пишет заголовок новой версии при создании, мигрирует старый файл. Поднимает `LogMigrationError(OSError)`, если миграция невозможна; в этом случае ничего не пишет.

- [ ] **Step 1: Тесты идентификатора и времени**

```python
def test_ids_differ_for_same_paths_in_same_second():
    t1 = "2026-11-06T10:00:00.000001Z"
    t2 = "2026-11-06T10:00:00.000002Z"
    assert entry_id(t1, "rename", "C:/r", "a.mp4", "b.mp4") != entry_id(t2, "rename", "C:/r", "a.mp4", "b.mp4")


def test_format_log_timestamp_has_microseconds_and_z():
    value = format_log_timestamp(datetime(2026, 11, 6, 10, 0, 0, 5, tzinfo=timezone.utc))
    assert value == "2026-11-06T10:00:00.000005Z"


def test_entry_id_is_10_lowercase_hex():
    value = entry_id("t", "rename", "r", "a", "b")
    assert len(value) == 10 and value == value.lower()
```

- [ ] **Step 2: Тесты чтения и состояния**

```python
def test_read_log_missing_file_is_empty(tmp_path):
    assert read_log(str(tmp_path / "none.csv")) == []


def test_read_log_parses_new_format(tmp_path):
    path = tmp_path / "rename_log.csv"
    append_log(str(path), [rename_entry(root=str(tmp_path), old="a.mp4", new="b.mp4")])
    entries = read_log(str(path))
    assert [(e.old, e.new, e.legacy) for e in entries] == [("a.mp4", "b.mp4", False)]


def test_read_log_parses_legacy_rows_with_empty_root(tmp_path):
    path = tmp_path / "rename_log.csv"
    path.write_text("timestamp;old;new;oshash;scene_id;source\n2026-11-04T12:00:00Z;a.mp4;b.mp4;h;s;fingerprint\n", encoding="utf-8-sig")
    entries = read_log(str(path))
    assert entries[0].legacy is True
    assert entries[0].root == "" and entries[0].size is None


def test_active_renames_hides_undone_rows(tmp_path):
    rename = rename_entry(root="R", old="a.mp4", new="b.mp4")
    undo = undo_entry_for(rename, timestamp="2026-11-06T11:00:00.000000Z")
    assert active_renames([rename, undo]) == []


def test_undo_row_cannot_be_undone_again(tmp_path):
    rename = rename_entry(root="R", old="a.mp4", new="b.mp4")
    undo = undo_entry_for(rename, timestamp="2026-11-06T11:00:00.000000Z")
    assert rename.id not in {e.id for e in active_renames([rename, undo])}
```

`rename_entry(...)` и `undo_entry_for(...)` — вспомогательные функции в тесте: первая строит `LogEntry(action="rename")` с заданными полями и текущей меткой, вторая — `LogEntry(action="undo", undoes=rename.id, ...)` с полями, переставленными местами (`old` = имя после переименования, `new` = исходное имя).

- [ ] **Step 3: Тесты миграции**

```python
def test_append_migrates_legacy_file_once(tmp_path):
    path = tmp_path / "rename_log.csv"
    path.write_text("timestamp;old;new;oshash;scene_id;source\nT;a;b;h;s;f\n", encoding="utf-8-sig")
    append_log(str(path), [rename_entry(root="R", old="c.mp4", new="d.mp4")])
    assert (tmp_path / "rename_log.legacy.csv").exists()
    assert path.read_text(encoding="utf-8-sig").splitlines()[0].startswith("timestamp;action;undoes")
    assert len(read_log(str(path))) == 2


def test_append_aborts_when_legacy_rename_fails(tmp_path, monkeypatch):
    path = tmp_path / "rename_log.csv"
    path.write_text("timestamp;old;new;oshash;scene_id;source\nT;a;b;h;s;f\n", encoding="utf-8-sig")
    monkeypatch.setattr(rename_log_module.os, "replace", boom_oserror)
    with pytest.raises(LogMigrationError):
        append_log(str(path), [rename_entry(root="R", old="c.mp4", new="d.mp4")])
    assert path.read_text(encoding="utf-8-sig").splitlines()[0].startswith("timestamp;old;new")


def test_append_writes_bom_only_to_new_file(tmp_path):
    path = tmp_path / "rename_log.csv"
    append_log(str(path), [rename_entry(root="R", old="a.mp4", new="b.mp4")])
    append_log(str(path), [rename_entry(root="R", old="c.mp4", new="d.mp4")])
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    assert raw.count(b"\xef\xbb\xbf") == 1
```

- [ ] **Step 4: Запустить тесты, убедиться в падении**

Run: `python -m pytest tests/test_rename_log.py -q -p no:cacheprovider`
Expected: FAIL (модуля нет).

- [ ] **Step 5: Реализовать модуль.** Миграция: если `path` существует, непуст и заголовок равен старому — переименовать через `os.replace` в `rename_log.legacy.csv` (если занято — `rename_log.legacy-YYYYMMDD.csv`, затем `-2`, `-3`). При ошибке `os.replace` поднять `LogMigrationError`. Запись строк — `csv.writer` с `delimiter=";"`, поля проходят `_guard_formula`/`_unguard_formula` из `cli.py` (перенести их в `rename_log`, `cli` реэкспортирует). Пустой `size` пишется как пустая ячейка.

- [ ] **Step 6: Запустить тесты, убедиться в прохождении**

Run: `python -m pytest tests/test_rename_log.py -q -p no:cacheprovider`
Expected: PASS.

---

### Task 2: Интеграция с apply и единый формат времени

**Files:**
- Modify: `video_renamer/core/title_service.py` (`PreviewRow.size`, `_prepare_apply_item`)
- Modify: `video_renamer/cli.py` (`write_preview_csv`, `append_rename_log`, `_run_apply`)
- Modify: `video_renamer/tui.py` (`_apply_worker_impl`)
- Test: `tests/test_title_service.py`, `tests/test_fix_video_titles.py`

**Interfaces:**
- Consumes: `rename_log.LogEntry`, `append_log`, `format_log_timestamp` (Task 1).
- Produces:
  - `PreviewRow.size: int | None`.
  - `append_rename_log(path: str, rows, results, timestamp: str, *, root: str) -> None` — пишет строки `rename` через `append_log`, `root` — абсолютный путь корня. Сохраняет поведение «только renamed».
  - `write_preview_csv` пишет пустую ячейку для `size is None`.

- [ ] **Step 1: Тест пропуска проверки размера**

```python
def test_apply_skips_size_check_when_size_unknown(tmp_path):
    write(tmp_path / "a.mp4", size=50)
    row = PreviewRow(True, "a.mp4", "b.mp4", "fingerprint", 1.0, "s", "", "h", None)
    report = TitleService(str(tmp_path), {}, None).apply_rows([row], dry_run=False, ...)
    assert report.results[0].status == "renamed"
```

Заметка: `_prepare_apply_item` проверяет `source_stat.st_size != row.size` только при `row.size is not None`. OSHASH проверяется всегда, как раньше. Тест проверяет, что при совпадении OSHASH строка без размера откатывается.

- [ ] **Step 2: Тест, что размер при `None` не ломает существующие пути**

```python
def test_apply_still_rejects_stale_size_when_known(tmp_path):
    ...  # существующий тест test_apply_rejects_stale_size_and_oshash остаётся без изменений
```

- [ ] **Step 3: Обновить тесты журнала в `test_fix_video_titles.py`.** Тесты `test_append_rename_log_only_renamed_and_header_once` и `test_append_rename_log_writes_all_six_fields` ожидают старый заголовок. Перевести на новый формат (10 полей, `action=rename`, `root`). Это намеренное изменение формата, фиксируется в журнале задач.

- [ ] **Step 4: Запустить тесты, убедиться в падении**

Run: `python -m pytest tests/test_title_service.py tests/test_fix_video_titles.py -q -p no:cacheprovider -k "size or rename_log"`
Expected: FAIL.

- [ ] **Step 5: Реализовать.** `append_rename_log` — тонкая обёртка над `rename_log`. `_run_apply` передаёт `root=root`, `_apply_worker_impl` (TUI) — `root=str(self.model.folder)` и `format_log_timestamp()` вместо `isoformat()`. Старое имя `append_rename_log` в `cli.py` сохраняется.

- [ ] **Step 6: Запустить весь набор**

Run: `python -m pytest -q -p no:cacheprovider`
Expected: PASS (TUI-тесты требуют Textual; в системном Python пропускаются).

---

### Task 3: Движок отката `core/undo.py`

**Files:**
- Create: `video_renamer/core/undo.py`
- Test: `tests/test_undo.py`

**Interfaces:**
- Consumes: `LogEntry`, `read_log`, `active_renames`, `append_log`, `format_log_timestamp`, `entry_id` (Task 1); `PreviewRow`, `TitleService.apply_rows` (Task 2).
- Produces:
  - `@dataclass(frozen=True) class UndoTarget`: `entry: LogEntry`, `root: str`.
  - `@dataclass(frozen=True) class UndoSelection`: `targets: tuple[UndoTarget, ...]`, `errors: tuple[str, ...]`.
  - `select_for_undo(entries: Sequence[LogEntry], ids: Sequence[str], *, root_override: str | None = None, confirm_legacy: bool = False) -> UndoSelection`. Ошибки: id не найден; id неактивен; строка старого формата без корня; строка старого формата без `confirm_legacy`. Дубликаты id схлопываются. Порядок `targets` — от самой новой строки к самой старой (обратный порядок).
  - `@dataclass(frozen=True) class UndoResult`: `id, old_rel, new_rel, status, message` (`status`: `renamed`, `error`, `dry-run`, `journal-missing`).
  - `@dataclass(frozen=True) class UndoReport`: `results: tuple[UndoResult, ...]`, `had_errors: bool`.
  - `run_undo(selection: UndoSelection, *, log_path: str, dry_run: bool, now: datetime | None = None) -> UndoReport` — группирует цели по корню, для каждого корня вызывает `TitleService(root, {}, None).apply_rows(rows, dry_run=dry_run)`, где строка `PreviewRow(approved=True, old_rel=entry.new, new_rel=entry.old, source=entry.source, confidence=0.0, scene_id=entry.scene_id, note="undo", oshash=entry.oshash, size=entry.size, candidates=())`. В боевом режиме успешные строки дописываются через `append_log` как `undo` с `undoes = entry.id`. Если `append_log` не сработал, все `renamed` строки этого запуска получают статус `journal-missing` и сообщение «откат выполнен, журнал не записан: <id>», `had_errors = True`.

- [ ] **Step 1: Тесты выбора строк**

```python
def test_select_rejects_unknown_and_inactive_ids(...)  # ошибки для неизвестного id и для уже откатённой строки
def test_select_legacy_requires_root_and_confirm(...)   # без root — ошибка; без confirm — ошибка; с обоими — цель
def test_select_orders_newest_first(...)                 # две строки: цели в порядке от новой к старой
def test_select_deduplicates_ids(...)
```

- [ ] **Step 2: Тесты отката на диске** (во временной папке, файлы реальные)

```python
def test_undo_restores_file_and_appends_undo_row(tmp_path): ...
def test_undo_refuses_when_oshash_changed(tmp_path): ...      # Review Focus 1
def test_undo_chain_restores_both(tmp_path): ...               # Review Focus 2
def test_undo_rejects_already_undone_row(tmp_path): ...        # Review Focus 3, через run_undo второй раз
def test_undo_dry_run_changes_nothing(tmp_path): ...
def test_undo_reports_unwritten_journal(tmp_path, monkeypatch): ...  # Review Focus 5
```

Для chain-теста: в одном запуске apply переименованы строка 1 `a.mp4 → b.mp4` и строка 2 `b.mp4 → c.mp4` (имя `b.mp4` освобождается второй строкой и занимается первой). Откат: строка 2 даёт `c.mp4 → b.mp4`, строка 1 даёт `b.mp4 → a.mp4`. Порядок от новой к старой; `apply_rows` сам разрешает занятость через двухфазную логику. Тест проверяет итоговое состояние диска: `a.mp4` существует, `b.mp4` и `c.mp4` нет.

- [ ] **Step 3: Запустить тесты, убедиться в падении**

Run: `python -m pytest tests/test_undo.py -q -p no:cacheprovider`
Expected: FAIL.

- [ ] **Step 4: Реализовать** `select_for_undo` и `run_undo`. Группировка по корню: словарь `root -> [targets]`, внутри группы сохраняется порядок. `apply_rows` возвращает результаты в порядке входа — сопоставлять по индексу. Для `dry_run` журнал не пишется.

- [ ] **Step 5: Запустить тесты, убедиться в прохождении**

Run: `python -m pytest tests/test_undo.py -q -p no:cacheprovider`
Expected: PASS.

---

### Task 4: CLI — подкоманда `undo`

**Files:**
- Modify: `video_renamer/cli.py` (`_build_parser`, `main`, новая `_run_undo`)
- Test: `tests/test_fix_video_titles.py`

**Interfaces:**
- Consumes: `read_log`, `active_renames` (Task 1); `select_for_undo`, `run_undo` (Task 3).
- Produces: `video-renamer undo --list [--log PATH]`, `video-renamer undo --ids ID[,ID...] [--log PATH] [--root PATH] [--apply] [--confirm-legacy]`.

Вывод `--list`: строки `id<TAB>старое → новое<TAB>корень или (старый формат)<TAB>время`. Вывод `--ids`: по строке на результат, в конце сводка `undo: N renamed, M error(s)` (как у apply). Dry-run печатает «would rename». Код: 0 — без ошибок; 1 — были ошибки строк или `journal-missing`; 2 — неверные аргументы или нечитаемый журнал или `LogMigrationError`.

- [ ] **Step 1: Тесты**

```python
def test_cli_undo_list_prints_active_rows(tmp_path, capsys): ...
def test_cli_undo_dry_run_changes_nothing(tmp_path): ...
def test_cli_undo_apply_restores_file(tmp_path): ...
def test_cli_undo_legacy_requires_confirm(tmp_path, capsys): ...   # Review Focus 4
def test_cli_undo_unknown_id_exits_1(tmp_path, capsys): ...
def test_cli_undo_missing_log_exits_2(tmp_path, capsys): ...
```

- [ ] **Step 2: Запустить, убедиться в падении**

Run: `python -m pytest tests/test_fix_video_titles.py -q -p no:cacheprovider -k cli_undo`
Expected: FAIL.

- [ ] **Step 3: Реализовать** подкоманду. Аргументы `--ids` разбираются по запятым, пробелы удаляются. По умолчанию `--log` — `DEFAULT_LOG`. `--root` без `--ids`/`--list` игнорируется с предупреждением.

- [ ] **Step 4: Запустить весь файл**

Run: `python -m pytest tests/test_fix_video_titles.py -q -p no:cacheprovider`
Expected: PASS.

---

### Task 5: TUI — экран журнала и откат

**Files:**
- Modify: `video_renamer/tui.py` (новые классы `LogScreen`, `ConfirmUndo`; привязка `u` в `RenamerApp.BINDINGS`; обработчик)
- Modify: `video_renamer/tui_model.py` (при необходимости — вспомогательные функции списка журнала без Textual)
- Test: `tests/test_tui.py`, `tests/test_tui_model.py`

**Interfaces:**
- Consumes: `read_log`, `active_renames` (Task 1); `select_for_undo`, `run_undo`, `UndoReport` (Task 3); `LOG_PATH`.
- Produces: `LogScreen(ModalScreen[None])`, `ConfirmUndo(ModalScreen[bool])`. Привязка `Binding("u", "undo_log", "Журнал")` в `RenamerApp`.

Поведение: `u` открывает `LogScreen` с активными строками из `LOG_PATH`. `Space` отмечает строку. «Откатить выбранные» открывает `ConfirmUndo` с dry-run предпросмотром (вычисляется в фоновом рабочем потоке, как `apply`). Если среди выбранных есть строки старого формата, в `ConfirmUndo` обязательна отметка (`Checkbox`) «подтверждаю откат строк старого формата»; кнопка «Откатить» неактивна без неё. Корень для строк старого формата — текущая папка TUI (`self.model.folder`). После отката показывается `ApplyResultModal`-подобный отчёт с числом откатов и ошибок. `Esc` закрывает экран.

- [ ] **Step 1: Тест модели списка** (без Textual, `tests/test_tui_model.py`):

```python
def test_log_rows_flag_legacy_entries(): ...
```

- [ ] **Step 2: Тесты экрана** (`tests/test_tui.py`, с `run_test`; пропускаются без Textual):

```python
async def test_undo_screen_lists_active_rows(...): ...
async def test_legacy_selection_needs_checkbox(...): ...    # Review Focus 4 в TUI
async def test_undo_confirmed_restores_file(...): ...
```

- [ ] **Step 3: Запустить в venv с Textual**

Run: `& "C:\Users\isoko\AppData\Local\Temp\opencode\venv-tui\Scripts\python.exe" -m pytest tests/test_tui.py tests/test_tui_model.py -q -p no:cacheprovider`
Expected: FAIL до реализации; PASS после.

- [ ] **Step 4: Реализовать** экран по образцу `ConfirmApply` и `ApplyResultModal` (те же `Binding`, `dismiss`, `@work(thread=True)`). Отметки храни в `set` идентификаторов. Dry-run и откат — только через `run_undo`.

- [ ] **Step 5: Запустить весь набор в venv**

Run: `& "C:\Users\isoko\AppData\Local\Temp\opencode\venv-tui\Scripts\python.exe" -m pytest -q -p no:cacheprovider`
Expected: PASS без пропусков.

---

### Task 6: Документация и проверка на реальной папке

**Files:**
- Modify: `README.md` (раздел про `undo`, формат журнала, флаги), `CHANGELOG.md` (раздел «Unreleased»), `docs/architecture.md` (формат журнала в разделе 6 CSV-ревью, ссылка на спецификацию отката).

- [ ] **Step 1: Обновить документы.** Описать: `undo --list`, `undo --ids`, `--root`, `--confirm-legacy`, `--apply`; формат журнала; миграцию старого файла; что откат не удаляет строки журнала.

- [ ] **Step 2: Полный прогон**

Run: `& "C:\Users\isoko\AppData\Local\Temp\opencode\venv-tui\Scripts\python.exe" -m pytest -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 3: Проверка на реальной папке** (только чтение журнала и dry-run): `python -m video_renamer.cli undo --list --log <путь к реальному журналу>` (если журнала нет — код 0, пустой список). Затем `undo --ids <id> --root D:\Videos\PH` без `--apply`. Ничего не переименовывать без явного согласования с пользователем.

---

## Self-Review

**1. Покрытие спецификации:** формат журнала и миграция — Task 1; `id` и состояние — Task 1; откат с проверками и цепочками — Task 3; `apply` с `None`-размером — Task 2; CLI — Task 4; TUI — Task 5; корень и единый формат времени — Task 2; документы — Task 6. Решение «`--confirm-legacy`» — Tasks 3–5. Открытых пунктов спецификации нет.

**2. Соответствие шагов:** каждый шаг с тестом содержит имя теста и смысл проверки; тела функций не даны, кроме инструкций к миграции (порядок переименования и суффиксы).

**3. Согласованность типов:** `LogEntry.size: int | None` (Task 1) совпадает с `PreviewRow.size: int | None` (Task 2). `append_rename_log(..., *, root)` (Task 2) вызывается из `_run_apply` и TUI. `select_for_undo`/`run_undo` (Task 3) вызываются из Tasks 4–5 с теми же именами.

**4. Review Focus:** все семь строк имеют тест в указанной задаче.

**Отклонение от спецификации:** отдельный метод `undo_rows` не добавляется. Откат использует `TitleService.apply_rows` с обратными строками, поскольку `apply_rows` уже содержит все проверки и двухфазную логику. Изменение `size` в `_prepare_apply_item` даёт поддержку строк без размера. Решение зафиксировано в журнале задач.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-11-06-undo-implementation.md`. Please review the plan. Which execution approach would you prefer?

- **Subagent-driven** — отдельный субагент на каждую задачу, после каждой задачи проверка свежим ревьюером, в конце ревью всей ветки. Надёжнее, но дороже по контексту.
- **Native** — я выполняю все задачи сам в этой сессии, в конце один ревьюер проверяет результат. Быстрее и дешевле, но независимой проверки по ходу не будет.

Рекомендую **Native**: задачи идут последовательно и опираются друг на друга (`LogEntry`, `apply_rows`, `run_undo`), а большая часть проверок — тесты на реальных файлах во временной папке. Какой вариант берём?

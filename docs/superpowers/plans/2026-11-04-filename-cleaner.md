# Filename Cleaner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Скрипт чистит мусорные имена в D:\Videos\PH в шаблон `Студия - ДД.ММ.ГГГГ - Актер1, Актер2 - Название`.

**Architecture:** Один Python-файл без зависимостей + studios.json рядом; сначала preview.csv/dry-run, переименование только с флагом --apply; расширение файла никогда не трогается (работа только со stem).

**Tech Stack:** Python 3.10+ stdlib (pathlib, re, argparse, csv, json), Windows, pytest для тестов.

**Spec:** Короткий дизайн согласован в чате 2026-11-04: студия нормализуется по словарю (brazzersexxtra -> BrazzersExxtra), дата -> ДД.ММ.ГГГГ, техмусор 1080p/4K/XXX/2160p/(1)/(720)/(1080) резать, осмысленное (Remastered)/(Part 2) оставлять, разделитель ` - `, расширение .mp4 сохранять всегда.

## Global Constraints

- Windows + Python 3 без pip-пакетов.
- Работать только со stem (Path.stem); Path.suffix сохраняется байт-в-байт, скрипт никогда не удаляет и не меняет расширение.
- Разделитель итогового имени ` - ` (ASCII дефис с пробелами).
- Дата на выходе строго `ДД.ММ.ГГГГ`; вход `ГГ.ММ.ДД` трактуется как 20ГГ при ГГ<=30 иначе 19ГГ; вход `ГГГГ.ММ.ДД` и `ДД.ММ.ГГГГ`/`(ГГГГ.ММ.ДД)` тоже принимаются.
- Техмусор резать: `\b(1080p|720p|2160p|4k|8k|xxx|hd|fullhd)\b`, `\(\d{3,4}\)`, `\(\d\)` как дубль, отдельные токены `XXX`; оставлять `(Remastered)`, `(Part \d+)`, `Part \d+`.
- Dry-run по умолчанию; реальное переименование только с `--apply`; всегда пишется preview.csv `old;new`.
- При коллизии добавлять ` (2)`, `(3)`; уже чистые имена идемпотентны (повторный прогон ничего не меняет).

## Review Focus

- Уже чистое имя `Vrbangers - 22.12.2025 - Gabbie Carter - Title` не должно меняться при повторном прогоне — ожидается байт-в-байт то же имя.
- Два разных мусора дают одно чистое имя — ожидается суффикс ` (2)` вместо затирания.
- Файл без расширения или со странным регистром `.MP4` — ожидается сохранение исходного суффикса как есть.
- Юникод `Olivia del Río.mp4` — ожидается сохранение `í` без кракозябр.
- Очень длинное имя >240 символов после чистки — ожидается обрезка title до лимита без ломки расширения.

---

### Task 1: Extension-safe ядро + снос техмусора

**Files:**
- Create: `clean_ph_names.py`
- Test: `tests/test_clean_ph_names.py`

**Interfaces:**
- Consumes: ничего (первая задача).
- Produces:
  - `split_name(filename: str) -> tuple[str, str]` — возвращает (stem, suffix); suffix включает точку, может быть ''.
  - `strip_tech_junk(stem: str) -> str` — режет техмусор, чистит разделители, схлопывает пробелы.

- [ ] **Step 1: Write the failing test**

```python
def test_split_name_keeps_mp4_suffix():
    assert split_name("foo.1080p.mp4") == ("foo.1080p", ".mp4")

def test_split_name_upper_mp4_kept():
    assert split_name("foo.MP4") == ("foo", ".MP4")

def test_strip_tech_junk_cuts_quality_and_dubs():
    assert strip_tech_junk("AdultTime.Ebony.Scene.4.XXX.2160p(1)") == "AdultTime Ebony Scene 4"

def test_strip_tech_junk_keeps_remastered():
    assert "Remastered" in strip_tech_junk("Title (Remastered) 1080p")

def test_unicode_stem_untouched():
    assert strip_tech_junk("Olivia del Río 1080p") == "Olivia del Río"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_clean_ph_names.py -v`
Expected: FAIL with "split_name not defined" / "strip_tech_junk not defined"

- [ ] **Step 3: Implement `split_name(filename: str) -> tuple[str, str]` и `strip_tech_junk(stem: str) -> str` в `clean_ph_names.py`**

Подход: только pathlib/os.path.splitext для suffix; дальше regex на stem: сначала вырезать `\(\d{3,4}\)` и `\(\d\)`-дубли отдельно от `(Remastered|Part\s*\d+)`, потом `\b(1080p|720p|2160p|4k|8k|xxx|hd)\b` case-insensitive, потом нормализация разделителей `[._+]+` -> пробел.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_clean_ph_names.py -v`
Expected: PASS (4 теста)

- [ ] **Step 5: Commit**

```bash
git add clean_ph_names.py tests/test_clean_ph_names.py
git commit -m "feat: extension-safe core and tech-junk stripping"
```

### Task 2: Даты + словарь студий

**Files:**
- Create: `studios.json`
- Modify: `clean_ph_names.py`
- Test: `tests/test_clean_ph_names.py`

**Interfaces:**
- Consumes: `strip_tech_junk` из Task 1.
- Produces:
  - `load_studios(path: str) -> dict[str, str]` — ключи lower без пробелов, значения дисплейные.
  - `extract_date(stem: str) -> tuple[str, str | None]` — возвращает (stem_без_даты, дата_ДД.ММ.ГГГГ или None).
  - `extract_studio(stem: str, studios: dict[str, str]) -> tuple[str | None, str]`

- [ ] **Step 1: Write the failing test**

```python
def test_extract_date_yy_mm_dd():
    rest, d = extract_date("brazzersexxtra 26.10.03 ryan reid")
    assert d == "03.10.2026"

def test_extract_date_yyyy_mm_dd():
    rest, d = extract_date("Title 2023.10.14 rest")
    assert d == "14.10.2023"

def test_extract_studio_from_dict():
    studios = {"brazzersexxtra": "BrazzersExxtra"}
    studio, rest = extract_studio("brazzersexxtra ryan reid", studios)
    assert studio == "BrazzersExxtra"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_clean_ph_names.py -v -k "date or studio"`
Expected: FAIL with "extract_date not defined"

- [ ] **Step 3: Implement `load_studios`, `extract_date`, `extract_studio` в `clean_ph_names.py`, создать `studios.json`**

`studios.json` начальный набор: brazzersexxtra, hussiepass, julesjordan, tonightsgirlfriend, pornworld, vrbangers, adulttime + по файлам D:\Videos\PH. `extract_date` ищет `(\d{2})\.(\d{2})\.(\d{2})`, `(\d{4})\.(\d{2})\.(\d{2})`, `(\d{2})\.(\d{2})\.(\d{4})` и вариант в скобках; правило 20ГГ при ГГ<=30.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_clean_ph_names.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add clean_ph_names.py studios.json tests/test_clean_ph_names.py
git commit -m "feat: date normalization and studio dictionary"
```

### Task 3: Актеры vs название

**Files:**
- Modify: `clean_ph_names.py`
- Test: `tests/test_clean_ph_names.py`

**Interfaces:**
- Consumes: `strip_tech_junk`, `extract_date`, `extract_studio` из Task 1-2.
- Produces:
  - `split_camel(s: str) -> str` — `PhillipsJenna` -> `Phillips Jenna`.
  - `split_actors_title(rest: str) -> tuple[list[str], str]` — actors титульным регистром, title в Title Case без ломки `(Remastered)`.
  - `build_new_name(studio: str | None, date: str | None, actors: list[str], title: str) -> str` — склейка через ` - `, пропуск пустых блоков.
  - `clean_stem(stem: str, studios: dict[str, str]) -> str` — полный конвейер для stem.

- [ ] **Step 1: Write the failing test**

```python
def test_split_camel_joined_names():
    assert split_camel("PhillipsJenna") == "Phillips Jenna"

def test_split_actors_title_comma():
    actors, title = split_actors_title("Lily Phillips, Martina Smeraldi DOUBLE PENETRATION BANG")
    assert actors == ["Lily Phillips", "Martina Smeraldi"]

def test_clean_stem_vrbangers_example():
    studios = {"vrbangers": "Vrbangers"}
    out = clean_stem("vrbangers 22.12.2025 Gabbie Carter, Karma Rx Sorority Hookup Party Never Ends (Remastered)", studios)
    assert out == "Vrbangers - 22.12.2025 - Gabbie Carter, Karma Rx - Sorority Hookup Party Never Ends (Remastered)"

def test_eporner_prefix_stripped():
    studios = {}
    out = clean_stem("Eporner Com [4Jfvjnyhvqb] Adriana Lorenzi", studios)
    assert "Eporner" not in out and "Adriana Lorenzi" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_clean_ph_names.py -v -k "camel or actors or vrbangers or eporner"`
Expected: FAIL

- [ ] **Step 3: Implement `split_camel`, `split_actors_title`, `build_new_name`, `clean_stem` в `clean_ph_names.py`**

Алгоритм только здесь кодом: сначала `split_camel` regex `(?<=[a-z])(?=[A-Z])`; снести префикс `Eporner.Com - [hash]`; запятая/точка как граница актеров; пара `Имя Фамилия` = два титульных слова подряд в начале; остаток = title.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_clean_ph_names.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add clean_ph_names.py tests/test_clean_ph_names.py
git commit -m "feat: actors/title split and full stem pipeline"
```

### Task 4: CLI dry-run + preview + безопасное переименование

**Files:**
- Modify: `clean_ph_names.py`
- Test: `tests/test_clean_ph_names.py`

**Interfaces:**
- Consumes: `split_name`, `clean_stem`, `load_studios` из Task 1-3.
- Produces:
  - `preview_directory(directory: str, studios: dict[str, str]) -> list[tuple[str, str]]` — список (old, new) без записи.
  - CLI: `clean_ph_names.py <dir> [--apply] [--csv preview.csv] [--recursive]`; без `--apply` только печать + csv.

- [ ] **Step 1: Write the failing test**

```python
def test_preview_idempotent_and_collision(tmp_path):
    (tmp_path / "a 1080p.mp4").touch()
    (tmp_path / "a XXX.mp4").touch()
    rows = preview_directory(str(tmp_path), {})
    assert rows[0][1] != rows[1][1]
    assert rows[0][1].endswith(".mp4")

def test_preview_idempotent_clean_name(tmp_path):
    (tmp_path / "Vrbangers - 22.12.2025 - Gabbie Carter - Title.mp4").touch()
    rows = preview_directory(str(tmp_path), {"vrbangers": "Vrbangers"})
    assert rows[0][0] == rows[0][1]

def test_preview_truncates_very_long(tmp_path):
    (tmp_path / ("a" * 250 + " 1080p.mp4")).touch()
    rows = preview_directory(str(tmp_path), {})
    assert len(rows[0][1]) <= 255 and rows[0][1].endswith(".mp4")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_clean_ph_names.py -v -k "preview or collision"`
Expected: FAIL with "preview_directory not defined"

- [ ] **Step 3: Implement `preview_directory` и `main()` с argparse в `clean_ph_names.py`**

Только печать/запись csv в dry-run; с `--apply` — `os.rename` с проверкой коллизий ` (2)` и пропуском если `old == new`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_clean_ph_names.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add clean_ph_names.py tests/test_clean_ph_names.py
git commit -m "feat: dry-run CLI with preview csv and collision-safe rename"
```

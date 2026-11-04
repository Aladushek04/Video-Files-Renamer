# Video Title Fixer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Собрать локальный CLI, который находит видео в StashDB по OSHASH или тексту, предлагает имя `Студия - ДД.ММ.ГГГГ - Исполнители - Название`, пишет CSV на ревью и переименовывает только одобренные строки. Плюс скрапер всех студий StashDB в `studios.json`.

**Architecture:** Плоские stdlib-модули рядом с существующим `clean_ph_names.py`. `title_service` — общее ядро для CLI и будущего TUI; `fix_video_titles.py` и `scrape_studios.py` — тонкие CLI-адаптеры. Сеть — `urllib.request`, кэш — SQLite, отпечаток — OSHASH, без pip-зависимостей.

**Tech Stack:** Python 3.14 stdlib (`urllib`, `sqlite3`, `csv`, `json`, `difflib`, `argparse`), pytest, Windows.

**Spec:** `docs/superpowers/specs/2026-11-04-video-title-fixer-design.md`

## Global Constraints

- Python 3.14, только stdlib. Никаких `httpx`, `requests`, `rich` в core.
- StashDB: `https://stashdb.org/graphql`, заголовок `ApiKey`, не Bearer.
- `findScenesBySceneFingerprints` — максимум 40 сцен за запрос.
- OSHASH: `size + little-endian uint64 первых и последних 64 КБ`, 16 hex.
- CSV: `;`, `utf-8-sig`, колонки `approved;old;new;source;confidence;scene_id;note;oshash;size`.
- `preview` не меняет файлы; `apply` без `--apply` — dry-run.
- `approved=y` по умолчанию только для однозначного OSHASH и валидного имени.
- Ключ только из `STASHDB_API_KEY`; не печатать, не писать в CSV/логи.
- Неверный ключ — фатально (код 2, CSV не пишется). Временная ошибка — строка
  `stashdb_error`, остальные обрабатываются, код 1.
- Windows: санитизация имён, запрет path traversal, сохранение расширения.
- Проект не под git. Шаг «Commit» заменён на checkpoint: тесты + запись файлов.
  Если пользователь позже инициализирует git, эти шаги станут коммитами.
- TUI — отдельная вторая фаза, в этот план не входит. `title_service` обязан
  быть пригоден для TUI: без stdout и без CSV внутри.
- PHASH и MD5 целого файла — не в этом плане.

## Review Focus

1. **Точность OSHASH.** Неверный алгоритм молча даёт ноль совпадений; тесты
   обязаны совпасть с векторами Go-тестов Stash.
2. **CSV-ревью и apply.** Одобренные строки, stale-хэш, ручные правки и коллизии
   не должны переименовать не тот файл; проверять OSHASH/size и двухфазность.
3. **Ошибки StashDB.** Неверный ключ не должен выглядеть как «просто нет
   совпадений»; временный сбой не должен ронять весь прогон.
4. **Windows-имена.** Запрещённые символы, `CON`, длина >255, Unicode и `.MP4`
   должны давать предсказуемый результат в preview и apply.
5. **Возобновление и идемпотентность.** Скрапер студий и кэш должны переживать
   обрыв, повторный запуск и отрицательный результат.

---

### Task 1: OSHASH

**Files:**
- Create: `oshash.py`
- Test: `tests/test_oshash.py`

**Interfaces:**
- Consumes: ничего.
- Produces:
  - `class OshashError(ValueError)`
  - `oshash_from_path(path: str | os.PathLike[str]) -> str` — 16 hex, lowercase.

- [ ] **Step 1: Write the failing test**

```python
import pytest
from oshash import OshashError, oshash_from_path

def test_oshash_known_vectors(tmp_path):
    p = tmp_path / "hello.bin"
    p.write_bytes(b"hello world")
    assert oshash_from_path(p) == "d3e392dee38cd4df"

    p.write_bytes(b"this is a test" * 15)
    assert oshash_from_path(p) == "6a0eba04654d0b9b"

def test_oshash_rejects_tiny_file(tmp_path):
    p = tmp_path / "tiny.bin"
    p.write_bytes(b"hello")
    with pytest.raises(OshashError):
        oshash_from_path(p)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_oshash.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'oshash'`

- [ ] **Step 3: Implement `oshash_from_path`**

`chunk = 64 * 1024`; при `size <= 8` — `OshashError`; при `size < chunk`
`chunk = (size // 8) * 8`. Прочитать ровно `chunk` байт с начала и с
`size - chunk`; сумму считать как
`sum(int.from_bytes(buf[i:i+8], "little") for i in range(0, len(buf), 8))`,
сложить с `size`, взять по модулю `2**64`, вернуть `f"{value:016x}"`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_oshash.py -v`
Expected: PASS

- [ ] **Step 5: Checkpoint**

Run: `python -m pytest -q`
Expected: PASS, старые тесты не сломаны.

---

### Task 2: Модели StashDB и разбор ответа

**Files:**
- Create: `stashdb_client.py`
- Test: `tests/test_stashdb_client.py`

**Interfaces:**
- Consumes: ничего.
- Produces:
  - `@dataclass(frozen=True) class Studio`: `id`, `name`, `parent_id=None`,
    `parent_name=None`.
  - `@dataclass(frozen=True) class Performer`: `id`, `name`, `alias=None`;
    свойство `display_name -> str` возвращает `alias or name`.
  - `@dataclass(frozen=True) class Scene`: `id`, `title`, `release_date`,
    `date`, `code`, `duration`, `studio`, `performers` (tuple);
    `to_dict() -> dict`, `classmethod from_dict(dict) -> Scene`.
  - `scene_from_node(node: dict) -> Scene`
  - `studio_from_node(node: dict) -> Studio`

- [ ] **Step 1: Write the failing test**

```python
from stashdb_client import Performer, Scene, scene_from_node

def test_scene_from_node_reads_all_fields():
    scene = scene_from_node({
        "id": "s1", "title": "Poking Glory", "release_date": "2026-10-03",
        "date": "2026-10-03", "code": "GP1", "duration": 1200,
        "studio": {"id": "st1", "name": "BrazzersExxtra",
                    "parent": {"id": "st0", "name": "Brazzers"}},
        "performers": [{"as": "Ryan Reid",
                        "performer": {"id": "p1", "name": "Ryan Reid"}}],
    })
    assert scene.studio.name == "BrazzersExxtra"
    assert scene.studio.parent_name == "Brazzers"
    assert scene.performers[0].display_name == "Ryan Reid"
    assert Scene.from_dict(scene.to_dict()) == scene

def test_scene_from_node_allows_null_studio_and_alias():
    scene = scene_from_node({
        "id": "s2", "title": None, "release_date": None, "date": None,
        "code": None, "duration": None, "studio": None,
        "performers": [{"as": "Alias", "performer": {"id": "p2", "name": "Real"}}],
    })
    assert scene.studio is None
    assert scene.performers[0].display_name == "Alias"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_stashdb_client.py -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Implement dataclasses and parsers**

`scene_from_node` читает `release_date`, `date`, `code`, `duration`;
`studio` может быть `None`; `performers` — список `{"as", "performer"}`.
`to_dict`/`from_dict` должны быть симметричны для SQLite-кэша.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_stashdb_client.py -v`
Expected: PASS

- [ ] **Step 5: Checkpoint**

Run: `python -m pytest -q`
Expected: PASS.

---

### Task 3: GraphQL-клиент

**Files:**
- Modify: `stashdb_client.py`
- Test: `tests/test_stashdb_client.py`

**Interfaces:**
- Consumes: модели из Task 2.
- Produces:
  - `class StashDBError(RuntimeError)`, `StashDBAuthError`,
    `StashDBQueryError`, `StashDBTransientError`,
    `StashDBRateLimitError(StashDBTransientError)`, `StashDBResponseError`.
  - `class StashDBClient`:
    - `__init__(api_key: str, *, endpoint: str = STASHDB_ENDPOINT, delay: float = 1.0, timeout: float = 30.0, max_attempts: int = 3, sleep: Callable[[float], None] = time.sleep, transport: Callable[[urllib.request.Request, float], bytes] | None = None)`
    - `find_scenes_by_hashes(hashes: Sequence[str]) -> list[list[Scene]]`
    - `search_scenes(term: str, limit: int = 10) -> list[Scene]`
    - `query_studios(page: int, per_page: int = 100) -> tuple[list[Studio], int]`
  - Константы запросов `SCENES_BY_FINGERPRINTS_QUERY`, `SEARCH_SCENES_QUERY`,
    `STUDIOS_QUERY`.

- [ ] **Step 1: Write the failing test**

```python
import io, json, urllib.error
import pytest
from stashdb_client import (StashDBAuthError, StashDBClient,
                            StashDBQueryError, StashDBTransientError)

def transport_returning(payload):
    def _transport(req, timeout):
        return json.dumps(payload).encode()
    return _transport

def test_batches_40_and_keeps_order():
    calls = []
    def _transport(req, timeout):
        body = json.loads(req.data)
        calls.append(body)
        groups = body["variables"]["fingerprints"]
        data = []
        for group in groups:
            h = group[0]["hash"]
            data.append([{"id": f"s-{h}", "title": h, "release_date": None,
                          "date": None, "code": None, "duration": None,
                          "studio": None, "performers": []}])
        return json.dumps({"data": {"findScenesBySceneFingerprints": data}}).encode()
    client = StashDBClient("k", delay=0, transport=_transport)
    hashes = [f"h{i:02d}" for i in range(41)]
    result = client.find_scenes_by_hashes(hashes)
    assert len(result) == 41
    assert result[0][0].id == "s-h00"
    assert len(calls) == 2
    assert len(calls[0]["variables"]["fingerprints"]) == 40

def test_auth_error_is_fatal():
    def _transport(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b""))
    client = StashDBClient("bad", delay=0, transport=_transport)
    with pytest.raises(StashDBAuthError):
        client.search_scenes("test")

def test_graphql_errors_become_query_error():
    transport = transport_returning({"errors": [{"message": "bad query"}]})
    client = StashDBClient("k", delay=0, transport=transport)
    with pytest.raises(StashDBQueryError):
        client.search_scenes("test")

def test_429_retries_then_rate_limit():
    attempts = []
    def _transport(req, timeout):
        attempts.append(1)
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many",
                                     {"Retry-After": "0"}, io.BytesIO(b""))
    client = StashDBClient("k", delay=0, max_attempts=2,
                           sleep=lambda s: None, transport=_transport)
    with pytest.raises(StashDBTransientError):
        client.search_scenes("test")
    assert len(attempts) == 2

def test_query_studios_parses_count():
    transport = transport_returning({"data": {"queryStudios": {
        "count": 2, "studios": [{"id": "1", "name": "A"}]}}})
    client = StashDBClient("k", delay=0, transport=transport)
    studios, count = client.query_studios(1, 100)
    assert count == 2 and studios[0].name == "A"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_stashdb_client.py -v`
Expected: FAIL with `ImportError: StashDBClient`

- [ ] **Step 3: Implement `_post` and methods**

`_post(query, variables)`:
- `urllib.request.Request(endpoint, data=json.dumps({"query": query, "variables": variables}).encode(), headers={"ApiKey": api_key, "Content-Type": "application/json", "User-Agent": "filename-cleaner/1.0", "Accept": "application/json"}, method="POST")`.
- `transport(req, timeout)` или `urllib.request.urlopen(req, timeout=timeout).read()`.
- 401/403 → `StashDBAuthError`; 429 → `Retry-After` (int, cap 300), ретрай;
  5xx/`URLError`/`TimeoutError` → бэкофф 1/3/9; после попыток →
  `StashDBTransientError`.
- JSON-ошибка → `StashDBResponseError`.
- `errors`: если сообщение содержит `not authorized` → `StashDBAuthError`,
  иначе `StashDBQueryError`.
- Пауза `delay` перед запросом; `delay=0` в тестах.

`find_scenes_by_hashes`: дедуплицировать хэши, батчи по 40, вернуть список
списков в порядке исходных хэшей. `search_scenes` — `searchScene`.
`query_studios` — `queryStudios`, вернуть `(studios, count)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_stashdb_client.py -v`
Expected: PASS

- [ ] **Step 5: Checkpoint**

Run: `python -m pytest -q`
Expected: PASS.

---

### Task 4: SQLite-кэш

**Files:**
- Create: `title_cache.py`
- Test: `tests/test_title_cache.py`

**Interfaces:**
- Consumes: `Scene` из Task 2.
- Produces:
  - `class TitleCache`:
    - `__init__(path: str | os.PathLike[str], *, clock: Callable[[], datetime] = utcnow)`
    - `get_fingerprint(oshash: str) -> list[Scene] | None`
    - `put_fingerprint(oshash: str, scenes: Sequence[Scene]) -> None`
    - `get_text_query(term: str) -> list[Scene] | None`
    - `put_text_query(term: str, scenes: Sequence[Scene]) -> None`
    - `close() -> None`
  - TTL 30 дней; `None` — нет записи/истекла, `[]` — отрицательный результат.

- [ ] **Step 1: Write the failing test**

```python
from datetime import datetime, timedelta, timezone
from stashdb_client import Scene
from title_cache import TitleCache

def make_scene(i="s1"):
    return Scene(id=i, title="T", release_date=None, date=None, code=None,
                 duration=None, studio=None, performers=())

def test_roundtrip_and_negative(tmp_path):
    cache = TitleCache(tmp_path / "c.sqlite3")
    assert cache.get_fingerprint("h") is None
    cache.put_fingerprint("h", [make_scene()])
    assert cache.get_fingerprint("h")[0].id == "s1"
    cache.put_fingerprint("missing", [])
    assert cache.get_fingerprint("missing") == []
    cache.close()

def test_expired_returns_none(tmp_path):
    now = [datetime(2026, 11, 4, tzinfo=timezone.utc)]
    cache = TitleCache(tmp_path / "c.sqlite3", clock=lambda: now[0])
    cache.put_text_query("q", [make_scene()])
    now[0] += timedelta(days=31)
    assert cache.get_text_query("q") is None
    cache.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_title_cache.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement `TitleCache`**

Создать родительскую папку. `sqlite3.connect(str(path))`. Таблицы:
`fingerprints(hash TEXT PRIMARY KEY, scenes TEXT NOT NULL, fetched_at TEXT NOT NULL)`,
`text_queries(query TEXT PRIMARY KEY, scenes TEXT NOT NULL, fetched_at TEXT NOT NULL)`.
`fetched_at` — ISO UTC. `get_*` ловит `json.JSONDecodeError` и возвращает
`None`. `put_*` сериализует `[s.to_dict() for s in scenes]`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_title_cache.py -v`
Expected: PASS

- [ ] **Step 5: Checkpoint**

Run: `python -m pytest -q`
Expected: PASS.

---

### Task 5: Имена, даты и fallback

**Files:**
- Modify: `clean_ph_names.py`
- Create: `title_matcher.py`
- Test: `tests/test_title_matcher.py`, `tests/test_clean_ph_names.py`

**Interfaces:**
- Consumes: `Scene`, `Studio`, `Performer` из Task 2; старые функции
  `clean_ph_names`.
- Produces:
  - `normalize_studio_key(name: str) -> str` в `clean_ph_names.py`.
  - `prepare_stem(stem: str, studios: dict[str, str]) -> tuple[str, str | None, str | None]`
    — `(остаток, дата, студия)`.
  - `@dataclass(frozen=True) class LocalNameInfo`: `stem`, `date`, `studio`,
    `rest`.
  - `parse_local_name(filename: str, studios: dict[str, str]) -> LocalNameInfo`
  - `format_scene_date(value: str | None) -> str | None`
  - `sanitize_windows_stem(name: str) -> str`
  - `is_valid_final_stem(stem: str) -> bool`
  - `build_scene_stem(scene: Scene, local: LocalNameInfo, studios: dict[str, str]) -> str`
  - `build_heuristic_stem(local: LocalNameInfo, studios: dict[str, str]) -> str`

- [ ] **Step 1: Write the failing test**

```python
from clean_ph_names import extract_studio, normalize_studio_key
from stashdb_client import Performer, Scene, Studio
from title_matcher import (build_scene_stem, format_scene_date, LocalNameInfo,
                           sanitize_windows_stem, is_valid_final_stem)

def test_dates_and_windows_sanitization():
    assert format_scene_date("2026-10-03") == "03.10.2026"
    assert format_scene_date("2026-10") == "10.2026"
    assert format_scene_date("2026") == "2026"
    assert sanitize_windows_stem('A/B: C?') == "A B C"
    assert sanitize_windows_stem("CON") == "_CON"
    assert sanitize_windows_stem("Olivia del Río") == "Olivia del Río"
    assert is_valid_final_stem("Normal")
    assert not is_valid_final_stem("CON")
    assert not is_valid_final_stem("x" * 256)

def test_scene_stem_uses_official_fields():
    scene = Scene(id="s1", title="Poking Glory", release_date="2026-10-03",
                  date=None, code="GP1", duration=1,
                  studio=Studio("st", "BrazzersExxtra"),
                  performers=(Performer("p", "Ryan Reid"),))
    local = LocalNameInfo("old", "03.10.2026", "BrazzersExxtra", "old title")
    assert build_scene_stem(scene, local, {}) == \
        "BrazzersExxtra - 03.10.2026 - Ryan Reid - Poking Glory"

def test_multitoken_studio_prefix():
    studios = {"hussiepass": "HussiePass", "brazzersexxtra": "BrazzersExxtra"}
    studio, rest = extract_studio("Hussie Pass Anastasia Shades", studios)
    assert studio == "HussiePass" and rest == "Anastasia Shades"
    studio, rest = extract_studio("Brazzers Exxtra Ryan Reid", studios)
    assert studio == "BrazzersExxtra" and rest == "Ryan Reid"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_title_matcher.py tests/test_clean_ph_names.py -v`
Expected: FAIL with missing imports/functions.

- [ ] **Step 3: Implement names and fallback**

- `normalize_studio_key` — `re.sub(r"[\s._-]+", "", name.lower())`.
- `extract_studio`: пробовать префиксы из 1–5 токенов от длинного к короткому;
  ключ короче 4 символов игнорировать.
- `prepare_stem`: вынести из `clean_stem` шаги `extract_date`,
  `strip_tech_junk`, снос `[...]`/`EpornER`, `extract_studio`, `split_camel`.
  `clean_stem` переписать через `prepare_stem`, чтобы не дублировать логику.
- `parse_local_name`: stem без расширения, `prepare_stem`, вернуть `LocalNameInfo`.
- `format_scene_date`: regex `YYYY-MM-DD`, `YYYY-MM`, `YYYY`; иначе `None`.
- `sanitize_windows_stem`: `[<>:"/\\|?*\x00-\x1f]` → пробел, схлопнуть пробелы,
  `strip(" .")`, зарезервированные имена → `_` + имя.
- `is_valid_final_stem`: непустой, без запрещённых символов, не reserved,
  без ведущих/хвостовых пробелов и точек, длина ≤ 255.
- `build_scene_stem`: студия сцены → локальная; дата `release_date` → `date` →
  локальная; исполнители `display_name`; title или `local.rest`; собрать
  `build_new_name`, затем `sanitize_windows_stem`.
- `build_heuristic_stem`: `sanitize_windows_stem(clean_stem(local.stem, studios))`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_title_matcher.py tests/test_clean_ph_names.py -v`
Expected: PASS

- [ ] **Step 5: Checkpoint**

Run: `python -m pytest -q`
Expected: PASS, все старые тесты `clean_ph_names` зелёные.

---

### Task 6: Скоринг и выбор сцены

**Files:**
- Modify: `title_matcher.py`
- Test: `tests/test_title_matcher.py`

**Interfaces:**
- Consumes: `LocalNameInfo`, `build_scene_stem`, `build_heuristic_stem` из Task 5.
- Produces:
  - `normalize_for_match(text: str) -> str`
  - `@dataclass(frozen=True) class ScoredScene`: `scene`, `score`,
    `title_sim`, `performer_hit`, `studio_hit`, `date_hit`.
  - `score_scene(scene: Scene, local: LocalNameInfo, studios: dict[str, str]) -> ScoredScene`
  - `pick_text_match(scored: Sequence[ScoredScene]) -> tuple[ScoredScene | None, bool]`
    — `(лучший или None, ambiguous)`.
  - `@dataclass(frozen=True) class MatchResult`: `source`, `confidence`,
    `scene`, `candidates`, `new_stem`, `note`, `approved`.
  - `resolve_match(local: LocalNameInfo, studios: dict[str, str], fingerprint_scenes: Sequence[Scene], text_scenes: Sequence[Scene]) -> MatchResult`

- [ ] **Step 1: Write the failing test**

```python
from stashdb_client import Performer, Scene, Studio
from title_matcher import (LocalNameInfo, normalize_for_match, resolve_match)

def scene(i="s1", title="Poking Glory", performers=("Ryan Reid",),
          studio="BrazzersExxtra", date="2026-10-03"):
    return Scene(i, title, date, None, None, 1,
                 Studio("st", studio) if studio else None,
                 tuple(Performer(f"p{idx}", name)
                       for idx, name in enumerate(performers)))

def local(rest="Ryan Reid Poking Glory",
          stem="BrazzersExxtra 03.10.2026 Ryan Reid Poking Glory"):
    return LocalNameInfo(stem, "03.10.2026", "BrazzersExxtra", rest)

def test_normalize_strips_punctuation_and_diacritics():
    assert normalize_for_match("Café!!  Poking-Glory") == "cafe poking glory"

def test_unique_fingerprint_is_approved():
    result = resolve_match(local(), {}, [scene()], [])
    assert result.source == "fingerprint"
    assert result.approved is True
    assert result.new_stem.endswith("Poking Glory")

def test_low_text_score_falls_back_to_heuristic():
    result = resolve_match(LocalNameInfo("x", None, None, "totally different"),
                           {}, [], [scene(title="Another Movie")])
    assert result.source == "heuristic"
    assert result.approved is False

def test_text_match_is_not_auto_approved():
    result = resolve_match(local("Poking Glory"), {}, [],
                           [scene(), scene("s2", "Other")])
    assert result.source == "text"
    assert result.approved is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_title_matcher.py -v -k "normalize or resolve"`
Expected: FAIL with missing names.

- [ ] **Step 3: Implement scoring and `resolve_match`**

- `normalize_for_match`: NFKD, убрать combining, lower, non-alnum → пробел.
- `score_scene`: `title_sim = max(SequenceMatcher.ratio(), jaccard)`;
  `performer_hit` — доля имён, найденных в нормализованном stem;
  `studio_hit` — 1/0; `date_hit` — 1 при полной дате, 0.5 при годе.
  Веса `0.55/0.25/0.10/0.10`, недоступные сигналы выбросить и перенормировать.
- `pick_text_match`: порог 0.85, guard `performer_hit >= 0.5` или
  `title_sim >= 0.90`; ambiguous, если разрыв с вторым < 0.05.
- `resolve_match`:
  - fingerprint 1 сцена → `source=fingerprint`, `confidence=1.0`,
    `approved = is_valid_final_stem(stem) and (scene.title or два других блока)`;
    если `stem` невалиден или имени нет — подставить
    `build_heuristic_stem(...) or local.stem`, `approved=False`, в `note`
    добавить «имя сцены неполное, heuristic fallback»;
  - fingerprint 2+ → `source=fingerprint`, `confidence=0.99`, лучший по
    `score_scene`, `candidates=все`, `approved=False`, в `note` ID; если
    `stem` лучшего невалиден — тоже heuristic fallback;
  - text → `source=text`, `confidence=score`, `approved=False`, top-3 в
    `candidates`, `note` с сигналами; при ambiguous `approved=False` и
    `note=ambiguous`;
  - иначе → `source=heuristic`, `new_stem=build_heuristic_stem`, `approved=False`;
    в `candidates` положить top-3 текстовых кандидатов (даже с score < 0.85),
    в `note` — их score и причину отказа.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_title_matcher.py -v`
Expected: PASS

- [ ] **Step 5: Checkpoint**

Run: `python -m pytest -q`
Expected: PASS.

---

### Task 7: Сервис preview

**Files:**
- Create: `title_service.py`
- Test: `tests/test_title_service.py`

**Interfaces:**
- Consumes: `StashDBClient`, `TitleCache`, `resolve_match`, `parse_local_name`,
  `oshash_from_path`.
- Produces:
  - `@dataclass(frozen=True) class PreviewRow`: `approved`, `old_rel`, `new_rel`,
    `source`, `confidence`, `scene_id`, `note`, `oshash`, `size`.
  - `@dataclass(frozen=True) class PreviewReport`: `rows`, `warnings`,
    `had_transient_errors`.
  - `@dataclass(frozen=True) class ProgressEvent`: `phase`, `current`, `total`,
    `message=""`.
  - `class TitleService`:
    - `__init__(root: str, studios: dict[str, str], cache: TitleCache | None, client: StashDBClient | None = None, *, recursive=False, text_search=True, offline=False, refresh=False, limit: int | None = None, oshash_func=oshash_from_path, progress: Callable[[ProgressEvent], None] | None = None)`
    - `preview() -> PreviewReport`

- [ ] **Step 1: Write the failing test**

```python
from pathlib import Path
from title_service import TitleService

class FakeCache:
    def get_fingerprint(self, h): return None
    def put_fingerprint(self, h, scenes): pass
    def get_text_query(self, q): return None
    def put_text_query(self, q, scenes): pass

class FakeClient:
    def find_scenes_by_hashes(self, hashes):
        return [[SCENE] for _ in hashes]
    def search_scenes(self, term, limit=10):
        return []

def test_preview_marks_unique_fingerprint_approved(tmp_path):
    (tmp_path / "brazzersexxtra 26.10.03 ryan reid poking glory.mp4").write_bytes(b"x" * 100)
    service = TitleService(str(tmp_path), {}, FakeCache(), FakeClient(),
                           oshash_func=lambda p: "abc123")
    report = service.preview()
    assert len(report.rows) == 1
    row = report.rows[0]
    assert row.source == "fingerprint"
    assert row.approved is True
    assert row.new_rel.endswith("Poking Glory.mp4")
```

`SCENE` — `Scene(...)` с title/performers/studio/date, как в Task 6.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_title_service.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement preview**

- `iter_video_files(root, recursive)` — регулярные файлы с расширениями
  `.mp4 .mkv .avi .wmv .mov .m4v .webm .ts .mpg .mpeg`; пропускать
  `__ren_tmp_*`, symlink/junction, не выходить за root.
- Для каждого файла: `size`, `oshash`, `parse_local_name`.
- Кэш fingerprint: `get_fingerprint`; отсутствующие хэши — одним
  `find_scenes_by_hashes`; `put_fingerprint` для каждого (включая `[]`).
- `StashDBTransientError` → warning, `had_transient_errors=True`, считать
  результат `[]`. `StashDBAuthError`/`StashDBQueryError` пробросить.
- Если fingerprint пуст и `text_search`, `client is not None` и не `offline`:
  ключ `normalize_for_match(local.rest)`, кэш `get_text_query`, иначе
  `search_scenes`; `put_text_query`.
- `StashDBTransientError` в текстовом поиске — тот же warning,
  `had_transient_errors=True`, `text_scenes=[]`; в `note` строки добавить
  `stashdb_error`.
- `resolve_match`; `scene_id = match.scene.id if match.scene else ""`.
- Итоговый stem обрезать через `_cap_length(match.new_stem, suffix)`; если
  итоговое имя совпало с исходным — `source=unchanged`, `approved=False`.
- `approved = match.approved and is_valid_final_stem(final_stem)`.
- Коллизии: цель внутри партии или существующий файл, который сам
  переименовывается, не считается занятой; иначе ` (2)`, `(3)`.
- `--limit` обрезает список файлов до обработки.
- `progress` вызывать на фазах `scan`, `hash`, `fingerprint`, `text`, `done`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_title_service.py -v`
Expected: PASS

- [ ] **Step 5: Checkpoint**

Run: `python -m pytest -q`
Expected: PASS.

---

### Task 8: Сервис apply

**Files:**
- Modify: `title_service.py`
- Test: `tests/test_title_service.py`

**Interfaces:**
- Consumes: `PreviewRow`, `TitleService` из Task 7; `is_valid_final_stem`,
  `oshash_from_path`.
- Produces:
  - `@dataclass(frozen=True) class ApplyResult`: `old_rel`, `new_rel`,
    `status` (`renamed|skipped|error`), `message`.
  - `@dataclass(frozen=True) class ApplyReport`: `results`, `had_errors`.
  - `TitleService.apply_rows(rows: Sequence[PreviewRow], *, dry_run=True, force=False, suffix_collisions=False) -> ApplyReport`

- [ ] **Step 1: Write the failing test**

```python
from title_service import PreviewRow, TitleService

def row(old, new, approved=True, oshash="h", size=3):
    return PreviewRow(approved, old, new, "fingerprint", 1.0, "s", "",
                      oshash, size)

def test_apply_renames_approved_and_skips_rest(tmp_path):
    (tmp_path / "a.mp4").write_bytes(b"abc")
    service = TitleService(str(tmp_path), {}, None, None,
                           oshash_func=lambda p: "h")
    report = service.apply_rows(
        [row("a.mp4", "b.mp4"), row("a.mp4", "c.mp4", approved=False)],
        dry_run=False)
    assert (tmp_path / "b.mp4").exists()
    assert report.results[1].status == "skipped"

def test_apply_rejects_path_traversal(tmp_path):
    (tmp_path / "a.mp4").write_bytes(b"abc")
    service = TitleService(str(tmp_path), {}, None, None,
                           oshash_func=lambda p: "h")
    report = service.apply_rows([row("a.mp4", "..\\evil.mp4")], dry_run=False)
    assert report.had_errors is True
    assert not (tmp_path.parent / "evil.mp4").exists()

def test_apply_handles_rename_chain_and_upper_extension(tmp_path):
    (tmp_path / "a.MP4").write_bytes(b"abc")
    (tmp_path / "b.MP4").write_bytes(b"abc")
    service = TitleService(str(tmp_path), {}, None, None,
                           oshash_func=lambda p: "h")
    report = service.apply_rows(
        [row("a.MP4", "b.MP4"), row("b.MP4", "c.MP4")], dry_run=False)
    assert report.had_errors is False
    assert (tmp_path / "b.MP4").exists() and (tmp_path / "c.MP4").exists()

def test_apply_rejects_reserved_name(tmp_path):
    (tmp_path / "a.mp4").write_bytes(b"abc")
    service = TitleService(str(tmp_path), {}, None, None,
                           oshash_func=lambda p: "h")
    report = service.apply_rows([row("a.mp4", "CON.mp4")], dry_run=False)
    assert report.had_errors is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_title_service.py -v -k "apply"`
Expected: FAIL with `AttributeError: apply_rows`

- [ ] **Step 3: Implement apply**

Сначала отбросить строки с `approved=False` (статус `skipped`), не проверяя их
файлы. Затем проверки: `old`/`new` относительные; `new` без подкаталогов; путь
после `abspath` внутри root; `old` — обычный файл, не symlink/junction; расширение
совпадает; `is_valid_final_stem(new_stem)`; полная длина ≤ 255; `size` и
`oshash` совпадают (если не `force`); цель не занята (или
`suffix_collisions`).
`dry_run` проходит все проверки, но не вызывает `os.rename`.
Двухфазно: все источники → `__ren_tmp_<uuid>` в той же папке, затем temp →
цель. Ошибка одной строки не мешает остальным; `had_errors=True`.
Цель, которая сама входит в партию, занятой не считается.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_title_service.py -v`
Expected: PASS

- [ ] **Step 5: Checkpoint**

Run: `python -m pytest -q`
Expected: PASS.

---

### Task 9: CLI `fix_video_titles.py`

**Files:**
- Create: `fix_video_titles.py`
- Test: `tests/test_fix_video_titles.py`

**Interfaces:**
- Consumes: `TitleService`, `PreviewRow`, `PreviewReport`, `TitleCache`,
  `StashDBClient`, `load_studios`.
- Produces:
  - `main(argv: Sequence[str] | None = None) -> int`
  - `write_preview_csv(path: str, rows: Sequence[PreviewRow]) -> None`
  - `read_preview_csv(path: str) -> list[PreviewRow]`
  - `append_rename_log(path: str, results: Sequence[ApplyResult], timestamp: str) -> None`

- [ ] **Step 1: Write the failing test**

```python
from fix_video_titles import main, read_preview_csv, write_preview_csv
from title_service import PreviewRow

def test_csv_roundtrip_and_formula_guard(tmp_path):
    p = tmp_path / "preview.csv"
    row = PreviewRow(True, "old.mp4", "=New.mp4", "fingerprint", 1.0,
                     "s1", "note", "h", 3)
    write_preview_csv(str(p), [row])
    raw = p.read_text(encoding="utf-8-sig")
    assert "'=New.mp4" in raw
    assert read_preview_csv(str(p))[0].new_rel == "=New.mp4"

def test_preview_cli_writes_csv_and_does_not_rename(tmp_path, monkeypatch):
    (tmp_path / "old.mp4").write_bytes(b"abcdefghij")
    monkeypatch.setenv("STASHDB_API_KEY", "")
    code = main(["preview", str(tmp_path), "--offline",
                 "--csv", str(tmp_path / "p.csv"),
                 "--cache", str(tmp_path / "c.sqlite3")])
    assert code == 0
    assert (tmp_path / "old.mp4").exists()
    assert (tmp_path / "p.csv").exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_fix_video_titles.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement CLI**

`argparse` с subcommands `preview` и `apply`.
`preview`: `directory`, `--csv preview.csv`, `--recursive`, `--limit`,
`--no-text`, `--offline`, `--refresh`, `--studios studios.json`,
`--cache cache/title_fixer.sqlite3`.
`apply`: `csv`, `--root` (required), `--apply`, `--log rename_log.csv`,
`--force`, `--suffix-collisions`.
Ключ из `os.environ["STASHDB_API_KEY"]`; пустой ключ → warning и `offline`.
`StashDBAuthError` → stderr «проверь STASHDB_API_KEY», код 2, CSV не писать.
`StashDBQueryError` → stderr, код 2.
`had_transient_errors` → код 1. `apply` пишет лог только для `renamed` и только
с `--apply`. `--refresh` + `--offline` → `parser.error`.
Формула-guard для `old` и `new`: апостроф перед `= + - @`; при чтении снимать
один апостроф.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_fix_video_titles.py -v`
Expected: PASS

- [ ] **Step 5: Checkpoint**

Run: `python -m pytest -q`
Expected: PASS.

---

### Task 10: Скрапер студий

**Files:**
- Create: `scrape_studios.py`
- Test: `tests/test_scrape_studios.py`

**Interfaces:**
- Consumes: `StashDBClient`, `Studio`, `normalize_studio_key`.
- Produces:
  - `class ScraperGapError(RuntimeError)`
  - `normalize_key(name: str) -> str`
  - `merge_studios(existing: dict[str, str], names: Sequence[str]) -> tuple[dict[str, str], dict[str, list[str]]]`
  - `@dataclass(frozen=True) class Checkpoint`: `page`, `per_page`, `count`,
    `complete`.
  - `load_checkpoint(path: str) -> Checkpoint | None`
  - `save_checkpoint(path: str, checkpoint: Checkpoint) -> None` (атомарно)
  - `load_raw(path: str) -> list[Studio]`
  - `save_raw(path: str, studios: Sequence[Studio]) -> None` (атомарно)
  - `@dataclass(frozen=True) class ScrapeReport`: `studios`, `next_page`,
    `complete`, `pages_fetched`.
  - `run_scrape(client: StashDBClient, *, start_page: int, per_page: int,
    max_pages: int | None, initial: Sequence[Studio] = (),
    fetch=fetch_studios_page, on_page: Callable | None = None) -> ScrapeReport`
  - `main(argv: Sequence[str] | None = None) -> int`

- [ ] **Step 1: Write the failing test**

```python
import pytest
from scrape_studios import (Checkpoint, ScraperGapError, load_checkpoint,
                            main, merge_studios, normalize_key, run_scrape,
                            save_checkpoint)
from stashdb_client import Studio

def test_key_matches_cleaner():
    assert normalize_key("BrazzersExxtra") == "brazzersexxtra"
    assert normalize_key("Hussie Pass") == "hussiepass"

def test_merge_keeps_existing_and_reports_collisions():
    merged, collisions = merge_studios(
        {"brazzers": "Brazzers"}, ["Brazzers", "Brazzers!"])
    assert merged["brazzers"] == "Brazzers"
    assert collisions["brazzers"] == ["Brazzers!"]

def test_paginates_to_count():
    pages = {
        1: ([Studio("1", "A"), Studio("2", "B")], 3),
        2: ([Studio("3", "C")], 3),
    }
    def fetch(client, page, per_page):
        return pages[page]
    report = run_scrape(None, start_page=1, per_page=2, max_pages=None,
                        fetch=fetch)
    assert [s.name for s in report.studios] == ["A", "B", "C"]
    assert report.complete is True

def test_empty_page_inside_count_raises():
    def fetch(client, page, per_page):
        return [], 10
    with pytest.raises(ScraperGapError):
        run_scrape(None, start_page=1, per_page=2, max_pages=None, fetch=fetch)

def test_main_without_key_returns_2(tmp_path, monkeypatch):
    monkeypatch.delenv("STASHDB_API_KEY", raising=False)
    assert main(["--out", str(tmp_path / "studios.json")]) == 2

def test_checkpoint_roundtrip(tmp_path):
    p = str(tmp_path / "cp.json")
    save_checkpoint(p, Checkpoint(4, 100, 500, False))
    assert load_checkpoint(p) == Checkpoint(4, 100, 500, False)
    assert load_checkpoint(str(tmp_path / "no.json")) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_scrape_studios.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement scraper**

- `fetch_studios_page(client, page, per_page)` → `client.query_studios`.
- `run_scrape` дедуплицирует по `Studio.id`, вызывает `on_page(page, page_studios, count)`
  после каждой страницы, останавливается при `len(studios) >= count`, пустой
  странице внутри `count` — `ScraperGapError`, `max_pages` — остановка с
  `complete=False`.
- `main`: `--out studios.json`, `--checkpoint studios_checkpoint.json`,
  `--raw studios_raw.json`, `--per-page 100`, `--delay 1.0`, `--max-pages N`,
  `--apply`, `--reset`. Без `--apply` ничего не пишет. При `--apply` после
  каждой страницы сохранять raw и checkpoint; в конце — backup
  `studios.json.bak`, merge, `studios_collisions.json`, атомарная запись.
  `--reset` с `--apply` удаляет checkpoint/raw и начинает с 1.
  Если `checkpoint.per_page` не совпал с `--per-page`, завершиться ошибкой
  с просьбой `--reset`, а не продолжать с другим размером страницы.
  Ключ из env; auth → код 2, transient → код 1.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_scrape_studios.py -v`
Expected: PASS

- [ ] **Step 5: Checkpoint**

Run: `python -m pytest -q`
Expected: PASS.

---

### Task 11: Документация и живая проверка

**Files:**
- Create: `README.md`
- Create: `.gitignore`
- Test: полный прогон + offline smoke на реальной папке.

**Interfaces:**
- Consumes: всё выше.
- Produces: инструкция запуска и безопасный smoke-прогон.

- [ ] **Step 1: Написать README**

Разделы: что это, требования (Python 3.14, ключ StashDB), быстрый старт,
`preview` → ревью CSV → `apply`, колонки CSV, offline/refresh, лимиты StashDB,
скрапер студий, troubleshooting (401, нет совпадений, Excel-разделитель),
будущий TUI. Без секретов.

- [ ] **Step 2: Добавить `.gitignore`**

`__pycache__/`, `.pytest_cache/`, `.venv/`, `cache/`, `preview.csv`,
`rename_log.csv`, `studios_raw.json`, `studios_checkpoint.json`,
`studios_collisions.json`, `studios.json.bak`, `.env`.

- [ ] **Step 3: Полный прогон тестов**

Run: `python -m pytest -q`
Expected: PASS.

- [ ] **Step 4: Offline smoke на реальной папке**

Run: `python fix_video_titles.py preview D:\Videos\PH --limit 2 --offline --csv preview_smoke.csv`
Expected: команда завершается 0, `preview_smoke.csv` создан, видео не
переименованы. Проверить, что в CSV 2 строки и `source=heuristic`. Удалить
`preview_smoke.csv` после проверки.

- [ ] **Step 5: Checkpoint**

Записать в план фактические результаты smoke и полного pytest. Живой прогон с
реальным ключом и `--limit 5` выполняет человек; ожидаемый результат — CSV с
`source=fingerprint` или `text` для найденных сцен.

---

## Self-Review

- **Spec coverage:** OSHASH, GraphQL client, кэш, имена/даты/W‌indows, скоринг,
  preview, apply, CLI, студии, документация, TUI-ready service layer — покрыты
  Task 1–11. TUI, PHASH, MD5 — явно вне плана.
- **Step scan:** каждый шаг содержит либо тест с точными значениями, либо
  сигнатуру/правило, либо команду проверки; без «TBD» и «handle edge cases».
- **Type consistency:** `Scene`/`Studio`/`Performer`, `LocalNameInfo`,
  `MatchResult`, `PreviewRow`, `ApplyResult`, `TitleCache`, `TitleService`
  используются с одинаковыми именами полей во всех задачах.
- **Review Focus:** пять пунктов покрыты тестами Task 1 (OSHASH), Task 8/9
  (CSV/apply), Task 3/7 (ошибки StashDB), Task 5/8 (Windows), Task 4/10
  (кэш/возобновление).
- **Proportion:** план длиннее спеки, но содержит в основном сигнатуры и
  короткие тесты, а не тела функций.

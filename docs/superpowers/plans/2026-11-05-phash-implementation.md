# PHASH и поиск по нескольким источникам: план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить поиск по фингерпринтам PHASH и поиск по OSHASH во всех трёх сервисах (StashDB, ThePornDB, FansDB) с одобрением строк только для OSHASH-совпадений, как сейчас.

**Architecture:** Новый модуль `video_renamer/core/phash.py` считает PHASH тем же алгоритмом, что Stash (ffmpeg + stdlib). `TitleService` опрашивает все источники по очереди: кэш по `(source, algorithm, hash)`, затем сеть. Сцены объединяются и дедуплицируются. Текстовый поиск остаётся в StashDB. Ядро остаётся на stdlib.

**Tech Stack:** Python 3.14 stdlib (`sqlite3`, `subprocess`, `struct`, `urllib`), внешние `ffmpeg`/`ffprobe`, `pytest`.

**Spec:** `docs/superpowers/specs/2026-11-05-phash-design.md`

## Global Constraints

- Без обязательных pip-зависимостей: только stdlib. Ядро не импортирует Textual.
- Windows 10/11, Python 3.14. Пути к ffmpeg/ffprobe передаются списком аргументов, без `shell=True`.
- PHASH: `approved=n` всегда. `source=phash`.
- OSHASH: `approved=y` только для одной сцены после дедупликации с валидным именем (как сейчас).
- Фингерпринты: эндпоинты `https://stashdb.org/graphql`, `https://theporndb.net/graphql`, `https://fansdb.cc/graphql`. Ключи: `STASHDB_API_KEY`, `THE_PORNDB_API_KEY`, `FANSDB_API_KEY`. Заголовок `ApiKey`.
- Батч фингерпринтов не более 40. TTL кэша 30 дней, отрицательный результат — `[]`.
- Временная ошибка StashDB — код возврата 1, как сейчас. Неверный ключ StashDB — фатально для preview, как сейчас.
- `apply` и формат `preview.csv` не меняются, кроме значения `source=phash`.
- Ключи и значения `.env` не печатаются и не пишутся в CSV, журналы и `note`.
- Файлы в `D:\Videos\PH` только читаются. Тесты не переименовывают реальные файлы.

## Review Focus

1. Одна и та же сцена пришла из двух сервисов с одинаковыми название, датой и студией: должна схлопнуться в одну и дать одобрение OSHASH. Тест: `test_merge_scenes_collapses_duplicates_across_sources` (Task 2) и `test_fingerprint_single_merged_scene_is_approved` (Task 7).
2. У сцены из ThePornDB или FansDB пустые `title`, `studio`, `release_date`, `performers`: не должно быть падения, итоговое имя не одобряется без содержательных блоков. Тест: `test_merge_scenes_handles_missing_fields` (Task 2) и `test_secondary_scene_without_title_is_not_approved` (Task 6).
3. Ключ ThePornDB или FansDB отсутствует или неверный: preview работает со StashDB, в `warnings` предупреждение, код возврата 0. Тест: `test_secondary_source_auth_error_is_warning` (Task 7) и `test_missing_secondary_key_is_skipped` (Task 8).
4. Старая база кэша с таблицей `fingerprints` после обновления: записи переносятся в `fingerprint_cache`, отрицательный кэш `[]` сохраняется, повторный запуск миграции ничего не ломает. Тест: `test_legacy_fingerprints_migrated_once` (Task 5).
5. `--phash` задан, а ffmpeg или ffprobe не найдены: ошибка до обхода файлов, файлы не трогаются. Тест: `test_phash_missing_tool_fails_fast` (Task 8).
6. Повреждённый файл, который ffmpeg не открывает: строка без PHASH, preview продолжается, `note` содержит `phash error`. Тест: `test_phash_error_per_file_does_not_stop_preview` (Task 7).

---

## File Structure

- Create: `video_renamer/core/phash.py` — PHASH: кадры, BMP, сетка, ресемплинг, DCT, медиана, `phash_from_path`.
- Create: `video_renamer/core/fingerprint_sources.py` — описание источников, ключи, `merge_scenes`, `build_fingerprint_clients`.
- Modify: `video_renamer/core/stashdb_client.py` — поле `Scene.source`, параметр алгоритма в `find_scenes_by_hashes`.
- Modify: `video_renamer/core/title_cache.py` — таблицы `fingerprint_cache`, `local_phash`, миграция.
- Modify: `video_renamer/core/title_matcher.py` — параметр `phash_scenes` в `resolve_match`.
- Modify: `video_renamer/core/title_service.py` — несколько источников, этап PHASH, ошибки источников.
- Modify: `video_renamer/cli.py` — флаги, сборка клиентов, ранняя проверка инструментов.
- Modify: `video_renamer/tui_model.py`, `video_renamer/tui.py` — источник `phash`, сборка клиентов.
- Modify: `docs/architecture.md`, `README.md`, `CHANGELOG.md` — значения `source`, флаги, запись в «Unreleased».
- Test: `tests/test_phash.py` (новый), `tests/test_fingerprint_sources.py` (новый), плюс дополнения в `tests/test_stashdb_client.py`, `tests/test_title_cache.py`, `tests/test_title_matcher.py`, `tests/test_title_service.py`, `tests/test_fix_video_titles.py`.

Задачи 1–3 не зависят друг от друга, кроме порядка `Scene`. Задачи 4 и 5 независимы. Задача 6 нужна задачам 7–9.

---

### Task 1: Поле `Scene.source` и описание источников

**Files:**
- Modify: `video_renamer/core/stashdb_client.py` (dataclass `Scene`, `to_dict`, `from_dict`)
- Create: `video_renamer/core/fingerprint_sources.py`
- Test: `tests/test_stashdb_client.py`, `tests/test_fingerprint_sources.py`

**Interfaces:**
- Consumes: —
- Produces:
  - `Scene.source: str = "StashDB"` (последнее поле dataclass, со значением по умолчанию). `to_dict` и `from_dict` включают `source`; `from_dict` без ключа `source` даёт `"StashDB"`.
  - `@dataclass(frozen=True) class FingerprintSource` с полями `name: str`, `endpoint: str`, `api_key_env: str`.
  - `FINGERPRINT_SOURCES: tuple[FingerprintSource, ...]` — порядок StashDB, ThePornDB, FansDB.
  - `PRIMARY_SOURCE_NAME = "StashDB"`.

- [ ] **Step 1: Тест поля и сериализации**

```python
def test_scene_defaults_to_stashdb_source():
    scene = Scene(id="s1", title=None, release_date=None, date=None,
                  code=None, duration=None)
    assert scene.source == "StashDB"


def test_scene_source_round_trips_through_dict():
    scene = Scene(id="s1", title="T", release_date=None, date=None,
                  code=None, duration=None, source="ThePornDB")
    assert Scene.from_dict(scene.to_dict()).source == "ThePornDB"


def test_scene_from_dict_without_source_defaults_to_stashdb():
    data = {"id": "s1", "title": "T"}
    assert Scene.from_dict(data).source == "StashDB"
```

- [ ] **Step 2: Тест констант источников**

```python
def test_fingerprint_sources_order_and_endpoints():
    names = [s.name for s in FINGERPRINT_SOURCES]
    assert names == ["StashDB", "ThePornDB", "FansDB"]
    endpoints = {s.name: s.endpoint for s in FINGERPRINT_SOURCES}
    assert endpoints["ThePornDB"] == "https://theporndb.net/graphql"
    assert endpoints["FansDB"] == "https://fansdb.cc/graphql"
```

Дополнительно: тест, что эндпоинты совпадают с `video_renamer/scrapers/stashdb.py` (`STASHDB_SOURCE`, `THEPORNDB_SOURCE`, `FANSDB_SOURCE`), чтобы константы не разъехались.

- [ ] **Step 3: Запустить тесты, убедиться, что падают**

Run: `python -m pytest tests/test_stashdb_client.py tests/test_fingerprint_sources.py -q`
Expected: FAIL (поля и модуля нет).

- [ ] **Step 4: Реализация**

Добавить `source: str = "StashDB"` последним полем `Scene`. В `to_dict` добавить ключ `"source"`, в `from_dict` читать `data.get("source") or "StashDB"`. Создать `fingerprint_sources.py` с `FingerprintSource` и кортежем `FINGERPRINT_SOURCES`, значения эндпоинтов и ключей — как в Global Constraints.

- [ ] **Step 5: Запустить тесты, убедиться, что проходят**

Run: `python -m pytest tests/test_stashdb_client.py tests/test_fingerprint_sources.py -q`
Expected: PASS.

---

### Task 2: Дедупликация сцен между источниками

**Files:**
- Modify: `video_renamer/core/fingerprint_sources.py`
- Test: `tests/test_fingerprint_sources.py`

**Interfaces:**
- Consumes: `Scene` (Task 1).
- Produces: `merge_scenes(per_source: Sequence[Sequence[Scene]]) -> list[Scene]`.

Правило: ключ дедупликации — `(normalize_for_match(title), release_date или date, normalize_for_match(studio.name))`. Пустые поля участвуют в ключе как пустые строки. Порядок результата: сначала сцены первого источника в исходном порядке, затем новые из следующих. Из одинаковых ключей остаётся сцена с более ранним источником.

- [ ] **Step 1: Тесты**

```python
def test_merge_scenes_collapses_duplicates_across_sources():
    stash = [scene("a", "Same Title", "2026-01-02", "Studio X", source="StashDB")]
    porn = [scene("b", "Same  Title!", "2026-01-02", "studio x", source="ThePornDB")]
    merged = merge_scenes([stash, porn])
    assert [s.id for s in merged] == ["a"]


def test_merge_scenes_keeps_distinct_scenes():
    merged = merge_scenes([
        [scene("a", "One", "2026-01-02", "S", source="StashDB")],
        [scene("b", "Two", "2026-01-02", "S", source="ThePornDB")],
    ])
    assert [s.id for s in merged] == ["a", "b"]


def test_merge_scenes_handles_missing_fields():
    merged = merge_scenes([[scene("a", None, None, None, source="FansDB")]])
    assert [s.id for s in merged] == ["a"]
```

Вспомогательная `scene(...)` строит `Scene` с `studio=Studio(id="x", name=...)`, если имя студии задано.

- [ ] **Step 2: Запустить, убедиться в падении**

Run: `python -m pytest tests/test_fingerprint_sources.py -q`
Expected: FAIL.

- [ ] **Step 3: Реализация `merge_scenes`** в `fingerprint_sources.py`. Использовать `normalize_for_match` из `title_matcher.py`. Импорт из `title_matcher` в `fingerprint_sources` допустим; циклического импорта быть не должно (`title_matcher` не импортирует `fingerprint_sources`).

- [ ] **Step 4: Запустить, убедиться в прохождении**

Run: `python -m pytest tests/test_fingerprint_sources.py -q`
Expected: PASS.

---

### Task 3: Алгоритм фингерпринта в `StashDBClient`

**Files:**
- Modify: `video_renamer/core/stashdb_client.py` (`find_scenes_by_hashes`)
- Test: `tests/test_stashdb_client.py`

**Interfaces:**
- Consumes: `Scene.source` (Task 1).
- Produces: `StashDBClient.find_scenes_by_hashes(hashes: Sequence[str], algorithm: str = "OSHASH") -> list[list[Scene]]`. Для каждого источника вызывающий код сам проставляет `source` через `dataclasses.replace`.

- [ ] **Step 1: Тесты**

```python
def test_find_scenes_by_hashes_sends_phash_algorithm(recording_transport):
    client = StashDBClient("k", transport=recording_transport({...}), delay=0)
    client.find_scenes_by_hashes(["dc75c0f03795438e"], algorithm="PHASH")
    body = json.loads(recording_transport.last_request_body)
    assert body["variables"]["fingerprints"] == [
        [{"hash": "dc75c0f03795438e", "algorithm": "PHASH"}]
    ]


def test_find_scenes_by_hashes_defaults_to_oshash(recording_transport):
    ...  # как выше, без algorithm; ожидается "OSHASH"
```

Использовать существующий в `tests/test_stashdb_client.py` стиль транспорта (`transport=`), не добавлять новый.

- [ ] **Step 2: Запустить, убедиться в падении**

Run: `python -m pytest tests/test_stashdb_client.py -q -k "algorithm or defaults_to_oshash"`
Expected: FAIL (`algorithm` — неизвестный аргумент).

- [ ] **Step 3: Реализация.** Добавить параметр `algorithm: str = "OSHASH"`, в `variables` подставлять его вместо литерала. Остальная логика батчей не меняется.

- [ ] **Step 4: Запустить весь файл**

Run: `python -m pytest tests/test_stashdb_client.py -q`
Expected: PASS, включая прежние тесты батчей.

---

### Task 4: Ядро PHASH

**Files:**
- Create: `video_renamer/core/phash.py`
- Test: `tests/test_phash.py`

**Interfaces:**
- Consumes: —
- Produces:
  - `class PhashError(ValueError)`, `class PhashToolError(RuntimeError)`.
  - `frame_times(duration: float) -> list[float]` — 25 значений `0.05*D + i*(0.9*D/25)`.
  - `probe_duration(path: str, ffprobe: str, run=subprocess.run) -> float`.
  - `extract_frame(path: str, t: float, *, accurate: bool, ffmpeg: str, run=subprocess.run) -> bytes` — аргументы: `-ss t` перед `-i` (если не `accurate`), `-i path`, `-ss t` после `-i` (если `accurate`), `-frames:v 1`, `-vf scale=160:-2`, `-c:v bmp`, `-f rawvideo`, `-`. Путь в аргументах — абсолютный.
  - `decode_bmp(data: bytes) -> tuple[int, int, list[list[float]]]` — ширина, высота, строки серых значений `0.299R+0.587G+0.114B`, сверху вниз.
  - `build_montage(frames: Sequence[tuple[int, int, list[list[float]]]]) -> list[list[float]]`.
  - `resize_bilinear(gray: list[list[float]], out_w: int, out_h: int) -> list[list[float]]` — треугольный фильтр, масштаб `in/out`.
  - `dct_top_left_8x8(pixels64: list[list[float]]) -> list[float]` — 64 значения, `flat[8*j+i]`.
  - `quick_select_median(seq: list[float], low: int, hi: int, k: int) -> float` — порт `quickSelectMedian` из goimagehash; вызывается как `quick_select_median(list(flat), 0, 63, 32)`.
  - `phash_from_path(path: str, *, ffmpeg: str, ffprobe: str, run=subprocess.run) -> int` — 64-битное значение.
  - `format_phash(value: int) -> str` — 16 hex-символов в нижнем регистре.

- [ ] **Step 1: Тесты чистых функций**

```python
def test_frame_times_match_stash_formula():
    times = frame_times(100.0)
    assert len(times) == 25
    assert times[0] == pytest.approx(5.0)
    assert times[-1] == pytest.approx(5.0 + 24 * 3.6)


def test_decode_bmp_reads_24bit_bottom_up():
    data = make_bmp_24(width=2, height=2, pixels_top_down=[...])  # helper в тесте
    w, h, gray = decode_bmp(data)
    assert (w, h) == (2, 2)
    assert gray[0][0] == pytest.approx(0.299 * 255)


def test_build_montage_places_cells_row_major():
    frames = [(1, 1, [[float(i)]]) for i in range(25)]
    canvas = build_montage(frames)
    assert canvas[0][4] == 4.0
    assert canvas[1][0] == 5.0


def test_quick_select_median_matches_sorted_average():
    values = [float(v) for v in random.Random(1).sample(range(1000), 64)]
    expected = (sorted(values)[31] + sorted(values)[32]) / 2
    assert quick_select_median(list(values), 0, 63, 32) == pytest.approx(expected)


def test_dct_top_left_matches_naive_dct():
    pixels = [[float((r * 7 + c * 3) % 11) for c in range(64)] for r in range(64)]
    flat = dct_top_left_8x8(pixels)
    # наивная DCT-II без нормировки по формуле cos(pi/64*(n+0.5)*k)
    assert flat[0] == pytest.approx(sum(sum(row) for row in pixels))


def test_format_phash_is_16_lowercase_hex():
    assert format_phash(0xDC75C0F03795438E) == "dc75c0f03795438e"
```

- [ ] **Step 2: Тесты внешних вызовов с фейковым `run`**

```python
def test_extract_frame_uses_fast_seek_args():
    calls = []
    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=b"BM...", stderr=b"")
    extract_frame("C:\\v.mp4", 12.5, accurate=False, ffmpeg="ffmpeg", run=fake_run)
    args = calls[0]
    assert args.index("-ss") < args.index("-i")
    assert "scale=160:-2" in args
    assert "-shell" not in args and isinstance(args, list)


def test_phash_from_path_fails_on_missing_duration():
    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout=b"N/A\n", stderr=b"")
    with pytest.raises(PhashError):
        phash_from_path("C:\\v.mp4", ffmpeg="ffmpeg", ffprobe="ffprobe", run=fake_run)
```

- [ ] **Step 3: Запустить, убедиться в падении**

Run: `python -m pytest tests/test_phash.py -q`
Expected: FAIL (модуля нет).

- [ ] **Step 4: Реализация** в `phash.py` по сигнатурам выше. Алгоритм: `probe_duration` → 25 кадров (быстрый seek; при ошибке процесса переключиться на `accurate=True` для оставшихся кадров файла, как в Stash) → `decode_bmp` → `build_montage` → `resize_bilinear(…, 64, 64)` → `dct_top_left_8x8` → `quick_select_median` → бит `63-idx` при `p > median`. Реализацию ресемплинга и DCT взять из эскиза пробы `C:\Users\isoko\AppData\Local\Temp\opencode\phash_spike.py` (функции `resize_bilinear`, `dct_top_left_8x8`, `quick_select_median`, `decode_bmp`, `build_montage`) — это проверено на 16 файлах, с расстоянием 0–2 бита до StashDB. Скопировать логику, не импортировать файл пробы.

- [ ] **Step 5: Интеграционный тест на реальном файле (локальный)**

```python
REFERENCE_FILE = r"D:\Videos\PH\brazzersexxtra.26.10.03.ryan.reid.poking.glory.mp4"
REFERENCE_PHASH = 0xDC75C0F03795438E

@pytest.mark.skipif(not os.path.exists(REFERENCE_FILE), reason="local sample file")
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_phash_matches_stashdb_reference_within_threshold():
    value = phash_from_path(REFERENCE_FILE, ffmpeg="ffmpeg", ffprobe="ffprobe")
    assert bin(value ^ REFERENCE_PHASH).count("1") <= 4
```

Примечание: `shutil.which("ffmpeg")` не находит бинарник из WinGet Links, если нет в PATH. В тесте использовать путь по переменной окружения `FFMPEG_PATH` и явный запасной путь `%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe`.

- [ ] **Step 6: Запустить тесты**

Run: `python -m pytest tests/test_phash.py -q`
Expected: PASS (интеграционный тест пропускается, если файла нет).

---

### Task 5: Кэш по источникам и миграция

**Files:**
- Modify: `video_renamer/core/title_cache.py`
- Test: `tests/test_title_cache.py`

**Interfaces:**
- Consumes: `Scene.source` (Task 1).
- Produces:
  - Таблица `fingerprint_cache(source TEXT, algorithm TEXT, hash TEXT, scenes TEXT NOT NULL, fetched_at TEXT NOT NULL, PRIMARY KEY (source, algorithm, hash))`.
  - Таблица `local_phash(oshash TEXT, size INTEGER, phash TEXT NOT NULL, computed_at TEXT NOT NULL, PRIMARY KEY (oshash, size))`.
  - `TitleCache.get_source_fingerprint(source: str, algorithm: str, digest: str) -> list[Scene] | None`.
  - `TitleCache.put_source_fingerprint(source: str, algorithm: str, digest: str, scenes: Sequence[Scene]) -> None`.
  - `TitleCache.get_local_phash(oshash: str, size: int) -> str | None`, `put_local_phash(oshash: str, size: int, phash: str) -> None`.
  - Прежние `get_fingerprint(oshash)` и `put_fingerprint(oshash, scenes)` делегируют в `('StashDB', 'OSHASH', oshash)`.
  - Миграция выполняется в `_connect`: `INSERT OR IGNORE INTO fingerprint_cache (source, algorithm, hash, scenes, fetched_at) SELECT 'StashDB', 'OSHASH', hash, scenes, fetched_at FROM fingerprints`.

- [ ] **Step 1: Тесты**

```python
def test_source_fingerprint_is_keyed_by_source_and_algorithm(tmp_path):
    cache = TitleCache(tmp_path / "c.sqlite3")
    cache.put_source_fingerprint("ThePornDB", "OSHASH", "abc", [scene("x")])
    assert cache.get_source_fingerprint("StashDB", "OSHASH", "abc") is None
    assert cache.get_source_fingerprint("ThePornDB", "PHASH", "abc") is None
    assert cache.get_source_fingerprint("ThePornDB", "OSHASH", "abc")[0].id == "x"


def test_negative_result_is_kept(tmp_path):
    cache = TitleCache(tmp_path / "c.sqlite3")
    cache.put_source_fingerprint("FansDB", "PHASH", "h", [])
    assert cache.get_source_fingerprint("FansDB", "PHASH", "h") == []


def test_legacy_fingerprints_migrated_once(tmp_path):
    path = tmp_path / "old.sqlite3"
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE fingerprints (hash TEXT PRIMARY KEY, scenes TEXT NOT NULL, fetched_at TEXT NOT NULL)")
    raw.execute("INSERT INTO fingerprints VALUES ('h1', '[]', ?)", (utc_now_iso(),))
    raw.commit(); raw.close()
    TitleCache(path).close()
    cache = TitleCache(path)
    assert cache.get_source_fingerprint("StashDB", "OSHASH", "h1") == []
    cache.close()
    TitleCache(path).close()  # второй запуск не падает и не дублирует


def test_local_phash_keyed_by_content_not_path(tmp_path):
    cache = TitleCache(tmp_path / "c.sqlite3")
    cache.put_local_phash("oh", 100, "dc75c0f03795438e")
    assert cache.get_local_phash("oh", 100) == "dc75c0f03795438e"
    assert cache.get_local_phash("oh", 101) is None
```

Тесты используют существующий `clock`-параметр `TitleCache` для TTL, как прежние тесты.

- [ ] **Step 2: Запустить, убедиться в падении**

Run: `python -m pytest tests/test_title_cache.py -q`
Expected: FAIL.

- [ ] **Step 3: Реализация.** Добавить новые таблицы в `_SCHEMA`, миграцию после создания таблиц в `_connect`, методы. TTL и `_scenes_from_row` переиспользовать. `local_phash` без TTL.

- [ ] **Step 4: Запустить**

Run: `python -m pytest tests/test_title_cache.py -q`
Expected: PASS, прежние тесты кэша тоже.

---

### Task 6: `resolve_match` с PHASH-кандидатами

**Files:**
- Modify: `video_renamer/core/title_matcher.py` (`resolve_match`, около строки 438)
- Test: `tests/test_title_matcher.py`

**Interfaces:**
- Consumes: `Scene.source` (Task 1).
- Produces: `resolve_match(local, studios, fingerprint_scenes, text_scenes, phash_scenes=()) -> MatchResult`.

Правила:
- `fingerprint_scenes` уже объединён и дедуплицирован (Task 2). Ветки «одна сцена» и «несколько сцен» не меняются.
- Если `fingerprint_scenes` пуст и `phash_scenes` непуст: `MatchResult(source="phash", confidence=0.9, approved=False)`. Одна сцена: `scene` = она, `new_stem` через `build_scene_stem` (если валидно, иначе `_heuristic_stem`). Несколько: `scene` = первая по порядку, `candidates` = все, `note` перечисляет `"<source> <id>"`.
- `note` всегда начинается с `"phash"`.
- Текст пропускается при любом непустом `phash_scenes` (уже ветка возвращает раньше).

- [ ] **Step 1: Тесты**

```python
def test_phash_single_scene_is_never_approved(local, studios):
    result = resolve_match(local, studios, [], [], phash_scenes=[full_scene()])
    assert result.source == "phash"
    assert result.approved is False
    assert result.note.startswith("phash")


def test_phash_multiple_scenes_list_candidates(local, studios):
    result = resolve_match(local, studios, [], [], phash_scenes=[full_scene("a"), full_scene("b")])
    assert [s.id for s in result.candidates] == ["a", "b"]
    assert result.approved is False


def test_phash_skips_text_search(local, studios):
    result = resolve_match(local, studios, [], [full_scene("t")], phash_scenes=[full_scene("p")])
    assert result.source == "phash"


def test_secondary_scene_without_title_is_not_approved(local, studios):
    # одна fingerprint-сцена без названия и с одним блоком: approved=False
    result = resolve_match(local, studios, [sparse_scene(source="ThePornDB")], [])
    assert result.approved is False
```

`full_scene`, `sparse_scene`, `local`, `studios` — фикстуры, которые уже есть в `tests/test_title_matcher.py` (или строятся по образцу существующих). Не дублировать код.

- [ ] **Step 2: Запустить, убедиться в падении**

Run: `python -m pytest tests/test_title_matcher.py -q -k "phash or without_title"`
Expected: FAIL.

- [ ] **Step 3: Реализация** по правилам выше.

- [ ] **Step 4: Запустить весь файл**

Run: `python -m pytest tests/test_title_matcher.py -q`
Expected: PASS.

---

### Task 7: `TitleService` — несколько источников и этап PHASH

**Files:**
- Modify: `video_renamer/core/title_service.py`
- Test: `tests/test_title_service.py`

**Interfaces:**
- Consumes: `FINGERPRINT_SOURCES`, `merge_scenes` (Tasks 1–2); `find_scenes_by_hashes(..., algorithm)` (Task 3); `phash_from_path`, `format_phash`, `PhashError`, `PhashToolError` (Task 4); `get_source_fingerprint`, `put_source_fingerprint`, `get_local_phash`, `put_local_phash` (Task 5); `resolve_match(..., phash_scenes)` (Task 6).
- Produces:
  - `TitleService.__init__(..., extra_clients: Mapping[str, StashDBClient] | None = None, phash: bool = False, phash_func: Callable[[str], str] | None = None, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe", ...)`. `extra_clients` — ключи `"ThePornDB"`, `"FansDB"`. `client` остаётся клиентом StashDB (он же для текста).
  - `_Candidate` получает поле `phash_scenes: list[Scene]`, `phash_error: str` (пусто или текст ошибки файла).
  - `PreviewRow.note` для сбоев: `"<Name>_error"` для вторичных источников, `stashdb_error` для StashDB (как сейчас).

Правила:
- `_resolve_fingerprints` перебирает источники по порядку `FINGERPRINT_SOURCES`, у которых есть клиент (StashDB — `self._client`, остальные — `extra_clients`). Для каждого: сначала `get_source_fingerprint`, потом `find_scenes_by_hashes(missing, algorithm="OSHASH")`, сцены помечаются `source` через `dataclasses.replace`, результат кладётся в кэш (кроме временных ошибок).
- Ошибки: `StashDBTransientError` у StashDB — предупреждение и `stashdb_error` (как сейчас). `StashDBAuthError` у StashDB — пробрасывается наружу (фатально). Любые `StashDBError` у вторичного источника — предупреждение `"<Name>: ..."` и пометка файлов `<Name>_error`; `StashDBAuthError` у вторичного источника тоже только предупреждение.
- После опроса всех источников: `candidate.fingerprint_scenes = merge_scenes(per_source_lists)` для каждого кандидата.
- Этап PHASH (`_resolve_phash`) включается при `phash=True`. Только кандидаты с `oshash` и пустым `fingerprint_scenes` и без `hash_error`. Для каждого файла: `get_local_phash(oshash, size)`, иначе `phash_func(path)` (ошибка `PhashError` → `candidate.phash_error`, строка идёт дальше), затем запись в `put_local_phash`. Затем для каждого источника: кэш `("<Name>", "PHASH", hex)`, иначе батч `find_scenes_by_hashes(hexes, algorithm="PHASH")` с той же обработкой ошибок. `candidate.phash_scenes = merge_scenes(...)`.
- `_plan` передаёт `candidate.phash_scenes` в `resolve_match`. Текст (`_resolve_text`) пропускает кандидатов, у которых есть `fingerprint_scenes` или `phash_scenes`.
- `offline`: сеть запрещена; кэш всех источников читается. `refresh`: кэш StashDB и вторичных источников игнорируется, `local_phash` — нет.
- `PhashToolError` в `phash_func` пробрасывается наружу (это не ошибка файла), CLI ловит её до обхода (Task 8).

Тесты (в `tests/test_title_service.py`, с фейковыми клиентами по образцу существующих `FakeClient`):

- [ ] **Step 1: Тесты**

```python
def test_fingerprint_single_merged_scene_is_approved(tmp_path):
    # StashDB и ThePornDB возвращают одну и ту же сцену (та же дата/название/студия)
    report = run_preview(tmp_path, stash_hits={"h": [scene("a", source="StashDB")]},
                         porn_hits={"h": [scene("b", source="ThePornDB")]})
    assert report.rows[0].approved is True
    assert report.rows[0].source == "fingerprint"


def test_secondary_source_auth_error_is_warning(tmp_path):
    report = run_preview(tmp_path, stash_hits={"h": [scene("a")]},
                         porn_auth_error=True)
    assert report.rows[0].scene_id == "a"
    assert any("ThePornDB" in w for w in report.warnings)
    assert report.had_transient_errors is False


def test_secondary_transient_error_marks_row(tmp_path):
    report = run_preview(tmp_path, stash_hits={"h": []}, fans_transient_error=True)
    assert "FansDB_error" in report.rows[0].note
    assert report.had_transient_errors is True


def test_primary_auth_error_is_fatal(tmp_path):
    with pytest.raises(StashDBAuthError):
        run_preview(tmp_path, stash_auth_error=True)


def test_phash_runs_only_without_oshash_match(tmp_path):
    hasher = CountingPhash({"x.mp4": "dc75c0f03795438e"})
    run_preview(tmp_path, stash_hits={"h": [scene("a")]}, phash=True, phash_func=hasher)
    assert hasher.calls == []


def test_phash_match_is_never_approved(tmp_path):
    report = run_preview(tmp_path, stash_hits={"h": []},
                         phash=True, phash_func=lambda p: "dc75c0f03795438e",
                         phash_hits={"dc75c0f03795438e": [scene("p")]})
    assert report.rows[0].source == "phash"
    assert report.rows[0].approved is False


def test_phash_error_per_file_does_not_stop_preview(tmp_path):
    def broken(path):
        raise PhashError("ffmpeg returned no frame")
    report = run_preview(tmp_path, stash_hits={"h": []}, phash=True, phash_func=broken)
    assert report.rows and "phash error" in report.rows[0].note


def test_phash_local_cache_avoids_recomputation(tmp_path):
    hasher = CountingPhash({"x.mp4": "dc75c0f03795438e"})
    run_preview(tmp_path, stash_hits={"h": []}, phash=True, phash_func=hasher)
    run_preview(tmp_path, stash_hits={"h": []}, phash=True, phash_func=hasher)
    assert len(hasher.calls) == 1


def test_phash_tool_error_propagates(tmp_path):
    def missing(path):
        raise PhashToolError("ffmpeg not found")
    with pytest.raises(PhashToolError):
        run_preview(tmp_path, stash_hits={"h": []}, phash=True, phash_func=missing)
```

`run_preview` — вспомогательная функция в тесте: создаёт файл в `tmp_path`, `TitleCache` с временным путём, фейковые клиенты (`FakeClient` с `fingerprints` по хэшу для каждого `algorithm`), вызывает `TitleService(...).preview()`. Ошибки клиента задаются через `StashDBAuthError`/`StashDBTransientError`. Фейки определяются внутри тестового файла, без сети.

- [ ] **Step 2: Запустить, убедиться в падении**

Run: `python -m pytest tests/test_title_service.py -q -k "merged or secondary or phash or primary_auth"`
Expected: FAIL.

- [ ] **Step 3: Реализация** в `title_service.py`: новые параметры в `__init__`, новые поля `_Candidate`, `_resolve_fingerprints` по источникам, `_resolve_phash`, правка `_plan` и `_resolve_text`. Сохранить `oshash_func`, `build_alternative`, `apply_rows` без изменений.

- [ ] **Step 4: Запустить весь файл**

Run: `python -m pytest tests/test_title_service.py -q`
Expected: PASS, включая прежние тесты.

---

### Task 8: CLI — флаги, сборка клиентов, ранняя проверка инструментов

**Files:**
- Modify: `video_renamer/core/fingerprint_sources.py` (функция `build_fingerprint_clients`)
- Modify: `video_renamer/cli.py` (`_run_preview`, парсер `preview`, около строк 230–332)
- Test: `tests/test_fingerprint_sources.py`, `tests/test_fix_video_titles.py`

**Interfaces:**
- Consumes: `FINGERPRINT_SOURCES` (Task 1); `TitleService(..., extra_clients, phash, phash_func, ffmpeg, ffprobe)` (Task 7); `PhashToolError`, `phash_from_path`, `format_phash` (Task 4).
- Produces:
  - `build_fingerprint_clients(env: Mapping[str, str], *, offline: bool) -> dict[str, StashDBClient]` — для вторичных источников создаёт клиент только при непустом ключе и `offline=False`. Ключ StashDB не возвращает (он в `client`).
  - `check_phash_tools(ffmpeg: str, ffprobe: str) -> None` — бросает `PhashToolError`, если бинарник не запускается (`--version`).
  - Флаги `preview`: `--phash` (по умолчанию выключен), `--ffmpeg PATH`, `--ffprobe PATH`, `--no-secondary` (не опрашивать ThePornDB и FansDB).

Правила:
- `_run_preview` вызывает `load_project_env()` (уже есть) и строит клиенты. Если `--no-secondary` — `extra_clients={}`.
- `--phash` вместе с `--offline` разрешён: PHASH считается локально, сеть не используется, кэш фингерпринтов читается. Ошибки аргументов нет.
- Если `--phash` задан: `check_phash_tools` до обхода файлов; при ошибке — `error: ...` на stderr, код возврата 2.
- Пути `--ffmpeg`/`--ffprobe` по умолчанию берутся из `FFMPEG_PATH`/`FFPROBE_PATH`, иначе `ffmpeg`/`ffprobe`.
- `PhashToolError`, пойманный во время `preview()`, — код возврата 2.

Тесты:

- [ ] **Step 1: Тесты**

```python
def test_missing_secondary_key_is_skipped():
    clients = build_fingerprint_clients({"THE_PORNDB_API_KEY": ""}, offline=False)
    assert "ThePornDB" not in clients


def test_secondary_clients_built_when_keys_present():
    clients = build_fingerprint_clients(
        {"THE_PORNDB_API_KEY": "t", "FANSDB_API_KEY": "f"}, offline=False)
    assert set(clients) == {"ThePornDB", "FansDB"}


def test_offline_builds_no_clients():
    assert build_fingerprint_clients({"FANSDB_API_KEY": "f"}, offline=True) == {}


def test_phash_missing_tool_fails_fast(tmp_path, capsys):
    rc = main(["preview", str(tmp_path), "--phash", "--ffmpeg", r"C:\nope\ffmpeg.exe"])
    assert rc == 2
    assert "ffmpeg" in capsys.readouterr().err
    assert not (tmp_path / "preview.csv").exists()
```

- [ ] **Step 2: Запустить, убедиться в падении**

Run: `python -m pytest tests/test_fingerprint_sources.py tests/test_fix_video_titles.py -q -k "secondary or offline or phash_missing"`
Expected: FAIL.

- [ ] **Step 3: Реализация** по правилам выше. Существующие флаги `preview` не меняются.

- [ ] **Step 4: Запустить тесты CLI**

Run: `python -m pytest tests/test_fingerprint_sources.py tests/test_fix_video_titles.py -q`
Expected: PASS.

---

### Task 9: TUI — источник `phash` и сборка клиентов

**Files:**
- Modify: `video_renamer/tui_model.py` (около строк 12–14, 99–101)
- Modify: `video_renamer/tui.py` (около строк 59–74, 596, 744, 826, 1005)
- Test: `tests/test_tui.py` (если Textual установлен; иначе пропускается)

**Interfaces:**
- Consumes: `build_fingerprint_clients` (Task 8); `TitleService` (Task 7).
- Produces: в модели и вьюхе — источник `phash` с меткой `PHASH`, цветом и подписью фазы `phash` в `PHASE_LABELS` (`Считаю PHASH`).

Правила:
- В `tui_model.py` добавить `("phash", "PHASH")` в список источников и `"phash": 0` в счётчики. Порядок меток: после `fingerprint`.
- В `tui.py` добавить метку, цвет (`bold #c4b5fd`), подпись `PHASE_LABELS["phash"]`.
- В `tui.py` при создании `TitleService` (строка 826) передавать `extra_clients=build_fingerprint_clients(os.environ, offline=offline)`. Флаг `phash` в TUI по умолчанию выключен и не выводится в UI (отдельная задача). Строки 744 и 1005 (`TitleService(..., None, None)`) не меняются.

- [ ] **Step 1: Тест модели**

```python
def test_phash_counter_is_present():
    model = ...  # как в существующих тестах tui_model
    assert "phash" in model.source_counts
```

Если в `tests/test_tui.py` нет подходящих тестов модели, добавить один, без запуска приложения.

- [ ] **Step 2: Запустить, убедиться в падении**

Run: `python -m pytest tests/test_tui.py -q -k phash`
Expected: FAIL или SKIP (если Textual не установлен). Если SKIP — отметить в отчёте.

- [ ] **Step 3: Реализация** по правилам выше.

- [ ] **Step 4: Запустить тесты TUI**

Run: `python -m pytest tests/test_tui.py -q`
Expected: PASS или SKIP.

---

### Task 10: Документация и полный прогон

**Files:**
- Modify: `docs/architecture.md` (раздел «CSV-ревью», значения `source`)
- Modify: `README.md` (флаги `preview`, раздел про источники)
- Modify: `CHANGELOG.md` (раздел «Unreleased» → «Добавлено»)

**Interfaces:**
- Consumes: итоговые флаги (Task 8).
- Produces: —

- [ ] **Step 1: Обновить документацию.** В `architecture.md`: `source` может быть `phash`; `note` содержит `<Name>_error`. В `README.md`: флаги `--phash`, `--ffmpeg`, `--ffprobe`, `--no-secondary`, список источников и ключей. В `CHANGELOG.md`: пункт «PHASH-поиск по перцептивному хэшу и поиск по ThePornDB и FansDB наряду со StashDB; совпадения PHASH всегда на ревью».

- [ ] **Step 2: Полный прогон тестов**

Run: `python -m pytest -q`
Expected: все тесты проходят или пропускаются; ни одного FAIL. Число тестов и пропусков записать в отчёт.

- [ ] **Step 3: Проверка на реальной папке (только чтение)**

Run: `python -m video_renamer.cli preview "D:\Videos\PH" --limit 5 --csv preview_phash_check.csv`
Expected: CSV создан, файлы не переименованы. Затем вручную проверить строки с `source=fingerprint` и `source=phash`.

Run (с PHASH): `python -m video_renamer.cli preview "D:\Videos\PH" --limit 5 --phash --csv preview_phash_check.csv --ffmpeg "%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe" --ffprobe "%LOCALAPPDATA%\Microsoft\WinGet\Links\ffprobe.exe"`
Expected: строки с `source=phash` не одобрены (`approved=n`).

---

## Self-Review

**1. Покрытие спецификации:**
- Интент, цели, анти-цели — Tasks 4, 7, 8 (PHASH, approved=n, без ffmpeg-шелла).
- Источники фингерпринтов: эндпоинты и ключи — Task 1; дедупликация — Task 2; OSHASH-запросы по источникам — Tasks 3, 7; ошибки источников — Task 7; кэш и миграция — Task 5; offline/refresh — Task 7.
- Алгоритм PHASH — Task 4.
- Компоненты `title_matcher`, `title_service`, CLI, TUI — Tasks 6, 7, 8, 9.
- Формат CSV — Task 10 (документ), `apply` не меняется (Global Constraints).
- Тесты из спецификации — распределены по задачам 1–9.
- Проверка на реальной папке и замер 67 файлов — Task 10 (проверка на 5 файлах). Полный замер не входит в план; это отдельное решение пользователя.

**2. Соответствие шагов:** Каждый шаг с тестом содержит имя теста, ассерт или точный паттерн проверки. Реализация описана сигнатурами и правилами; тела функций не даны, кроме эталона из пробы, на который ссылается Task 4.

**3. Согласованность типов:** `Scene.source` (Task 1) используется в Tasks 2, 3, 5, 6, 7. `merge_scenes` (Task 2) вызывается в Task 7. `find_scenes_by_hashes(..., algorithm)` (Task 3) вызывается в Task 7 с `"OSHASH"` и `"PHASH"`. `get_source_fingerprint`/`put_source_fingerprint` (Task 5) вызываются в Task 7 с `(source, algorithm, digest)`. `resolve_match(..., phash_scenes)` (Task 6) вызывается в Task 7. `build_fingerprint_clients` (Task 8) вызывается в Task 9. Названия совпадают.

**4. Review Focus:** Все шесть строк имеют тесты в задачах 2, 5, 6, 7, 8. Строка 6 (повреждённый файл) покрыта `test_phash_error_per_file_does_not_stop_preview` в Task 7.

**Решения, которые приняты в плане (можно изменить при согласовании):**
- Дедупликация: ключ «название + дата + студия» по нормализованным значениям. Сцены одной записи в разных сервисах с другими названиями не схлопнутся — останутся кандидатами, одобрение не выдаётся (безопасная сторона).
- Ошибки вторичных источников (включая неверный ключ) не фатальны, фатален только StashDB, как сейчас.
- Текстовый поиск остаётся только в StashDB.
- `--no-secondary` — дополнительный флаг, чтобы отключить ThePornDB и FansDB без правки ключей.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-11-05-phash-implementation.md`. Please review the plan. Which execution approach would you prefer?

- **Subagent-driven** — отдельный субагент на каждую задачу, после каждой задачи проверка свежим ревьюером, в конце ревью всей ветки. Надёжнее, но дороже по контексту.
- **Native** — я выполняю все задачи сам в этой сессии, в конце один ревьюер проверяет результат. Быстрее и дешевле, но независимой проверки по ходу не будет.

Для этого плана я рекомендую **Native**, потому что задачи последовательны и опираются на интерфейсы друг друга (Scene, кэш, `TitleService`), а ошибка в объединении источников легко обнаруживается тестами. Plan captures what you want, and which approach should we use?

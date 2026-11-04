# Studios Scraper Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Разовый скрипт выгружает все студии из StashDB GraphQL в `studios.json` формата чистильщика.

**Architecture:** Один синхронный Python-скрипт `scrape_studios.py` на `httpx` рядом с `clean_ph_names.py`: листает `queryStudios` пагинацией, нормализует ключи тем же правилом, что чистильщик, пишет `studios.json` атомарно; прогресс в чекпоинте, повторный прогон продолжает с места обрыва.

**Tech Stack:** Python 3.10+ stdlib + `httpx` (sync `httpx.Client`, `httpx.MockTransport` в тестах), pytest, Windows.

**Spec:** `docs/superpowers/specs/2026-11-04-studios-scraper-design.md`

## Global Constraints

- Endpoint `https://stashdb.org/graphql`, auth-заголовок `ApiKey`, не Bearer.
- Ключ только из `STASHDB_API_KEY`; никакого хардкода и коммитов ключа.
- Только официальные полные имена, без алиасов и доменов.
- Нормализация ключей та же, что в `clean_ph_names.py`: lower + снос пробелов/точек/дефисов/подчеркиваний.
- Пауза между страницами; 429 уважать через `Retry-After`.
- Dry-run по умолчанию ничего не пишет; реальная запись только с `--apply`.
- Повторный прогон идемпотентен — тот же `studios.json`.

## Review Focus

- Невалидный/протухший API-ключ (401/403) — ожидается понятное сообщение «проверь STASHDB_API_KEY», а не трейсбек.
- Две студии нормализуются в один ключ — ожидается суффикс ` (2)` у ключа-дубля вместо тихой потери студии.
- Пустая страница внутри заявленного диапазона — ожидается остановка с ошибкой, а не «готово».
- Обрыв сети посреди прогона — ожидается продолжение со страницы чекпоинта, а не с начала.
- Упорный 429 после всех ретраев — ожидается внятная ошибка лимита, а не бесконечный цикл.

---

### Task 1: GraphQL-клиент студий

**Files:**
- Create: `scrape_studios.py`
- Test: `tests/test_scrape_studios.py`

**Interfaces:**
- Consumes: ничего (первая задача).
- Produces:
  - `fetch_studios_page(client: httpx.Client, api_key: str, page: int, per_page: int) -> tuple[list[dict], int]` — возвращает (студии `[{id, name}]`, всего `count`); 401/403 -> `ScraperAuthError`, упорный 429 -> `ScraperRateLimitError`, ошибки запроса -> `ScraperTransientError`.
  - `STUDIOS_QUERY: str` — GraphQL-запрос к `queryStudios` (поле и инпут уточнить живым запросом с ключом; фолбэк — интроспекция схемы).

- [ ] **Step 1: Write the failing test**

```python
def test_fetch_page_parses_studios_and_count():
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"data": {"queryStudios": {"count": 3, "studios": [{"id": "1", "name": "Brazzers"}, {"id": "2", "name": "Hussie Pass"}]}}}))
    client = httpx.Client(transport=transport)
    studios, count = fetch_studios_page(client, "key", 1, 50)
    assert count == 3
    assert [s["name"] for s in studios] == ["Brazzers", "Hussie Pass"]

def test_fetch_page_auth_error():
    transport = httpx.MockTransport(lambda r: httpx.Response(401, json={}))
    client = httpx.Client(transport=transport)
    with pytest.raises(ScraperAuthError):
        fetch_studios_page(client, "bad", 1, 50)

def test_fetch_page_retries_then_rate_limit():
    transport = httpx.MockTransport(lambda r: httpx.Response(429, headers={"Retry-After": "0"}))
    client = httpx.Client(transport=transport)
    with pytest.raises(ScraperRateLimitError):
        fetch_studios_page(client, "key", 1, 50, delay=0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_scrape_studios.py -v`
Expected: FAIL with "fetch_studios_page not defined" (и `pip install httpx`, если его нет: `pip install httpx -q`)

- [ ] **Step 3: Implement `fetch_studios_page(client, api_key, page, per_page, delay=1.0)` и исключения в `scrape_studios.py`**

Подход: POST с заголовками `ApiKey` + `Content-Type: application/json` + `User-Agent: filename-cleaner/1.0`; до 3 попыток, 429 ждет `Retry-After` (кап 300с), сетевые сбои — бэкофф 1/3/9с.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_scrape_studios.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scrape_studios.py tests/test_scrape_studios.py
git commit -m "feat: studios GraphQL client with retries and rate-limit handling"
```

### Task 2: Маппинг в формат studios.json

**Files:**
- Modify: `scrape_studios.py`
- Test: `tests/test_scrape_studios.py`

**Interfaces:**
- Consumes: `fetch_studios_page` из Task 1.
- Produces:
  - `normalize_key(name: str) -> str` — то же правило, что `_KEY_NORM` в `clean_ph_names.py`.
  - `merge_studios(existing: dict[str, str], names: list[str]) -> dict[str, str]` — добавляет новые, коллизии ключей -> `ключ (2)`; существующие записи не затирает.
  - `write_studios_atomic(path: str, data: dict[str, str]) -> None` — атомарная запись через tmp + `os.replace`.

- [ ] **Step 1: Write the failing test**

```python
def test_normalize_key_matches_cleaner():
    assert normalize_key("BrazzersExxtra") == "brazzersexxtra"
    assert normalize_key("Hussie Pass") == "hussiepass"
    assert normalize_key("Tonights-Girlfriend") == "tonightsgirlfriend"

def test_merge_keeps_existing_and_suffixes_collisions():
    merged = merge_studios({"brazzers": "Brazzers"}, ["Brazzers", "Brazzers!"])
    assert merged["brazzers"] == "Brazzers"
    assert merged["brazzers (2)"] == "Brazzers!"

def test_write_atomic_roundtrip(tmp_path):
    p = tmp_path / "studios.json"
    write_studios_atomic(str(p), {"a": "A"})
    assert json.loads(p.read_text(encoding="utf-8")) == {"a": "A"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_scrape_studios.py -v -k "normalize or merge or atomic"`
Expected: FAIL with "normalize_key not defined"

- [ ] **Step 3: Implement `normalize_key`, `merge_studios`, `write_studios_atomic` в `scrape_studios.py`**

Импорт `json`, `os`; нормализация — `re.sub(r"[\s._-]+", "", name.lower())`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_scrape_studios.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scrape_studios.py tests/test_scrape_studios.py
git commit -m "feat: studios mapping to cleaner dict format with atomic write"
```

### Task 3: CLI с чекпоинтом и dry-run

**Files:**
- Modify: `scrape_studios.py`
- Test: `tests/test_scrape_studios.py`

**Interfaces:**
- Consumes: `fetch_studios_page`, `merge_studios`, `write_studios_atomic` из Task 1-2.
- Produces:
  - `run_scrape(client, api_key, *, start_page: int, per_page: int, delay: float, max_pages: int | None, _fetch=fetch_studios_page) -> tuple[dict, int]` — возвращает (словарь, следующая_страница); пустая страница внутри диапазона -> `ScraperGapError`; `_fetch` — инжект для тестов с той же сигнатурой, что `fetch_studios_page`.
  - `load_checkpoint(path: str) -> int`, `save_checkpoint(path: str, page: int) -> None`.
  - CLI: `scrape_studios.py [--out studios.json] [--checkpoint FILE] [--per-page 50] [--delay 1.0] [--max-pages N] [--apply]`; без `--apply` — dry-run: печать первых/итоговых строк, файлы не трогаются; с `--apply` — бэкап `studios.json.bak` + атомарная запись.

- [ ] **Step 1: Write the failing test**

```python
def test_run_scrape_paginates_to_end():
    pages = {1: (["A", "B"], 3), 2: (["C"], 3)}
    def fake_fetch(client, key, page, per_page, delay=0):
        return ([{"id": str(i), "name": n} for i, n in enumerate(pages[page][0])], pages[page][1])
    data, nxt = run_scrape(None, "k", start_page=1, per_page=50, delay=0, max_pages=None, _fetch=fake_fetch)
    assert data == {"a": "A", "b": "B", "c": "C"}

def test_run_scrape_empty_page_inside_range_raises():
    def fake_fetch(client, key, page, per_page, delay=0):
        return ([], 10)
    with pytest.raises(ScraperGapError):
        run_scrape(None, "k", start_page=1, per_page=50, delay=0, max_pages=None, _fetch=fake_fetch)

def test_checkpoint_roundtrip(tmp_path):
    p = str(tmp_path / "cp.json")
    save_checkpoint(p, 4)
    assert load_checkpoint(p) == 4
    assert load_checkpoint(str(tmp_path / "nope.json")) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_scrape_studios.py -v -k "run_scrape or checkpoint"`
Expected: FAIL with "run_scrape not defined"

- [ ] **Step 3: Implement `run_scrape`, `load_checkpoint`, `save_checkpoint`, `main()` с argparse в `scrape_studios.py`**

Цикл страниц от `start_page`, пока накоплено < count; ошибка страницы — стоп без продвижения чекпоинта; в `main` чекпоинт обновляется после каждой страницы, dry-run не пишет ни чекпоинт, ни json.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_scrape_studios.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scrape_studios.py tests/test_scrape_studios.py
git commit -m "feat: studios CLI with checkpoint resume and dry-run"
```

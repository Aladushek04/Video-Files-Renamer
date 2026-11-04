#!/usr/bin/env python3
"""Чистит мусорные имена файлов в шаблон 'Студия - ДД.ММ.ГГГГ - Актеры - Название'."""
import json
import os
import re

_KEEP_PAREN = re.compile(r"\((remastered|part\s*\d+)\)", re.IGNORECASE)
_TECH_PAREN = re.compile(r"\(\s*\d{1,4}\s*\)")
_TECH_WORD = re.compile(r"(?i)\b(1080p|720p|2160p|4320p|4k|8k|uhd|hd|fullhd|full-hd|xxx)\b")
_SEP = re.compile(r"[._+]+")
_WS = re.compile(r"\s+")
_EMPTY_PAREN = re.compile(r"\(\s*\)")
_DATE_YMD4 = re.compile(r"\(?(\d{4})[.-](\d{2})[.-](\d{2})\)?")
_DATE_YMD2 = re.compile(r"\(?(\d{2})[.-](\d{2})[.-](\d{2})\)?")
_DATE_DMY4 = re.compile(r"\(?(\d{2})[.-](\d{2})[.-](\d{4})\)?")
_HASH_BRACKET = re.compile(r"\[[a-zA-Z0-9]{5,}\]")
_PARTICLES = {"del", "de", "la", "van", "von", "di", "da", "le", "dos", "das", "do"}
_ARTICLES = {"the", "a", "an", "and", "of", "for"}
_KEY_NORM = re.compile(r"[\s._-]+")


def split_name(filename: str) -> tuple:
    """Возвращает (stem, suffix); suffix включает точку, сохраняется как есть."""
    stem, suffix = os.path.splitext(filename)
    return stem, suffix


def strip_tech_junk(stem: str) -> str:
    """Режет техмусор, чистит разделители. Скобки (Remastered)/(Part N) сохраняет."""
    keepers = []

    def _hold(m):
        keepers.append(m.group(0))
        return f"\x00{len(keepers) - 1}\x00"

    s = _KEEP_PAREN.sub(_hold, stem)
    s = _TECH_PAREN.sub(" ", s)
    s = _TECH_WORD.sub(" ", s)
    s = _SEP.sub(" ", s)
    s = _EMPTY_PAREN.sub(" ", s)
    s = _WS.sub(" ", s).strip(" -")
    for i, k in enumerate(keepers):
        s = s.replace(f"\x00{i}\x00", k)
    s = _WS.sub(" ", s).strip(" -")
    return s


def normalize_studio_key(name: str) -> str:
    """Ключ студии: lower без пробелов, точек, подчёркиваний и дефисов."""
    return _KEY_NORM.sub("", name.lower())


def load_studios(path: str) -> dict:
    """Читает studios.json; ключи приводит к lower без разделителей."""
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    return {normalize_studio_key(str(k)): v for k, v in raw.items()}


def extract_date(stem: str) -> tuple:
    """Ищет дату, возвращает (stem_без_даты, ДД.ММ.ГГГГ или None)."""
    m = _DATE_YMD4.search(stem)
    if m:
        yyyy, mm, dd = m.group(1), m.group(2), m.group(3)
        out = f"{dd}.{mm}.{yyyy}"
        return _WS.sub(" ", stem[: m.start()] + " " + stem[m.end():]).strip(" -"), out
    m = _DATE_DMY4.search(stem)
    if m:
        # Отличаем ДД.ММ.ГГГГ от ГГГГ.ММ.ДД по длине первого блока: тут первый блок 2 цифры.
        # Конфликт с YY.MM.DD невозможен — там год 2 цифры в конце нет.
        dd, mm, yyyy = m.group(1), m.group(2), m.group(3)
        # Если первая группа >31 — это год первым (YYYY.MM.DD уже пойман выше),
        # но для безопасности: если dd > 31, считаем что это год.
        if int(dd) > 31:
            out = f"{yyyy}.{mm}.{dd}"  # невалидная дата, не трогаем как дату
            return stem, None
        out = f"{dd}.{mm}.{yyyy}"
        return _WS.sub(" ", stem[: m.start()] + " " + stem[m.end():]).strip(" -"), out
    m = _DATE_YMD2.search(stem)
    if m:
        yy, mm, dd = m.group(1), m.group(2), m.group(3)
        yyyy = f"20{yy}" if int(yy) <= 30 else f"19{yy}"
        out = f"{dd}.{mm}.{yyyy}"
        return _WS.sub(" ", stem[: m.start()] + " " + stem[m.end():]).strip(" -"), out
    return stem, None


_STUDIO_MAX_TOKENS = 5
_STUDIO_MIN_KEY = 4


def extract_studio(stem: str, studios: dict) -> tuple:
    """Студия — самый длинный префикс из 1–5 слов, найденный в словаре.

    Возвращает (студия или None, остаток). Ключи короче 4 символов
    игнорируются, чтобы случайные слова не принимались за студию.
    """
    tokens = stem.split()
    if not tokens:
        return None, stem
    for size in range(min(_STUDIO_MAX_TOKENS, len(tokens)), 0, -1):
        key = normalize_studio_key(" ".join(tokens[:size]))
        if len(key) < _STUDIO_MIN_KEY:
            continue
        if key in studios:
            return studios[key], " ".join(tokens[size:])
    return None, stem


_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])")
_EPORNER = re.compile(r"(?i)^eporner\s*com\s*(-\s*)?")


def split_camel(s: str) -> str:
    """Вставляет пробел на границе lower->Upper: PhillipsJenna -> Phillips Jenna."""
    return _CAMEL.sub(" ", s)


def prepare_stem(stem: str, studios: dict) -> tuple:
    """Готовит stem: вырезает дату, техмусор и студию, разбивает camelCase.

    Возвращает (остаток, дата ДД.ММ.ГГГГ или None, студия или None).
    """
    s, date = extract_date(stem)
    s = strip_tech_junk(s)
    s = _HASH_BRACKET.sub(" ", s)
    s = s.replace("[", " ").replace("]", " ")
    s = _EMPTY_PAREN.sub(" ", s)
    s = _EPORNER.sub("", s).strip()
    s = _WS.sub(" ", s).strip(" -")
    studio, s = extract_studio(s, studios)
    s = split_camel(s)
    s = _WS.sub(" ", s).strip(" -")
    return s, date, studio


def _title_case(text: str) -> str:
    keepers = []

    def _hold(m):
        keepers.append(m.group(0))
        return f"\x00{len(keepers) - 1}\x00"

    s = _KEEP_PAREN.sub(_hold, text)
    words = s.split(" ")
    out = []
    for w in words:
        if not w or "\x00" in w:
            out.append(w)
        elif w.isupper():
            out.append(w.capitalize())
        elif w[0].isupper() or w[0].isdigit():
            out.append(w[0].upper() + w[1:])
        else:
            out.append(w[0].upper() + w[1:] if w else w)
    s = " ".join(out)
    for i, k in enumerate(keepers):
        s = s.replace(f"\x00{i}\x00", k[0].upper() + k[1:] if k else k)
    return s


def _looks_like_name(word: str) -> bool:
    return bool(word) and len(word) >= 2 and word.isalpha()


def _actor_len(words: list) -> int:
    """Сколько ведущих слов — один актер: 3 при частице (Del Rio), иначе 2."""
    if len(words) >= 3 and words[1].lower() in _PARTICLES and _looks_like_name(words[2]):
        return 3
    return 2


def split_actors_title(rest: str) -> tuple:
    """Делит остаток на (актеры, название). Актеры — ведущие пары Имя Фамилия."""
    s = _WS.sub(" ", rest).strip(" -,")
    if not s:
        return [], ""
    if " - " in s:
        left, _, right = s.partition(" - ")
        left, right = left.strip(), right.strip()
        # Сторона с запятой — актеры.
        if "," in left and "," not in right:
            actors, _ = split_actors_title(left)
            return actors, _title_case(right)
        if "," in right and "," not in left:
            actors, extra = split_actors_title(right)
            title = (left + " " + extra).strip() if extra else left
            return actors, _title_case(title)
        lw, rw = left.split(" "), right.split(" ")
        # Точно 2 слова с обеих сторон: принято 'Название - Актер'.
        if len(lw) == 2 and len(rw) == 2 and all(_looks_like_name(w) for w in rw):
            return [right], _title_case(left)
        if len(rw) == 2 and all(_looks_like_name(w) for w in rw):
            return [right], _title_case(left)
        if len(lw) == 2 and all(_looks_like_name(w) for w in lw):
            return [left], _title_case(right)
        # Иначе правая короче — считаем ее актерами.
        if len(rw) <= 2 and all(_looks_like_name(w) for w in rw):
            return [right], _title_case(left)
        return [], _title_case(s)
    if "," in s:
        actors = []
        chunks = [c.strip() for c in s.split(",")]
        title = ""
        for i, ch in enumerate(chunks):
            words = ch.split(" ")
            if (
                len(words) >= 2
                and _looks_like_name(words[0])
                and _looks_like_name(words[1])
                and words[1].lower() not in _ARTICLES
            ):
                n = _actor_len(words)
                actors.append(" ".join(words[:n]))
                rest_words = words[n:]
                if rest_words:
                    title = " ".join(rest_words)
                    if i + 1 < len(chunks):
                        title += " " + " ".join(chunks[i + 1:])
                    break
            else:
                title = " ".join(chunks[i:])
                break
        actors = [_title_case(a) for a in actors]
        return actors, _title_case(title.strip())
    words = s.split(" ")
    if len(words) == 2 and all(_looks_like_name(w) for w in words):
        return [_title_case(s)], ""
    if len(words) == 3 and all(_looks_like_name(w) for w in words) and words[1].lower() in _PARTICLES:
        return [_title_case(s)], ""
    if len(words) >= 4 and _looks_like_name(words[0]) and _looks_like_name(words[1]):
        if words[1].lower() in _ARTICLES:
            return [], _title_case(s)
        n = _actor_len(words)
        return [_title_case(" ".join(words[:n]))], _title_case(" ".join(words[n:]))
    return [], _title_case(s)


def build_new_name(studio, date, actors, title) -> str:
    """Склеивает блоки через ' - ', пустые пропускает."""
    parts = []
    if studio:
        parts.append(studio)
    if date:
        parts.append(date)
    if actors:
        parts.append(", ".join(actors))
    if title:
        parts.append(title)
    return " - ".join(parts)


def clean_stem(stem: str, studios: dict) -> str:
    """Полный конвейер для stem без расширения."""
    s, date, studio = prepare_stem(stem, studios)
    actors, title = split_actors_title(s)
    return build_new_name(studio, date, actors, title)


def _cap_length(base: str, suffix: str) -> str:
    """Урезает base так, чтобы base+suffix <= 255."""
    full = base + suffix
    if len(full) > 255:
        keep = 255 - len(suffix)
        base = base[:keep].rstrip(" -")
        full = base + suffix
    return full


def clean_filename(filename: str, studios: dict) -> str:
    """Чистит одно имя, суффикс сохраняет как есть. Пустой результат -> исходное."""
    stem, suffix = split_name(filename)
    if not stem:
        return filename
    new_stem = clean_stem(stem, studios)
    if not new_stem:
        return filename
    new_stem = _WS.sub(" ", new_stem).strip(" -")
    return _cap_length(new_stem, suffix)


def _iter_files(directory: str, recursive: bool):
    if recursive:
        for root, _, files in os.walk(directory):
            for f in sorted(files):
                yield os.path.join(root, f)
    else:
        for f in sorted(os.listdir(directory)):
            p = os.path.join(directory, f)
            if os.path.isfile(p):
                yield p


def preview_directory(directory: str, studios: dict, recursive: bool = False) -> list:
    """Список (old_rel, new_rel) без записи. Коллизии -> ' (2)' в пределах папки."""
    rows = []
    used_per_dir: dict = {}
    for path in _iter_files(directory, recursive):
        rel = os.path.relpath(path, directory)
        parent = os.path.dirname(rel)
        old_base = os.path.basename(rel)
        new_base = clean_filename(old_base, studios)
        base, suffix = os.path.splitext(new_base)
        used = used_per_dir.setdefault(parent, set())
        cand, n = new_base, 2
        while cand.lower() in used:
            cand = _cap_length(f"{base} ({n})", suffix)
            n += 1
        used.add(cand.lower())
        new_rel = os.path.join(parent, cand) if parent else cand
        rows.append((rel, new_rel))
    return rows


def apply_renames(directory: str, rows: list) -> list:
    """Двухфазное переименование: сначала во временные, потом в финальные."""
    todo = [(o, n) for o, n in rows if o != n]
    if not todo:
        return []
    tmp_map = []
    for i, (old_rel, new_rel) in enumerate(todo):
        src = os.path.join(directory, old_rel)
        tmp = os.path.join(directory, os.path.dirname(old_rel), f"__ren_tmp_{i}.tmp")
        os.rename(src, tmp)
        tmp_map.append((tmp, os.path.join(directory, new_rel)))
    done = []
    for tmp, dst in tmp_map:
        os.makedirs(os.path.dirname(dst) or directory, exist_ok=True)
        base, suffix = os.path.splitext(os.path.basename(dst))
        cand, n = dst, 2
        while os.path.exists(cand):
            cand = os.path.join(os.path.dirname(dst), _cap_length(f"{base} ({n})", suffix))
            n += 1
        os.rename(tmp, cand)
        done.append((tmp, cand))
    return done


def main() -> None:
    import argparse
    import csv
    import sys

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="Чистит мусорные имена в 'Студия - ДД.ММ.ГГГГ - Актеры - Название'.")
    ap.add_argument("directory", help="Папка с файлами")
    ap.add_argument("--apply", action="store_true", help="Реально переименовать (без флага — dry-run)")
    ap.add_argument("--csv", default="preview.csv", help="Куда писать preview")
    ap.add_argument("--recursive", action="store_true", help="Обходить подпапки")
    ap.add_argument("--studios", default=os.path.join(here, "studios.json"), help="Путь к studios.json")
    args = ap.parse_args()
    studios = load_studios(args.studios) if os.path.isfile(args.studios) else {}
    rows = preview_directory(args.directory, studios, recursive=args.recursive)
    with open(args.csv, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["old", "new"])
        w.writerows(rows)
    for old, new in rows:
        mark = "OK " if old == new else ("RENAME" if args.apply else "DRY  ")
        print(f"{mark} {old} -> {new}")
    if not args.apply:
        print(f"Dry-run done. CSV: {args.csv}. Add --apply to rename.")
        return
    for old_rel, new_rel in rows:
        if old_rel == new_rel:
            print(f"OK {old_rel} -> {new_rel}")
    moved = apply_renames(args.directory, rows)
    for old_rel, new_rel in rows:
        if old_rel != new_rel:
            print(f"OK {old_rel} -> {new_rel}")


if __name__ == "__main__":
    main()

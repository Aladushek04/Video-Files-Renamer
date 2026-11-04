#!/usr/bin/env python3
"""Сервисный слой предпросмотра переименования видео.

`TitleService.preview()` обходит видеофайлы, считает OSHASH, ищет сцены в
StashDB (с кэшем) и строит `PreviewRow` для каждого файла. Сервис ничего не
печатает, не пишет CSV и не запускает subprocess — это задача CLI/TUI.

Порядок обработки:

1. обход файлов (`iter_video_files`) и `--limit`;
2. OSHASH и разбор локального имени;
3. пакетный поиск по отпечаткам с кэшем отрицательных результатов;
4. текстовый поиск для файлов без отпечатка (если разрешён и не offline);
5. `resolve_match`, обрезка имени и разрешение коллизий.

Временные ошибки StashDB (`StashDBTransientError`) превращаются в
предупреждения, а не прерывают обработку. Ошибки авторизации и запроса
пробрасываются наружу: с ними бессмысленно продолжать.

Зависимостей нет — только стандартная библиотека.
"""
import os
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field

from clean_ph_names import _cap_length
from oshash import OshashError, oshash_from_path
from stashdb_client import Scene, StashDBClient, StashDBTransientError
from title_cache import TitleCache
from title_matcher import (
    LocalNameInfo,
    MatchResult,
    is_valid_final_stem,
    normalize_for_match,
    parse_local_name,
    resolve_match,
)

__all__ = [
    "PreviewRow",
    "PreviewReport",
    "ProgressEvent",
    "TitleService",
    "iter_video_files",
]

VIDEO_EXTENSIONS = frozenset(
    {".mp4", ".mkv", ".avi", ".wmv", ".mov", ".m4v", ".webm",
     ".ts", ".mpg", ".mpeg"}
)
_TMP_PREFIX = "__ren_tmp_"
_MAX_FULL_NAME = 255


@dataclass(frozen=True)
class PreviewRow:
    """Одна строка предпросмотра (одна строка будущего CSV)."""

    approved: bool
    old_rel: str
    new_rel: str
    source: str
    confidence: float
    scene_id: str
    note: str
    oshash: str
    size: int


@dataclass(frozen=True)
class PreviewReport:
    """Результат preview: строки, предупреждения и флаг сетевых сбоев."""

    rows: tuple[PreviewRow, ...]
    warnings: tuple[str, ...]
    had_transient_errors: bool


@dataclass(frozen=True)
class ProgressEvent:
    """Событие прогресса для CLI/TUI.

    `total` относится к текущей фазе: число файлов для `scan`/`hash`, число
    уникальных хэшей для `fingerprint`, число текстовых запросов для `text`.
    """

    phase: str
    current: int
    total: int
    message: str = ""


@dataclass
class _Candidate:
    """Файл и промежуточные результаты его обработки."""

    path: str
    rel: str
    parent: str
    name: str
    suffix: str
    local: LocalNameInfo
    oshash: str
    size: int
    hash_error: str = ""
    fingerprint_scenes: list[Scene] = field(default_factory=list)
    text_scenes: list[Scene] = field(default_factory=list)
    fingerprint_error: bool = False
    text_error: bool = False


@dataclass
class _Planned:
    """Строка до разрешения коллизий."""

    candidate: _Candidate
    match: MatchResult
    final_name: str
    final_stem: str
    source: str
    approved: bool
    note: str


def _extension(name: str) -> str:
    """Расширение файла в нижнем регистре."""
    return os.path.splitext(name)[1].lower()


def _inside_root(path: str, root_real: str) -> bool:
    """Лежит ли путь внутри корня после нормализации."""
    try:
        common = os.path.commonpath(
            [os.path.normcase(os.path.realpath(path)),
             os.path.normcase(root_real)]
        )
    except ValueError:
        return False
    return common == os.path.normcase(root_real)


def _walk(directory: str, recursive: bool, root_real: str) -> Iterator[str]:
    """Обход папки без symlink/junction и без выхода за корень."""
    try:
        with os.scandir(directory) as entries:
            ordered = sorted(entries, key=lambda entry: entry.name.casefold())
    except OSError:
        return

    for entry in ordered:
        try:
            if entry.is_symlink():
                continue
            if os.path.isjunction(entry.path):
                continue
            if not _inside_root(entry.path, root_real):
                continue
            if entry.is_dir(follow_symlinks=False):
                if recursive:
                    yield from _walk(entry.path, True, root_real)
                continue
            if not entry.is_file(follow_symlinks=False):
                continue
        except OSError:
            continue

        if entry.name.startswith(_TMP_PREFIX):
            continue
        if _extension(entry.name) not in VIDEO_EXTENSIONS:
            continue
        yield entry.path


def iter_video_files(root: str, recursive: bool = False) -> Iterator[str]:
    """Видеофайлы под `root` в детерминированном порядке.

    Пропускаются не-видео расширения, временные файлы `__ren_tmp_*`,
    symlink/junction и всё, что после нормализации оказалось вне `root`.
    """
    root_path = os.path.abspath(os.fspath(root))
    if not os.path.isdir(root_path):
        return
    yield from _walk(root_path, recursive, os.path.realpath(root_path))


def _as_scenes(value: object) -> list[Scene]:
    """Список сцен из значения клиента; мусор игнорируется."""
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, Scene)]


def _join_note(left: str, right: str) -> str:
    """Склеивает непустые части примечания через `; `."""
    if not left:
        return right
    if not right:
        return left
    return f"{left}; {right}"


def _collision_name(stem: str, suffix: str, number: int) -> str:
    """Имя с суффиксом ` (n)`; суффикс коллизии сохраняется при обрезке.

    В отличие от простого `_cap_length(stem + " (2)")`, здесь база заранее
    укорачивается так, чтобы маркер коллизии не потерялся на длинных именах.
    """
    marker = "" if number <= 1 else f" ({number})"
    room = _MAX_FULL_NAME - len(suffix) - len(marker)
    if room < 0:
        room = 0
    if len(stem) > room:
        stem = stem[:room].rstrip(" -")
    return _cap_length(stem + marker, suffix)


class TitleService:
    """Preview без побочных эффектов: файлы только читаются.

    `refresh` игнорирует кэш, но не отключает сеть; `offline` наоборот
    запрещает сеть и читает только кэш (даже при `refresh=True`).
    """

    def __init__(
        self,
        root: str,
        studios: dict[str, str],
        cache: TitleCache | None,
        client: StashDBClient | None = None,
        *,
        recursive: bool = False,
        text_search: bool = True,
        offline: bool = False,
        refresh: bool = False,
        limit: int | None = None,
        oshash_func: Callable[[str], str] = oshash_from_path,
        progress: Callable[[ProgressEvent], None] | None = None,
    ) -> None:
        self._root = os.path.abspath(os.fspath(root))
        self._studios = dict(studios)
        self._cache = cache
        self._client = client
        self._recursive = recursive
        self._text_search = text_search
        self._offline = offline
        self._refresh = refresh
        self._limit = limit
        self._oshash_func = oshash_func
        self._progress = progress

    @property
    def root(self) -> str:
        """Корень, относительно которого считаются `old_rel`/`new_rel`."""
        return self._root

    # --- preview ---------------------------------------------------------

    def preview(self) -> PreviewReport:
        """Строит предпросмотр, ничего не изменяя на диске."""
        files = list(iter_video_files(self._root, self._recursive))
        if self._limit is not None:
            files = files[:max(0, self._limit)]

        total = len(files)
        self._emit("scan", total, total, f"{total} files")

        warnings: list[str] = []
        had_transient = False

        candidates = self._collect(files, total, warnings)
        fingerprint_errors = self._resolve_fingerprints(
            candidates, warnings
        )
        if fingerprint_errors:
            had_transient = True
        text_failed = self._resolve_text(candidates, warnings)
        if text_failed:
            had_transient = True

        for candidate in candidates:
            candidate.fingerprint_error = (
                bool(candidate.oshash)
                and candidate.oshash in fingerprint_errors
            )
            candidate.text_error = candidate.rel in text_failed

        planned = self._plan(candidates)
        rows = self._resolve_collisions(planned)
        self._emit("done", total, total, "")
        return PreviewReport(
            rows=tuple(rows),
            warnings=tuple(warnings),
            had_transient_errors=had_transient,
        )

    def _collect(
        self, files: Sequence[str], total: int, warnings: list[str]
    ) -> list[_Candidate]:
        """Читает размер/OSHASH и разбирает локальные имена."""
        candidates: list[_Candidate] = []
        if not total:
            self._emit("hash", 0, 0, "")
        for index, path in enumerate(files):
            rel = os.path.relpath(path, self._root)
            name = os.path.basename(rel)
            try:
                size = os.path.getsize(path)
            except OSError as exc:
                warnings.append(f"{rel}: cannot read file size: {exc}")
                self._emit("hash", index + 1, total, rel)
                continue

            hash_error = ""
            try:
                digest = self._oshash_func(path)
            except (OSError, OshashError) as exc:
                digest = ""
                hash_error = str(exc) or exc.__class__.__name__

            _, suffix = os.path.splitext(name)
            candidates.append(_Candidate(
                path=path,
                rel=rel,
                parent=os.path.dirname(rel),
                name=name,
                suffix=suffix,
                local=parse_local_name(name, self._studios),
                oshash=digest,
                size=size,
                hash_error=hash_error,
            ))
            self._emit("hash", index + 1, total, rel)
        return candidates

    def _cache_reads_allowed(self) -> bool:
        """Можно ли читать кэш (offline важнее refresh)."""
        return self._cache is not None and (not self._refresh or self._offline)

    def _network_allowed(self) -> bool:
        return self._client is not None and not self._offline

    def _resolve_fingerprints(
        self, candidates: Sequence[_Candidate], warnings: list[str]
    ) -> set[str]:
        """Дополняет кандидатов сценами по OSHASH; возвращает сбойные хэши."""
        unique = list(dict.fromkeys(
            candidate.oshash for candidate in candidates if candidate.oshash
        ))
        resolved: dict[str, list[Scene]] = {}
        missing: list[str] = []

        if self._cache_reads_allowed():
            for digest in unique:
                cached = self._cache.get_fingerprint(digest)
                if cached is None:
                    missing.append(digest)
                else:
                    resolved[digest] = list(cached)
        else:
            missing = list(unique)

        failed: set[str] = set()
        if missing and self._network_allowed():
            try:
                groups = self._client.find_scenes_by_hashes(missing)
            except StashDBTransientError as exc:
                warnings.append(
                    f"StashDB fingerprint lookup failed: {exc}"
                )
                failed = set(missing)
                for digest in missing:
                    resolved[digest] = []
            else:
                if not isinstance(groups, (list, tuple)):
                    groups = []
                for position, digest in enumerate(missing):
                    group = groups[position] if position < len(groups) else []
                    scenes = _as_scenes(group)
                    resolved[digest] = scenes
                    # Отрицательный результат кэшируется намеренно; результат
                    # временной ошибки — нет, иначе кэш отравится на TTL.
                    if self._cache is not None:
                        self._cache.put_fingerprint(digest, scenes)
        else:
            # Нет сети/клиента или кэш пуст: считаем, что сцен нет, но
            # отрицательный результат не пишем — он не подтверждён запросом.
            for digest in missing:
                resolved[digest] = []

        for candidate in candidates:
            if candidate.oshash:
                candidate.fingerprint_scenes = list(
                    resolved.get(candidate.oshash, [])
                )

        self._emit(
            "fingerprint",
            len(unique),
            len(unique),
            f"{len(missing)} fetched",
        )
        return failed

    def _resolve_text(
        self, candidates: Sequence[_Candidate], warnings: list[str]
    ) -> set[str]:
        """Текстовый поиск для файлов без fingerprint.

        Кэш текстовых запросов читается и в `offline`-режиме: `offline`
        запрещает только сеть. Сетевой вызов гейтится отдельно —
        `client is not None and not offline` (см. `_network_allowed`).
        """
        pending: list[tuple[_Candidate, str]] = []
        if self._text_search:
            for candidate in candidates:
                if candidate.fingerprint_scenes:
                    continue
                term = normalize_for_match(candidate.local.rest)
                if term:
                    pending.append((candidate, term))

        failed: set[str] = set()
        total = len(pending)
        if not total:
            self._emit("text", 0, 0, "")
            return failed

        for index, (candidate, term) in enumerate(pending):
            cached = None
            if self._cache_reads_allowed():
                cached = self._cache.get_text_query(term)

            if cached is not None:
                candidate.text_scenes = list(cached)
            elif self._network_allowed():
                try:
                    found = self._client.search_scenes(term, limit=10)
                except StashDBTransientError as exc:
                    warnings.append(
                        f"StashDB text search failed for "
                        f"{candidate.rel}: {exc}"
                    )
                    failed.add(candidate.rel)
                else:
                    candidate.text_scenes = _as_scenes(found)
                    if self._cache is not None:
                        self._cache.put_text_query(
                            term, candidate.text_scenes
                        )
            self._emit("text", index + 1, total, candidate.rel)
        return failed

    def _plan(self, candidates: Sequence[_Candidate]) -> list[_Planned]:
        """Собирает основное имя и решение об одобрении для каждого файла."""
        planned: list[_Planned] = []
        for candidate in candidates:
            match = resolve_match(
                candidate.local,
                self._studios,
                candidate.fingerprint_scenes,
                candidate.text_scenes,
            )
            final_name = _cap_length(match.new_stem, candidate.suffix)
            final_stem = self._stem(final_name, candidate.suffix)

            note = match.note
            if candidate.hash_error:
                note = _join_note(
                    note, f"hash error: {candidate.hash_error}"
                )
            if candidate.fingerprint_error or candidate.text_error:
                note = _join_note(note, "stashdb_error")

            if final_name == candidate.name:
                source = "unchanged"
                approved = False
            else:
                source = match.source
                approved = bool(
                    match.approved and is_valid_final_stem(final_stem)
                )

            planned.append(_Planned(
                candidate=candidate,
                match=match,
                final_name=final_name,
                final_stem=final_stem,
                source=source,
                approved=approved,
                note=note,
            ))
        return planned

    @staticmethod
    def _stem(name: str, suffix: str) -> str:
        """Stem из полного имени с учётом расширения."""
        if suffix and name.endswith(suffix):
            return name[:-len(suffix)]
        return name

    def _resolve_collisions(self, planned: Sequence[_Planned]) -> list[PreviewRow]:
        """Добавляет ` (2)`, `(3)` к занятым именам и строит строки."""
        moving = {
            os.path.normcase(item.candidate.rel)
            for item in planned
            if item.final_name != item.candidate.name
        }
        claimed: set[str] = set()
        rows: list[PreviewRow] = []

        for item in planned:
            candidate = item.candidate
            final_name = item.final_name
            final_stem = item.final_stem
            approved = item.approved
            note = item.note

            if final_name != candidate.name:
                number = 1
                while True:
                    final_name = _collision_name(
                        item.final_stem, candidate.suffix, number
                    )
                    final_stem = self._stem(final_name, candidate.suffix)
                    rel = self._relative(candidate.parent, final_name)
                    key = os.path.normcase(rel)
                    occupied = key in claimed
                    if not occupied:
                        full = os.path.join(self._root, rel)
                        occupied = (
                            os.path.lexists(full) and key not in moving
                        )
                    if not occupied:
                        break
                    number += 1

                if number > 1:
                    note = _join_note(note, f"collision: ({number})")
                approved = bool(
                    approved and is_valid_final_stem(final_stem)
                )
                claimed.add(
                    os.path.normcase(
                        self._relative(candidate.parent, final_name)
                    )
                )

            rows.append(PreviewRow(
                approved=approved,
                old_rel=candidate.rel,
                new_rel=self._relative(candidate.parent, final_name),
                source=item.source,
                confidence=item.match.confidence,
                scene_id=item.match.scene.id if item.match.scene else "",
                note=note,
                oshash=candidate.oshash,
                size=candidate.size,
            ))
        return rows

    @staticmethod
    def _relative(parent: str, name: str) -> str:
        """Относительный путь из папки и имени файла."""
        return os.path.join(parent, name) if parent else name

    def _emit(
        self, phase: str, current: int, total: int, message: str = ""
    ) -> None:
        """Отправляет событие прогресса, если подписчик задан."""
        if self._progress is not None:
            self._progress(ProgressEvent(phase, current, total, message))

#!/usr/bin/env python3
"""Сервисный слой предпросмотра и применения переименований видео.

`TitleService.preview()` обходит видеофайлы, считает OSHASH, ищет сцены в
StashDB (с кэшем) и строит `PreviewRow` для каждого файла.
`TitleService.apply_rows()` проверяет одобренные строки и выполняет двухфазное
переименование. Сервис ничего не печатает, не пишет CSV и не запускает
subprocess — это задача CLI/TUI.

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
import ntpath
import os
import stat
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from uuid import uuid4

from video_renamer.core.clean_ph_names import _cap_length
from video_renamer.core.fingerprint_sources import (
    FINGERPRINT_SOURCES,
    PRIMARY_SOURCE_NAME,
    merge_scenes,
)
from video_renamer.core.oshash import OshashError, oshash_from_path
from video_renamer.core.phash import PhashError, format_phash, phash_from_path
from video_renamer.core.stashdb_client import (
    Scene,
    StashDBAuthError,
    StashDBClient,
    StashDBError,
    StashDBTransientError,
)
from video_renamer.core.title_cache import TitleCache
from video_renamer.core.title_matcher import (
    LocalNameInfo,
    MatchResult,
    build_scene_stem,
    is_valid_final_stem,
    normalize_for_match,
    parse_local_name,
    resolve_match,
)

__all__ = [
    "ApplyReport",
    "ApplyResult",
    "PreviewReport",
    "PreviewRow",
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
    size: int | None
    candidates: tuple[Scene, ...] = ()


@dataclass(frozen=True)
class PreviewReport:
    """Результат preview: строки, предупреждения и флаг сетевых сбоев."""

    rows: tuple[PreviewRow, ...]
    warnings: tuple[str, ...]
    had_transient_errors: bool


@dataclass(frozen=True)
class ApplyResult:
    """Результат применения одной строки предпросмотра."""

    old_rel: str
    new_rel: str
    status: str
    message: str


@dataclass(frozen=True)
class ApplyReport:
    """Результаты apply в исходном порядке и общий признак ошибок."""

    results: tuple[ApplyResult, ...]
    had_errors: bool


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
    phash: str = ""
    phash_scenes: list[Scene] = field(default_factory=list)
    phash_error: str = ""
    failed_sources: list[str] = field(default_factory=list)
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


@dataclass
class _ApplyItem:
    """Проверенная строка apply и её состояние при двухфазном rename."""

    index: int
    row: PreviewRow
    old_path: str
    new_path: str
    new_rel: str
    temp_path: str | None = None
    renamed: bool = False


def _extension(name: str) -> str:
    """Расширение файла в нижнем регистре."""
    return os.path.splitext(name)[1].lower()


def _relative_parent(value: str) -> str:
    """Папка относительного пути по правилам Windows."""
    return ntpath.dirname(ntpath.normpath(value))


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


def _path_key(path: str) -> str:
    """Ключ пути для сравнения целей и источников на текущей ОС."""
    return os.path.normcase(os.path.abspath(path))


def _abspath_inside_root(path: str, root: str) -> bool:
    """Проверка containment без разрешения symlink (после `abspath`)."""
    try:
        common = os.path.commonpath(
            [os.path.normcase(path), os.path.normcase(root)]
        )
    except ValueError:
        return False
    return common == os.path.normcase(root)


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
    room = max(room, 0)
    if len(stem) > room:
        stem = stem[:room].rstrip(" -")
    return _cap_length(stem + marker, suffix)


class TitleService:
    """TUI-ready сервис preview и collision-safe apply.

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
        extra_clients: Mapping[str, StashDBClient] | None = None,
        phash: bool = False,
        phash_func: Callable[[str], str] | None = None,
        ffmpeg: str = "ffmpeg",
        ffprobe: str = "ffprobe",
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
        self._extra_clients = dict(extra_clients or {})
        self._phash = phash
        self._ffmpeg = ffmpeg
        self._ffprobe = ffprobe
        self._phash_func = phash_func or self._default_phash

    def _default_phash(self, path: str) -> str:
        return format_phash(
            phash_from_path(path, ffmpeg=self._ffmpeg, ffprobe=self._ffprobe)
        )

    @property
    def root(self) -> str:
        """Корень, относительно которого считаются `old_rel`/`new_rel`."""
        return self._root

    # --- preview ---------------------------------------------------------

    def build_alternative(
        self,
        row: PreviewRow,
        scene: Scene,
        rows: Sequence[PreviewRow],
    ) -> PreviewRow:
        """Собирает строку для вручную выбранного кандидата.

        Имя строится тем же `build_scene_stem`, что и обычное совпадение;
        коллизии с другими строками и файлами разрешаются суффиксом ` (n)`.
        Выбранный вручную кандидат сразу одобряется — человек уже сделал выбор.
        """
        suffix = os.path.splitext(row.old_rel)[1]
        local = parse_local_name(row.old_rel, self._studios)
        stem = build_scene_stem(scene, local, self._studios)
        name = _cap_length(stem, suffix)
        final_stem = self._stem(name, suffix)
        if not is_valid_final_stem(final_stem):
            raise ValueError("alternative name is invalid")

        parent = os.path.dirname(row.old_rel)
        desired = self._relative(parent, name)
        used = {
            os.path.normcase(other.new_rel)
            for other in rows
            if other.old_rel != row.old_rel
        }
        moving = {
            os.path.normcase(other.old_rel)
            for other in rows
            if other.old_rel != row.old_rel
            and other.new_rel != other.old_rel
        }
        number = 1
        while True:
            key = os.path.normcase(desired)
            occupied = key in used
            if not occupied:
                full = os.path.join(self._root, desired)
                occupied = os.path.lexists(full) and key not in moving
            if not occupied:
                break
            number += 1
            name = _collision_name(stem, suffix, number)
            final_stem = self._stem(name, suffix)
            if not is_valid_final_stem(final_stem):
                raise ValueError("alternative name is invalid")
            desired = self._relative(parent, name)

        note = f"выбран кандидат: {scene.title or scene.id}"
        if number > 1:
            note = _join_note(note, f"collision: ({number})")
        return replace(
            row,
            new_rel=desired,
            source="text",
            confidence=0.0,
            scene_id=scene.id,
            note=note,
            approved=True,
            candidates=row.candidates,
        )

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
        oshash_failures, oshash_transient = self._resolve_fingerprints(
            candidates, warnings
        )
        phash_failures, phash_transient = (
            self._resolve_phash(candidates, warnings) if self._phash else ({}, False)
        )
        text_failed = self._resolve_text(candidates, warnings)
        if oshash_transient or phash_transient or text_failed:
            had_transient = True

        for candidate in candidates:
            failed = [
                name for name, digests in oshash_failures.items()
                if candidate.oshash and candidate.oshash in digests
            ]
            failed += [
                name for name, digests in phash_failures.items()
                if candidate.phash and candidate.phash in digests
                and name not in failed
            ]
            candidate.failed_sources = failed
            candidate.text_error = candidate.rel in text_failed

        planned = self._plan(candidates)
        rows = self._resolve_collisions(planned)
        self._emit("done", total, total, "")
        return PreviewReport(
            rows=tuple(rows),
            warnings=tuple(warnings),
            had_transient_errors=had_transient,
        )

    # --- apply -----------------------------------------------------------

    def apply_rows(
        self,
        rows: Sequence[PreviewRow],
        *,
        dry_run: bool = True,
        force: bool = False,
        suffix_collisions: bool = False,
    ) -> ApplyReport:
        """Проверяет и двухфазно переименовывает одобренные строки.

        Неодобренные строки пропускаются до обращения к файловой системе.
        В dry-run выполняются те же проверки и разрешение коллизий, но ни один
        `os.rename` не вызывается.

        В `ApplyResult` для переименованной строки `new_rel` — фактическая
        цель после разрешения коллизий (включая добавленный ` (2)` при
        `suffix_collisions`), а не значение из исходного CSV.
        """
        results: list[ApplyResult | None] = []
        approved: list[tuple[int, PreviewRow]] = []
        for index, row in enumerate(rows):
            if row.approved:
                results.append(None)
                approved.append((index, row))
            else:
                results.append(ApplyResult(
                    row.old_rel, row.new_rel, "skipped", "not approved"
                ))

        root_real = os.path.realpath(self._root)
        items: list[_ApplyItem] = []
        for index, row in approved:
            item, error = self._prepare_apply_item(
                index, row, root_real, force
            )
            if error:
                results[index] = ApplyResult(
                    row.old_rel, row.new_rel, "error", error
                )
            elif item is not None:
                items.append(item)

        pending = items
        ready: list[_ApplyItem] = []
        while pending:
            moving = {_path_key(item.old_path) for item in pending}
            claimed: set[str] = set()
            ready = []
            blocked: list[_ApplyItem] = []
            for item in pending:
                error = self._resolve_apply_target(
                    item, moving, claimed, suffix_collisions
                )
                if error:
                    results[item.index] = ApplyResult(
                        item.row.old_rel, item.row.new_rel, "error", error
                    )
                    blocked.append(item)
                else:
                    ready.append(item)
            if not blocked:
                break
            # Заблокированная строка не войдёт в phase one: её old path
            # остаётся занятым и может сделать невалидными другие цели.
            pending = ready

        if dry_run:
            for item in ready:
                results[item.index] = ApplyResult(
                    item.row.old_rel, item.new_rel, "renamed", "dry-run"
                )
            return self._apply_report(results)

        active: list[_ApplyItem] = []
        for item in ready:
            temp_path = self._new_apply_temp_path(item.old_path)
            try:
                os.rename(item.old_path, temp_path)
            except OSError as exc:
                message = str(exc) or exc.__class__.__name__
                results[item.index] = ApplyResult(
                    item.row.old_rel, item.new_rel, "error",
                    f"temporary rename failed: {message}",
                )
            else:
                item.temp_path = temp_path
                active.append(item)

        for item in active:
            error = ""
            if os.path.lexists(item.new_path):
                error = "target became occupied during rename"
            else:
                try:
                    os.rename(item.temp_path, item.new_path)
                except OSError as exc:
                    message = str(exc) or exc.__class__.__name__
                    error = f"final rename failed: {message}"
                else:
                    item.temp_path = None
                    item.renamed = True
                    results[item.index] = ApplyResult(
                        item.row.old_rel, item.new_rel, "renamed", ""
                    )
                    continue

            rollback_errors = self._rollback_apply_item(
                item, active, results, frozenset()
            )
            if rollback_errors:
                error += "; rollback failed: " + "; ".join(rollback_errors)
            results[item.index] = ApplyResult(
                item.row.old_rel, item.new_rel, "error", error
            )

        return self._apply_report(results)

    def _prepare_apply_item(
        self,
        index: int,
        row: PreviewRow,
        root_real: str,
        force: bool,
    ) -> tuple[_ApplyItem | None, str]:
        """Выполняет все проверки строки, кроме занятости цели."""
        old_rel = row.old_rel
        new_rel = row.new_rel
        for label, value in (("old", old_rel), ("new", new_rel)):
            if "\0" in value:
                return None, f"{label} path contains NUL"
            if (not value or os.path.isabs(value) or ntpath.isabs(value)
                    or os.path.splitdrive(value)[0]
                    or ntpath.splitdrive(value)[0]):
                return None, f"{label} path must be relative"

        old_path = os.path.abspath(os.path.join(self._root, old_rel))
        new_path = os.path.abspath(os.path.join(self._root, new_rel))
        if (not _abspath_inside_root(old_path, self._root)
                or not _abspath_inside_root(new_path, self._root)
                or not _inside_root(old_path, root_real)
                or not _inside_root(new_path, root_real)):
            return None, "path escapes root"
        if (ntpath.normcase(_relative_parent(old_rel))
                != ntpath.normcase(_relative_parent(new_rel))):
            return None, "new path must stay in the old file's folder"

        new_name = ntpath.basename(new_rel)
        new_stem = ntpath.splitext(new_name)[0]
        if len(new_name) > _MAX_FULL_NAME:
            return None, "new name exceeds 255 characters"
        if not is_valid_final_stem(new_stem):
            return None, "invalid new name"
        if _extension(old_rel) != _extension(new_rel):
            return None, "old and new extensions differ"

        try:
            if os.path.islink(old_path) or os.path.isjunction(old_path):
                return None, "old path must not be a symlink or junction"
            source_stat = os.lstat(old_path)
        except OSError as exc:
            message = str(exc) or exc.__class__.__name__
            return None, f"cannot stat old path: {message}"
        if not stat.S_ISREG(source_stat.st_mode):
            return None, "old path is not a regular file"

        if not force:
            if row.size is not None and source_stat.st_size != row.size:
                return None, (
                    f"stale size: expected {row.size}, "
                    f"actual {source_stat.st_size}"
                )
            try:
                digest = self._oshash_func(old_path)
            except (OSError, OshashError) as exc:
                message = str(exc) or exc.__class__.__name__
                return None, f"cannot hash old path: {message}"
            if digest != row.oshash:
                return None, "stale oshash"

        return _ApplyItem(
            index=index,
            row=row,
            old_path=old_path,
            new_path=new_path,
            new_rel=new_rel,
        ), ""

    def _resolve_apply_target(
        self,
        item: _ApplyItem,
        moving: set[str],
        claimed: set[str],
        suffix_collisions: bool,
    ) -> str:
        """Проверяет цель или явно добавляет суффикс коллизии."""
        original_stem, suffix = os.path.splitext(item.new_rel)
        number = 1
        while True:
            candidate_rel = item.new_rel if number == 1 else _collision_name(
                original_stem, suffix, number
            )
            candidate_path = os.path.abspath(
                os.path.join(self._root, candidate_rel)
            )
            key = _path_key(candidate_path)
            occupied = key in claimed or (
                os.path.lexists(candidate_path) and key not in moving
            )
            if not occupied:
                item.new_rel = candidate_rel
                item.new_path = candidate_path
                claimed.add(key)
                return ""
            if not suffix_collisions:
                return "target is occupied"
            number += 1

    @staticmethod
    def _new_apply_temp_path(old_path: str) -> str:
        """Уникальное временное имя в той же папке, что и источник."""
        directory = os.path.dirname(old_path)
        while True:
            candidate = os.path.join(directory, _TMP_PREFIX + uuid4().hex)
            if not os.path.lexists(candidate):
                return candidate

    def _rollback_apply_item(
        self,
        item: _ApplyItem,
        active: Sequence[_ApplyItem],
        results: list[ApplyResult | None],
        visiting: frozenset[int],
    ) -> list[str]:
        """Возвращает файл на old, при необходимости откатывая цепочку."""
        if item.index in visiting:
            return [f"{item.row.old_rel}: rollback cycle detected"]
        source = item.temp_path
        if source is None and item.renamed:
            source = item.new_path
        if source is None:
            return [f"{item.row.old_rel}: no renamed file to restore"]

        if item.renamed and _path_key(source) == _path_key(item.old_path):
            item.renamed = False
            results[item.index] = ApplyResult(
                item.row.old_rel, item.new_rel, "error",
                "rolled back because another rename failed",
            )
            return []

        errors: list[str] = []
        if os.path.lexists(item.old_path):
            occupant = next((
                other for other in active
                if other.index != item.index
                and other.renamed
                and _path_key(other.new_path) == _path_key(item.old_path)
            ), None)
            if occupant is not None:
                errors.extend(self._rollback_apply_item(
                    occupant, active, results, visiting | {item.index}
                ))
            if os.path.lexists(item.old_path):
                errors.append(f"{item.row.old_rel}: original path is occupied")
                return errors

        try:
            os.rename(source, item.old_path)
        except OSError as exc:
            message = str(exc) or exc.__class__.__name__
            errors.append(f"{item.row.old_rel}: {message}")
        else:
            was_renamed = item.renamed
            item.temp_path = None
            item.renamed = False
            if was_renamed:
                results[item.index] = ApplyResult(
                    item.row.old_rel, item.new_rel, "error",
                    "rolled back because another rename failed",
                )
        return errors

    @staticmethod
    def _apply_report(results: Sequence[ApplyResult | None]) -> ApplyReport:
        """Собирает неизменяемый отчёт и проверяет полноту результатов."""
        final: list[ApplyResult] = []
        for result in results:
            if result is None:
                raise RuntimeError("missing apply result")
            final.append(result)
        return ApplyReport(
            results=tuple(final),
            had_errors=any(result.status == "error" for result in final),
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
        return self._source_network_allowed(self._client)

    def _source_network_allowed(self, client: StashDBClient | None) -> bool:
        return client is not None and not self._offline

    def _fingerprint_clients(self) -> list[tuple[str, StashDBClient | None]]:
        """Источники в порядке приоритета: StashDB, затем ThePornDB и FansDB."""
        clients: list[tuple[str, StashDBClient | None]] = [
            (PRIMARY_SOURCE_NAME, self._client)
        ]
        for source in FINGERPRINT_SOURCES[1:]:
            client = self._extra_clients.get(source.name)
            if client is not None:
                clients.append((source.name, client))
        return clients

    def _lookup_source(
        self,
        name: str,
        client: StashDBClient | None,
        algorithm: str,
        digests: Sequence[str],
        warnings: list[str],
    ) -> tuple[dict[str, list[Scene]], set[str], bool]:
        """Сцены одного источника по фингерпринтам.

        Возвращает сцены, сбойные хэши (для заметки `<Имя>_error`) и признак
        временной ошибки (для кода возврата 1). Сбойные хэши не кэшируются.
        Неверный ключ и ошибка запроса вторичного источника — предупреждение и
        сбойные хэши, но не временная ошибка. Неверный ключ и ошибка запроса
        StashDB пробрасываются наружу.
        """
        resolved: dict[str, list[Scene]] = {}
        missing: list[str] = []
        if self._cache_reads_allowed():
            for digest in digests:
                cached = self._cache.get_source_fingerprint(name, algorithm, digest)
                if cached is None:
                    missing.append(digest)
                else:
                    resolved[digest] = list(cached)
        else:
            missing = list(digests)

        failed: set[str] = set()
        transient = False
        if missing and self._source_network_allowed(client):
            try:
                groups = client.find_scenes_by_hashes(missing, algorithm=algorithm)
            except StashDBTransientError as exc:
                warnings.append(self._source_warning(name, algorithm, exc))
                failed = set(missing)
                transient = True
                for digest in missing:
                    resolved[digest] = []
            except StashDBError as exc:
                if name == PRIMARY_SOURCE_NAME:
                    raise
                warnings.append(self._source_warning(name, algorithm, exc))
                failed = set(missing)
                for digest in missing:
                    resolved[digest] = []
            else:
                if not isinstance(groups, (list, tuple)):
                    groups = []
                for position, digest in enumerate(missing):
                    group = groups[position] if position < len(groups) else []
                    scenes = [replace(scene, source=name) for scene in _as_scenes(group)]
                    resolved[digest] = scenes
                    # Отрицательный результат кэшируется намеренно; результат
                    # временной ошибки — нет, иначе кэш отравится на TTL.
                    if self._cache is not None:
                        self._cache.put_source_fingerprint(name, algorithm, digest, scenes)
        else:
            # Нет сети/клиента или кэш пуст: считаем, что сцен нет, но
            # отрицательный результат не пишем — он не подтверждён запросом.
            for digest in missing:
                resolved[digest] = []
        return resolved, failed, transient

    @staticmethod
    def _source_warning(name: str, algorithm: str, exc: StashDBError) -> str:
        """Текст предупреждения о сбое источника; для вторичных — без слова StashDB."""
        if name == PRIMARY_SOURCE_NAME:
            return f"{name} {algorithm} lookup failed: {exc}"
        env_name = next(
            source.api_key_env for source in FINGERPRINT_SOURCES
            if source.name == name
        )
        if isinstance(exc, StashDBAuthError):
            return f"{name} {algorithm}: ключ отклонён, проверь {env_name}"
        if isinstance(exc, StashDBTransientError):
            return f"{name} {algorithm}: временная ошибка сети или сервера"
        return f"{name} {algorithm}: ошибка запроса"

    def _resolve_fingerprints(
        self, candidates: Sequence[_Candidate], warnings: list[str]
    ) -> tuple[dict[str, set[str]], bool]:
        """Дополняет кандидатов сценами по OSHASH из всех источников.

        Возвращает сбойные хэши по каждому источнику и признак временной ошибки.
        """
        unique = list(dict.fromkeys(
            candidate.oshash for candidate in candidates if candidate.oshash
        ))
        per_source: dict[str, dict[str, list[Scene]]] = {}
        failures: dict[str, set[str]] = {}
        fetched = 0
        transient = False
        for name, client in self._fingerprint_clients():
            resolved, failed, source_transient = self._lookup_source(
                name, client, "OSHASH", unique, warnings
            )
            per_source[name] = resolved
            failures[name] = failed
            transient = transient or source_transient
            fetched += len(failed)

        for candidate in candidates:
            if candidate.oshash:
                candidate.fingerprint_scenes = merge_scenes([
                    per_source[name].get(candidate.oshash, [])
                    for name in per_source
                ])

        self._emit(
            "fingerprint",
            len(unique),
            len(unique),
            f"{fetched} failed",
        )
        return failures, transient

    def _resolve_phash(
        self, candidates: Sequence[_Candidate], warnings: list[str]
    ) -> tuple[dict[str, set[str]], bool]:
        """PHASH для файлов без OSHASH-совпадения.

        Возвращает сбойные хэши по источникам и признак временной ошибки.
        """
        pending = [
            candidate for candidate in candidates
            if candidate.oshash and not candidate.fingerprint_scenes
            and not candidate.hash_error
        ]
        total = len(pending)
        for index, candidate in enumerate(pending):
            candidate.phash = self._local_phash(candidate)
            self._emit("phash", index + 1, total, candidate.rel)

        digests = list(dict.fromkeys(
            candidate.phash for candidate in pending if candidate.phash
        ))
        per_source: dict[str, dict[str, list[Scene]]] = {}
        failures: dict[str, set[str]] = {}
        transient = False
        for name, client in self._fingerprint_clients():
            resolved, failed, source_transient = self._lookup_source(
                name, client, "PHASH", digests, warnings
            )
            per_source[name] = resolved
            failures[name] = failed
            transient = transient or source_transient

        for candidate in pending:
            if candidate.phash:
                candidate.phash_scenes = merge_scenes([
                    per_source[name].get(candidate.phash, [])
                    for name in per_source
                ])
        self._emit("phash", total, total, "")
        return failures, transient

    def _local_phash(self, candidate: _Candidate) -> str:
        """PHASH файла из локального кэша или вычисленный; `""` при ошибке файла."""
        if self._cache is not None:
            cached = self._cache.get_local_phash(candidate.oshash, candidate.size)
            if cached:
                return cached
        try:
            value = self._phash_func(candidate.path)
        except PhashError as exc:
            candidate.phash_error = str(exc) or exc.__class__.__name__
            return ""
        if self._cache is not None:
            self._cache.put_local_phash(candidate.oshash, candidate.size, value)
        return value

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
                if candidate.fingerprint_scenes or candidate.phash_scenes:
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
                candidate.phash_scenes,
            )
            final_name = _cap_length(match.new_stem, candidate.suffix)
            final_stem = self._stem(final_name, candidate.suffix)

            note = match.note
            if candidate.hash_error:
                note = _join_note(
                    note, f"hash error: {candidate.hash_error}"
                )
            if candidate.phash_error:
                note = _join_note(
                    note, f"phash error: {candidate.phash_error}"
                )
            for name in candidate.failed_sources:
                token = "stashdb_error" if name == PRIMARY_SOURCE_NAME else f"{name}_error"
                note = _join_note(note, token)
            if candidate.text_error and "stashdb_error" not in note:
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
                candidates=item.match.candidates,
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

"""Тонкий адаптер GUI над общими операциями ревью.

Вся логика preview, apply и правок строк живёт в `core.review_actions` и
`TitleService`: контроллер только хранит состояние экрана. Все методы, меняющие
`model`, вызываются из главного потока. Только `run_preview` и `run_apply`
предназначены для фонового потока: они не трогают GUI-модель и возвращают
отчёты.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from pathlib import Path

from video_renamer.core.fingerprint_sources import FINGERPRINT_SOURCES
from video_renamer.core.performer_names import PerformerIndex
from video_renamer.core.review_actions import (
    apply_rows,
    basename_new_rel,
    build_service,
    load_performer_index,
    remove_renamed,
    run_preview,
    studios_catalog,
    validate_basename,
)
from video_renamer.core.stashdb_client import Scene
from video_renamer.core.title_service import (
    ApplyReport,
    PreviewReport,
    PreviewRow,
    ProgressEvent,
    TitleService,
)
from video_renamer.paths import LOG_PATH, load_project_env, resolve_studios_path
from video_renamer.ui.tui_model import ReviewModel

PreviewFactory = Callable[[str, Callable[[ProgressEvent], None]], PreviewReport]
ApplyFactory = Callable[[list[PreviewRow]], ApplyReport]


class RenamerController:
    """Состояние экрана ревью и операции поверх `TitleService`."""

    def __init__(
        self,
        folder: str | Path | None = None,
        *,
        preview_factory: PreviewFactory | None = None,
        apply_factory: ApplyFactory | None = None,
        log_path: str | None = None,
    ) -> None:
        self.model = ReviewModel("", [])
        self.folder: Path | None = None
        self._preview_factory = preview_factory
        self._apply_factory = apply_factory
        self._service: TitleService | None = None
        self.log_path = log_path or str(LOG_PATH)
        self._preview_root = ""
        self.keys: dict[str, bool] = {}
        self.studios: dict[str, str] = {}
        self.studios_problem = ""
        self.performers: PerformerIndex | None = None
        self.reload_environment()
        if folder:
            self.set_folder(str(folder))

    # --- окружение -------------------------------------------------------

    def reload_environment(self) -> None:
        """Перечитывает .env, справочники и сбрасывает кэш сервиса."""
        load_project_env()
        self.keys = {
            source.name: bool((os.environ.get(source.api_key_env) or "").strip())
            for source in FINGERPRINT_SOURCES
        }
        self.studios, self.studios_problem = studios_catalog()
        self.performers = load_performer_index()
        self._service = None

    @property
    def stashdb_key(self) -> str:
        return (os.environ.get("STASHDB_API_KEY") or "").strip()

    @property
    def offline(self) -> bool:
        """Нет ключа StashDB: его поиск недоступен, вторичные — по своим ключам."""
        return not bool(self.stashdb_key)

    def source_configured(self, name: str) -> bool:
        return bool(self.keys.get(name))

    def catalog_text(self) -> str:
        """Состояние справочника: размер, отсутствие или повреждение — различимо."""
        if self.studios_problem == "missing":
            return "Справочник: studios.json не найден — поиск студий отключён"
        if self.studios_problem:
            reason = self.studios_problem.removeprefix("unreadable: ").strip()
            return f"Справочник повреждён: {reason[:80]} — поиск студий отключён"
        path = resolve_studios_path()
        try:
            size = path.stat().st_size / (1024 * 1024)
        except OSError:
            return "Справочник: размер недоступен"
        return f"Справочник: {size:.1f} MB"

    # --- папка и preview -------------------------------------------------

    def resolve_folder(self, folder: str) -> Path:
        """Проверяет папку, не меняя выбранный корень и текущее ревью."""
        folder = (folder or "").strip().strip('"')
        if not folder:
            raise ValueError("Выберите папку с видео")
        path = Path(folder).expanduser()
        if not path.is_dir():
            raise ValueError(f"Папка не найдена: {path}")
        return path

    def set_folder(self, folder: str) -> Path:
        """Запоминает папку для следующего preview; строки не перепривязываются."""
        path = self.resolve_folder(folder)
        self.folder = path
        self._service = None
        return path

    @property
    def review_root(self) -> str:
        """Корень, для которого получены строки текущего ревью."""
        return self.model.folder

    @property
    def review_stale(self) -> bool:
        """Строки есть, но выбрана другая папка — применять их нельзя."""
        if self.folder is None or not self.model.rows:
            return False
        return not self.model.is_for(str(self.folder))

    def folder_change_needs_confirmation(self, folder: str | Path) -> bool:
        """Нужно ли подтверждение: в таблице строки другого корня."""
        return bool(self.model.rows) and not self.model.is_for(str(folder))

    def discard_review(self) -> None:
        """Сбрасывает строки и привязку к корню после подтверждения."""
        self.model.reset_review()
        self._service = None

    def run_preview(
        self,
        progress: Callable[[ProgressEvent], None],
        *,
        cancel_event: threading.Event | None = None,
    ) -> PreviewReport:
        """Строит предложения в фоновом потоке. Модель здесь не меняется."""
        if self.folder is None:
            raise ValueError("Сначала выберите папку")
        folder = str(self.folder)
        self._preview_root = folder
        if self._preview_factory is not None:
            return self._preview_factory(folder, progress)

        return run_preview(
            folder,
            api_key=self.stashdb_key,
            studios=self.studios,
            performers=self.performers,
            progress=progress,
            cancel_event=cancel_event,
            service_callback=self._remember_service,
        )

    def _remember_service(self, service: TitleService) -> None:
        self._service = service

    def apply_preview(self, report: PreviewReport, *, root: str | None = None) -> None:
        """Публикует отчёт и привязывает строки к корню, для которого он получен.

        `root` — папка, фактически просканированная preview; по умолчанию —
        последний запущенный `run_preview`, и только затем текущая папка.
        """
        review_root = (
            root or self._preview_root or (str(self.folder) if self.folder else "")
        ).strip()
        self.model = ReviewModel(review_root, list(report.rows))

    # --- ревью -----------------------------------------------------------

    def approve(self, index: int) -> PreviewRow:
        return self.model.set_approved(index, True)

    def reject(self, index: int) -> PreviewRow:
        return self.model.set_approved(index, False)

    def toggle(self, index: int) -> PreviewRow:
        return self.model.toggle(index)

    def rename_basename(self, index: int, value: str) -> PreviewRow:
        """Меняет имя файла, сохраняя папку и расширение; строка одобряется."""
        row = self.model.rows[index]
        error = validate_basename(row, value)
        if error:
            raise ValueError(error)
        return self.model.set_new_rel(index, basename_new_rel(row, value), approved=True)

    def build_alternative(self, index: int, scene: Scene) -> PreviewRow:
        """Собирает имя по выбранному вручную кандидату."""
        row = self.model.rows[index]
        updated = self._get_service().build_alternative(row, scene, self.model.rows)
        self.model.set_row(index, updated)
        return updated

    def _get_service(self) -> TitleService:
        if self._service is not None:
            return self._service
        folder = self.model.folder or (str(self.folder) if self.folder else "")
        self._service = build_service(
            folder,
            studios=self.studios,
            performers=self.performers,
        )
        return self._service

    def invalidate_service(self) -> None:
        self._service = None

    # --- apply -----------------------------------------------------------

    def run_apply(self, rows: list[PreviewRow]) -> tuple[ApplyReport, str]:
        """Переименовывает строки в корне ревью; сбой журнала не отменяет apply."""
        root = self.model.folder
        if not root:
            raise ValueError("Ревью не привязано к папке: выполните Preview")
        if self.review_stale:
            raise ValueError("Папка изменилась после Preview: выполните Preview заново")
        if self._apply_factory is not None:
            return self._apply_factory(rows), ""

        return apply_rows(root, rows, log_path=self.log_path)

    def remove_renamed(self, report: ApplyReport) -> None:
        """Убирает из ревью строки, которые действительно переименованы."""
        self.model.set_rows(remove_renamed(self.model.rows, report))

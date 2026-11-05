"""Чистая модель TUI: фильтры, отметки и статистика без Textual."""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable

from video_renamer.core.title_service import PreviewRow

FILTERS: tuple[tuple[str, str], ...] = (
    ("all", "Все"),
    ("pending", "Не одобрено"),
    ("text", "Текст"),
    ("heuristic", "Эвристика"),
    ("fingerprint", "Fingerprint"),
    ("phash", "PHASH"),
    ("unchanged", "Без изменений"),
)


@dataclass
class ReviewModel:
    """Состояние экрана ревью: строки, фильтр и ручные approvals."""

    folder: str
    rows: list[PreviewRow]

    def __post_init__(self) -> None:
        self._filter = "all"

    def set_rows(self, rows: Iterable[PreviewRow]) -> None:
        self.rows = list(rows)

    def visible(self) -> list[tuple[int, PreviewRow]]:
        result: list[tuple[int, PreviewRow]] = []
        for index, row in enumerate(self.rows):
            if self._matches(row):
                result.append((index, row))
        return result

    def _matches(self, row: PreviewRow) -> bool:
        if self.filter == "all":
            return True
        if self.filter == "pending":
            return not row.approved and row.source != "unchanged"
        return row.source == self.filter

    @property
    def filter(self) -> str:
        return self._filter

    @filter.setter
    def filter(self, value: str) -> None:
        if value not in {key for key, _ in FILTERS}:
            raise ValueError(f"unknown filter: {value}")
        self._filter = value

    def toggle(self, index: int) -> PreviewRow:
        row = self.rows[index]
        updated = replace(row, approved=not row.approved)
        self.rows[index] = updated
        return updated

    def set_approved(self, index: int, approved: bool) -> PreviewRow:
        row = replace(self.rows[index], approved=approved)
        self.rows[index] = row
        return row

    def set_row(self, index: int, row: PreviewRow) -> PreviewRow:
        self.rows[index] = row
        return row

    def set_new_rel(
        self,
        index: int,
        new_rel: str,
        *,
        approved: bool | None = None,
    ) -> PreviewRow:
        row = self.rows[index]
        if new_rel == row.old_rel:
            updated = replace(row, new_rel=new_rel, source="unchanged",
                              approved=False)
        else:
            updated = replace(
                row,
                new_rel=new_rel,
                approved=row.approved if approved is None else approved,
            )
        self.rows[index] = updated
        return updated

    def approved_rows(self) -> list[PreviewRow]:
        return [row for row in self.rows if row.approved]

    def stats(self) -> dict[str, int]:
        stats = {
            "total": len(self.rows),
            "approved": 0,
            "pending": 0,
            "text": 0,
            "heuristic": 0,
            "fingerprint": 0,
            "phash": 0,
            "unchanged": 0,
        }
        for row in self.rows:
            if row.approved:
                stats["approved"] += 1
            elif row.source != "unchanged":
                stats["pending"] += 1
            if row.source in stats:
                stats[row.source] += 1
        return stats

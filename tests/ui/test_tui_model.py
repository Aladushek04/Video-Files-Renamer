"""Тесты модели TUI без зависимости от Textual."""

from video_renamer.core.title_service import PreviewRow
from video_renamer.ui.tui_model import ReviewModel


def row(source="text", approved=False, old="old.mp4", new="new.mp4"):
    return PreviewRow(approved, old, new, source, 0.7, "scene", "note", "h", 3)


def test_filter_and_toggle():
    model = ReviewModel(
        "D:/Videos",
        [
            row("fingerprint", True),
            row("text", False),
            row("heuristic", False),
            row("unchanged", False),
        ],
    )
    assert len(model.visible()) == 4
    model.filter = "pending"
    assert [item.source for _, item in model.visible()] == ["text", "heuristic"]
    model.filter = "text"
    assert len(model.visible()) == 1
    assert model.toggle(1).approved is True
    assert model.approved_rows()[0].old_rel == "old.mp4"


def test_set_new_rel_can_approve_and_distinguish_unchanged():
    model = ReviewModel("D:/Videos", [row("heuristic", False)])
    updated = model.set_new_rel(0, "Custom.mp4", approved=True)
    assert updated.new_rel == "Custom.mp4"
    assert updated.approved is True
    assert updated.source == "heuristic"

    unchanged = model.set_new_rel(0, "old.mp4")
    assert unchanged.source == "unchanged"
    assert unchanged.approved is False


def test_set_row_replaces_manual_choice():
    model = ReviewModel("D:/Videos", [row("text", False)])
    replacement = row("text", True, new="Chosen.mp4")
    assert model.set_row(0, replacement) == replacement
    assert model.rows[0].new_rel == "Chosen.mp4"


def test_stats_counts_phash_rows_as_pending():
    model = ReviewModel("D:/Videos", [row("phash", False)])
    stats = model.stats()
    assert stats["phash"] == 1
    assert stats["pending"] == 1
    assert stats["approved"] == 0


def test_stats_counts_sources_and_approvals():
    model = ReviewModel(
        "D:/Videos",
        [
            row("fingerprint", True),
            row("text", False),
            row("heuristic", False),
            row("unchanged", False),
        ],
    )
    assert model.stats() == {
        "total": 4,
        "approved": 1,
        "pending": 2,
        "text": 1,
        "heuristic": 1,
        "fingerprint": 1,
        "phash": 0,
        "unchanged": 1,
    }

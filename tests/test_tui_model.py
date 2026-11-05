"""Тесты модели TUI без зависимости от Textual."""
from video_renamer.tui_model import ReviewModel
from video_renamer.core.title_service import PreviewRow


def row(source="text", approved=False, old="old.mp4", new="new.mp4"):
    return PreviewRow(approved, old, new, source, 0.7, "scene", "note", "h", 3)


def test_filter_and_toggle():
    model = ReviewModel("D:/Videos", [
        row("fingerprint", True),
        row("text", False),
        row("heuristic", False),
        row("unchanged", False),
    ])
    assert len(model.visible()) == 4
    model.filter = "pending"
    assert [item.source for _, item in model.visible()] == ["text", "heuristic"]
    model.filter = "text"
    assert len(model.visible()) == 1
    _, visible = model.visible()[0]
    assert model.toggle(1).approved is True
    assert model.approved_rows()[0].old_rel == "old.mp4"


def test_stats_counts_sources_and_approvals():
    model = ReviewModel("D:/Videos", [
        row("fingerprint", True),
        row("text", False),
        row("heuristic", False),
        row("unchanged", False),
    ])
    assert model.stats() == {
        "total": 4,
        "approved": 1,
        "pending": 2,
        "text": 1,
        "heuristic": 1,
        "fingerprint": 1,
        "unchanged": 1,
    }

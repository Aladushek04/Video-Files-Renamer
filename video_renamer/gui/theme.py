"""Палитра, шрифты и подписи для CustomTkinter-интерфейса."""

from __future__ import annotations

COLORS = {
    "bg": "#0b1020",
    "panel": "#0f172a",
    "panel_2": "#111c33",
    "border": "#1e293b",
    "border_light": "#334155",
    "text": "#dbeafe",
    "muted": "#94a3b8",
    "accent": "#38bdf8",
    "accent_hover": "#0ea5e9",
    "green": "#047857",
    "green_hover": "#059669",
    "green_text": "#34d399",
    "orange": "#b45309",
    "orange_hover": "#d97706",
    "orange_text": "#fbbf24",
    "red": "#b91c1c",
    "red_hover": "#dc2626",
    "red_text": "#f87171",
    "row": "#0f172a",
    "row_alt": "#0d1426",
    "row_selected": "#132b45",
    "row_hover": "#14213a",
}

SOURCE_LABELS = {
    "fingerprint": "Fingerprint",
    "phash": "PHASH",
    "text": "Текст",
    "heuristic": "Эвристика",
    "unchanged": "Без изменений",
}

SOURCE_COLORS = {
    "fingerprint": ("#0e7490", "#cffafe"),
    "phash": ("#6d28d9", "#ede9fe"),
    "text": ("#b45309", "#fef3c7"),
    "heuristic": ("#b91c1c", "#fee2e2"),
    "unchanged": ("#334155", "#cbd5e1"),
}

PHASE_LABELS = {
    "scan": "Сканирую файлы",
    "hash": "Считаю отпечатки",
    "fingerprint": "Ищу в StashDB, ThePornDB и FansDB",
    "phash": "Считаю PHASH",
    "text": "Текстовый поиск",
    "done": "Готово",
}

FONT_FAMILY = "Segoe UI"
MONO_FAMILY = "Consolas"


def font(size: int, weight: str = "normal") -> tuple[str, int, str]:
    """Шрифт интерфейса; weight — normal или bold."""
    return (FONT_FAMILY, size, weight)


def mono(size: int) -> tuple[str, int]:
    return (MONO_FAMILY, size)


def source_label(source: str) -> str:
    return SOURCE_LABELS.get(source, source or "—")


def source_palette(source: str) -> tuple[str, str]:
    """Фон и цвет текста бейджа источника."""
    return SOURCE_COLORS.get(source, (COLORS["border_light"], COLORS["text"]))


def clip(text: str, limit: int) -> str:
    """Обрезает длинный текст, сохраняя читаемый хвост в подсказке."""
    text = str(text)
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"

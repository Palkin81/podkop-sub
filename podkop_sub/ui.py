"""Тонкая обёртка над questionary: стрелки, галочки, «Назад»."""
from __future__ import annotations

import questionary
from questionary import Choice
from .i18n import _

BACK = "__back__"


def select(message: str, choices: list[tuple[str, object]],
           back_label: str | None = None):
    """Возвращает value выбранного пункта или BACK (в том числе по Ctrl+C)."""
    items = [Choice(title, value=value) for title, value in choices]
    if back_label is None:
        back_label = _("← Back")
    if back_label:
        items.append(Choice(back_label, value=BACK))
    result = questionary.select(message, choices=items,
                                instruction=_("(↑↓ and Enter)")).ask()
    return BACK if result is None else result


def checkbox(message: str, items: list[tuple[str, object, bool]]):
    """items: (заголовок, значение, отмечен). None — пользователь вернулся назад."""
    return questionary.checkbox(
        message,
        choices=[Choice(t, value=v, checked=c) for t, v, c in items],
        instruction=_("(space — mark, a — all, i — invert, Enter — next, Ctrl+C — back)"),
    ).ask()


def text(message: str, default: str = ""):
    return questionary.text(message, default=default).ask()


def confirm(message: str, default: bool = False):
    return questionary.confirm(message, default=default).ask()


def pause() -> None:
    try:
        input(_("\nPress Enter to continue…"))
    except (EOFError, KeyboardInterrupt):
        pass

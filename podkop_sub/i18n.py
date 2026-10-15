"""Языки интерфейса. Ключи в коде — английские фразы, русский перевод — в словаре RU.

Если перевода нет, показывается английский ключ. Выбор языка хранится в
config.toml → [ui] language и меняется в мастере и в Настройках.
"""
from __future__ import annotations

from .i18n_ru import RU

LANGUAGES = {"en": "English", "ru": "Русский"}
DEFAULT_LANGUAGE = "ru"
_current = DEFAULT_LANGUAGE


def set_language(lang: str) -> None:
    global _current
    _current = lang if lang in LANGUAGES else DEFAULT_LANGUAGE


def get_language() -> str:
    return _current


def tr(text: str, **kw) -> str:
    out = RU.get(text, text) if _current == "ru" else text
    return out.format(**kw) if kw else out


_ = tr

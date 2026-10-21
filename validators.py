"""Проверка и нормализация текста для поиска в Вавилонской библиотеке."""
from __future__ import annotations

import re
from dataclasses import dataclass

# Лимит со страницы поиска: «Enter up to 3200 characters».
MAX_LENGTH = 3200

# Библиотека содержит только строчные a–z, пробел, запятую и точку.
ALLOWED_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz ,.")

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    "…": "...",
}

_CYRILLIC_RE = re.compile(r"[а-яё]", re.IGNORECASE)


@dataclass(frozen=True)
class Validation:
    text: str                      # нормализованный текст (нижний регистр, схлопнутые пробелы)
    unsupported: tuple[str, ...]   # уникальные неподдерживаемые символы в порядке появления
    too_long: bool

    @property
    def ok(self) -> bool:
        return bool(self.text) and not self.unsupported and not self.too_long


def normalize(raw: str) -> str:
    """Нижний регистр, любые пробельные символы (включая переводы строк) -> один пробел."""
    return re.sub(r"\s+", " ", raw.lower()).strip()


def find_unsupported(text: str) -> tuple[str, ...]:
    seen: list[str] = []
    for ch in text:
        if ch not in ALLOWED_CHARS and ch not in seen:
            seen.append(ch)
    return tuple(seen)


def has_cyrillic(text: str) -> bool:
    return bool(_CYRILLIC_RE.search(text))


def transliterate(text: str) -> str:
    """Русский -> латиница (упрощённая транслитерация). Остальные символы не трогает."""
    out = "".join(_TRANSLIT.get(ch, ch) for ch in text.lower())
    return normalize(out)


def validate(raw: str) -> Validation:
    text = normalize(raw)
    return Validation(
        text=text,
        unsupported=find_unsupported(text),
        too_long=len(text) > MAX_LENGTH,
    )

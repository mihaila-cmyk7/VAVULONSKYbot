"""Сборка сообщений и клавиатур. Всё, что пришло с сайта или от пользователя, экранируется."""
from __future__ import annotations

from html import escape
from typing import Iterable, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from babel_client import MODE_ANY, MODE_EXACT, SearchResult
from validators import MAX_LENGTH

SNIPPET_LEN = 300
QUERY_PREVIEW_LEN = 200

MODE_TITLES = {
    MODE_EXACT: "точное совпадение",
    MODE_ANY: "внутри страницы",
}


def _h(value: object) -> str:
    return escape(str(value), quote=False)


def welcome_text() -> str:
    return (
        "👋 Привет! Я ищу слова и фразы в Вавилонской библиотеке "
        "(libraryofbabel.info) и показываю, где именно они нашлись.\n\n"
        "Просто отправьте текст сообщением или командой /find &lt;текст&gt;.\n"
        "Библиотека знает только строчные латинские буквы a–z, пробел, запятую и точку. "
        "Заглавные я переведу в строчные сам, а русский текст могу транслитерировать.\n\n"
        "Правила и примеры: /help"
    )


def help_text() -> str:
    return (
        "<b>Что можно искать</b>\n"
        "Строчные латинские буквы a–z, пробел, запятая и точка. "
        f"Максимальная длина запроса: {MAX_LENGTH} символов.\n\n"
        "<b>Что я сделаю сам</b>\n"
        "• приведу текст к нижнему регистру;\n"
        "• схлопну лишние пробелы и переводы строк;\n"
        "• если останутся неподдерживаемые символы (цифры, знаки, кириллица), скажу, какие именно, "
        "и предложу транслитерацию русского текста.\n\n"
        "<b>Примеры</b>\n"
        "<code>hello world</code>\n"
        "<code>/find the library is infinite.</code>\n"
        "<code>privet, mir</code>\n\n"
        "<b>Режимы</b>\n"
        "• <i>точное совпадение</i>: страница, на которой ваш текст стоит как есть;\n"
        "• <i>внутри страницы</i>: ваш текст внутри страницы среди остального текста.\n"
        "Режим переключается кнопкой под результатом.\n\n"
        "Запросы ограничены: не чаще одного раза в 3 секунды."
    )


def usage_text() -> str:
    return "Отправьте текст для поиска, например: <code>hello world</code>"


def too_long_message(length: int) -> str:
    return (
        f"Запрос слишком длинный: {length} симв. Лимит сайта: {MAX_LENGTH}. "
        "Сократите текст и отправьте снова."
    )


def unsupported_message(chars: Iterable[str], cyrillic: bool) -> str:
    shown = ", ".join(f"«{_h(c)}»" for c in list(chars)[:30])
    text = (
        "В тексте есть символы, которых нет в библиотеке: " + shown + ".\n"
        "Доступны только a–z, пробел, запятая и точка."
    )
    if cyrillic:
        text += "\n\nМогу транслитерировать русский текст в латиницу и искать его."
    return text


def after_translit_unsupported_message(chars: Iterable[str], translit: str) -> str:
    shown = ", ".join(f"«{_h(c)}»" for c in list(chars)[:30])
    return (
        "После транслитерации остались неподдерживаемые символы: " + shown + ".\n"
        "Уберите их или замените (например, цифры словами) и отправьте запрос заново."
    )


def site_unavailable() -> str:
    return "Сайт libraryofbabel.info сейчас недоступен. Попробуйте чуть позже."


def parse_error() -> str:
    return (
        "Сайт ответил в неожиданном формате, и я не смог разобрать результат. "
        "Возможно, разметка изменилась. Попробуйте позже."
    )


def generic_error() -> str:
    return "Что-то пошло не так. Попробуйте ещё раз чуть позже."


def rate_limited(wait: float) -> str:
    return f"Слишком часто. Подождите {max(1, round(wait))} с."


def make_snippet(text: str, start: Optional[int], length: int) -> str:
    """Фрагмент около SNIPPET_LEN символов; найденная часть в <b>."""
    if start is None:
        head = text[:SNIPPET_LEN]
        return _h(head) + ("…" if len(text) > SNIPPET_LEN else "")

    found_len = min(length, SNIPPET_LEN)
    pad = max(0, (SNIPPET_LEN - found_len) // 2)
    s = max(0, start - pad)
    e = min(len(text), start + found_len + pad)
    before = text[s:start]
    found = text[start:start + found_len]
    after = text[start + found_len:e]
    return (
        ("…" if s > 0 else "")
        + _h(before)
        + "<b>" + _h(found) + "</b>"
        + ("…" if found_len < length else "")
        + _h(after)
        + ("…" if e < len(text) else "")
    )


def format_result(result: SearchResult) -> str:
    loc = result.location
    q = result.query
    q_shown = q if len(q) <= QUERY_PREVIEW_LEN else q[:QUERY_PREVIEW_LEN] + "…"
    snippet = make_snippet(result.page_text, result.match_start, len(result.query))
    note = ""
    if result.match_start is None:
        note = "\n<i>Точное место текста на странице определить не удалось, показано начало страницы.</i>"
    url = escape(result.url, quote=True)
    return (
        f"🔎 Запрос: {_h(q_shown)}\n"
        f"Режим: {MODE_TITLES.get(result.mode, result.mode)}\n\n"
        "📍 Расположение:\n"
        f"Гексагон: {_h(loc.hexagon)}\n"
        f"Стена: {_h(loc.wall)}\n"
        f"Полка: {_h(loc.shelf)}\n"
        f"Том: {_h(loc.volume)}\n"
        f"Страница: {_h(loc.page)}\n\n"
        f"📖 {snippet}{note}\n\n"
        f'🔗 <a href="{url}">{url}</a>'
    )


def result_keyboard(url: str, token: str, mode: str) -> InlineKeyboardMarkup:
    if mode == MODE_EXACT:
        toggle = InlineKeyboardButton("Искать внутри страницы", callback_data=f"m:{token}:{MODE_ANY}")
    else:
        toggle = InlineKeyboardButton("Искать как точное совпадение", callback_data=f"m:{token}:{MODE_EXACT}")
    rows = [
        [InlineKeyboardButton("Открыть на сайте", url=url)],
        [toggle],
    ]
    if mode == MODE_ANY:
        # В режиме «внутри страницы» расположение может отличаться от запуска к запуску.
        rows.append([InlineKeyboardButton("Ещё один результат", callback_data=f"r:{token}")])
    return InlineKeyboardMarkup(rows)


def translit_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Транслитерировать и искать", callback_data=f"tr:{token}")]]
    )

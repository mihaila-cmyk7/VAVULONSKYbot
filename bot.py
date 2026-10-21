"""Telegram-бот для поиска текста в Вавилонской библиотеке (libraryofbabel.info)."""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections import OrderedDict
from logging.handlers import RotatingFileHandler
from typing import Optional

from dotenv import load_dotenv
from telegram import InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import formatters as fmt
import validators
from babel_client import (
    MODE_ANY,
    MODE_EXACT,
    BabelClient,
    BabelError,
    BabelParseError,
    BabelUnavailable,
)

log = logging.getLogger("babel-bot")

DEFAULT_MODE = MODE_EXACT
RATE_LIMIT_SECONDS = 3.0
MAX_PARALLEL_SEARCHES = 3


# --------------------------------------------------------------- инфраструктура
def setup_logging() -> None:
    log_file = os.getenv("LOG_FILE", "babel_bot.log")
    handler = RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    root.addHandler(console)
    # Не светим токен бота в логах: httpx пишет полный URL запроса.
    logging.getLogger("httpx").setLevel(logging.WARNING)


class RateLimiter:
    """Не более одного запроса на пользователя за interval секунд."""

    def __init__(self, interval: float) -> None:
        self.interval = interval
        self._last: dict[int, float] = {}

    def check(self, user_id: int) -> float:
        """0, если можно; иначе сколько секунд ждать."""
        now = time.monotonic()
        last = self._last.get(user_id)
        if last is not None and now - last < self.interval:
            return self.interval - (now - last)
        self._last[user_id] = now
        if len(self._last) > 10_000:
            cutoff = now - self.interval
            self._last = {u: t for u, t in self._last.items() if t > cutoff}
        return 0.0


class TokenStore:
    """Короткие токены для callback_data (в нём максимум 64 байта)."""

    def __init__(self, limit: int = 1000) -> None:
        self._data: OrderedDict[str, dict] = OrderedDict()
        self._limit = limit

    def put(self, payload: dict) -> str:
        token = uuid.uuid4().hex[:10]
        self._data[token] = payload
        while len(self._data) > self._limit:
            self._data.popitem(last=False)
        return token

    def get(self, token: str) -> Optional[dict]:
        return self._data.get(token)


async def post_init(app: Application) -> None:
    app.bot_data["client"] = BabelClient()
    app.bot_data["tokens"] = TokenStore()
    app.bot_data["limiter"] = RateLimiter(RATE_LIMIT_SECONDS)
    app.bot_data["sem"] = asyncio.Semaphore(MAX_PARALLEL_SEARCHES)


async def post_shutdown(app: Application) -> None:
    client: BabelClient = app.bot_data.get("client")
    if client:
        await client.aclose()


# ------------------------------------------------------------------ ядро поиска
async def do_search(
    context: ContextTypes.DEFAULT_TYPE, query: str, mode: str, use_cache: bool = True
) -> tuple[str, Optional[InlineKeyboardMarkup]]:
    """Выполняет поиск и возвращает (html-текст, клавиатура). Ошибки превращает в сообщения."""
    bd = context.application.bot_data
    try:
        async with bd["sem"]:
            result = await bd["client"].search(query, mode, use_cache=use_cache)
    except BabelUnavailable:
        log.exception("Сайт недоступен")
        return fmt.site_unavailable(), None
    except BabelParseError:
        log.exception("Не удалось разобрать ответ сайта (запрос=%r, режим=%s)", query[:80], mode)
        return fmt.parse_error(), None
    except BabelError:
        log.exception("Ошибка клиента")
        return fmt.generic_error(), None
    except Exception:
        log.exception("Неожиданная ошибка поиска")
        return fmt.generic_error(), None

    token = bd["tokens"].put({"query": query, "mode": mode})
    return fmt.format_result(result), fmt.result_keyboard(result.url, token, mode)


async def _safe_edit(edit_coro) -> None:
    try:
        await edit_coro
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            log.warning("Не удалось отредактировать сообщение: %s", exc)
    except TelegramError:
        log.exception("Ошибка Telegram при редактировании сообщения")


async def handle_query(update: Update, context: ContextTypes.DEFAULT_TYPE, raw_text: str) -> None:
    msg = update.effective_message
    user = update.effective_user
    if msg is None or user is None:
        return

    v = validators.validate(raw_text)
    if not v.text:
        await msg.reply_text(fmt.usage_text(), parse_mode=ParseMode.HTML)
        return
    if v.too_long:
        await msg.reply_text(fmt.too_long_message(len(v.text)))
        return
    if v.unsupported:
        cyr = validators.has_cyrillic(v.text)
        kb = None
        if cyr:
            token = context.application.bot_data["tokens"].put({"raw": v.text})
            kb = fmt.translit_keyboard(token)
        await msg.reply_text(
            fmt.unsupported_message(v.unsupported, cyr), parse_mode=ParseMode.HTML, reply_markup=kb
        )
        return

    wait = context.application.bot_data["limiter"].check(user.id)
    if wait:
        await msg.reply_text(fmt.rate_limited(wait))
        return

    status = await msg.reply_text("🔎 Ищу…")
    text, kb = await do_search(context, v.text, DEFAULT_MODE)
    await _safe_edit(
        status.edit_text(
            text, parse_mode=ParseMode.HTML, reply_markup=kb, disable_web_page_preview=True
        )
    )


# ------------------------------------------------------------------- команды
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(fmt.welcome_text(), parse_mode=ParseMode.HTML)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(fmt.help_text(), parse_mode=ParseMode.HTML)


async def cmd_find(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await handle_query(update, context, " ".join(context.args or []))


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message and update.effective_message.text:
        await handle_query(update, context, update.effective_message.text)


# ------------------------------------------------------------------- кнопки
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q is None or q.data is None:
        return
    bd = context.application.bot_data
    parts = q.data.split(":")
    action = parts[0]
    payload = bd["tokens"].get(parts[1]) if len(parts) > 1 else None
    if payload is None:
        await q.answer("Кнопка устарела, отправьте запрос заново.", show_alert=True)
        return

    wait = bd["limiter"].check(q.from_user.id)
    if wait:
        await q.answer(fmt.rate_limited(wait))
        return
    await q.answer()

    if action == "tr":
        translit = validators.transliterate(payload["raw"])
        v = validators.validate(translit)
        if not v.text:
            await _safe_edit(q.edit_message_text(fmt.usage_text(), parse_mode=ParseMode.HTML))
            return
        if v.too_long:
            await _safe_edit(q.edit_message_text(fmt.too_long_message(len(v.text))))
            return
        if v.unsupported:
            await _safe_edit(
                q.edit_message_text(
                    fmt.after_translit_unsupported_message(v.unsupported, v.text),
                    parse_mode=ParseMode.HTML,
                )
            )
            return
        query, mode, use_cache = v.text, DEFAULT_MODE, True
    elif action == "m" and len(parts) == 3 and parts[2] in (MODE_EXACT, MODE_ANY):
        query, mode, use_cache = payload["query"], parts[2], True
    elif action == "r":
        query, mode, use_cache = payload["query"], payload["mode"], False
    else:
        return

    await _safe_edit(q.edit_message_text("🔎 Ищу…"))
    text, kb = await do_search(context, query, mode, use_cache=use_cache)
    await _safe_edit(
        q.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=kb, disable_web_page_preview=True
        )
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Необработанная ошибка", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(fmt.generic_error())
        except TelegramError:
            pass


def main() -> None:
    load_dotenv()
    setup_logging()
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("Не задан BOT_TOKEN. Скопируйте .env.example в .env и впишите токен.")

    app = (
        ApplicationBuilder()
        .token(token)
        .concurrent_updates(True)  # иначе один медленный поиск блокирует всех
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("find", cmd_find))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    log.info("Бот запущен")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

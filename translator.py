"""Перевод русского текста на английский для поиска в Вавилонской библиотеке.

Библиотека знает только латиницу, поэтому русский запрос нужно именно переводить,
а не транслитерировать: «привет» -> «hello», а не «privet» (транслитерация — это
запись русских слов латиницей, а не английские слова).

Модуль по очереди обращается к бесплатным сервисам перевода и кэширует результат:
Яндекс.Переводчик (если задан YANDEX_TRANSLATE_KEY), Google, MyMemory.
Если все сервисы недоступны, возвращается понятная ошибка, а не искажённый текст.
"""
from __future__ import annotations

import html
import logging
import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional

import httpx

import validators

log = logging.getLogger(__name__)

# Основной адрес Google — тот, которым пользуется словарь Chrome: он отвечает заметно
# стабильнее, чем публичный translate_a/single (тот часто отдаёт 429).
GOOGLE_URL = "https://clients5.google.com/translate_a/t"
GOOGLE_SINGLE_URL = "https://translate.googleapis.com/translate_a/single"
MYMEMORY_URL = "https://api.mymemory.translated.net/get"
YANDEX_URL = "https://translate.yandex.net/api/v1.5/tr.json/translate"
YANDEX_CLOUD_URL = "https://translate.api.cloud.yandex.net/translate/v2/translate"

# Лимит запроса в библиотеке; больше переводить нет смысла.
MAX_INPUT = 3200

# Провайдер режет текст на куски не длиннее указанного числа символов.
# MyMemory принимает не больше 500 байт, а кириллица в UTF-8 занимает 2 байта.
_PROVIDER_LIMITS = {"google": 1200, "yandex": 9000, "mymemory": 220}

_LATIN_RE = re.compile(r"[A-Za-z]")
_CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
_DIGIT_RE = re.compile(r"\d")

VALID_PROVIDERS = ("yandex", "google", "mymemory")

PROVIDER_TITLES = {
    "yandex": "Яндекс",
    "google": "Google",
    "mymemory": "MyMemory",
}

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class TranslationError(Exception):
    """Перевод не удался: сервисы недоступны или ответ не удалось разобрать."""


@dataclass(frozen=True)
class Translation:
    text: str      # английский текст
    provider: str  # "yandex" | "google" | "mymemory"

    @property
    def provider_title(self) -> str:
        return PROVIDER_TITLES.get(self.provider, self.provider)


def split_chunks(text: str, limit: int) -> list[str]:
    """Делит текст на куски не длиннее limit символов, не разрывая слова."""
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for word in text.split(" "):
        # Слово длиннее лимита (в русском почти невозможно) режем принудительно.
        while len(word) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(word[:limit])
            word = word[limit:]
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= limit:
            current += " " + word
        else:
            chunks.append(current)
            current = word
    if current:
        chunks.append(current)
    return chunks


def _check_response(resp: object, title: str) -> None:
    status = getattr(resp, "status_code", 0)
    if status != 200:
        raise TranslationError(f"{title}: HTTP {status}")


def _looks_broken(translated: str) -> bool:
    """Ответ, который не является английским текстом, — ошибка любого сервиса."""
    return (
        not translated.strip()
        or not _LATIN_RE.search(translated)
        or bool(_CYRILLIC_RE.search(translated))
    )


def _looks_mangled(source: str, translated: str) -> bool:
    """Строгая проверка для MyMemory: он иногда выдаёт транслитерацию или мусор.

    Например, «я тебя люблю» -> «r te6r» вместо «I love you». Google и Яндекс
    так не ошибаются, поэтому к ним эта проверка не применяется: иначе она
    отбрасывает нормальные английские формы имён и заимствований («Иван» ->
    «Ivan», «суши» -> «sushi»).
    """
    if _CYRILLIC_RE.search(translated):
        return True
    if not _LATIN_RE.search(translated):
        return True
    # Цифры, которых не было в исходнике, — типичный след транслитерации.
    if not _DIGIT_RE.search(source) and _DIGIT_RE.search(translated):
        return True
    low = translated.strip().lower()
    if low == source.strip().lower():
        return True
    translit = validators.transliterate(source).strip().lower()
    if translit and low == translit:
        return True
    return False


class Translator:
    """Асинхронный переводчик русский -> английский с кэшем и запасными сервисами."""

    def __init__(
        self,
        *,
        source: Optional[str] = None,
        yandex_key: Optional[str] = None,
        timeout: float = 8.0,
        cache_size: int = 256,
        cache_ttl: float = 3600.0,
        http: Optional[httpx.AsyncClient] = None,
    ) -> None:
        raw_source = source if source is not None else os.getenv("TRANSLATE_SOURCE", "auto")
        self._auto = (raw_source or "").strip().lower() in ("", "auto")
        self._order = self._parse_order(raw_source)
        self.yandex_key = (
            yandex_key if yandex_key is not None else os.getenv("YANDEX_TRANSLATE_KEY", "")
        ).strip()

        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
        )
        self._cache: OrderedDict[str, tuple[float, Translation]] = OrderedDict()
        self._cache_size = cache_size
        self._cache_ttl = cache_ttl

    @staticmethod
    def _parse_order(value: str) -> list[str]:
        value = (value or "").strip().lower()
        order: list[str] = []
        for part in value.split(","):
            name = part.strip()
            if name in VALID_PROVIDERS and name not in order:
                order.append(name)
        return order or ["google", "mymemory"]

    def _providers(self) -> list[str]:
        order = list(self._order)
        # В России Яндекс обычно доступнее остальных, поэтому при наличии ключа он первый.
        if self._auto and self.yandex_key and "yandex" not in order:
            order.insert(0, "yandex")
        return order

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def translate(self, text: str, use_cache: bool = True) -> Translation:
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            raise TranslationError("Пустой текст")
        if len(text) > MAX_INPUT:
            raise TranslationError(f"Текст длиннее {MAX_INPUT} символов")

        if use_cache:
            hit = self._cache.get(text)
            if hit is not None and time.monotonic() - hit[0] < self._cache_ttl:
                self._cache.move_to_end(text)
                return hit[1]

        errors: list[str] = []
        for name in self._providers():
            try:
                translated = await self._translate_with(name, text)
            except Exception as exc:  # httpx-ошибки, таймауты, кривой ответ
                errors.append(f"{name}: {exc}")
                log.warning("Перевод через %s не удался: %s", name, exc)
                continue
            translated = re.sub(r"\s+", " ", translated).strip()
            if not translated:
                errors.append(f"{name}: пустой ответ")
                continue
            if _looks_broken(translated):
                errors.append(f"{name}: ответ не на английском")
                log.warning("Перевод через %s отброшен: %r", name, translated[:100])
                continue
            # Google и Яндекс переводят надёжно; строгая проверка нужна только
            # для MyMemory, который иногда выдаёт транслитерацию вместо перевода.
            if name == "mymemory" and _looks_mangled(text, translated):
                errors.append(f"{name}: ответ похож на транслитерацию или мусор")
                log.warning("Перевод через %s отброшен: %r", name, translated[:100])
                continue
            result = Translation(translated, name)
            self._remember(text, result)
            log.info("Перевод выполнен через %s (%d симв.)", name, len(text))
            return result

        raise TranslationError("; ".join(errors) or "нет доступных сервисов перевода")

    async def _translate_with(self, name: str, text: str) -> str:
        chunks = split_chunks(text, _PROVIDER_LIMITS[name])
        parts: list[str] = []
        for chunk in chunks:
            if name == "google":
                parts.append(await self._google(chunk))
            elif name == "yandex":
                parts.append(await self._yandex(chunk))
            else:
                parts.append(await self._mymemory(chunk))
        return " ".join(part.strip() for part in parts if part.strip())

    # ---------------------------------------------------------------- сервисы
    async def _google(self, text: str) -> str:
        try:
            return await self._google_dict(text)
        except TranslationError as exc:
            # Запасной адрес Google понимает только короткие запросы.
            if len(text) > 1500:
                raise
            log.info("Основной адрес Google не ответил (%s), пробую запасной", exc)
            return await self._google_single(text)

    async def _google_dict(self, text: str) -> str:
        params = {"client": "dict-chrome-ex", "sl": "ru", "tl": "en", "q": text}
        resp = await self._http.get(GOOGLE_URL, params=params)
        _check_response(resp, "Google")
        try:
            data = resp.json()
            if isinstance(data, list):
                out = " ".join(part for part in data if isinstance(part, str))
            elif isinstance(data, dict):
                out = str(data.get("translation") or "")
            else:
                out = ""
        except (ValueError, TypeError) as exc:
            raise TranslationError("Google вернул неожиданный ответ") from exc
        if not out.strip():
            raise TranslationError("Google вернул пустой перевод")
        return out

    async def _google_single(self, text: str) -> str:
        params = {"client": "gtx", "sl": "ru", "tl": "en", "dt": "t", "q": text}
        resp = await self._http.get(GOOGLE_SINGLE_URL, params=params)
        _check_response(resp, "Google")
        try:
            segments = resp.json()[0]
            out = "".join(str(seg[0]) for seg in segments if seg and seg[0])
        except (ValueError, TypeError, IndexError, KeyError) as exc:
            raise TranslationError("Google вернул неожиданный ответ") from exc
        if not out.strip():
            raise TranslationError("Google вернул пустой перевод")
        return out

    async def _yandex(self, text: str) -> str:
        if not self.yandex_key:
            raise TranslationError("не задан YANDEX_TRANSLATE_KEY")
        # Ключи Яндекс.Облака начинаются с AQVN и работают только с API v2;
        # старые ключи вида trnsl.1.1... — с API v1.5.
        if self.yandex_key.startswith("trnsl."):
            return await self._yandex_legacy(text)
        return await self._yandex_cloud(text)

    async def _yandex_cloud(self, text: str) -> str:
        resp = await self._http.post(
            YANDEX_CLOUD_URL,
            headers={"Authorization": f"Api-Key {self.yandex_key}"},
            json={
                "sourceLanguageCode": "ru",
                "targetLanguageCode": "en",
                "texts": [text],
            },
        )
        _check_response(resp, "Яндекс")
        try:
            data = resp.json()
        except ValueError as exc:
            raise TranslationError("Яндекс вернул не JSON") from exc
        translations = data.get("translations") or []
        out = "".join(str(item.get("text") or "") for item in translations)
        if not out.strip():
            raise TranslationError("Яндекс вернул пустой перевод")
        return out

    async def _yandex_legacy(self, text: str) -> str:
        resp = await self._http.post(
            YANDEX_URL,
            params={"key": self.yandex_key, "lang": "ru-en", "format": "plain"},
            data={"text": text},
        )
        _check_response(resp, "Яндекс")
        try:
            data = resp.json()
        except ValueError as exc:
            raise TranslationError("Яндекс вернул не JSON") from exc
        if str(data.get("code")) != "200":
            raise TranslationError(f"Яндекс: код {data.get('code')}")
        out = "".join(str(part) for part in (data.get("text") or []))
        if not out.strip():
            raise TranslationError("Яндекс вернул пустой перевод")
        return out

    async def _mymemory(self, text: str) -> str:
        resp = await self._http.get(MYMEMORY_URL, params={"q": text, "langpair": "ru|en"})
        _check_response(resp, "MyMemory")
        try:
            data = resp.json()
        except ValueError as exc:
            raise TranslationError("MyMemory вернул не JSON") from exc
        status = str(data.get("responseStatus"))
        if status != "200":
            raise TranslationError(f"MyMemory: статус {status}")
        out = html.unescape(str((data.get("responseData") or {}).get("translatedText") or ""))
        if not out.strip() or "MYMEMORY WARNING" in out.upper():
            raise TranslationError("MyMemory вернул пустой перевод")
        return out

    # ------------------------------------------------------------------ кэш
    def _remember(self, text: str, result: Translation) -> None:
        self._cache[text] = (time.monotonic(), result)
        self._cache.move_to_end(text)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)

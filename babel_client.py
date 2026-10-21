"""Клиент libraryofbabel.info: отправка поиска и разбор ответа.

Важно: сайт отдаёт форму поиска через search.html, а точные имена полей и значения режимов
клиент пытается определить на лету (_discover_form). Если не получилось, используются значения
по умолчанию из DEFAULT_* ниже. Если сайт поменяет разметку, правьте только этот файл.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

import local_library

log = logging.getLogger(__name__)

BASE_URL = "https://libraryofbabel.info"
SEARCH_PAGE = f"{BASE_URL}/search.html"

MODE_EXACT = "exact"   # точное совпадение на странице
MODE_ANY = "any"       # текст внутри страницы среди «случайного» текста

# --- Значения по умолчанию (ПРЕДПОЛОЖЕНИЯ, проверьте вручную, см. README) ---
DEFAULT_ACTION = "search.cgi"
DEFAULT_METHOD = "POST"
DEFAULT_TEXT_FIELD = "find"
DEFAULT_MODE_FIELD = "method"
DEFAULT_MODE_VALUES = {MODE_EXACT: "x", MODE_ANY: "m"}
DEFAULT_EXTRA = {"btnSubmit": "Search"}

# book.cgi?<hex>-w<стена>-s<полка>-v<том>:<страница>
LOCATION_RE = re.compile(
    r"(?:book|browse)\.cgi\?([0-9a-z]+)-w(\d+)-s(\d+)-v(\d+)(?::(\d+))?", re.IGNORECASE
)
PAGE_TEXT_IDS = ("textblock", "page", "text", "bookcontent")

# Сайт может отвечать 403 на запросы с «ботовым» User-Agent, поэтому представляемся обычным браузером.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
BROWSER_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"{BASE_URL}/search.html",
}


class BabelError(Exception):
    """Базовая ошибка клиента."""


class BabelUnavailable(BabelError):
    """Сайт недоступен или стабильно отвечает ошибкой."""


class BabelParseError(BabelError):
    """Сайт вернул неожиданный HTML."""


@dataclass(frozen=True)
class Location:
    hexagon: str
    wall: str
    shelf: str
    volume: str
    page: str

    @property
    def url(self) -> str:
        return (
            f"{BASE_URL}/book.cgi?{self.hexagon}-w{self.wall}-s{self.shelf}"
            f"-v{self.volume}:{self.page}"
        )


@dataclass(frozen=True)
class SearchResult:
    query: str
    mode: str
    location: Location
    page_text: str
    match_start: Optional[int]   # индекс найденного текста в page_text или None
    source: str = "site"         # "site" (libraryofbabel.info) или "local" (встроенная библиотека)

    @property
    def url(self) -> Optional[str]:
        """Ссылка на страницу на сайте; для локального результата её не существует."""
        return self.location.url if self.source == "site" else None


@dataclass
class FormSpec:
    action: str = DEFAULT_ACTION
    method: str = DEFAULT_METHOD
    text_field: str = DEFAULT_TEXT_FIELD
    mode_field: Optional[str] = DEFAULT_MODE_FIELD
    mode_values: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_MODE_VALUES))
    extra: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_EXTRA))


def _radio_label(form, radio) -> str:
    parts = [str(radio.get("value", ""))]
    rid = radio.get("id")
    if rid:
        lbl = form.find("label", attrs={"for": rid})
        if lbl:
            parts.append(lbl.get_text(" "))
    sib = radio.next_sibling
    if isinstance(sib, str):
        parts.append(sib)
    elif sib is not None and hasattr(sib, "get_text"):
        parts.append(sib.get_text(" "))
    return " ".join(parts).lower()


class BabelClient:
    def __init__(
        self,
        timeout: float = 12.0,
        retries: int = 3,
        cache_size: int = 256,
        cache_ttl: float = 3600.0,
    ) -> None:
        self.retries = retries
        self._http = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers=BROWSER_HEADERS,
        )
        self._cache: OrderedDict[tuple[str, str], tuple[float, SearchResult]] = OrderedDict()
        self._cache_size = cache_size
        self._cache_ttl = cache_ttl
        self._form: Optional[FormSpec] = None
        self._form_ts = 0.0
        # BABEL_SOURCE: auto (сайт, при сбое локально) | site (только сайт) | local (только локально)
        self.source_mode = os.getenv("BABEL_SOURCE", "auto").strip().lower()
        self._site_down_until = 0.0

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ HTTP
    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        last_exc: Optional[BaseException] = None
        for attempt in range(self.retries):
            try:
                resp = await self._http.request(method, url, **kwargs)
                if resp.status_code == 429 or resp.status_code >= 500:
                    last_exc = BabelUnavailable(f"HTTP {resp.status_code}")
                elif resp.status_code >= 400:
                    raise BabelError(f"HTTP {resp.status_code}")
                else:
                    return resp
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
            log.warning("Запрос %s %s: попытка %d/%d не удалась: %r",
                        method, url, attempt + 1, self.retries, last_exc)
            if attempt < self.retries - 1:
                await asyncio.sleep(0.7 * 2 ** attempt)
        raise BabelUnavailable(f"Сайт недоступен: {last_exc!r}") from last_exc

    # ------------------------------------------------------------- форма
    async def _get_form(self) -> FormSpec:
        if self._form is not None and time.monotonic() - self._form_ts < 3600:
            return self._form
        spec = FormSpec()
        try:
            resp = await self._request("GET", SEARCH_PAGE)
            spec = self._parse_form(resp.text)
        except BabelError as exc:
            log.warning("Не удалось прочитать форму поиска, использую значения по умолчанию: %r", exc)
        self._form, self._form_ts = spec, time.monotonic()
        return spec

    @staticmethod
    def _parse_form(html_text: str) -> FormSpec:
        spec = FormSpec()
        soup = BeautifulSoup(html_text, "html.parser")
        form = soup.find("form")
        if form is None:
            return spec
        if form.get("action"):
            spec.action = form["action"]
        if form.get("method"):
            spec.method = form["method"].upper()
        field_el = form.find("textarea") or form.find("input", attrs={"type": "text"})
        if field_el is not None and field_el.get("name"):
            spec.text_field = field_el["name"]

        extra: dict[str, str] = {}
        for inp in form.find_all("input"):
            itype = (inp.get("type") or "text").lower()
            name = inp.get("name")
            if not name:
                continue
            if itype in ("hidden", "submit"):
                extra[name] = inp.get("value", "")
        if extra:
            spec.extra = extra

        found: dict[str, str] = {}
        mode_field = None
        for radio in form.find_all("input", attrs={"type": "radio"}):
            label = _radio_label(form, radio)
            value = radio.get("value", "")
            if "exact" in label:
                found[MODE_EXACT] = value
                mode_field = radio.get("name")
            elif "any" in label:
                found[MODE_ANY] = value
                mode_field = radio.get("name")
        if mode_field and len(found) == 2:
            spec.mode_field, spec.mode_values = mode_field, found
        return spec

    # -------------------------------------------------------------- поиск
    async def search(self, query: str, mode: str = MODE_EXACT, use_cache: bool = True) -> SearchResult:
        key = (query, mode)
        if use_cache:
            hit = self._cache.get(key)
            if hit and time.monotonic() - hit[0] < self._cache_ttl:
                self._cache.move_to_end(key)
                return hit[1]

        result: Optional[SearchResult] = None
        site_allowed = self.source_mode != "local" and (
            self.source_mode == "site" or time.monotonic() >= self._site_down_until
        )
        if site_allowed:
            try:
                result = await self._search_site(query, mode)
            except BabelError as exc:
                if self.source_mode == "site":
                    raise
                # Не ждём таймауты на каждом запросе: 5 минут сразу идём в локальную библиотеку.
                self._site_down_until = time.monotonic() + 300
                log.warning("Сайт не ответил (%r), перехожу на локальную библиотеку на 5 минут", exc)
        if result is None:
            result = self._search_local(query, mode, fresh=not use_cache)

        self._cache[key] = (time.monotonic(), result)
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return result

    async def _search_site(self, query: str, mode: str) -> SearchResult:
        spec = await self._get_form()
        data = {spec.text_field: query, **spec.extra}
        if spec.mode_field:
            data[spec.mode_field] = spec.mode_values[mode]
        url = urljoin(BASE_URL + "/", spec.action)
        if spec.method == "GET":
            resp = await self._request("GET", url, params=data)
        else:
            resp = await self._request("POST", url, data=data)
        return self._parse_result(resp.text, str(resp.url), query, mode)

    @staticmethod
    def _search_local(query: str, mode: str, fresh: bool = False) -> SearchResult:
        loc, page, start = local_library.search(query, mode, fresh=fresh)
        return SearchResult(
            query=query,
            mode=mode,
            location=Location(loc.hexagon, str(loc.wall), str(loc.shelf), str(loc.volume), str(loc.page)),
            page_text=page,
            match_start=start,
            source="local",
        )

    # ------------------------------------------------------------- разбор
    @staticmethod
    def _extract_location(html_text: str, final_url: str, soup: BeautifulSoup) -> Location:
        candidates = [final_url]
        candidates += [a["href"] for a in soup.find_all("a", href=True)]
        candidates.append(html_text)
        for cand in candidates:
            for m in LOCATION_RE.finditer(cand):
                if m.group(5):
                    return Location(m.group(1).lower(), m.group(2), m.group(3), m.group(4), m.group(5))
        raise BabelParseError("В ответе сайта не найден адрес страницы")

    @staticmethod
    def _extract_page_text(soup: BeautifulSoup) -> str:
        el = None
        for pid in PAGE_TEXT_IDS:
            el = soup.find(id=pid)
            if el is not None:
                break
        if el is None:
            el = soup.find("pre")
        if el is None:
            raise BabelParseError("В ответе сайта не найден текст страницы")
        # Страница набрана строками фиксированной ширины: переводы строк это просто перенос.
        text = el.get_text().replace("\r", "").replace("\n", "")
        if not text.strip():
            raise BabelParseError("Текст страницы пустой")
        return text

    @classmethod
    def _parse_result(cls, html_text: str, final_url: str, query: str, mode: str) -> SearchResult:
        soup = BeautifulSoup(html_text, "html.parser")
        location = cls._extract_location(html_text, final_url, soup)
        page_text = cls._extract_page_text(soup)
        idx = page_text.find(query)
        return SearchResult(
            query=query,
            mode=mode,
            location=location,
            page_text=page_text,
            match_start=idx if idx >= 0 else None,
        )

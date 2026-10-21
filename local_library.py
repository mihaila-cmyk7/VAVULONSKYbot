"""Локальная «Вавилонская библиотека»: работает без сайта и без интернета.

ВАЖНО: это НЕ копия libraryofbabel.info. Адреса здесь свои, они не совпадут с адресами на сайте.
Зато у библиотеки те же правила: страница = 3200 символов из 29 знаков (a-z, пробел, запятая, точка),
4 стены, 5 полок, 32 тома, 410 страниц. Любой текст всегда находится, а адрес можно превратить
обратно в страницу (decode), то есть это настоящая обратимая «библиотека», а не выдумка.

Как это устроено: страница читается как число N в системе счисления по основанию 29.
Адрес = (A*N + C) mod 29^3200, где A взаимно просто с 29, значит отображение обратимо
(в обратную сторону умножаем на A^-1). Полученное число раскладывается на
гексагон / стену / полку / том / страницу.
"""
from __future__ import annotations

import hashlib
import random
import secrets
from dataclasses import dataclass

ALPHABET = "abcdefghijklmnopqrstuvwxyz ,."
BASE = len(ALPHABET)  # 29
PAGE_LEN = 3200
WALLS, SHELVES, VOLUMES, PAGES = 4, 5, 32, 410
PER_HEX = WALLS * SHELVES * VOLUMES * PAGES  # 262400
MODULUS = BASE ** PAGE_LEN

_IDX = {ch: i for i, ch in enumerate(ALPHABET)}
_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def _const(label: str) -> int:
    rng = random.Random(hashlib.sha256(label.encode()).digest())
    return rng.getrandbits(MODULUS.bit_length() + 64) % MODULUS


_A = _const("babel-bot:A")
if _A % BASE == 0:
    _A += 1
_C = _const("babel-bot:C")
_A_INV = pow(_A, -1, MODULUS)


@dataclass(frozen=True)
class LocalLocation:
    hexagon: str
    wall: int
    shelf: int
    volume: int
    page: int


def _to_base36(n: int) -> str:
    if n == 0:
        return "0"
    out = []
    while n:
        n, r = divmod(n, 36)
        out.append(_B36[r])
    return "".join(reversed(out))


def _page_to_int(page: str) -> int:
    n = 0
    for ch in page:
        n = n * BASE + _IDX[ch]
    return n


def _int_to_page(n: int) -> str:
    chars = []
    for _ in range(PAGE_LEN):
        n, r = divmod(n, BASE)
        chars.append(ALPHABET[r])
    return "".join(reversed(chars))


def encode(page: str) -> LocalLocation:
    """Страница (ровно 3200 допустимых символов) -> адрес."""
    if len(page) != PAGE_LEN or any(ch not in _IDX for ch in page):
        raise ValueError("Страница должна состоять ровно из 3200 допустимых символов")
    y = (_A * _page_to_int(page) + _C) % MODULUS
    y, p = divmod(y, PAGES)
    y, v = divmod(y, VOLUMES)
    y, s = divmod(y, SHELVES)
    h, w = divmod(y, WALLS)
    return LocalLocation(_to_base36(h), w + 1, s + 1, v + 1, p + 1)


def decode(loc: LocalLocation) -> str:
    """Адрес -> страница (обратная операция к encode)."""
    h = int(loc.hexagon, 36)
    y = ((((h * WALLS + loc.wall - 1) * SHELVES + loc.shelf - 1) * VOLUMES + loc.volume - 1)
         * PAGES + loc.page - 1)
    if y >= MODULUS:
        raise ValueError("Такого адреса в локальной библиотеке нет")
    return _int_to_page(((y - _C) * _A_INV) % MODULUS)


def build_page(query: str, mode: str, fresh: bool = False) -> tuple[str, int]:
    """Собирает страницу вокруг запроса. Возвращает (страница, индекс начала запроса).

    mode 'exact': запрос в начале страницы, остаток пробелами (как «точное совпадение»).
    иначе: запрос среди псевдослучайного текста; fresh=True даёт другой результат при каждом вызове.
    """
    if len(query) > PAGE_LEN:
        raise ValueError("Запрос длиннее страницы")
    if mode == "exact":
        return query + " " * (PAGE_LEN - len(query)), 0
    seed = secrets.token_bytes(16) if fresh else hashlib.sha256(("any:" + query).encode()).digest()
    rng = random.Random(seed)
    start = rng.randint(0, PAGE_LEN - len(query))
    fill = "".join(rng.choices(ALPHABET, k=PAGE_LEN))
    page = fill[:start] + query + fill[start + len(query):]
    return page, start


def search(query: str, mode: str, fresh: bool = False) -> tuple[LocalLocation, str, int]:
    page, start = build_page(query, mode, fresh)
    return encode(page), page, start

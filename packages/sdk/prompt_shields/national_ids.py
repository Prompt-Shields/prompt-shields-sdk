"""Checksum-validated detectors for Nordic national identity numbers.

Feeds the `national_id` category in `prompt_shields.pii`. Each detector pairs
a regex for the shape with a validator for the date and check digits, so that
phone numbers, order IDs and dates that merely look similar are rejected.

Covered:
    - Swedish personnummer and samordningsnummer (Luhn)
    - Norwegian fødselsnummer, D-number and H-number (two mod-11 check digits)
    - Danish CPR number (date validity; mod-11 when written without hyphen)
    - Finnish henkilötunnus / HETU (century marker and mod-31 check character)

Pure Python, no network, no dependencies. Detects presence only; matched
numbers are never returned or stored.
"""

import calendar
import re

# Left boundary: not preceded by a word char, '+' or '-' (so we never start
# matching in the middle of a longer number such as "+4712345678901").
_L = r"(?<![\w+-])"
# Right boundary: not followed by a word char, or by '-'/'.' plus a digit.
_R = r"(?!\w)(?![-.]\d)"


def _valid_date(year: int, month: int, day: int) -> bool:
    if not 1 <= month <= 12 or day < 1:
        return False
    return day <= calendar.monthrange(year, month)[1]


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


# ---------------------------------------------------------------------------
# Sweden — personnummer / samordningsnummer
#   [CC]YYMMDD[-+]NNNC — C is a Luhn check over the 10-digit YYMMDDNNNC.
#   Samordningsnummer: day + 60. '+' marks a person aged 100 or older.
#   A bare 10-digit run (no century, no separator) is not accepted: it is
#   too often a phone or order number that happens to pass Luhn.
# ---------------------------------------------------------------------------
_SE_RE = re.compile(_L + r"((?:18|19|20)?)(\d{2})(\d{2})(\d{2})([-+]?)(\d{3})(\d)" + _R)


def _valid_se(m: re.Match) -> bool:
    century, yy, mm, dd, sep, serial, check = m.groups()
    if not century and not sep:
        return False
    day = int(dd)
    if day > 60:
        day -= 60  # samordningsnummer
    # Without a century, pick a leap-compatible year (2000 + yy is a leap year
    # exactly when yy % 4 == 0, which also covers 1904..1996).
    year = int(century + yy) if century else 2000 + int(yy)
    if not _valid_date(year, int(mm), day):
        return False
    return _luhn_ok(yy + mm + dd + serial + check)


# ---------------------------------------------------------------------------
# Norway — fødselsnummer / D-number / H-number
#   DDMMYYIIIKK — K1, K2 are weighted mod-11 check digits.
#   D-number: day + 40. H-number: month + 40. Synthetic test numbers
#   (Skatteetaten Tenor): month + 80.
# ---------------------------------------------------------------------------
_NO_RE = re.compile(_L + r"(\d{6})[ ]?(\d{5})" + _R)
_NO_W1 = (3, 7, 6, 1, 8, 9, 4, 5, 2)
_NO_W2 = (5, 4, 3, 2, 7, 6, 5, 4, 3, 2)


def _mod11_check(digits: str, weights: tuple[int, ...]) -> int | None:
    rest = sum(int(d) * w for d, w in zip(digits, weights)) % 11
    k = 0 if rest == 0 else 11 - rest
    return None if k == 10 else k


def _no_year(yy: int, individual: int) -> int | None:
    if individual <= 499:
        return 1900 + yy
    if individual <= 749 and yy >= 54:
        return 1800 + yy
    if individual >= 500 and yy <= 39:
        return 2000 + yy
    if individual >= 900 and yy >= 40:
        return 1900 + yy
    return None


def _valid_no(m: re.Match) -> bool:
    digits = m.group(1) + m.group(2)
    day, month, yy = int(digits[0:2]), int(digits[2:4]), int(digits[4:6])
    if day > 40:
        day -= 40  # D-number
    if month > 80:
        month -= 80  # synthetic test number
    elif month > 40:
        month -= 40  # H-number
    year = _no_year(yy, int(digits[6:9]))
    if year is None or not _valid_date(year, month, day):
        return False
    k1 = _mod11_check(digits[:9], _NO_W1)
    if k1 is None or k1 != int(digits[9]):
        return False
    k2 = _mod11_check(digits[:10], _NO_W2)
    return k2 is not None and k2 == int(digits[10])


# ---------------------------------------------------------------------------
# Denmark — CPR number
#   DDMMYY-SSSS. The 7th digit plus YY gives the century. Since 2007 the
#   mod-11 check is no longer guaranteed, so a hyphenated CPR only needs a
#   valid date. An unseparated 10-digit run is far more likely to be some
#   other number, so there the mod-11 check is also required.
# ---------------------------------------------------------------------------
_DK_RE = re.compile(_L + r"(\d{2})(\d{2})(\d{2})(-?)(\d{4})" + _R)
_DK_W = (4, 3, 2, 7, 6, 5, 4, 3, 2, 1)


def _dk_year(yy: int, d7: int) -> int:
    if d7 <= 3:
        return 1900 + yy
    if d7 in (4, 9):
        return 2000 + yy if yy <= 36 else 1900 + yy
    # 5-8
    if yy <= 57:
        return 2000 + yy
    return 1800 + yy


def _valid_dk(m: re.Match) -> bool:
    dd, mm, yy, sep, serial = m.groups()
    year = _dk_year(int(yy), int(serial[0]))
    if not _valid_date(year, int(mm), int(dd)):
        return False
    if sep:
        return True
    digits = dd + mm + yy + serial
    return sum(int(d) * w for d, w in zip(digits, _DK_W)) % 11 == 0


# ---------------------------------------------------------------------------
# Finland — henkilötunnus (HETU)
#   DDMMYYCZZZQ — C is the century marker, ZZZ the individual number
#   (002-899, 900-999 temporary), Q = table[int(DDMMYYZZZ) % 31].
# ---------------------------------------------------------------------------
_FI_RE = re.compile(
    _L + r"(\d{2})(\d{2})(\d{2})([-+ABCDEFUVWXY])(\d{3})([0-9A-Y])" + _R,
    re.IGNORECASE,
)
_FI_CHECK = "0123456789ABCDEFHJKLMNPRSTUVWXY"
_FI_CENTURY = {"+": 1800, "-": 1900, "A": 2000}
_FI_CENTURY.update(dict.fromkeys("UVWXY", 1900))
_FI_CENTURY.update(dict.fromkeys("BCDEF", 2000))


def _valid_fi(m: re.Match) -> bool:
    dd, mm, yy, marker, individual, check = m.groups()
    year = _FI_CENTURY[marker.upper()] + int(yy)
    if not _valid_date(year, int(mm), int(dd)):
        return False
    if int(individual) < 2:
        return False
    return _FI_CHECK[int(dd + mm + yy + individual) % 31] == check.upper()


_DETECTORS = (
    (_SE_RE, _valid_se),
    (_NO_RE, _valid_no),
    (_DK_RE, _valid_dk),
    (_FI_RE, _valid_fi),
)


def contains_nordic_national_id(text: str) -> bool:
    """True when `text` contains a checksum-valid Nordic national ID."""
    for pattern, validate in _DETECTORS:
        for m in pattern.finditer(text):
            if validate(m):
                return True
    return False

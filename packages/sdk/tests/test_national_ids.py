"""Nordic national-ID detection.

Every number here is synthetic: either from an official test range (Swedish
Skatteverket test person 19121212-1212, Norwegian synthetic numbers with
month + 80, Finnish temporary individual numbers 900-999, the Finnish DVV
documentation example 131052-308T) or computed to pass the checksum for a
made-up date. None belongs to a real person.
"""

import pytest
from prompt_shields.national_ids import contains_nordic_national_id
from prompt_shields.pii import detect_pii_categories


def _has(text: str) -> bool:
    return contains_nordic_national_id(text)


# ---------------------------------------------------------------- Sweden


@pytest.mark.parametrize(
    "text",
    [
        "19121212-1212",  # with century and separator
        "191212121212",  # 12 digits, no separator
        "121212-1212",  # without century
        "121212+1212",  # '+' separator (aged 100+)
        "19121272-1219",  # samordningsnummer (day + 60)
        "121272-1219",
    ],
)
def test_swedish_valid(text):
    assert _has(f"Mitt personnummer är {text}.")


@pytest.mark.parametrize(
    "text",
    [
        "191212121213",  # Luhn fails
        "19121212-1213",  # Luhn fails
        "19121312-1212",  # month 13
        "20230230-1234",  # 30 February
        "1212121212",  # bare 10 digits: too ambiguous, not accepted
    ],
)
def test_swedish_invalid(text):
    assert not _has(text)


# ---------------------------------------------------------------- Norway


@pytest.mark.parametrize(
    "text",
    [
        "01818510078",  # synthetic: month + 80
        "29129610024",  # synthetic, 29 Dec 1996
        "41818510061",  # synthetic D-number (day + 40)
        "01418510095",  # H-number (month + 40)
        "29020450051",  # 29 Feb 2004 (leap year, 21st-century individual no.)
        "018185 10078",  # space between date and personal number
    ],
)
def test_norwegian_valid(text):
    assert _has(f"Fødselsnummer: {text}")


@pytest.mark.parametrize(
    "text",
    [
        "01818510079",  # second check digit wrong
        "01818510068",  # first check digit wrong
        "29020350051",  # 29 Feb 2003 does not exist
        "32018510078",  # day 32
        "12345678901",  # sequential digits
    ],
)
def test_norwegian_invalid(text):
    assert not _has(text)


# ---------------------------------------------------------------- Denmark


@pytest.mark.parametrize(
    "text",
    [
        "010190-1234",  # hyphenated: valid date is enough (post-2007 rules)
        "290200-4321",  # 29 Feb 2000 (7th digit 4, yy <= 36)
        "0101701018",  # unseparated: mod-11 must also hold
        "2902004001",  # 29 Feb 2000, unseparated
    ],
)
def test_danish_valid(text):
    assert _has(f"CPR-nr. {text}")


@pytest.mark.parametrize(
    "text",
    [
        "300290-1234",  # 30 February
        "011390-1234",  # month 13
        "290201-4321",  # 29 Feb 2001 does not exist
        "0101901234",  # unseparated and fails mod-11
        "2902001002",  # passes mod-11, but 7th digit 1 means 1900: no 29 Feb
    ],
)
def test_danish_invalid(text):
    assert not _has(text)


# ---------------------------------------------------------------- Finland


@pytest.mark.parametrize(
    "text",
    [
        "131052-308T",  # DVV documentation example
        "010101A900R",  # temporary individual number range
        "290200A901C",  # leap day 2000
        "311299-902A",
        "131052-308t",  # lower-case check character
    ],
)
def test_finnish_valid(text):
    assert _has(f"Henkilötunnus {text}")


@pytest.mark.parametrize(
    "text",
    [
        "131052-308U",  # wrong check character
        "290201A901C",  # 29 Feb 2001 does not exist
        "010101A001X",  # individual numbers 000-001 are not issued
        "131052Z308T",  # unknown century marker
    ],
)
def test_finnish_invalid(text):
    assert not _has(text)


# ---------------------------------------------------------------- look-alikes


@pytest.mark.parametrize(
    "text",
    [
        "Ring mig på +46 70 123 45 67",
        "Ring 0701234567 eller 08-123 456 78",
        "Ring meg på +47 912 34 567 eller +4791234567",
        "Order ORD-20240115-0042 shipped",
        "Invoice #4821937560 is overdue",
        "Tracking 1Z999AA10123456784",
        "Meeting on 2024-01-15 at 10:30",
        "Date 15.01.2024, ref 15012024",
        "Timestamp 20240115103000",
        "Build 1.2.3-121212",
        "Card 4111 1111 1111 1111",
        "Long id 123456789012345678",
    ],
)
def test_lookalikes_not_national_id(text):
    assert not _has(text)


def test_embedded_in_longer_number_not_matched():
    # A valid ID buried inside a longer digit run is not an ID.
    assert not _has("9" + "01818510078" + "9")
    assert not _has("x19121212-12125")


# ---------------------------------------------------------------- integration


def test_category_emitted_by_detect_pii_categories():
    assert "national_id" in detect_pii_categories("Kunde: 19121212-1212")
    assert "national_id" in detect_pii_categories("HETU 131052-308T")


def test_category_absent_for_plain_text():
    assert "national_id" not in detect_pii_categories("Order 2024-01-15 #4821")

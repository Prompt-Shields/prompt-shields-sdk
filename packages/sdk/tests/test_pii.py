from prompt_shields.pii import detect_pii_categories, scan_messages


def test_detects_email():
    assert "email" in detect_pii_categories("Contact jane.doe@acme.com please")


def test_detects_phone():
    assert "phone" in detect_pii_categories("Call me at 415-555-0199")


def test_detects_ssn():
    assert "ssn" in detect_pii_categories("SSN is 123-45-6789")


def test_detects_ip_address():
    assert "ip_address" in detect_pii_categories("Server at 192.168.1.1")


def test_detects_health_keywords():
    assert "health_data" in detect_pii_categories(
        "Patient diagnosis: ICD-10 J45.909"
    )


def test_detects_financial_keywords():
    assert "financial_data" in detect_pii_categories(
        "Account number 1234, routing number 5678"
    )


def test_no_pii_returns_empty():
    assert detect_pii_categories("Hello world, this is just text.") == []


def test_none_returns_empty():
    assert detect_pii_categories(None) == []


def test_empty_string_returns_empty():
    assert detect_pii_categories("") == []


def test_categories_alphabetical():
    """Output must be deterministic for stable deduplication."""
    text = "Email jane@a.com phone 415-555-0199 SSN 123-45-6789"
    cats = detect_pii_categories(text)
    assert cats == sorted(cats)


def test_multiple_categories():
    text = "Contact jane@acme.com or call 415-555-0199. SSN: 123-45-6789"
    cats = detect_pii_categories(text)
    assert "email" in cats
    assert "phone" in cats
    assert "ssn" in cats


def test_scan_messages_combines_content():
    messages = [
        {"role": "system", "content": "Be helpful"},
        {"role": "user", "content": "My email is bob@test.com"},
    ]
    cats = scan_messages(messages)
    assert "email" in cats


def test_scan_messages_handles_non_dict():
    messages = [None, "not a dict", {"role": "user", "content": "phone 415-555-0199"}]
    cats = scan_messages(messages)
    assert "phone" in cats

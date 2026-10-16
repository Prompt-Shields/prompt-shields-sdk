from collector.dedup import compute_confidence


def test_confidence_single_sdk():
    assert compute_confidence(["sdk"]) == "high"

def test_confidence_single_browser():
    assert compute_confidence(["browser_extension"]) == "medium"

def test_confidence_single_survey():
    assert compute_confidence(["survey"]) == "low"

def test_confidence_multiple_sources():
    assert compute_confidence(["sdk", "browser_extension"]) == "verified"

def test_confidence_empty():
    assert compute_confidence([]) == "low"

def test_confidence_single_otel_is_high():
    # OTel GenAI spans are observed runtime calls, like the SDK/gateway (#92).
    assert compute_confidence(["otel"]) == "high"

def test_confidence_single_code_scan_is_medium():
    # A static code reference shows intent, not an observed call (#95).
    assert compute_confidence(["code_scan"]) == "medium"

def test_confidence_code_scan_corroborated_is_verified():
    assert compute_confidence(["code_scan", "otel"]) == "verified"
    assert compute_confidence(["code_scan", "sdk"]) == "verified"

def test_confidence_duplicate_source_is_not_verified():
    assert compute_confidence(["code_scan", "code_scan"]) == "medium"

def test_confidence_unknown_source_is_low():
    assert compute_confidence(["something_new"]) == "low"

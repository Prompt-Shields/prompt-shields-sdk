"""Smoke run of the ingest load test (loadtest/ingest_load.py, #82).

A few dozen requests against the real collector on a local port and the test
database: the per-tenant limit holds under concurrency, 429s carry
Retry-After, and the database holds exactly the accepted events. The full
load test is run by hand; see the module docstring.
"""

from loadtest.ingest_load import LoadConfig, LoadReport, Sample, percentile, run


def test_percentile_nearest_rank():
    values = [float(v) for v in range(1, 101)]
    assert percentile(values, 50) == 50
    assert percentile(values, 99) == 99
    assert percentile(values, 100) == 100
    assert percentile([7.0], 95) == 7


def test_report_flags_a_window_over_the_limit():
    cfg = LoadConfig(database_url="", tenants=1, events_per_request=1, rate_limit=2)
    samples = [Sample(0, 200, 1.0, window="60") for _ in range(3)]
    report = LoadReport(cfg, 1.0, samples, stored_events={0: 3}, rejected_events={0: 0})
    assert report.failures() == ["tenant 0 window 60: 3 accepted > limit 2"]


def test_ingest_load_smoke_enforces_limit_per_tenant(test_database_url):
    cfg = LoadConfig(
        database_url=test_database_url,
        tenants=2,
        concurrency=8,
        requests=24,
        events_per_request=2,
        rate_limit=5,
    )
    report = run(cfg)
    d = report.as_dict()

    assert report.failures() == [], d
    # 12 requests per tenant against 5 a minute: some are always refused,
    # even when the run straddles a minute boundary.
    assert report.rate_limited() > 0
    assert report.accepted() >= 2 * cfg.rate_limit
    assert set(d["status_counts"]) <= {200, 429}
    assert set(d["latency_ms"]) == {"p50", "p90", "p95", "p99", "max"}
    assert all(int(v) >= 1 for v in d["retry_after_values"])

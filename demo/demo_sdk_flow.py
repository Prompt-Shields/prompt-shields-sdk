"""End-to-end demo: SDK call -> telemetry -> registry API shows the asset."""
import httpx

COLLECTOR_URL = "http://localhost:8000"
API_KEY = "ps-demo-key-acme"
HEADERS = {"Authorization": f"Bearer {API_KEY}"}


def main():
    print("=== Prompt Shields SDK Demo ===\n")

    print("1. Simulating SDK call (HR interview screening with GPT-4o)...")
    resp = httpx.post(f"{COLLECTOR_URL}/ingest/events", headers=HEADERS, json={
        "events": [{
            "vendor": "openai",
            "model": "gpt-4o",
            "use_case_name": "interview-screening",
            "business_unit": "HR",
            "owner_email": "jane.doe@acme.com",
            "environment": "production",
            "data_classification": "confidential",
            "source": "sdk",
            "tokens_in": 250,
            "tokens_out": 500,
            "latency_ms": 320,
            "data_sources": ["candidates_db", "linkedin_api"],
            "output_destination": "hiring_dashboard",
            "detected_pii_types": ["email", "national_id"],
        }]
    })
    print(f"   Ingested: {resp.json()}\n")

    print("2. Querying registry API for all assets...")
    resp = httpx.get(f"{COLLECTOR_URL}/api/v1/registry/assets", headers=HEADERS)
    assets = resp.json()
    print(f"   Found {assets['meta']['total']} assets:")
    for a in assets["data"]:
        print(f"   - {a['vendor']}/{a['model']} | {a['use_case_name']} | {a['business_unit']} | confidence: {a['confidence']}")

    print("\n3. Filtering by business_unit=HR...")
    resp = httpx.get(f"{COLLECTOR_URL}/api/v1/registry/assets?business_unit=HR", headers=HEADERS)
    hr_assets = resp.json()
    print(f"   Found {hr_assets['meta']['total']} HR assets")

    print("\n4. Listing all AI vendors in use...")
    resp = httpx.get(f"{COLLECTOR_URL}/api/v1/registry/vendors", headers=HEADERS)
    print(f"   Vendors: {resp.json()['data']}")

    print("\n5. Data flows derived from the telemetry...")
    for a in assets["data"]:
        resp = httpx.get(
            f"{COLLECTOR_URL}/api/v1/registry/assets/{a['id']}/data-flows", headers=HEADERS
        )
        for f in resp.json()["data"]:
            print(f"   - {f['source_system']} -> {f['destination_system']} "
                  f"({f['direction']}, PII: {f['detected_pii_types']})")

    print("\n=== Demo complete ===")
    print(f"Registry endpoint: GET {COLLECTOR_URL}/api/v1/registry/assets")
    print(f"Auth: Bearer {API_KEY}")


if __name__ == "__main__":
    main()

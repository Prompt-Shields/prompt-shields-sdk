from db.models import AIAsset, AIUsageEvent, DataFlow, RiskMapping, Tenant


def test_models_exist():
    assert Tenant.__tablename__ == "tenants"
    assert AIAsset.__tablename__ == "ai_assets"
    assert AIUsageEvent.__tablename__ == "ai_usage_events"
    assert DataFlow.__tablename__ == "data_flows"
    assert RiskMapping.__tablename__ == "risk_mappings"

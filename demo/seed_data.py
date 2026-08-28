"""Seed the database with synthetic AI assets for demo purposes."""
import asyncio
import os
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from db.models import Base, Tenant, AIAsset, DataFlow, RiskMapping
from datetime import datetime, timezone

# Same environment variable and default the collector uses, so a host that has
# remapped the database port (PS_DB_PORT) does not have to edit this file.
DB_URL = os.environ.get(
    "PS_DATABASE_URL",
    "postgresql+asyncpg://ps_user:ps_local_dev@localhost:5432/prompt_shields",
)


async def seed():
    engine = create_async_engine(DB_URL)
    session_factory = async_sessionmaker(engine, class_=AsyncSession)

    async with session_factory() as db:
        tenant = Tenant(name="Acme Insurance", domain="acme.com",
                        settings={"api_key": "ps-demo-key-acme"})
        db.add(tenant)
        await db.flush()

        now = datetime.now(timezone.utc)

        assets = [
            AIAsset(tenant_id=tenant.id, vendor="openai", model="gpt-4o",
                    use_case_name="interview-screening", business_unit="HR",
                    owner_email="jane.doe@acme.com", environment="production",
                    data_classification="confidential", discovery_source=["sdk", "browser_extension"],
                    confidence="verified", first_seen=now, last_seen=now),
            AIAsset(tenant_id=tenant.id, vendor="anthropic", model="claude-sonnet-4-20250514",
                    use_case_name="contract-review", business_unit="Legal",
                    owner_email="bob.smith@acme.com", environment="production",
                    data_classification="restricted", discovery_source=["gateway"],
                    confidence="high", first_seen=now, last_seen=now),
            AIAsset(tenant_id=tenant.id, vendor="openai", model="gpt-4o-mini",
                    use_case_name="customer-support-bot", business_unit="Customer Service",
                    owner_email="alice.wong@acme.com", environment="production",
                    data_classification="internal", discovery_source=["sdk"],
                    confidence="high", first_seen=now, last_seen=now),
            AIAsset(tenant_id=tenant.id, vendor="google", model="gemini-1.5-pro",
                    use_case_name="claims-analysis", business_unit="Claims",
                    owner_email="charlie.brown@acme.com", environment="staging",
                    data_classification="restricted", discovery_source=["browser_extension"],
                    confidence="medium", first_seen=now, last_seen=now),
        ]
        db.add_all(assets)
        await db.flush()

        flows = [
            DataFlow(tenant_id=tenant.id, asset_id=assets[0].id,
                     source_system="candidates_db", destination_system="hiring_dashboard",
                     data_classification="confidential", direction="input",
                     detected_pii_types=["name", "email", "phone"]),
            DataFlow(tenant_id=tenant.id, asset_id=assets[1].id,
                     source_system="contract_repository", destination_system="legal_review_app",
                     data_classification="restricted", direction="input",
                     detected_pii_types=["contract_terms", "financial_data"]),
        ]
        db.add_all(flows)

        risks = [
            RiskMapping(tenant_id=tenant.id, asset_id=assets[0].id,
                        risk_category="compliance", risk_level="high", framework="EU_AI_ACT"),
            RiskMapping(tenant_id=tenant.id, asset_id=assets[0].id,
                        risk_category="operational", risk_level="medium", framework="NIST_AI_RMF"),
            RiskMapping(tenant_id=tenant.id, asset_id=assets[1].id,
                        risk_category="compliance", risk_level="critical", framework="EU_AI_ACT"),
            RiskMapping(tenant_id=tenant.id, asset_id=assets[1].id,
                        risk_category="reputational", risk_level="high", framework="ISO_42001"),
        ]
        db.add_all(risks)

        await db.commit()
        print(f"Seeded {len(assets)} assets, {len(flows)} data flows, {len(risks)} risk mappings")
        print(f"Demo API key: ps-demo-key-acme")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed())

"""Recompute ai_assets.embedding with the configured backend.

Vectors from different models are not comparable, so run this after changing
PS_EMBEDDINGS_BACKEND or PS_EMBEDDINGS_MODEL:

    python -m collector.reembed

With PS_EMBEDDINGS_BACKEND=none it clears every stored embedding instead.
Assets whose embedding fails are left NULL and drop out of semantic results
until the next run.
"""

import asyncio
import logging

from db.models import AIAsset
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from collector import rls
from collector.config import settings
from collector.embeddings import build_asset_text, get_embedder, get_embedding

logger = logging.getLogger("prompt_shields.reembed")

BATCH_SIZE = 100


async def reembed(session_factory: async_sessionmaker) -> tuple[int, int]:
    """Re-embed all assets. Returns (embedded, failed)."""
    async with session_factory() as session:
        if get_embedder() is None:
            await session.execute(update(AIAsset).values(embedding=None))
            await session.commit()
            return 0, 0

    embedded = failed = 0
    last_id = None
    while True:
        async with session_factory() as session:
            stmt = select(AIAsset).order_by(AIAsset.id).limit(BATCH_SIZE)
            if last_id is not None:
                stmt = stmt.where(AIAsset.id > last_id)
            assets = (await session.execute(stmt)).scalars().all()
            if not assets:
                return embedded, failed
            for asset in assets:
                vector = await get_embedding(
                    build_asset_text(
                        vendor=asset.vendor,
                        model=asset.model,
                        use_case_name=asset.use_case_name,
                        business_unit=asset.business_unit,
                        data_classification=asset.data_classification,
                    )
                )
                asset.embedding = vector
                if vector is None:
                    failed += 1
                else:
                    embedded += 1
            last_id = assets[-1].id
            await session.commit()


async def _main() -> None:
    engine = create_async_engine(settings.database_url)
    try:
        embedded, failed = await reembed(
            async_sessionmaker(
                engine,
                class_=AsyncSession,
                expire_on_commit=False,
                info=rls.bypass_info("reembed-cli"),
            )
        )
    finally:
        await engine.dispose()
    embedder = get_embedder()
    if embedder is None:
        print("Embeddings disabled: cleared all stored embeddings.")
    else:
        print(f"Re-embedded {embedded} assets with {embedder.name}; {failed} failed.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())

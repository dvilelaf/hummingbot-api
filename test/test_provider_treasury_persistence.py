import asyncio

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


def test_rebalance_repository_persists_and_claims_once_across_sessions(tmp_path):
    from database.models import Base
    from database.repositories.provider_treasury_rebalance_repository import (
        ProviderTreasuryRebalanceRepository,
    )

    async def exercise():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'hba.db'}")
        sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        async with sessions.begin() as session:
            repository = ProviderTreasuryRebalanceRepository(session)
            await repository.create_built(
                "rebalance-001",
                {
                    "amount": "1",
                    "destination_chain": "ethereum",
                    "destination_network": "base",
                    "destination_wallet_ref": "base:mainnet:evm_gateway",
                },
                {"id": "rebalance-001", "status": "built"},
            )

        async with sessions.begin() as session:
            repository = ProviderTreasuryRebalanceRepository(session)
            claimed = await repository.claim_for_execution("rebalance-001")
            assert claimed is not None
            assert claimed.status == "pending"

        async with sessions.begin() as session:
            repository = ProviderTreasuryRebalanceRepository(session)
            await repository.update_status(
                "rebalance-001",
                "built",
                {"id": "rebalance-001", "status": "built"},
            )
            assert await repository.claim_for_execution("rebalance-001") is None
            restored = await repository.get_rebalance("rebalance-001")
            assert restored.status == "pending"
            assert restored.request_payload == {
                "amount": "1",
                "destination_chain": "ethereum",
                "destination_network": "base",
                "destination_wallet_ref": "base:mainnet:evm_gateway",
            }
            assert restored.response_payload == {"id": "rebalance-001", "status": "built"}

        await engine.dispose()

    asyncio.run(exercise())

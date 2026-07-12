import asyncio
import json

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
                    "target_notional_eur": "1",
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
                "target_notional_eur": "1",
                "destination_chain": "ethereum",
                "destination_network": "base",
                "destination_wallet_ref": "base:mainnet:evm_gateway",
            }
            assert restored.response_payload == {"id": "rebalance-001", "status": "built"}

        await engine.dispose()

    asyncio.run(exercise())


def test_rebalance_repository_economics_persists_and_restores_across_sessions(tmp_path):
    from database.models import Base
    from database.repositories.provider_treasury_rebalance_repository import (
        ProviderTreasuryRebalanceRepository,
    )

    response_payload = {
        "id": "rebalance-econ-001",
        "status": "confirmed",
        "transaction_hash": "0xabc123",
        "source_amount": "5000.00",
        "source_asset": "DAI",
        "destination_amount": "4995.00",
        "destination_asset": "USDC",
        "quoted_provider_cost_usd": "25.00",
        "quoted_gas_cost_usd": "3.50",
        "quoted_native_gas_amount": "0.0021",
        "quoted_native_gas_asset": "ETH",
        "quoted_at": "2025-06-15T10:30:00+00:00",
    }
    response_json = json.dumps(response_payload, sort_keys=True)

    async def exercise():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'hba-econ.db'}")
        sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        async with sessions.begin() as session:
            repository = ProviderTreasuryRebalanceRepository(session)
            await repository.create_built(
                "rebalance-econ-001",
                {"target_notional_eur": "1000"},
                response_payload,
            )

        async with sessions.begin() as session:
            repository = ProviderTreasuryRebalanceRepository(session)
            restored = await repository.get_rebalance("rebalance-econ-001")
            restored_json = json.dumps(restored.response_payload, sort_keys=True)
            assert restored_json == response_json

        await engine.dispose()

    asyncio.run(exercise())

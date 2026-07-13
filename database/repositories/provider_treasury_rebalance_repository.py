from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import ProviderTreasuryRebalance


class ProviderTreasuryRebalanceRepository:
    """Persistence and atomic execution claims for provider-neutral rebalances."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_rebalance(self, rebalance_id: str) -> ProviderTreasuryRebalance | None:
        result = await self.session.execute(
            select(ProviderTreasuryRebalance)
            .where(ProviderTreasuryRebalance.rebalance_id == rebalance_id)
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def create_built(
        self,
        rebalance_id: str,
        request_payload: dict[str, Any],
        response_payload: dict[str, Any],
    ) -> ProviderTreasuryRebalance:
        existing = await self.get_rebalance(rebalance_id)
        if existing is not None:
            return existing
        record = ProviderTreasuryRebalance(
            rebalance_id=rebalance_id,
            status="built",
            request_payload=request_payload,
            response_payload=response_payload,
        )
        self.session.add(record)
        await self.session.flush()
        return record

    async def claim_for_execution(self, rebalance_id: str) -> ProviderTreasuryRebalance | None:
        result = await self.session.execute(
            update(ProviderTreasuryRebalance)
            .where(
                ProviderTreasuryRebalance.rebalance_id == rebalance_id,
                ProviderTreasuryRebalance.status == "built",
            )
            .values(status="pending")
            .returning(ProviderTreasuryRebalance)
        )
        return result.scalar_one_or_none()

    async def update_status(
        self,
        rebalance_id: str,
        status: str,
        response_payload: dict[str, Any],
    ) -> ProviderTreasuryRebalance | None:
        payload = dict(response_payload)
        # Preserve internal baseline fields from the existing record.
        existing = await self.get_rebalance(rebalance_id)
        if existing is not None and isinstance(existing.response_payload, dict):
            baseline = existing.response_payload.get("_hl_baseline_usdc")
            if baseline is not None:
                payload.setdefault("_hl_baseline_usdc", baseline)
        values: dict[str, Any] = {"response_payload": payload}
        # Gateway may briefly report its pre-submit build state while another
        # process owns the durable execution claim. Never reopen that claim.
        if status != "built":
            values["status"] = status
        statement = update(ProviderTreasuryRebalance).where(
            ProviderTreasuryRebalance.rebalance_id == rebalance_id,
        )
        if status != "confirmed":
            statement = statement.where(ProviderTreasuryRebalance.status != "confirmed")
        result = await self.session.execute(
            statement.values(**values).returning(ProviderTreasuryRebalance)
        )
        updated = result.scalar_one_or_none()
        return updated if updated is not None else await self.get_rebalance(rebalance_id)

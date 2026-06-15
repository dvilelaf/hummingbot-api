"""Gateway bridge execution routes."""
import logging

from fastapi import APIRouter, Depends, HTTPException

from deps import get_accounts_service
from models import BridgeExecuteRequest, BridgeExecuteResponse
from services.accounts_service import AccountsService
from services.live_trading_gate import assert_live_bridge_execution_allowed

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Gateway Bridge"], prefix="/gateway")


def _transaction_hash(result: dict) -> str | None:
    return (
        result.get("transaction_hash")
        or result.get("transactionHash")
        or result.get("signature")
        or result.get("txHash")
        or result.get("hash")
    )


def _gateway_status(result: dict) -> str:
    status = str(result.get("status") or "submitted").strip().lower()
    if status in {"1", "confirmed"}:
        return "confirmed"
    if status in {"-1", "failed", "failure", "error", "reverted", "revert"}:
        return "failed"
    return "submitted"


@router.post("/bridge/execute", response_model=BridgeExecuteResponse)
async def execute_bridge(
    request: BridgeExecuteRequest,
    accounts_service: AccountsService = Depends(get_accounts_service),
):
    """Execute a Marlin-approved bridge transaction through Gateway."""
    try:
        if not await accounts_service.gateway_client.ping():
            raise HTTPException(status_code=503, detail="Gateway service is not available")

        chain, network = accounts_service.gateway_client.parse_network_id(request.network)
        assert_live_bridge_execution_allowed(
            expected_authorization_nonce=request.authorization_nonce,
            expected_calldata_hash=request.tx_calldata_hash,
            expected_provider=request.provider,
            expected_provider_route_id=request.provider_route_id,
            expected_quote_id=request.quote_id,
            expected_route_payload_hash=request.route_payload_hash,
            expected_source_chain_id=request.source_chain_id,
            expected_target=request.tx_target,
            expected_value=request.tx_value,
            live_action_authorization=request.live_action_authorization,
            source="gateway_bridge.execute_bridge",
        )
        wallet_address = await accounts_service.gateway_client.get_wallet_address_or_default(
            chain=chain,
            wallet_address=request.wallet_address,
        )
        result = await accounts_service.gateway_client.execute_bridge(
            provider=request.provider,
            provider_route_id=request.provider_route_id,
            quote_id=request.quote_id,
            route_payload_hash=request.route_payload_hash,
            source_chain=request.source_chain,
            source_chain_id=request.source_chain_id,
            network=network,
            wallet_address=wallet_address,
            tx_target=request.tx_target,
            tx_value=request.tx_value,
            tx_calldata=request.tx_calldata,
            tx_calldata_hash=request.tx_calldata_hash,
            gas_limit=request.gas_limit,
            live_action_authorization=request.live_action_authorization,
        )
        if not result:
            raise HTTPException(status_code=500, detail="Gateway service is not able to execute bridge")
        transaction_hash = _transaction_hash(result)
        if not transaction_hash:
            raise HTTPException(status_code=500, detail="No transaction hash returned from Gateway")
        return BridgeExecuteResponse(transaction_hash=transaction_hash, status=_gateway_status(result))
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error("Error executing bridge: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error executing bridge: {exc}")

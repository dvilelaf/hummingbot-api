"""Gateway router liquidity routes."""
import logging

from fastapi import APIRouter, Depends, HTTPException

from deps import get_accounts_service
from models import RouterAddLiquidityRequest, RouterLiquidityResponse, RouterRemoveLiquidityRequest
from services.accounts_service import AccountsService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Gateway Liquidity"], prefix="/gateway")


def _transaction_hash(result: dict) -> str | None:
    return (
        result.get("transaction_hash")
        or result.get("transactionHash")
        or result.get("signature")
        or result.get("txHash")
        or result.get("hash")
    )


@router.post("/lp/add", response_model=RouterLiquidityResponse)
async def add_router_liquidity(
    request: RouterAddLiquidityRequest,
    accounts_service: AccountsService = Depends(get_accounts_service),
):
    """Add liquidity through a Gateway router connector."""
    try:
        if not await accounts_service.gateway_client.ping():
            raise HTTPException(status_code=503, detail="Gateway service is not available")

        chain, network = accounts_service.gateway_client.parse_network_id(request.network)
        wallet_address = await accounts_service.gateway_client.get_wallet_address_or_default(
            chain=chain,
            wallet_address=request.wallet_address,
        )
        result = await accounts_service.gateway_client.router_add_liquidity(
            connector=request.connector,
            network=network,
            wallet_address=wallet_address,
            token_a=request.token_a,
            token_b=request.token_b,
            amount_a=float(request.amount_a),
            amount_b=float(request.amount_b),
            pool_type=request.pool_type,
            slippage_pct=float(request.slippage_pct) if request.slippage_pct is not None else None,
        )
        if not result:
            raise HTTPException(status_code=500, detail="Gateway service is not able to add liquidity")
        transaction_hash = _transaction_hash(result)
        if not transaction_hash:
            raise HTTPException(status_code=500, detail="No transaction hash returned from Gateway")
        return RouterLiquidityResponse(transaction_hash=transaction_hash, status="submitted")
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error("Error adding router liquidity: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error adding router liquidity: {exc}")


@router.post("/lp/remove", response_model=RouterLiquidityResponse)
async def remove_router_liquidity(
    request: RouterRemoveLiquidityRequest,
    accounts_service: AccountsService = Depends(get_accounts_service),
):
    """Remove liquidity through a Gateway router connector."""
    try:
        if not await accounts_service.gateway_client.ping():
            raise HTTPException(status_code=503, detail="Gateway service is not available")

        chain, network = accounts_service.gateway_client.parse_network_id(request.network)
        wallet_address = await accounts_service.gateway_client.get_wallet_address_or_default(
            chain=chain,
            wallet_address=request.wallet_address,
        )
        result = await accounts_service.gateway_client.router_remove_liquidity(
            connector=request.connector,
            network=network,
            wallet_address=wallet_address,
            token_a=request.token_a,
            token_b=request.token_b,
            liquidity=float(request.liquidity),
            pool_type=request.pool_type,
            slippage_pct=float(request.slippage_pct) if request.slippage_pct is not None else None,
        )
        if not result:
            raise HTTPException(status_code=500, detail="Gateway service is not able to remove liquidity")
        transaction_hash = _transaction_hash(result)
        if not transaction_hash:
            raise HTTPException(status_code=500, detail="No transaction hash returned from Gateway")
        return RouterLiquidityResponse(transaction_hash=transaction_hash, status="submitted")
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error("Error removing router liquidity: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error removing router liquidity: {exc}")

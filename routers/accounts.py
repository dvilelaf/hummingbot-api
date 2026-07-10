from typing import Dict, List

from fastapi import APIRouter, Depends, HTTPException
from starlette import status

from deps import get_accounts_service
from models import MarlinDefaultWalletRequest
from services.accounts_service import AccountsService
from services.marlin_runtime import (
    assert_connector_credential_deletion_allowed,
    assert_marlin_default_wallet_identity,
    is_marlin_runtime,
    sanitize_account_credential_update,
)

router = APIRouter(tags=["Accounts"], prefix="/accounts")


@router.get("/", response_model=List[str])
async def list_accounts(accounts_service: AccountsService = Depends(get_accounts_service)):
    """
    Get a list of all account names in the system.

    Returns:
        List of account names
    """
    return accounts_service.list_accounts()


@router.get("/{account_name}/credentials", response_model=List[str])
async def list_account_credentials(account_name: str,
                                   accounts_service: AccountsService = Depends(get_accounts_service)):
    """
    Get a list of all connectors that have credentials configured for a specific account.

    Args:
        account_name: Name of the account to list credentials for

    Returns:
        List of connector names that have credentials configured

    Raises:
        HTTPException: 404 if account not found
    """
    try:
        credentials = accounts_service.list_credentials(account_name)
        # Remove .yml extension from filenames
        return [cred.replace('.yml', '') for cred in credentials]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/add-account", status_code=status.HTTP_201_CREATED)
async def add_account(account_name: str, accounts_service: AccountsService = Depends(get_accounts_service)):
    """
    Create a new account with default configuration files.

    Args:
        account_name: Name of the new account to create

    Returns:
        Success message when account is created

    Raises:
        HTTPException: 400 if account already exists
    """
    try:
        accounts_service.add_account(account_name)
        return {"message": "Account added successfully."}
    except FileExistsError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/delete-account")
async def delete_account(account_name: str, accounts_service: AccountsService = Depends(get_accounts_service)):
    """
    Delete an account and all its associated credentials.

    Args:
        account_name: Name of the account to delete

    Returns:
        Success message when account is deleted

    Raises:
        HTTPException: 400 if trying to delete master account, 404 if account not found
    """
    try:
        if account_name == "master_account":
            raise HTTPException(status_code=400, detail="Cannot delete master account.")
        await accounts_service.delete_account(account_name)
        return {"message": "Account deleted successfully."}
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/delete-credential/{account_name}/{connector_name}")
async def delete_credential(account_name: str, connector_name: str, accounts_service: AccountsService = Depends(get_accounts_service)):
    """
    Delete a specific connector credential for an account.

    Args:
        account_name: Name of the account
        connector_name: Name of the connector to delete credentials for

    Returns:
        Success message when credential is deleted

    Raises:
        HTTPException: 404 if credential not found
    """
    try:
        assert_connector_credential_deletion_allowed(connector_name)
        await accounts_service.delete_credentials(account_name, connector_name)
        return {"message": "Credential deleted successfully."}
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/add-credential/{account_name}/{connector_name}", status_code=status.HTTP_201_CREATED)
async def add_credential(account_name: str, connector_name: str, credentials: Dict, accounts_service: AccountsService = Depends(get_accounts_service)):
    """
    Add or update connector credentials (API keys) for a specific account and connector.

    Args:
        account_name: Name of the account
        connector_name: Name of the connector
        credentials: Dictionary containing the connector credentials

    Returns:
        Success message when credentials are added

    Raises:
        HTTPException: 400 if there's an error adding the credentials
    """
    try:
        sanitized_credentials = sanitize_account_credential_update(
            connector_name=connector_name,
            credentials=credentials,
        )
        await accounts_service.add_credentials(account_name, connector_name, sanitized_credentials)
        return {"message": "Connector credentials added successfully."}
    except HTTPException:
        raise
    except Exception as e:
        await accounts_service.delete_credentials(account_name, connector_name)
        raise HTTPException(status_code=400, detail=str(e))


# ============================================
# Gateway Wallet Management Endpoints
# ============================================

@router.get("/gateway/wallets")
async def list_gateway_wallets(accounts_service: AccountsService = Depends(get_accounts_service)):
    """
    List all wallets managed by Gateway.
    Gateway manages its own encrypted wallet storage.

    Returns:
        List of wallet information from Gateway

    Raises:
        HTTPException: 503 if Gateway unavailable
    """
    try:
        wallets = await accounts_service.get_gateway_wallets()
        return wallets
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/gateway/wallets/default")
async def set_marlin_default_gateway_wallet(
    request: MarlinDefaultWalletRequest,
    accounts_service: AccountsService = Depends(get_accounts_service)
) -> Dict:
    """Set a Gateway default wallet only after Marlin supplies scoped public identity."""
    try:
        if not is_marlin_runtime():
            raise HTTPException(
                status_code=403,
                detail="Marlin default wallet endpoint requires MARLIN_RUNTIME_PROFILE=marlin",
            )
        if not request.wallet_ref.strip():
            raise HTTPException(status_code=400, detail="wallet_ref is required")
        if not request.network.strip():
            raise HTTPException(status_code=400, detail="network is required")
        assert_marlin_default_wallet_identity(
            chain=request.chain,
            network=request.network,
            address=request.address,
            wallet_ref=request.wallet_ref,
        )
        if not await accounts_service.gateway_client.ping():
            raise HTTPException(status_code=503, detail="Gateway service is not available")

        result = await accounts_service.gateway_client.set_marlin_default_wallet(
            chain=request.chain,
            network=request.network,
            address=request.address,
            wallet_ref=request.wallet_ref,
        )

        if result is None:
            raise HTTPException(status_code=502, detail="Failed to set default wallet: Gateway returned no response")

        if "error" in result:
            raise HTTPException(status_code=400, detail=f"Failed to set default wallet: {result.get('error')}")

        return {
            "success": True,
            "chain": request.chain,
            "network": request.network,
            "address": request.address,
            "wallet_ref": request.wallet_ref,
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error setting Marlin default wallet: {str(e)}")

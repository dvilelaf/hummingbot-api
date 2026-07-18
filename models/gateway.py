from typing import List, Optional

from pydantic import BaseModel, Field

# ============================================
# Container Management Models
# ============================================


class GatewayConfig(BaseModel):
    """Configuration for Gateway container deployment"""
    passphrase: str = Field(description="Gateway passphrase for configuration encryption")
    image: str = Field(default="hummingbot/gateway:latest", description="Docker image for Gateway")
    port: int = Field(default=15888, description="Port for Gateway API")
    dev_mode: bool = Field(default=True, description="Enable development mode")


class GatewayStatus(BaseModel):
    """Status information for Gateway instance"""
    running: bool = Field(description="Whether Gateway container is running")
    container_id: Optional[str] = Field(default=None, description="Container ID if running")
    image: Optional[str] = Field(default=None, description="Image used for the container")
    created_at: Optional[str] = Field(default=None, description="Container creation timestamp")
    port: Optional[int] = Field(default=None, description="Port Gateway is running on")


# ============================================
# Wallet Management Models
# ============================================

class GatewayTransactionPollRequest(BaseModel):
    """Request to poll a Gateway transaction by chain/network and hash."""

    chain: str = Field(description="Blockchain chain (e.g., 'solana', 'ethereum')")
    network: str = Field(description="Network (e.g., 'mainnet-beta', 'base')")
    tx_hash: str = Field(description="Transaction hash/signature to poll")


class GatewayWalletInfo(BaseModel):
    """Information about a connected Gateway wallet"""
    chain: str = Field(description="Blockchain chain")
    address: str = Field(description="Wallet address")
    network: str = Field(description="Network the wallet is configured for")


class MarlinDefaultWalletRequest(BaseModel):
    """Marlin-scoped request to set a mnemonic-derived Gateway default wallet."""

    chain: str = Field(description="Blockchain chain (e.g., 'solana', 'ethereum')")
    network: str = Field(description="Network scope for the derived wallet")
    address: str = Field(description="Mnemonic-derived public wallet address")
    wallet_ref: str = Field(description="Marlin wallet policy reference")


# ============================================
# Pool and Token Management Models
# ============================================

class AddPoolRequest(BaseModel):
    """Request to add a liquidity pool"""
    connector_name: str = Field(description="DEX connector name (e.g., 'raydium', 'meteora')")
    type: str = Field(description="Pool type ('clmm' or 'amm')")
    network: Optional[str] = Field(
        default=None,
        description="Network name (e.g., 'mainnet-beta') - optional for /networks/{network_id}/pools"
    )
    address: str = Field(description="Pool contract address")
    base: str = Field(description="Base token symbol")
    quote: str = Field(description="Quote token symbol")
    base_address: str = Field(description="Base token contract address")
    quote_address: str = Field(description="Quote token contract address")
    fee_pct: Optional[float] = Field(default=None, description="Pool fee percentage (e.g., 0.25)")


class AddTokenRequest(BaseModel):
    """Request to add a custom token to Gateway"""
    address: str = Field(description="Token contract address")
    symbol: str = Field(description="Token symbol")
    name: Optional[str] = Field(default=None, description="Token name (defaults to symbol)")
    decimals: int = Field(description="Number of decimals for the token")


# ============================================
# Balance Query Models
# ============================================

class GatewayBalanceRequest(BaseModel):
    """Request for Gateway wallet balances"""
    account_name: str = Field(description="Account name")
    chain: str = Field(description="Blockchain chain")
    tokens: Optional[List[str]] = Field(default=None, description="List of token symbols to query (optional)")


# ============================================
# API Keys Management Models
# ============================================

class UpdateApiKeysRequest(BaseModel):
    """Request to update Gateway API keys"""
    api_keys: dict = Field(
        description="Dict mapping service name to API key value (e.g., {'service': 'xyz789'})"
    )

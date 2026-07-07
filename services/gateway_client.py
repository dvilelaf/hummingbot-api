import asyncio
import logging
import os
import ssl
from decimal import Decimal
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import aiohttp

logger = logging.getLogger(__name__)
DEFAULT_GATEWAY_REQUEST_TIMEOUT_SECONDS = 180.0


class GatewayClient:
    """
    Simplified Gateway HTTP client for API integration.
    Provides essential functionality for wallet management and balance queries.
    """

    CLMM_SWAP_CONNECTORS = frozenset({"meteora", "orca", "pancakeswap-sol", "raydium"})
    ROUTER_OR_CLMM_SWAP_CONNECTORS = frozenset({"pancakeswap", "uniswap"})

    def __init__(self, base_url: str = "http://localhost:15888"):
        self.base_url = base_url
        self._session: Optional[aiohttp.ClientSession] = None

    @staticmethod
    def parse_network_id(network_id: str) -> tuple[str, str]:
        """
        Parse network_id in format 'chain-network' into (chain, network).

        Examples:
            'solana-mainnet-beta' -> ('solana', 'mainnet-beta')
            'ethereum-mainnet' -> ('ethereum', 'mainnet')
        """
        parts = network_id.split('-', 1)
        if len(parts) != 2:
            raise ValueError(f"Invalid network_id format. Expected 'chain-network', got '{network_id}'")
        return parts[0], parts[1]

    @staticmethod
    def _decimal_payload_value(value) -> str:
        if isinstance(value, Decimal):
            return format(value, "f")
        return str(value)

    @staticmethod
    def _gateway_passphrase() -> str:
        value = os.getenv("GATEWAY_PASSPHRASE", "").strip()
        if value:
            return value
        file_path = os.getenv("GATEWAY_PASSPHRASE_FILE", "").strip()
        if not file_path:
            return ""
        try:
            with open(file_path, encoding="utf-8") as handle:
                return handle.read().strip()
        except OSError:
            return ""

    @staticmethod
    def _marlin_gateway_provider_intent_token() -> str:
        value = os.getenv("MARLIN_GATEWAY_PROVIDER_INTENT_TOKEN", "").strip()
        if value:
            return value
        file_path = os.getenv("MARLIN_GATEWAY_PROVIDER_INTENT_TOKEN_FILE", "").strip()
        if not file_path:
            return ""
        try:
            with open(file_path, encoding="utf-8") as handle:
                return handle.read().strip()
        except OSError:
            return ""

    @staticmethod
    def _request_timeout() -> aiohttp.ClientTimeout:
        raw_value = os.getenv("GATEWAY_REQUEST_TIMEOUT_SECONDS", "").strip()
        if not raw_value:
            return aiohttp.ClientTimeout(total=DEFAULT_GATEWAY_REQUEST_TIMEOUT_SECONDS)
        try:
            seconds = float(raw_value)
        except ValueError:
            seconds = DEFAULT_GATEWAY_REQUEST_TIMEOUT_SECONDS
        if seconds <= 0:
            return aiohttp.ClientTimeout(total=None)
        return aiohttp.ClientTimeout(total=seconds)

    async def get_wallet_address_or_default(self, chain: str, wallet_address: Optional[str] = None) -> str:
        """Get wallet address - use provided or get default for chain"""
        if wallet_address:
            return wallet_address

        default_wallet = await self.get_default_wallet_address(chain)
        if not default_wallet:
            raise ValueError(f"No wallet configured for chain '{chain}'")
        # Skip placeholder wallet addresses (e.g., "ethereum-default-wallet", "solana-default-wallet")
        if default_wallet.endswith("-default-wallet"):
            raise ValueError(f"No valid wallet configured for chain '{chain}' (found placeholder: {default_wallet})")
        return default_wallet

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session"""
        if self._session is None or self._session.closed:
            connector = self._create_ssl_connector()
            timeout = self._request_timeout()
            if connector is not None:
                self._session = aiohttp.ClientSession(connector=connector, timeout=timeout)
            else:
                self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    def _create_ssl_connector(self) -> Optional[aiohttp.TCPConnector]:
        """Create a TLS client-certificate connector when Gateway HTTPS cert env is complete."""
        if urlparse(self.base_url).scheme != "https":
            return None

        ca_cert_file = os.getenv("GATEWAY_CA_CERT_FILE")
        client_cert_file = os.getenv("GATEWAY_CLIENT_CERT_FILE")
        client_key_file = os.getenv("GATEWAY_CLIENT_KEY_FILE")
        if not (ca_cert_file and client_cert_file and client_key_file):
            return None

        ssl_context = ssl.create_default_context(cafile=ca_cert_file)
        ssl_context.load_cert_chain(certfile=client_cert_file, keyfile=client_key_file)
        if os.getenv("GATEWAY_TLS_SKIP_HOSTNAME_VERIFY", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }:
            ssl_context.check_hostname = False
        return aiohttp.TCPConnector(ssl=ssl_context)

    async def close(self):
        """Close the aiohttp session"""
        if self._session and not self._session.closed:
            await self._session.close()

    async def _request(
        self,
        method: str,
        path: str,
        params: Dict = None,
        json: Dict = None,
        headers: Dict[str, str] | None = None,
    ) -> Optional[Dict]:
        """Make HTTP request to Gateway"""
        session = await self._get_session()
        url = f"{self.base_url}/{path}"

        try:
            if method == "GET":
                async with session.get(url, params=params, headers=headers) as response:
                    if not response.ok:
                        error_body = await self._get_error_body(response)
                        logger.warning(f"Gateway request failed: {method} {url} - {response.status} - {error_body}")
                        return {"error": error_body, "status": response.status}
                    return await response.json()
            elif method == "POST":
                async with session.post(url, params=params, json=json, headers=headers) as response:
                    if not response.ok:
                        error_body = await self._get_error_body(response)
                        logger.warning(f"Gateway request failed: {method} {url} - {response.status} - {error_body}")
                        return {"error": error_body, "status": response.status}
                    return await response.json()
            elif method == "DELETE":
                async with session.delete(url, params=params, json=json, headers=headers) as response:
                    if not response.ok:
                        error_body = await self._get_error_body(response)
                        logger.warning(f"Gateway request failed: {method} {url} - {response.status} - {error_body}")
                        return {"error": error_body, "status": response.status}
                    return await response.json()
        except aiohttp.ClientError as e:
            logger.debug(f"Gateway request error: {method} {url} - {e}")
            return None
        except asyncio.TimeoutError:
            logger.warning(f"Gateway request timed out: {method} {url}")
            return {"error": "Gateway request timed out", "status": 504}
        except Exception as e:
            logger.debug(f"Gateway request failed: {method} {url} - {e}")
            raise

    async def _get_error_body(self, response: aiohttp.ClientResponse) -> str:
        """Extract error message from response body"""
        try:
            data = await response.json()
            if isinstance(data, dict):
                return data.get("message") or data.get("error") or str(data)
            return str(data)
        except Exception:
            try:
                return await response.text()
            except Exception:
                return f"HTTP {response.status}"

    async def ping(self) -> bool:
        """Check if Gateway is online"""
        try:
            response = await self._request("GET", "")
            return response.get("status") == "ok"
        except Exception:
            return False

    async def get_wallets(self) -> List[Dict]:
        """Get all connected wallets"""
        return await self._request("GET", "wallet")

    async def get_default_wallet_address(self, chain: str) -> Optional[str]:
        """Get default wallet address for a chain from Gateway config"""
        try:
            config = await self._request("GET", "config", params={"namespace": chain})
            return config.get("defaultWallet")
        except Exception as e:
            logger.error(f"Error getting default wallet for chain {chain}: {e}")
            return None

    async def get_all_wallet_addresses(self, chain: Optional[str] = None) -> Dict[str, List[str]]:
        """
        Get all wallet addresses, optionally filtered by chain.

        Args:
            chain: Optional chain filter (e.g., 'solana', 'ethereum').
                   If not provided, returns wallets for all chains.

        Returns:
            Dict mapping chain name to list of wallet addresses.
            Example: {"solana": ["addr1", "addr2"], "ethereum": ["addr3"]}
        """
        try:
            wallets = await self.get_wallets()
            if wallets is None:
                return {}

            result = {}
            for wallet in wallets:
                wallet_chain = wallet.get("chain")
                if chain and wallet_chain != chain:
                    continue

                addresses = wallet.get("walletAddresses", [])
                if addresses and wallet_chain:
                    result[wallet_chain] = addresses

            return result
        except Exception as e:
            logger.error(f"Error getting all wallet addresses: {e}")
            return {}

    async def add_wallet(self, chain: str, private_key: str, set_default: bool = True) -> Dict:
        """Add a wallet to Gateway"""
        return await self._request("POST", "wallet/add", json={
            "chain": chain,
            "privateKey": private_key,
            "setDefault": set_default
        })

    async def create_wallet(self, chain: str, set_default: bool = True) -> Dict:
        """Create a new wallet in Gateway"""
        return await self._request("POST", "wallet/create", json={
            "chain": chain,
            "setDefault": set_default
        })

    async def show_private_key(self, chain: str, address: str, passphrase: str) -> Dict:
        """Show private key for a wallet"""
        return await self._request("POST", "wallet/show-private-key", json={
            "chain": chain,
            "address": address,
            "passphrase": passphrase
        })

    async def sign_typed_data(
        self,
        chain: str,
        network: str,
        address: str,
        domain: Dict,
        types: Dict,
        value: Dict,
    ) -> Dict:
        """Sign EIP-712 typed data with a Gateway-managed wallet."""
        return await self._request("POST", "wallet/sign-typed-data", json={
            "chain": chain,
            "network": network,
            "address": address,
            "domain": domain,
            "types": types,
            "value": value,
        })

    async def send_transaction(
        self,
        chain: str,
        network: str,
        address: str,
        to_address: str,
        amount: str,
        live_action_authorization: Optional[Dict[str, Any]] = None,
    ) -> Dict:
        """Send a native token transaction"""
        payload = {
            "chain": chain,
            "network": network,
            "address": address,
            "toAddress": to_address,
            "amount": amount,
        }
        return await self._request("POST", "wallet/send", json=payload)

    async def remove_wallet(self, chain: str, address: str) -> Dict:
        """Remove a wallet from Gateway"""
        return await self._request("DELETE", "wallet/remove", json={
            "chain": chain,
            "address": address
        })

    async def set_default_wallet(self, chain: str, address: str) -> Dict:
        """Set the default wallet for a chain in Gateway"""
        return await self._request("POST", "wallet/setDefault", json={
            "chain": chain,
            "address": address
        })

    async def set_marlin_default_wallet(
        self,
        chain: str,
        network: str,
        address: str,
        wallet_ref: str,
    ) -> Dict:
        """Set the mnemonic-derived Marlin default wallet in Gateway."""
        return await self._request("POST", "wallet/marlin-default", json={
            "chain": chain,
            "network": network,
            "address": address,
            "walletRef": wallet_ref
        })

    async def get_balances(self, chain: str, network: str, address: str, tokens: Optional[List[str]] = None) -> Dict:
        """Get token balances for a wallet"""
        return await self._request("POST", f"chains/{chain}/balances", json={
            "network": network,
            "address": address,
            "tokens": tokens if tokens is not None else []
        })

    async def get_allowances(
        self,
        chain: str,
        network: str,
        address: str,
        spender: str,
        tokens: Optional[List[str]] = None,
    ) -> Dict:
        """Get token allowances for a wallet and spender."""
        return await self._request("POST", f"chains/{chain}/allowances", json={
            "network": network,
            "address": address,
            "spender": spender,
            "tokens": tokens if tokens is not None else []
        })

    async def get_chains(self) -> Dict:
        """Get available chains"""
        return await self._request("GET", "config/chains")

    async def get_default_network(self, chain: str) -> Optional[str]:
        """Get default network for a chain"""
        try:
            config = await self._request("GET", "config", params={"namespace": chain})
            return config.get("defaultNetwork")
        except Exception:
            return None

    async def get_tokens(self, chain: str, network: str) -> Dict:
        """Get available tokens for a chain/network"""
        return await self._request("GET", "tokens", params={
            "chain": chain,
            "network": network
        })

    async def add_token(self, chain: str, network: str, address: str, symbol: str, name: str, decimals: int) -> Dict:
        """Add a custom token to Gateway's token list"""
        return await self._request("POST", "tokens", json={
            "chain": chain,
            "network": network,
            "token": {
                "address": address,
                "symbol": symbol,
                "name": name,
                "decimals": decimals
            }
        })

    async def delete_token(self, chain: str, network: str, token_address: str) -> Dict:
        """Delete a custom token from Gateway's token list"""
        return await self._request("DELETE", f"tokens/{token_address}", params={
            "chain": chain,
            "network": network
        })

    async def save_token(self, chain: str, network: str, token_address: str) -> Dict:
        """Save a token by address - auto-fetches info from GeckoTerminal"""
        chain_network = f"{chain}-{network}"
        return await self._request("POST", f"tokens/save/{token_address}", params={
            "chainNetwork": chain_network
        }, json={})

    async def get_config(self, namespace: str) -> Dict:
        """Get configuration for a specific namespace (connector or chain-network)"""
        return await self._request("GET", "config", params={"namespace": namespace})

    async def update_config(self, namespace: str, path: str, value: any) -> Dict:
        """Update a configuration value for a namespace"""
        return await self._request("POST", "config/update", json={
            "namespace": namespace,
            "path": path,
            "value": value
        })

    async def get_api_keys(self) -> Dict:
        """Get all configured API keys from Gateway"""
        return await self._request("GET", "config", params={"namespace": "apiKeys"})

    async def update_api_keys(self, api_keys: Dict[str, str]) -> List[Dict]:
        """
        Update API keys in Gateway configuration.

        Args:
            api_keys: Dict mapping provider name to API key value
                     (e.g., {"helius": "abc123", "infura": "xyz789"})

        Returns:
            List of results for each API key update
        """
        results = []
        for provider, api_key in api_keys.items():
            result = await self._request("POST", "config/update", json={
                "namespace": "apiKeys",
                "path": provider,
                "value": api_key
            })
            results.append(result)
        return results

    async def get_pools(
        self,
        chain: str,
        network: str,
        connector: Optional[str] = None,
        pool_type: Optional[str] = None,
        search: Optional[str] = None
    ) -> List[Dict]:
        """Get pools for a chain and network with optional filtering"""
        params = {
            "chain": chain,
            "network": network
        }
        if connector:
            params["connector"] = connector
        if pool_type:
            params["type"] = pool_type.lower()
        if search:
            params["search"] = search
        return await self._request("GET", "pools", params=params)

    async def add_pool(
        self,
        chain: str,
        network: str,
        connector: str,
        pool_type: str,
        address: str,
        base_symbol: str,
        quote_symbol: str,
        base_token_address: str,
        quote_token_address: str,
        fee_pct: Optional[float] = None
    ) -> Dict:
        """Add a new pool"""
        payload = {
            "chain": chain,
            "connector": connector,
            "type": pool_type.lower(),  # Gateway expects lowercase (amm, clmm)
            "network": network,
            "address": address,
            "baseSymbol": base_symbol,
            "quoteSymbol": quote_symbol,
            "baseTokenAddress": base_token_address,
            "quoteTokenAddress": quote_token_address
        }
        if fee_pct is not None:
            payload["feePct"] = fee_pct
        return await self._request("POST", "pools", json=payload)

    async def save_pool(self, chain_network: str, address: str) -> Dict:
        """Save a pool by address using GeckoTerminal lookup"""
        return await self._request("POST", f"pools/save/{address}", params={
            "chainNetwork": chain_network
        }, json={})

    async def delete_pool(self, chain: str, network: str, address: str) -> Dict:
        """Delete a pool from Gateway's pool list"""
        return await self._request("DELETE", f"pools/{address}", params={
            "chain": chain,
            "network": network
        })

    async def pool_info(self, connector: str, network: str, pool_address: str) -> Dict:
        """Get detailed information about a specific pool"""
        return await self._request("POST", "clmm/liquidity/pool", json={
            "connector": connector,
            "network": network,
            "poolAddress": pool_address
        })

    # ============================================
    # Swap Operations
    # ============================================

    def _swap_route_type(self, connector: str, pool_address: Optional[str] = None) -> str:
        connector_key = connector.lower()
        if connector_key in self.CLMM_SWAP_CONNECTORS:
            return "clmm"
        if connector_key in self.ROUTER_OR_CLMM_SWAP_CONNECTORS and pool_address:
            return "clmm"
        return "router"

    async def quote_swap(
        self,
        connector: str,
        network: str,
        base_asset: str,
        quote_asset: str,
        amount: float,
        side: str,
        slippage_pct: Optional[float] = None,
        pool_address: Optional[str] = None,
    ) -> Dict:
        """Get a quote for a swap"""
        payload = {
            "network": network,
            "baseToken": base_asset,
            "quoteToken": quote_asset,
            "amount": self._decimal_payload_value(amount),
            "side": side.upper()
        }
        if slippage_pct is not None:
            payload["slippagePct"] = slippage_pct
        if pool_address:
            payload["poolAddress"] = pool_address

        route_type = self._swap_route_type(connector, pool_address)
        return await self._request("GET", f"connectors/{connector}/{route_type}/quote-swap", params=payload)

    async def execute_swap(
        self,
        connector: str,
        network: str,
        wallet_address: str,
        base_asset: str,
        quote_asset: str,
        amount: float,
        side: str,
        slippage_pct: Optional[float] = None,
        pool_address: Optional[str] = None,
        live_action_authorization: Optional[Dict[str, Any]] = None,
        marlin_provider_intent_authorized: bool = False,
    ) -> Dict:
        """Execute a swap"""
        payload = {
            "network": network,
            "walletAddress": wallet_address,
            "baseToken": base_asset,
            "quoteToken": quote_asset,
            "amount": self._decimal_payload_value(amount),
            "side": side.upper()
        }
        if slippage_pct is not None:
            payload["slippagePct"] = slippage_pct
        if pool_address:
            payload["poolAddress"] = pool_address
        headers = None
        if live_action_authorization is not None and marlin_provider_intent_authorized:
            payload["liveActionAuthorization"] = live_action_authorization
            token = self._marlin_gateway_provider_intent_token()
            if token:
                headers = {"x-marlin-gateway-provider-intent-token": token}
        route_type = self._swap_route_type(connector, pool_address)
        return await self._request(
            "POST",
            f"connectors/{connector}/{route_type}/execute-swap",
            json=payload,
            headers=headers,
        )

    async def execute_bridge(
        self,
        provider: str,
        provider_route_id: str,
        quote_id: str,
        route_payload_hash: str,
        source_chain: str,
        source_chain_id: str,
        network: str,
        wallet_address: str,
        tx_target: str,
        tx_value: str,
        tx_calldata: str,
        tx_calldata_hash: str,
        gas_limit: Optional[int] = None,
        live_action_authorization: Optional[Dict[str, Any]] = None,
    ) -> Dict:
        """Execute a Marlin-approved bridge transaction through Gateway."""
        payload = {
            "provider": provider,
            "providerRouteId": provider_route_id,
            "quoteId": quote_id,
            "routePayloadHash": route_payload_hash,
            "sourceChain": source_chain,
            "sourceChainId": source_chain_id,
            "network": network,
            "walletAddress": wallet_address,
            "txTarget": tx_target,
            "txValue": tx_value,
            "txCalldata": tx_calldata,
            "txCalldataHash": tx_calldata_hash,
        }
        if gas_limit is not None:
            payload["gasLimit"] = gas_limit
        if live_action_authorization is not None:
            payload["liveActionAuthorization"] = live_action_authorization
        return await self._request("POST", "bridge/execute", json=payload)

    async def build_treasury_rebalance(
        self,
        *,
        idempotency_key: str,
        wallet_address: str,
        destination_address: str,
        amount: str,
    ) -> Dict:
        """Build a provider-owned treasury rebalance through Gateway."""
        payload = {
            "provider": "hyperliquid_bridge2",
            "idempotencyKey": idempotency_key,
            "mode": "mainnet",
            "sourceChain": "ethereum",
            "sourceNetwork": "arbitrum",
            "sourceAsset": "USDC",
            "destinationVenue": "hyperliquid",
            "destinationAsset": "USDC",
            "walletAddress": wallet_address,
            "destinationAddress": destination_address,
            "amount": amount,
        }
        return await self._request("POST", "bridge/rebalance/build", json=payload)

    async def execute_treasury_rebalance(
        self,
        *,
        idempotency_key: str,
        wallet_address: str,
        destination_address: str,
        amount: str,
        live_action_authorization: Optional[Dict[str, Any]] = None,
        marlin_provider_intent_authorized: bool = False,
    ) -> Dict:
        """Execute a provider-owned treasury rebalance through Gateway."""
        payload = {
            "provider": "hyperliquid_bridge2",
            "idempotencyKey": idempotency_key,
            "mode": "mainnet",
            "sourceChain": "ethereum",
            "sourceNetwork": "arbitrum",
            "sourceAsset": "USDC",
            "destinationVenue": "hyperliquid",
            "destinationAsset": "USDC",
            "walletAddress": wallet_address,
            "destinationAddress": destination_address,
            "amount": amount,
        }
        headers = None
        if live_action_authorization is not None and marlin_provider_intent_authorized:
            payload["liveActionAuthorization"] = live_action_authorization
            token = self._marlin_gateway_provider_intent_token()
            if token:
                headers = {"x-marlin-gateway-provider-intent-token": token}
        return await self._request("POST", "bridge/rebalance/execute", json=payload, headers=headers)

    async def get_treasury_rebalance(self, rebalance_id: str) -> Dict:
        """Fetch provider-owned treasury rebalance status from Gateway."""
        return await self._request("GET", f"bridge/rebalance/{rebalance_id}")

    async def execute_quote(
        self,
        connector: str,
        network: str,
        wallet_address: str,
        quote_id: str
    ) -> Dict:
        """Execute a previously obtained quote"""
        return await self._request("POST", f"connectors/{connector}/router/execute-quote", json={
            "network": network,
            "walletAddress": wallet_address,
            "quoteId": quote_id
        })

    async def router_add_liquidity(
        self,
        connector: str,
        network: str,
        wallet_address: str,
        token_a: str,
        token_b: str,
        amount_a: float,
        amount_b: float,
        pool_type: str,
        slippage_pct: Optional[float] = None,
        live_action_authorization: Optional[Dict[str, Any]] = None,
    ) -> Dict:
        """Add liquidity through a Gateway router connector"""
        payload = {
            "network": network,
            "walletAddress": wallet_address,
            "tokenA": token_a,
            "tokenB": token_b,
            "amountA": self._decimal_payload_value(amount_a),
            "amountB": self._decimal_payload_value(amount_b),
            "poolType": pool_type
        }
        if slippage_pct is not None:
            payload["slippagePct"] = slippage_pct
        return await self._request("POST", f"connectors/{connector}/router/add-liquidity", json=payload)

    async def router_remove_liquidity(
        self,
        connector: str,
        network: str,
        wallet_address: str,
        token_a: str,
        token_b: str,
        liquidity: float,
        pool_type: str,
        slippage_pct: Optional[float] = None,
        live_action_authorization: Optional[Dict[str, Any]] = None,
    ) -> Dict:
        """Remove liquidity through a Gateway router connector"""
        payload = {
            "network": network,
            "walletAddress": wallet_address,
            "tokenA": token_a,
            "tokenB": token_b,
            "liquidity": self._decimal_payload_value(liquidity),
            "poolType": pool_type
        }
        if slippage_pct is not None:
            payload["slippagePct"] = slippage_pct
        return await self._request("POST", f"connectors/{connector}/router/remove-liquidity", json=payload)

    # ============================================
    # Liquidity Operations - CLMM (Concentrated Liquidity)
    # ============================================

    async def clmm_open_position(
        self,
        connector: str,
        network: str,
        wallet_address: str,
        pool_address: str,
        lower_price: float,
        upper_price: float,
        base_token_amount: Optional[float] = None,
        quote_token_amount: Optional[float] = None,
        slippage_pct: Optional[float] = None,
        extra_params: Optional[Dict] = None,
        live_action_authorization: Optional[Dict[str, Any]] = None,
    ) -> Dict:
        """Open a NEW CLMM position with initial liquidity"""
        payload = {
            "network": network,
            "walletAddress": wallet_address,
            "poolAddress": pool_address,
            "lowerPrice": lower_price,
            "upperPrice": upper_price
        }
        if base_token_amount is not None:
            payload["baseTokenAmount"] = str(base_token_amount)
        if quote_token_amount is not None:
            payload["quoteTokenAmount"] = str(quote_token_amount)
        if slippage_pct is not None:
            payload["slippagePct"] = slippage_pct

        # Add any connector-specific parameters
        if extra_params:
            payload.update(extra_params)
        return await self._request("POST", f"connectors/{connector}/clmm/open-position", json=payload)

    async def clmm_add_liquidity(
        self,
        connector: str,
        network: str,
        wallet_address: str,
        position_address: str,
        base_token_amount: Optional[float] = None,
        quote_token_amount: Optional[float] = None,
        slippage_pct: Optional[float] = None,
        live_action_authorization: Optional[Dict[str, Any]] = None,
    ) -> Dict:
        """Add more liquidity to an existing CLMM position"""
        payload = {
            "connector": connector,
            "network": network,
            "address": wallet_address,
            "positionAddress": position_address
        }
        if base_token_amount is not None:
            payload["baseTokenAmount"] = str(base_token_amount)
        if quote_token_amount is not None:
            payload["quoteTokenAmount"] = str(quote_token_amount)
        if slippage_pct is not None:
            payload["slippagePct"] = slippage_pct
        return await self._request("POST", "clmm/liquidity/add", json=payload)

    async def clmm_close_position(
        self,
        connector: str,
        network: str,
        wallet_address: str,
        position_address: str,
        live_action_authorization: Optional[Dict[str, Any]] = None,
    ) -> Dict:
        """Close a CLMM position completely"""
        payload = {
            "network": network,
            "walletAddress": wallet_address,
            "positionAddress": position_address,
        }
        return await self._request("POST", f"connectors/{connector}/clmm/close-position", json=payload)

    async def clmm_remove_liquidity(
        self,
        connector: str,
        network: str,
        wallet_address: str,
        position_address: str,
        percentage: float,
        live_action_authorization: Optional[Dict[str, Any]] = None,
    ) -> Dict:
        """Remove liquidity from a CLMM position (partial)"""
        payload = {
            "connector": connector,
            "network": network,
            "address": wallet_address,
            "positionAddress": position_address,
            "percentage": percentage,
        }
        return await self._request("POST", "clmm/liquidity/remove", json=payload)

    async def clmm_position_info(
        self,
        connector: str,
        chain_network: str,
        position_address: str
    ) -> Dict:
        """
        Get CLMM position information including pending fees.

        Note: Gateway returns 500 instead of 404 when position doesn't exist (is closed).
        Callers should treat 500 errors as "position not found/closed".
        """
        # Validate required parameters
        if not connector:
            raise ValueError("connector is required for clmm_position_info")
        if not chain_network:
            raise ValueError("chain_network is required for clmm_position_info")
        if not position_address:
            raise ValueError("position_address is required for clmm_position_info")

        params = {
            "connector": connector,
            "chainNetwork": chain_network,
            "positionAddress": position_address
        }
        return await self._request("GET", "trading/clmm/position-info", params=params)

    async def clmm_positions_owned(
        self,
        connector: str,
        chain_network: str,
        wallet_address: str,
        pool_address: Optional[str] = None
    ) -> List[Dict]:
        """
        Get CLMM positions owned by a wallet.

        Args:
            connector: CLMM connector (e.g., 'meteora', 'raydium')
            chain_network: Chain and network in format 'chain-network' (e.g., 'solana-mainnet-beta')
            wallet_address: Wallet address to query
            pool_address: Optional pool address to filter positions.
                         If not provided, returns ALL positions across all pools.

        Returns:
            List of position dictionaries with fields like:
            - address: Position NFT address
            - poolAddress: Pool address
            - baseTokenAddress, quoteTokenAddress
            - baseTokenAmount, quoteTokenAmount
            - baseFeeAmount, quoteFeeAmount
            - lowerBinId, upperBinId
            - lowerPrice, upperPrice, price
        """
        params = {
            "connector": connector,
            "chainNetwork": chain_network,
            "walletAddress": wallet_address,
        }

        # Only add poolAddress if specified (allows fetching all positions)
        if pool_address:
            params["poolAddress"] = pool_address

        return await self._request("GET", "trading/clmm/positions-owned", params=params)

    async def clmm_collect_fees(
        self,
        connector: str,
        network: str,
        wallet_address: str,
        position_address: str,
        live_action_authorization: Optional[Dict[str, Any]] = None,
    ) -> Dict:
        """Collect accumulated fees from a CLMM position"""
        payload = {
            "network": network,
            "address": wallet_address,
            "positionAddress": position_address,
        }
        return await self._request("POST", f"connectors/{connector}/clmm/collect-fees", json=payload)

    async def clmm_pool_info(
        self,
        connector: str,
        network: str,
        pool_address: str
    ) -> Dict:
        """Get detailed CLMM pool information by pool address"""
        return await self._request("GET", f"connectors/{connector}/clmm/pool-info", params={
            "network": network,
            "poolAddress": pool_address
        })

    # ============================================
    # Transaction Polling
    # ============================================

    async def poll_transaction(
        self,
        network_id: str,
        tx_hash: str,
    ) -> Optional[Dict]:
        """
        Poll transaction status on blockchain.

        Args:
            network_id: Network ID in format 'chain-network' (e.g., 'solana-mainnet-beta', 'ethereum-mainnet')
            tx_hash: Transaction hash/signature

        Returns:
            Transaction status dict with fields:
            - txStatus: 1 for confirmed, 0 for pending, -1 for failed
            - fee: Transaction fee amount
            - error: Parsed error message if transaction failed (e.g., "SLIPPAGE_EXCEEDED (0x1771): ...")
            - txData: Full transaction data including meta.err
            Returns None if Gateway is unavailable or request fails.
        """
        try:
            # Split network_id into chain and network
            parts = network_id.split('-', 1)
            if len(parts) != 2:
                logger.error(f"Invalid network_id format: {network_id}. Expected 'chain-network'")
                return None

            chain, network = parts

            payload = {
                "network": network,
                "signature": tx_hash
            }

            return await self._request("POST", f"chains/{chain}/poll", json=payload)
        except Exception as e:
            logger.error(f"Error polling transaction {tx_hash}: {e}")
            return None

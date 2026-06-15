from typing import Any, Dict, Optional


def gateway_poll_error_detail(result: Dict[str, Any]) -> Optional[str]:
    error = result.get("error")
    return str(error) if error else None

"""Interface web local do verify-bridge (zero dependencias externas)."""

from .server import VerifyBridgeServer, create_server, run_ui

__all__ = ["VerifyBridgeServer", "create_server", "run_ui"]

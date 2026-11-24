"""Vibe-Trading sidecar integration (HKUDS/Vibe-Trading over MCP Streamable HTTP). See sidecar/vibe-trading/README.md."""

from app.vibe.client import VibeClient, VibeError, VibeNotFound, VibeUnavailable, get_client, reset_client

__all__ = ["VibeClient", "VibeError", "VibeNotFound", "VibeUnavailable", "get_client", "reset_client"]

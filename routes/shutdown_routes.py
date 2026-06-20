"""Shutdown endpoint — allows authenticated frontend to stop the server."""
import asyncio
import logging
import os
import signal

from fastapi import APIRouter, HTTPException, Request

from src.auth_helpers import get_current_user

logger = logging.getLogger(__name__)


def setup_shutdown_routes():
    """Set up shutdown endpoint. No dependencies needed."""
    router = APIRouter(prefix="/api", tags=["shutdown"])

    @router.post("/shutdown")
    async def shutdown_server(request: Request):
        """Gracefully shut down the Odysseus server.

        Requires authentication (or auth disabled). Returns a response
        before scheduling the shutdown to avoid the caller getting a
        connection error.
        """
        user = get_current_user(request)
        # If auth is enabled and no user, deny
        from core.auth import AuthManager
        auth_enabled = os.getenv("AUTH_ENABLED", "true").lower() in ("1", "true", "yes")
        if auth_enabled and not user:
            raise HTTPException(401, "Authentication required")

        logger.warning("Shutdown requested by user=%s — stopping server", user or "anonymous")

        async def _delayed_shutdown():
            await asyncio.sleep(0.5)
            os.kill(os.getpid(), signal.SIGTERM)

        asyncio.create_task(_delayed_shutdown())
        return {"ok": True, "message": "Server shutting down..."}

    return router

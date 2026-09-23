from __future__ import annotations

import logging
from .hermes_9router_web_search.provider import NineRouterWebSearchProvider

logger = logging.getLogger(__name__)

def register(ctx) -> None:
    """Plugin entry point called by Hermes plugin loader."""
    try:
        provider = NineRouterWebSearchProvider()
        ctx.register_web_search_provider(provider)
        logger.info("Registered 9Router web search provider")
    except Exception as e:
        logger.error("Failed to register 9Router web search provider: %s", e)

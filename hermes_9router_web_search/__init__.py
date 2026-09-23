"""Hermes package entry point."""
from .provider import NineRouterWebSearchProvider


def register(ctx) -> None:
    ctx.register_web_search_provider(NineRouterWebSearchProvider())

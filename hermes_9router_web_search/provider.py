"""9Router Web Search & Extract provider for Hermes.

Proxies web_search and web_extract calls to a local 9Router instance
via /v1/search (search-combo) and /v1/web/fetch (fetch-combo).

Config in ~/.hermes/config.yaml:

    web:
      search_backend: 9router
      extract_backend: 9router
      9router:
        base_url: http://127.0.0.1:20128   # default
        api_key: sk-...                      # optional; reads from 9Router DB if blank
        search_model: search-combo           # default
        fetch_model: fetch-combo             # default
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.web_search_provider import WebSearchProvider

logger = logging.getLogger(__name__)

_CONFIG_CACHE: Optional[Dict[str, Any]] = None
_CONFIG_MTIME: float = 0.0


def _load_config() -> Dict[str, Any]:
    """Read 9router section from Hermes config.yaml (cached by mtime)."""
    global _CONFIG_CACHE, _CONFIG_MTIME
    config_path = Path.home() / ".hermes" / "config.yaml"
    try:
        mtime = config_path.stat().st_mtime
    except OSError:
        return {}
    if _CONFIG_CACHE is not None and mtime == _CONFIG_MTIME:
        return _CONFIG_CACHE
    try:
        import yaml
        with open(config_path) as f:
            raw = yaml.safe_load(f) or {}
        _CONFIG_CACHE = raw.get("web", {}).get("9router", {}) or {}
        _CONFIG_MTIME = mtime
    except Exception:
        _CONFIG_CACHE = {}
    return _CONFIG_CACHE


def _base_url() -> str:
    cfg = _load_config()
    url = cfg.get("base_url", "").strip()
    if url:
        return url.rstrip("/")
    return os.getenv("NINEROUTER_URL", "http://127.0.0.1:20128").rstrip("/")


def _api_key() -> str:
    """Resolve 9Router API key: config → env → DB."""
    cfg = _load_config()
    key = cfg.get("api_key", "").strip()
    if key:
        return key
    key = os.getenv("NINEROUTER_KEY", "").strip()
    if key:
        return key
    return _read_db_key()


_DB_KEY_CACHE: Optional[str] = None
_DB_KEY_TIME: float = 0.0


def _read_db_key() -> str:
    """Read first API key from 9Router SQLite DB."""
    global _DB_KEY_CACHE, _DB_KEY_TIME
    import time
    now = time.time()
    if _DB_KEY_CACHE and now - _DB_KEY_TIME < 60:
        return _DB_KEY_CACHE
    db_path = Path.home() / ".9router" / "db" / "data.sqlite"
    if not db_path.exists():
        return ""
    try:
        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT key FROM apiKeys LIMIT 1").fetchone()
        conn.close()
        _DB_KEY_CACHE = row[0] if row else ""
        _DB_KEY_TIME = now
    except Exception as e:
        logger.debug("Failed to read 9Router DB key: %s", e)
        _DB_KEY_CACHE = ""
        _DB_KEY_TIME = now
    return _DB_KEY_CACHE


def _search_model() -> str:
    cfg = _load_config()
    return cfg.get("search_model", "search-combo")


def _fetch_model() -> str:
    cfg = _load_config()
    return cfg.get("fetch_model", "fetch-combo")


def _post(endpoint: str, payload: dict, timeout: int = 30) -> dict:
    """POST to 9Router and return parsed JSON."""
    import httpx
    url = f"{_base_url()}{endpoint}"
    headers = {"Content-Type": "application/json"}
    key = _api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    resp = httpx.post(url, json=payload, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


class NineRouterWebSearchProvider(WebSearchProvider):
    """Proxy web search & extract to 9Router /v1/search and /v1/web/fetch."""

    @property
    def name(self) -> str:
        return "9router"

    @property
    def display_name(self) -> str:
        return "9Router"

    def is_available(self) -> bool:
        # Available if base_url is set or 9Router DB exists
        return bool(_base_url())

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return True

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        """Search via 9Router /v1/search."""
        payload = {
            "model": _search_model(),
            "query": query,
            "max_results": max(1, min(int(limit), 20)),
        }
        try:
            data = _post("/v1/search", payload)
        except Exception as exc:
            logger.error("9Router search failed: %s", exc)
            return {"success": False, "error": f"9Router search failed: {exc}"}

        if "error" in data:
            msg = data["error"]
            if isinstance(msg, dict):
                msg = msg.get("message", str(msg))
            return {"success": False, "error": msg}

        results = data.get("results", [])
        web = []
        for r in results:
            web.append({
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "description": r.get("snippet", "") or r.get("content", "") or "",
                "position": r.get("position", 0),
            })
        return {"success": True, "data": {"web": web}}

    def extract(self, urls: List[str], **kwargs: Any) -> Any:
        """Fetch page content via 9Router /v1/web/fetch."""
        results = []
        for url in urls:
            payload = {
                "model": _fetch_model(),
                "url": url,
            }
            try:
                data = _post("/v1/web/fetch", payload, timeout=45)
            except Exception as exc:
                logger.error("9Router fetch failed for %s: %s", url, exc)
                results.append({
                    "url": url,
                    "title": "",
                    "content": "",
                    "raw_content": "",
                    "error": f"9Router fetch failed: {exc}",
                })
                continue

            if "error" in data:
                msg = data["error"]
                if isinstance(msg, dict):
                    msg = msg.get("message", str(msg))
                results.append({
                    "url": url,
                    "title": "",
                    "content": "",
                    "raw_content": "",
                    "error": msg,
                })
                continue

            content_obj = data.get("content", {})
            if isinstance(content_obj, dict):
                text = content_obj.get("text", "")
            else:
                text = str(content_obj)

            results.append({
                "url": data.get("url", url),
                "title": data.get("title", ""),
                "content": text,
                "raw_content": text,
                "metadata": data.get("metadata", {}),
            })

        return {"success": True, "data": results}

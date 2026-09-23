"""Profile-scoped 9Router search/fetch adapter. No independent credential cache."""
from __future__ import annotations

import asyncio
from contextlib import closing
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit

import httpx
from agent.web_search_provider import WebSearchProvider

LOCAL_URL = "http://127.0.0.1:20128"


def _load_config() -> dict:
    from hermes_cli.config_effective import load_user_config_effective
    try:
        raw = load_user_config_effective(fail_closed=True)
    except Exception:
        raise ValueError("Cannot read active profile configuration") from None
    web = raw.get("web", {})
    if not isinstance(web, dict) or not isinstance(web.get("9router", {}), dict):
        raise ValueError("web.9router must be a mapping")
    return web.get("9router", {})


def _env(name: str) -> str:
    from agent.secret_scope import (get_secret, current_secret_scope,
                                    serves_routed_profile, UnscopedSecretError)
    if serves_routed_profile():
        scope = current_secret_scope()
        if scope is None:
            raise UnscopedSecretError(name)
        return scope.get(name, "") or ""
    return get_secret(name, "") or ""


def _string(cfg: dict, key: str, default: str = "") -> str:
    value = cfg.get(key, default)
    if not isinstance(value, str):
        raise ValueError(f"web.9router.{key} must be a string")
    return value.strip()


def _base_url() -> str:
    url = (_string(_load_config(), "base_url") or _env("NINEROUTER_URL") or LOCAL_URL).rstrip("/")
    try:
        parsed = urlsplit(url)
        parsed.port  # Validate malformed/out-of-range ports.
        local = parsed.hostname in {"127.0.0.1", "::1", "localhost"}
        valid = (parsed.scheme == "https" or (parsed.scheme == "http" and local))
        valid = valid and parsed.hostname and not (parsed.username or parsed.password or parsed.query or parsed.fragment)
    except ValueError:
        valid = False
    if not valid or any(ch.isspace() for ch in url):
        raise ValueError("9Router URL requires HTTPS (HTTP only for loopback), no credentials/query/fragment")
    return url


def _read_db_key() -> str:
    db = Path.home() / ".9router/db/data.sqlite"
    if not db.is_file():
        return ""
    try:
        with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=2)) as conn:
            row = conn.execute("SELECT key FROM apiKeys WHERE isActive = 1 ORDER BY id LIMIT 1").fetchone()
        return row[0] if row and isinstance(row[0], str) else ""
    except sqlite3.Error:
        raise ValueError("Cannot read active 9Router key from local database") from None


def _api_key() -> str:
    from hermes_constants import get_hermes_home
    cfg = _load_config()
    key = _string(cfg, "api_key") or _env("NINEROUTER_KEY")
    if key:
        return key
    # Only the default profile inherits historical local DB discovery. Other
    # profiles must explicitly opt in; remote origins never receive DB keys.
    default_home = Path.home() / ".hermes"
    auto = cfg.get("use_local_db_key", get_hermes_home().resolve() == default_home.resolve())
    if not isinstance(auto, bool):
        raise ValueError("web.9router.use_local_db_key must be boolean")
    if auto and _base_url() == LOCAL_URL:
        return _read_db_key()
    return ""


def _headers() -> dict:
    key = _api_key()
    return {"Authorization": f"Bearer {key}"} if key else {}


def _model(kind: str) -> str:
    return _string(_load_config(), kind + "_model", kind + "-combo") or kind + "-combo"


def _decode(response: httpx.Response) -> dict:
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("9Router response must be an object")
    if data.get("error") or data.get("success") is False:
        raise ValueError("9Router reported an upstream error")
    return data


def _post(endpoint: str, payload: dict) -> dict:
    with httpx.Client(timeout=30, follow_redirects=False, trust_env=False) as client:
        return _decode(client.post(_base_url() + endpoint, json=payload, headers=_headers()))


def _error(exc: Exception) -> str:
    # Never include upstream bodies, request URLs, config text, or credentials.
    if isinstance(exc, httpx.HTTPStatusError):
        return f"9Router HTTP {exc.response.status_code}"
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return "9Router request timed out"
    return f"9Router request failed ({type(exc).__name__}); check configuration or upstream response"


def _http_url(url: object) -> bool:
    if not isinstance(url, str):
        return False
    try:
        p = urlsplit(url)
        return bool(p.scheme in {"http", "https"} and p.hostname and not (p.username or p.password) and not any(c.isspace() for c in url))
    except ValueError:
        return False


async def _check_url(url: str) -> None:
    from tools.url_safety import async_is_safe_url
    from tools.website_policy import check_website_access
    if not _http_url(url) or not await async_is_safe_url(url):
        raise PermissionError("Blocked by URL safety policy")
    if check_website_access(url):
        raise PermissionError("Blocked by website policy")


class NineRouterWebSearchProvider(WebSearchProvider):
    @property
    def name(self):
        return "9router"

    @property
    def display_name(self):
        return "9Router"

    def is_available(self):
        try:
            from hermes_cli.config_effective import load_user_config_effective
            raw = load_user_config_effective(fail_closed=True)
            web = raw.get("web", {})
            configured = (web.get("search_backend") == self.name or web.get("extract_backend") == self.name
                          or bool(web.get("9router")) or bool(_env("NINEROUTER_URL")))
            return bool(configured and _base_url())
        except Exception:
            return False

    def supports_search(self):
        return True

    def supports_extract(self):
        return True

    def search(self, query: str, limit: int = 5) -> dict:
        try:
            if not isinstance(query, str) or not query.strip() or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
                raise ValueError("Invalid search query or limit")
            data = _post("/v1/search", {"model": _model("search"), "query": query, "max_results": limit})
            rows = data.get("results")
            if not isinstance(rows, list) or (not rows and data.get("errors")):
                raise ValueError("Invalid or failed search response")
            web = []
            for row in rows[:limit]:
                if not isinstance(row, dict) or not _http_url(row.get("url")):
                    raise ValueError("Invalid search result")
                for field in ("title", "snippet", "content"):
                    if row.get(field) is not None and not isinstance(row[field], str):
                        raise ValueError("Invalid search text")
                title = row.get("title") or ""
                description = row.get("snippet") or row.get("content") or ""
                if not isinstance(title, str) or not isinstance(description, str):
                    raise ValueError("Invalid search text")
                web.append({"title": title, "url": row["url"], "description": description,
                            "position": len(web) + 1})
            return {"success": True, "data": {"web": web}}
        except Exception as exc:
            return {"success": False, "error": _error(exc)}

    async def extract(self, urls: list[str], **kwargs) -> list[dict]:
        results = []
        format_ = kwargs.get("format") or "markdown"
        # ponytail: sequential async requests keep ordering and cancellation simple;
        # add bounded concurrency only if measured batch latency requires it.
        async with httpx.AsyncClient(timeout=45, follow_redirects=False, trust_env=False) as client:
            for url in urls:
                try:
                    if format_ not in {"markdown", "text", "html"}:
                        raise ValueError("Unsupported extraction format")
                    await _check_url(url)
                    payload = {"model": _model("fetch"), "url": url, "format": format_}
                    response = await asyncio.wait_for(client.post(_base_url() + "/v1/web/fetch", json=payload, headers=_headers()), timeout=45)
                    data = _decode(response)
                    if data.get("errors"):
                        raise ValueError("Upstream fetch errors")
                    final_url = data.get("url") or url
                    await _check_url(final_url)
                    content = data.get("content")
                    text = content.get("text") if isinstance(content, dict) else content
                    if not isinstance(text, str) or not text.strip():
                        raise ValueError("Missing or invalid extracted text")
                    title = data.get("title") or ""
                    if not isinstance(title, str):
                        raise ValueError("Invalid extracted title")
                    results.append({"url": final_url, "title": title, "content": text, "raw_content": text,
                                    "metadata": data.get("metadata") if isinstance(data.get("metadata"), dict) else {}})
                except PermissionError as exc:
                    results.append({"url": url, "title": "", "content": "", "error": str(exc), "blocked_by_policy": True})
                except Exception as exc:
                    results.append({"url": url, "title": "", "content": "", "error": _error(exc)})
        return results

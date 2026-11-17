"""Profile-scoped 9Router search/fetch adapter. No independent credential cache."""
from __future__ import annotations

import asyncio
from contextlib import closing
from pathlib import Path
import sqlite3
from time import monotonic
from urllib.parse import urlsplit

import httpx
from agent.web_search_provider import WebSearchProvider

LOCAL_URL = "http://127.0.0.1:20128"
FETCH_TIMEOUT = 45.0


class _PolicyRefusal(PermissionError):
    """Only fixed, public messages from the URL gate may use this marker."""


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


def _base_url(cfg: dict | None = None) -> str:
    cfg = _load_config() if cfg is None else cfg
    url = (_string(cfg, "base_url") or _env("NINEROUTER_URL") or LOCAL_URL).rstrip("/")
    try:
        parsed = urlsplit(url)
        parsed.port  # Validate malformed/out-of-range ports.
        local = parsed.hostname in {"127.0.0.1", "::1", "localhost"}
        valid = (parsed.scheme == "https" or (parsed.scheme == "http" and local))
        valid = valid and parsed.hostname and parsed.username is None and parsed.password is None and not (parsed.query or parsed.fragment)
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


def _api_key(cfg: dict | None = None, base_url: str | None = None) -> str:
    from hermes_constants import get_hermes_home
    cfg = _load_config() if cfg is None else cfg
    base_url = _base_url(cfg) if base_url is None else base_url
    key = _string(cfg, "api_key") or _env("NINEROUTER_KEY")
    if key:
        return key
    # Only the default profile inherits historical local DB discovery. Other
    # profiles must explicitly opt in; remote origins never receive DB keys.
    default_home = Path.home() / ".hermes"
    auto = cfg.get("use_local_db_key", get_hermes_home().resolve() == default_home.resolve())
    if not isinstance(auto, bool):
        raise ValueError("web.9router.use_local_db_key must be boolean")
    if auto and base_url == LOCAL_URL:
        return _read_db_key()
    return ""


def _headers(cfg: dict | None = None, base_url: str | None = None) -> dict:
    key = _api_key(cfg, base_url)
    return {"Authorization": f"Bearer {key}"} if key else {}


def _model(kind: str, cfg: dict) -> str:
    return _string(cfg, kind + "_model", kind + "-combo") or kind + "-combo"


def _request_settings(kind: str) -> tuple[str, dict, str]:
    # One ephemeral snapshot binds DB eligibility to the destination actually used.
    cfg = _load_config()
    base_url = _base_url(cfg)
    return base_url, _headers(cfg, base_url), _model(kind, cfg)


def _decode(response: httpx.Response) -> dict:
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("9Router response must be an object")
    if data.get("error") or data.get("success") is False:
        raise ValueError("9Router reported an upstream error")
    return data


def _post(endpoint: str, payload: dict) -> dict:
    base_url, headers, model = _request_settings("search")
    with httpx.Client(timeout=30, follow_redirects=False, trust_env=False) as client:
        return _decode(client.post(base_url + endpoint, json={**payload, "model": model}, headers=headers))


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
        p.port  # Reject malformed/out-of-range ports in search results too.
        return bool(p.scheme in {"http", "https"} and p.hostname and p.username is None
                    and p.password is None and not any(c.isspace() for c in url))
    except ValueError:
        return False


def _remaining(deadline: float) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError
    return remaining


async def _check_url(url: str, deadline: float) -> None:
    try:
        from tools.url_safety import async_is_safe_url
        from tools.website_policy import check_website_access
        if not _http_url(url):
            raise _PolicyRefusal("Blocked by URL safety policy")
        timeout = _remaining(deadline)
        if not await asyncio.wait_for(async_is_safe_url(url), timeout=timeout):
            raise _PolicyRefusal("Blocked by URL safety policy")
        if check_website_access(url):
            raise _PolicyRefusal("Blocked by website policy")
        _remaining(deadline)
    except _PolicyRefusal:
        raise
    except TimeoutError:
        raise _PolicyRefusal("URL safety or website policy check timed out") from None
    except Exception:
        # A missing/broken checker is a refusal, never a rescueable fetch failure.
        raise _PolicyRefusal("Cannot verify URL safety or website policy") from None


async def _extract_one(client: httpx.AsyncClient, url: str, format_: str) -> dict:
    deadline = monotonic() + FETCH_TIMEOUT
    if format_ not in {"markdown", "text", "html"}:
        raise ValueError("Unsupported extraction format")
    await _check_url(url, deadline)
    base_url, headers, model = _request_settings("fetch")
    payload = {"model": model, "url": url, "format": format_}
    # The same deadline covers both gates, preparation, POST and response mapping.
    # Synchronous native/local work is cooperative: re-check before sending/returning.
    timeout = _remaining(deadline)
    response = await asyncio.wait_for(
        client.post(base_url + "/v1/web/fetch", json=payload, headers=headers),
        timeout=timeout,
    )
    data = _decode(response)
    if data.get("errors"):
        raise ValueError("Upstream fetch errors")
    final_url = data.get("url") or url
    await _check_url(final_url, deadline)
    content = data.get("content")
    text = content.get("text") if isinstance(content, dict) else content
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Missing or invalid extracted text")
    title = data.get("title") or ""
    if not isinstance(title, str):
        raise ValueError("Invalid extracted title")
    metadata = dict(data["metadata"]) if isinstance(data.get("metadata"), dict) else {}
    # Hermes uses sourceURL to associate redirect content with the requested cache key.
    metadata["sourceURL"] = url
    _remaining(deadline)
    return {"url": final_url, "title": title, "content": text, "raw_content": text,
            "metadata": metadata}


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
            configured = (any(isinstance(web.get(key), str) and web[key].strip().lower() == self.name
                              for key in ("backend", "search_backend", "extract_backend"))
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
            data = _post("/v1/search", {"query": query, "max_results": limit})
            rows = data.get("results")
            if not isinstance(rows, list) or (not rows and data.get("errors")):
                raise ValueError("Invalid or failed search response")
            web = []
            for row in rows[:limit]:
                if not isinstance(row, dict) or not _http_url(row.get("url")):
                    raise ValueError("Invalid search result")
                for field in ("title", "snippet"):
                    if row.get(field) is not None and not isinstance(row[field], str):
                        raise ValueError("Invalid search text")
                content = row.get("content")
                if isinstance(content, dict):
                    content = content.get("text")
                    if not isinstance(content, str):
                        raise ValueError("Invalid search content")
                elif content is not None and not isinstance(content, str):
                    raise ValueError("Invalid search content")
                title = row.get("title") or ""
                description = row.get("snippet") or content or ""
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
        # Sequential requests preserve order and never start a new POST after cancellation.
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT, follow_redirects=False, trust_env=False) as client:
            for url in urls:
                try:
                    results.append(await _extract_one(client, url, format_))
                except _PolicyRefusal as exc:
                    results.append({"url": url, "title": "", "content": "", "error": str(exc), "blocked_by_policy": True})
                except Exception as exc:
                    results.append({"url": url, "title": "", "content": "", "error": _error(exc)})
        return results

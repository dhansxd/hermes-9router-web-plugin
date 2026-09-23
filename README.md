# hermes-9router-web-plugin

Profile-scoped 9Router Web Search & Fetch backend for Hermes Agent.
Requires a current Hermes installation exposing `load_user_config_effective`,
`agent.secret_scope`, URL safety and website-policy APIs. Missing APIs fail closed.

## Installation

```bash
git clone https://github.com/dhansxd/hermes-9router-web-plugin.git ~/.hermes/plugins/web/9router-web
hermes plugins enable 9router-web
```

Directory and Python package entry points share the same registration function.
Reloading a running gateway requires its owner to restart it.

## Configuration

In the **active profile's** `config.yaml`:

```yaml
web:
  search_backend: 9router
  extract_backend: 9router
  9router:
    base_url: http://127.0.0.1:20128
    search_model: search-combo
    fetch_model: fetch-combo
    # api_key: ${NINEROUTER_KEY}
    # use_local_db_key: false
```

Credentials: config `api_key`, then profile-scoped `NINEROUTER_KEY`.
URL: config `base_url`, then profile-scoped `NINEROUTER_URL`, then loopback default.
Default profile preserves automatic local SQLite key lookup. Other profiles must
explicitly enable `use_local_db_key: true` or supply their own key. DB lookup is
allowed ONLY for exact `http://127.0.0.1:20128`, read-only, selecting an active key
in deterministic ID order. No credential cache. Remote routers never borrow DB keys.
HTTP is allowed only for loopback endpoints; remote routers require HTTPS.
Credential-bearing endpoint URLs, queries and fragments are rejected. Environment
HTTP proxies and automatic router redirects are disabled.

## Behavior and security boundaries

- Search accepts limits 1–100; upstream providers can return fewer results.
- Fetch is asynchronous and cancellable, preserves order, and honors markdown/text/html.
- Each fetch request has a 45-second wall-clock deadline. Hermes supplies the overall
  batch timeout; cancellation stops starting further requests. Cancellation cannot
  guarantee cancellation of work already running inside 9Router/upstream services.
- Initial and reported final page URLs pass Hermes URL safety and website policy.
  Blocking returns `blocked_by_policy` to prevent keyless policy rescue.
- **Upstream redirect hops remain 9Router/provider responsibility.** Checking a
  reported final URL prevents returning blocked content, not a remote server's
  already-completed request. Hidden redirect destinations cannot be verified here.
- Private-URL allowance and website blocklists follow active Hermes configuration;
  the plugin does not silently change those settings.
- Malformed responses fail explicitly; errors omit upstream bodies and secrets.
- 9Router handles combo fallback. Hermes may additionally rescue failed calls via
  its keyless providers. Set `web.keyless_rescue: false` yourself if routing must
  remain exclusively through 9Router; this plugin does not alter that setting.
- Website content remains untrusted data, not agent instructions.

## Regression checks

No extra test dependency; run with Hermes's Python environment:

```bash
PYTHONPATH=/path/to/hermes-agent /path/to/hermes-agent/.venv/bin/python -m unittest -v test_provider
```

Tests use synthetic keys and mock HTTP transport, not real hostile URLs.

## License

MIT

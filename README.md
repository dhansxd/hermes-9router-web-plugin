# hermes-9router-web-plugin

Version 1.1.1. An additional, selectable 9Router Web Search & Fetch backend for
Hermes Agent, registered through its official provider API. Hermes core remains
unchanged; other providers can coexist.

Source-reviewed against Hermes Agent 0.23.2
(`ae2a81ccc00e9b7c06301523e5bb09608397149c`) and 9Router 0.5.119
(`1855aa44473e88feac9d3f131b9c0c5fc9706b68`). This is source-contract review,
not a claim of runtime testing. Requires `load_user_config_effective`,
`agent.secret_scope`, URL safety and website-policy APIs. Missing URL-check APIs
refuse extraction.

## Installation and lifecycle

Use the official Hermes plugin manager in the intended profile:

```bash
hermes plugins install dhansxd/hermes-9router-web-plugin --enable
hermes plugins list
```

The **manager plugin ID is `9router-web`**; the **web provider/config namespace is
`9router`**. Directory and Python package entry points share one registration
function. Installing/enabling registers a provider; select it separately below.

```bash
hermes plugins update 9router-web
hermes plugins disable 9router-web
hermes plugins enable 9router-web
hermes plugins remove 9router-web
```

After disabling/removing, select another installed web provider if the profile
still selects `9router`. Reloading a running gateway requires its owner to restart
it. See the [official plugin manager](https://github.com/NousResearch/hermes-agent/blob/ae2a81ccc00e9b7c06301523e5bb09608397149c/hermes_cli/plugins.py)
and [web provider interface](https://github.com/NousResearch/hermes-agent/blob/ae2a81ccc00e9b7c06301523e5bb09608397149c/agent/web_search_provider.py).

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

Hermes also supports the shared `web.backend: 9router` selection; the two
capability-specific keys above take precedence. Choose either capability alone
when the other should use a different provider.

The model values are existing 9Router combo names or supported bare provider
IDs/aliases, not Hermes model selections. The defaults `search-combo` and
`fetch-combo` are 9Router examples and must exist in that router's configuration.
9Router owns provider selection and combo fallback. The adapter sends
`POST /v1/search` with `model/query/max_results` and `POST /v1/web/fetch` with
`model/url/format`; no chat-completions emulation or extra fallback ladder.

Credentials: config `api_key`, then profile-scoped `NINEROUTER_KEY`.
URL: config `base_url`, then profile-scoped `NINEROUTER_URL`, then loopback default.
Each request resolves one configuration snapshot and binds key lookup to that
exact gateway destination. No credential cache.

Default profile preserves automatic local SQLite key lookup. Other profiles must
explicitly enable `use_local_db_key: true` or supply their own key. DB lookup is
allowed ONLY for exact `http://127.0.0.1:20128`, read-only, selecting an active key
in deterministic ID order. Remote routers never borrow DB keys.
HTTP is allowed only for loopback endpoints; remote routers require HTTPS.
Credential-bearing endpoint URLs, queries and fragments are rejected. Environment
HTTP proxies and automatic router redirects are disabled. Authorization headers
go only to the configured gateway, never directly to page URLs.

## Behavior and security boundaries

- Search accepts limits 1–100; upstream providers can return fewer results.
  Normalized `content.text` objects and legacy content strings are supported;
  snippets take precedence for the Hermes description.
- Fetch is asynchronous, sequential, preserves input order, and requests
  markdown/text/html. Individual failures retain their position in the batch.
- Each URL gets one 45-second deadline shared by initial/final URL checks, request
  preparation, HTTP and response handling. Every async wait uses the remaining
  budget. Synchronous local/native helpers are cooperative: the budget is checked
  before sending or returning, but they cannot be forcibly interrupted mid-call.
  Hermes additionally supplies the overall `web.extract_timeout` batch timeout
  (120 seconds by default on the reviewed host).
- Cancellation propagates and stops the batch from starting further requests.
  Already-running native DNS threads, local synchronous work, or work inside
  9Router/upstream services cannot be guaranteed to stop.
- Initial and reported page URLs pass native Hermes URL safety and website policy.
  Refusals and raised/missing checker failures carry `blocked_by_policy` to prevent
  keyless policy rescue. Native private-URL settings and website-policy semantics
  remain authoritative, including its documented handling of malformed policy
  configuration.
- **Upstream redirect hops remain 9Router/provider responsibility.** The reviewed
  9Router fetch normalizer generally reports the submitted URL, so hidden redirect
  destinations cannot be verified here. A reported final-URL check can prevent
  returning blocked content; it cannot undo a request already made remotely.
- Malformed responses fail explicitly; public errors omit upstream bodies,
  request URLs, configuration text and credentials.
- Hermes may rescue failed calls via native keyless providers. Set
  `web.keyless_rescue: false` yourself if routing must remain exclusively through
  9Router; the plugin does not alter that setting.
- Website content remains untrusted data, not agent instructions.

## Regression checks (deferred)

Regression source uses synthetic keys and mocked HTTP transport. No runtime,
lifecycle or live-provider tests were run for this release; no CI was added.
For later execution in an existing Hermes Python environment:

```bash
PYTHONPATH=/path/to/hermes-agent /path/to/hermes-agent/.venv/bin/python -m unittest -v test_provider
```

## License

MIT

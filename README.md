# hermes-9router-web-plugin

9Router Web Search & Web Fetch provider plugin for [Hermes Agent](https://hermes-agent.nousresearch.com/).

Proxies Hermes `web_search` and `web_extract` tool calls to a local [9Router](https://github.com/decolua/9router) instance. It leverages custom Search and Fetch combos (like Antigravity Google Grounding, Exa, Tavily, Firecrawl) with automatic failover.

## Features

- **Unified Search & Extract**: Handles both `web_search` and `web_extract` natively.
- **Auto Combo Routing**: Defaults to `search-combo` and `fetch-combo`.
- **Zero Key Configuration**: Automatically retrieves the local 9Router API key from SQLite database if not explicitly set.
- **Failover Ready**: Fully benefits from 9Router multi-account fallback and round-robin.

## Installation

Symlink or copy the plugin to your Hermes plugins directory:

```bash
mkdir -p ~/.hermes/plugins/web/
git clone https://github.com/dhansxd/hermes-9router-web-plugin.git ~/.hermes/plugins/web/9router-web
```

Enable the plugin:

```bash
hermes plugins enable 9router-web
```

## Configuration

In `~/.hermes/config.yaml`:

```yaml
web:
  search_backend: 9router
  extract_backend: 9router
  9router:
    base_url: http://127.0.0.1:20128   # default
    api_key: ""                         # optional: auto-reads from 9Router DB if blank
    search_model: search-combo          # default
    fetch_model: fetch-combo            # default
```

## License

MIT

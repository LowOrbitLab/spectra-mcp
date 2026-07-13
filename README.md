# spectra_mcp

An MCP server that gives AI agents a stateful anti-detect Firefox powered by
[`invisible_playwright`](https://github.com/feder-cr/invisible_playwright).

Agents can navigate, inspect accessible page snapshots, interact through stable
element refs, fill forms, read text, and capture screenshots without managing
raw Playwright state.

```text
AI agent ── MCP/stdio ──► spectra_mcp ──► patched Firefox
```

## Highlights

- Agent-first `snapshot → ref → action` workflow
- Automatic fingerprint profile, timezone, and locale selection
- Humanized mouse movement and browser-level fingerprint patches
- Typed MCP results and real `isError=true` failures
- Secret redaction and environment-based proxy credentials
- Compact default tool profile for Hermes, OpenClaw, and other agents

## Install

Requires Python 3.11+.

```bash
git clone https://github.com/LowOrbitLab/spectra_mcp.git
cd spectra_mcp
pip install -e .
python -m invisible_playwright fetch
```

The patched Firefox download is approximately 100 MB and only needs to run
once. The browser dependency is pinned to an exact upstream commit.

## MCP configuration

```json
{
  "mcpServers": {
    "spectra_mcp": {
      "command": "/absolute/path/to/venv/bin/spectra_mcp",
      "env": {
        "SPECTRA_MCP_TOOL_PROFILE": "agent"
      }
    }
  }
}
```

Use an absolute executable path when the client filters subprocess environment
variables. Keep parallel tool calls disabled because operations within one
browser session are serialized.

Client-specific examples:

- [Hermes Agent](docs/hermes.md)
- [OpenClaw](docs/openclaw.md)
- [Claude Desktop, Cursor, and OpenCode](docs/configuration.md)

## Agent workflow

1. Call `binary_status`; use `binary_install` if needed.
2. Call `browser_start`.
3. Navigate with `browser_navigate`.
4. Inspect the page with `browser_snapshot`.
5. Act through `browser_click_ref`, `browser_set_value_ref`,
   `browser_type_text_ref`, `browser_select_option_ref`, or
   `browser_set_form_values`.
6. Use `browser_list_tabs` and `browser_activate_tab` when a popup or new tab opens.
7. Use `browser_wait_for` for asynchronous text, URL, or element changes.
8. Read targeted content with `browser_find_text` or paginated `browser_get_text`.
9. Call `browser_stop` when finished.

Mutating ref actions return a compact refreshed snapshot by default; use
`observe="full"` for structured element/frame arrays or `observe="none"` to
skip observation. Agent tools automatically use the only live session, so
models normally do not need to remember a session ID.

## Documentation

- [Tools and profiles](docs/tools.md)
- [Configuration and environment variables](docs/configuration.md)
- [Fingerprint profiles and geo behavior](docs/fingerprinting.md)
- [Security and operational safeguards](docs/security.md)
- [Development and testing](docs/development.md)

## License

MIT. Firefox remains MPL-2.0. This project downloads the patched browser from
upstream releases and does not redistribute it.

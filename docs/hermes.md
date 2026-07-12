# Hermes Agent

Hermes supports stdio MCP servers. Use an absolute executable path because its
subprocess environment is filtered.

```yaml
mcp_servers:
  spectra_mcp:
    command: /absolute/path/to/venv/bin/spectra_mcp
    timeout: 300
    connect_timeout: 30
    supports_parallel_tool_calls: false
    env:
      SPECTRA_MCP_TOOL_PROFILE: agent
      SPECTRA_MCP_DATA_ROOT: /absolute/path/to/spectra-data
```

Recommended behavior:

- Keep `supports_parallel_tool_calls` disabled; one browser session is serialized.
- Prefer `browser_snapshot` and ref tools over CSS selectors or raw HTML.
- Use `browser_get_text` for paginated text.
- Treat `browser_screenshot` as optional: Hermes handles text MCP content more
  reliably than image-only content in current implementations.
- Allow 180–300 seconds for browser startup and slow navigation.

Hermes MCP stderr is normally written to `~/.hermes/logs/mcp-stderr.log`.
Secrets should be supplied through environment variables rather than tool calls.

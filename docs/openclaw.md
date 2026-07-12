# OpenClaw

OpenClaw supports stdio MCP servers. Register an absolute executable path and
select the compact Agent profile.

```text
/absolute/path/to/venv/bin/spectra_mcp
```

Configure these environment variables on the server entry:

```text
SPECTRA_MCP_TOOL_PROFILE=agent
SPECTRA_MCP_DATA_ROOT=/absolute/path/to/spectra-data
```

Recommended behavior:

- Keep `supportsParallelToolCalls` disabled.
- Run OpenClaw's MCP doctor/probe after configuration.
- Increase the embedded MCP idle TTL when browser sessions must survive more
  than roughly ten idle minutes.
- Prefer `browser_snapshot`, `find_text`, and paginated `browser_get_text`.
- Avoid large raw HTML results. OpenClaw may cap tool results around 16K
  characters on smaller model contexts.

Proxy credentials should be configured through `SPECTRA_MCP_PROXY_SERVER`,
`SPECTRA_MCP_PROXY_USERNAME`, and `SPECTRA_MCP_PROXY_PASSWORD` rather than
being placed in model-visible tool arguments.

# Configuration

## Claude Desktop and Cursor

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

## OpenCode

```jsonc
{
  "mcp": {
    "spectra_mcp": {
      "type": "local",
      "command": ["/absolute/path/to/venv/bin/spectra_mcp"],
      "enabled": true,
      "environment": {
        "SPECTRA_MCP_TOOL_PROFILE": "agent"
      }
    }
  }
}
```

The module form is also available:

```json
{"command": "/absolute/path/to/python", "args": ["-m", "spectra_mcp"]}
```

## Proxy and geo defaults

Keep proxy secrets out of model tool-call history with:

```bash
export SPECTRA_MCP_PROXY_SERVER='http://proxy.example:8080'
export SPECTRA_MCP_PROXY_USERNAME='username'
export SPECTRA_MCP_PROXY_PASSWORD='password'
```

Explicit `browser_start`/`start_session` arguments override the environment.
Empty `timezone` and `locale="auto"` derive their values from the proxy/browser
egress IP.

## Tool profile

```bash
export SPECTRA_MCP_TOOL_PROFILE=agent  # setup | agent | core | full
```

The default is `agent`.

## Runtime limits

| Variable | Default |
|---|---:|
| `SPECTRA_MCP_MAX_SESSIONS` | `8` |
| `SPECTRA_MCP_MAX_PAGES_PER_SESSION` | `32` |
| `SPECTRA_MCP_MAX_TEXT_CHARS` | `200000` |
| `SPECTRA_MCP_MAX_ELEMENT_RESULTS` | `500` |
| `SPECTRA_MCP_MAX_TIMEOUT_MS` | `300000` |
| `SPECTRA_MCP_MAX_DELAY_MS` | `10000` |
| `SPECTRA_MCP_MAX_CLICK_COUNT` | `10` |
| `SPECTRA_MCP_MAX_MOUSE_STEPS` | `1000` |
| `SPECTRA_MCP_MAX_SCREENSHOT_BYTES` | `10485760` |
| `SPECTRA_MCP_MAX_SCREENSHOT_PIXELS` | `50000000` |
| `SPECTRA_MCP_MAX_EVALUATE_RESULT_BYTES` | `1048576` |
| `SPECTRA_MCP_DEFAULT_PAGE_CHARS` | `8000` |
| `SPECTRA_MCP_MAX_FORM_FIELDS` | `50` |
| `SPECTRA_MCP_MAX_SNAPSHOT_FRAMES` | `32` |
| `SPECTRA_MCP_MAX_FRAME_DEPTH` | `8` |

Set `SPECTRA_MCP_DATA_ROOT` to restrict persistent profiles and storage-state
input/output to one filesystem tree.

## Platform notes

Python 3.11+ is required. Supported targets are Windows x86_64, Linux
x86_64/arm64, and macOS arm64/x86_64.

On macOS, the downloaded application is ad-hoc signed. If Gatekeeper blocks it:

```bash
xattr -dr com.apple.quarantine /path/to/cached/Firefox.app
```

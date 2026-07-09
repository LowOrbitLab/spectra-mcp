# invisible-playwright-mcp

An [MCP](https://modelcontextprotocol.io) server that exposes
[`invisible_playwright`](https://github.com/feder-cr/invisible_playwright) — an
anti-detect Firefox that passes every bot-detection test — to LLMs.

Your assistant can launch a fingerprint-randomized browser, navigate, click,
type, read the DOM, and take screenshots through it, using the standard
Playwright surface, but with a patched Firefox engine under the hood.

> The patched Firefox binary is a one-time ~100 MB download
> (`python -m invisible_playwright fetch`, or the `fetch_binary` tool).

---

## How it works

- `invisible_playwright` is a **drop-in Playwright replacement** that patches
  Firefox at the C++ level (navigator, GPU/WebGL, canvas, fonts, audio, WebRTC,
  timezone, network) and humanizes mouse motion in the driver.
- An MCP server is a **resident process**, which is exactly what a stateful
  browser needs. This server keeps long-lived *sessions* in-process; each
  session owns an `InvisiblePlaywright` instance plus one or more pages. Tools
  operate on the session's **active page** unless told otherwise.

```
LLM ──MCP/stdio──► server ──► InvisiblePlaywright ──► patched Firefox
                       ▲                (async API)
              sessions held in-process
```

---

## Install

```bash
pip install -e .                       # or: pip install invisible-playwright-mcp
python -m invisible_playwright fetch   # one-time ~100 MB, SHA256-verified
```

Requires Python 3.11+. Supported platforms: Windows x86_64, Linux x86_64/arm64,
macOS arm64/x86_64.

> On macOS the app is ad-hoc signed (not notarized): if Gatekeeper complains,
> clear the quarantine flag once with
> `xattr -dr com.apple.quarantine` on the cached `Firefox.app`.

---

## Configure your MCP client

### opencode (`opencode.json`)

```jsonc
{
  "mcp": {
    "invisible-playwright": {
      "type": "local",
      "command": ["invisible-playwright-mcp"],
      "enabled": true
    }
  }
}
```

### Claude Desktop (`claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "invisible-playwright": {
      "command": "invisible-playwright-mcp"
    }
  }
}
```

### Cursor (`~/.cursor/mcp.json`)

```json
{
  "mcpServers": {
    "invisible-playwright": {
      "command": "invisible-playwright-mcp"
    }
  }
}
```

If the console script isn't on `PATH`, use the module form instead:

```json
{ "command": "python", "args": ["-m", "invisible_playwright_mcp"] }
```

---

## Quick start (for the LLM)

1. **`fetch_binary`** (once) — download the patched Firefox if `binary_status`
   says `ready: false`.
2. **`start_session`** → returns `session_id`, `seed`, and the initial
   `page_id`. Pass a `proxy_server` for residential/socks proxies.
3. **`goto`** a URL, then **`query_elements`** / **`get_text`** / **`screenshot`**
   to understand the page.
4. **`click`** / **`fill`** / **`press_key`** to act. Mouse motion is humanized.
5. **`close_session`** when done (or just let the server shut down — the lifespan
   cleans up all Firefox processes).

Log the `seed` to replay an identical fingerprint later (`start_session` with
the same `seed`).

---

## Tool reference

### Setup / binary
| Tool | Description |
|------|-------------|
| `binary_status` | Is the patched Firefox cached? Does **not** download. |
| `fetch_binary` | Download + SHA256-verify the binary (~100 MB, one-time). `force=True` to re-download. |

### Session lifecycle
| Tool | Description |
|------|-------------|
| `start_session` | Launch a browser with a fresh/seeded fingerprint. Returns `session_id`, `seed`, `page_id`. |
| `close_session` | Close a session and free its Firefox process. |
| `list_sessions` | Active session ids. |
| `session_info` | Pages, active page, current url/title for a session. |

### Pages
| Tool | Description |
|------|-------------|
| `new_page` | Open a new tab; becomes the active page. |
| `close_page` | Close a page (active by default). |
| `switch_page` | Change the active page. |
| `list_pages` | Pages in a session. |

### Navigation
`goto`, `go_back`, `go_forward`, `reload`

### Interaction (humanized)
`click`, `fill`, `type_text`, `press_key`, `keyboard_press`, `select_option`,
`hover`, `focus`, `check`, `uncheck`, `scroll`

### Reading
`get_url`, `get_title`, `get_text`, `get_html`, `get_attribute`,
`query_elements`, `is_visible`

`query_elements` returns a structured snapshot (tag, id, name, type, role, href,
text, value, placeholder, visible, bounding rect) so the LLM can decide what to
click without scraping raw HTML.

### Screenshot
`screenshot` → returns image content directly to the LLM. Defaults to JPEG
(`quality=85`) to keep payloads small; `image_format="png"` for lossless.
`full_page=True` captures the whole scrollable page.

### Waiting
`wait_for_selector`, `wait_for_timeout`

### Advanced
`evaluate` — run arbitrary JavaScript in the page and return its value. Powerful;
use sparingly.

---

## `start_session` parameters

| Param | Default | Notes |
|-------|---------|-------|
| `seed` | random | Reproducible fingerprint when set. |
| `headless` | `true` | Windows/macOS self-cloak the window. |
| `proxy_server` | `""` | `socks5://host:1080`, `http://...`, etc. DNS routes via proxy. |
| `proxy_username` / `proxy_password` | `""` | Proxy auth. |
| `timezone` | `""` | IANA zone; empty = auto from egress IP. |
| `locale` | `"auto"` | `auto` = from egress country, or e.g. `en-US`. |
| `humanize` | `true` | Bezier mouse paths + human timing. |
| `profile_dir` | `""` | Persistent profile path (enables persistent context). |
| `prep_recaptcha` | `false` | Pre-seed reCAPTCHA cookies (non-persistent only). |

---

## Notes & caveats

- **IP matters.** The browser fingerprint is handled; ~90% of captchas that
  remain are from known/blocked proxy IPs. Use clean residential IPs.
- **First `start_session` needs network** to resolve geo/timezone (and to
  download the binary if you skipped `fetch_binary`).
- **Humanized clicks are slower** than vanilla Playwright — raise `timeout_ms`
  if needed.
- **`evaluate` runs arbitrary JS** — only enable for trusted use.
- All logging goes to **stderr** so it never corrupts the stdio JSON-RPC stream.
- Calls against a single session must be sequential (the natural LLM pattern: the model waits for each tool result before sending the next). Concurrent calls to one session are not supported.

---

## License

MIT. The patched Firefox binary is MPL-2.0 (Firefox upstream). This wrapper
does not redistribute the binary; it downloads it on demand from the upstream
releases.

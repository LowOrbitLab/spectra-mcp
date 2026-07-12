# Tools and profiles

Select the advertised tool set with `SPECTRA_MCP_TOOL_PROFILE` and restart the
server after changing it.

| Profile | Tools | Purpose |
|---|---:|---|
| `setup` | 2 | Browser binary status and download only |
| `agent` | 18 | Default compact workflow for AI agents |
| `core` | 28 | Selector-based browsing, tabs, HTML, and JavaScript |
| `full` | 71 | Every agent, storage, frame, mouse, and keyboard tool |

```bash
SPECTRA_MCP_TOOL_PROFILE=agent spectra_mcp
```

## Agent profile

### Setup

- `binary_status` checks whether patched Firefox is available.
- `fetch_binary` downloads and verifies it. `force=true` replaces the cache.

### Browser lifecycle

- `browser_start` launches the single Agent browser.
- `browser_status` returns its current page and session state.
- `browser_stop` closes it.

Agent tools automatically select the only live session. Their optional
`session_id` is needed only when advanced/full tools created multiple sessions.

### Navigation and observation

- `browser_navigate` navigates and returns a fresh observation by default.
- `browser_reload` reloads and returns a fresh observation.
- `browser_snapshot` returns visible controls and headings with stable refs.
- `snapshot_find` searches accessible names, values, and roles.
- `browser_screenshot` returns MCP image content.

A snapshot looks like:

```text
- heading "Sign in" [ref=e821b14d]
- textbox "Email" [ref=e512f730]
- button "Continue" [ref=e6e67ce1]
```

Ref mappings are stored in server memory. They do not add attributes or global
variables to the page. Refresh the snapshot after navigation or when a ref is
reported as stale.

### Interaction

- `click_ref`
- `fill_ref`
- `type_ref`
- `select_ref`
- `fill_form`

Mutating ref actions return a new snapshot unless `observe=false`. Filled and
typed values are never echoed; results contain only their lengths.

`fill_form` accepts:

```json
{
  "fields": [
    {"ref": "e512f730", "value": "user@example.com"},
    {"ref": "e2734ac1", "value": "secret"}
  ]
}
```

### Reading and waiting

- `find_text` returns short matching snippets.
- `browser_get_text` returns paginated visible text.
- `browser_wait` lets short asynchronous page updates settle.

Text results include `offset`, `next_offset`, `full_length`, and `truncated`.
The default page size is 8,000 characters.

## Core and full profiles

The lower-level API uses explicit session IDs and CSS selectors.

- Lifecycle: `start_session`, `close_session`, `list_sessions`, `session_info`
- Pages: `new_page`, `close_page`, `switch_page`, `wait_for_page`
- Navigation: `goto`, `go_forward`, `reload`
- Interaction: `click`, `fill`, `type_text`, `press_key`, `keyboard_press`,
  `select_option`, `hover`, `focus`, `check`, `uncheck`, `scroll`
- Reading: `get_text`, `get_html`, `get_attribute`, `query_elements`, `is_visible`
- Waiting: `wait_for_selector`, `wait_for_timeout`
- JavaScript: `evaluate`
- Mouse: `mouse_drag`, `mouse_move`, `mouse_click`
- Keyboard: `keyboard_down`, `keyboard_up`, `keyboard_type`
- Dialogs: `set_dialog_handler`, `get_dialogs`
- Cookies/state: `get_cookies`, `add_cookies`, `clear_cookies`,
  `save_storage_state`
- Frames: `list_frames`, `frame_click`, `frame_fill`, `frame_type`,
  `frame_get_text`, `frame_wait_for_selector`, `frame_query_elements`,
  `frame_evaluate`, `frame_get_html`, `frame_get_attribute`
- Screenshot: `screenshot`

`evaluate`, raw HTML, cookies, storage state, and arbitrary filesystem paths are
intended for trusted full-profile workflows.

Storage state contains cookies and localStorage. Treat it as a secret. Save it
with `save_storage_state` and restore it with
`start_session` with `storage_state_path`. Files are atomically replaced and use
mode `0600` on Unix.

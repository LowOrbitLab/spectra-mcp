# Tools and profiles

Select the advertised tool set with `SPECTRA_MCP_TOOL_PROFILE` and restart the
server after changing it.

| Profile | Tools | Purpose |
|---|---:|---|
| `setup` | 2 | Browser binary status and download only |
| `agent` | 19 | Default compact workflow for AI agents |
| `core` | 28 | Selector-based browsing, tabs, HTML, and JavaScript |
| `full` | 73 | Every agent, storage, frame, mouse, and keyboard tool |

```bash
SPECTRA_MCP_TOOL_PROFILE=agent spectra_mcp
```

## Naming convention

Public tools use `<scope>_<action>[_<object>]` names:

- `binary_*` manages the patched browser binary.
- `browser_*` is the agent-facing, implicit single-browser/ref workflow.
- `session_*` manages explicit multi-session state.
- `page_*` manages explicit pages, navigation, page content, and page waits.
- `element_*` targets CSS selectors in the active page.
- `frame_*` targets selectors inside an iframe.
- `mouse_*` and `keyboard_*` expose device-level input.
- `dialog_*`, `cookie_*`, and `storage_state_*` expose context state.

The server registers only these canonical names; legacy tool aliases are not
advertised or retained.

## Agent profile

### Setup

- `binary_status` checks whether patched Firefox is available.
- `binary_install` downloads and verifies it. `force=true` replaces the cache.

### Browser lifecycle

- `browser_start` launches the single Agent browser. It exposes only seed,
  headless, humanization, and fingerprint profile controls; proxy and geo
  configuration comes from the server environment.
- `browser_status` returns its current page, humanization setting, dialog
  policy, and the page capabilities exposed by the active tool profile.
- `browser_list_tabs` lists open tabs and the active tab.
- `browser_activate_tab` activates a tab by `page_id`.
- `browser_stop` closes it.

Agent tools automatically select the only live session. Their optional
`session_id` is needed only when advanced/full tools created multiple sessions.

### Navigation and observation

- `browser_navigate` navigates and returns a fresh observation by default.
- `browser_reload` reloads and returns a fresh observation.
- `browser_snapshot` returns visible controls and headings with stable refs. Its
  optional `query` and `role` filters also return matching controls.
- `browser_screenshot` returns MCP image content.

A snapshot looks like:

```text
- heading "Sign in" [ref=e821b14d]
- textbox "Email" [ref=e512f730]
- button "Continue" [ref=e6e67ce1]
```

Each snapshot has a `snapshot_id`, which is embedded in refs such as
`p1s3:e512f730`. Ref mappings are stored in server memory and do not add
attributes or global variables to the page. Several recent snapshots are kept
per page, so taking a new snapshot does not immediately invalidate older refs.
Refresh after navigation or when a ref is reported as stale or expired.

Snapshots recurse through same-origin and cross-origin Playwright frames,
including nested frames. Frame refs contain both versions, such as
`p1s3:f1:e512f730`, and the normal ref tools automatically execute in the
correct frame. A frame whose
realm cannot be inspected is still listed as `inaccessible` instead of being
silently omitted.

### Interaction

- `browser_click_ref`
- `browser_set_value_ref`
- `browser_type_text_ref`
- `browser_select_option_ref`
- `browser_set_form_values`

Navigation and mutating ref actions accept `observe="none"`, `"compact"`, or
`"full"`. The default `compact` response refreshes refs and returns the textual
snapshot without duplicating the structured element/frame arrays. `full`
returns the complete snapshot; `none` skips observation. Filled and typed
values are never echoed; results contain only their lengths.

`browser_set_form_values` accepts:

```json
{
  "fields": [
    {"ref": "e512f730", "value": "user@example.com"},
    {"ref": "e2734ac1", "value": "secret"}
  ]
}
```

Each field is a strict object containing exactly `ref` and `value`.

### Reading and waiting

- `browser_find_text` returns short matching snippets.
- `browser_get_text` returns paginated visible text.
- `browser_wait_for` waits for page text, a URL substring, a ref state, or a
  combination of those conditions. Fixed sleeps remain available only in
  lower-level profiles.

Text results include `offset`, `next_offset`, `full_length`, and `truncated`.
The default page size is 8,000 characters.

## Core and full profiles

The lower-level API uses explicit session IDs and CSS selectors.

- Lifecycle: `session_start`, `session_stop`, `session_list`, `session_status`
- Pages: `page_open`, `page_close`, `page_activate`, `session_wait_for_new_page`
- Navigation: `page_navigate`, `page_go_forward`, `page_reload`
- Interaction: `element_click`, `element_set_value`, `element_type_text`,
  `element_press_key`, `keyboard_press_key`, `element_select_option`,
  `element_hover`, `element_focus`, `element_check`, `element_uncheck`,
  `page_scroll`
- Reading: `page_get_text`, `page_get_html`, `element_get_attribute`,
  `element_query`, `element_is_visible`
- Waiting: `element_wait_for`, `page_sleep`, `browser_sleep`, `browser_wait_for`
- JavaScript: `page_evaluate`
- Mouse: `mouse_drag_between`, `mouse_move_to`, `mouse_click_at`
- Keyboard: `keyboard_key_down`, `keyboard_key_up`, `keyboard_type_text`
- Dialogs: `dialog_set_policy`, `dialog_list`
- Cookies/state: `cookie_list`, `cookie_add`, `cookie_clear`,
  `storage_state_save`
- Frames: `page_list_frames`, `frame_click`, `frame_set_value`,
  `frame_type_text`, `frame_get_text`, `frame_wait_for`, `frame_query`,
  `frame_evaluate`, `frame_get_html`, `frame_get_attribute`
- Screenshot: `page_screenshot`

`page_evaluate`, raw HTML, cookies, storage state, and arbitrary filesystem paths are
intended for trusted full-profile workflows.

`cookie_add` accepts either `{name, value, url}` cookies or
`{name, value, domain, path}` cookies, plus optional Playwright cookie flags.

Storage state contains cookies and localStorage. Treat it as a secret. Save it
with `storage_state_save` and restore it with
`session_start` with `storage_state_path`. Files are atomically replaced and use
mode `0600` on Unix.

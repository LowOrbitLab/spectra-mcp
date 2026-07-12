# Security and safeguards

## MCP result behavior

Successful structured results use an unwrapped `{ "ok": true, ... }` object.
Failures return MCP `isError=true` with a stable JSON message containing:

```json
{
  "error": {
    "code": "SESSION_NOT_FOUND",
    "message": "...",
    "retryable": false,
    "details": {}
  }
}
```

URL userinfo and authorization headers are redacted from normalized errors.

## Secret handling

- `fill`, ref input tools, `fill_form`, and dialog configuration do not echo
  submitted values.
- Password values are omitted from element snapshots.
- Proxy credentials should use environment variables.
- Storage state contains authentication cookies and must be treated as secret.

## Resource safeguards

The server limits sessions, pages, text, screenshots, evaluation results,
timeouts, mouse steps, and form sizes. Browser binary cache mutation is guarded
by process and cross-process locks.

Storage-state files are atomically replaced and use mode `0600` on Unix.
`SPECTRA_MCP_DATA_ROOT` can restrict file-backed profiles and state to one tree.

## Trusted profiles

The default `agent` profile avoids raw HTML, arbitrary JavaScript evaluation,
cookie mutation, storage-state access, and session enumeration.

The `core` and `full` profiles expose more powerful capabilities. Only enable
them for trusted agents and pages. In particular, `evaluate` executes arbitrary
JavaScript in the active page.

All server logging goes to stderr so it cannot corrupt stdio JSON-RPC.

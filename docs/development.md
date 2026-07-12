# Development

## Checks

```bash
uv lock --check
uv run --with ruff ruff check .
uv run --with pytest python -m pytest -q
python -m compileall -q src tests
```

The test suite also supports standard-library discovery:

```bash
python -m unittest discover -s tests -v
```

Some pytest-style Linux-native checks are collected only by pytest.

## Real browser integration

Integration tests are opt-in because they start patched Firefox and may use the
network for geo initialization:

```bash
SPECTRA_MCP_RUN_INTEGRATION=1 \
  uv run --with pytest python -m pytest tests/test_integration.py -q
```

## Runtime architecture

The stdio server is resident and owns long-lived browser sessions. Each session
contains one browser/context, one or more tracked pages, and a lifecycle lock.
Calls within one session are sequential. Server shutdown closes all remaining
browser processes.

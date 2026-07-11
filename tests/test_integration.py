from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

from invisible_playwright.download import (
    BINARY_ENTRY_REL,
    BINARY_VERSION,
    cache_dir_for_version,
)

import spectra_mcp.server as server


class _Ctx:
    async def info(self, *args):
        return None

    async def debug(self, *args):
        return None


def _binary_ready() -> bool:
    entry = BINARY_ENTRY_REL.get(sys.platform)
    return bool(entry and (cache_dir_for_version(BINARY_VERSION) / entry).exists())


@unittest.skipUnless(
    os.environ.get("SPECTRA_MCP_RUN_INTEGRATION") == "1",
    "设置 SPECTRA_MCP_RUN_INTEGRATION=1 后运行真实浏览器集成测试",
)
class BrowserIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        if not _binary_ready():
            self.skipTest("补丁 Firefox 尚未下载")

    async def asyncTearDown(self) -> None:
        await server._close_all_sessions()
        async with server._LOCK:
            server._SESSIONS.clear()
            server._STARTING_SESSIONS = 0

    async def test_persistent_context_adopts_initial_page(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = await server.start_session(
                _Ctx(),
                headless=True,
                humanize=False,
                timezone="UTC",
                locale="en-US",
                profile_dir=str(Path(tmpdir) / "profile"),
            )
            self.assertTrue(result["ok"], result)
            session_id = result["session_id"]
            try:
                session = server._SESSIONS[session_id]

                self.assertTrue(result["persistent"])
                self.assertEqual(len(session.browser_or_ctx.pages), 1)
                self.assertEqual(len(session.pages), 1)
            finally:
                await server.close_session(session_id, _Ctx())

    async def test_browser_disconnect_preserves_tool_error_contract(self):
        result = await server.start_session(
            _Ctx(),
            headless=True,
            humanize=False,
            timezone="UTC",
            locale="en-US",
        )
        self.assertTrue(result["ok"], result)
        session_id = result["session_id"]
        session = server._SESSIONS[session_id]

        await session.browser_or_ctx.close()
        tool_result = await server.get_text(session_id, _Ctx())

        self.assertIsInstance(tool_result, dict)
        self.assertFalse(tool_result["ok"])


if __name__ == "__main__":
    unittest.main()

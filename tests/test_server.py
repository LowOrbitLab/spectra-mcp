from __future__ import annotations

import asyncio
import threading
import time
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

import spectra_mcp.server as server


ROOT = Path(__file__).resolve().parents[1]


class _Ctx:
    async def info(self, *args):
        return None

    async def debug(self, *args):
        return None

    async def report_progress(self, *args):
        return None


class _Page:
    url = "https://example.test/"

    def __init__(self) -> None:
        self.closed = False

    def is_closed(self) -> bool:
        return self.closed


class _AsyncToolTest(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        async with server._LOCK:
            server._SESSIONS.clear()

    async def install_page(self, sid: str, page, ipw=None):
        session = server._Session(sid, ipw, object(), 1, False)
        session.pages[1] = page
        session.active_page_id = 1
        async with server._LOCK:
            server._SESSIONS[sid] = session
        return session


class RuntimeRecoveryTests(_AsyncToolTest):
    async def test_close_page_preserves_registration_when_close_fails(self):
        class Page(_Page):
            async def close(self):
                raise ValueError("close failed")

        session = await self.install_page("close", Page())
        result = await server.close_page("close", _Ctx())

        self.assertFalse(result["ok"])
        self.assertIn(1, session.pages)
        self.assertIs(session.pages[1], session.pages.get(session.active_page_id))

    async def test_goto_keeps_success_when_title_lookup_fails(self):
        class Page(_Page):
            async def goto(self, *args, **kwargs):
                return SimpleNamespace(status=200)

            async def title(self):
                raise ValueError("title failed")

        await self.install_page("goto", Page())
        try:
            result = await server.goto("goto", "https://example.test/", _Ctx())
        except Exception as exc:  # 当前缺陷会走到这里，转换成明确的测试失败。
            self.fail(f"goto 泄漏异常：{exc}")

        self.assertTrue(result["ok"])
        self.assertIsNone(result["title"])
        self.assertIn("title failed", result["title_error"])

    async def test_mouse_drag_releases_button_after_move_failure(self):
        class Mouse:
            def __init__(self) -> None:
                self.moves = 0
                self.up_called = False

            async def move(self, *args, **kwargs):
                self.moves += 1
                if self.moves == 2:
                    raise ValueError("move failed")

            async def down(self):
                return None

            async def up(self):
                self.up_called = True

        page = _Page()
        page.mouse = Mouse()
        await self.install_page("drag", page)

        result = await server.mouse_drag("drag", 0, 0, 10, 10, _Ctx())

        self.assertFalse(result["ok"])
        self.assertTrue(page.mouse.up_called)

    async def test_dialog_accept_failure_falls_back_to_dismiss(self):
        class Dialog:
            type = "alert"
            message = "message"
            default_value = ""

            def __init__(self) -> None:
                self.dismissed = False

            async def accept(self, *args):
                raise ValueError("accept failed")

            async def dismiss(self):
                self.dismissed = True

        session = SimpleNamespace(
            session_id="dialog",
            dialog_action="accept",
            dialog_prompt_text="",
            dialog_log=[],
        )
        dialog = Dialog()
        with patch.object(server._log, "warning"):
            await server._on_dialog(session, dialog)

        self.assertTrue(dialog.dismissed)
        self.assertEqual(session.dialog_log[-1]["action"], "dismiss_fallback")

    async def test_prompt_can_submit_an_explicit_empty_string(self):
        class Dialog:
            type = "prompt"
            message = "message"
            default_value = "default"

            def __init__(self) -> None:
                self.prompt_text = object()

            async def accept(self, prompt_text=None):
                self.prompt_text = prompt_text

            async def dismiss(self):
                return None

        session = SimpleNamespace(
            session_id="dialog",
            dialog_action="accept",
            dialog_prompt_text="",
            dialog_log=[],
        )
        dialog = Dialog()
        await server._on_dialog(session, dialog)

        self.assertEqual(dialog.prompt_text, "")


class ContractTests(_AsyncToolTest):
    async def test_fetch_binary_normalizes_download_failure(self):
        def fail(*args, **kwargs):
            raise ValueError("download failed")

        with patch.object(server, "ensure_binary", fail):
            try:
                result = await server.fetch_binary(_Ctx())
            except Exception as exc:
                self.fail(f"fetch_binary 泄漏异常：{exc}")

        self.assertFalse(result["ok"])
        self.assertIn("download failed", result["error"])

    async def test_fetch_binary_serializes_shared_cache_mutation(self):
        state_lock = threading.Lock()
        active = 0
        max_active = 0

        def ensure(*args, **kwargs):
            nonlocal active, max_active
            with state_lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.08)
            with state_lock:
                active -= 1
            return Path("binary")

        with patch.object(server, "ensure_binary", ensure):
            results = await asyncio.gather(
                server.fetch_binary(_Ctx()), server.fetch_binary(_Ctx())
            )

        self.assertTrue(all(result["ok"] for result in results))
        self.assertEqual(max_active, 1)

    async def test_start_session_normalizes_constructor_failure(self):
        class FailConstructor:
            def __init__(self, *args, **kwargs):
                raise ValueError("bad launch option")

        with patch.object(server, "InvisiblePlaywright", FailConstructor):
            try:
                result = await server.start_session(_Ctx())
            except Exception as exc:
                self.fail(f"start_session 泄漏异常：{exc}")

        self.assertFalse(result["ok"])
        self.assertIn("bad launch option", result["error"])

    async def test_close_session_reports_cleanup_failure(self):
        class IPW:
            async def __aexit__(self, *args):
                raise ValueError("browser exit failed")

        session = server._Session("cleanup", IPW(), object(), 1, False)
        with patch.object(server._log, "warning"):
            errors = await server._close_session(session)

        self.assertTrue(errors)
        self.assertIn("browser exit failed", errors[-1])

    async def test_close_session_continues_when_page_state_check_fails(self):
        class Page:
            def __init__(self) -> None:
                self.close_called = False

            def is_closed(self):
                raise ValueError("state unavailable")

            async def close(self):
                self.close_called = True

        class IPW:
            def __init__(self) -> None:
                self.exit_called = False

            async def __aexit__(self, *args):
                self.exit_called = True

        page = Page()
        ipw = IPW()
        session = server._Session("cleanup-state", ipw, object(), 1, False)
        session.pages[1] = page
        with patch.object(server._log, "warning"):
            try:
                errors = await server._close_session(session)
            except Exception as exc:
                self.fail(f"清理流程被页面状态检查中断：{exc}")

        self.assertTrue(page.close_called)
        self.assertTrue(ipw.exit_called)
        self.assertEqual(errors, [])

    async def test_is_visible_waits_until_visible(self):
        locator = SimpleNamespace()
        locator.first = locator
        locator.wait_for = AsyncMock(return_value=None)
        page = _Page()
        page.locator = lambda selector: locator
        await self.install_page("visible", page)

        result = await server.is_visible("visible", "#target", _Ctx(), timeout_ms=25)

        self.assertTrue(result["ok"])
        self.assertTrue(result["visible"])
        locator.wait_for.assert_awaited_once_with(state="visible", timeout=25)

    async def test_is_visible_timeout_returns_false(self):
        locator = SimpleNamespace()
        locator.first = locator
        locator.wait_for = AsyncMock(side_effect=PlaywrightTimeoutError("timeout"))
        page = _Page()
        page.locator = lambda selector: locator
        await self.install_page("hidden", page)

        result = await server.is_visible("hidden", "#target", _Ctx(), timeout_ms=25)

        self.assertTrue(result["ok"])
        self.assertFalse(result["visible"])

    async def test_negative_output_limits_are_rejected(self):
        page = _Page()
        page.inner_text = AsyncMock(return_value="abcdef")
        page.evaluate = AsyncMock(return_value=[])
        await self.install_page("limits", page)

        text_result = await server.get_text("limits", _Ctx(), max_chars=-1)
        elements_result = await server.query_elements(
            "limits", "div", _Ctx(), max_results=-1
        )

        self.assertFalse(text_result["ok"])
        self.assertFalse(elements_result["ok"])
        page.inner_text.assert_not_awaited()
        page.evaluate.assert_not_awaited()

    async def test_screenshot_rejects_unknown_format(self):
        page = _Page()
        page.screenshot = AsyncMock(return_value=b"image")
        await self.install_page("shot", page)

        with self.assertRaisesRegex(RuntimeError, "image_format"):
            await server.screenshot("shot", _Ctx(), image_format="webp")
        page.screenshot.assert_not_awaited()

    def test_element_snapshot_checks_computed_visibility(self):
        self.assertIn("getComputedStyle", server._QUERY_ELEMENTS_JS)
        self.assertNotIn(
            "!!(e.offsetWidth || e.offsetHeight || e.getClientRects().length)",
            server._QUERY_ELEMENTS_JS,
        )


class MetadataTests(unittest.TestCase):
    def test_every_registered_tool_has_a_description(self):
        tools = asyncio.run(server.mcp.list_tools())
        self.assertEqual(len(tools), 55)
        self.assertEqual(
            [tool.name for tool in tools if not (tool.description or "").strip()], []
        )

    def test_readme_mentions_every_tool_and_storage_state_parameter(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        tools = asyncio.run(server.mcp.list_tools())
        missing = [tool.name for tool in tools if f"`{tool.name}`" not in readme]

        self.assertEqual(missing, [])
        self.assertIn("`storage_state_path`", readme)

    def test_release_metadata_uses_loworbitlab_identity(self):
        document = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        project = document["project"]

        self.assertEqual(project["name"], "spectra-mcp")
        self.assertEqual(
            project["scripts"], {"spectra-mcp": "spectra_mcp.server:main"}
        )
        self.assertEqual(project["authors"], [{"name": "LowOrbitLab"}])
        self.assertTrue((ROOT / "LICENSE").is_file())
        self.assertNotIn("anyio>=4", project["dependencies"])
        self.assertIn("invisible-playwright>=0.3.0,<0.4", project["dependencies"])
        self.assertIn("playwright>=1.40,<1.61", project["dependencies"])
        self.assertIn("mcp>=1.2,<2", project["dependencies"])

    def test_logger_does_not_duplicate_messages_through_root(self):
        self.assertFalse(server._log.propagate)


if __name__ == "__main__":
    unittest.main()

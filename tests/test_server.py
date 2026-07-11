from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unittest
from contextlib import asynccontextmanager
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


@asynccontextmanager
async def _noop_binary_guard():
    yield


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
            server._STARTING_SESSIONS = 0

    async def install_page(self, sid: str, page, ipw=None):
        session = server._Session(sid, ipw, object(), 1, False)
        session.pages[1] = page
        session.active_page_id = 1
        async with server._LOCK:
            server._SESSIONS[sid] = session
        return session


class RuntimeRecoveryTests(_AsyncToolTest):
    async def test_session_info_snapshots_pages_before_reading_entries(self):
        class Page(_Page):
            session = None

            @property
            def url(self):
                self.session.pages[2] = _Page()
                return "https://example.test/"

        page = Page()
        session = await self.install_page("snapshot", page)
        page.session = session

        result = await server.session_info("snapshot", _Ctx())

        self.assertTrue(result["ok"])
        self.assertEqual([item["page_id"] for item in result["pages"]], [1])

    async def test_close_page_preserves_registration_when_close_fails(self):
        class Page(_Page):
            async def close(self):
                raise ValueError("close failed")

        session = await self.install_page("close", Page())
        result = await server.close_page("close", _Ctx())

        self.assertFalse(result["ok"])
        self.assertIn(1, session.pages)
        self.assertIs(session.pages[1], session.pages.get(session.active_page_id))

    async def test_close_page_skips_closed_pages_when_selecting_active_page(self):
        class CloseablePage(_Page):
            async def close(self):
                self.closed = True

        closed_page = _Page()
        closed_page.closed = True
        active_page = CloseablePage()
        live_page = _Page()
        session = await self.install_page("reselect", active_page)
        session.pages.clear()
        session.pages[1] = closed_page
        session.pages[2] = active_page
        session.pages[3] = live_page
        session.active_page_id = 2

        result = await server.close_page("reselect", _Ctx())

        self.assertTrue(result["ok"])
        self.assertEqual(result["active_page_id"], 3)
        self.assertEqual(session.active_page_id, 3)

    async def test_active_page_state_failure_returns_error_dict(self):
        class BrokenPage(_Page):
            def is_closed(self):
                raise ValueError("connection lost")

        await self.install_page("broken-active", BrokenPage())

        result = await server.get_text("broken-active", _Ctx())

        self.assertFalse(result["ok"])
        self.assertIn("state check failed", result["error"])

    async def test_switch_page_state_failure_returns_error_dict(self):
        class BrokenPage(_Page):
            def is_closed(self):
                raise ValueError("connection lost")

        await self.install_page("broken-switch", BrokenPage())

        result = await server.switch_page("broken-switch", 1, _Ctx())

        self.assertFalse(result["ok"])
        self.assertIn("switch_page failed", result["error"])

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


class LifecycleHardeningTests(_AsyncToolTest):
    async def test_start_session_cancellation_cleans_unpublished_browser(self):
        class Context:
            pages = []

            async def new_page(self):
                raise asyncio.CancelledError()

        class IPW:
            seed = 7

            def __init__(self, *args, **kwargs):
                self.exit_called = False

            async def __aenter__(self):
                return Context()

            async def __aexit__(self, *args):
                self.exit_called = True

        ipw = IPW()
        with (
            patch.object(server, "InvisiblePlaywright", return_value=ipw),
            patch.object(server, "_binary_cache_guard", _noop_binary_guard),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await server.start_session(_Ctx())

        self.assertTrue(ipw.exit_called)
        self.assertEqual(server._SESSIONS, {})

    async def test_close_session_retries_failed_browser_exit(self):
        class Page(_Page):
            def __init__(self):
                super().__init__()
                self.calls = 0

            async def close(self):
                self.calls += 1
                if self.calls == 1:
                    raise ValueError("page close failed")
                self.closed = True

        class IPW:
            def __init__(self):
                self.calls = 0

            async def __aexit__(self, *args):
                self.calls += 1
                if self.calls == 1:
                    raise ValueError("exit failed")

        ipw = IPW()
        session = server._Session("retry", ipw, object(), 1, False)
        page = Page()
        session.pages[1] = page
        async with server._LOCK:
            server._SESSIONS["retry"] = session

        first = await server.close_session("retry", _Ctx())
        second = await server.close_session("retry", _Ctx())

        self.assertFalse(first["ok"])
        self.assertFalse(first["cleanup_complete"])
        self.assertTrue(second["ok"])
        self.assertEqual(page.calls, 2)
        self.assertEqual(ipw.calls, 2)
        self.assertNotIn("retry", server._SESSIONS)

    async def test_persistent_context_adopts_existing_page(self):
        initial_page = _Page()

        class Context:
            def __init__(self):
                self.pages = [initial_page]
                self.new_page_calls = 0

            async def new_page(self):
                self.new_page_calls += 1
                return _Page()

        initial_page.on = lambda *args: None
        context = Context()
        session = server._Session("persistent", object(), context, 1, True)

        page, pid = await server._initialize_session_page(session)

        self.assertIs(page, initial_page)
        self.assertEqual(pid, 1)
        self.assertEqual(context.new_page_calls, 0)
        self.assertIs(session.pages[1], initial_page)

    async def test_session_limit_rejects_before_browser_construction(self):
        await self.install_page("existing", _Page())
        with (
            patch.object(server, "_MAX_SESSIONS", 1),
            patch.object(server, "InvisiblePlaywright") as constructor,
        ):
            result = await server.start_session(_Ctx())

        self.assertFalse(result["ok"])
        self.assertIn("session limit", result["error"])
        constructor.assert_not_called()

    async def test_new_page_limit_closes_untracked_page(self):
        created = _Page()

        async def close():
            created.closed = True

        created.close = close
        context = SimpleNamespace(new_page=AsyncMock(return_value=created))
        session = await self.install_page("page-limit", _Page())
        session.browser_or_ctx = SimpleNamespace(new_context=AsyncMock())
        session.primary_context = context
        with patch.object(server, "_MAX_PAGES_PER_SESSION", 1):
            result = await server.new_page("page-limit", _Ctx())

        self.assertFalse(result["ok"])
        self.assertIn("page limit", result["error"])
        self.assertTrue(created.closed)


class BoundaryTests(_AsyncToolTest):
    async def test_evaluate_zero_timeout_disables_server_timeout(self):
        page = _Page()
        page.evaluate = AsyncMock(return_value={"value": 1})
        await self.install_page("evaluate-zero", page)

        result = await server.evaluate(
            "evaluate-zero", "() => ({value: 1})", _Ctx(), timeout_ms=0
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["result"], {"value": 1})

    async def test_output_limit_rejects_excessive_value_before_page_call(self):
        page = _Page()
        page.inner_text = AsyncMock(return_value="text")
        await self.install_page("output-limit", page)

        result = await server.get_text(
            "output-limit", _Ctx(), max_chars=server._MAX_TEXT_CHARS + 1
        )

        self.assertFalse(result["ok"])
        page.inner_text.assert_not_awaited()

    async def test_hidden_wait_reports_reached_without_match_confusion(self):
        page = _Page()
        page.wait_for_selector = AsyncMock(return_value=None)
        await self.install_page("hidden", page)

        result = await server.wait_for_selector(
            "hidden", "#gone", _Ctx(), state="hidden"
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["reached"])
        self.assertFalse(result["element_present"])
        self.assertNotIn("matched", result)

    async def test_scroll_missing_selector_returns_error(self):
        page = _Page()
        page.query_selector = AsyncMock(return_value=None)
        page.mouse = SimpleNamespace(wheel=AsyncMock())
        await self.install_page("scroll", page)

        result = await server.scroll("scroll", _Ctx(), dy=100, selector="#missing")

        self.assertFalse(result["ok"])
        page.mouse.wheel.assert_not_awaited()

    async def test_storage_state_is_written_atomically_with_private_mode(self):
        context = SimpleNamespace(
            storage_state=AsyncMock(return_value={"cookies": [], "origins": []})
        )
        session = await self.install_page("storage", _Page())
        session.primary_context = context
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "nested" / "state.json"

            result = await server.save_storage_state(
                "storage", str(target), _Ctx()
            )

            self.assertTrue(result["ok"])
            self.assertEqual(
                json.loads(target.read_text(encoding="utf-8")),
                {"cookies": [], "origins": []},
            )
            self.assertEqual(list(target.parent.glob("*.tmp")), [])
            if os.name != "nt":
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    async def test_configured_data_root_rejects_path_escape(self):
        session = await self.install_page("root", _Page())
        session.primary_context = SimpleNamespace(storage_state=AsyncMock())
        with tempfile.TemporaryDirectory() as tmpdir:
            outside = Path(tmpdir).parent / "outside-state.json"
            with patch.object(server, "_DATA_ROOT", tmpdir):
                result = await server.save_storage_state(
                    "root", str(outside), _Ctx()
                )

        self.assertFalse(result["ok"])
        self.assertIn("configured data root", result["error"])

    async def test_force_fetch_rejects_active_sessions(self):
        await self.install_page("active", _Page())
        with (
            patch.object(server, "_binary_cache_guard", _noop_binary_guard),
            patch.object(server, "ensure_binary") as ensure,
        ):
            result = await server.fetch_binary(_Ctx(), force=True)

        self.assertFalse(result["ok"])
        self.assertIn("sessions are active", result["error"])
        ensure.assert_not_called()

    async def test_binary_file_lock_is_released_when_waiter_is_cancelled(self):
        started = threading.Event()
        allow_acquire = threading.Event()
        released = threading.Event()
        handle = object()

        def acquire(path):
            started.set()
            allow_acquire.wait(timeout=2)
            return handle

        def release(value):
            self.assertIs(value, handle)
            released.set()

        async def use_guard():
            async with server._binary_cache_guard():
                self.fail("cancelled waiter must not enter the guarded section")

        with (
            patch.object(server, "_acquire_process_file_lock", acquire),
            patch.object(server, "_release_process_file_lock", release),
        ):
            task = asyncio.create_task(use_guard())
            await asyncio.to_thread(started.wait, 2)
            task.cancel()
            allow_acquire.set()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertTrue(released.is_set())


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
        expected_counts = {"setup": 2, "core": 28, "full": 55}
        self.assertEqual(len(tools), expected_counts[server._TOOL_PROFILE])
        self.assertEqual(
            [tool.name for tool in tools if not (tool.description or "").strip()], []
        )

    def test_tool_profiles_register_expected_tools(self):
        script = (
            "import asyncio,json; import spectra_mcp.server as s; "
            "print(json.dumps([t.name for t in asyncio.run(s.mcp.list_tools())]))"
        )
        expected_counts = {"setup": 2, "core": 28, "full": 55}
        for profile, expected_count in expected_counts.items():
            with self.subTest(profile=profile):
                env = os.environ.copy()
                env["SPECTRA_MCP_TOOL_PROFILE"] = profile
                env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get(
                    "PYTHONPATH", ""
                )
                output = subprocess.check_output(
                    [sys.executable, "-c", script],
                    cwd=ROOT,
                    env=env,
                    text=True,
                )
                names = json.loads(output)
                self.assertEqual(len(names), expected_count)
                if profile == "setup":
                    self.assertEqual(names, ["binary_status", "fetch_binary"])
                if profile == "core":
                    self.assertIn("start_session", names)
                    self.assertIn("scroll", names)
                    self.assertNotIn("frame_click", names)
                if profile == "full":
                    self.assertIn("frame_click", names)
                    self.assertIn("save_storage_state", names)

    def test_tool_schema_exposes_enum_constraints(self):
        tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}

        self.assertEqual(
            tools["click"].inputSchema["properties"]["button"]["enum"],
            ["left", "middle", "right"],
        )
        self.assertEqual(
            tools["wait_for_selector"].inputSchema["properties"]["state"]["enum"],
            ["attached", "detached", "hidden", "visible"],
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

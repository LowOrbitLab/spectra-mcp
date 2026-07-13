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

from mcp.server.fastmcp.exceptions import ToolError
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
    async def test_session_status_snapshots_pages_before_reading_entries(self):
        class Page(_Page):
            session = None

            @property
            def url(self):
                self.session.pages[2] = _Page()
                return "https://example.test/"

        page = Page()
        session = await self.install_page("snapshot", page)
        page.session = session

        result = await server.session_status("snapshot", _Ctx())

        self.assertTrue(result["ok"])
        self.assertEqual([item["page_id"] for item in result["pages"]], [1])

    async def test_page_close_preserves_registration_when_close_fails(self):
        class Page(_Page):
            async def close(self):
                raise ValueError("close failed")

        session = await self.install_page("close", Page())
        result = await server.page_close("close", _Ctx())

        self.assertFalse(result["ok"])
        self.assertIn(1, session.pages)
        self.assertIs(session.pages[1], session.pages.get(session.active_page_id))

    async def test_page_close_skips_closed_pages_when_selecting_active_page(self):
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

        result = await server.page_close("reselect", _Ctx())

        self.assertTrue(result["ok"])
        self.assertEqual(result["active_page_id"], 3)
        self.assertEqual(session.active_page_id, 3)

    async def test_active_page_state_failure_returns_error_dict(self):
        class BrokenPage(_Page):
            def is_closed(self):
                raise ValueError("connection lost")

        await self.install_page("broken-active", BrokenPage())

        result = await server.page_get_text("broken-active", _Ctx())

        self.assertFalse(result["ok"])
        self.assertIn("state check failed", result["error"])

    async def test_page_activate_state_failure_returns_error_dict(self):
        class BrokenPage(_Page):
            def is_closed(self):
                raise ValueError("connection lost")

        await self.install_page("broken-switch", BrokenPage())

        result = await server.page_activate("broken-switch", 1, _Ctx())

        self.assertFalse(result["ok"])
        self.assertIn("page_activate failed", result["error"])

    async def test_page_navigate_keeps_success_when_title_lookup_fails(self):
        class Page(_Page):
            async def goto(self, *args, **kwargs):
                return SimpleNamespace(status=200)

            async def title(self):
                raise ValueError("title failed")

        await self.install_page("goto", Page())
        try:
            result = await server.page_navigate(
                "goto", "https://example.test/", _Ctx()
            )
        except Exception as exc:  # 当前缺陷会走到这里，转换成明确的测试失败。
            self.fail(f"page_navigate 泄漏异常：{exc}")

        self.assertTrue(result["ok"])
        self.assertIsNone(result["title"])
        self.assertIn("title failed", result["title_error"])

    async def test_mouse_drag_between_releases_button_after_move_failure(self):
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

        result = await server.mouse_drag_between("drag", 0, 0, 10, 10, _Ctx())

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
    async def test_auto_profile_selects_linux_native_on_linux(self):
        if not sys.platform.startswith("linux"):
            self.skipTest("Linux-specific auto selection")

        class FailConstructor:
            def __init__(self, *args, **kwargs):
                raise ValueError("auto selected linux")

        with (
            patch.object(server, "LinuxNativePlaywright", FailConstructor),
            patch.object(server, "InvisiblePlaywright") as windows_constructor,
        ):
            result = await server.session_start(_Ctx())

        self.assertFalse(result["ok"])
        self.assertIn("auto selected linux", result["error"])
        windows_constructor.assert_not_called()

    async def test_linux_native_profile_uses_linux_launcher(self):
        class FailConstructor:
            def __init__(self, *args, **kwargs):
                self.kwargs = kwargs
                raise ValueError("linux launcher selected")

        with (
            patch.object(server, "LinuxNativePlaywright", FailConstructor),
            patch.object(server, "InvisiblePlaywright") as windows_constructor,
        ):
            result = await server.session_start(
                _Ctx(),
                seed=15,
                fingerprint_profile="linux_native",
            )

        self.assertFalse(result["ok"])
        self.assertIn("linux launcher selected", result["error"])
        windows_constructor.assert_not_called()

    async def test_linux_native_profile_rejects_persistent_profile(self):
        result = await server.session_start(
            _Ctx(),
            profile_dir="/tmp/profile",
            fingerprint_profile="linux_native",
        )

        self.assertFalse(result["ok"])
        self.assertIn("does not support profile_dir", result["error"])

    async def test_unknown_fingerprint_profile_is_rejected(self):
        result = await server.session_start(
            _Ctx(),
            fingerprint_profile="unknown",  # type: ignore[arg-type]
        )

        self.assertFalse(result["ok"])
        self.assertIn("fingerprint_profile must be", result["error"])

    async def test_session_start_cancellation_cleans_unpublished_browser(self):
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
                await server.session_start(_Ctx(), fingerprint_profile="windows")

        self.assertTrue(ipw.exit_called)
        self.assertEqual(server._SESSIONS, {})

    async def test_session_stop_retries_failed_browser_exit(self):
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

        first = await server.session_stop("retry", _Ctx())
        second = await server.session_stop("retry", _Ctx())

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
            result = await server.session_start(_Ctx())

        self.assertFalse(result["ok"])
        self.assertIn("session limit", result["error"])
        constructor.assert_not_called()

    async def test_page_open_limit_closes_untracked_page(self):
        created = _Page()

        async def close():
            created.closed = True

        created.close = close
        context = SimpleNamespace(new_page=AsyncMock(return_value=created))
        session = await self.install_page("page-limit", _Page())
        session.browser_or_ctx = SimpleNamespace(new_context=AsyncMock())
        session.primary_context = context
        with patch.object(server, "_MAX_PAGES_PER_SESSION", 1):
            result = await server.page_open("page-limit", _Ctx())

        self.assertFalse(result["ok"])
        self.assertIn("page limit", result["error"])
        self.assertTrue(created.closed)


class BoundaryTests(_AsyncToolTest):
    async def test_evaluate_zero_timeout_disables_server_timeout(self):
        page = _Page()
        page.evaluate = AsyncMock(return_value={"value": 1})
        await self.install_page("evaluate-zero", page)

        result = await server.page_evaluate(
            "evaluate-zero", "() => ({value: 1})", _Ctx(), timeout_ms=0
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["result"], {"value": 1})

    async def test_output_limit_rejects_excessive_value_before_page_call(self):
        page = _Page()
        page.inner_text = AsyncMock(return_value="text")
        await self.install_page("output-limit", page)

        result = await server.page_get_text(
            "output-limit", _Ctx(), max_chars=server._MAX_TEXT_CHARS + 1
        )

        self.assertFalse(result["ok"])
        page.inner_text.assert_not_awaited()

    async def test_hidden_wait_reports_reached_without_match_confusion(self):
        page = _Page()
        page.wait_for_selector = AsyncMock(return_value=None)
        await self.install_page("hidden", page)

        result = await server.element_wait_for(
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

        result = await server.page_scroll(
            "scroll", _Ctx(), dy=100, selector="#missing"
        )

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

            result = await server.storage_state_save(
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
                result = await server.storage_state_save(
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
            result = await server.binary_install(_Ctx(), force=True)

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
    async def test_browser_start_keeps_agent_configuration_compact(self):
        with patch.object(
            server,
            "session_start",
            AsyncMock(return_value={"ok": True, "session_id": "compact"}),
        ) as start:
            result = await server.browser_start(
                _Ctx(), seed=12, headless=False, humanize=False
            )

        self.assertTrue(result["ok"])
        start.assert_awaited_once_with(
            unittest.mock.ANY,
            seed=12,
            headless=False,
            humanize=False,
            fingerprint_profile="auto",
        )

    async def test_browser_wait_for_uses_page_conditions(self):
        page = _Page()
        page.url = "https://example.test/ready"
        page.inner_text = AsyncMock(side_effect=["Loading", "Everything Ready"])
        await self.install_page("wait-condition", page)

        result = await server.browser_wait_for(
            _Ctx(),
            text="ready",
            url="/ready",
            session_id="wait-condition",
            timeout_ms=1000,
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["matched"])
        self.assertEqual(page.inner_text.await_count, 2)

    async def test_browser_wait_for_requires_a_condition(self):
        result = await server.browser_wait_for(_Ctx())

        self.assertFalse(result["ok"])
        self.assertIn("at least one", result["error"])

    async def test_binary_install_normalizes_download_failure(self):
        def fail(*args, **kwargs):
            raise ValueError("download failed")

        with patch.object(server, "ensure_binary", fail):
            try:
                result = await server.binary_install(_Ctx())
            except Exception as exc:
                self.fail(f"binary_install 泄漏异常：{exc}")

        self.assertFalse(result["ok"])
        self.assertIn("download failed", result["error"])

    async def test_binary_install_serializes_shared_cache_mutation(self):
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
                server.binary_install(_Ctx()), server.binary_install(_Ctx())
            )

        self.assertTrue(all(result["ok"] for result in results))
        self.assertEqual(max_active, 1)

    async def test_session_start_normalizes_constructor_failure(self):
        class FailConstructor:
            def __init__(self, *args, **kwargs):
                raise ValueError("bad launch option")

        with patch.object(server, "InvisiblePlaywright", FailConstructor):
            try:
                result = await server.session_start(
                    _Ctx(), fingerprint_profile="windows"
                )
            except Exception as exc:
                self.fail(f"session_start 泄漏异常：{exc}")

        self.assertFalse(result["ok"])
        self.assertIn("bad launch option", result["error"])

    async def test_session_stop_reports_cleanup_failure(self):
        class IPW:
            async def __aexit__(self, *args):
                raise ValueError("browser exit failed")

        session = server._Session("cleanup", IPW(), object(), 1, False)
        with patch.object(server._log, "warning"):
            errors = await server._close_session(session)

        self.assertTrue(errors)
        self.assertIn("browser exit failed", errors[-1])

    async def test_session_stop_continues_when_page_state_check_fails(self):
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

        result = await server.element_is_visible(
            "visible", "#target", _Ctx(), timeout_ms=25
        )

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

        result = await server.element_is_visible(
            "hidden", "#target", _Ctx(), timeout_ms=25
        )

        self.assertTrue(result["ok"])
        self.assertFalse(result["visible"])

    async def test_negative_output_limits_are_rejected(self):
        page = _Page()
        page.inner_text = AsyncMock(return_value="abcdef")
        page.evaluate = AsyncMock(return_value=[])
        await self.install_page("limits", page)

        text_result = await server.page_get_text("limits", _Ctx(), max_chars=-1)
        elements_result = await server.element_query(
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
            await server.page_screenshot("shot", _Ctx(), image_format="webp")
        page.screenshot.assert_not_awaited()

    def test_element_snapshot_checks_computed_visibility(self):
        self.assertIn("getComputedStyle", server._QUERY_ELEMENTS_JS)
        self.assertNotIn(
            "!!(e.offsetWidth || e.offsetHeight || e.getClientRects().length)",
            server._QUERY_ELEMENTS_JS,
        )

    async def test_agent_snapshot_refs_and_fill_results_are_redacted(self):
        raw = {
            "title": "Login",
            "url": "https://example.test/login",
            "total_candidates": 1,
            "items": [
                {
                    "selector": "#password",
                    "tag": "input",
                    "role": "textbox",
                    "name": "Password",
                    "disabled": False,
                    "checked": None,
                    "selected": None,
                    "value": None,
                }
            ],
        }
        page = _Page()
        page.url = raw["url"]
        page.evaluate = AsyncMock(side_effect=[raw, raw])
        locator = SimpleNamespace(
            count=AsyncMock(return_value=1),
            evaluate=AsyncMock(
                return_value={
                    "tag": "input",
                    "role": "textbox",
                    "name": "Password",
                    "visible": True,
                }
            ),
            fill=AsyncMock(return_value=None),
        )
        page.locator = lambda selector: locator
        await self.install_page("snapshot-ref", page)

        snapshot = await server.browser_snapshot(_Ctx(), "snapshot-ref")
        ref = snapshot["elements"][0]["ref"]
        result = await server.browser_set_value_ref(
            ref, "super-secret", _Ctx(), "snapshot-ref"
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["length"], 12)
        self.assertNotIn("super-secret", json.dumps(result))
        locator.fill.assert_awaited_once_with("super-secret", timeout=30000)

    async def test_agent_can_list_and_switch_tabs(self):
        first = _Page()
        first.url = "https://first.test/"
        first.title = AsyncMock(return_value="First")
        second = _Page()
        second.url = "https://second.test/"
        second.title = AsyncMock(return_value="Second")
        session = await self.install_page("tabs", first)
        session.pages[2] = second

        listed = await server.browser_list_tabs(_Ctx(), "tabs")
        switched = await server.browser_activate_tab(2, _Ctx(), "tabs")

        self.assertTrue(listed["ok"])
        self.assertEqual([tab["page_id"] for tab in listed["tabs"]], [1, 2])
        self.assertEqual(listed["active_page_id"], 1)
        self.assertTrue(switched["ok"])
        self.assertEqual(switched["active_page_id"], 2)
        self.assertEqual(session.active_page_id, 2)

    async def test_browser_status_exposes_effective_policy_and_capabilities(self):
        page = _Page()
        page.title = AsyncMock(return_value="Status")
        session = await self.install_page("status-fields", page)
        session.humanize = False
        session.dialog_action = "dismiss"
        session.dialog_prompt_text = "configured"

        result = await server.browser_status(_Ctx(), "status-fields")

        self.assertTrue(result["ok"])
        self.assertFalse(result["humanize"])
        self.assertEqual(
            result["dialog_policy"],
            {"action": "dismiss", "prompt_text_configured": True},
        )
        self.assertTrue(result["page_capabilities"]["active_page"])
        self.assertTrue(result["page_capabilities"]["snapshot_refs"])
        self.assertTrue(result["page_capabilities"]["tabs"])

    async def test_agent_snapshot_and_click_ref_include_child_frames(self):
        top_raw = {
            "title": "Checkout",
            "url": "https://shop.test/",
            "total_candidates": 0,
            "items": [],
        }
        frame_raw = {
            "title": "Payment",
            "url": "https://pay.test/widget",
            "total_candidates": 1,
            "items": [
                {
                    "selector": "#pay",
                    "tag": "button",
                    "role": "button",
                    "name": "Pay",
                    "disabled": False,
                    "checked": None,
                    "selected": None,
                    "value": None,
                }
            ],
        }
        locator = SimpleNamespace(
            count=AsyncMock(return_value=1),
            evaluate=AsyncMock(
                return_value={
                    "tag": "button",
                    "role": "button",
                    "name": "Pay",
                    "visible": True,
                }
            ),
            click=AsyncMock(return_value=None),
        )
        main_frame = SimpleNamespace(parent_frame=None)
        child_frame = SimpleNamespace(
            parent_frame=main_frame,
            name="payment",
            url=frame_raw["url"],
            evaluate=AsyncMock(return_value=frame_raw),
            locator=lambda selector: locator,
            is_detached=lambda: False,
        )
        page = _Page()
        page.url = top_raw["url"]
        page.main_frame = main_frame
        page.frames = [main_frame, child_frame]
        page.evaluate = AsyncMock(return_value=top_raw)
        await self.install_page("frame-ref", page)

        snapshot = await server.browser_snapshot(_Ctx(), "frame-ref")
        ref = snapshot["elements"][0]["ref"]
        result = await server.browser_click_ref(ref, _Ctx(), "frame-ref")

        self.assertTrue(snapshot["ok"])
        self.assertEqual(snapshot["frame_count"], 1)
        self.assertIn(":f1:", ref)
        self.assertIn('iframe "payment" [frame=f1]', snapshot["snapshot"])
        self.assertTrue(result["ok"])
        locator.click.assert_awaited_once_with(timeout=30000)
        page.evaluate.assert_any_await(server._SNAPSHOT_JS, 80)
        child_frame.evaluate.assert_any_await(server._SNAPSHOT_JS, 40)

    async def test_recent_snapshot_refs_remain_usable_per_page(self):
        first_raw = {
            "title": "Page",
            "url": "https://example.test/",
            "total_candidates": 1,
            "items": [
                {
                    "selector": "#first",
                    "tag": "button",
                    "role": "button",
                    "name": "First",
                    "disabled": False,
                    "checked": None,
                    "selected": None,
                    "value": None,
                }
            ],
        }
        second_raw = {
            **first_raw,
            "items": [
                {
                    **first_raw["items"][0],
                    "selector": "#second",
                    "name": "Second",
                }
            ],
        }
        first_locator = SimpleNamespace(
            count=AsyncMock(return_value=1),
            evaluate=AsyncMock(
                return_value={
                    "tag": "button",
                    "role": "button",
                    "name": "First",
                    "visible": True,
                }
            ),
            click=AsyncMock(return_value=None),
        )
        page = _Page()
        page.evaluate = AsyncMock(side_effect=[first_raw, second_raw])
        page.locator = lambda selector: first_locator
        await self.install_page("snapshot-history", page)

        first = await server.browser_snapshot(_Ctx(), "snapshot-history")
        second = await server.browser_snapshot(_Ctx(), "snapshot-history")
        result = await server.browser_click_ref(
            first["elements"][0]["ref"],
            _Ctx(),
            "snapshot-history",
            observe="none",
        )

        self.assertNotEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertTrue(first["elements"][0]["ref"].startswith(first["snapshot_id"]))
        self.assertTrue(result["ok"])
        first_locator.click.assert_awaited_once_with(timeout=30000)

    async def test_agent_snapshot_reports_inaccessible_frames(self):
        top_raw = {
            "title": "Protected",
            "url": "https://example.test/",
            "total_candidates": 0,
            "items": [],
            "child_frames": [
                {
                    "id": None,
                    "name": None,
                    "title": "Security challenge",
                    "aria_label": None,
                    "src": "https://challenge.test/",
                    "visible": True,
                    "rect": {"x": 10, "y": 20, "w": 300, "h": 80},
                }
            ],
        }
        main_frame = SimpleNamespace(parent_frame=None)
        child_frame = SimpleNamespace(
            parent_frame=main_frame,
            name="",
            url="",
            evaluate=AsyncMock(side_effect=RuntimeError("permission denied")),
        )
        page = _Page()
        page.url = top_raw["url"]
        page.main_frame = main_frame
        page.frames = [main_frame, child_frame]
        page.evaluate = AsyncMock(return_value=top_raw)
        await self.install_page("inaccessible-frame", page)

        snapshot = await server.browser_snapshot(_Ctx(), "inaccessible-frame")

        self.assertTrue(snapshot["ok"])
        self.assertEqual(snapshot["frame_count"], 1)
        self.assertFalse(snapshot["frames"][0]["accessible"])
        self.assertIn("permission denied", snapshot["frames"][0]["error"])
        self.assertIn(
            'iframe "Security challenge" [frame=f1 inaccessible]',
            snapshot["snapshot"],
        )

    async def test_get_text_supports_pagination(self):
        page = _Page()
        page.inner_text = AsyncMock(return_value="abcdefghij")
        await self.install_page("paginate", page)

        result = await server.page_get_text(
            "paginate", _Ctx(), max_chars=4, offset=3
        )

        self.assertEqual(result["text"], "defg")
        self.assertEqual(result["next_offset"], 7)
        self.assertTrue(result["truncated"])

    async def test_browser_snapshot_filters_controls_without_separate_tool(self):
        raw = {
            "title": "Actions",
            "url": "https://example.test/",
            "total_candidates": 2,
            "items": [
                {
                    "selector": "#save",
                    "tag": "button",
                    "role": "button",
                    "name": "Save changes",
                    "disabled": False,
                    "checked": None,
                    "selected": None,
                    "value": None,
                },
                {
                    "selector": "#cancel",
                    "tag": "button",
                    "role": "button",
                    "name": "Cancel",
                    "disabled": False,
                    "checked": None,
                    "selected": None,
                    "value": None,
                },
            ],
        }
        page = _Page()
        page.evaluate = AsyncMock(return_value=raw)
        await self.install_page("snapshot-filter", page)

        result = await server.browser_snapshot(
            _Ctx(), "snapshot-filter", query="save", role="button"
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["match_count"], 1)
        self.assertEqual(result["matches"][0]["name"], "Save changes")

    async def test_browser_find_text_uses_explicit_agent_name(self):
        page = _Page()
        page.inner_text = AsyncMock(return_value="Alpha target Omega")
        await self.install_page("find-text", page)

        result = await server.browser_find_text(
            "target", _Ctx(), "find-text"
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 1)

    async def test_protocol_boundary_turns_error_dict_into_tool_error(self):
        tool = server.mcp._tool_manager._tools["browser_status"]
        with self.assertRaises(ToolError) as caught:
            await tool.run({}, convert_result=True)

        payload = str(caught.exception)
        self.assertIn('"code":"SESSION_NOT_FOUND"', payload)
        self.assertIn('"retryable":false', payload)

    def test_protocol_output_schema_is_unwrapped_and_typed(self):
        tool = server.mcp._tool_manager._tools["browser_status"]
        schema = tool.output_schema

        self.assertIn("session_id", schema["properties"])
        self.assertIn("humanize", schema["properties"])
        self.assertIn("dialog_policy", schema["properties"])
        self.assertIn("page_capabilities", schema["properties"])
        self.assertNotIn("result", schema["properties"])

    def test_agent_browser_tools_do_not_require_session_id(self):
        tools = asyncio.run(server.mcp.list_tools())
        for tool in tools:
            if tool.name.startswith("browser_"):
                self.assertNotIn("session_id", tool.inputSchema.get("required", []))

    def test_browser_start_schema_has_only_agent_level_options(self):
        tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
        tool = tools["browser_start"]

        self.assertEqual(
            set(tool.inputSchema["properties"]),
            {"seed", "headless", "humanize", "fingerprint_profile"},
        )

    def test_error_sanitizer_redacts_url_credentials_and_auth_headers(self):
        message = server._sanitize_message(
            "proxy http://user:pass@example.test Authorization: Bearer-token"
        )
        self.assertNotIn("user:pass", message)
        self.assertNotIn("Bearer-token", message)

    def test_query_elements_redacts_password_values(self):
        self.assertIn("!== 'password'", server._QUERY_ELEMENTS_JS)


class MetadataTests(unittest.TestCase):
    def test_every_registered_tool_has_a_description(self):
        tools = asyncio.run(server.mcp.list_tools())
        expected_counts = {"setup": 2, "agent": 19, "core": 28, "full": 73}
        self.assertEqual(len(tools), expected_counts[server._TOOL_PROFILE])
        self.assertEqual(
            [tool.name for tool in tools if not (tool.description or "").strip()], []
        )

    def test_tool_profiles_register_expected_tools(self):
        script = (
            "import asyncio,json; import spectra_mcp.server as s; "
            "print(json.dumps([t.name for t in asyncio.run(s.mcp.list_tools())]))"
        )
        expected_counts = {"setup": 2, "agent": 19, "core": 28, "full": 73}
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
                    self.assertEqual(names, ["binary_status", "binary_install"])
                if profile == "core":
                    self.assertIn("session_start", names)
                    self.assertIn("page_scroll", names)
                    self.assertNotIn("frame_click", names)
                if profile == "agent":
                    self.assertIn("browser_snapshot", names)
                    self.assertIn("browser_set_form_values", names)
                    self.assertIn("browser_list_tabs", names)
                    self.assertIn("browser_activate_tab", names)
                    self.assertIn("browser_wait_for", names)
                    self.assertIn("browser_find_text", names)
                    self.assertNotIn("browser_wait", names)
                    self.assertNotIn("snapshot_find", names)
                    self.assertNotIn("find_text", names)
                    self.assertNotIn("page_evaluate", names)
                if profile == "full":
                    self.assertIn("frame_click", names)
                    self.assertIn("storage_state_save", names)
                    legacy_names = {
                        "fetch_binary", "start_session", "close_session",
                        "list_sessions", "session_info", "browser_tabs",
                        "browser_switch_page", "click_ref", "fill_ref",
                        "type_ref", "select_ref", "fill_form", "new_page",
                        "close_page", "switch_page", "goto", "go_forward",
                        "reload", "click", "fill", "type_text", "press_key",
                        "keyboard_press", "select_option", "hover", "focus",
                        "check", "uncheck", "scroll", "get_text", "get_html",
                        "get_attribute", "query_elements", "is_visible",
                        "screenshot", "wait_for_selector", "wait_for_timeout",
                        "browser_wait", "evaluate", "mouse_drag", "mouse_move",
                        "mouse_click", "keyboard_down", "keyboard_up",
                        "keyboard_type", "set_dialog_handler", "get_dialogs",
                        "wait_for_page", "get_cookies", "add_cookies",
                        "clear_cookies", "save_storage_state", "list_frames",
                        "frame_fill", "frame_type", "frame_wait_for_selector",
                        "frame_query_elements",
                    }
                    self.assertTrue(legacy_names.isdisjoint(names))

    def test_tool_schema_exposes_enum_constraints(self):
        script = (
            "import asyncio,json; import spectra_mcp.server as s; "
            "print(json.dumps({t.name:t.inputSchema for t in asyncio.run(s.mcp.list_tools())}))"
        )
        env = os.environ.copy()
        env["SPECTRA_MCP_TOOL_PROFILE"] = "full"
        env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
        tools = json.loads(
            subprocess.check_output(
                [sys.executable, "-c", script], cwd=ROOT, env=env, text=True
            )
        )
        self.assertEqual(
            tools["element_click"]["properties"]["button"]["enum"],
            ["left", "middle", "right"],
        )
        self.assertEqual(
            tools["element_wait_for"]["properties"]["state"]["enum"],
            ["attached", "detached", "hidden", "visible"],
        )
        self.assertEqual(
            tools["browser_click_ref"]["properties"]["observe"]["enum"],
            ["none", "compact", "full"],
        )

    def test_observation_modes_control_snapshot_detail(self):
        snapshot = {
            "ok": True,
            "session_id": "s",
            "snapshot_id": "p1s1",
            "page_id": 1,
            "url": "https://example.test/",
            "title": "Example",
            "count": 1,
            "frame_count": 0,
            "truncated": False,
            "snapshot": '- button "Go" [ref=p1s1:e1]',
            "elements": [{"ref": "p1s1:e1"}],
            "frames": [],
        }

        self.assertIsNone(server._observation_payload(snapshot, "none"))
        compact = server._observation_payload(snapshot, "compact")
        self.assertNotIn("elements", compact)
        self.assertNotIn("frames", compact)
        self.assertIs(server._observation_payload(snapshot, "full"), snapshot)

    def test_structured_tool_schemas_are_precise(self):
        script = (
            "import asyncio,json; import spectra_mcp.server as s; "
            "print(json.dumps({t.name:{'input':t.inputSchema,'output':t.outputSchema} "
            "for t in asyncio.run(s.mcp.list_tools())}))"
        )
        env = os.environ.copy()
        env["SPECTRA_MCP_TOOL_PROFILE"] = "full"
        env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
        tools = json.loads(
            subprocess.check_output(
                [sys.executable, "-c", script], cwd=ROOT, env=env, text=True
            )
        )

        fill_schema = tools["browser_set_form_values"]["input"]
        form_ref = fill_schema["properties"]["fields"]["items"]["$ref"]
        form_name = form_ref.rsplit("/", 1)[-1]
        form = fill_schema["$defs"][form_name]
        self.assertEqual(set(form["required"]), {"ref", "value"})
        self.assertFalse(form["additionalProperties"])

        cookie_schema = tools["cookie_add"]["input"]
        cookie_items = cookie_schema["properties"]["cookies"]["items"]
        self.assertIn("anyOf", cookie_items)

        snapshot_schema = tools["browser_snapshot"]["output"]
        self.assertEqual(
            snapshot_schema["properties"]["elements"]["items"]["$ref"],
            "#/$defs/SnapshotElement",
        )
        self.assertEqual(
            snapshot_schema["properties"]["frames"]["items"]["$ref"],
            "#/$defs/SnapshotFrame",
        )

    def test_documentation_mentions_every_tool_and_storage_state_parameter(self):
        documentation = "\n".join(
            path.read_text(encoding="utf-8")
            for path in [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]
        )
        tools = asyncio.run(server.mcp.list_tools())
        missing = [
            tool.name
            for tool in tools
            if f"`{tool.name}`" not in documentation
        ]

        self.assertEqual(missing, [])
        self.assertIn("`storage_state_path`", documentation)

    def test_release_metadata_uses_loworbitlab_identity(self):
        document = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        project = document["project"]

        self.assertEqual(project["name"], "spectra_mcp")
        self.assertEqual(
            project["scripts"], {"spectra_mcp": "spectra_mcp.server:main"}
        )
        self.assertEqual(project["authors"], [{"name": "LowOrbitLab"}])
        self.assertTrue((ROOT / "LICENSE").is_file())
        self.assertNotIn("anyio>=4", project["dependencies"])
        self.assertTrue(
            any(
                dependency.startswith("invisible-playwright @ git+")
                and "@a1b75d93e928c37130833d667ae1acf15a23d027" in dependency
                for dependency in project["dependencies"]
            )
        )
        self.assertIn("playwright>=1.40,<1.61", project["dependencies"])
        self.assertIn("mcp>=1.2,<2", project["dependencies"])

    def test_logger_does_not_duplicate_messages_through_root(self):
        self.assertFalse(server._log.propagate)


if __name__ == "__main__":
    unittest.main()

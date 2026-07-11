"""MCP server exposing the invisible_playwright anti-detect browser.

The server keeps long-lived browser sessions in-process (an MCP server is a
resident process, which fits a stateful browser perfectly). Each session owns
an ``InvisiblePlaywright`` instance plus one or more Playwright pages. Tools
operate on the session's *active* page unless told otherwise.

Requires the one-time patched Firefox binary: call the ``fetch_binary`` tool
(or ``python -m invisible_playwright fetch`` on the CLI) before starting a
session.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import sys
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from mcp.server.fastmcp import Context, FastMCP, Image
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

# invisible_playwright ships an async façade that is a drop-in for the
# Playwright async API and returns a standard playwright Browser/BrowserContext.
from invisible_playwright.async_api import InvisiblePlaywright
from invisible_playwright.download import (
    BINARY_ENTRY_REL,
    BINARY_VERSION,
    BROKEN_VERSIONS,
    cache_dir_for_version,
    cache_root,
    ensure_binary,
)

# Logging MUST go to stderr: stdio transport reserves stdout for JSON-RPC.
_log = logging.getLogger("spectra_mcp")
if not _log.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    _log.addHandler(_h)
    _log.setLevel(logging.INFO)
_log.propagate = False


# --------------------------------------------------------------------------- #
# Session store
# --------------------------------------------------------------------------- #


@dataclass
class _Session:
    session_id: str
    ipw: InvisiblePlaywright
    browser_or_ctx: Any  # playwright.async_api.Browser | BrowserContext
    seed: int
    persistent: bool
    primary_context: Any = None  # cached shared BrowserContext (Browser path only)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)  # lifecycle guard (close-vs-inflight)
    pages: Dict[int, Any] = field(default_factory=dict)  # page_id -> Page
    next_page_id: int = 1
    active_page_id: Optional[int] = None
    ready: bool = True
    closed: bool = False
    cleanup_complete: bool = False
    cleanup_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    cleanup_task: Optional[asyncio.Task] = field(default=None, repr=False)
    # --- capability state (all defaulted; mutable ones need default_factory or
    # the dataclass raises at import) ---
    dialog_action: str = "accept"  # how JS dialogs are auto-handled: accept|dismiss
    dialog_prompt_text: str = ""  # text submitted for prompt() when accepting
    dialog_log: List[dict] = field(default_factory=list)  # capped, newest-last
    storage_state_path: Optional[str] = None  # seed cookies/localStorage at ctx creation
    _ctx_handlers_done: bool = False  # idempotency guard for context.on("dialog")


_SESSIONS: Dict[str, _Session] = {}
_LOCK = asyncio.Lock()
_BINARY_LOCK = asyncio.Lock()
_STARTING_SESSIONS = 0

# How long _close_session waits for an in-flight tool to finish before
# force-closing pages. Long Playwright calls (e.g. goto with a big timeout)
# that don't yield within this window get force-closed; their tool's except
# block sees s.closed=True and returns a clean _err("session closed").
_CLOSE_DRAIN_TIMEOUT = 5.0

# 单个 Playwright 清理动作的最长等待时间，防止服务退出永久卡住。
_CLEANUP_TIMEOUT = 10.0

# Max JS dialogs retained per session in _Session.dialog_log (newest kept).
_DIALOG_CAP = 50


def _env_positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        value = int(raw)
        if value <= 0:
            raise ValueError
        return value
    except ValueError:
        _log.warning("invalid %s=%r; using %d", name, raw, default)
        return default


# 常驻服务的默认资源边界，可通过环境变量覆盖。
_MAX_SESSIONS = _env_positive_int("SPECTRA_MCP_MAX_SESSIONS", 8)
_MAX_PAGES_PER_SESSION = _env_positive_int(
    "SPECTRA_MCP_MAX_PAGES_PER_SESSION", 32
)
_MAX_TEXT_CHARS = _env_positive_int("SPECTRA_MCP_MAX_TEXT_CHARS", 200000)
_MAX_ELEMENT_RESULTS = _env_positive_int(
    "SPECTRA_MCP_MAX_ELEMENT_RESULTS", 500
)
_MAX_TIMEOUT_MS = _env_positive_int("SPECTRA_MCP_MAX_TIMEOUT_MS", 300000)
_MAX_DELAY_MS = _env_positive_int("SPECTRA_MCP_MAX_DELAY_MS", 10000)
_MAX_CLICK_COUNT = _env_positive_int("SPECTRA_MCP_MAX_CLICK_COUNT", 10)
_MAX_MOUSE_STEPS = _env_positive_int("SPECTRA_MCP_MAX_MOUSE_STEPS", 1000)
_MAX_SCREENSHOT_BYTES = _env_positive_int(
    "SPECTRA_MCP_MAX_SCREENSHOT_BYTES", 10 * 1024 * 1024
)
_MAX_SCREENSHOT_PIXELS = _env_positive_int(
    "SPECTRA_MCP_MAX_SCREENSHOT_PIXELS", 50000000
)
_MAX_EVALUATE_RESULT_BYTES = _env_positive_int(
    "SPECTRA_MCP_MAX_EVALUATE_RESULT_BYTES", 1024 * 1024
)
_DATA_ROOT = os.environ.get("SPECTRA_MCP_DATA_ROOT", "").strip()


def _ok(**kw: Any) -> Dict[str, Any]:
    kw["ok"] = True
    return kw


def _err(msg: str, **kw: Any) -> Dict[str, Any]:
    kw["ok"] = False
    kw["error"] = msg
    return kw


def _range_error(name: str, value: int, minimum: int, maximum: int) -> Optional[str]:
    if value < minimum or value > maximum:
        return f"{name} must be between {minimum} and {maximum}"
    return None


def _timeout_error(timeout_ms: int) -> Optional[str]:
    return _range_error("timeout_ms", timeout_ms, 0, _MAX_TIMEOUT_MS)


async def _await_with_timeout(awaitable: Any, timeout_ms: int) -> Any:
    if timeout_ms == 0:
        return await awaitable
    return await asyncio.wait_for(awaitable, timeout=timeout_ms / 1000)


def _serialized_size(value: Any) -> int:
    return len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )


def _resolve_data_path(path: str, *, must_exist: bool = False) -> Path:
    target = Path(path).expanduser().resolve()
    if _DATA_ROOT:
        root = Path(_DATA_ROOT).expanduser().resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"path must stay within configured data root: {root}") from exc
    if must_exist and not target.exists():
        raise ValueError(f"path not found: {str(target)!r}")
    return target


def _write_private_json_atomic(target: Path, data: Any) -> None:
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            os.chmod(tmp, 0o600)
        os.replace(tmp, target)
        if os.name != "nt":
            os.chmod(target, 0o600)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _acquire_process_file_lock(path: Path) -> Any:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return handle
    except BaseException:
        handle.close()
        raise


def _release_process_file_lock(handle: Any) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


@asynccontextmanager
async def _binary_cache_guard():
    async with _BINARY_LOCK:
        lock_path = Path(cache_root()) / ".spectra-mcp.lock"
        acquire_task = asyncio.create_task(
            asyncio.to_thread(_acquire_process_file_lock, lock_path)
        )
        try:
            handle = await asyncio.shield(acquire_task)
        except asyncio.CancelledError:
            handle = await acquire_task
            await asyncio.shield(
                asyncio.to_thread(_release_process_file_lock, handle)
            )
            raise
        try:
            yield
        finally:
            await asyncio.shield(
                asyncio.to_thread(_release_process_file_lock, handle)
            )


async def _get_session(session_id: str) -> _Session:
    async with _LOCK:
        s = _SESSIONS.get(session_id)
    if s is None:
        raise KeyError(f"unknown session_id: {session_id!r}")
    if not s.ready:
        raise KeyError(f"session {session_id!r} is not ready")
    if s.closed:
        raise KeyError(f"session {session_id!r} is closed")
    return s


def _active_page(session: _Session) -> Any:
    if session.active_page_id is None:
        raise RuntimeError("session has no active page; call new_page first")
    page = session.pages.get(session.active_page_id)
    if page is None:
        raise RuntimeError("active page is gone; switch_page or new_page")
    try:
        closed = page.is_closed()
    except Exception as exc:
        raise RuntimeError(f"active page state check failed: {exc}") from exc
    if closed:
        raise RuntimeError("active page is gone; switch_page or new_page")
    return page


# Shared JS for query_elements / frame_query_elements: a compact, LLM-friendly
# snapshot of matched elements (used against a Page or a Frame realm).
_QUERY_ELEMENTS_JS = """
(params) => {
  const els = Array.from(document.querySelectorAll(params.sel)).slice(0, params.limit);
  return els.map(e => {
    const r = e.getBoundingClientRect();
    const style = getComputedStyle(e);
    return {
      tag: e.tagName.toLowerCase(),
      id: e.id || null,
      name: e.getAttribute('name'),
      type: e.getAttribute('type'),
      role: e.getAttribute('role'),
      href: e.tagName === 'A' ? (e.href || null) : null,
      value: ('value' in e) ? e.value : null,
      placeholder: e.getAttribute('placeholder'),
      text: (e.innerText || '').slice(0, 200),
      visible: style.visibility === 'visible' && !!(r.width || r.height),
      rect: { x: r.x, y: r.y, w: r.width, h: r.height }
    };
  });
}
"""


def _alloc_pid(session: _Session) -> int:
    """Allocate the next page id. Centralized so _make_page and the popup handler
    share one counter; always call AFTER the Page object exists so a popup
    arriving during an ``await ctx.new_page()`` can't collide."""
    pid = session.next_page_id
    session.next_page_id += 1
    return pid


def _tracked_page_id(session: _Session, page: Any) -> Optional[int]:
    for pid, tracked in list(session.pages.items()):
        if tracked is page:
            return pid
    return None


def _track_page(session: _Session, page: Any, activate: bool = False) -> int:
    existing = _tracked_page_id(session, page)
    if existing is not None:
        if activate:
            session.active_page_id = existing
        return existing
    if len(session.pages) >= _MAX_PAGES_PER_SESSION:
        raise RuntimeError(
            f"page limit reached ({_MAX_PAGES_PER_SESSION} per session)"
        )
    pid = _alloc_pid(session)
    session.pages[pid] = page
    if activate:
        session.active_page_id = pid
    _register_page_handlers(session, page)
    return pid


def _close_untracked_page(page: Any) -> None:
    async def _close() -> None:
        try:
            if not page.is_closed():
                await page.close()
        except Exception as exc:
            _log.warning("failed to close untracked page: %s", exc)

    task = asyncio.create_task(_close())
    task.add_done_callback(
        lambda done: done.exception() if not done.cancelled() else None
    )


def _context_of(session: _Session) -> Any:
    """The session's BrowserContext: the persistent context itself, or the shared
    ``primary_context`` cached on the Browser path (None until _make_page runs)."""
    return session.browser_or_ctx if session.persistent else session.primary_context


async def _on_dialog(session: _Session, dialog: Any) -> None:
    """Auto-handle a JS dialog per the session policy.

    Registering any dialog listener means Playwright will NOT auto-dismiss — the
    handler MUST accept or dismiss or the page freezes. So we handle FIRST (with a
    dismiss fallback) and only then record it; nothing above the accept/dismiss is
    allowed to throw. Runs lock-free (a dialog fires *during* a lock-holding tool's
    await, e.g. click → alert(); taking the lock here would deadlock)."""
    try:
        rec = {"type": dialog.type, "message": dialog.message,
               "default_value": dialog.default_value}
    except Exception:
        rec = {"type": None, "message": None, "default_value": None}
    action = session.dialog_action
    try:
        if action == "accept":
            if rec["type"] == "prompt":
                await dialog.accept(session.dialog_prompt_text)
            else:
                await dialog.accept()
        else:
            await dialog.dismiss()
        rec["action"] = action
    except Exception as exc:
        if action == "accept":
            try:
                await dialog.dismiss()
                rec["action"] = "dismiss_fallback"
                _log.warning(
                    "dialog accept failed on session %s; dismissed instead: %s",
                    session.session_id,
                    exc,
                )
            except Exception as fallback_exc:
                rec["action"] = (
                    f"error: {exc}; dismiss fallback failed: {fallback_exc}"
                )
                _log.warning(
                    "dialog handling failed on session %s: %s; "
                    "dismiss fallback failed: %s",
                    session.session_id,
                    exc,
                    fallback_exc,
                )
        else:
            rec["action"] = f"error: {exc}"
            _log.warning(
                "dialog handling failed on session %s: %s", session.session_id, exc
            )
    session.dialog_log.append(rec)
    if len(session.dialog_log) > _DIALOG_CAP:
        del session.dialog_log[: len(session.dialog_log) - _DIALOG_CAP]


def _on_popup(session: _Session, popup: Any) -> None:
    """Adopt a popup / new tab (OAuth, window.open, target=_blank) into the session.

    Sync (runs inline during the emit): assign an id and track it so session_info /
    switch_page see it. Does NOT steal focus — active_page_id is unchanged. Also
    registers handlers on the popup so it can spawn its own popups."""
    if session.closed:
        _close_untracked_page(popup)
        return
    try:
        _track_page(session, popup)
    except RuntimeError as exc:
        _log.warning("popup rejected for session %s: %s", session.session_id, exc)
        _close_untracked_page(popup)


def _register_page_handlers(session: _Session, page: Any) -> None:
    """Per-page handlers. Popups are a page-level event; dialogs are handled at the
    context level (see _register_context_handlers), so they aren't wired here."""
    page.on("popup", partial(_on_popup, session))


def _register_context_handlers(session: _Session) -> None:
    """Register the dialog listener once, at CONTEXT level, so it covers every tab
    (incl. popups) and the ``dialog.page is None`` case. Idempotent."""
    if session._ctx_handlers_done:
        return
    c = _context_of(session)
    if c is None:
        return
    c.on("dialog", partial(_on_dialog, session))
    session._ctx_handlers_done = True


def _frame_locator(page: Any, frame_selector: str) -> Any:
    """Resolve a (possibly nested) iframe to a FrameLocator. ``frame_selector`` is a
    CSS selector for the <iframe>; chain nested frames with ``>>>``. FrameLocator
    auto-waits for the frame at action time (a wrong selector surfaces as a timeout)."""
    segments = [seg.strip() for seg in frame_selector.split(">>>") if seg.strip()]
    if not segments:
        raise ValueError("frame_selector is empty")
    fl = page.frame_locator(segments[0])
    for seg in segments[1:]:
        fl = fl.frame_locator(seg)
    return fl


async def _resolve_frame(page: Any, frame_selector: str) -> Any:
    """Resolve a (possibly nested) iframe to a real Frame via element-handle descent
    (``query_selector(seg).content_frame()`` per ``>>>`` segment). Needed for
    frame.evaluate(), which the page-level evaluate can't reach across frames.
    Does not long-wait: a missing/absent frame raises promptly."""
    segments = [seg.strip() for seg in frame_selector.split(">>>") if seg.strip()]
    if not segments:
        raise ValueError("frame_selector is empty")
    node = page
    frame = None
    for seg in segments:
        el = await node.query_selector(seg)
        if el is None:
            raise RuntimeError(f"iframe not found: {seg!r}")
        frame = await el.content_frame()
        if frame is None:
            raise RuntimeError(f"element is not an iframe: {seg!r}")
        node = frame
    return frame


async def _make_page(session: _Session) -> Any:
    """Create a new page (real tab) in the session's shared BrowserContext.

    On the Browser path the first call creates the (patched) BrowserContext and
    caches it on the session; subsequent calls open pages in that same context,
    so all pages share cookies/storage — matching the persistent path and real
    browser tab behaviour. The patched ``new_context()`` injects fingerprint
    viewport/screen/timezone/locale defaults once, at context creation, and
    forwards ``storage_state=`` when the session was started with one.
    """
    boc = session.browser_or_ctx
    if hasattr(boc, "new_context"):
        if session.primary_context is None:
            kwargs: Dict[str, Any] = {}
            if session.storage_state_path:
                # Seed cookies+localStorage at creation (applied exactly once;
                # later new_page() calls reuse this cached context).
                kwargs["storage_state"] = session.storage_state_path
            ctx = await boc.new_context(**kwargs)
            session.primary_context = ctx
        else:
            ctx = session.primary_context
        page = await ctx.new_page()
    else:
        # Persistent BrowserContext path: new_page() shares the single context.
        page = await boc.new_page()
    try:
        pid = _track_page(session, page, activate=True)
    except RuntimeError:
        try:
            await page.close()
        except Exception:
            pass
        raise
    return page, pid


async def _initialize_session_page(session: _Session) -> Any:
    if session.persistent:
        existing_pages = list(getattr(session.browser_or_ctx, "pages", []))
        first_page = None
        first_pid = None
        for page in existing_pages:
            try:
                if page.is_closed():
                    continue
                pid = _track_page(session, page, activate=first_page is None)
            except RuntimeError:
                _close_untracked_page(page)
                continue
            except Exception:
                continue
            if first_page is None:
                first_page = page
                first_pid = pid
        if first_page is not None:
            return first_page, first_pid
    return await _make_page(session)


async def _run_cleanup(label: str, awaitable: Any, errors: List[str]) -> bool:
    try:
        await asyncio.wait_for(awaitable, timeout=_CLEANUP_TIMEOUT)
        return True
    except asyncio.TimeoutError:
        message = f"{label} timed out after {_CLEANUP_TIMEOUT:.1f}s"
        errors.append(message)
        _log.warning("session cleanup: %s", message)
    except Exception as exc:
        message = f"{label} failed: {exc}"
        errors.append(message)
        _log.warning("session cleanup: %s", message)
    return False


async def _close_session(session: _Session) -> List[str]:
    async with session.cleanup_lock:
        if session.cleanup_complete:
            return []
        # closed 只表示拒绝新调用；cleanup_complete 才表示资源已经回收。
        session.closed = True
        session.ready = False
        errors: List[str] = []
        got_lock = False
        try:
            await asyncio.wait_for(
                session.lock.acquire(), timeout=_CLOSE_DRAIN_TIMEOUT
            )
            got_lock = True
        except asyncio.TimeoutError:
            _log.warning(
                "close_session %s: in-flight tool did not yield within %.1fs; "
                "force-closing (best-effort: in-flight tail code may race with teardown)",
                session.session_id,
                _CLOSE_DRAIN_TIMEOUT,
            )
        try:
            for pid, page in list(session.pages.items()):
                try:
                    closed = page.is_closed()
                except Exception as exc:
                    message = f"page {pid} state check failed: {exc}"
                    _log.warning("session cleanup: %s", message)
                    closed = False
                if closed:
                    session.pages.pop(pid, None)
                    continue
                if await _run_cleanup(f"page {pid} close", page.close(), errors):
                    session.pages.pop(pid, None)
            if session.primary_context is not None:
                if await _run_cleanup(
                    "primary context close",
                    session.primary_context.close(),
                    errors,
                ):
                    session.primary_context = None
            exited = await _run_cleanup(
                "browser exit", session.ipw.__aexit__(None, None, None), errors
            )
            if exited:
                session.cleanup_complete = True
                session.pages.clear()
                session.primary_context = None
                session.active_page_id = None
        finally:
            if got_lock:
                session.lock.release()
        return errors


async def _await_session_cleanup(session: _Session) -> List[str]:
    task = session.cleanup_task
    if task is None or task.done():
        task = asyncio.create_task(_close_session(session))
        session.cleanup_task = task
    try:
        return await asyncio.shield(task)
    finally:
        if task.done() and session.cleanup_task is task:
            session.cleanup_task = None


async def _remove_completed_session(session: _Session) -> None:
    if not session.cleanup_complete:
        return
    async with _LOCK:
        if _SESSIONS.get(session.session_id) is session:
            _SESSIONS.pop(session.session_id, None)


async def _retain_session_for_cleanup(session: _Session) -> None:
    async with _LOCK:
        _SESSIONS.setdefault(session.session_id, session)


async def _cleanup_failed_start(session: _Session) -> List[str]:
    await _retain_session_for_cleanup(session)
    errors = await _await_session_cleanup(session)
    await _remove_completed_session(session)
    return errors


async def _close_all_sessions() -> None:
    async with _LOCK:
        sessions = list(_SESSIONS.values())
    for s in sessions:
        errors = await _await_session_cleanup(s)
        await _remove_completed_session(s)
        if errors:
            _log.warning(
                "session %s closed with %d cleanup error(s)", s.session_id, len(errors)
            )


@asynccontextmanager
async def _lifespan(app: FastMCP):
    try:
        yield {}
    finally:
        await _close_all_sessions()


mcp = FastMCP("spectra-mcp", lifespan=_lifespan)

_SETUP_TOOL_NAMES = {
    "binary_status",
    "fetch_binary",
}

_CORE_TOOL_NAMES = _SETUP_TOOL_NAMES | {
    "start_session",
    "close_session",
    "list_sessions",
    "session_info",
    "new_page",
    "close_page",
    "switch_page",
    "wait_for_page",
    "goto",
    "reload",
    "click",
    "fill",
    "type_text",
    "press_key",
    "scroll",
    "hover",
    "select_option",
    "get_text",
    "get_html",
    "get_attribute",
    "query_elements",
    "is_visible",
    "screenshot",
    "wait_for_selector",
    "wait_for_timeout",
    "evaluate",
}


def _normalize_tool_profile(value: str) -> str:
    profile = (value or "core").strip().lower()
    if profile not in {"setup", "core", "full"}:
        _log.warning("unknown tool profile %r; using core", value)
        return "core"
    return profile


_TOOL_PROFILE = _normalize_tool_profile(
    os.environ.get("SPECTRA_MCP_TOOL_PROFILE", "core")
)


def _tool_enabled(name: str, profile: Optional[str] = None) -> bool:
    selected = _normalize_tool_profile(profile) if profile is not None else _TOOL_PROFILE
    if selected == "full":
        return True
    if selected == "setup":
        return name in _SETUP_TOOL_NAMES
    return name in _CORE_TOOL_NAMES


_register_mcp_tool = mcp.tool


def _profiled_tool(*args: Any, **kwargs: Any):
    def _decorate(func: Any) -> Any:
        if not _tool_enabled(func.__name__):
            return func
        return _register_mcp_tool(*args, **kwargs)(func)

    return _decorate


mcp.tool = _profiled_tool  # type: ignore[method-assign]


# --------------------------------------------------------------------------- #
# Setup / binary tools
# --------------------------------------------------------------------------- #


@mcp.tool()
async def binary_status(ctx: Context) -> Dict[str, Any]:
    """Report whether the patched Firefox binary is already cached locally.

    Returns the cache path, version, and a ``ready`` flag. Does NOT download.
    """
    vdir = cache_dir_for_version(BINARY_VERSION)
    entry_rel = BINARY_ENTRY_REL.get(sys.platform)
    entry = vdir / entry_rel if entry_rel else None
    ready = bool(entry and entry.exists())
    await ctx.debug(f"binary_status ready={ready} path={entry}")
    return _ok(
        ready=ready,
        version=BINARY_VERSION,
        tool_profile=_TOOL_PROFILE,
        cache_dir=str(vdir),
        entry=str(entry) if entry else None,
        cache_root=str(cache_root()),
        broken_versions=list(BROKEN_VERSIONS),
    )


@mcp.tool()
async def fetch_binary(ctx: Context, force: bool = False) -> Dict[str, Any]:
    """Download (and verify) the patched Firefox binary if not already cached.

    One-time ~100 MB download, SHA256-verified. Set ``force=True`` to re-download
    even if a cached copy exists. Call this before ``start_session``. Reports
    download progress (0-100%) when the server reports a size and the client
    supports progress tokens; phase changes (downloading/verifying/extracting)
    are always logged.
    """
    import shutil

    loop = asyncio.get_running_loop()

    # progress/status run on the asyncio.to_thread worker thread and cannot
    # await ctx directly. Bridge each notification back to the event loop with
    # run_coroutine_threadsafe (fire-and-forget: we never .result(), so the
    # download thread never blocks on the MCP client). A done-callback retrieves
    # any exception so a client disconnect mid-download doesn't surface as an
    # "exception never retrieved" warning.
    def _emit(coro):
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        fut.add_done_callback(
            lambda f: _log.debug("progress emit failed: %s", f.exception())
            if (not f.cancelled() and f.exception() is not None)
            else None
        )

    last_pct = -1

    def _progress(done: int, total: int) -> None:
        nonlocal last_pct
        if total <= 0:
            return
        pct = round(done * 100 / total)
        if pct == last_pct:
            return
        last_pct = pct
        _emit(ctx.report_progress(float(pct), 100.0, f"downloading {pct}%"))

    def _status(phase: str) -> None:
        _log.info("fetch_binary phase=%s", phase)
        _emit(ctx.report_progress(0.0, None, phase))

    async with _binary_cache_guard():
        try:
            if force:
                async with _LOCK:
                    has_live_sessions = _STARTING_SESSIONS > 0 or any(
                        not session.cleanup_complete
                        for session in _SESSIONS.values()
                    )
                if has_live_sessions:
                    return _err(
                        "force fetch is unavailable while sessions are active or cleaning up"
                    )
            vdir = cache_dir_for_version(BINARY_VERSION)
            if force and vdir.exists():
                await ctx.info("force: removing existing cache dir")
                await asyncio.to_thread(shutil.rmtree, str(vdir))
            await ctx.info("ensuring patched Firefox binary (may download ~100 MB)")
            path = await asyncio.to_thread(
                ensure_binary, BINARY_VERSION, _progress, _status
            )
            await ctx.info(f"binary ready at {path}")
        except Exception as exc:
            return _err(f"fetch_binary failed: {exc}")
        return _ok(
            path=str(path), version=BINARY_VERSION, cache_root=str(cache_root())
        )


# --------------------------------------------------------------------------- #
# Session lifecycle
# --------------------------------------------------------------------------- #


@mcp.tool()
async def start_session(
    ctx: Context,
    seed: Optional[int] = None,
    headless: bool = True,
    proxy_server: str = "",
    proxy_username: str = "",
    proxy_password: str = "",
    timezone: str = "",
    locale: str = "auto",
    humanize: bool = True,
    profile_dir: str = "",
    prep_recaptcha: bool = False,
    storage_state_path: str = "",
) -> Dict[str, Any]:
    """Start an anti-detect browser session with a fresh (or seeded) fingerprint.

    Returns ``session_id``, ``seed`` (log it to replay), and the initial
    ``page_id``. Every Playwright method on the resulting browser works as-is.

    Parameters
    ----------
    seed : int, optional
        Reproducible fingerprint. Random per session when omitted. NOTE: the
        render-noise seed pool is calibrated on one GPU family and is NOT
        host-independent, so a random seed has a meaningful chance (~40% on a
        QEMU virtual GPU vs. Fingerprint Pro) of producing a "dirty" canvas/WebGL
        hash that strict detectors flag as tampering. For best results pass an
        explicit seed you have pre-validated on the deployment host (screen a
        small range, e.g. 1-50, against your target detector and keep the ones
        with low tampering_ml / anti_detect=false). Which seeds are clean
        depends on the host GPU — do not assume a seed good on one machine is
        good on another. On the tested host (Linux + QEMU virtual GPU) seeds
        12, 15, 21, 25 scored lowest; the returned ``seed`` is logged for
        replay, so record a good one and reuse it.
    headless : bool
        On Windows/macOS the patched binary self-cloaks its window.
    proxy_server : str
        e.g. ``socks5://host:1080`` (socks5/socks4/http/https). DNS routes
        through the proxy by default.
    proxy_username, proxy_password : str
        Proxy credentials, used only when ``proxy_server`` is set.
    timezone : str
        IANA zone (e.g. ``America/New_York``). Empty = auto-derive from egress IP.
    locale : str
        ``auto`` derives language from egress country, or an explicit locale.
    humanize : bool
        Bezier-curve mouse motion + human timing on clicks/hover/drag.
    profile_dir : str
        Optional persistent profile path (enables a persistent context).
    prep_recaptcha : bool
        Pre-seed reCAPTCHA cookies (only when not using a persistent profile).
    storage_state_path : str
        Path to a JSON file saved by ``save_storage_state`` — seeds the context's
        cookies + localStorage at creation so a logged-in state resumes without
        re-authenticating. Non-persistent sessions only (cannot be combined with
        ``profile_dir``, whose on-disk profile already persists state).
    """
    global _STARTING_SESSIONS

    if storage_state_path and profile_dir:
        return _err(
            "storage_state_path cannot be combined with profile_dir: a "
            "persistent profile already persists cookies/localStorage"
        )
    try:
        if storage_state_path:
            storage_path = _resolve_data_path(storage_state_path, must_exist=True)
            if not storage_path.is_file():
                return _err(f"storage_state_path is not a file: {str(storage_path)!r}")
            storage_state_path = str(storage_path)
        if profile_dir:
            profile_dir = str(_resolve_data_path(profile_dir))
    except ValueError as exc:
        return _err(str(exc))

    async with _LOCK:
        if len(_SESSIONS) + _STARTING_SESSIONS >= _MAX_SESSIONS:
            return _err(f"session limit reached ({_MAX_SESSIONS})")
        _STARTING_SESSIONS += 1
    reserved_slot = True

    proxy: Optional[Dict[str, str]] = None
    if proxy_server:
        proxy = {"server": proxy_server}
        if proxy_username:
            proxy["username"] = proxy_username
        if proxy_password:
            proxy["password"] = proxy_password

    ipw = None
    session = None
    sid = secrets.token_hex(8)
    try:
        try:
            ipw = InvisiblePlaywright(
                seed=seed,
                headless=headless,
                proxy=proxy,
                humanize=humanize,
                locale=locale or "auto",
                timezone=timezone or "",
                profile_dir=profile_dir or None,
                prep_recaptcha=prep_recaptcha,
            )
        except Exception as exc:
            return _err(f"launch setup failed: {exc}")

        await ctx.info(f"launching session {sid} (seed={ipw.seed})")
        try:
            async with _binary_cache_guard():
                browser_or_ctx = await ipw.__aenter__()
        except asyncio.CancelledError:
            try:
                await asyncio.shield(ipw.__aexit__(None, None, None))
            except Exception:
                pass
            raise
        except Exception as exc:
            try:
                await ipw.__aexit__(None, None, None)
            except Exception:
                pass
            return _err(f"launch failed: {exc}")

        persistent = not hasattr(browser_or_ctx, "new_context")
        session = _Session(
            session_id=sid,
            ipw=ipw,
            browser_or_ctx=browser_or_ctx,
            seed=ipw.seed,
            persistent=persistent,
            ready=False,
        )
        session.storage_state_path = storage_state_path or None
        async with _LOCK:
            _SESSIONS[sid] = session
            _STARTING_SESSIONS -= 1
            reserved_slot = False

        try:
            page, pid = await _initialize_session_page(session)
            _register_context_handlers(session)
            url = page.url
            session.ready = True
            await ctx.info(f"session {sid} ready, page_id={pid}")
        except asyncio.CancelledError:
            await _cleanup_failed_start(session)
            raise
        except Exception as exc:
            cleanup_errors = await _cleanup_failed_start(session)
            return _err(
                f"initial session setup failed: {exc}",
                session_id=sid,
                cleanup_errors=cleanup_errors,
                cleanup_pending=not session.cleanup_complete,
            )

        return _ok(
            session_id=sid,
            seed=ipw.seed,
            persistent=persistent,
            page_id=pid,
            url=url,
        )
    finally:
        if reserved_slot:
            async with _LOCK:
                _STARTING_SESSIONS -= 1


@mcp.tool()
async def close_session(session_id: str, ctx: Context) -> Dict[str, Any]:
    """Close a browser session and free its Firefox process.

    Failed cleanup remains registered internally so calling this tool again can
    retry resource recovery.
    """
    async with _LOCK:
        session = _SESSIONS.get(session_id)
    if session is None:
        return _err(f"unknown session_id: {session_id!r}")
    cleanup_errors = await _await_session_cleanup(session)
    await _remove_completed_session(session)
    await ctx.info(f"closed session {session_id}")
    if cleanup_errors:
        return _err(
            "session cleanup reported errors",
            session_id=session_id,
            cleanup_errors=cleanup_errors,
            cleanup_complete=session.cleanup_complete,
        )
    if not session.cleanup_complete:
        return _err(
            "session cleanup incomplete; retry close_session",
            session_id=session_id,
        )
    return _ok(session_id=session_id)


@mcp.tool()
async def list_sessions(ctx: Context) -> Dict[str, Any]:
    """List active session ids and their active page id."""
    async with _LOCK:
        items = [
            {
                "session_id": s.session_id,
                "seed": s.seed,
                "persistent": s.persistent,
                "pages": list(s.pages.keys()),
                "active_page_id": s.active_page_id,
            }
            for s in _SESSIONS.values()
            if s.ready and not s.closed
        ]
    return _ok(sessions=items)


@mcp.tool()
async def session_info(session_id: str, ctx: Context) -> Dict[str, Any]:
    """Return session metadata, pages, and the active page's URL/title."""
    try:
        s = await _get_session(session_id)
    except KeyError as exc:
        return _err(str(exc))
    async with s.lock:
        if s.closed:
            return _err("session closed")
        pages_info = []
        for pid, page in list(s.pages.items()):
            try:
                pages_info.append({"page_id": pid, "url": page.url, "closed": page.is_closed()})
            except Exception:
                pages_info.append({"page_id": pid, "url": None, "closed": True})
        active_url = None
        active_title = None
        active_page = s.pages.get(s.active_page_id) if s.active_page_id is not None else None
        if active_page is not None:
            try:
                if not active_page.is_closed():
                    active_url = active_page.url
                    active_title = await active_page.title()
            except Exception:
                pass
        return _ok(
            session_id=s.session_id,
            seed=s.seed,
            persistent=s.persistent,
            active_page_id=s.active_page_id,
            active_url=active_url,
            active_title=active_title,
            pages=pages_info,
        )


# --------------------------------------------------------------------------- #
# Page management
# --------------------------------------------------------------------------- #


@mcp.tool()
async def new_page(session_id: str, ctx: Context) -> Dict[str, Any]:
    """Open a new page (tab) in the session and make it the active page."""
    try:
        s = await _get_session(session_id)
    except KeyError as exc:
        return _err(str(exc))
    async with s.lock:
        if s.closed:
            return _err("session closed")
        try:
            page, pid = await _make_page(s)
        except Exception as exc:
            return _pw_err(s, "new_page", exc)
        return _ok(page_id=pid, url=page.url)


@mcp.tool()
async def close_page(session_id: str, ctx: Context, page_id: Optional[int] = None) -> Dict[str, Any]:
    """Close a page. Defaults to the active page. Switches active page if needed."""
    try:
        s = await _get_session(session_id)
    except KeyError as exc:
        return _err(str(exc))
    async with s.lock:
        if s.closed:
            return _err("session closed")
        target = page_id if page_id is not None else s.active_page_id
        if target is None:
            return _err("no page to close")
        page = s.pages.get(target)
        if page is None:
            return _err(f"unknown page_id: {target}")
        try:
            if not page.is_closed():
                await page.close()
        except Exception as exc:
            try:
                closed = page.is_closed()
            except Exception:
                closed = False
            if not closed:
                return _pw_err(s, "close_page", exc)
        s.pages.pop(target, None)
        if s.active_page_id == target:
            s.active_page_id = None
            for pid, candidate in list(s.pages.items()):
                try:
                    if not candidate.is_closed():
                        s.active_page_id = pid
                        break
                except Exception:
                    continue
        return _ok(closed_page_id=target, active_page_id=s.active_page_id)


@mcp.tool()
async def switch_page(session_id: str, page_id: int, ctx: Context) -> Dict[str, Any]:
    """Set the active page (subsequent tools act on it)."""
    try:
        s = await _get_session(session_id)
    except KeyError as exc:
        return _err(str(exc))
    async with s.lock:
        if s.closed:
            return _err("session closed")
        if page_id not in s.pages:
            return _err(f"unknown page_id: {page_id}")
        page = s.pages[page_id]
        try:
            if page.is_closed():
                return _err(f"page {page_id} is closed")
            url = page.url
        except Exception as exc:
            return _pw_err(s, "switch_page", exc)
        s.active_page_id = page_id
        return _ok(active_page_id=page_id, url=url)


# --------------------------------------------------------------------------- #
# Navigation
# --------------------------------------------------------------------------- #


@asynccontextmanager
async def _use_page(session_id: str):
    """Resolve the session + active page under the session lock.

    Acquires ``s.lock``, checks ``s.closed``, and yields ``(s, page)``. The lock
    is held until the ``async with`` block exits — this is the lifecycle guard
    that lets ``_close_session`` drain in-flight tools before force-closing.
    Raises ``KeyError`` (unknown session) or ``RuntimeError`` (session closed /
    no active page); callers catch both and return ``_err(str(exc))``.
    """
    s = await _get_session(session_id)
    async with s.lock:
        if s.closed:
            raise RuntimeError("session closed")
        yield s, _active_page(s)


def _pw_err(s: _Session, what: str, exc: Exception) -> Dict[str, Any]:
    """Error from a Playwright call. Returns a clean ``session closed`` if the
    session was closed (e.g. force-closed by ``_close_session`` mid-call);
    otherwise the raw Playwright error message."""
    if s.closed:
        return _err("session closed")
    return _err(f"{what} failed: {exc}")


@mcp.tool()
async def goto(
    session_id: str,
    url: str,
    ctx: Context,
    timeout_ms: int = 30000,
    wait_until: Literal["commit", "domcontentloaded", "load", "networkidle"] = "load",
) -> Dict[str, Any]:
    """Navigate the active page to ``url``.

    ``wait_until``: ``load`` | ``domcontentloaded`` | ``networkidle`` | ``commit``.
    """
    if error := _timeout_error(timeout_ms):
        return _err(error)
    try:
        async with _use_page(session_id) as (s, page):
            try:
                resp = await page.goto(url, timeout=timeout_ms, wait_until=wait_until)
            except Exception as exc:
                return _pw_err(s, "goto", exc)
            status = resp.status if resp is not None else None
            try:
                title = await page.title()
            except Exception as exc:
                if s.closed:
                    return _pw_err(s, "goto", exc)
                return _ok(
                    url=page.url,
                    status=status,
                    title=None,
                    title_error=str(exc),
                )
            return _ok(url=page.url, status=status, title=title)
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))


@mcp.tool()
async def go_forward(session_id: str, ctx: Context, timeout_ms: int = 30000) -> Dict[str, Any]:
    """Navigate the active page forward by one browser-history entry."""
    if error := _timeout_error(timeout_ms):
        return _err(error)
    try:
        async with _use_page(session_id) as (s, page):
            try:
                ok = await page.go_forward(timeout=timeout_ms)
            except Exception as exc:
                return _pw_err(s, "go_forward", exc)
            return _ok(url=page.url, navigated=bool(ok))
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))


@mcp.tool()
async def reload(
    session_id: str,
    ctx: Context,
    timeout_ms: int = 30000,
    wait_until: Literal["commit", "domcontentloaded", "load", "networkidle"] = "load",
) -> Dict[str, Any]:
    """Reload the active page and wait for the requested lifecycle state."""
    if error := _timeout_error(timeout_ms):
        return _err(error)
    try:
        async with _use_page(session_id) as (s, page):
            try:
                resp = await page.reload(timeout=timeout_ms, wait_until=wait_until)
            except Exception as exc:
                return _pw_err(s, "reload", exc)
            status = resp.status if resp is not None else None
            return _ok(url=page.url, status=status)
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))


# --------------------------------------------------------------------------- #
# Interaction
# --------------------------------------------------------------------------- #


@mcp.tool()
async def click(
    session_id: str,
    selector: str,
    ctx: Context,
    timeout_ms: int = 30000,
    button: Literal["left", "middle", "right"] = "left",
    click_count: int = 1,
) -> Dict[str, Any]:
    """Click an element matched by ``selector`` (humanized Bezier mouse path)."""
    if error := _timeout_error(timeout_ms):
        return _err(error)
    if error := _range_error("click_count", click_count, 1, _MAX_CLICK_COUNT):
        return _err(error)
    try:
        async with _use_page(session_id) as (s, page):
            try:
                await page.click(selector, timeout=timeout_ms, button=button, click_count=click_count)
            except Exception as exc:
                return _pw_err(s, "click", exc)
            return _ok(selector=selector)
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))


@mcp.tool()
async def fill(
    session_id: str,
    selector: str,
    value: str,
    ctx: Context,
    timeout_ms: int = 30000,
) -> Dict[str, Any]:
    """Clear and fill an input/textarea with ``value`` (atomic, not per-keystroke)."""
    if error := _timeout_error(timeout_ms):
        return _err(error)
    try:
        async with _use_page(session_id) as (s, page):
            try:
                await page.fill(selector, value, timeout=timeout_ms)
            except Exception as exc:
                return _pw_err(s, "fill", exc)
            return _ok(selector=selector, value=value)
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))


@mcp.tool()
async def type_text(
    session_id: str,
    selector: str,
    text: str,
    ctx: Context,
    delay_ms: int = 0,
    timeout_ms: int = 30000,
) -> Dict[str, Any]:
    """Type ``text`` into an element one keystroke at a time (use for key-by-key input)."""
    if error := _range_error("delay_ms", delay_ms, 0, _MAX_DELAY_MS):
        return _err(error)
    if error := _timeout_error(timeout_ms):
        return _err(error)
    try:
        async with _use_page(session_id) as (s, page):
            try:
                await page.type(selector, text, delay=delay_ms, timeout=timeout_ms)
            except Exception as exc:
                return _pw_err(s, "type", exc)
            return _ok(selector=selector, length=len(text))
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))


@mcp.tool()
async def press_key(
    session_id: str,
    selector: str,
    key: str,
    ctx: Context,
    timeout_ms: int = 30000,
) -> Dict[str, Any]:
    """Focus ``selector`` and press a keyboard ``key`` (e.g. ``Enter``, ``Tab``, ``ArrowDown``)."""
    if error := _timeout_error(timeout_ms):
        return _err(error)
    try:
        async with _use_page(session_id) as (s, page):
            try:
                await page.press(selector, key, timeout=timeout_ms)
            except Exception as exc:
                return _pw_err(s, "press_key", exc)
            return _ok(selector=selector, key=key)
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))


@mcp.tool()
async def keyboard_press(session_id: str, key: str, ctx: Context) -> Dict[str, Any]:
    """Press a keyboard ``key`` on the focused element (no selector)."""
    try:
        async with _use_page(session_id) as (s, page):
            try:
                await page.keyboard.press(key)
            except Exception as exc:
                return _pw_err(s, "keyboard_press", exc)
            return _ok(key=key)
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))


@mcp.tool()
async def select_option(
    session_id: str,
    selector: str,
    value: str,
    ctx: Context,
    timeout_ms: int = 30000,
) -> Dict[str, Any]:
    """Select an ``<option>`` by value in a ``<select>``."""
    if error := _timeout_error(timeout_ms):
        return _err(error)
    try:
        async with _use_page(session_id) as (s, page):
            try:
                selected = await page.select_option(selector, value, timeout=timeout_ms)
            except Exception as exc:
                return _pw_err(s, "select_option", exc)
            return _ok(selector=selector, selected=list(selected))
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))


@mcp.tool()
async def hover(
    session_id: str,
    selector: str,
    ctx: Context,
    timeout_ms: int = 30000,
) -> Dict[str, Any]:
    """Hover an element (humanized mouse path)."""
    if error := _timeout_error(timeout_ms):
        return _err(error)
    try:
        async with _use_page(session_id) as (s, page):
            try:
                await page.hover(selector, timeout=timeout_ms)
            except Exception as exc:
                return _pw_err(s, "hover", exc)
            return _ok(selector=selector)
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))


@mcp.tool()
async def focus(session_id: str, selector: str, ctx: Context, timeout_ms: int = 30000) -> Dict[str, Any]:
    """Focus the first element matching ``selector``."""
    if error := _timeout_error(timeout_ms):
        return _err(error)
    try:
        async with _use_page(session_id) as (s, page):
            try:
                await page.focus(selector, timeout=timeout_ms)
            except Exception as exc:
                return _pw_err(s, "focus", exc)
            return _ok(selector=selector)
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))


@mcp.tool()
async def check(session_id: str, selector: str, ctx: Context, timeout_ms: int = 30000) -> Dict[str, Any]:
    """Check a checkbox/radio matched by ``selector``."""
    if error := _timeout_error(timeout_ms):
        return _err(error)
    try:
        async with _use_page(session_id) as (s, page):
            try:
                await page.check(selector, timeout=timeout_ms)
            except Exception as exc:
                return _pw_err(s, "check", exc)
            return _ok(selector=selector)
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))


@mcp.tool()
async def uncheck(session_id: str, selector: str, ctx: Context, timeout_ms: int = 30000) -> Dict[str, Any]:
    """Uncheck a checkbox matched by ``selector``."""
    if error := _timeout_error(timeout_ms):
        return _err(error)
    try:
        async with _use_page(session_id) as (s, page):
            try:
                await page.uncheck(selector, timeout=timeout_ms)
            except Exception as exc:
                return _pw_err(s, "uncheck", exc)
            return _ok(selector=selector)
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))


@mcp.tool()
async def scroll(
    session_id: str,
    ctx: Context,
    dx: int = 0,
    dy: int = 0,
    selector: str = "",
) -> Dict[str, Any]:
    """Scroll the page by (dx, dy) pixels. If ``selector`` is given, scroll that element into view first."""
    try:
        async with _use_page(session_id) as (s, page):
            try:
                if selector:
                    el = await page.query_selector(selector)
                    if el is None:
                        return _err(f"no element matches {selector!r}")
                    await el.scroll_into_view_if_needed()
                await page.mouse.wheel(dx, dy)
            except Exception as exc:
                return _pw_err(s, "scroll", exc)
            return _ok(dx=dx, dy=dy)
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #


@mcp.tool()
async def get_text(
    session_id: str,
    ctx: Context,
    selector: str = "",
    max_chars: int = 20000,
) -> Dict[str, Any]:
    """Get visible text. Empty ``selector`` = the whole body. Output is capped to ``max_chars``."""
    if error := _range_error("max_chars", max_chars, 0, _MAX_TEXT_CHARS):
        return _err(error)
    try:
        async with _use_page(session_id) as (s, page):
            try:
                if selector:
                    el = await page.query_selector(selector)
                    if el is None:
                        return _err(f"no element matches {selector!r}")
                    text = await el.inner_text()
                else:
                    text = await page.inner_text("body")
            except Exception as exc:
                return _pw_err(s, "get_text", exc)
            truncated = len(text) > max_chars
            return _ok(text=text[:max_chars], truncated=truncated, full_length=len(text))
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))


@mcp.tool()
async def get_html(
    session_id: str,
    ctx: Context,
    selector: str = "",
    max_chars: int = 20000,
) -> Dict[str, Any]:
    """Get HTML. Empty ``selector`` = full document (``page.content()``). Capped to ``max_chars``."""
    if error := _range_error("max_chars", max_chars, 0, _MAX_TEXT_CHARS):
        return _err(error)
    try:
        async with _use_page(session_id) as (s, page):
            try:
                if selector:
                    html = await page.eval_on_selector(
                        selector, "el => el.outerHTML"
                    )
                else:
                    html = await page.content()
            except Exception as exc:
                return _pw_err(s, "get_html", exc)
            truncated = len(html) > max_chars
            return _ok(html=html[:max_chars], truncated=truncated, full_length=len(html))
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))


@mcp.tool()
async def get_attribute(
    session_id: str,
    selector: str,
    attribute: str,
    ctx: Context,
) -> Dict[str, Any]:
    """Return a single attribute of the first element matching ``selector``."""
    try:
        async with _use_page(session_id) as (s, page):
            try:
                val = await page.get_attribute(selector, attribute)
            except Exception as exc:
                return _pw_err(s, "get_attribute", exc)
            return _ok(selector=selector, attribute=attribute, value=val)
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))


@mcp.tool()
async def query_elements(
    session_id: str,
    selector: str,
    ctx: Context,
    max_results: int = 50,
) -> Dict[str, Any]:
    """Return a structured snapshot of elements matching ``selector``.

    Each entry: tag, id, name, type, role, href, text (<=200 chars), value,
    placeholder, visible flag and bounding rect. Useful for the LLM to decide
    what to click next without scraping raw HTML.
    """
    if error := _range_error(
        "max_results", max_results, 0, _MAX_ELEMENT_RESULTS
    ):
        return _err(error)
    try:
        async with _use_page(session_id) as (s, page):
            try:
                data = await page.evaluate(_QUERY_ELEMENTS_JS, {"sel": selector, "limit": max_results})
            except Exception as exc:
                return _pw_err(s, "query_elements", exc)
            return _ok(selector=selector, count=len(data), elements=data)
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))


@mcp.tool()
async def is_visible(
    session_id: str,
    selector: str,
    ctx: Context,
    timeout_ms: int = 5000,
) -> Dict[str, Any]:
    """Check whether ``selector`` matches a visible element (with a short wait)."""
    if error := _timeout_error(timeout_ms):
        return _err(error)
    try:
        async with _use_page(session_id) as (s, page):
            try:
                loc = page.locator(selector)
                await loc.first.wait_for(state="visible", timeout=timeout_ms)
            except PlaywrightTimeoutError:
                return _ok(selector=selector, visible=False)
            except Exception as exc:
                return _pw_err(s, "is_visible", exc)
            return _ok(selector=selector, visible=True)
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))


# --------------------------------------------------------------------------- #
# Screenshot
# --------------------------------------------------------------------------- #


@mcp.tool()
async def screenshot(
    session_id: str,
    ctx: Context,
    full_page: bool = False,
    image_format: Literal["jpeg", "jpg", "png"] = "jpeg",
    quality: int = 85,
) -> Image:
    """Capture a screenshot of the active page and return it as image content.

    Defaults to JPEG (``quality=85``) to keep payloads small for the LLM; set
    ``image_format="png"`` for lossless (much larger) output. ``full_page=True``
    captures the whole scrollable page.
    """
    fmt = (image_format or "jpeg").lower()
    if fmt == "jpg":
        fmt = "jpeg"
    if fmt not in ("jpeg", "png"):
        raise RuntimeError("image_format must be 'jpeg', 'jpg', or 'png'")
    if fmt == "jpeg" and not 0 <= quality <= 100:
        raise RuntimeError("quality must be between 0 and 100 for JPEG")
    try:
        async with _use_page(session_id) as (s, page):
            try:
                if full_page:
                    dimensions = await page.evaluate(
                        """() => {
                          const d = document.documentElement;
                          const b = document.body;
                          return {
                            width: Math.max(d.scrollWidth, b ? b.scrollWidth : 0),
                            height: Math.max(d.scrollHeight, b ? b.scrollHeight : 0)
                          };
                        }"""
                    )
                    pixels = int(dimensions["width"]) * int(dimensions["height"])
                    if pixels > _MAX_SCREENSHOT_PIXELS:
                        raise RuntimeError(
                            "full-page screenshot exceeds pixel limit "
                            f"({_MAX_SCREENSHOT_PIXELS})"
                        )
                if fmt == "png":
                    data = await page.screenshot(full_page=full_page, type="png")
                    mime = "png"
                else:
                    data = await page.screenshot(full_page=full_page, type="jpeg", quality=quality)
                    mime = "jpeg"
                if len(data) > _MAX_SCREENSHOT_BYTES:
                    raise RuntimeError(
                        f"screenshot exceeds byte limit ({_MAX_SCREENSHOT_BYTES})"
                    )
            except Exception as exc:
                raise RuntimeError("session closed" if s.closed else f"screenshot failed: {exc}") from exc
            await ctx.debug(f"screenshot fmt={mime} full_page={full_page} bytes={len(data)}")
            return Image(data=data, format=mime)
    except (KeyError, RuntimeError) as exc:
        raise RuntimeError(str(exc)) from exc


# --------------------------------------------------------------------------- #
# Waiting
# --------------------------------------------------------------------------- #


@mcp.tool()
async def wait_for_selector(
    session_id: str,
    selector: str,
    ctx: Context,
    timeout_ms: int = 30000,
    state: Literal["attached", "detached", "hidden", "visible"] = "visible",
) -> Dict[str, Any]:
    """Wait until an element matching ``selector`` reaches ``state``.

    ``state``: ``visible`` | ``hidden`` | ``attached`` | ``detached``.
    """
    if error := _timeout_error(timeout_ms):
        return _err(error)
    try:
        async with _use_page(session_id) as (s, page):
            try:
                el = await page.wait_for_selector(selector, timeout=timeout_ms, state=state)
            except Exception as exc:
                return _pw_err(s, "wait_for_selector", exc)
            return _ok(
                selector=selector,
                state=state,
                reached=True,
                element_present=el is not None,
            )
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))


@mcp.tool()
async def wait_for_timeout(session_id: str, ms: int, ctx: Context) -> Dict[str, Any]:
    """Sleep for ``ms`` milliseconds (used to let async page updates settle)."""
    if error := _range_error("ms", ms, 0, _MAX_TIMEOUT_MS):
        return _err(error)
    try:
        async with _use_page(session_id) as (s, page):
            try:
                await page.wait_for_timeout(ms)
            except Exception as exc:
                return _pw_err(s, "wait_for_timeout", exc)
            return _ok(ms=ms)
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))


# --------------------------------------------------------------------------- #
# Advanced
# --------------------------------------------------------------------------- #


@mcp.tool()
async def evaluate(
    session_id: str,
    script: str,
    ctx: Context,
    arg: Any = None,
    timeout_ms: int = 30000,
) -> Dict[str, Any]:
    """Execute arbitrary JavaScript in the active page and return its value.

    ``script`` is evaluated as an async function body receiving ``arg``:
    e.g. ``async (arg) => { return document.title; }`` or a plain expression.

    ``timeout_ms`` bounds how long the server waits for the script to return;
    ``0`` disables the server timeout. Positive values are enforced via
    ``asyncio.wait_for``. Note: on timeout the Python-side await is cancelled
    but the JS keeps running in the browser — the page may be left
    unresponsive (its main thread occupied). Call ``close_page`` then
    ``new_page`` to recover. Use sparingly — arbitrary JS is powerful.
    """
    if error := _timeout_error(timeout_ms):
        return _err(error)
    try:
        async with _use_page(session_id) as (s, page):
            try:
                result = await _await_with_timeout(
                    page.evaluate(script, arg), timeout_ms
                )
            except asyncio.TimeoutError:
                return _err(
                    f"evaluate timed out after {timeout_ms}ms; "
                    f"the page may be unresponsive — close_page to recover"
                )
            except Exception as exc:
                return _pw_err(s, "evaluate", exc)
            try:
                result_size = _serialized_size(result)
            except (TypeError, ValueError) as exc:
                return _err(f"evaluate returned a non-serializable result: {exc}")
            if result_size > _MAX_EVALUATE_RESULT_BYTES:
                return _err(
                    "evaluate result exceeds byte limit "
                    f"({_MAX_EVALUATE_RESULT_BYTES})"
                )
            return _ok(result=result)
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))


# --------------------------------------------------------------------------- #
# Drag & coordinate mouse
# --------------------------------------------------------------------------- #


@mcp.tool()
async def mouse_drag(
    session_id: str,
    from_x: float,
    from_y: float,
    to_x: float,
    to_y: float,
    ctx: Context,
    steps: int = 10,
) -> Dict[str, Any]:
    """Press at (from_x, from_y), drag to (to_x, to_y) over ``steps`` moves, release.

    Coordinate-based drag for sliders, canvas handles and slider-captchas (no
    selector). Motion is humanized by the patched browser when humanize is on.

    Note: on the patched anti-detect build, hold-drag only works when the session
    was started with ``humanize=False`` (the humanized mouse can't hold-and-drag).
    """
    if error := _range_error("steps", steps, 1, _MAX_MOUSE_STEPS):
        return _err(error)
    try:
        async with _use_page(session_id) as (s, page):
            pressed = False
            try:
                await page.mouse.move(from_x, from_y)
                await page.mouse.down()
                pressed = True
                await page.mouse.move(to_x, to_y, steps=steps)
                await page.mouse.up()
                pressed = False
            except Exception as exc:
                if pressed:
                    try:
                        await page.mouse.up()
                    except Exception as release_exc:
                        _log.warning(
                            "mouse_drag release failed on session %s: %s",
                            s.session_id,
                            release_exc,
                        )
                return _pw_err(s, "mouse_drag", exc)
            return _ok(
                from_xy=[from_x, from_y],
                to_xy=[to_x, to_y],
                steps=steps,
            )
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))


@mcp.tool()
async def mouse_move(
    session_id: str,
    x: float,
    y: float,
    ctx: Context,
    steps: int = 1,
) -> Dict[str, Any]:
    """Move the mouse to viewport coordinate (x, y) over ``steps`` intermediate moves."""
    if error := _range_error("steps", steps, 1, _MAX_MOUSE_STEPS):
        return _err(error)
    try:
        async with _use_page(session_id) as (s, page):
            try:
                await page.mouse.move(x, y, steps=steps)
            except Exception as exc:
                return _pw_err(s, "mouse_move", exc)
            return _ok(x=x, y=y)
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))


@mcp.tool()
async def mouse_click(
    session_id: str,
    x: float,
    y: float,
    ctx: Context,
    button: Literal["left", "middle", "right"] = "left",
    click_count: int = 1,
) -> Dict[str, Any]:
    """Click at viewport coordinate (x, y) — for canvas/SVG/custom controls (no selector).

    Coordinates are viewport pixels; read them from ``screenshot`` or the ``rect``
    fields of ``query_elements``. Combine with ``keyboard_down``/``keyboard_up`` for
    Shift/Ctrl+click."""
    if error := _range_error("click_count", click_count, 1, _MAX_CLICK_COUNT):
        return _err(error)
    try:
        async with _use_page(session_id) as (s, page):
            try:
                await page.mouse.click(x, y, button=button, click_count=click_count)
            except Exception as exc:
                return _pw_err(s, "mouse_click", exc)
            return _ok(x=x, y=y, button=button, click_count=click_count)
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))


# --------------------------------------------------------------------------- #
# Keyboard (modifier hold / focused-element typing)
# --------------------------------------------------------------------------- #


@mcp.tool()
async def keyboard_down(session_id: str, key: str, ctx: Context) -> Dict[str, Any]:
    """Hold a key down (e.g. ``Shift``, ``Control``, ``Meta``).

    Pair with ``mouse_click`` / ``click`` for Shift+click, or another key for
    combos (Ctrl+A). Always release it later with ``keyboard_up``."""
    try:
        async with _use_page(session_id) as (s, page):
            try:
                await page.keyboard.down(key)
            except Exception as exc:
                return _pw_err(s, "keyboard_down", exc)
            return _ok(key=key)
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))


@mcp.tool()
async def keyboard_up(session_id: str, key: str, ctx: Context) -> Dict[str, Any]:
    """Release a key previously held with ``keyboard_down``."""
    try:
        async with _use_page(session_id) as (s, page):
            try:
                await page.keyboard.up(key)
            except Exception as exc:
                return _pw_err(s, "keyboard_up", exc)
            return _ok(key=key)
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))


@mcp.tool()
async def keyboard_type(
    session_id: str,
    text: str,
    ctx: Context,
    delay_ms: int = 0,
) -> Dict[str, Any]:
    """Type ``text`` into the currently focused element, key by key (no selector).

    Complements ``type_text`` (which targets a selector). Use after focusing via
    ``mouse_click`` / ``focus`` — handy for canvas or custom widgets."""
    if error := _range_error("delay_ms", delay_ms, 0, _MAX_DELAY_MS):
        return _err(error)
    try:
        async with _use_page(session_id) as (s, page):
            try:
                await page.keyboard.type(text, delay=delay_ms)
            except Exception as exc:
                return _pw_err(s, "keyboard_type", exc)
            return _ok(length=len(text))
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))


# --------------------------------------------------------------------------- #
# Dialogs (alert / confirm / prompt / beforeunload)
# --------------------------------------------------------------------------- #


@mcp.tool()
async def set_dialog_handler(
    session_id: str,
    ctx: Context,
    action: Literal["accept", "dismiss"] = "accept",
    prompt_text: str = "",
) -> Dict[str, Any]:
    """Set how JS dialogs are auto-handled for this session.

    A dialog (``alert``/``confirm``/``prompt``/``beforeunload``) blocks the page
    until answered, so it can't be surfaced to you mid-action — this sets the
    policy applied automatically to every tab. ``action`` is ``accept`` (default:
    OK / Leave / submit ``prompt_text``) or ``dismiss`` (Cancel / Stay). Inspect
    what actually fired with ``get_dialogs``."""
    action = (action or "").lower()
    if action not in ("accept", "dismiss"):
        return _err("action must be 'accept' or 'dismiss'")
    try:
        s = await _get_session(session_id)
    except KeyError as exc:
        return _err(str(exc))
    async with s.lock:
        if s.closed:
            return _err("session closed")
        s.dialog_action = action
        s.dialog_prompt_text = prompt_text
        return _ok(action=action, prompt_text=prompt_text)


@mcp.tool()
async def get_dialogs(session_id: str, ctx: Context, clear: bool = False) -> Dict[str, Any]:
    """Return JS dialogs seen this session (type, message, default_value, action taken).

    Set ``clear=True`` to also empty the log."""
    try:
        s = await _get_session(session_id)
    except KeyError as exc:
        return _err(str(exc))
    async with s.lock:
        if s.closed:
            return _err("session closed")
        out = list(s.dialog_log)
        if clear:
            s.dialog_log.clear()
        return _ok(dialogs=out, count=len(out))


# --------------------------------------------------------------------------- #
# Popups / new tabs
# --------------------------------------------------------------------------- #


@mcp.tool()
async def wait_for_page(
    session_id: str,
    ctx: Context,
    known_page_ids: Optional[List[int]] = None,
    timeout_ms: int = 30000,
) -> Dict[str, Any]:
    """Wait for a new tab/window (popup) to open, e.g. after an OAuth ``click``.

    New tabs (``window.open`` / ``target=_blank`` / OAuth popups) are adopted
    automatically; this waits until one appears and returns the new ``page_id``(s).
    Then ``switch_page`` to it. Because a popup may be adopted *before* this call,
    pass the page ids you already had (``known_page_ids``, e.g. from ``session_info``
    before the click) so an already-open popup is returned immediately; otherwise
    the current pages form the baseline and only strictly-new tabs are reported."""
    if error := _timeout_error(timeout_ms):
        return _err(error)
    try:
        s = await _get_session(session_id)
    except KeyError as exc:
        return _err(str(exc))
    loop = asyncio.get_running_loop()
    deadline = None if timeout_ms == 0 else loop.time() + timeout_ms / 1000
    async with s.lock:
        if s.closed:
            return _err("session closed")
        baseline = set(known_page_ids) if known_page_ids is not None else set(s.pages.keys())
        while True:
            new = [pid for pid in list(s.pages.keys()) if pid not in baseline]
            if new:
                return _ok(new_page_ids=sorted(new), active_page_id=s.active_page_id)
            if s.closed:
                return _err("session closed")
            if deadline is not None and loop.time() >= deadline:
                return _err("wait_for_page timed out")
            await asyncio.sleep(0.15)


# --------------------------------------------------------------------------- #
# Cookies & storage state
# --------------------------------------------------------------------------- #


@mcp.tool()
async def get_cookies(
    session_id: str,
    ctx: Context,
    urls: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Return the context's cookies, optionally filtered to ``urls``."""
    try:
        s = await _get_session(session_id)
    except KeyError as exc:
        return _err(str(exc))
    async with s.lock:
        if s.closed:
            return _err("session closed")
        c = _context_of(s)
        if c is None:
            return _err("no browser context available")
        try:
            cookies = await c.cookies(urls)
        except Exception as exc:
            return _pw_err(s, "get_cookies", exc)
        return _ok(cookies=list(cookies), count=len(cookies))


@mcp.tool()
async def add_cookies(
    session_id: str,
    cookies: List[Dict[str, Any]],
    ctx: Context,
) -> Dict[str, Any]:
    """Add cookies to the context (affects all tabs).

    Each cookie needs ``name`` + ``value`` and either ``url`` or (``domain`` +
    ``path``). Useful to inject a login state for debugging."""
    try:
        s = await _get_session(session_id)
    except KeyError as exc:
        return _err(str(exc))
    async with s.lock:
        if s.closed:
            return _err("session closed")
        c = _context_of(s)
        if c is None:
            return _err("no browser context available")
        try:
            await c.add_cookies(list(cookies))
        except Exception as exc:
            return _pw_err(s, "add_cookies", exc)
        return _ok(added=len(cookies))


@mcp.tool()
async def clear_cookies(session_id: str, ctx: Context) -> Dict[str, Any]:
    """Clear all cookies in the context."""
    try:
        s = await _get_session(session_id)
    except KeyError as exc:
        return _err(str(exc))
    async with s.lock:
        if s.closed:
            return _err("session closed")
        c = _context_of(s)
        if c is None:
            return _err("no browser context available")
        try:
            await c.clear_cookies()
        except Exception as exc:
            return _pw_err(s, "clear_cookies", exc)
        return _ok(cleared=True)


@mcp.tool()
async def save_storage_state(session_id: str, path: str, ctx: Context) -> Dict[str, Any]:
    """Save the context's cookies + localStorage to a JSON file at ``path``.

    Reload it in a future session via ``start_session(storage_state_path=...)`` to
    resume a logged-in state without re-authenticating (non-persistent sessions)."""
    if not path:
        return _err("path is required")
    try:
        target = _resolve_data_path(path)
    except ValueError as exc:
        return _err(str(exc))
    try:
        s = await _get_session(session_id)
    except KeyError as exc:
        return _err(str(exc))
    async with s.lock:
        if s.closed:
            return _err("session closed")
        c = _context_of(s)
        if c is None:
            return _err("no browser context available")
        try:
            state = await c.storage_state()
            await asyncio.to_thread(_write_private_json_atomic, target, state)
        except Exception as exc:
            return _pw_err(s, "save_storage_state", exc)
        return _ok(path=str(target))


# --------------------------------------------------------------------------- #
# Frames (iframe access)
# --------------------------------------------------------------------------- #


@mcp.tool()
async def list_frames(session_id: str, ctx: Context) -> Dict[str, Any]:
    """List the page's top-level ``<iframe>`` elements with a ready-to-use selector.

    Each entry: index, id, name, src, and ``suggested_selector`` to pass as the
    ``frame_selector`` of the ``frame_*`` tools. For nested iframes, chain
    selectors with ``>>>`` (e.g. ``"iframe#outer >>> iframe.inner"``)."""
    js = r"""
    () => {
      // 转义双引号 CSS 属性选择器中的反斜杠和双引号。
      const escAttr = (s) => String(s).split('\\').join('\\\\').split('"').join('\\"');
      const isUnique = (selector) => {
        try {
          return document.querySelectorAll(selector).length === 1;
        } catch (_) {
          return false;
        }
      };

      // 从目标元素逐层构造结构路径；nth-of-type 只使用当前父元素内的同类兄弟序号。
      const buildPath = (element) => {
        const parts = [];
        let current = element;
        while (current) {
          if (current === document.documentElement) {
            parts.unshift(current.localName);
            break;
          }
          const parent = current.parentElement;
          if (!parent) break;
          const sameType = Array.from(parent.children)
            .filter((sibling) => sibling.localName === current.localName);
          parts.unshift(
            current.localName + ':nth-of-type(' + (sameType.indexOf(current) + 1) + ')'
          );
          current = parent;
        }
        return parts.join(' > ');
      };

      return Array.from(document.querySelectorAll('iframe')).map((e, i) => {
        const candidates = [];
        if (e.id) candidates.push('iframe#' + CSS.escape(e.id));
        if (e.getAttribute('name')) {
          candidates.push('iframe[name="' + escAttr(e.getAttribute('name')) + '"]');
        }
        if (e.getAttribute('src')) {
          candidates.push('iframe[src="' + escAttr(e.getAttribute('src')) + '"]');
        }
        const sel = candidates.find(isUnique) || buildPath(e);
        return { index: i, id: e.id || null, name: e.getAttribute('name'),
                 src: e.getAttribute('src'), suggested_selector: sel };
      });
    }
    """
    try:
        async with _use_page(session_id) as (s, page):
            try:
                data = await page.evaluate(js)
            except Exception as exc:
                return _pw_err(s, "list_frames", exc)
            return _ok(count=len(data), frames=data)
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))


@mcp.tool()
async def frame_click(
    session_id: str,
    frame_selector: str,
    selector: str,
    ctx: Context,
    timeout_ms: int = 30000,
) -> Dict[str, Any]:
    """Click ``selector`` inside the iframe addressed by ``frame_selector``.

    ``frame_selector`` is a CSS selector for the ``<iframe>`` (chain nested frames
    with ``>>>``); discover it via ``list_frames``. Ideal for reCAPTCHA checkboxes
    and payment widgets living in cross-origin frames."""
    if error := _timeout_error(timeout_ms):
        return _err(error)
    try:
        async with _use_page(session_id) as (s, page):
            try:
                await _frame_locator(page, frame_selector).locator(selector).click(timeout=timeout_ms)
            except Exception as exc:
                return _pw_err(s, "frame_click", exc)
            return _ok(frame_selector=frame_selector, selector=selector)
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))


@mcp.tool()
async def frame_fill(
    session_id: str,
    frame_selector: str,
    selector: str,
    value: str,
    ctx: Context,
    timeout_ms: int = 30000,
) -> Dict[str, Any]:
    """Clear and fill ``selector`` inside the iframe ``frame_selector`` with ``value``."""
    if error := _timeout_error(timeout_ms):
        return _err(error)
    try:
        async with _use_page(session_id) as (s, page):
            try:
                await _frame_locator(page, frame_selector).locator(selector).fill(value, timeout=timeout_ms)
            except Exception as exc:
                return _pw_err(s, "frame_fill", exc)
            return _ok(frame_selector=frame_selector, selector=selector)
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))


@mcp.tool()
async def frame_type(
    session_id: str,
    frame_selector: str,
    selector: str,
    text: str,
    ctx: Context,
    delay_ms: int = 0,
    timeout_ms: int = 30000,
) -> Dict[str, Any]:
    """Type ``text`` key-by-key into ``selector`` inside the iframe ``frame_selector``."""
    if error := _range_error("delay_ms", delay_ms, 0, _MAX_DELAY_MS):
        return _err(error)
    if error := _timeout_error(timeout_ms):
        return _err(error)
    try:
        async with _use_page(session_id) as (s, page):
            try:
                loc = _frame_locator(page, frame_selector).locator(selector)
                await loc.type(text, delay=delay_ms, timeout=timeout_ms)
            except Exception as exc:
                return _pw_err(s, "frame_type", exc)
            return _ok(frame_selector=frame_selector, selector=selector, length=len(text))
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))


@mcp.tool()
async def frame_get_text(
    session_id: str,
    frame_selector: str,
    ctx: Context,
    selector: str = "body",
    max_chars: int = 20000,
) -> Dict[str, Any]:
    """Get inner text of ``selector`` inside the iframe ``frame_selector``.

    Defaults to the frame's whole ``body``. Output is capped to ``max_chars``."""
    if error := _range_error("max_chars", max_chars, 0, _MAX_TEXT_CHARS):
        return _err(error)
    try:
        async with _use_page(session_id) as (s, page):
            try:
                loc = _frame_locator(page, frame_selector).locator(selector)
                text = await loc.first.inner_text()
            except Exception as exc:
                return _pw_err(s, "frame_get_text", exc)
            truncated = len(text) > max_chars
            return _ok(text=text[:max_chars], truncated=truncated, full_length=len(text))
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))


@mcp.tool()
async def frame_wait_for_selector(
    session_id: str,
    frame_selector: str,
    selector: str,
    ctx: Context,
    timeout_ms: int = 30000,
    state: Literal["attached", "detached", "hidden", "visible"] = "visible",
) -> Dict[str, Any]:
    """Wait until ``selector`` inside the iframe ``frame_selector`` reaches ``state``.

    ``state``: ``visible`` | ``hidden`` | ``attached`` | ``detached``."""
    if error := _timeout_error(timeout_ms):
        return _err(error)
    try:
        async with _use_page(session_id) as (s, page):
            try:
                loc = _frame_locator(page, frame_selector).locator(selector)
                await loc.first.wait_for(timeout=timeout_ms, state=state)
            except Exception as exc:
                return _pw_err(s, "frame_wait_for_selector", exc)
            return _ok(
                frame_selector=frame_selector,
                selector=selector,
                state=state,
                reached=True,
            )
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))


@mcp.tool()
async def frame_query_elements(
    session_id: str,
    frame_selector: str,
    selector: str,
    ctx: Context,
    max_results: int = 50,
) -> Dict[str, Any]:
    """Structured snapshot of elements matching ``selector`` INSIDE an iframe.

    Same shape as ``query_elements`` but frame-aware (the page-level version can't
    see into iframes). ``frame_selector`` is the iframe CSS selector chain
    (``>>>`` for nesting); the frames must already be present on the page."""
    if error := _range_error(
        "max_results", max_results, 0, _MAX_ELEMENT_RESULTS
    ):
        return _err(error)
    try:
        async with _use_page(session_id) as (s, page):
            try:
                frame = await _resolve_frame(page, frame_selector)
                data = await frame.evaluate(_QUERY_ELEMENTS_JS, {"sel": selector, "limit": max_results})
            except Exception as exc:
                return _pw_err(s, "frame_query_elements", exc)
            return _ok(frame_selector=frame_selector, selector=selector, count=len(data), elements=data)
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))


@mcp.tool()
async def frame_evaluate(
    session_id: str,
    frame_selector: str,
    script: str,
    ctx: Context,
    arg: Any = None,
    timeout_ms: int = 30000,
) -> Dict[str, Any]:
    """Execute arbitrary JavaScript INSIDE the iframe ``frame_selector`` and return its value.

    Like ``evaluate`` but runs in the frame's realm — the page-level ``evaluate``
    can't see into iframes, so use this to read cross-origin widget state (e.g. a
    payment iframe's hidden token). ``script`` is evaluated as a function body
    receiving ``arg``; e.g. ``() => document.title``.

    ``timeout_ms`` bounds the server wait; ``0`` disables the server timeout,
    while positive values use ``asyncio.wait_for``. On timeout the JS
    keeps running in the frame and the frame may be left unresponsive — recover
    with ``close_page`` + ``new_page``. Use sparingly.
    """
    if error := _timeout_error(timeout_ms):
        return _err(error)
    try:
        async with _use_page(session_id) as (s, page):
            try:
                frame = await _resolve_frame(page, frame_selector)
                result = await _await_with_timeout(
                    frame.evaluate(script, arg), timeout_ms
                )
            except asyncio.TimeoutError:
                return _err(
                    f"frame_evaluate timed out after {timeout_ms}ms; "
                    f"the frame may be unresponsive — close_page to recover"
                )
            except Exception as exc:
                return _pw_err(s, "frame_evaluate", exc)
            try:
                result_size = _serialized_size(result)
            except (TypeError, ValueError) as exc:
                return _err(
                    f"frame_evaluate returned a non-serializable result: {exc}"
                )
            if result_size > _MAX_EVALUATE_RESULT_BYTES:
                return _err(
                    "frame_evaluate result exceeds byte limit "
                    f"({_MAX_EVALUATE_RESULT_BYTES})"
                )
            return _ok(result=result)
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))


@mcp.tool()
async def frame_get_html(
    session_id: str,
    frame_selector: str,
    ctx: Context,
    selector: str = "",
    max_chars: int = 20000,
) -> Dict[str, Any]:
    """Get HTML inside the iframe ``frame_selector``.

    Empty ``selector`` = the frame's full content (``frame.content()``);
    otherwise the matched element's ``outerHTML``. Capped to ``max_chars``.
    """
    if error := _range_error("max_chars", max_chars, 0, _MAX_TEXT_CHARS):
        return _err(error)
    try:
        async with _use_page(session_id) as (s, page):
            try:
                frame = await _resolve_frame(page, frame_selector)
                if selector:
                    html = await frame.eval_on_selector(selector, "el => el.outerHTML")
                else:
                    html = await frame.content()
            except Exception as exc:
                return _pw_err(s, "frame_get_html", exc)
            truncated = len(html) > max_chars
            return _ok(html=html[:max_chars], truncated=truncated, full_length=len(html))
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))


@mcp.tool()
async def frame_get_attribute(
    session_id: str,
    frame_selector: str,
    selector: str,
    attribute: str,
    ctx: Context,
) -> Dict[str, Any]:
    """Return a single attribute of the first element matching ``selector`` inside the iframe ``frame_selector``."""
    try:
        async with _use_page(session_id) as (s, page):
            try:
                frame = await _resolve_frame(page, frame_selector)
                val = await frame.eval_on_selector(
                    selector, "(el, attr) => el.getAttribute(attr)", attribute
                )
            except Exception as exc:
                return _pw_err(s, "frame_get_attribute", exc)
            return _ok(
                frame_selector=frame_selector,
                selector=selector,
                attribute=attribute,
                value=val,
            )
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main() -> None:
    """Run the MCP server over stdio."""
    _log.info("starting spectra-mcp (stdio, tool_profile=%s)", _TOOL_PROFILE)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

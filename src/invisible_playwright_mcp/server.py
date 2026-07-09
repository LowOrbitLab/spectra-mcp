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
import secrets
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import Context, FastMCP, Image

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
_log = logging.getLogger("invisible_playwright_mcp")
if not _log.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    _log.addHandler(_h)
    _log.setLevel(logging.INFO)


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
    contexts: List[Any] = field(default_factory=list)  # contexts we created (Browser path)
    pages: Dict[int, Any] = field(default_factory=dict)  # page_id -> Page
    next_page_id: int = 1
    active_page_id: Optional[int] = None
    closed: bool = False


_SESSIONS: Dict[str, _Session] = {}
_LOCK = asyncio.Lock()


def _ok(**kw: Any) -> Dict[str, Any]:
    kw["ok"] = True
    return kw


def _err(msg: str, **kw: Any) -> Dict[str, Any]:
    kw["ok"] = False
    kw["error"] = msg
    return kw


async def _get_session(session_id: str) -> _Session:
    async with _LOCK:
        s = _SESSIONS.get(session_id)
    if s is None:
        raise KeyError(f"unknown session_id: {session_id!r}")
    if s.closed:
        raise KeyError(f"session {session_id!r} is closed")
    return s


def _active_page(session: _Session) -> Any:
    if session.active_page_id is None:
        raise RuntimeError("session has no active page; call new_page first")
    page = session.pages.get(session.active_page_id)
    if page is None or page.is_closed():
        raise RuntimeError("active page is gone; switch_page or new_page")
    return page


async def _make_page(session: _Session) -> Any:
    """Create a new page, honouring the patched new_context defaults."""
    boc = session.browser_or_ctx
    if hasattr(boc, "new_context"):
        # Browser path: new_context() is patched by InvisiblePlaywright to
        # inject fingerprint viewport/screen/timezone/locale defaults.
        ctx = await boc.new_context()
        session.contexts.append(ctx)
        page = await ctx.new_page()
    else:
        # Persistent BrowserContext path.
        page = await boc.new_page()
    pid = session.next_page_id
    session.next_page_id += 1
    session.pages[pid] = page
    session.active_page_id = pid
    return page, pid


async def _close_session(session: _Session) -> None:
    if session.closed:
        return
    for page in list(session.pages.values()):
        try:
            if not page.is_closed():
                await page.close()
        except Exception:
            pass
    for ctx in session.contexts:
        try:
            await ctx.close()
        except Exception:
            pass
    session.contexts.clear()
    session.pages.clear()
    try:
        await session.ipw.__aexit__(None, None, None)
    except Exception as exc:
        _log.warning("error tearing down session %s: %s", session.session_id, exc)
    session.closed = True


async def _close_all_sessions() -> None:
    async with _LOCK:
        sessions = list(_SESSIONS.values())
        _SESSIONS.clear()
    for s in sessions:
        await _close_session(s)


@asynccontextmanager
async def _lifespan(app: FastMCP):
    try:
        yield {}
    finally:
        await _close_all_sessions()


mcp = FastMCP("invisible-playwright-mcp", lifespan=_lifespan)


# --------------------------------------------------------------------------- #
# Setup / binary tools
# --------------------------------------------------------------------------- #


@mcp.tool()
async def binary_status(ctx: Context) -> Dict[str, Any]:
    """Report whether the patched Firefox binary is already cached locally.

    Returns the cache path, version, and a ``ready`` flag. Does NOT download.
    """
    import sys as _sys

    vdir = cache_dir_for_version(BINARY_VERSION)
    entry_rel = BINARY_ENTRY_REL.get(_sys.platform)
    entry = vdir / entry_rel if entry_rel else None
    ready = bool(entry and entry.exists())
    await ctx.debug(f"binary_status ready={ready} path={entry}")
    return _ok(
        ready=ready,
        version=BINARY_VERSION,
        cache_dir=str(vdir),
        entry=str(entry) if entry else None,
        cache_root=str(cache_root()),
        broken_versions=list(BROKEN_VERSIONS),
    )


@mcp.tool()
async def fetch_binary(force: bool = False, ctx: Context = None) -> Dict[str, Any]:
    """Download (and verify) the patched Firefox binary if not already cached.

    One-time ~100 MB download, SHA256-verified. Set ``force=True`` to re-download
    even if a cached copy exists. Call this before ``start_session``. May take
    tens of seconds on a cold cache.
    """
    import shutil

    vdir = cache_dir_for_version(BINARY_VERSION)
    if force and vdir.exists():
        await ctx.info("force: removing existing cache dir")
        await asyncio.to_thread(shutil.rmtree, str(vdir), True)

    def _status(phase: str) -> None:
        _log.info("fetch_binary phase=%s", phase)
        try:
            asyncio.get_event_loop()  # no-op; keep logger path
        except Exception:
            pass

    await ctx.info("ensuring patched Firefox binary (may download ~100 MB)")
    path = await asyncio.to_thread(ensure_binary, BINARY_VERSION, None, _status)
    await ctx.info(f"binary ready at {path}")
    return _ok(path=str(path), version=BINARY_VERSION, cache_root=str(cache_root()))


# --------------------------------------------------------------------------- #
# Session lifecycle
# --------------------------------------------------------------------------- #


@mcp.tool()
async def start_session(
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
    ctx: Context = None,
) -> Dict[str, Any]:
    """Start an anti-detect browser session with a fresh (or seeded) fingerprint.

    Returns ``session_id``, ``seed`` (log it to replay), and the initial
    ``page_id``. Every Playwright method on the resulting browser works as-is.

    Parameters
    ----------
    seed : int, optional
        Reproducible fingerprint. Random per session when omitted.
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
    """
    proxy: Optional[Dict[str, str]] = None
    if proxy_server:
        proxy = {"server": proxy_server}
        if proxy_username:
            proxy["username"] = proxy_username
        if proxy_password:
            proxy["password"] = proxy_password

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

    sid = secrets.token_hex(8)
    await ctx.info(f"launching session {sid} (seed={ipw.seed})")
    try:
        browser_or_ctx = await ipw.__aenter__()
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
    )
    try:
        page, pid = await _make_page(session)
    except Exception as exc:
        await _close_session(session)
        return _err(f"initial page failed: {exc}")

    async with _LOCK:
        _SESSIONS[sid] = session

    await ctx.info(f"session {sid} ready, page_id={pid}")
    return _ok(
        session_id=sid,
        seed=ipw.seed,
        persistent=persistent,
        page_id=pid,
        url=page.url,
    )


@mcp.tool()
async def close_session(session_id: str, ctx: Context = None) -> Dict[str, Any]:
    """Close a browser session and free its Firefox process."""
    async with _LOCK:
        session = _SESSIONS.pop(session_id, None)
    if session is None:
        return _err(f"unknown session_id: {session_id!r}")
    await _close_session(session)
    await ctx.info(f"closed session {session_id}")
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
            if not s.closed
        ]
    return _ok(sessions=items)


@mcp.tool()
async def session_info(session_id: str, ctx: Context) -> Dict[str, Any]:
    """Return details about a session: pages, active page, current url/title."""
    try:
        s = await _get_session(session_id)
    except KeyError as exc:
        return _err(str(exc))
    pages_info = []
    for pid, page in s.pages.items():
        try:
            pages_info.append({"page_id": pid, "url": page.url, "closed": page.is_closed()})
        except Exception:
            pages_info.append({"page_id": pid, "url": None, "closed": True})
    return _ok(
        session_id=s.session_id,
        seed=s.seed,
        persistent=s.persistent,
        active_page_id=s.active_page_id,
        pages=pages_info,
    )


# --------------------------------------------------------------------------- #
# Page management
# --------------------------------------------------------------------------- #


@mcp.tool()
async def new_page(session_id: str, ctx: Context = None) -> Dict[str, Any]:
    """Open a new page (tab) in the session and make it the active page."""
    try:
        s = await _get_session(session_id)
    except KeyError as exc:
        return _err(str(exc))
    try:
        page, pid = await _make_page(s)
    except Exception as exc:
        return _err(f"new_page failed: {exc}")
    return _ok(page_id=pid, url=page.url)


@mcp.tool()
async def close_page(session_id: str, page_id: Optional[int] = None, ctx: Context = None) -> Dict[str, Any]:
    """Close a page. Defaults to the active page. Switches active page if needed."""
    try:
        s = await _get_session(session_id)
    except KeyError as exc:
        return _err(str(exc))
    target = page_id if page_id is not None else s.active_page_id
    if target is None:
        return _err("no page to close")
    page = s.pages.pop(target, None)
    if page is None:
        return _err(f"unknown page_id: {target}")
    try:
        if not page.is_closed():
            await page.close()
    except Exception as exc:
        return _err(f"close_page failed: {exc}")
    if s.active_page_id == target:
        s.active_page_id = next(iter(s.pages), None)
    return _ok(closed_page_id=target, active_page_id=s.active_page_id)


@mcp.tool()
async def switch_page(session_id: str, page_id: int, ctx: Context = None) -> Dict[str, Any]:
    """Set the active page (subsequent tools act on it)."""
    try:
        s = await _get_session(session_id)
    except KeyError as exc:
        return _err(str(exc))
    if page_id not in s.pages:
        return _err(f"unknown page_id: {page_id}")
    if s.pages[page_id].is_closed():
        return _err(f"page {page_id} is closed")
    s.active_page_id = page_id
    return _ok(active_page_id=page_id, url=s.pages[page_id].url)


@mcp.tool()
async def list_pages(session_id: str, ctx: Context) -> Dict[str, Any]:
    """List pages in a session with their id, url and closed flag."""
    try:
        s = await _get_session(session_id)
    except KeyError as exc:
        return _err(str(exc))
    pages = []
    for pid, page in s.pages.items():
        try:
            pages.append({"page_id": pid, "url": page.url, "closed": page.is_closed()})
        except Exception:
            pages.append({"page_id": pid, "url": None, "closed": True})
    return _ok(active_page_id=s.active_page_id, pages=pages)


# --------------------------------------------------------------------------- #
# Navigation
# --------------------------------------------------------------------------- #


async def _resolve_page(session_id: str) -> Any:
    s = await _get_session(session_id)
    return _active_page(s)


@mcp.tool()
async def goto(
    session_id: str,
    url: str,
    timeout_ms: int = 30000,
    wait_until: str = "load",
    ctx: Context = None,
) -> Dict[str, Any]:
    """Navigate the active page to ``url``.

    ``wait_until``: ``load`` | ``domcontentloaded`` | ``networkidle`` | ``commit``.
    """
    try:
        page = await _resolve_page(session_id)
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))
    try:
        resp = await page.goto(url, timeout=timeout_ms, wait_until=wait_until)
    except Exception as exc:
        return _err(f"goto failed: {exc}")
    status = resp.status if resp is not None else None
    return _ok(url=page.url, status=status, title=await page.title())


@mcp.tool()
async def go_back(session_id: str, timeout_ms: int = 30000, ctx: Context = None) -> Dict[str, Any]:
    try:
        page = await _resolve_page(session_id)
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))
    try:
        ok = await page.go_back(timeout=timeout_ms)
    except Exception as exc:
        return _err(f"go_back failed: {exc}")
    return _ok(url=page.url, navigated=bool(ok))


@mcp.tool()
async def go_forward(session_id: str, timeout_ms: int = 30000, ctx: Context = None) -> Dict[str, Any]:
    try:
        page = await _resolve_page(session_id)
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))
    try:
        ok = await page.go_forward(timeout=timeout_ms)
    except Exception as exc:
        return _err(f"go_forward failed: {exc}")
    return _ok(url=page.url, navigated=bool(ok))


@mcp.tool()
async def reload(session_id: str, timeout_ms: int = 30000, wait_until: str = "load", ctx: Context = None) -> Dict[str, Any]:
    try:
        page = await _resolve_page(session_id)
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))
    try:
        resp = await page.reload(timeout=timeout_ms, wait_until=wait_until)
    except Exception as exc:
        return _err(f"reload failed: {exc}")
    status = resp.status if resp is not None else None
    return _ok(url=page.url, status=status)


# --------------------------------------------------------------------------- #
# Interaction
# --------------------------------------------------------------------------- #


@mcp.tool()
async def click(
    session_id: str,
    selector: str,
    timeout_ms: int = 30000,
    button: str = "left",
    click_count: int = 1,
    ctx: Context = None,
) -> Dict[str, Any]:
    """Click an element matched by ``selector`` (humanized Bezier mouse path)."""
    try:
        page = await _resolve_page(session_id)
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))
    try:
        await page.click(selector, timeout=timeout_ms, button=button, click_count=click_count)
    except Exception as exc:
        return _err(f"click failed: {exc}")
    return _ok(selector=selector)


@mcp.tool()
async def fill(
    session_id: str,
    selector: str,
    value: str,
    timeout_ms: int = 30000,
    ctx: Context = None,
) -> Dict[str, Any]:
    """Clear and fill an input/textarea with ``value`` (atomic, not per-keystroke)."""
    try:
        page = await _resolve_page(session_id)
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))
    try:
        await page.fill(selector, value, timeout=timeout_ms)
    except Exception as exc:
        return _err(f"fill failed: {exc}")
    return _ok(selector=selector, value=value)


@mcp.tool()
async def type_text(
    session_id: str,
    selector: str,
    text: str,
    delay_ms: int = 0,
    timeout_ms: int = 30000,
    ctx: Context = None,
) -> Dict[str, Any]:
    """Type ``text`` into an element one keystroke at a time (use for key-by-key input)."""
    try:
        page = await _resolve_page(session_id)
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))
    try:
        await page.type(selector, text, delay=delay_ms, timeout=timeout_ms)
    except Exception as exc:
        return _err(f"type failed: {exc}")
    return _ok(selector=selector, length=len(text))


@mcp.tool()
async def press_key(
    session_id: str,
    selector: str,
    key: str,
    timeout_ms: int = 30000,
    ctx: Context = None,
) -> Dict[str, Any]:
    """Focus ``selector`` and press a keyboard ``key`` (e.g. ``Enter``, ``Tab``, ``ArrowDown``)."""
    try:
        page = await _resolve_page(session_id)
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))
    try:
        await page.press(selector, key, timeout=timeout_ms)
    except Exception as exc:
        return _err(f"press_key failed: {exc}")
    return _ok(selector=selector, key=key)


@mcp.tool()
async def keyboard_press(session_id: str, key: str, ctx: Context = None) -> Dict[str, Any]:
    """Press a keyboard ``key`` on the focused element (no selector)."""
    try:
        page = await _resolve_page(session_id)
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))
    try:
        await page.keyboard.press(key)
    except Exception as exc:
        return _err(f"keyboard_press failed: {exc}")
    return _ok(key=key)


@mcp.tool()
async def select_option(
    session_id: str,
    selector: str,
    value: str,
    timeout_ms: int = 30000,
    ctx: Context = None,
) -> Dict[str, Any]:
    """Select an ``<option>`` by value in a ``<select>``."""
    try:
        page = await _resolve_page(session_id)
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))
    try:
        selected = await page.select_option(selector, value, timeout=timeout_ms)
    except Exception as exc:
        return _err(f"select_option failed: {exc}")
    return _ok(selector=selector, selected=list(selected))


@mcp.tool()
async def hover(
    session_id: str,
    selector: str,
    timeout_ms: int = 30000,
    ctx: Context = None,
) -> Dict[str, Any]:
    """Hover an element (humanized mouse path)."""
    try:
        page = await _resolve_page(session_id)
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))
    try:
        await page.hover(selector, timeout=timeout_ms)
    except Exception as exc:
        return _err(f"hover failed: {exc}")
    return _ok(selector=selector)


@mcp.tool()
async def focus(session_id: str, selector: str, timeout_ms: int = 30000, ctx: Context = None) -> Dict[str, Any]:
    try:
        page = await _resolve_page(session_id)
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))
    try:
        await page.focus(selector, timeout=timeout_ms)
    except Exception as exc:
        return _err(f"focus failed: {exc}")
    return _ok(selector=selector)


@mcp.tool()
async def check(session_id: str, selector: str, timeout_ms: int = 30000, ctx: Context = None) -> Dict[str, Any]:
    """Check a checkbox/radio matched by ``selector``."""
    try:
        page = await _resolve_page(session_id)
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))
    try:
        await page.check(selector, timeout=timeout_ms)
    except Exception as exc:
        return _err(f"check failed: {exc}")
    return _ok(selector=selector)


@mcp.tool()
async def uncheck(session_id: str, selector: str, timeout_ms: int = 30000, ctx: Context = None) -> Dict[str, Any]:
    """Uncheck a checkbox matched by ``selector``."""
    try:
        page = await _resolve_page(session_id)
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))
    try:
        await page.uncheck(selector, timeout=timeout_ms)
    except Exception as exc:
        return _err(f"uncheck failed: {exc}")
    return _ok(selector=selector)


@mcp.tool()
async def scroll(
    session_id: str,
    dx: int = 0,
    dy: int = 0,
    selector: str = "",
    ctx: Context = None,
) -> Dict[str, Any]:
    """Scroll the page by (dx, dy) pixels. If ``selector`` is given, scroll that element into view first."""
    try:
        page = await _resolve_page(session_id)
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))
    try:
        if selector:
            el = await page.query_selector(selector)
            if el is not None:
                await el.scroll_into_view_if_needed()
        await page.mouse.wheel(dx, dy)
    except Exception as exc:
        return _err(f"scroll failed: {exc}")
    return _ok(dx=dx, dy=dy)


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #


@mcp.tool()
async def get_url(session_id: str, ctx: Context = None) -> Dict[str, Any]:
    try:
        page = await _resolve_page(session_id)
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))
    return _ok(url=page.url)


@mcp.tool()
async def get_title(session_id: str, ctx: Context = None) -> Dict[str, Any]:
    try:
        page = await _resolve_page(session_id)
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))
    try:
        title = await page.title()
    except Exception as exc:
        return _err(f"get_title failed: {exc}")
    return _ok(title=title)


@mcp.tool()
async def get_text(
    session_id: str,
    selector: str = "",
    max_chars: int = 20000,
    ctx: Context = None,
) -> Dict[str, Any]:
    """Get visible text. Empty ``selector`` = the whole body. Output is capped to ``max_chars``."""
    try:
        page = await _resolve_page(session_id)
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))
    try:
        if selector:
            el = await page.query_selector(selector)
            if el is None:
                return _err(f"no element matches {selector!r}")
            text = await el.inner_text()
        else:
            text = await page.inner_text("body")
    except Exception as exc:
        return _err(f"get_text failed: {exc}")
    truncated = len(text) > max_chars
    return _ok(text=text[:max_chars], truncated=truncated, full_length=len(text))


@mcp.tool()
async def get_html(
    session_id: str,
    selector: str = "",
    max_chars: int = 20000,
    ctx: Context = None,
) -> Dict[str, Any]:
    """Get HTML. Empty ``selector`` = full document (``page.content()``). Capped to ``max_chars``."""
    try:
        page = await _resolve_page(session_id)
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))
    try:
        if selector:
            html = await page.eval_on_selector(
                selector, "el => el.outerHTML"
            )
        else:
            html = await page.content()
    except Exception as exc:
        return _err(f"get_html failed: {exc}")
    truncated = len(html) > max_chars
    return _ok(html=html[:max_chars], truncated=truncated, full_length=len(html))


@mcp.tool()
async def get_attribute(
    session_id: str,
    selector: str,
    attribute: str,
    ctx: Context = None,
) -> Dict[str, Any]:
    """Return a single attribute of the first element matching ``selector``."""
    try:
        page = await _resolve_page(session_id)
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))
    try:
        val = await page.get_attribute(selector, attribute)
    except Exception as exc:
        return _err(f"get_attribute failed: {exc}")
    return _ok(selector=selector, attribute=attribute, value=val)


@mcp.tool()
async def query_elements(
    session_id: str,
    selector: str,
    max_results: int = 50,
    ctx: Context = None,
) -> Dict[str, Any]:
    """Return a structured snapshot of elements matching ``selector``.

    Each entry: tag, id, name, type, role, href, text (<=200 chars), value,
    placeholder, visible flag and bounding rect. Useful for the LLM to decide
    what to click next without scraping raw HTML.
    """
    try:
        page = await _resolve_page(session_id)
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))
    js = """
    (params) => {
      const els = Array.from(document.querySelectorAll(params.sel)).slice(0, params.limit);
      return els.map(e => {
        const r = e.getBoundingClientRect();
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
          visible: !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length),
          rect: { x: r.x, y: r.y, w: r.width, h: r.height }
        };
      });
    }
    """
    try:
        data = await page.evaluate(js, {"sel": selector, "limit": max_results})
    except Exception as exc:
        return _err(f"query_elements failed: {exc}")
    return _ok(selector=selector, count=len(data), elements=data)


@mcp.tool()
async def is_visible(
    session_id: str,
    selector: str,
    timeout_ms: int = 5000,
    ctx: Context = None,
) -> Dict[str, Any]:
    """Check whether ``selector`` matches a visible element (with a short wait)."""
    try:
        page = await _resolve_page(session_id)
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))
    try:
        loc = page.locator(selector)
        visible = await loc.first.is_visible(timeout=timeout_ms)
    except Exception as exc:
        return _err(f"is_visible failed: {exc}")
    return _ok(selector=selector, visible=bool(visible))


# --------------------------------------------------------------------------- #
# Screenshot
# --------------------------------------------------------------------------- #


@mcp.tool()
async def screenshot(
    session_id: str,
    full_page: bool = False,
    image_format: str = "jpeg",
    quality: int = 85,
    ctx: Context = None,
) -> Image:
    """Capture a screenshot of the active page and return it as image content.

    Defaults to JPEG (``quality=85``) to keep payloads small for the LLM; set
    ``image_format="png"`` for lossless (much larger) output. ``full_page=True``
    captures the whole scrollable page.
    """
    try:
        page = await _resolve_page(session_id)
    except (KeyError, RuntimeError) as exc:
        raise RuntimeError(str(exc)) from exc
    fmt = (image_format or "jpeg").lower()
    try:
        if fmt == "png":
            data = await page.screenshot(full_page=full_page, type="png")
            mime = "png"
        else:
            data = await page.screenshot(full_page=full_page, type="jpeg", quality=quality)
            mime = "jpeg"
    except Exception as exc:
        raise RuntimeError(f"screenshot failed: {exc}") from exc
    await ctx.debug(f"screenshot fmt={mime} full_page={full_page} bytes={len(data)}")
    return Image(data=data, format=mime)


# --------------------------------------------------------------------------- #
# Waiting
# --------------------------------------------------------------------------- #


@mcp.tool()
async def wait_for_selector(
    session_id: str,
    selector: str,
    timeout_ms: int = 30000,
    state: str = "visible",
    ctx: Context = None,
) -> Dict[str, Any]:
    """Wait until an element matching ``selector`` reaches ``state``.

    ``state``: ``visible`` | ``hidden`` | ``attached`` | ``detached``.
    """
    try:
        page = await _resolve_page(session_id)
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))
    try:
        el = await page.wait_for_selector(selector, timeout=timeout_ms, state=state)
    except Exception as exc:
        return _err(f"wait_for_selector failed: {exc}")
    return _ok(selector=selector, state=state, matched=el is not None)


@mcp.tool()
async def wait_for_timeout(session_id: str, ms: int, ctx: Context = None) -> Dict[str, Any]:
    """Sleep for ``ms`` milliseconds (used to let async page updates settle)."""
    try:
        page = await _resolve_page(session_id)
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))
    await page.wait_for_timeout(ms)
    return _ok(ms=ms)


# --------------------------------------------------------------------------- #
# Advanced
# --------------------------------------------------------------------------- #


@mcp.tool()
async def evaluate(
    session_id: str,
    script: str,
    arg: Any = None,
    ctx: Context = None,
) -> Dict[str, Any]:
    """Execute arbitrary JavaScript in the active page and return its value.

    ``script`` is evaluated as an async function body receiving ``arg``:
    e.g. ``async (arg) => { return document.title; }`` or a plain expression.
    The result is JSON-serialized. Use sparingly — arbitrary JS is powerful.
    """
    try:
        page = await _resolve_page(session_id)
    except (KeyError, RuntimeError) as exc:
        return _err(str(exc))
    try:
        result = await page.evaluate(script, arg)
    except Exception as exc:
        return _err(f"evaluate failed: {exc}")
    try:
        rendered = json.dumps(result, default=str, ensure_ascii=False)
    except Exception:
        rendered = str(result)
    return _ok(result=rendered)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main() -> None:
    """Run the MCP server over stdio."""
    _log.info("starting invisible-playwright-mcp (stdio)")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

"""Linux-coherent launcher for the patched invisible_playwright Firefox."""

from __future__ import annotations

import asyncio
import os
import secrets
from typing import Any

from invisible_playwright._geo import prepare_session_geo, resolve_session_locale
from invisible_playwright._headless import make_virtual_display
from invisible_playwright._proxy import configure_proxy
from invisible_playwright.async_api import _patch_new_page_sleep
from invisible_playwright.download import ensure_binary
from invisible_playwright.launcher import _tz_env
from playwright.async_api import Browser, Playwright, async_playwright

_LINUX_RENDERERS = (
    ("Intel", "Mesa Intel(R) UHD Graphics 630 (CFL GT2)"),
    ("AMD", "AMD Radeon RX 580 Series (polaris10, LLVM 15.0.7, DRM 3.54, 6.6.0)"),
)


class LinuxNativePlaywright:
    """Launch a Linux identity without Spectra's cross-OS Windows overrides."""

    def __init__(
        self,
        seed: int | None = None,
        *,
        headless: bool = True,
        proxy: dict[str, str] | None = None,
        humanize: bool | float = True,
        locale: str = "auto",
        timezone: str = "",
    ) -> None:
        self.seed = int(seed) if seed is not None else secrets.randbits(31)
        self._headless = headless
        self._proxy = proxy
        self._humanize = humanize
        self._locale = locale
        self._timezone = timezone
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._virtual_display: Any = None
        self._webrtc_egress_ip: str | None = None

    async def __aenter__(self) -> Browser:
        geo = await asyncio.to_thread(
            prepare_session_geo,
            self._timezone,
            self._proxy,
        )
        self._timezone = geo.timezone
        self._webrtc_egress_ip = geo.egress_ip
        if (self._locale or "").strip().lower() == "auto":
            self._locale = await asyncio.to_thread(
                resolve_session_locale,
                geo.egress_ip,
                self._proxy,
            )

        prefs = self._prefs()
        playwright_proxy = configure_proxy(self._proxy, prefs)
        headless = self._resolve_headless()
        try:
            self._pw = await async_playwright().start()
            self._browser = await self._pw.firefox.launch(
                executable_path=str(ensure_binary()),
                headless=headless,
                firefox_user_prefs=prefs,
                proxy=playwright_proxy,
                env=self._environment(),
            )
        except BaseException:
            await self._teardown()
            raise
        self._patch_context_defaults(self._browser)
        return self._browser

    async def __aexit__(self, *_: Any) -> None:
        await self._teardown()

    def _prefs(self) -> dict[str, Any]:
        vendor, renderer = _LINUX_RENDERERS[self.seed % len(_LINUX_RENDERERS)]
        prefs: dict[str, Any] = {
            "browser.newtabpage.enabled": False,
            "browser.newtab.preload": False,
            "browser.newtabpage.activity-stream.enabled": False,
            "zoom.stealth.debugger.force_detach": True,
            "zoom.stealth.webgl.vendor": vendor,
            "zoom.stealth.webgl.renderer": renderer,
            "stealthfox.humanize": bool(self._humanize),
        }
        if self._humanize:
            cap = 1.5 if self._humanize is True else float(self._humanize)
            prefs["stealthfox.humanize.maxTime"] = str(cap)
        return prefs

    def _patch_context_defaults(self, browser: Browser) -> None:
        original = browser.new_context
        defaults: dict[str, Any] = {}
        if self._timezone:
            defaults["timezone_id"] = self._timezone
        if self._locale:
            defaults["locale"] = self._locale

        async def patched(**kwargs: Any) -> Any:
            context = await original(**{**defaults, **kwargs})
            _patch_new_page_sleep(context)
            return context

        browser.new_context = patched  # type: ignore[method-assign]

    def _resolve_headless(self) -> bool:
        if not self._headless:
            return False
        display = make_virtual_display()
        if display is not None:
            display.start()
            self._virtual_display = display
        return False

    def _environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        if self._timezone:
            environment["TZ"] = _tz_env(self._timezone)
        if self._webrtc_egress_ip:
            environment["STEALTHFOX_WEBRTC_PUBLIC_IP"] = self._webrtc_egress_ip
            environment["STEALTHFOX_WEBRTC_DISABLE_IPV6"] = "1"
        return environment

    async def _teardown(self) -> None:
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:
                pass
            self._pw = None
        if self._virtual_display is not None:
            try:
                self._virtual_display.stop()
            except Exception:
                pass
            self._virtual_display = None

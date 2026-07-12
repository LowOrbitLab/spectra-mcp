from spectra_mcp.linux_native import LinuxNativePlaywright


def test_linux_native_prefs_keep_linux_identity_and_detach_debugger() -> None:
    launcher = LinuxNativePlaywright(seed=15, humanize=True)

    prefs = launcher._prefs()

    assert "general.useragent.override" not in prefs
    assert "general.platform.override" not in prefs
    assert prefs["zoom.stealth.debugger.force_detach"] is True
    assert prefs["zoom.stealth.webgl.vendor"] == "AMD"
    assert "Radeon" in prefs["zoom.stealth.webgl.renderer"]
    assert prefs["stealthfox.humanize"] is True


def test_linux_native_renderer_is_deterministic_from_seed() -> None:
    first = LinuxNativePlaywright(seed=12)._prefs()
    repeated = LinuxNativePlaywright(seed=12)._prefs()
    other = LinuxNativePlaywright(seed=15)._prefs()

    assert first["zoom.stealth.webgl.renderer"] == repeated[
        "zoom.stealth.webgl.renderer"
    ]
    assert first["zoom.stealth.webgl.renderer"] != other[
        "zoom.stealth.webgl.renderer"
    ]

# encoding:utf-8

"""Starting the debug browser the client attaches to.

Nothing here launches a real browser: ``subprocess.Popen`` and the port probe
are both replaced, so what is under test is the decision-making -- where the
executable and profile come from, and which failures are reported with a fix
the operator can act on rather than as a bare "no browser is listening".
"""

import os

import pytest

from douyin4u import browser


@pytest.fixture
def launched(monkeypatch):
    """Capture the command line instead of starting anything."""
    calls = []
    monkeypatch.setattr(
        browser.subprocess, "Popen",
        lambda args, **kw: calls.append((args, kw)) or object(),
    )
    return calls


def _port_opens(monkeypatch, *answers):
    """Make the port probe answer each of *answers* in turn, last one sticking."""
    remaining = list(answers)
    monkeypatch.setattr(
        browser, "cdp_port_open",
        lambda url: remaining.pop(0) if len(remaining) > 1 else remaining[0],
    )


@pytest.fixture
def edge(tmp_path):
    exe = tmp_path / "msedge.exe"
    exe.write_text("")
    return str(exe)


# ------------------------------------------------------------------- profile
def test_the_profile_defaults_to_the_dedicated_one():
    """This repo's own folder, not a path under some host's data directory --
    see browser.py's module docstring and ``_default_profile_dir``."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(browser.__file__)))
    assert browser.browser_profile_dir("") == os.path.join(repo_root, "browser-profile")


def test_a_configured_profile_wins_and_expands_a_home_shortcut():
    # Normalized before comparing: expanduser keeps whichever separators the
    # operator typed, and both work on Windows.
    resolved = os.path.normpath(browser.browser_profile_dir("~/elsewhere/profile"))
    assert "~" not in resolved
    assert resolved.endswith(os.path.join("elsewhere", "profile"))


# ---------------------------------------------------------------- executable
def test_a_configured_browser_path_is_used_as_given(edge):
    assert browser.resolve_browser_path(edge) == edge


def test_a_configured_browser_path_that_does_not_exist_is_not_silently_replaced():
    """An operator who names a specific build means that build; quietly
    falling back to another one would send the automation somewhere they did
    not ask for."""
    assert browser.resolve_browser_path(r"C:\nope\msedge.exe") == ""


# -------------------------------------------------------------------- launch
def test_the_browser_is_started_on_the_given_port_and_profile(launched, monkeypatch, tmp_path, edge):
    profile = tmp_path / "profile"
    _port_opens(monkeypatch, True)

    ok, detail = browser.launch_debug_browser("http://127.0.0.1:9333", edge, str(profile))

    assert (ok, detail) == (True, "")
    args = launched[0][0]
    assert args[0] == edge
    assert "--remote-debugging-port=9333" in args
    assert f"--user-data-dir={profile}" in args
    # The chat page is the first tab, so the client finds it already open
    # rather than a blank window it would have to navigate.
    assert args[-1] == browser.CHAT_URL
    assert profile.exists()


def test_the_launch_waits_for_the_port_rather_than_assuming_it(launched, monkeypatch, tmp_path, edge):
    """The port answers a moment after the process starts, not with it."""
    monkeypatch.setattr(browser, "_PORT_POLL_SECONDS", 0.01)
    _port_opens(monkeypatch, False, False, True)

    ok, _ = browser.launch_debug_browser("http://127.0.0.1:9333", edge, str(tmp_path / "p"))

    assert ok is True


def test_a_port_that_never_opens_names_the_profile_to_close(launched, monkeypatch, tmp_path, edge):
    """The silent failure this cannot detect any other way: launching Edge on
    a profile some window already holds hands the command to that window and
    drops the debug port, with no error to catch."""
    monkeypatch.setattr(browser, "_PORT_POLL_SECONDS", 0.01)
    _port_opens(monkeypatch, False)

    ok, detail = browser.launch_debug_browser(
        "http://127.0.0.1:9333", edge, str(tmp_path / "p"), wait_seconds=0.05
    )

    assert ok is False
    assert "close that window" in detail
    assert str(tmp_path / "p") in detail


def test_a_missing_browser_says_how_to_fix_it(launched, monkeypatch):
    monkeypatch.setattr(browser, "resolve_browser_path", lambda configured="": "")

    ok, detail = browser.launch_debug_browser("http://127.0.0.1:9333")

    assert ok is False
    assert "browser path" in detail and "--remote-debugging-port=9333" in detail
    assert launched == []


def test_a_cdp_url_with_no_port_is_refused_before_anything_starts(launched):
    ok, detail = browser.launch_debug_browser("not-a-url")

    assert ok is False
    assert "no port" in detail
    assert launched == []


def test_a_browser_that_will_not_start_is_reported_not_raised(monkeypatch, tmp_path, edge):
    """A client that cannot start its browser still has to leave the host running."""
    monkeypatch.setattr(
        browser.subprocess, "Popen",
        lambda args, **kw: (_ for _ in ()).throw(OSError("Access is denied")),
    )

    ok, detail = browser.launch_debug_browser("http://127.0.0.1:9333", edge, str(tmp_path / "p"))

    assert ok is False
    assert "Access is denied" in detail


def test_the_browser_outlives_this_process(launched, monkeypatch, tmp_path, edge):
    """The logged-in session has to survive a host restart, which is the whole
    reason the profile is long-lived."""
    _port_opens(monkeypatch, True)

    browser.launch_debug_browser("http://127.0.0.1:9333", edge, str(tmp_path / "p"))

    kwargs = launched[0][1]
    assert kwargs.get("creationflags") or kwargs.get("start_new_session")

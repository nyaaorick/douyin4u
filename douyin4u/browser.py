# encoding:utf-8

"""Bringing up the debug browser the client attaches to.

The client drives a real browser over CDP, which until 2026-09-18 had to be
started by hand: with nothing listening on the debug port, startup refused to
go on and printed instructions. That is a step the operator had to repeat
after every reboot, and getting it wrong looked like a client fault
(``connect_over_cdp ECONNREFUSED``), so the client now starts the browser
itself.

What it still does **not** do is log in. Launching a browser is a mechanical
step with one right answer; authenticating a real Douyin account is the
operator's own action, out of band, exactly as ``wcferry`` never logs WeChat
in. A browser started here that lands on the login screen is reported as
``logged_out`` by the client's own page-state check and waits for a human.

Two things make this narrower than "open a browser":

1. **The profile is fixed** (``profile_dir``, default
   ``~/cow/douyin-probe-profile``). The whole point is to reuse the session
   the operator already logged in once; a fresh profile would come up logged
   out every time and be useless. It is deliberately not the operator's
   everyday Edge profile -- automating a browser that holds their personal
   session is a different and much worse proposition.
2. **A profile that is already open cannot be given a debug port.** Launching
   Edge a second time on a profile that some window already holds hands the
   command to that window and silently drops ``--remote-debugging-port``.
   There is no error to catch -- the port simply never opens -- so that case
   is reported by the wait below timing out, and the message says what to
   close.

Every setting arrives as an argument. This package reads no configuration of
its own; the host application decides where its settings live.
"""

import logging
import os
import platform
import shutil
import socket
import subprocess
import time
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# The chat page itself is opened as the browser's first tab, so the client
# finds it already there instead of a blank window it would have to navigate.
# The main site's chat, not the creator center's: see client.py's module
# docstring for why the creator-center chat is never used.
CHAT_URL = "https://www.douyin.com/chat?isPopup=1"

DEFAULT_CDP_URL = "http://127.0.0.1:9222"

# Long enough for a cold Edge start on a spinning disk; short enough that an
# already-open profile (which never opens the port at all) is reported while
# the operator is still watching.
_PORT_WAIT_SECONDS = 20.0
_PORT_POLL_SECONDS = 0.5

# The pre-attach dial is loopback-only in practice, so a second is generous;
# it only has to outlast Windows' habit of retrying a refused localhost SYN
# for a while before giving up.
_PROBE_TIMEOUT_SECONDS = 1.0

_DEFAULT_PROFILE_SUBPATH = ("cow", "douyin-probe-profile")

# Where Edge installs itself, most common first. `shutil.which` covers a PATH
# install and the non-Windows names; nothing here is a guess the caller acts
# on blindly -- a path that does not exist is skipped, and finding none at all
# is reported rather than assumed.
_WINDOWS_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)
_MACOS_CANDIDATES = (
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
)
_WHICH_NAMES = ("msedge", "microsoft-edge", "microsoft-edge-stable", "msedge.exe")


def cdp_port_open(cdp_url: str) -> bool:
    """Is anything accepting TCP where the debug browser should be?

    Checked before Playwright is started at all, the way the WeChat gateway
    checks for WeChat.exe before dialing spy.dll: the common failure is simply
    that the browser is not running, and answering that with a socket dial
    means no node driver is spawned just to fail. A URL with no dialable host
    and port counts as closed. This is a diagnostic, not the gate: an open
    port that does not speak CDP still fails in ``connect_over_cdp`` and is
    reported from there.
    """
    try:
        parsed = urlparse(cdp_url)
        host, port = parsed.hostname, parsed.port
    except ValueError:
        return False
    if not host or not port:
        return False
    try:
        with socket.create_connection((host, port), timeout=_PROBE_TIMEOUT_SECONDS):
            return True
    except OSError:
        return False


def browser_profile_dir(configured: str = "") -> str:
    """The browser profile to reuse. See this module's docstring on why it is
    a fixed, dedicated one rather than the operator's everyday profile."""
    configured = (configured or "").strip()
    if configured:
        return os.path.expanduser(configured)
    return os.path.join(os.path.expanduser("~"), *_DEFAULT_PROFILE_SUBPATH)


def resolve_browser_path(configured: str = "") -> str:
    """Where Edge is, or ``""`` when it cannot be found.

    A configured path wins and is *not* second-guessed beyond existing --
    an operator who names a specific build means that build.
    """
    configured = (configured or "").strip()
    if configured:
        return configured if os.path.exists(configured) else ""

    system = platform.system()
    candidates = _WINDOWS_CANDIDATES if system == "Windows" else (
        _MACOS_CANDIDATES if system == "Darwin" else ()
    )
    for path in candidates:
        if os.path.exists(path):
            return path
    for name in _WHICH_NAMES:
        found = shutil.which(name)
        if found:
            return found
    return ""


def launch_debug_browser(cdp_url: str, browser_path: str = "", profile_dir: str = "",
                         wait_seconds: float = _PORT_WAIT_SECONDS):
    """Start the debug browser and wait for its port. ``(ok, detail)``.

    ``detail`` is empty on success and otherwise names the fix, because a host
    shows it to the operator as-is -- "no browser is listening" was accurate
    and useless, since the operator could not tell a missing Edge from a
    wrong port from a profile already open in another window.

    Never raises: a client that cannot start its browser still has to leave
    the host application running.
    """
    port = urlparse(cdp_url).port
    if not port:
        return False, f"the CDP URL ({cdp_url!r}) names no port to start a browser on"

    exe = resolve_browser_path(browser_path)
    if not exe:
        return False, (
            "could not find Microsoft Edge to start. Set the browser path to its "
            "full path, or start the browser yourself with "
            f"--remote-debugging-port={port}"
        )

    profile = browser_profile_dir(profile_dir)
    try:
        os.makedirs(profile, exist_ok=True)
    except OSError as e:
        return False, f"could not create the browser profile directory {profile}: {e}"

    logger.info(f"[Douyin4u] starting {os.path.basename(exe)} on port {port} with profile {profile}")
    try:
        subprocess.Popen(
            [
                exe,
                f"--remote-debugging-port={port}",
                "--remote-debugging-address=127.0.0.1",
                f"--user-data-dir={profile}",
                CHAT_URL,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            # Detached on purpose: the browser has to outlive this process so
            # the logged-in session survives a host restart, which is the
            # whole reason it is a long-lived profile and not a fresh one.
            **_detach_kwargs(),
        )
    except Exception as e:
        return False, f"could not start the browser at {exe}: {e}"

    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if cdp_port_open(cdp_url):
            logger.info(f"[Douyin4u] the browser is listening on port {port}")
            return True, ""
        time.sleep(_PORT_POLL_SECONDS)
    return False, (
        f"the browser was started but port {port} never opened within "
        f"{int(wait_seconds)}s. An Edge window already using the profile at {profile} "
        "takes the launch over and drops the debug port -- close that window and "
        "start again."
    )


def _detach_kwargs() -> dict:
    """Keep the browser out of this process's console and process group."""
    if platform.system() == "Windows":
        # DETACHED_PROCESS alone is enough; CREATE_NEW_PROCESS_GROUP keeps a
        # Ctrl+C aimed at the host from reaching the browser too.
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
        return {"creationflags": flags}
    return {"start_new_session": True}

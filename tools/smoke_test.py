# encoding:utf-8

"""Read-only live diagnostics for the Douyin chat page.

Attaches over CDP to whatever tab is already open on the www.douyin.com chat
page -- exactly like ``Douyin.start()`` does -- and prints a structured report
of what the page currently looks like: page state (ready / logged out /
challenged), the conversation roster, and the open panel's messages.

Read-only by design: it never clicks a conversation, types, or sends
anything. The account this attaches to is the operator's real, live account,
and a script an operator can run casually must never risk touching a real
fan's conversation. The one exception is the opt-in ``--open-chat`` flag,
which navigates from wherever the tab currently is to the chat page (one
fixed, hardcoded URL -- never an arbitrary one) if it isn't there already.
That's following a link inside an already-authenticated session, not driving
login or touching a conversation, which is why it's allowed despite the
read-only stance above.

Runs the exact same JS the client evaluates (``douyin4u/page_scripts.py``),
imported rather than re-derived, so a report from this script reflects what
the client itself would see, not a second copy of the scraping logic that
could quietly drift from it.

Why this exists: the client reads Douyin's page through test hooks, class
names and React internals that can change on any front-end deploy (see
page_scripts.py's own module docstring), so its selectors WILL eventually go
stale. When that happens, this is the tool to point at the live page and see
what broke, instead of re-deriving diagnostics from scratch the way the
client was first built (a string of throwaway probe scripts, 2026-09-15/16).

Usage, from this repository's root (playwright is a dependency as a driver
only; no ``playwright install`` browser download is needed, since this only
ever attaches to a browser the operator already has open)::

    python tools/smoke_test.py [--cdp-url http://127.0.0.1:9222] [--open-chat]
"""

import argparse
import json
import os
import sys
from urllib.parse import urlparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from douyin4u.browser import CHAT_URL, DEFAULT_CDP_URL  # noqa: E402
from douyin4u.client import _on_chat_page  # noqa: E402
from douyin4u.page_scripts import JS_DETECT_STATE, JS_READ_PANEL, JS_READ_ROSTER  # noqa: E402

_ROSTER_PRINT_LIMIT = 20
_PANEL_TAIL_LIMIT = 5


def _find_chat_page(browser):
    """The same tab choice as ``Douyin._first_douyin_page``: the chat tab,
    else any Douyin tab. Not imported, since that is a method of a client
    this script never constructs."""
    douyin_tab = None
    for ctx in browser.contexts:
        for pg in ctx.pages:
            try:
                url = pg.url or ""
            except Exception:
                continue
            if _on_chat_page(url):
                return pg
            host = urlparse(url).hostname or ""
            if douyin_tab is None and (host == "douyin.com" or host.endswith(".douyin.com")):
                douyin_tab = pg
    return douyin_tab


def _print_state(page) -> dict:
    state = json.loads(page.evaluate(JS_DETECT_STATE))
    print("\n--- page state (the same check the client runs every tick) ---")
    print(json.dumps(state, ensure_ascii=False, indent=2))
    if state.get("challenge"):
        print("[WARN] a bot-verification challenge is on screen right now.")
    elif state.get("logged_out"):
        print("[WARN] the session is logged out.")
    elif not state.get("chat_ready"):
        print("[WARN] page does not look like the chat UI -- selectors may be stale.")
    else:
        print("[OK] page looks like a normal, logged-in chat UI.")
    return state


def _print_roster(page) -> list:
    roster = json.loads(page.evaluate(JS_READ_ROSTER))
    print(f"\n--- roster: {len(roster)} conversation(s) ---")
    for row in roster[:_ROSTER_PRINT_LIMIT]:
        print(
            f"  [{row.get('index')}] {row.get('nickname')!r} "
            f"sec_uid={row.get('sec_uid')!r} group={row.get('is_group')} "
            f"stranger={row.get('is_stranger')} active={row.get('is_active')}"
        )
    if len(roster) > _ROSTER_PRINT_LIMIT:
        print(f"  ... and {len(roster) - _ROSTER_PRINT_LIMIT} more")
    return roster


def _print_panel(page) -> dict:
    panel = json.loads(page.evaluate(JS_READ_PANEL))
    print(f"\n--- open panel: {panel.get('header_text')!r} ---")
    print(f"  active_sec_uid={panel.get('active_sec_uid')!r}")
    items = panel.get("items") or []
    print(f"  {len(items)} row(s) visible; last {_PANEL_TAIL_LIMIT}:")
    for row in items[-_PANEL_TAIL_LIMIT:]:
        text = (row.get("text") or "")[:40]
        # self_only on one of our own messages means the fan never got it.
        self_only = " SELF-ONLY" if row.get("self_only") else ""
        print(f"    [{row.get('index')}] {row.get('kind')} is_me={row.get('is_me')}{self_only} {text!r}")
    return panel


def main() -> int:
    from playwright.sync_api import sync_playwright

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cdp-url", default=DEFAULT_CDP_URL)
    # Opt-in only, and to exactly one hardcoded destination -- not a general
    # "navigate anywhere" flag. See the module docstring.
    parser.add_argument("--open-chat", action="store_true")
    args = parser.parse_args()

    # Windows' console defaults to cp1252, which cannot encode most Chinese
    # text -- and every nickname/message this script prints is Chinese by
    # the nature of the product.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    print(f"[douyin-smoke] connecting to {args.cdp_url} ...")

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(args.cdp_url)
        except Exception as e:
            print(f"[FAIL] could not attach to a browser at {args.cdp_url}: {e}")
            print("       Open the operator's browser with --remote-debugging-port,")
            print("       log into www.douyin.com by hand, then re-run this script.")
            return 1

        page = _find_chat_page(browser)
        if page is None:
            print("[FAIL] no open tab is on the chat page.")
            print(f"       Open {CHAT_URL}")
            print("       and log in, then re-run this script.")
            return 1

        print(f"[OK] attached: {page.url}")

        if args.open_chat and not _on_chat_page(page.url or ""):
            print(f"[douyin-smoke] navigating to {CHAT_URL} ...")
            try:
                page.goto(CHAT_URL, wait_until="load", timeout=20000)
                page.wait_for_timeout(1500)  # let the SPA finish mounting
                print(f"[OK] now at: {page.url}")
            except Exception as e:
                print(f"[FAIL] navigation failed: {e}")
                return 1

        state = _print_state(page)
        if state.get("chat_ready"):
            _print_roster(page)
            _print_panel(page)
        else:
            print("\n[douyin-smoke] skipping roster/panel reads -- page is not in a ready state.")

    print("\n[douyin-smoke] done -- nothing was clicked, typed, or sent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# encoding:utf-8

"""Stand-ins for the Playwright page, and builders for what its scripts return.

Carried over from CowAgent's Douyin channel tests, where every one of these
knobs was added for a failure seen live; see each class for which.
"""

import json
import time

from douyin4u.tracker import PanelMessage

CHAT_URL = "https://www.douyin.com/chat?isPopup=1"
SHORT = "short1"
# Another page on the same site, where the operator may have clicked the tab to.
PROFILE_URL = "https://www.douyin.com/user/self"
# The chat page driven until 2026-09-21. See client.py's module docstring:
# nothing sent from it reached anyone.
CREATOR_CHAT_URL = "https://creator.douyin.com/creator-micro/data/following/chat"


class FakePage:
    """Stands in for a Playwright Page: mostly ``evaluate()``. Dispatches on
    which JS snippet was passed -- the scripts are distinguishable by a
    substring unique to each (the panel script builds a ``header_text`` key,
    the page-state script a ``chat_ready`` key).

    ``active_sec_uid`` is what the real panel script reports as the
    conversation currently on screen; it defaults to the id the drain tests
    use so they read as "the expected conversation is open", and a test can
    set it to something else to simulate the operator switching
    conversations mid-read. ``active_short_id`` is that conversation's id as
    every one of its message rows carries it.

    ``panels`` replays a sequence of panel reads, one per read, the last one
    repeating -- how a list that is still loading, or that fills in between
    two reads, is simulated.

    ``state`` and ``url`` default to a healthy, logged-in, fully loaded chat
    page so every test reads as "nothing is wrong with the browser" unless it
    says otherwise.
    """

    def __init__(self, roster=None, panel=None, active_sec_uid="fan1", panel_raises=False,
                 state=None, state_raises=False, url=CHAT_URL, goto_raises=False,
                 goto_lands_on=None, active_short_id=SHORT, panels=None):
        self.roster = roster if roster is not None else []
        self.panel = panel if panel is not None else {"header_text": None, "items": []}
        self.panels = list(panels) if panels else None
        self.active_sec_uid = active_sec_uid
        self.active_short_id = active_short_id
        self.panel_raises = panel_raises
        self.url = url
        # goto() lands on its target unless goto_lands_on names a redirect.
        self.goto_raises = goto_raises
        self.goto_lands_on = goto_lands_on
        self.gotos = []
        self.state = state if state is not None else {
            "challenge": False, "logged_out": False, "chat_ready": True, "ready_state": "complete",
        }
        self.state_raises = state_raises
        self.evaluated = []
        self.brought_to_front = 0
        # How often JS_SCROLL_TO_NEWEST ran; the fake list never moves.
        self.scrolled_back = 0
        # Every wait asked for, in order -- how the idle wait's own shape (one
        # long sleep vs. sliced) is asserted.
        self.waits = []

    def bring_to_front(self):
        self.brought_to_front += 1

    def wait_for_timeout(self, ms):
        self.waits.append(ms)

    def goto(self, url, wait_until=None, timeout=None):
        self.gotos.append((url, wait_until))
        if self.goto_raises:
            raise RuntimeError("net::ERR_TIMED_OUT")
        self.url = self.goto_lands_on or url

    def evaluate(self, js, *args, **kwargs):
        self.evaluated.append(js)
        if "scrolled_back" in js:
            self.scrolled_back += 1
            return json.dumps({"scrolled_back": True, "before": 1400})
        if "chat_ready" in js:
            if self.state_raises:
                raise RuntimeError("page went away mid-read")
            return json.dumps(self.state)
        if "header_text" in js:
            if self.panel_raises:
                raise RuntimeError("page went away mid-read")
            if self.panels:
                current = self.panels.pop(0) if len(self.panels) > 1 else self.panels[0]
            else:
                current = self.panel
            payload = dict(current)
            payload.setdefault("active_sec_uid", self.active_sec_uid)
            payload.setdefault("active_short_id", self.active_short_id)
            return json.dumps(payload)
        return json.dumps(self.roster)


class _FakeKeyboard:
    def __init__(self, page):
        self._page = page

    def type(self, char):
        self._page.press_key(char)

    def press(self, key):
        self._page.press_key(key)


class _FakeLocator:
    """Every locator the send path builds resolves to the same page-level
    click -- which element it was is irrelevant here, only whether the click
    landed and where the caret ended up."""

    def __init__(self, page):
        self._page = page

    @property
    def first(self):
        return self

    def nth(self, index):
        return self

    def click(self, timeout=None):
        self._page.click_element()


class FakeSendPage(FakePage):
    """FakePage plus the parts of the page the typing path touches: a composer
    that only accepts keystrokes while it holds the caret, and a panel that
    grows one of the account's own bubbles when Enter actually sends.

    Three knobs, one per failure mode the send path has to survive:

    ``focus_after_click=False`` -- the click lands but the caret goes
    somewhere else, so every keystroke would be typed into the page at large.
    ``steal_focus_after=N`` -- the caret is lost after N characters, leaving a
    half-typed box: what a reply truncated to its opening words looks like
    from this side (seen live 2026-09-17).
    ``enter_behaviour`` -- ``"send"`` delivers and clears; ``"empty_only"``
    clears the box without delivering anything, which is the case an
    empty-composer check alone cannot tell from success; ``"keep"`` leaves the
    text sitting there, the case of a send the page refused.
    """

    def __init__(self, *args, focus_after_click=True, steal_focus_after=None,
                 enter_behaviour="send", **kwargs):
        super().__init__(*args, **kwargs)
        self.composer = ""
        self.focused = False
        self.focus_after_click = focus_after_click
        self.steal_focus_after = steal_focus_after
        self.enter_behaviour = enter_behaviour
        self.typed = []
        self.delivered = []
        self.keyboard = _FakeKeyboard(self)

    def locator(self, selector):
        return _FakeLocator(self)

    def click_element(self):
        self.focused = self.focus_after_click

    def press_key(self, key):
        if key == "Backspace":
            self.composer = ""
        elif key == "Control+A":
            pass
        elif key == "Enter":
            if self.enter_behaviour == "keep":
                return
            if self.enter_behaviour == "send":
                self.deliver(self.composer)
            self.composer = ""
        else:
            self._type_char(key)

    def _type_char(self, char):
        # Keystrokes go wherever the caret is; a composer that does not have
        # it simply never sees them.
        if not self.focused:
            return
        self.composer += char
        self.typed.append(char)
        if self.steal_focus_after is not None and len(self.typed) >= self.steal_focus_after:
            self.focused = False

    def deliver(self, text):
        """Put *text* on the panel as one of the account's own bubbles."""
        self.delivered.append(text)
        items = self.panel.setdefault("items", [])
        items.append({
            "index": len(items), "kind": "text", "is_me": True, "text": text,
            "server_id": "", "seq": "", "created_at_ms": None, "short_id": SHORT,
        })

    def evaluate(self, js, *args, **kwargs):
        if "page_focused" in js:
            self.evaluated.append(js)
            return json.dumps({
                "present": True, "text": self.composer,
                "focused": self.focused, "page_focused": True,
            })
        return super().evaluate(js, *args, **kwargs)


# ------------------------------------------------------------------ builders
def row(sec_uid, **kw):
    """One roster row as JS_READ_ROSTER reports it."""
    out = {"sec_uid": sec_uid, "is_group": False, "is_stranger": False, "nickname": "n"}
    out.update(kw)
    return out


def now_ms():
    return int(time.time() * 1000)


def msg_rows(seq, text="t", is_me=False, kind="text", short_id=SHORT, server_id=None, created_at_ms=None):
    """One message as JS_READ_PANEL reports it: its date-divider row and its
    bubble row, both sitting on the same message object."""
    ids = {
        "server_id": server_id or f"sid{seq}",
        "seq": str(seq),
        "created_at_ms": now_ms() if created_at_ms is None else created_at_ms,
        "short_id": short_id,
    }
    return [
        {"index": 0, "kind": "divider", "is_me": False, "text": "", **ids},
        {"index": 0, "kind": kind, "is_me": is_me, "text": text, **ids},
    ]


def panel(*messages):
    return {"header_text": "小明", "items": [r for message in messages for r in message]}


def tracked(seq):
    """A mark's worth of message, for seeding ``client._marks``."""
    return PanelMessage(server_id=f"sid{seq}", seq=seq, created_at_ms=now_ms(), is_me=False,
                        kind="text", text="t")


def drained(client) -> list:
    """Every message the client has handed out so far, oldest first."""
    out = []
    while not client._msg_queue.empty():
        out.append(client._msg_queue.get_nowait())
    return out


def drain(client, sec_uid="fan1"):
    """Drain *sec_uid*'s open panel once: ``(handed_out, trusted)``."""
    trusted = client._drain_open_panel(sec_uid, [row(sec_uid)])
    return drained(client), trusted

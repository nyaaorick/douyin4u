# encoding:utf-8

"""The client's outbound side: type a reply the way a person would, and
believe it went out only when the page shows it.

A mixin for the same reason inbound.py is one: every step touches the
thread-bound ``self._page``, and only the client's page thread may call any of
it (``Douyin._run_sends`` is the one caller of ``_send_now``). It relies on the
client's ``_page``, ``_read_roster``, ``_read_panel``, ``_open_conversation``
and ``_notify_sent``.

Two rules shape everything here:

* **Every keystroke is humanized** (``_type_like_a_person``). A fixed 25ms
  interval preceded the 2026-09-15 logout; see humanize.py.
* **Only the page is proof.** An empty composer after Enter is not a send --
  Escape, a stray click or the operator clearing the box empties it just as
  well -- so a line counts as sent only when a new bubble of the account's own
  reading as that line appears on the panel (``_confirm_sent``). This is the
  rule ``Wcf.send_text`` follows by returning spy.dll's own status code,
  applied to the only confirmation a page can give.
"""

import json
import logging

from douyin4u.humanize import (
    sample_inter_line_ms,
    sample_keystroke_ms,
    sample_pre_enter_ms,
    sample_reading_ms,
    sample_thinking_pause_ms,
)
from douyin4u.page_scripts import JS_COMPOSER_STATE
from douyin4u.tracker import alnum_only

logger = logging.getLogger(__name__)

# After Enter, how long to wait before looking a second time for the bubble
# that should now be on the panel. One re-check only: the first look happens
# after _POST_ENTER_SETTLE_MS already, and a bubble that has not rendered by
# then is far more likely to be a send that did not happen than a slow paint.
_SEND_CONFIRM_RETRY_MS = 1200
# How long Douyin is given to clear the composer and paint the new bubble.
_POST_ENTER_SETTLE_MS = 1500

# The page's own test hook for the message box (see client.py's module
# docstring); also written out inside page_scripts.py.
_SEL_COMPOSER = '[data-e2e="msg-input"] [contenteditable="true"]'


def reply_lines(text: str) -> list:
    """The separate messages a reply goes out as: one per non-empty line."""
    return [ln.strip() for ln in (text or "").split("\n") if ln.strip()]


def composer_holds(typed, text: str) -> bool:
    """Did ``text`` actually land in the composer?

    Deliberately not equality: an emoji drops out of ``innerText`` entirely
    (see ``tracker.alnum_only``), which for a fan-facing persona is most
    replies. Compare only the characters the composer is known to keep as
    text, and treat an empty composer as the real failure.
    """
    typed = (typed or "").strip()
    if not typed:
        return False

    expected = alnum_only(text)
    if not expected:
        # Nothing but emoji/punctuation: a non-empty composer is all the
        # confirmation available.
        return True
    return alnum_only(typed) == expected


class OutboundMixin:
    def _send_now(self, sec_uid: str, text: str) -> int:
        """Type and send one reply. Returns how many messages actually went out.

        That count is what a host's quota should be charged for: a multi-line
        reply is several messages as far as Douyin is concerned, not one.
        """
        roster = self._read_roster()
        row = next((r for r in roster if r.get("sec_uid") == sec_uid), None)
        if row is None:
            logger.error(
                f"[Douyin4u] cannot send to {sec_uid}: not in the currently visible "
                "conversation list (searching or scrolling for it is not supported)"
            )
            return 0

        # Reads (roster/panel/page-state, all plain evaluate()) work fine on a
        # background tab, but a real send does not: a click requires the
        # element to be visible in the foreground tab, and fails with "element
        # is not visible" on a tab the operator merely switched away from in
        # the same window -- confirmed live 2026-09-16. Raised unconditionally,
        # once per send, before either branch below: a conversation that is
        # already open still needs this if its tab is not the active one.
        try:
            self._page.bring_to_front()
        except Exception as e:
            logger.warning(f"[Douyin4u] could not bring the tab to the front: {e}")

        opened = False
        if not row.get("is_active"):
            if not self._open_conversation(row["index"], sec_uid):
                logger.error(f"[Douyin4u] send to {sec_uid} aborted: could not open the right conversation")
                return 0
            opened = True

        # A multi-line reply is sent as separate messages, closer to how a
        # person chats than one message with embedded newlines (Enter sends;
        # there is no tested in-composer soft-newline path).
        lines = reply_lines(text)
        if not lines:
            return 0

        # Reading time. Nobody starts typing the instant a conversation opens.
        #
        # Only after a click this client just made. When the conversation was
        # already open -- the usual case, since a fan's message is read where
        # it arrived -- the last thing the page saw was that message, followed
        # by the host's seconds of thinking. The gap this pause creates is
        # already there, and pausing again only made every such reply wait
        # another 0.6-6s on top of it.
        if opened:
            self._page.wait_for_timeout(sample_reading_ms(len(text or "")))

        sent = 0
        for position, line in enumerate(lines):
            if position:
                self._page.wait_for_timeout(sample_inter_line_ms(len(line)))
            if not self._type_and_send_line(line):
                logger.error(
                    f"[Douyin4u] send to {sec_uid} stopped partway through a "
                    f"multi-line reply after a failed line: {line[:40]!r}"
                )
                break
            sent += 1
            # Before the next panel read can see this bubble come back as the
            # account's own message -- see Douyin.add_sent_listener.
            self._notify_sent(sec_uid, line)
        if sent:
            logger.info(f"[Douyin4u] sent {sent} line(s) to {sec_uid}")
        return sent

    def _type_and_send_line(self, text: str) -> bool:
        # Raised per line, not once per reply: _send_now's own call happens
        # before a reading pause of up to 6s and, between lines, gaps of up to
        # 5s. The operator shares this browser, and a tab switched away during
        # either one leaves the click below failing on an invisible element.
        try:
            self._page.bring_to_front()
        except Exception as e:
            logger.warning(f"[Douyin4u] could not bring the tab to the front: {e}")

        composer = self._page.locator(_SEL_COMPOSER)
        try:
            composer.click(timeout=8000)
            self._clear_composer()
        except Exception as e:
            logger.error(f"[Douyin4u] could not focus the composer: {e}")
            self._clear_composer()
            return False

        # Typing goes wherever the caret is. A composer that does not hold it
        # scatters the reply across the page instead of sending it, so this
        # stops before the first keystroke rather than finding out afterwards.
        if not self._composer_state().get("focused"):
            logger.error(
                "[Douyin4u] the composer does not hold the caret after being clicked; "
                "aborting this line rather than typing into whatever does"
            )
            return False

        # Counted before the send so the confirmation below can tell this
        # line's own bubble from an identical one sent earlier in the same
        # conversation -- the text alone cannot.
        expected = alnum_only(text)
        baseline = self._own_bubble_count(expected) if expected else None

        try:
            self._type_like_a_person(text)
        except Exception as e:
            logger.error(f"[Douyin4u] could not type into composer: {e}")
            self._clear_composer()
            return False

        state = self._composer_state()
        if not composer_holds(state.get("text"), text):
            lost_focus = "" if state.get("focused") else " (the composer lost the caret while typing)"
            logger.error(
                f"[Douyin4u] composer content mismatch after typing "
                f"(got {state.get('text')!r}){lost_focus}; aborting this line"
            )
            self._clear_composer()
            return False

        # The beat where a person re-reads what they just wrote.
        self._page.wait_for_timeout(sample_pre_enter_ms())
        try:
            self._page.keyboard.press("Enter")
        except Exception as e:
            logger.error(f"[Douyin4u] Enter key press failed: {e}")
            return False
        self._page.wait_for_timeout(_POST_ENTER_SETTLE_MS)

        if self._composer_text():
            # Enter did not clear the composer, so something blocked the send
            # (length cap, rate limit, content filter). There is nothing else
            # to press: this page has no send button (probed 2026-09-21), and
            # pressing Enter again would only repeat whatever the page refused.
            logger.error(f"[Douyin4u] line was not sent (composer still holds text): {text[:40]!r}")
            self._clear_composer()
            return False

        if not self._confirm_sent(expected, baseline):
            logger.error(
                f"[Douyin4u] the composer emptied but no matching message appeared on the "
                f"panel, so this line did not reach the fan: {text[:40]!r}"
            )
            return False
        return True

    def _own_bubble_count(self, expected: str):
        """How many of the account's own bubbles on the open panel read as
        *expected*. ``None`` when the panel could not be read at all.

        Counted off the raw rows rather than through ``read_panel_messages``:
        a bubble just sent carries no ``serverId`` yet, so the tracker
        deliberately stops before it (see tracker.py) and would never show the
        very message this is trying to find.
        """
        panel = self._read_panel()
        if panel is None:
            return None
        return sum(
            1
            for item in (panel.get("items") or [])
            if item.get("is_me") and alnum_only(item.get("text") or "") == expected
        )

    def _confirm_sent(self, expected: str, baseline) -> bool:
        """Did a new bubble of our own actually appear for this line?

        An empty composer is not proof of a send, and treating it as one is
        how replies a fan never received came to read as delivered
        (2026-09-17). See the module docstring.

        Fails open, loudly, when the panel cannot be read. An unreadable read
        is this client's standing "change nothing" case everywhere else, and
        asserting a negative from it would drop the rest of a reply that may
        well have gone out.
        """
        if not expected or baseline is None:
            # Nothing the panel's own text extraction can match on (an
            # emoji-only line), or no baseline to compare against.
            return True
        for attempt in range(2):
            if attempt:
                self._page.wait_for_timeout(_SEND_CONFIRM_RETRY_MS)
            count = self._own_bubble_count(expected)
            if count is None:
                logger.warning(
                    "[Douyin4u] could not read the panel back to confirm the line was sent; "
                    "treating it as sent"
                )
                return True
            if count > baseline:
                return True
        return False

    def _type_like_a_person(self, text: str):
        """Type *text* one character at a time, with sampled gaps.

        Playwright's own ``delay=`` applies one fixed interval to the entire
        string, and a fixed 25ms interval is exactly the signature that
        preceded the 2026-09-15 logout: perfectly even keystrokes, which the
        page's own input telemetry reports server-side. Driving the loop from
        here is what makes a per-character distribution -- and the occasional
        longer pause once a clause closes -- possible at all.

        The cost is real: a long reply takes seconds to type, and this is the
        same thread that scans for inbound messages, so the client is deaf
        while it types. That matches how a person behaves and is accepted
        deliberately.
        """
        keyboard = self._page.keyboard
        for char in text:
            keyboard.type(char)
            self._page.wait_for_timeout(sample_keystroke_ms())
            pause = sample_thinking_pause_ms(char)
            if pause:
                self._page.wait_for_timeout(pause)

    def _clear_composer(self):
        """Leave the composer empty, before typing and after any failure.

        Typing appends at the cursor, so text a failed line left behind would
        be prepended to the next one; ``composer_holds`` would then reject
        every subsequent line and sending would stay stuck until a human
        emptied the box by hand. Human-paced typing widens the window for a
        mid-line failure from milliseconds to seconds, which is what made this
        worth doing explicitly instead of trusting Enter to clear it.
        """
        try:
            if not self._composer_text():
                return
            self._page.keyboard.press("Control+A")
            self._page.keyboard.press("Backspace")
        except Exception as e:
            logger.warning(f"[Douyin4u] could not clear the composer: {e}")

    def _composer_state(self) -> dict:
        """What the composer holds and whether it still owns the caret.

        A failed read answers "empty, unfocused" rather than raising: every
        caller treats that as "do not send", which is the safe direction.
        """
        try:
            return json.loads(self._page.evaluate(JS_COMPOSER_STATE))
        except Exception:
            return {"present": False, "text": "", "focused": False, "page_focused": False}

    def _composer_text(self) -> str:
        return self._composer_state().get("text") or ""

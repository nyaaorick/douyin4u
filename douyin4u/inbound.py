# encoding:utf-8

"""The client's inbound side: find what is new on the page and hand it out.

A mixin rather than free functions because every step reads the live page
through the client's own thread-bound ``self._page`` (see client.py's module
docstring on threading). It relies on attributes ``Douyin.__init__`` sets --
``_page``, ``_marks``, ``_reopen_after``, ``_ids_unreadable_warned``,
``_behind_since``, ``_msg_queue`` -- and on the client's ``_read_roster``,
``_read_panel``, ``_open_conversation`` and ``_note_roster``.

What counts as *new* is decided in tracker.py, by message id; this module is
the order of operations around it:

1. The conversation already open is drained every tick, and read again
   between ticks the moment it moves (``catch_up_open_conversation``).
2. At most one other conversation is opened per tick: one whose last-message
   time (or, failing that, preview) differs from where it stood when last
   drained. That is saved with the fan's mark, so a restart does not re-open
   every fan -- each re-open used to be one more chance to replay history.
3. Each message past the fan's mark becomes one ``DyMsg`` on the queue
   ``get_msg`` reads, and the mark moves past it. Both directions: the
   account's own messages are handed out too, flagged ``is_self``, because
   only the host knows which of them it sent and which a person typed on the
   real page or app.

What happens to a message after that -- who may be answered, whether it was
already stored, whether it is too old to answer -- is the host's business.
"""

import json
import logging
import time

from douyin4u.dymsg import DyMsg
from douyin4u.page_scripts import JS_SCROLL_TO_NEWEST
from douyin4u.tracker import messages_after, panel_is_behind, read_panel_messages

logger = logging.getLogger(__name__)

# After this client clicks a fan's conversation open, how long before it may
# click that one open again. Set before the click, so neither a click that
# keeps failing nor a conversation whose messages cannot be read turns into a
# click every tick -- a bot pattern of its own.
_REOPEN_COOLDOWN_SECONDS = 60.0

# How long the open conversation may sit scrolled up, away from its newest
# message, before the client scrolls it back itself (see
# _scroll_back_if_left_too_long). Long enough for a person to glance back
# through history in the attached browser; short enough that a fan waits no
# longer than a slow human reply.
_SCROLL_BACK_AFTER_SECONDS = 20.0

# First sight of a fan: read, wait this long, read again, and take the newest
# message as the starting point only if both reads agree on it. A list still
# rendering must not set where a fan's history "begins".
_BASELINE_RECHECK_MS = 800


def _preview_of(row: dict) -> tuple:
    return (row.get("preview_text") or "", bool(row.get("is_from_me")))


def _activity_of(row: dict):
    """When this conversation's newest message arrived, in epoch ms, or None.

    Read from the conversation's own ``createdTime`` (see JS_READ_ROSTER).
    None means the page did not give one, and the caller falls back to
    comparing preview text rather than treating the conversation as quiet.
    """
    value = row.get("created_time_ms")
    return value if isinstance(value, int) and value > 0 else None


def _active_row(roster: list):
    return next(
        (r for r in roster if r.get("is_active") and r.get("sec_uid") and not r.get("is_group")),
        None,
    )


class InboundScanMixin:
    # ------------------------------------------------------------------ scan
    def _scan_inbound(self):
        roster = self._read_roster()
        if not roster:
            return

        # Every roster read the scan already makes also answers
        # get_contacts(), so a host can build a fan catalog without asking the
        # page for anything extra -- there is nothing behind it to ask.
        self._note_roster(roster)

        active = _active_row(roster)
        if active is not None:
            self._drain_and_remember(active, roster)

        # At most one other conversation opened per tick -- a real click,
        # kept rare so this behaves like a person checking their inbox rather
        # than a script hammering the UI.
        candidate = self._pick_candidate(roster, skip_sec_uid=active["sec_uid"] if active else None)
        if candidate is None:
            return
        self._reopen_after[candidate["sec_uid"]] = time.time() + _REOPEN_COOLDOWN_SECONDS
        if self._open_conversation(candidate["index"], candidate["sec_uid"]):
            self._drain_and_remember(candidate, roster)

    def _drain_and_remember(self, row: dict, roster: list):
        """Drain *row*'s open panel; remember where it stood only once that worked.

        Remembering it any earlier -- before the click, as this once did --
        let a failed click or an unreadable panel consume the change, and the
        new message was never looked at again.
        """
        if self._drain_open_panel(row["sec_uid"], roster):
            self._marks.set_preview(row["sec_uid"], _preview_of(row), _activity_of(row))

    def _pick_candidate(self, roster: list, skip_sec_uid) -> dict:
        """Which not-currently-open conversation is worth a real click this
        tick, if any: the first that has moved since it was last drained.

        Direction does not matter. A conversation whose newest message is the
        account's own can be something the operator typed on the real page or
        app, which is only seen if the conversation is opened (live 2026-09-17).
        """
        now = time.time()
        for row in roster:
            sec_uid = row.get("sec_uid")
            if not sec_uid or row.get("is_group") or sec_uid == skip_sec_uid:
                continue
            if self._reopen_after.get(sec_uid, 0.0) > now:
                continue
            if self._changed(sec_uid, row):
                return row
        return None

    def _changed(self, sec_uid: str, row: dict) -> bool:
        """Has anything happened here since it was last drained?

        The signal is the conversation's own last-message timestamp, not its
        preview text. Text cannot answer the question: a fan who sends "在"
        twice, or an operator who types the same reply again, leaves the
        preview identical, and the conversation was then never opened again --
        the message was not late, it was never read at all. The timestamp
        moves on every message (verified live 2026-09-18).

        Falls back to the text comparison when the page gives no timestamp,
        so a front-end change that drops ``createdTime`` degrades to the
        previous behaviour instead of to never opening anything.
        """
        activity = _activity_of(row)
        if activity is None:
            return self._marks.preview(sec_uid) != _preview_of(row)
        last = self._marks.activity(sec_uid)
        if last is None:
            # Never drained, or a mark written before timestamps were read:
            # one open settles it and records the timestamp from then on.
            return self._marks.preview(sec_uid) != _preview_of(row)
        return activity > last

    # ------------------------------------------------------- between ticks
    def catch_up_open_conversation(self) -> bool:
        """Drain the conversation already on screen the moment it moves.

        Called from the idle wait between ticks, far more often than the tick
        itself, because everything it does is a **read**: the roster and the
        panel are plain ``evaluate()`` calls that run inside the browser and
        leave no server-visible trace. Only the click that opens a *different*
        conversation has to stay on the deliberately jittered tick -- measured
        2026-09-18, a message sat 12s between appearing on the page and
        reaching the host, most of it simply waiting for the next tick.

        Opens nothing, and deliberately records nothing: the tick's own scan
        owns the per-fan preview/activity bookkeeping. Writing it here would
        let a read that stopped early -- at a bubble the server has not
        acknowledged yet, which has no id to dedupe on -- mark the
        conversation as seen and skip the message for good.
        """
        roster = self._read_roster()
        if not roster:
            return False
        active = _active_row(roster)
        if active is None or not self._changed(active["sec_uid"], active):
            return False
        return self._drain_open_panel(active["sec_uid"], roster)

    # ----------------------------------------------------------------- drain
    def _drain_open_panel(self, sec_uid: str, roster: list) -> bool:
        """Hand out every message past *sec_uid*'s mark. True when the read
        could be trusted (whether or not anything was new).

        Every way a read can be wrong ends in "do nothing this tick", never in
        moving the mark: the read failed; the panel shows another fan; its
        rows still belong to the previous conversation; or the list is empty
        -- which is a conversation still loading, since no roster row exists
        without a message.
        """
        panel = self._read_panel()
        if panel is None:
            return False
        if (panel.get("active_sec_uid") or "") != sec_uid:
            logger.debug(
                f"[Douyin4u] panel now shows {panel.get('active_sec_uid')!r}, not {sec_uid!r} "
                "(conversation switched mid-read); skipping"
            )
            return False

        read = read_panel_messages(panel.get("items") or [], panel.get("active_short_id") or "")
        if read.foreign:
            logger.debug(f"[Douyin4u] {sec_uid}'s panel still holds another conversation's rows; skipping")
            return False
        if not read.messages:
            if not read.complete:
                self._warn_ids_unreadable(sec_uid)
            return False

        row = next((r for r in roster if r.get("sec_uid") == sec_uid), None) or {}
        behind = panel_is_behind(read.messages, row.get("last_seq"), panel.get("scrolled_px"))
        self._note_behind(sec_uid, behind, row.get("last_seq"), read.messages[-1].seq)
        if behind:
            self._scroll_back_if_left_too_long(sec_uid)

        if self._marks.seq(sec_uid) is None:
            # A baseline taken from an older window would later replay
            # everything between it and the real newest message as new.
            return False if behind else self._baseline(sec_uid, read.messages[-1])

        self._marks.rebase_if_renumbered(sec_uid, read.messages)
        new = messages_after(read.messages, self._marks.seq(sec_uid))
        if new:
            self._hand_out(sec_uid, row, new)
        # What was on screen has been handled; the conversation is still not
        # read up to date, so it is not remembered as such (see panel_is_behind).
        return not behind

    def _note_behind(self, sec_uid: str, behind: bool, last_seq, shown_seq: int):
        """Warn once per episode that a fan's newest messages are off screen,
        and remember when the episode began.

        Nothing is lost while it lasts -- the conversation stays "changed" and
        is read again every pass -- but nothing is handed out either, and from
        the host that looks like the fan being ignored.
        """
        if not behind:
            self._behind_since.pop(sec_uid, None)
            return
        if sec_uid in self._behind_since:
            return
        self._behind_since[sec_uid] = time.time()
        logger.warning(
            f"[Douyin4u] {sec_uid}'s open conversation is scrolled up, away from its newest "
            f"message (#{last_seq}; #{shown_seq} is the newest on screen). Nothing newer is "
            f"read until it is back at the bottom; it is scrolled back in "
            f"{int(_SCROLL_BACK_AFTER_SECONDS)}s if nobody does it first"
        )

    def _scroll_back_if_left_too_long(self, sec_uid: str):
        """Scroll the open conversation to its newest message once it has sat
        scrolled up for ``_SCROLL_BACK_AFTER_SECONDS``.

        The wait is for a person glancing back through history in the
        attached browser; past it, the need to read wins. A new episode starts
        after the scroll, so a person who keeps reading is interrupted at most
        once per wait, never on every pass.
        """
        since = self._behind_since.get(sec_uid)
        if since is None or time.time() - since < _SCROLL_BACK_AFTER_SECONDS:
            return
        self._behind_since.pop(sec_uid, None)
        try:
            result = json.loads(self._page.evaluate(JS_SCROLL_TO_NEWEST))
        except Exception as e:
            logger.warning(f"[Douyin4u] could not scroll {sec_uid}'s conversation back to its newest message: {e}")
            return
        if result.get("scrolled_back"):
            logger.info(
                f"[Douyin4u] scrolled {sec_uid}'s conversation back to its newest message "
                f"(it sat {result.get('before')}px up for over {int(_SCROLL_BACK_AFTER_SECONDS)}s)"
            )
        else:
            logger.warning(f"[Douyin4u] could not find {sec_uid}'s message list to scroll back")

    def _baseline(self, sec_uid: str, newest) -> bool:
        """First sight of a fan, ever: start after its newest message.

        Handing out the backlog of a fan never seen before would have a host
        answer history as if it were new, so nothing already on screen is
        handed out. Confirmed by a second read first, so a list that was still
        filling in cannot set the starting point too early and have the rest
        of it replayed as new.
        """
        self._page.wait_for_timeout(_BASELINE_RECHECK_MS)
        again = self._read_panel()
        if again is None or (again.get("active_sec_uid") or "") != sec_uid:
            return False
        reread = read_panel_messages(again.get("items") or [], again.get("active_short_id") or "")
        if reread.foreign or not reread.messages or reread.messages[-1].seq != newest.seq:
            logger.debug(f"[Douyin4u] {sec_uid}'s panel is still changing; baseline next tick")
            return False
        self._marks.advance(sec_uid, newest)
        logger.info(
            f"[Douyin4u] first sight of {sec_uid}: starting after its message #{newest.seq}; "
            "nothing already on screen is handed out"
        )
        return True

    def _warn_ids_unreadable(self, sec_uid: str):
        if sec_uid in self._ids_unreadable_warned:
            return
        self._ids_unreadable_warned.add(sec_uid)
        logger.warning(
            f"[Douyin4u] could not read message ids in {sec_uid}'s open conversation; nothing "
            "from it is handed out until they can be read. If this persists, Douyin changed "
            "the page and page_scripts.py's JS_READ_PANEL needs updating"
        )

    def _hand_out(self, sec_uid: str, row: dict, new: list):
        """Queue each new message for ``get_msg`` and move the mark past it.

        Per message, in order, so a mark never runs ahead of what was queued:
        whatever the host then makes of a message, it is handed out once.
        """
        nickname = row.get("nickname") or ""
        is_stranger = bool(row.get("is_stranger"))
        for message in new:
            self._msg_queue.put(DyMsg(
                id=message.server_id,
                seq=message.seq,
                type=message.kind,
                content=message.text,
                peer=sec_uid,
                peer_nickname=nickname,
                is_self=message.is_me,
                is_stranger=is_stranger,
                created_at_ms=message.created_at_ms,
                self_only=message.self_only,
                callback_code=message.callback_code,
            ))
            self._marks.advance(sec_uid, message)

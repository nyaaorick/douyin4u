# encoding:utf-8

"""Which Douyin messages are new: decided by message id, never by position.

WCF never had this problem. Its hook pushes each message once, with an id
(``wcf_msg.id``), and ``WcfChannel._is_duplicate`` drops a repeat. The Douyin
channel instead polls a React list that re-renders, and until 2026-09-17 it
remembered "how many rows this fan's panel had" and treated anything past
that count as new. The list broke that assumption four separate ways, all
seen live the same day:

1. **Right after a conversation switch the list is empty while it loads.** A
   read in that moment dropped the count to 0, and the next read replayed the
   entire rendered window (seq 59-78 at 06:52:42) -- old fan messages were
   answered a second time on the real account, and the account's own old
   replies were filed as messages typed by hand.
2. **Right after a switch the list can still hold the previous fan's rows**,
   while the roster highlight has already moved on.
3. **Scrolling up loads older history at the top**, shifting every position.
4. **Every restart, and every moment the page was not "ready", forgot every
   count**, so each fan was re-baselined -- silently skipping whatever had
   arrived meanwhile.

The page does carry what WCF has: each row's React props hold the real
message object, with a ``serverId``, a server-assigned per-conversation
sequence (``indexInConversationV2``), ``createdAt``, and the conversation's
``conversationShortId`` (found by a read-only probe; see page_scripts.py's
JS_READ_PANEL). So the rules here are:

- A message belongs to the open fan only if its own conversation id says so;
  one foreign row voids the whole read (``read_panel_messages``).
- A message is new only if its sequence is past the fan's mark
  (``messages_after``). An empty or partial read has nothing to lower the
  mark with, and scrolled-in history is below it.
- The mark is written to disk (``InboundMarks``), so a restart resumes where
  it stopped instead of re-baselining. Where on disk is the host's choice
  (``Douyin(marks_path=...)``); without one the marks live in memory only.
- A just-sent bubble has no ids yet; the read stops before it rather than
  letting the mark jump past it.

Everything here is pure and fails closed toward *not* processing: an
unreadable read is skipped, an unreadable marks file means "no marks" (the
next open baselines -- a silent gap, never a replay).
"""

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from typing import List, NamedTuple, Optional

logger = logging.getLogger(__name__)

_MARKS_VERSION = 1
_KIND_DIVIDER = "divider"
# How far from its newest end the message list may sit and still count as
# showing it: a sub-pixel scroll offset is not the operator scrolling away.
_NEWEST_EDGE_PX = 4


def alnum_only(value: str) -> str:
    """Keep only the characters Douyin's composer and panel reliably render
    as text. CJK counts as alphanumeric to ``str.isalnum``, so Chinese is kept
    in full; an emoji can render as an ``<img>`` and drop out of ``innerText``
    (the composer does this), so it is dropped on both sides of a comparison.
    Used wherever typed, sent, stored and on-screen text have to be matched:
    the composer check, the echo filter, and "is this own message a reply
    already in history".
    """
    return "".join(ch for ch in (value or "") if ch.isalnum())


@dataclass(frozen=True)
class PanelMessage:
    """One message on the open panel, identified the way Douyin identifies it.

    ``self_only``: Douyin stored it as visible to its sender alone, so the
    other side never received it (see JS_READ_PANEL); ``callback_code`` is
    the status Douyin recorded with it. Both only mean something on the
    account's own messages.
    """

    server_id: str
    seq: int
    created_at_ms: Optional[int]
    is_me: bool
    kind: str
    text: str
    self_only: bool = False
    callback_code: str = ""


class PanelRead(NamedTuple):
    """The messages a panel read could vouch for.

    ``foreign``: a row belonged to another conversation -- use nothing.
    ``complete``: False when the read stopped early at a row without ids (a
    bubble not yet acknowledged by the server, or a page shape this code no
    longer recognises); the messages before that row are still good.
    """

    messages: List[PanelMessage]
    complete: bool
    foreign: bool


def _parse_seq(value) -> Optional[int]:
    try:
        seq = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return seq if seq >= 0 else None


def _parse_ms(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def read_panel_messages(items: list, active_short_id: str) -> PanelRead:
    """Turn JS_READ_PANEL's rows into this conversation's messages, in order.

    Divider rows are skipped (they sit on the same message object as the
    bubble they head, so their ids would only duplicate it); a message whose
    server id already appeared is kept once.
    """
    if not active_short_id:
        return PanelRead([], complete=False, foreign=True)

    messages = []
    seen = set()
    complete = True
    for item in items or []:
        short_id = str(item.get("short_id") or "")
        if short_id and short_id != active_short_id:
            return PanelRead([], complete=False, foreign=True)
        if item.get("kind") == _KIND_DIVIDER:
            continue
        server_id = str(item.get("server_id") or "")
        seq = _parse_seq(item.get("seq"))
        if not short_id or not server_id or server_id == "0" or seq is None:
            complete = False
            break
        if server_id in seen:
            continue
        seen.add(server_id)
        messages.append(PanelMessage(
            server_id=server_id,
            seq=seq,
            created_at_ms=_parse_ms(item.get("created_at_ms")),
            is_me=bool(item.get("is_me")),
            kind=str(item.get("kind") or ""),
            text=str(item.get("text") or ""),
            self_only=bool(item.get("self_only")),
            callback_code=str(item.get("callback_code") or ""),
        ))
    messages.sort(key=lambda m: m.seq)
    return PanelRead(messages, complete=complete, foreign=False)


def messages_after(messages: List[PanelMessage], mark_seq: int) -> List[PanelMessage]:
    """The messages strictly past a fan's mark, oldest first."""
    return [m for m in messages if m.seq > mark_seq]


def panel_is_behind(messages: List[PanelMessage], last_seq, scrolled_px) -> bool:
    """Is the panel scrolled back to an older window that leaves out the
    conversation's newest message?

    The message list is virtualized: scrolled up, it renders only the older
    window on screen, and the newest messages are not on the page at all. A
    read of that window finds nothing past the mark, and without this check
    the drain records the conversation as up to date anyway -- after which
    ``InboundScanMixin._changed`` never looks at those messages again.

    Both signals are required, so neither alone can wedge a conversation:
    scrolled away but with the newest message still rendered is fine, and so
    is a newest message that never renders as a bubble while the list sits at
    its newest end (a freshly clicked conversation always does). An unknown
    sequence or scroll position reads as "not behind", as before this check.
    """
    newest = _parse_seq(last_seq) if last_seq not in (None, "") else None
    if newest is None or not isinstance(scrolled_px, (int, float)) or scrolled_px <= _NEWEST_EDGE_PX:
        return False
    shown = max((m.seq for m in messages), default=None)
    return shown is None or shown < newest


class InboundMarks:
    """Per-fan progress: the last message handled, and the last roster preview.

    Owned by the channel's loop thread, like everything else that touches the
    page, so it takes no lock. ``path=None`` keeps it in memory only, which
    is what every test (and a channel that has not started) uses -- nothing
    then ever touches the operator's real file.

    The preview rides along so a restart does not have to open every fan to
    find out whether anything changed while it was down; only a fan whose
    roster preview differs from the saved one is opened.
    """

    def __init__(self, path: str = None):
        self.path = path
        self._fans = {}
        if path:
            self._load()

    # -------------------------------------------------------------- reads
    def seq(self, sec_uid: str) -> Optional[int]:
        entry = self._fans.get(sec_uid)
        return entry["seq"] if entry else None

    def preview(self, sec_uid: str):
        entry = self._fans.get(sec_uid)
        return entry.get("preview") if entry else None

    def activity(self, sec_uid: str):
        """The conversation's newest-message time, in epoch ms, as of the last
        successful drain. None for a fan never drained, or for a marks file
        written before this was recorded."""
        entry = self._fans.get(sec_uid)
        return entry.get("activity") if entry else None

    # ------------------------------------------------------------- writes
    def advance(self, sec_uid: str, message: PanelMessage) -> None:
        """Move the mark to *message*, never backwards."""
        current = self.seq(sec_uid)
        if current is not None and message.seq <= current:
            return
        entry = dict(self._fans.get(sec_uid) or {})
        entry.update(seq=message.seq, server_id=message.server_id)
        self._fans = {**self._fans, sec_uid: entry}
        self._save()

    def rebase_if_renumbered(self, sec_uid: str, messages: List[PanelMessage]) -> bool:
        """Follow the mark's own message if the page now shows it at another seq.

        Never observed -- the sequence is server-assigned -- but if it ever
        happened, comparing against a stale number would silently swallow or
        replay everything between the two. Returns True when the mark moved.
        """
        entry = self._fans.get(sec_uid)
        if not entry or not entry.get("server_id"):
            return False
        anchor = next((m for m in messages if m.server_id == entry["server_id"]), None)
        if anchor is None or anchor.seq == entry["seq"]:
            return False
        logger.warning(
            f"[DouyinInbound] {sec_uid}: the marked message moved from seq {entry['seq']} "
            f"to {anchor.seq}; following it"
        )
        self._fans = {**self._fans, sec_uid: {**entry, "seq": anchor.seq}}
        self._save()
        return True

    def set_preview(self, sec_uid: str, preview, activity=None) -> None:
        """Record where this conversation stood at the last successful drain.

        ``activity`` is its newest message's time in epoch ms, which is what
        ``InboundScanMixin._changed`` compares on; the preview rides along as
        the fallback for a page that stops reporting a timestamp.
        """
        preview = [str(preview[0] or ""), bool(preview[1])]
        entry = self._fans.get(sec_uid)
        if (entry is not None and entry.get("preview") == tuple(preview)
                and entry.get("activity") == activity):
            return
        if entry is None:
            # A preview alone, before any message has been handled, still
            # has to be remembered -- without a mark, seq() stays None and
            # the next open baselines as usual.
            entry = {"seq": None, "server_id": ""}
        updated = {**entry, "preview": tuple(preview)}
        if activity is not None:
            updated["activity"] = activity
        self._fans = {**self._fans, sec_uid: updated}
        self._save()

    # -------------------------------------------------------- persistence
    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.error(f"[DouyinInbound] unreadable marks file, every fan will be re-baselined: {e}")
            return
        raw = data.get("fans") if isinstance(data, dict) else None
        if not isinstance(raw, dict):
            logger.error("[DouyinInbound] marks file has no fans map, every fan will be re-baselined")
            return
        fans = {}
        for sec_uid, entry in raw.items():
            if not isinstance(sec_uid, str) or not isinstance(entry, dict):
                continue
            seq = _parse_seq(entry.get("seq")) if entry.get("seq") is not None else None
            if entry.get("seq") is not None and seq is None:
                continue
            preview = entry.get("preview")
            activity = entry.get("activity")
            fans[sec_uid] = {
                "seq": seq,
                "server_id": str(entry.get("server_id") or ""),
                **({"preview": (str(preview[0]), bool(preview[1]))}
                   if isinstance(preview, list) and len(preview) == 2 else {}),
                # Absent in a file written before timestamps were recorded;
                # the fan is simply compared on its preview until the next
                # drain writes one.
                **({"activity": activity} if isinstance(activity, int) else {}),
            }
        self._fans = fans
        logger.info(f"[DouyinInbound] loaded marks for {len(fans)} fans")

    def _save(self) -> None:
        """Atomic, the same way ContactState.save is: a torn file would read
        as "no marks", which is safe, but still a silent gap for every fan."""
        if not self.path:
            return
        directory = os.path.dirname(self.path) or "."
        tmp_path = None
        try:
            os.makedirs(directory, exist_ok=True)
            payload = {
                "version": _MARKS_VERSION,
                "fans": {
                    sec_uid: {**entry, **({"preview": list(entry["preview"])} if "preview" in entry else {})}
                    for sec_uid, entry in self._fans.items()
                },
            }
            fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".inbound_marks-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self.path)
            tmp_path = None
        except Exception as e:
            logger.error(f"[DouyinInbound] failed to save marks: {e}")
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

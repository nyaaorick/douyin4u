# encoding:utf-8

"""One Douyin private message, shaped after ``wcferry.WxMsg``.

A host that already speaks WeChatFerry reads this with the code it has:
``id``, ``type``, ``sender``, ``content`` and ``ts`` mean what they mean on a
``WxMsg``, and ``from_self()`` / ``from_group()`` / ``is_text()`` answer the
same questions. That is the whole point of the shape -- one inbound pipeline
on the host side, whichever of the two platforms the message came from.

Where the two platforms genuinely differ, this says more rather than less:

* **The conversation is always known.** A self-sent private ``WxMsg`` names
  the account's own wxid as sender and carries no peer at all (CowAgent has to
  recover it from MSG0.db). Every ``DyMsg`` is read off an open conversation,
  so ``peer`` is the fan's ``secUid`` in both directions.
* **``type`` is a word, not a number.** The page classifies each bubble in the
  browser (``text``, ``image``, ``video_share``, ...); there is no numeric
  type table behind it to mirror.
* **Delivery can silently fail.** ``self_only`` marks one of the account's own
  messages that Douyin stored as visible to the sender alone -- the fan never
  received it (see ``page_scripts.JS_READ_PANEL``).
"""

from dataclasses import dataclass
from typing import Optional

TYPE_TEXT = "text"


@dataclass(frozen=True)
class DyMsg:
    """A message past its conversation's mark -- handed out exactly once.

    ``id`` is Douyin's own ``serverId``; ``seq`` its server-assigned position
    in the conversation, which is what "new" is decided on (see tracker.py).
    ``ts`` is the send time in epoch seconds, 0 when the page did not give
    one; ``created_at_ms`` keeps the same moment at the resolution the page
    reports it, or None.
    """

    id: str
    seq: int
    type: str
    content: str
    peer: str
    peer_nickname: str = ""
    is_self: bool = False
    is_stranger: bool = False
    created_at_ms: Optional[int] = None
    self_only: bool = False
    callback_code: str = ""

    @property
    def sender(self) -> str:
        """Who wrote it: the fan, or ``""`` for the account itself.

        ``""`` rather than a guessed own id: the page never names the account's
        own uid, and a host must not mistake a placeholder for a real id.
        """
        return "" if self.is_self else self.peer

    @property
    def roomid(self) -> str:
        """Always ``""``: group conversations are never opened (see client.py)."""
        return ""

    @property
    def ts(self) -> int:
        return int(self.created_at_ms / 1000) if self.created_at_ms else 0

    def from_self(self) -> bool:
        return self.is_self

    def from_group(self) -> bool:
        return False

    def is_text(self) -> bool:
        return self.type == TYPE_TEXT

    def __str__(self) -> str:
        who = "self" if self.is_self else self.peer
        return f"{who}->{self.peer}|{self.id}|#{self.seq}|{self.type}\n{self.content}"

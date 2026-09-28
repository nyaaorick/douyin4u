# encoding:utf-8

"""douyin4u -- Douyin private messages behind a ``wcferry``-shaped client.

    from douyin4u import Douyin

    dy = Douyin("http://127.0.0.1:9222", marks_path="inbound_marks.json")
    if dy.start():
        dy.enable_receiving_msg()
        msg = dy.get_msg()                  # a DyMsg, shaped like wcferry's WxMsg
        dy.send_text("收到", msg.peer)       # SendResult(status=0, sent=1, total=1)

See client.py for what the client does and why, and docs/RPA_NOTES.md for the
page research behind it.
"""

from douyin4u.browser import CHAT_URL, DEFAULT_CDP_URL
from douyin4u.client import (
    PAGE_CHALLENGE,
    PAGE_CHAT_NOT_RENDERED,
    PAGE_LOADING,
    PAGE_LOGGED_OUT,
    PAGE_READY,
    PAGE_STATES,
    PAGE_UNREADABLE,
    PAGE_WRONG_PAGE,
    STATUS_EMPTY,
    STATUS_FAILED,
    STATUS_NOT_RUNNING,
    STATUS_OK,
    STATUS_PARTIAL,
    STATUS_TIMEOUT,
    Douyin,
    SendResult,
)
from douyin4u.dymsg import TYPE_TEXT, DyMsg
from douyin4u.humanize import SendBudget, in_quiet_hours, parse_quiet_hours
from douyin4u.outbound import reply_lines
from douyin4u.tracker import alnum_only

__version__ = "0.1.0"

__all__ = [
    "CHAT_URL", "DEFAULT_CDP_URL",
    "Douyin", "DyMsg", "SendResult", "TYPE_TEXT",
    "PAGE_STATES", "PAGE_READY", "PAGE_LOGGED_OUT", "PAGE_CHALLENGE", "PAGE_WRONG_PAGE",
    "PAGE_LOADING", "PAGE_CHAT_NOT_RENDERED", "PAGE_UNREADABLE",
    "STATUS_OK", "STATUS_PARTIAL", "STATUS_FAILED", "STATUS_NOT_RUNNING", "STATUS_TIMEOUT",
    "STATUS_EMPTY",
    "SendBudget", "parse_quiet_hours", "in_quiet_hours",
    "alnum_only", "reply_lines",
]

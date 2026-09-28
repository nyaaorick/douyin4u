# encoding:utf-8

"""Telling a new Douyin message from an old one, by message id.

See douyin4u/tracker.py's module docstring for the incidents
behind every rule tested here. Pure logic: no page, no channel, no store.
"""

import json
import os
import sys

import pytest


from douyin4u.tracker import (
    InboundMarks,
    PanelMessage,
    messages_after,
    panel_is_behind,
    read_panel_messages,
)

SHORT = "7000000000000000001"  # synthetic conversationShortId


def _row(seq, kind="text", is_me=False, text="t", short_id=SHORT, server_id=None, created=None):
    return {
        "kind": kind,
        "is_me": is_me,
        "text": text,
        "server_id": server_id if server_id is not None else f"sid{seq}",
        "seq": str(seq),
        "created_at_ms": created if created is not None else 1_000_000 + seq,
        "short_id": short_id,
    }


def _bubble(seq, **kw):
    """A message as the page renders it: a divider row, then the bubble,
    both sitting on the same message object."""
    return [_row(seq, kind="divider", text=""), _row(seq, **kw)]


# ------------------------------------------------------------ read_panel_messages
def test_a_divider_and_its_bubble_become_one_message():
    read = read_panel_messages(_bubble(59, text="hi") + _bubble(60, text="yo"), SHORT)

    assert [m.seq for m in read.messages] == [59, 60]
    assert [m.text for m in read.messages] == ["hi", "yo"]
    assert read.complete is True
    assert read.foreign is False


def test_rows_left_over_from_another_conversation_make_the_whole_read_foreign():
    """Right after a switch the roster highlight has moved but the list can
    still hold the previous fan's rows. Filing those under this fan -- or
    advancing this fan's mark past them -- is exactly what must not happen."""
    rows = _bubble(59) + _bubble(3, short_id="someone_else")

    read = read_panel_messages(rows, SHORT)

    assert read.foreign is True
    assert read.messages == []


def test_no_active_conversation_id_reads_as_foreign():
    assert read_panel_messages(_bubble(59), "").foreign is True


def test_an_unacknowledged_bubble_stops_the_read_there():
    """A bubble that has just been sent has no server id or sequence yet.
    Nothing after it is trusted this tick: taking a later message now would
    move the mark past the pending one, and it would never be seen again once
    its id arrives."""
    rows = _bubble(59) + [_row(0, server_id="0", is_me=True)] + _bubble(61)

    read = read_panel_messages(rows, SHORT)

    assert [m.seq for m in read.messages] == [59]
    assert read.complete is False


def test_a_row_without_readable_ids_stops_the_read_too():
    rows = _bubble(59) + [dict(_row(60), seq="")]

    read = read_panel_messages(rows, SHORT)

    assert [m.seq for m in read.messages] == [59]
    assert read.complete is False


def test_an_empty_list_is_complete_and_empty():
    read = read_panel_messages([], SHORT)
    assert read.messages == [] and read.complete is True and read.foreign is False


def _read(*seqs):
    return read_panel_messages([row for seq in seqs for row in _bubble(seq)], SHORT).messages


def test_a_panel_scrolled_up_past_the_newest_message_is_behind():
    """The list renders only the window on screen: scrolled up, the newest
    messages are not on the page, and reading it proves nothing about them."""
    assert panel_is_behind(_read(33, 34, 63), "86", 1400) is True


def test_a_panel_at_its_newest_end_is_never_behind():
    """A newest message that never renders as a bubble must not wedge a
    conversation the channel just clicked open (it always opens at the end)."""
    assert panel_is_behind(_read(33, 63), "86", 0) is False


def test_a_panel_scrolled_up_that_still_renders_the_newest_message_is_not_behind():
    assert panel_is_behind(_read(84, 85, 86), "86", 200) is False


@pytest.mark.parametrize("last_seq, scrolled_px", [("", 1400), (None, 1400), ("86", None), ("x", 1400)])
def test_an_unknown_newest_message_or_scroll_position_is_not_behind(last_seq, scrolled_px):
    """What the channel did before this check existed -- not a new way to stall."""
    assert panel_is_behind(_read(33, 63), last_seq, scrolled_px) is False


def test_a_message_douyin_kept_to_its_sender_says_so():
    """The only sign a message never reached the other side (2026-09-21)."""
    rows = _bubble(59, is_me=True) + [dict(_row(60, is_me=True), self_only=True, callback_code="8101")]

    read = read_panel_messages(rows, SHORT)

    assert [(m.seq, m.self_only, m.callback_code) for m in read.messages] == [
        (59, False, ""), (60, True, "8101"),
    ]


def test_messages_come_back_in_sequence_order_whatever_the_row_order():
    read = read_panel_messages(_bubble(61) + _bubble(59) + _bubble(60), SHORT)
    assert [m.seq for m in read.messages] == [59, 60, 61]


def test_a_non_text_bubble_is_kept_with_its_kind():
    read = read_panel_messages(_bubble(59, kind="image", text=""), SHORT)
    assert read.messages[0].kind == "image"


# ------------------------------------------------------------------ messages_after
def _msgs(*seqs):
    return [PanelMessage(server_id=f"sid{s}", seq=s, created_at_ms=s, is_me=False, kind="text", text="t")
            for s in seqs]


def test_only_messages_past_the_mark_are_new():
    assert [m.seq for m in messages_after(_msgs(59, 60, 61), 60)] == [61]


def test_the_2026_09_17_replay_cannot_happen():
    """06:52:42: a read of a still-loading (empty) list dropped the old
    position cursor to 0, and the next read replayed seq 59-78 -- the whole
    rendered window -- as brand-new messages. A mark is a sequence number, so
    an empty read has nothing to lower it with, and the full window after it
    is all at or below the mark."""
    mark = 78
    assert messages_after(_msgs(), mark) == []
    assert messages_after(_msgs(*range(59, 79)), mark) == []


def test_older_history_loaded_by_scrolling_up_is_never_new():
    assert messages_after(_msgs(*range(20, 79)), 78) == []


# ------------------------------------------------------------------- InboundMarks
def _message(seq, server_id=None):
    return PanelMessage(server_id=server_id or f"sid{seq}", seq=seq, created_at_ms=seq, is_me=False,
                        kind="text", text="t")


def test_a_fan_never_seen_has_no_mark():
    assert InboundMarks().seq("fan1") is None


def test_a_mark_only_moves_forward():
    marks = InboundMarks()
    marks.advance("fan1", _message(79))
    marks.advance("fan1", _message(60))

    assert marks.seq("fan1") == 79


def test_marks_survive_a_restart(tmp_path):
    path = str(tmp_path / "douyin" / "inbound_marks.json")
    marks = InboundMarks(path)
    marks.advance("fan1", _message(79))
    marks.set_preview("fan1", ("在吗", False))

    reloaded = InboundMarks(path)

    assert reloaded.seq("fan1") == 79
    assert reloaded.preview("fan1") == ("在吗", False)


def test_a_memory_only_tracker_never_writes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    marks = InboundMarks()
    marks.advance("fan1", _message(79))
    assert os.listdir(tmp_path) == []


def test_a_malformed_marks_file_starts_empty_instead_of_crashing(tmp_path):
    """No marks means every fan is baselined on its next open -- a silent gap,
    never a replay -- which is the safe direction to fail in."""
    path = tmp_path / "inbound_marks.json"
    path.write_text("{not json", encoding="utf-8")

    assert InboundMarks(str(path)).seq("fan1") is None


def test_a_marks_file_with_garbage_entries_keeps_the_good_ones(tmp_path):
    path = tmp_path / "inbound_marks.json"
    path.write_text(json.dumps({"version": 1, "fans": {
        "good": {"seq": 5, "server_id": "s5", "preview": ["x", True]},
        "bad": {"seq": "not a number"},
        "worse": "nope",
    }}), encoding="utf-8")

    marks = InboundMarks(str(path))

    assert marks.seq("good") == 5
    assert marks.seq("bad") is None
    assert marks.seq("worse") is None


def test_a_mark_is_rebased_when_its_own_message_shows_a_different_sequence():
    """Defence against the page ever renumbering a conversation: the message
    the mark was taken from is found by its server id, and the mark follows
    it instead of silently swallowing or replaying everything in between."""
    marks = InboundMarks()
    marks.advance("fan1", _message(79, server_id="anchor"))

    rebased = marks.rebase_if_renumbered("fan1", [_message(80, server_id="x"), _message(12, server_id="anchor")])

    assert rebased is True
    assert marks.seq("fan1") == 12


def test_a_mark_whose_message_is_not_on_screen_is_left_alone():
    marks = InboundMarks()
    marks.advance("fan1", _message(79, server_id="anchor"))

    assert marks.rebase_if_renumbered("fan1", [_message(80)]) is False
    assert marks.seq("fan1") == 79


def test_a_preview_write_that_changes_nothing_does_not_touch_the_disk(tmp_path):
    path = tmp_path / "inbound_marks.json"
    marks = InboundMarks(str(path))
    marks.set_preview("fan1", ("hi", False))
    before = path.stat().st_mtime_ns

    marks.set_preview("fan1", ("hi", False))

    assert path.stat().st_mtime_ns == before

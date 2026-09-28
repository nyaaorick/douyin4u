# encoding:utf-8

"""What the client hands out, and when -- the inbound half of Douyin.

Every test here pins an incident from CowAgent's Douyin channel, which owned
this logic before it moved into douyin4u; the dates say which. What used to be
asserted as "produced to the agent" is now "handed out by get_msg", because
who gets answered is the host's decision, not the client's.
"""

import logging
import time

import pytest

from douyin4u.inbound import _SCROLL_BACK_AFTER_SECONDS
from douyin4u.tracker import InboundMarks
from fakes import FakePage, drain, drained, msg_rows, now_ms, panel, row, tracked


# ------------------------------------------------------------ first sight
def test_first_sight_of_a_fan_starts_after_the_newest_message_and_hands_out_nothing(client):
    """No backlog replay for a fan never seen before."""
    client._page = FakePage(panel=panel(msg_rows(59, "history 1"), msg_rows(60, "history 2")))

    out, trusted = drain(client)

    assert trusted is True
    assert out == []
    assert client._marks.seq("fan1") == 60


def test_first_sight_waits_while_the_list_is_still_filling_in(client):
    """Two reads must agree on the newest message before it becomes the
    starting point; otherwise the rest of a list still rendering would be
    replayed as new on the next read."""
    client._page = FakePage(panels=[panel(msg_rows(59)), panel(msg_rows(59), msg_rows(60))])

    out, trusted = drain(client)

    assert trusted is False
    assert out == []
    assert client._marks.seq("fan1") is None


def test_first_sight_never_starts_from_an_empty_list(client):
    """No roster row exists without a message, so an empty list is a
    conversation still loading."""
    client._page = FakePage(panel=panel())

    _, trusted = drain(client)

    assert trusted is False
    assert client._marks.seq("fan1") is None


def test_a_fan_first_seen_through_a_scrolled_up_panel_is_not_baselined_there(client):
    """A baseline at the newest message on screen would later replay every
    message between it and the real newest one as new."""
    client._page = FakePage(panel=dict(panel(msg_rows(63)), scrolled_px=1400))

    assert client._drain_open_panel("fan1", [row("fan1", last_seq="86")]) is False
    assert client._marks.seq("fan1") is None


# ------------------------------------------------------------- past the mark
def test_a_message_past_the_mark_is_handed_out_exactly_once(client):
    client._marks.advance("fan1", tracked(60))
    client._page = FakePage(panel=panel(msg_rows(59), msg_rows(60), msg_rows(61, "新消息")))

    first, _ = drain(client)
    second, _ = drain(client)

    assert [m.content for m in first] == ["新消息"]
    assert second == []
    assert client._marks.seq("fan1") == 61


def test_a_handed_out_message_reads_like_a_wxmsg(client):
    """The host's one inbound pipeline reads id/type/sender/content/ts, and
    from_self()/from_group()/is_text(), on either platform's message."""
    client._marks.advance("fan1", tracked(60))
    client._page = FakePage(panel=panel(msg_rows(61, "在吗", created_at_ms=1789653186726)))

    (msg,), _ = drain(client)

    assert (msg.id, msg.seq, msg.type, msg.content) == ("sid61", 61, "text", "在吗")
    assert (msg.sender, msg.peer, msg.roomid) == ("fan1", "fan1", "")
    assert msg.ts == 1789653186
    assert (msg.from_self(), msg.from_group(), msg.is_text()) == (False, False, True)


def test_the_2026_09_17_empty_read_replay_cannot_happen(client):
    """06:52:42 live: a read caught the list empty while it reloaded, the old
    position cursor dropped to 0, and the next read replayed seq 59-78 -- the
    whole window -- answering old messages again on the real account and
    filing old replies as typed by hand."""
    client._marks.advance("fan1", tracked(78))
    window = panel(*[msg_rows(seq, f"old {seq}", is_me=seq % 2 == 0) for seq in range(59, 79)])
    client._page = FakePage(panels=[panel(), window])

    loading, _ = drain(client)
    loaded, _ = drain(client)

    assert loading == [] and loaded == []
    assert client._marks.seq("fan1") == 78


def test_rows_still_belonging_to_the_previous_conversation_are_ignored(client):
    """Right after a switch the roster highlight can already say fan1 while
    the list still shows someone else's messages."""
    client._marks.advance("fan1", tracked(10))
    client._page = FakePage(panel=panel(msg_rows(11, "别人的消息", short_id="someone_else")))

    out, trusted = drain(client)

    assert trusted is False
    assert out == []
    assert client._marks.seq("fan1") == 10


def test_older_history_loaded_by_scrolling_up_is_not_new(client):
    client._marks.advance("fan1", tracked(78))
    client._page = FakePage(panel=panel(*[msg_rows(seq) for seq in range(20, 79)]))

    out, _ = drain(client)

    assert out == []


def test_two_identical_messages_in_a_row_are_both_handed_out(client):
    client._marks.advance("fan1", tracked(60))
    client._page = FakePage(panel=panel(msg_rows(61, "dd"), msg_rows(62, "dd")))

    out, _ = drain(client)

    assert [m.content for m in out] == ["dd", "dd"]


def test_rows_are_dropped_when_the_panel_switched_conversation_mid_read(client):
    """The operator shares this browser and can click away at any moment.
    Rows read after such a switch belong to a different fan and must never be
    filed under the expected fan's id."""
    client._marks.advance("fan1", tracked(10))
    client._page = FakePage(active_sec_uid="fan2", panel=panel(msg_rows(11, "另一个人的消息")))

    out, trusted = drain(client)

    assert trusted is False
    assert out == []
    assert client._marks.seq("fan1") == 10


def test_a_failed_panel_read_leaves_the_mark_alone(client):
    client._marks.advance("fan1", tracked(5))
    client._page = FakePage(panel_raises=True)

    out, trusted = drain(client)

    assert trusted is False
    assert out == []
    assert client._marks.seq("fan1") == 5


def test_the_accounts_own_messages_are_handed_out_flagged_as_self(client):
    """Only the host knows which of them it sent and which a person typed on
    the real page -- so the client hands both out and says which side wrote it."""
    client._marks.advance("fan1", tracked(0))
    client._page = FakePage(panel=panel(msg_rows(1, "我来接一下", is_me=True)))

    (msg,), _ = drain(client)

    assert msg.from_self() is True
    assert msg.sender == ""
    assert msg.peer == "fan1"


def test_an_own_message_douyin_kept_to_this_account_says_so(client):
    """2026-09-21: every message sent from the creator-center chat was stored
    as visible to its sender only, and nothing on this side said otherwise."""
    client._marks.advance("fan1", tracked(0))
    rows = [dict(r, self_only=True, callback_code="8101") for r in msg_rows(1, "在的", is_me=True)]
    client._page = FakePage(panel=panel(rows))

    (msg,), _ = drain(client)

    assert (msg.self_only, msg.callback_code) == (True, "8101")


def test_a_non_text_message_is_handed_out_with_its_type(client):
    """Classified in the browser; what to do with a picture is the host's call."""
    client._marks.advance("fan1", tracked(0))
    client._page = FakePage(panel=panel(msg_rows(1, "", kind="image")))

    (msg,), _ = drain(client)

    assert msg.type == "image" and msg.is_text() is False


def test_the_roster_row_names_the_fan_on_every_message(client):
    client._marks.advance("fan1", tracked(60))
    client._page = FakePage(panel=panel(msg_rows(61, "hi")))

    client._drain_open_panel("fan1", [row("fan1", nickname="小明", is_stranger=True)])
    (msg,) = drained(client)

    assert (msg.peer_nickname, msg.is_stranger) == ("小明", True)


def test_a_message_whose_ids_cannot_be_read_is_skipped_and_warned_about_once(client, caplog):
    client._marks.advance("fan1", tracked(60))
    rows = [{"index": 0, "kind": "text", "is_me": False, "text": "hi", "server_id": "", "seq": "",
             "created_at_ms": None, "short_id": "short1"}]
    client._page = FakePage(panel={"header_text": "小明", "items": rows})

    with caplog.at_level(logging.WARNING):
        first, trusted = drain(client)
        drain(client)

    assert trusted is False and first == []
    assert len([r for r in caplog.records if "could not read message ids" in r.getMessage()]) == 1


# ---------------------------------------------------------------- scrolled up
def test_a_panel_scrolled_away_from_the_newest_message_is_not_remembered_as_read(client, caplog):
    """What is on screen is handed out, but the conversation is not recorded
    as read up to date: its newest messages are not on the page at all, and
    recording it would mean they are never looked at again."""
    client._marks.advance("fan1", tracked(60))
    client._page = FakePage(panel=dict(panel(msg_rows(61, "在吗")), scrolled_px=1400))
    roster = [row("fan1", last_seq="86")]

    with caplog.at_level(logging.WARNING):
        trusted = client._drain_open_panel("fan1", roster)
        client._drain_open_panel("fan1", roster)

    assert [m.content for m in drained(client)] == ["在吗"]
    assert trusted is False
    assert len([r for r in caplog.records if "scrolled up" in r.getMessage()]) == 1


def test_a_panel_left_scrolled_up_is_scrolled_back_after_the_grace_period(client):
    """A person glancing back through history gets the grace period; past it
    the client scrolls back itself, once per episode rather than every pass."""
    client._marks.advance("fan1", tracked(60))
    client._page = FakePage(panel=dict(panel(msg_rows(61)), scrolled_px=1400))
    roster = [row("fan1", last_seq="86")]

    client._drain_open_panel("fan1", roster)
    assert client._page.scrolled_back == 0

    client._behind_since["fan1"] = time.time() - _SCROLL_BACK_AFTER_SECONDS - 1
    client._drain_open_panel("fan1", roster)
    client._drain_open_panel("fan1", roster)

    assert client._page.scrolled_back == 1


def test_a_panel_at_its_newest_message_is_never_scrolled(client):
    client._marks.advance("fan1", tracked(60))
    client._page = FakePage(panel=dict(panel(msg_rows(86)), scrolled_px=0))
    client._behind_since["fan1"] = time.time() - 3600

    client._drain_open_panel("fan1", [row("fan1", last_seq="86")])

    assert client._page.scrolled_back == 0
    assert "fan1" not in client._behind_since


# --------------------------------------------------------------- inbound scan
def test_scan_drains_the_open_conversation_and_remembers_its_preview(client):
    roster = [row("fan1", index=0, is_active=True, preview_text="hi", is_from_me=False)]
    client._page = FakePage(roster=roster, panel=panel(msg_rows(60, "hi")))

    client._scan_inbound()

    assert client._marks.seq("fan1") == 60
    assert client._marks.preview("fan1") == ("hi", False)


def test_a_conversation_whose_click_failed_is_not_clicked_again_right_away(client):
    roster = [row("fan1", index=0, preview_text="hi", is_from_me=False)]
    client._page = FakePage(roster=roster)
    clicks = []
    client._open_conversation = lambda index, sec_uid: clicks.append(sec_uid) or False

    client._scan_inbound()
    client._scan_inbound()

    assert clicks == ["fan1"]


def test_a_failed_click_does_not_use_up_the_change(client):
    """The preview is remembered only after the conversation was actually
    read. Remembering it before the click meant a failed click consumed the
    change and the message was never looked at again."""
    roster = [row("fan1", index=0, preview_text="hi", is_from_me=False)]
    client._page = FakePage(roster=roster)
    client._open_conversation = lambda index, sec_uid: False

    client._scan_inbound()
    client._reopen_after.clear()  # the cooldown has passed

    assert client._pick_candidate(roster, skip_sec_uid=None) is not None


def test_every_scan_publishes_the_one_to_one_roster_as_contacts(client):
    """What a host builds its fan catalog from -- the same read the scan
    already makes, so nothing extra is asked of the page."""
    client._pick_candidate = lambda *a, **kw: None
    client._page = FakePage(roster=[
        row("fan1", nickname="小明", is_stranger=True),
        row("group1", nickname="群", is_group=True),
    ])
    before = client.roster_version

    client._scan_inbound()

    assert client.get_contacts() == [{"sec_uid": "fan1", "nickname": "小明", "is_stranger": True}]
    assert client.roster_version == before + 1


def test_a_client_that_is_not_receiving_never_scans(client):
    """As with Wcf: nobody reading means no mark may move past a message."""
    client.disable_recv_msg()
    client._scan_inbound = lambda: pytest.fail("must not scan while not receiving")

    client._tick()


# --------------------------------------------------------- candidate picking
def test_pick_candidate_skips_group_chats(client):
    roster = [row("fan1", is_group=True, preview_text="hi", is_from_me=False)]
    assert client._pick_candidate(roster, skip_sec_uid=None) is None


def test_pick_candidate_skips_the_conversation_already_open(client):
    roster = [row("fan1", preview_text="hi", is_from_me=False)]
    assert client._pick_candidate(roster, skip_sec_uid="fan1") is None


def test_pick_candidate_opens_a_row_whose_latest_message_is_our_own(client):
    """A message typed directly on the real page/app is only seen if its
    conversation is opened (seen live 2026-09-17)."""
    roster = [row("fan1", preview_text="ok!", is_from_me=True)]
    assert client._pick_candidate(roster, skip_sec_uid=None) is not None


def test_pick_candidate_returns_a_fan_never_handled(client):
    roster = [row("fan1", preview_text="hi", is_from_me=False)]
    assert client._pick_candidate(roster, skip_sec_uid=None) is not None


def test_pick_candidate_ignores_a_fan_whose_preview_was_already_handled(client):
    client._marks.set_preview("fan1", ("hi", False))
    roster = [row("fan1", preview_text="hi", is_from_me=False)]

    assert client._pick_candidate(roster, skip_sec_uid=None) is None


def test_the_same_message_twice_is_still_seen_as_a_change(client):
    """The hole this replaces: identity by preview TEXT. A fan who sends "在"
    twice, or an operator who types the same reply again, left the preview
    identical, so the conversation was never opened again and the second
    message was not late -- it was never read at all."""
    client._marks.set_preview("fan1", ("在", False), 1_700_000_000_000)
    roster = [row("fan1", preview_text="在", is_from_me=False, created_time_ms=1_700_000_005_000)]

    assert client._pick_candidate(roster, skip_sec_uid=None) is not None


def test_a_conversation_that_has_not_moved_is_left_alone(client):
    client._marks.set_preview("fan1", ("在", False), 1_700_000_000_000)
    roster = [row("fan1", preview_text="在", is_from_me=False, created_time_ms=1_700_000_000_000)]

    assert client._pick_candidate(roster, skip_sec_uid=None) is None


def test_a_page_that_reports_no_timestamp_falls_back_to_the_preview(client):
    """A front-end change that drops createdTime must degrade to the old
    behaviour, never to opening nothing ever again."""
    client._marks.set_preview("fan1", ("在", False), 1_700_000_000_000)

    unchanged = [row("fan1", preview_text="在", is_from_me=False, created_time_ms=None)]
    changed = [row("fan1", preview_text="在吗", is_from_me=False, created_time_ms=None)]

    assert client._pick_candidate(unchanged, skip_sec_uid=None) is None
    assert client._pick_candidate(changed, skip_sec_uid=None) is not None


def test_a_mark_written_before_timestamps_existed_still_compares_on_preview(client):
    """Upgrade path: the marks file on disk has previews but no activity."""
    client._marks.set_preview("fan1", ("在", False))  # no activity recorded

    same = [row("fan1", preview_text="在", is_from_me=False, created_time_ms=1_700_000_000_000)]
    assert client._pick_candidate(same, skip_sec_uid=None) is None


def test_the_drained_timestamp_is_remembered_so_it_is_not_reopened(client):
    client._marks.advance("fan1", tracked(60))
    client._page = FakePage(panel=panel(msg_rows(61, "在")))
    roster = [row("fan1", index=0, is_active=True, preview_text="在", created_time_ms=1_700_000_005_000)]

    client._drain_and_remember(roster[0], roster)

    assert client._marks.activity("fan1") == 1_700_000_005_000


def test_a_restart_does_not_reopen_a_fan_whose_preview_did_not_change(client, tmp_path):
    """Previews are saved with the marks. Re-opening every fan on every
    restart was one more chance to replay history each time (two fans were
    replayed on five restarts in one morning)."""
    path = str(tmp_path / "inbound_marks.json")
    InboundMarks(path).set_preview("fan1", ("hi", False))
    client._marks = InboundMarks(path)

    roster = [row("fan1", preview_text="hi", is_from_me=False)]
    assert client._pick_candidate(roster, skip_sec_uid=None) is None


# -------------------------------------------------------- between-tick reads
def test_the_open_conversation_is_drained_as_soon_as_it_moves(client):
    """Reads are free -- they run inside the browser and the server cannot see
    them -- so the conversation on screen does not have to wait for the next
    tick. Measured 2026-09-18: 12s from a message appearing on the page to it
    reaching the console, most of it waiting for the tick."""
    client._marks.advance("fan1", tracked(60))
    client._marks.set_preview("fan1", ("old", False), 1_700_000_000_000)
    client._page = FakePage(
        roster=[row("fan1", index=0, is_active=True, preview_text="在吗", created_time_ms=1_700_000_005_000)],
        panel=panel(msg_rows(61, "在吗")),
    )

    assert client.catch_up_open_conversation() is True
    assert [m.content for m in drained(client)] == ["在吗"]


def test_a_between_tick_read_never_opens_a_conversation(client):
    """The whole point of splitting the cadences: clicking is what has to stay
    on the jittered tick, so this path must never click."""
    client._marks.advance("fan1", tracked(60))
    client._page = FakePage(
        roster=[
            row("fan1", index=0, is_active=True, created_time_ms=1_700_000_005_000),
            row("fan2", index=1, is_active=False, created_time_ms=1_700_000_009_000),
        ],
        panel=panel(msg_rows(61, "在吗")),
    )
    client._open_conversation = lambda index, sec_uid: pytest.fail("must not click between ticks")

    client.catch_up_open_conversation()


def test_a_quiet_conversation_costs_only_the_roster_read(client):
    client._marks.advance("fan1", tracked(60))
    client._marks.set_preview("fan1", ("在吗", False), 1_700_000_005_000)
    client._page = FakePage(
        roster=[row("fan1", index=0, is_active=True, preview_text="在吗", created_time_ms=1_700_000_005_000)],
        panel=panel(msg_rows(61, "在吗")),
    )

    assert client.catch_up_open_conversation() is False
    # The panel is never serialized when the roster says nothing moved.
    assert not any("header_text" in js for js in client._page.evaluated)


def test_a_between_tick_read_does_not_consume_the_tick_s_own_bookkeeping(client):
    """It records nothing on purpose. A read that stopped early -- at a bubble
    the server has not acknowledged, which carries no id -- would otherwise
    mark the conversation as seen and skip that message for good."""
    client._marks.advance("fan1", tracked(60))
    client._marks.set_preview("fan1", ("old", False), 1_700_000_000_000)
    client._page = FakePage(
        roster=[row("fan1", index=0, is_active=True, created_time_ms=1_700_000_005_000)],
        panel=panel(msg_rows(61, "在吗")),
    )

    client.catch_up_open_conversation()

    assert client._marks.activity("fan1") == 1_700_000_000_000


# ------------------------------------------------------------------ get_msg
def test_get_msg_raises_empty_after_a_second_of_nothing_like_wcferry(client):
    import queue

    started = time.monotonic()
    with pytest.raises(queue.Empty):
        client.get_msg()
    assert 0.5 < time.monotonic() - started < 3.0


def test_get_msg_returns_what_the_scan_handed_out(client):
    client._marks.advance("fan1", tracked(60))
    client._page = FakePage(panel=panel(msg_rows(61, "在吗", created_at_ms=now_ms())))
    client._drain_open_panel("fan1", [row("fan1")])

    assert client.get_msg().content == "在吗"

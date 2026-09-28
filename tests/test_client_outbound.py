# encoding:utf-8

"""Sending: humanized typing, proof on the page, and the cross-thread contract.

The typing tests were CowAgent's; the ``send_text`` tests are new with
douyin4u, which is the first place a send can be withdrawn for certain rather
than "may still go out once the loop recovers".
"""

import threading
import time

import pytest

from douyin4u import STATUS_EMPTY, STATUS_FAILED, STATUS_NOT_RUNNING, STATUS_OK, STATUS_PARTIAL, STATUS_TIMEOUT
from douyin4u.outbound import composer_holds
from fakes import FakePage, FakeSendPage, row


# -------------------------------------------------------- tab to the front
def test_send_now_brings_the_tab_to_front_even_when_already_active(client):
    """Confirmed live 2026-09-16: reads keep succeeding on a background tab,
    but a real click fails with "element is not visible" the moment the
    operator switches to another tab in the same window -- and that can happen
    to a conversation that was already open, which _open_conversation never
    touches."""
    client._page = FakePage(roster=[row("fan1", index=0, is_active=True)])
    client._open_conversation = lambda index, sec_uid: pytest.fail("must not click an active row")
    client._type_and_send_line = lambda text: True

    sent = client._send_now("fan1", "hi")

    assert client._page.brought_to_front == 1
    assert sent == 1


def test_a_reply_in_the_conversation_already_open_does_not_sit_out_a_reading_pause(client):
    """The pause separates a click from the first keystroke. With no click,
    the gap already exists and the pause only added 0.6-6s to every reply."""
    client._page = FakePage(roster=[row("fan1", index=0, is_active=True)])
    client._type_and_send_line = lambda text: True

    client._send_now("fan1", "在呢，怎么了")

    assert client._page.waits == []


def test_a_reply_that_had_to_open_the_conversation_still_reads_first(client):
    client._page = FakePage(roster=[row("fan1", index=0, is_active=False)])
    client._open_conversation = lambda index, sec_uid: True
    client._type_and_send_line = lambda text: True

    client._send_now("fan1", "在呢，怎么了")

    assert len(client._page.waits) == 1 and client._page.waits[0] >= 600


def test_open_conversation_also_brings_the_tab_to_front(client):
    """The inbound side opens conversations without ever going through
    _send_now -- the gap the first fix missed (live 2026-09-16)."""
    client._page = FakePage()  # no locator() -- the click itself fails, which is fine here
    client._open_conversation(0, "fan1")
    assert client._page.brought_to_front == 1


def test_send_now_still_works_when_bring_to_front_itself_fails(client):
    """The click that follows is still the real signal of whether it worked."""
    client._page = FakePage(roster=[row("fan1", index=0, is_active=True)])
    client._page.bring_to_front = lambda: (_ for _ in ()).throw(RuntimeError("tab gone"))
    client._type_and_send_line = lambda text: True

    assert client._send_now("fan1", "hi") == 1


def test_a_fan_not_on_screen_is_not_searched_for(client):
    client._page = FakePage(roster=[row("fan2", index=0)])

    assert client._send_now("fan1", "hi") == 0


def test_every_sent_line_is_reported_to_sent_listeners(client):
    """How a host recognises its own echo: every line is announced the moment
    it is confirmed, before a panel read can bring it back."""
    heard = []
    client.add_sent_listener(lambda sec_uid, line: heard.append((sec_uid, line)))
    client._page = FakePage(roster=[row("fan1", index=0, is_active=True)])
    client._type_and_send_line = lambda text: True

    client._send_now("fan1", "line one\nline two")

    assert heard == [("fan1", "line one"), ("fan1", "line two")]


def test_a_failing_listener_does_not_fail_a_line_that_went_out(client):
    client.add_sent_listener(lambda sec_uid, line: (_ for _ in ()).throw(RuntimeError("boom")))
    client._page = FakePage(roster=[row("fan1", index=0, is_active=True)])
    client._type_and_send_line = lambda text: True

    assert client._send_now("fan1", "a\nb") == 2


# ---------------------------------------------------------- composer check
def test_an_emoji_reply_counts_as_typed_even_though_innertext_drops_it():
    """The composer renders emoji as <img> and innerText omits them, so an
    exact comparison would abort every emoji-bearing reply."""
    assert composer_holds("你好呀", "你好呀 😊") is True


def test_an_empty_composer_is_still_treated_as_a_failure():
    assert composer_holds("", "你好呀") is False


def test_genuinely_different_composer_content_is_still_caught():
    assert composer_holds("完全不同的内容", "你好呀") is False


def test_a_reply_made_only_of_emoji_passes_on_presence_alone():
    assert composer_holds("🙂", "😊") is True


# ---------------------------------------------------- typing and confirming
def test_a_typed_line_confirmed_by_its_own_new_bubble_counts_as_sent(client):
    client._page = FakeSendPage()

    assert client._type_and_send_line("你好呀") is True
    assert client._page.delivered == ["你好呀"]


def test_a_line_the_composer_dropped_but_never_delivered_is_not_counted_as_sent(client):
    """The 2026-09-17 case the old check could not see: Enter emptied the
    composer and nothing went out."""
    client._page = FakeSendPage(enter_behaviour="empty_only")

    assert client._type_and_send_line("你好呀") is False
    assert client._page.delivered == []


def test_a_line_enter_did_not_send_fails_and_leaves_the_composer_empty(client):
    """This page has no send button to fall back on, so text Enter left behind
    is a refused send. The box is cleared so the next line does not inherit it."""
    client._page = FakeSendPage(enter_behaviour="keep")

    assert client._type_and_send_line("你好呀") is False
    assert client._page.delivered == []
    assert client._page.composer == ""


def test_an_identical_bubble_sent_earlier_does_not_confirm_a_new_line(client):
    """Confirmation counts matching bubbles rather than looking for one."""
    page = FakeSendPage(enter_behaviour="empty_only")
    page.deliver("你好呀")  # an identical reply from earlier in the conversation
    client._page = page

    assert client._type_and_send_line("你好呀") is False


def test_typing_never_starts_when_the_composer_does_not_hold_the_caret(client):
    client._page = FakeSendPage(focus_after_click=False)

    assert client._type_and_send_line("你好呀") is False
    assert client._page.typed == []


def test_a_line_whose_caret_was_stolen_midway_is_aborted_not_sent(client):
    client._page = FakeSendPage(steal_focus_after=2)

    assert client._type_and_send_line("你好呀我在的") is False
    assert client._page.delivered == []
    assert client._page.composer == ""


def test_an_unreadable_panel_lets_the_line_count_as_sent(client):
    """An unreadable read is the standing "change nothing" case; asserting a
    negative from it would drop the rest of a reply that may well have gone."""
    page = FakeSendPage(enter_behaviour="empty_only")
    page.panel_raises = True
    client._page = page

    assert client._type_and_send_line("你好呀") is True


def test_every_line_raises_the_tab_not_only_the_first(client):
    client._page = FakeSendPage(roster=[row("fan1", index=0, is_active=True)])

    assert client._send_now("fan1", "第一行\n第二行") == 2
    # Once in _send_now, then once per line.
    assert client._page.brought_to_front == 3


def test_an_emoji_only_line_is_confirmed_by_the_composer_alone(client):
    client._page = FakeSendPage(enter_behaviour="empty_only")

    assert client._type_and_send_line("😊") is True


# --------------------------------------------------------------- send_text
def _serve(client, sent_lines, delay=0.0):
    """Run the page thread's send pass once a command is queued."""
    client._send_now = lambda sec_uid, text: (time.sleep(delay), sent_lines)[1]

    def _pump():
        deadline = time.monotonic() + 5
        while client._commands.empty() and time.monotonic() < deadline:
            time.sleep(0.01)
        client._run_sends()

    thread = threading.Thread(target=_pump, daemon=True)
    thread.start()
    return thread


def test_send_text_waits_for_the_page_thread_and_reports_in_full(client):
    client._running = True
    _serve(client, 2)

    result = client.send_text("一\n二", "fan1", timeout=5)

    assert result == (STATUS_OK, 2, 2)
    assert result.ok


def test_a_send_that_stopped_partway_says_how_far_it_got(client):
    client._running = True
    _serve(client, 1)

    assert client.send_text("一\n二\n三", "fan1", timeout=5) == (STATUS_PARTIAL, 1, 3)


def test_a_send_where_nothing_went_out_is_failed(client):
    client._running = True
    _serve(client, 0)

    assert client.send_text("一", "fan1", timeout=5).status == STATUS_FAILED


def test_a_send_nobody_picked_up_is_withdrawn_and_never_goes_out_later(client):
    """The old queue could only warn that a timed-out send "may still go out
    once the loop recovers". A withdrawn command is skipped for certain."""
    client._running = True
    client._send_now = lambda sec_uid, text: pytest.fail("a withdrawn send must never run")

    result = client.send_text("你好", "fan1", timeout=0.05)
    client._run_sends()  # the page recovers after the caller gave up

    assert result == (STATUS_TIMEOUT, 0, 1)


def test_a_send_already_typing_is_waited_out_rather_than_cut_off(client):
    client._running = True
    _serve(client, 1, delay=0.3)

    result = client.send_text("你好", "fan1", timeout=0.1)

    assert result.status == STATUS_OK


def test_a_stopped_client_refuses_rather_than_hanging(client):
    assert client.send_text("你好", "fan1", timeout=5).status == STATUS_NOT_RUNNING


@pytest.mark.parametrize("text, receiver", [("", "fan1"), ("  \n ", "fan1"), ("你好", ""), ("你好", "  ")])
def test_an_empty_message_or_receiver_is_refused(client, text, receiver):
    client._running = True
    assert client.send_text(text, receiver, timeout=5).status == STATUS_EMPTY
    assert client._commands.empty()


def test_a_send_that_raised_still_answers_its_caller(client):
    client._running = True
    client._send_now = lambda sec_uid, text: (_ for _ in ()).throw(RuntimeError("page gone"))
    threading.Timer(0.05, client._run_sends).start()

    assert client.send_text("你好", "fan1", timeout=5).status == STATUS_FAILED


def test_sends_still_queued_when_the_loop_ends_are_answered(client):
    client._running = True
    threading.Timer(0.05, client._fail_pending_sends).start()

    assert client.send_text("你好", "fan1", timeout=5).status == STATUS_NOT_RUNNING


def test_a_client_that_stops_while_a_send_waits_answers_it(client):
    """Even with no timeout: a send nobody will ever run must not block forever."""
    client._running = True
    threading.Timer(0.05, client.cleanup).start()

    assert client.send_text("你好", "fan1").status == STATUS_NOT_RUNNING


def test_several_queued_sends_go_out_in_order_in_one_pass(client):
    client._running = True
    order = []
    client._send_now = lambda sec_uid, text: order.append(text) or 1
    results = []
    threads = [
        threading.Thread(target=lambda t=t: results.append(client.send_text(t, "fan1", timeout=5)))
        for t in ("一", "二", "三")
    ]
    for thread in threads:
        thread.start()
        time.sleep(0.02)
    client._run_sends()
    for thread in threads:
        thread.join(2)

    assert order == ["一", "二", "三"]
    assert [r.status for r in results] == [STATUS_OK] * 3

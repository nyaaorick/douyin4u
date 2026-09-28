# encoding:utf-8

"""The page itself: what state it is in, getting back to it, pacing, attaching.

Carried over from CowAgent's Douyin channel tests. The startup tests drive
``Douyin.start()``/``_main`` with Playwright, the port probe and the browser
launcher all replaced, so no browser is ever dialled or started.
"""

import logging
import socket
import time
from types import SimpleNamespace

import pytest

import douyin4u.client as client_module
from douyin4u import PAGE_STATES, Douyin
from douyin4u.browser import cdp_port_open
from douyin4u.client import (
    _RETURN_RETRY_SECONDS,
    _WRONG_PAGE_GRACE_SECONDS,
    STATUS_OK,
    _on_chat_page,
    _SendCommand,
)
from fakes import CHAT_URL, CREATOR_CHAT_URL, PROFILE_URL, FakePage, tracked


def _state(**kw):
    base = {"challenge": False, "logged_out": False, "chat_ready": False, "ready_state": "complete"}
    base.update(kw)
    return base


def _queue_send(client):
    command = _SendCommand("fan1", "你好", 1)
    client._commands.put(command)
    return command


# ------------------------------------------------------------------ page state
def test_a_healthy_chat_page_reports_ready(client):
    assert client._detect_page_state() == "ready"


def test_a_logged_out_page_is_named_rather_than_silently_spun_on(client):
    """The 2026-09-15 incident: risk control logged the account out and
    nothing noticed -- every tick read an empty roster and returned early,
    forever, in silence."""
    client._page = FakePage(state={"challenge": False, "logged_out": True, "chat_ready": False})
    assert client._detect_page_state() == "logged_out"


def test_a_visible_challenge_outranks_every_other_signal(client):
    client._page = FakePage(state={"challenge": True, "logged_out": True, "chat_ready": False})
    assert client._detect_page_state() == "challenge"


def test_a_tab_navigated_off_the_chat_page_says_where_it_went(client):
    client._page = FakePage(state=_state(), url=PROFILE_URL)

    assert client._detect_page_state() == "wrong_page"
    assert "/user/self" in client._page_detail


def test_a_tab_off_the_chat_page_is_never_ready_even_if_a_marker_matches(client):
    """Fail closed: every click and keystroke here assumes the chat page."""
    client._page = FakePage(state=_state(chat_ready=True), url=PROFILE_URL)
    assert client._detect_page_state() == "wrong_page"


def test_the_creator_center_chat_is_a_wrong_page_even_though_it_renders_a_chat(client):
    """Nothing sent from it reached anyone (2026-09-21)."""
    client._page = FakePage(state=_state(chat_ready=True), url=CREATOR_CHAT_URL)
    assert client._detect_page_state() == "wrong_page"


@pytest.mark.parametrize("url, expected", [
    (CHAT_URL, True),
    ("https://www.douyin.com/chat", True),
    ("https://www.douyin.com/chat/", True),
    (PROFILE_URL, False),
    (CREATOR_CHAT_URL, False),
    ("https://www.douyin.com/chatroom", False),
    ("https://www.douyin.com.example.net/chat", False),
    ("", False),
])
def test_only_the_main_site_chat_page_counts_as_the_chat_page(url, expected):
    assert _on_chat_page(url) is expected


def test_a_chat_page_that_has_not_finished_loading_is_loading(client):
    client._page = FakePage(state=_state(ready_state="interactive"))
    assert client._detect_page_state() == "loading"


def test_a_loaded_chat_page_without_its_chat_ui_is_named_as_such(client):
    client._page = FakePage(state=_state())
    assert client._detect_page_state() == "chat_not_rendered"


def test_a_failed_state_read_is_page_unreadable_and_keeps_the_error(client):
    client._page = FakePage(state_raises=True)

    assert client._detect_page_state() == "page_unreadable"
    assert "page went away" in client._page_detail


@pytest.mark.parametrize("url", [CHAT_URL, PROFILE_URL, "edge://newtab/", ""])
@pytest.mark.parametrize("ready_state", ["loading", "interactive", "complete"])
@pytest.mark.parametrize("challenge", [False, True])
@pytest.mark.parametrize("logged_out", [False, True])
@pytest.mark.parametrize("chat_ready", [False, True])
def test_every_page_the_client_can_see_has_a_named_state(client, url, ready_state, challenge,
                                                          logged_out, chat_ready):
    """No catch-all: whatever the tab shows, a host can name it."""
    client._page = FakePage(
        state=_state(challenge=challenge, logged_out=logged_out, chat_ready=chat_ready,
                     ready_state=ready_state),
        url=url,
    )
    assert client._detect_page_state() in PAGE_STATES


def test_every_state_change_is_logged_by_name(client, caplog):
    with caplog.at_level(logging.INFO):
        for state in PAGE_STATES:
            client._note_page_state(state)

    for state in PAGE_STATES:
        assert f"page state {state}" in caplog.text


def test_status_reads_starting_before_the_first_tick(client):
    assert client.get_status() == {"running": False, "page_state": "starting", "page_detail": ""}


def test_status_names_the_state_and_where_the_tab_went(client):
    client._running = True
    client._page = FakePage(state=_state(), url=PROFILE_URL)
    client._note_page_state(client._detect_page_state())

    status = client.get_status()

    assert status["page_state"] == "wrong_page"
    assert "/user/self" in status["page_detail"]
    assert client.is_ready() is False
    assert client.is_login() is True  # a wandered tab may well still be logged in


def test_a_logged_out_page_is_not_logged_in(client):
    client._running = True
    client._note_page_state("logged_out")

    assert client.is_login() is False


# ------------------------------------------------- returning to the chat page
def test_a_tab_that_just_left_the_chat_page_gets_a_grace_period(client):
    client._page = FakePage(state=_state(), url=PROFILE_URL)
    client._note_page_state("wrong_page")
    left_at = client._wrong_page_since

    assert client._should_return_to_chat(left_at + 1) is False
    assert client._should_return_to_chat(left_at + _WRONG_PAGE_GRACE_SECONDS) is True


def test_returning_navigates_the_tab_to_the_chat_page(client):
    client._page = FakePage(state=_state(), url=PROFILE_URL)

    client._return_to_chat_page()

    assert client._page.gotos == [(CHAT_URL, "load")]


def test_a_failed_return_is_not_retried_every_tick(client):
    """Forcing a navigation every 30s at a page that keeps refusing it is a
    bot pattern of its own."""
    client._page = FakePage(state=_state(), url=PROFILE_URL, goto_raises=True)
    client._note_page_state("wrong_page")
    now = client._wrong_page_since + _WRONG_PAGE_GRACE_SECONDS

    client._return_to_chat_page(now)

    assert client._should_return_to_chat(now + 60) is False
    assert client._should_return_to_chat(now + _RETURN_RETRY_SECONDS) is True


def test_a_return_that_is_redirected_elsewhere_counts_as_failed(client):
    client._page = FakePage(state=_state(), url=PROFILE_URL, goto_lands_on="https://www.douyin.com/?recommend=1")
    client._note_page_state("wrong_page")
    now = client._wrong_page_since + _WRONG_PAGE_GRACE_SECONDS

    client._return_to_chat_page(now)

    assert client._should_return_to_chat(now + 60) is False


def test_leaving_the_chat_page_keeps_every_fans_mark(client):
    """A mark is a server-assigned sequence that a re-render does not change."""
    client._marks.advance("fan1", tracked(5))
    client._note_page_state("ready")

    client._note_page_state("wrong_page")

    assert client._marks.seq("fan1") == 5


def test_a_tick_returns_a_wandered_tab_and_carries_on_in_the_same_pass(client):
    client._page = FakePage(state=_state(chat_ready=True), url=PROFILE_URL)
    client._note_page_state("wrong_page")
    client._wrong_page_since = time.time() - _WRONG_PAGE_GRACE_SECONDS - 1

    client._tick()

    assert client._page.gotos == [(CHAT_URL, "load")]
    assert client._page_state == "ready"


def test_a_tick_leaves_a_tab_alone_during_its_grace_period(client):
    client._page = FakePage(state=_state(chat_ready=True), url=PROFILE_URL)

    client._tick()

    assert client._page.gotos == []
    assert client._page_state == "wrong_page"


def test_a_ready_tick_runs_queued_sends_before_it_scans(client):
    order = []
    client._run_sends = lambda: order.append("send")
    client._scan_inbound = lambda: order.append("scan")

    client._tick()

    assert order == ["send", "scan"]


# ------------------------------------------------------------------ tabs
def test_startup_prefers_the_chat_tab_over_another_douyin_tab(client):
    profile = SimpleNamespace(url=PROFILE_URL)
    chat = SimpleNamespace(url=CHAT_URL)
    client._browser = SimpleNamespace(contexts=[SimpleNamespace(pages=[profile, chat])])

    assert client._find_or_open_chat_page() is chat


def test_with_no_chat_tab_a_douyin_tab_is_attached_so_the_tick_can_bring_it_back(client):
    other_site = SimpleNamespace(url="https://www.baidu.com/")
    creator = SimpleNamespace(url=CREATOR_CHAT_URL)
    client._browser = SimpleNamespace(contexts=[SimpleNamespace(pages=[other_site, creator])])

    assert client._find_or_open_chat_page() is creator


# ------------------------------------------------------------------ pacing
def test_the_loop_backs_off_hard_while_the_page_is_unusable(client):
    client._page_state = "challenge"
    blocked = client._next_tick_ms()
    client._page_state = "ready"
    ready = client._next_tick_ms()

    assert blocked > ready


def test_a_ready_tick_is_resampled_rather_than_fixed(client):
    """A dead-exact 2.000s poll is a bot signature by itself."""
    client._page_state = "ready"
    seen = {client._next_tick_ms() for _ in range(200)}
    assert len(seen) > 20


def test_a_queued_send_does_not_wait_out_the_whole_tick(client):
    client._page_state = "ready"
    client._running = True
    client._send_now = lambda sec_uid, text: 1
    command = _queue_send(client)

    client._idle(60)

    assert command.result.status == STATUS_OK


def test_an_early_wake_sends_but_never_scans(client):
    """Scanning clicks conversations, and that cadence is jittered on purpose
    (2026-09-15 logout). Nothing that shortens the wait for a queued send may
    make the client click more often than it already does."""
    client._page_state = "ready"
    client._running = True
    client._scan_inbound = lambda: pytest.fail("the idle wait must never scan")
    client._send_now = lambda sec_uid, text: 1
    _queue_send(client)

    client._idle(60)


def test_an_idle_wait_with_nothing_queued_leaves_the_page_state_alone(client):
    client._page_state = "ready"
    client._running = True

    client._idle(40)

    assert not any("chat_ready" in js for js in client._page.evaluated)


def test_a_failed_between_tick_read_does_not_stop_the_client(client, caplog):
    client._page_state = "ready"
    client._running = True
    client.catch_up_open_conversation = lambda: (_ for _ in ()).throw(RuntimeError("read blew up"))

    with caplog.at_level(logging.ERROR):
        client._idle(1400)

    assert client._running is True
    assert any("between-tick read failed" in r.message for r in caplog.records)


def test_a_send_that_blows_up_during_the_idle_wait_does_not_stop_the_client(client, caplog):
    client._page_state = "ready"
    client._running = True
    client._run_sends = lambda: (_ for _ in ()).throw(RuntimeError("send blew up"))
    _queue_send(client)

    with caplog.at_level(logging.ERROR):
        client._idle(40)

    assert client._running is True
    assert any("running queued sends failed" in r.message for r in caplog.records)


def test_stopping_the_client_cuts_the_idle_wait_short(client):
    """cleanup() is called from another thread and only sets the flag -- the
    page thread has to notice it, and sitting out a full tick first would make
    every shutdown that much slower."""
    client._page_state = "ready"
    client._running = False

    client._idle(30000)

    assert client._page.waits == []


def test_an_idle_wait_on_an_unusable_page_is_one_plain_sleep(client):
    client._page_state = "logged_out"
    _queue_send(client)

    client._idle(30000)

    assert client._page.waits == [30000]


# ------------------------------------------------------------------ attaching
class FakeDriver:
    """Stands in for what ``sync_playwright().start()`` returns. Counts
    ``stop()`` calls, because every one missing is a node driver left running
    inside the host for the rest of its life."""

    def __init__(self, browser=None, connect_error=None):
        self.chromium = self
        self._browser = browser
        self._connect_error = connect_error
        self.stopped = 0

    def connect_over_cdp(self, url):
        if self._connect_error is not None:
            raise self._connect_error
        return self._browser

    def stop(self):
        self.stopped += 1


@pytest.fixture
def driver(monkeypatch):
    """Install a FakeDriver behind the client's Playwright entry point, and
    replace the TCP pre-check and the browser launcher -- which would
    otherwise dial 127.0.0.1:9222 and start a real Edge on the test machine."""

    def _install(cdp_open=True, launch=(True, ""), **kw):
        fake = FakeDriver(**kw)
        fake.started = []
        fake.launches = []
        monkeypatch.setattr(client_module, "cdp_port_open", lambda url: cdp_open)
        monkeypatch.setattr(
            client_module, "launch_debug_browser",
            lambda url, path, profile: (fake.launches.append((url, path, profile)), launch)[1],
        )
        monkeypatch.setattr(
            client_module, "_sync_playwright",
            lambda: SimpleNamespace(start=lambda: fake.started.append(1) or fake),
        )
        return fake

    return _install


def _chat_tab_browser():
    return SimpleNamespace(contexts=[SimpleNamespace(pages=[SimpleNamespace(url=CHAT_URL)])])


def _attached(**kw):
    dy = Douyin(**kw)
    dy._run_loop = lambda: None  # start() ends in the loop; stop at the attach
    return dy


def test_a_browser_that_is_not_listening_is_started_rather_than_refused(driver):
    """Until 2026-09-18 this was where startup gave up and told the operator to
    launch Edge by hand -- a step they had to repeat after every reboot."""
    fake = driver(cdp_open=False, browser=_chat_tab_browser())
    dy = _attached(browser_path="/opt/edge", profile_dir="/tmp/profile")

    assert dy.start() is True
    assert fake.launches == [("http://127.0.0.1:9222", "/opt/edge", "/tmp/profile")]
    assert fake.started == [1]
    assert dy.last_error == ""


def test_a_browser_that_could_not_be_started_says_why_and_spawns_no_driver(driver):
    fake = driver(cdp_open=False, launch=(False, "could not find Microsoft Edge to start"))
    dy = _attached()

    assert dy.start() is False
    assert fake.started == []
    assert "could not find Microsoft Edge" in dy.last_error


def test_an_already_listening_browser_is_never_relaunched(driver):
    fake = driver(cdp_open=True, browser=_chat_tab_browser())

    assert _attached().start() is True
    assert fake.launches == []


def test_starting_the_browser_can_be_switched_off(driver):
    """For a host where something else owns the browser's lifecycle."""
    fake = driver(cdp_open=False)
    dy = _attached(launch_browser=False)

    assert dy.start() is False
    assert fake.launches == [] and fake.started == []
    assert "switched off" in dy.last_error


def test_a_failed_attach_stops_the_driver_it_started(driver):
    """Each start pressed against a port that accepts TCP but refuses CDP used
    to leave one more node driver running under the host."""
    fake = driver(connect_error=RuntimeError("connect ECONNREFUSED 127.0.0.1:9222"))
    dy = _attached()

    assert dy.start() is False
    assert fake.stopped == 1
    assert dy._playwright is None
    assert "ECONNREFUSED" in dy.last_error


def test_no_douyin_tab_stops_the_driver_too(driver):
    browser = SimpleNamespace(contexts=[SimpleNamespace(pages=[SimpleNamespace(url="https://www.baidu.com/")])])
    fake = driver(browser=browser)
    dy = _attached()

    assert dy.start() is False
    assert fake.stopped == 1
    assert dy._browser is None
    assert "Douyin page" in dy.last_error


def test_the_page_thread_ends_by_tearing_down_and_answering_queued_sends(driver):
    fake = driver(browser=_chat_tab_browser())
    dy = Douyin()
    ended = []
    dy._tick = lambda: dy.cleanup()
    dy._idle = lambda ms: None
    dy._fail_pending_sends = lambda: ended.append("answered")

    assert dy.start() is True
    dy._thread.join(2)

    assert ended == ["answered"]
    assert fake.stopped == 1
    assert dy.is_running is False


def test_a_tab_that_goes_away_stops_the_client_and_says_why(driver):
    driver(browser=_chat_tab_browser())
    dy = Douyin()
    dy._tick = lambda: None
    dy._idle = lambda ms: (_ for _ in ()).throw(RuntimeError("Target page, context or browser has been closed"))

    dy.start()
    dy._thread.join(2)

    assert dy.is_running is False
    assert "the attached tab is gone" in dy.last_error


# --------------------------------------------------------------- port probe
def test_cdp_port_open_sees_a_listening_socket():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        assert cdp_port_open(f"http://127.0.0.1:{port}") is True


def test_cdp_port_open_is_false_once_nobody_listens():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        port = server.getsockname()[1]
    assert cdp_port_open(f"http://127.0.0.1:{port}") is False


@pytest.mark.parametrize("url", ["", "not a url", "http://127.0.0.1:notaport"])
def test_cdp_port_open_is_false_for_a_url_it_cannot_dial(url):
    assert cdp_port_open(url) is False

# encoding:utf-8

"""The Douyin client -- ``wcferry.Wcf``'s counterpart, over a real browser.

There is no vendor SDK to wrap: Douyin's official private-message API requires
an authenticated enterprise/blue-V account and a review process, which a
personal-account digital avatar cannot get. This client instead drives the
same chat page a human operator would use, over Playwright attached to a
browser via Chrome DevTools Protocol (``connect_over_cdp``), and presents it
through the interface ``wcferry`` made familiar:

* ``start()`` attaches (starting the debug browser first if nothing listens),
* ``enable_receiving_msg()`` + ``get_msg()`` hand out each new message once,
  as a :class:`~douyin4u.dymsg.DyMsg` shaped like ``WxMsg``,
* ``send_text()`` is callable from any thread and returns a status,
* ``get_contacts()`` is the conversation roster last seen on screen,
* ``cleanup()`` detaches without closing the operator's browser.

It starts the browser itself but never logs in: opening a browser is
mechanical and has one right answer, while authenticating a real account stays
an operator action performed once, out of band, the same way ``wcferry``
never logs WeChat in. A browser that comes up on the login screen is reported
as ``logged_out`` and waits for a human.

**The page is the main site's chat, ``www.douyin.com/chat?isPopup=1``** -- the
message window the right-hand sidebar of ``www.douyin.com/user/self`` opens.
Until 2026-09-21 it was the creator-center chat
(``creator.douyin.com/creator-micro/data/following/chat``), and that page must
not be driven again: every message sent from it that day, by automation and by
the operator's own hand alike, was stored by Douyin as visible to the sender
only (``ext["s:visible"]`` = the account's own uid,
``im_callback_status_code`` 8101). The account's phone showed each one as
sent; the fan never received any of them. ``_on_chat_page`` therefore accepts
only the main-site page, and a self-only message is flagged on its ``DyMsg``.

Two page facts drive nearly every design choice (see page_scripts.py for the
probe that established them):

1. **The fan's stable id (``secUid``) is not reachable through any DOM
   attribute.** It IS present in React's own component props, reachable by
   walking the fiber tree already attached to any mounted DOM node -- an
   internal implementation detail, not a public contract, that can break on
   any Douyin front-end deploy. Every read fails closed: an unrecognised shape
   means "skip this conversation", never a guess.
2. **The rows to click and type into carry ``data-e2e`` test hooks**
   (``conversation-item``, ``msg-input``), the most stable handle the page
   offers.

**Threading.** Playwright's sync API is bound to the thread that started it,
so this client runs one dedicated thread for its whole life and that thread is
the *only* one that ever touches ``self._page``. Everything public is safe
from any other thread: ``send_text`` queues a command for the page thread and
waits for its result, the way ``Wcf.send_text`` waits for spy.dll's.

**Pacing is not decoration.** On 2026-09-15 the test account was logged out
by risk control, a bot-verification widget having appeared first. The
automation's timing signature is the near-certain cause: every keystroke
exactly 25ms apart, and a poll loop on an exactly fixed interval -- both of
which the page reports server-side, unlike the fiber reads, which never leave
the browser. Every delay here is sampled from a distribution in
``humanize.py``. None of this makes the client undetectable; it removes the
specific tells there is evidence for, and nothing more.

The loop also checks every tick that the attached tab is still a usable chat
page (``_detect_page_state``). Before that check existed, the same logout left
the loop spinning silently forever -- the roster selectors simply stopped
matching -- while queued sends failed with an error that blamed the fan
instead of naming the logout. A logged-out or challenged page now suspends
all work, says so once, and waits for a human.
"""

import json
import logging
import queue
import threading
import time
from typing import Callable, List, NamedTuple, Optional
from urllib.parse import urlparse

from douyin4u.browser import CHAT_URL, DEFAULT_CDP_URL, cdp_port_open, launch_debug_browser
from douyin4u.humanize import sample_tick_ms
from douyin4u.inbound import InboundScanMixin
from douyin4u.outbound import OutboundMixin, reply_lines
from douyin4u.page_scripts import JS_DETECT_STATE, JS_READ_PANEL, JS_READ_ROSTER
from douyin4u.tracker import InboundMarks

logger = logging.getLogger(__name__)

_CHAT_HOST = "www.douyin.com"
_CHAT_PATH = "/chat"
# Any tab on a Douyin host is worth attaching to when none is on the chat
# page: the tick then brings it to the chat page (see _return_to_chat_page).
_DOUYIN_DOMAIN = "douyin.com"
_DEFAULT_TICK_MIN_SECONDS = 2.0
_DEFAULT_TICK_MAX_SECONDS = 5.0

# How long a browser this client just started is given to put its first tab
# on the chat page. Only applied to a browser started here: one that was
# already running has had its whole lifetime to get there, and waiting on it
# would only delay an honest "no tab is on the chat page".
_LAUNCHED_TAB_WAIT_SECONDS = 20.0
_TAB_POLL_SECONDS = 0.5

# What the attached tab is currently showing. The client does real work only
# in PAGE_READY; every other state means "stop touching the page". There is
# deliberately no catch-all: each state names what is on screen, so a host can
# tell the operator what to fix instead of shrugging.
PAGE_READY = "ready"
PAGE_LOGGED_OUT = "logged_out"
PAGE_CHALLENGE = "challenge"
# The operator shares this browser, so clicking over to another page is normal.
PAGE_WRONG_PAGE = "wrong_page"
PAGE_LOADING = "loading"
# Right URL, fully loaded, yet no roster, tabs or composer: a stuck render,
# or Douyin changed the page and page_scripts.py's selectors need updating.
PAGE_CHAT_NOT_RENDERED = "chat_not_rendered"
PAGE_UNREADABLE = "page_unreadable"
PAGE_STATES = (
    PAGE_READY, PAGE_LOGGED_OUT, PAGE_CHALLENGE, PAGE_WRONG_PAGE,
    PAGE_LOADING, PAGE_CHAT_NOT_RENDERED, PAGE_UNREADABLE,
)

# Poll cadence while the page is not usable. Deliberately slow: there is
# nothing to do but wait for a human, and a tight loop against a page that
# just challenged us is the last thing this client should be doing.
_BLOCKED_TICK_SECONDS = 30.0

# How often the idle wait between ticks looks for a queued send. The scan's
# own cadence stays jittered and untouched (see _next_tick_ms); this only
# decides how long a send already asked for sits before anything picks it up.
_COMMAND_POLL_MS = 500

# How often the idle wait re-reads the roster to see whether the conversation
# on screen has moved. Far shorter than a tick because a read costs nothing
# the tick's jitter is protecting: reading runs entirely inside the browser,
# while the click that opens another conversation is what has to look human.
# See InboundScanMixin.catch_up_open_conversation.
_FAST_READ_MS = 1000

# How long a tab may sit off the chat page before it is navigated back. Under
# one blocked tick, so the return happens on the check after the one that
# first noticed -- the operator shares this browser, and a glance at the
# follower list should not be yanked away mid-click.
_WRONG_PAGE_GRACE_SECONDS = 20.0
# After a return that failed or was redirected elsewhere, wait this long
# before trying again.
_RETURN_RETRY_SECONDS = 300.0
_NAVIGATE_TIMEOUT_MS = 20000
# The SPA keeps mounting after "load"; same settle time as the smoke test.
_SPA_SETTLE_MS = 1500

# The page's own test hook for a roster row (see the module docstring).
# JS_READ_ROSTER's `index` counts matches of it, which is what makes
# `.nth(index)` click the row it describes. Also written out inside
# page_scripts.py -- it can't be interpolated into those raw JS blocks without
# turning them into f-strings and escaping every literal brace.
_SEL_CONVERSATION_ROW = '[data-e2e="conversation-item"]'

# send_text() statuses. 0 is delivered in full, as with ``Wcf.send_text``;
# every negative value means nothing went out.
STATUS_OK = 0
# How often a caller blocked in send_text() looks up to check the page thread
# is still there to answer it.
_SEND_WAIT_SLICE_SECONDS = 0.5
STATUS_PARTIAL = 1
STATUS_FAILED = -1
STATUS_NOT_RUNNING = -2
STATUS_TIMEOUT = -3
STATUS_EMPTY = -4

SentListener = Callable[[str, str], None]


class SendResult(NamedTuple):
    """What one ``send_text`` call achieved.

    Not a bare int, as ``Wcf.send_text`` returns, because a Douyin reply is
    sent one line per message and can stop partway: ``sent`` of ``total``
    lines went out. ``status`` still reads the way wcferry's does -- 0 is
    delivered in full -- so a host that only wants the one number has it.

    ``STATUS_TIMEOUT`` promises more than "no answer in time": the send was
    withdrawn before a single key was pressed, so it will never go out later.
    """

    status: int
    sent: int
    total: int

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK


class _SendCommand:
    """One ``send_text`` request, crossing from the caller's thread to the page's.

    ``pending`` -> ``started`` is claimed by the page thread and ``pending``
    -> ``cancelled`` by a caller that stopped waiting; the lock makes those two
    exclusive. That is what lets a timed-out send be withdrawn for certain
    rather than "maybe still sent later", which is what the old unguarded
    queue could only warn about.
    """

    def __init__(self, receiver: str, text: str, total: int):
        self.receiver = receiver
        self.text = text
        self.total = total
        self.result = None
        self.done = threading.Event()
        self._state = "pending"
        self._lock = threading.Lock()

    def claim(self) -> bool:
        with self._lock:
            if self._state != "pending":
                return False
            self._state = "started"
            return True

    def cancel(self) -> bool:
        with self._lock:
            if self._state != "pending":
                return False
            self._state = "cancelled"
            return True

    def finish(self, result: SendResult) -> None:
        self.result = result
        self.done.set()


def _on_chat_page(url: str) -> bool:
    """Is ``url`` the main-site chat page, the only page this client drives?

    Checked by URL, not only by DOM markers: the same IM components could
    one day render elsewhere on the site, and every click here assumes the
    chat page. The creator-center chat is deliberately *not* accepted, though
    its markers once matched -- see the module docstring for what sending
    from it did.
    """
    parsed = urlparse(url or "")
    return parsed.hostname == _CHAT_HOST and parsed.path.rstrip("/") == _CHAT_PATH


def _on_douyin_site(url: str) -> bool:
    host = urlparse(url or "").hostname or ""
    return host == _DOUYIN_DOMAIN or host.endswith("." + _DOUYIN_DOMAIN)


def _sync_playwright():
    """Playwright's sync entry point. A function so tests can replace it."""
    from playwright.sync_api import sync_playwright
    return sync_playwright()


class Douyin(InboundScanMixin, OutboundMixin):
    """One attached Douyin account. See the module docstring."""

    def __init__(self, cdp_url: str = DEFAULT_CDP_URL, *, launch_browser: bool = True,
                 browser_path: str = "", profile_dir: str = "", marks_path: Optional[str] = None,
                 tick_min_seconds: float = _DEFAULT_TICK_MIN_SECONDS,
                 tick_max_seconds: float = _DEFAULT_TICK_MAX_SECONDS):
        self.cdp_url = cdp_url
        self.launch_browser = launch_browser
        self.browser_path = browser_path
        self.profile_dir = profile_dir
        self._tick_min_seconds = tick_min_seconds
        self._tick_max_seconds = tick_max_seconds

        self._playwright = None
        self._browser = None
        self._page = None
        self._running = False
        self._attached = False
        self._thread = None
        self._started = threading.Event()
        # Why the client is not running: the attach that failed, or the tab
        # that went away. "" while healthy. A host shows it to the operator.
        self.last_error = ""

        # What the page was showing last tick, so a logout or a challenge is
        # logged once on the transition instead of every few seconds forever.
        self._page_state = None
        # What _page_state is about: where the tab went for wrong_page, the
        # read error for page_unreadable, "" otherwise.
        self._page_detail = ""
        # When the tab last moved off the chat page, and until when a failed
        # return to it is on hold. See _should_return_to_chat.
        self._wrong_page_since = 0.0
        self._return_blocked_until = 0.0

        # Per-fan progress: the last message handed out (by server-assigned
        # sequence) and the roster preview it was handed out at -- the whole
        # dedup mechanism, see tracker.py. Memory-only unless the host names a
        # file, so a client built in a test never touches real data.
        self._marks = InboundMarks(marks_path)
        # sec_uid -> time before which _scan_inbound will not click that
        # conversation open again (see inbound._REOPEN_COOLDOWN_SECONDS).
        self._reopen_after = {}
        # Fans whose "message ids unreadable" warning was already logged, so
        # a page Douyin changed does not flood the log every tick.
        self._ids_unreadable_warned = set()
        # sec_uid -> when its open panel was first seen scrolled away from its
        # newest message this episode (see _note_behind).
        self._behind_since = {}

        # Off until the host asks, as with Wcf: a client nobody reads from
        # must not advance any fan's mark past messages it never handed out.
        self._receiving = False
        self._msg_queue = queue.Queue()
        self._commands = queue.Queue()
        self._sent_listeners: List[SentListener] = []

        self._roster_lock = threading.Lock()
        self._contacts = []
        self._roster_version = 0

    # ================================================================ public
    def start(self) -> bool:
        """Attach to the debug browser, starting it first if it is not up.

        Blocks until the attach has either worked or failed, and never raises:
        a browser that cannot be started or attached to is reported through
        ``last_error`` and ``False``, and the host keeps running. ``True``
        means the page thread is up and owns a Douyin tab -- not that the tab
        is logged in; that is what ``get_status()`` keeps reporting.
        """
        if self._thread is not None and self._thread.is_alive():
            return self._running
        self._started.clear()
        self._attached = False
        self._thread = threading.Thread(target=self._main, name="Douyin4uPage", daemon=True)
        self._thread.start()
        self._started.wait()
        # The attach's own answer, not ``_running``: a page thread that died
        # right after attaching still attached, and ``is_running`` / ``last_error``
        # are what report that it has since stopped.
        return self._attached

    @property
    def is_running(self) -> bool:
        return self._running

    def is_login(self) -> bool:
        """Not known to be logged out. ``wcferry``'s question, answered from
        the page: a tab on the wrong page may well still hold a session."""
        return self._running and self._page_state not in (None, PAGE_LOGGED_OUT)

    def is_ready(self) -> bool:
        """Is the chat page up, logged in and rendered -- will a send be tried?"""
        return self._running and self._page_state == PAGE_READY

    def get_status(self) -> dict:
        """A live-health snapshot: ``running``, ``page_state``, ``page_detail``.

        Reads two plain attributes the page thread maintains for its own
        logging -- never touches the page, never blocks, safe from any
        thread. ``page_state`` is one of ``PAGE_STATES``, or ``"starting"``
        before the first tick has run (a host asking in the first instant
        after startup is a real case, not an edge case worth a wrong answer).
        """
        return {
            "running": self._running,
            "page_state": self._page_state or "starting",
            "page_detail": self._page_detail,
        }

    def enable_receiving_msg(self) -> bool:
        self._receiving = True
        return True

    def disable_recv_msg(self) -> int:
        self._receiving = False
        return 0

    def is_receiving_msg(self) -> bool:
        return self._receiving

    def get_msg(self, block: bool = True) -> "DyMsg":
        """The next new message. Raises ``queue.Empty`` after a second of
        nothing, exactly as ``Wcf.get_msg`` does, so the same pump reads both."""
        return self._msg_queue.get(block, timeout=1)

    def get_contacts(self) -> list:
        """The one-to-one conversations on screen at the last inbound scan.

        Only what the roster currently renders -- there is no address book
        behind the page to ask (see the RPA notes). A host that wants a
        catalog accumulates these; ``roster_version`` says when to look again.
        """
        with self._roster_lock:
            return [dict(c) for c in self._contacts]

    @property
    def roster_version(self) -> int:
        return self._roster_version

    def add_sent_listener(self, callback: SentListener) -> None:
        """Be told of every line that reached the page, as ``(sec_uid, line)``.

        Called on the page thread the moment a line is confirmed on screen,
        before the next panel read can see it come back as the account's own
        message -- which is what lets a host recognise that echo for what it
        is. A listener must be quick and must not call back into the client.
        """
        if callback not in self._sent_listeners:
            self._sent_listeners.append(callback)

    def send_text(self, msg: str, receiver: str, timeout: Optional[float] = None) -> SendResult:
        """Type *msg* into *receiver*'s conversation, one message per line.

        Safe from any thread: the typing happens on the page thread, humanized
        like every other keystroke this client makes, and this call waits for
        the outcome. No quota or quiet-hours rule is applied here -- which
        sends are autonomous is the host's knowledge, not this client's (see
        ``humanize.SendBudget``).

        After ``timeout`` seconds a send that has not started is withdrawn
        (``STATUS_TIMEOUT``: nothing went out, and nothing will). One that is
        already typing is waited out instead: cutting it off would leave half
        a reply on the fan's screen and a composer full of the rest.
        """
        receiver = (receiver or "").strip()
        total = len(reply_lines(msg))
        if not receiver or not total:
            return SendResult(STATUS_EMPTY, 0, total)
        if not self._running:
            return SendResult(STATUS_NOT_RUNNING, 0, total)

        command = _SendCommand(receiver, msg, total)
        self._commands.put(command)
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            slice_seconds = _SEND_WAIT_SLICE_SECONDS
            if deadline is not None:
                slice_seconds = max(0.0, min(slice_seconds, deadline - time.monotonic()))
            if command.done.wait(slice_seconds):
                return command.result
            # A page thread that stopped after the check above never runs
            # this command, and nothing else would ever answer it.
            if not self._running and command.cancel():
                return SendResult(STATUS_NOT_RUNNING, 0, total)
            if deadline is not None and time.monotonic() >= deadline:
                break
        if command.cancel():
            logger.warning(
                f"[Douyin4u] send to {receiver} withdrawn after {timeout:.0f}s waiting for the "
                "page -- it may be logged out or challenged; nothing was sent"
            )
            return SendResult(STATUS_TIMEOUT, 0, total)
        # Already typing: the page thread finishes every command it claims.
        command.done.wait()
        return command.result

    def cleanup(self) -> None:
        """Ask the page thread to stop. Deliberately touches nothing else.

        Every Playwright object here belongs to the page thread, so closing the
        connection from the caller's thread would be the exact cross-thread use
        this client's whole design avoids. Setting the flag is the only safe
        cross-thread action; the loop tears its own connection down on the way
        out -- and only its connection: the operator's browser stays open.
        """
        self._running = False

    stop = cleanup

    # ============================================================ page thread
    def _main(self):
        try:
            attached = self._attach()
        except Exception as e:
            self.last_error = f"could not attach to the browser: {e}"
            logger.error(f"[Douyin4u] {self.last_error}")
            attached = False
        if not attached:
            self._teardown()
            self._started.set()
            return
        self.last_error = ""
        self._running = True
        self._attached = True
        self._started.set()
        logger.info("[Douyin4u] attached; watching the chat page")
        self._run_loop()

    def _attach(self) -> bool:
        """Launch-if-needed, attach, and find a Douyin tab. False says why in
        ``last_error``; every early return after Playwright started leaves
        the teardown to ``_main``, on this thread -- the driver is a node child
        of the host process, and nothing else would ever stop one a failed
        start abandoned."""
        launched = False
        if not cdp_port_open(self.cdp_url):
            if not self.launch_browser:
                self.last_error = (
                    f"no browser is listening at {self.cdp_url}, and starting one is "
                    "switched off - start it yourself with --remote-debugging-port on "
                    f"that port, open {CHAT_URL} and log in."
                )
                logger.warning(f"[Douyin4u] {self.last_error}")
                return False
            launched, detail = launch_debug_browser(self.cdp_url, self.browser_path, self.profile_dir)
            if not launched:
                self.last_error = detail
                logger.warning(f"[Douyin4u] {detail}")
                return False

        logger.info(f"[Douyin4u] connecting to the browser at {self.cdp_url}...")
        try:
            self._playwright = _sync_playwright().start()
            self._browser = self._playwright.chromium.connect_over_cdp(self.cdp_url)
        except Exception as e:
            self.last_error = str(e)
            logger.warning(f"[Douyin4u] could not attach to a browser at {self.cdp_url}: {e}")
            return False

        self._page = self._find_or_open_chat_page(
            _LAUNCHED_TAB_WAIT_SECONDS if launched else 0.0
        )
        if self._page is None:
            self.last_error = (
                f"no browser tab is on a Douyin page; open {CHAT_URL} "
                "and log in, then start again."
            )
            logger.warning(f"[Douyin4u] {self.last_error}")
            return False
        return True

    def _find_or_open_chat_page(self, settle_seconds: float = 0.0):
        """The tab on the chat page, else any Douyin tab.

        The fallback still attaches, so a tab that was merely clicked away
        reports ``wrong_page`` and is navigated back by ``_tick``, rather
        than failing the whole start.

        ``settle_seconds`` is for a browser this client has just started
        itself: the port answers as soon as the browser is up, seconds before
        its first tab has finished loading the chat page, and giving up in
        that window would report "no tab is on the chat page" about a tab that
        was on its way. It stays 0 for a browser that was already running.
        """
        deadline = time.monotonic() + settle_seconds
        while True:
            page = self._first_douyin_page()
            if page is not None or time.monotonic() >= deadline:
                return page
            time.sleep(_TAB_POLL_SECONDS)

    def _first_douyin_page(self):
        """One pass over the attached browser's tabs. See _find_or_open_chat_page."""
        douyin_tab = None
        for ctx in self._browser.contexts:
            for pg in ctx.pages:
                try:
                    url = pg.url or ""
                except Exception as e:
                    logger.debug(f"[Douyin4u] skipping a tab whose URL could not be read: {e}")
                    continue
                if _on_chat_page(url):
                    return pg
                if douyin_tab is None and _on_douyin_site(url):
                    douyin_tab = pg
        return douyin_tab

    def _run_loop(self):
        """Own the page for the client's whole lifetime.

        One thread, one loop: confirm the page is still usable, run queued
        sends, scan for inbound messages, then yield. ``page.wait_for_timeout``
        (not ``time.sleep``) so Playwright's own event pump keeps running.

        Real work happens only in ``PAGE_READY``. A logged-out or challenged
        page is left strictly alone, checked on a slow cadence and nothing
        more: the one thing worse than an idle client is one that keeps
        clicking at a site which has just asked it to prove it is human.
        """
        while self._running:
            try:
                self._tick()
            except Exception as e:
                logger.error(f"[Douyin4u] loop iteration failed: {e}")
                logger.exception(e)
            try:
                self._idle(self._next_tick_ms())
            except Exception as e:
                # Recorded so a host's "not running" says why, instead of
                # looking like nobody ever started it.
                self.last_error = f"the attached tab is gone ({e}); reopen the chat page and start again"
                logger.error(f"[Douyin4u] page unavailable, stopping: {e}")
                self._running = False
        self._fail_pending_sends()
        self._teardown()

    def _idle(self, total_ms: int):
        """Wait out the gap between ticks -- but not past a send that is waiting.

        The wait is sliced, and a queued send cuts the current slice short.
        An early wake runs sends and nothing else. It deliberately does not
        scan: scanning clicks conversations, and that cadence is jittered on
        purpose (see ``_next_tick_ms``), so no shortcut here is allowed to
        make the client click more often than it already does. The deadline
        is wall-clock, so the seconds a humanized send spends typing count
        against this wait instead of being added to it.

        While the page is not ``ready`` there is nothing to send and nothing
        worth re-checking every half second, so the wait stays a single sleep.
        """
        if self._page_state != PAGE_READY:
            self._page.wait_for_timeout(total_ms)
            return
        deadline = time.monotonic() + total_ms / 1000.0
        next_read = time.monotonic() + _FAST_READ_MS / 1000.0
        while self._running:
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                return
            self._page.wait_for_timeout(min(_COMMAND_POLL_MS, remaining_ms))
            if self._receiving and time.monotonic() >= next_read:
                next_read = time.monotonic() + _FAST_READ_MS / 1000.0
                try:
                    self.catch_up_open_conversation()
                except Exception as e:
                    # Out in _run_loop this call would sit where only "the tab
                    # is gone" is ever raised, and that handler stops for good.
                    logger.error(f"[Douyin4u] between-tick read failed: {e}")
            if self._commands.empty():
                continue
            state = self._detect_page_state()
            self._note_page_state(state)
            if state != PAGE_READY:
                return
            try:
                self._run_sends()
            except Exception as e:
                # Same reasoning: a failed send is not "the tab is gone".
                logger.error(f"[Douyin4u] running queued sends failed: {e}")
                logger.exception(e)

    def _tick(self):
        """One pass: classify the page, bring a wandered tab home, then work.

        The only automatic recovery is ``wrong_page``, because it is the only
        state a navigation can fix. A logout or a challenge needs a human, and
        navigating at either would be the bot-shaped move this client avoids.
        """
        state = self._detect_page_state()
        self._note_page_state(state)
        if state == PAGE_WRONG_PAGE and self._should_return_to_chat(time.time()):
            self._return_to_chat_page()
            state = self._detect_page_state()
            self._note_page_state(state)
        if state == PAGE_READY:
            self._run_sends()
            if self._receiving:
                self._scan_inbound()

    def _run_sends(self):
        """Run every send queued so far, oldest first, on this thread.

        Every command is finished whatever happens to it -- a caller blocked in
        ``send_text`` must never be left waiting on one that raised.
        """
        while True:
            try:
                command = self._commands.get_nowait()
            except queue.Empty:
                return
            if not command.claim():
                continue  # withdrawn by a caller that stopped waiting
            result = SendResult(STATUS_FAILED, 0, command.total)
            try:
                sent = self._send_now(command.receiver, command.text)
                status = STATUS_OK if sent >= command.total else (STATUS_PARTIAL if sent else STATUS_FAILED)
                result = SendResult(status, sent, command.total)
            except Exception as e:
                logger.error(f"[Douyin4u] send to {command.receiver} failed: {e}")
            finally:
                command.finish(result)

    def _fail_pending_sends(self):
        """Answer every send still queued when the loop ends: none will run."""
        while True:
            try:
                command = self._commands.get_nowait()
            except queue.Empty:
                return
            if command.claim():
                command.finish(SendResult(STATUS_NOT_RUNNING, 0, command.total))

    def _notify_sent(self, sec_uid: str, line: str):
        for callback in list(self._sent_listeners):
            # One listener's failure must not fail a line that did go out.
            try:
                callback(sec_uid, line)
            except Exception as e:
                logger.error(f"[Douyin4u] sent-listener {callback} failed: {e}")

    def _should_return_to_chat(self, now: float) -> bool:
        """Has the tab been away long enough, and is a retry not on hold?"""
        if now - self._wrong_page_since < _WRONG_PAGE_GRACE_SECONDS:
            return False
        return now >= self._return_blocked_until

    def _return_to_chat_page(self, now: float = None):
        """Navigate the attached tab back to the chat page.

        Waited on ``"load"`` because the IM widget's open WebSocket means
        ``networkidle`` never arrives. A navigation that throws or ends up
        anywhere else (a redirect) puts retries on hold for
        ``_RETURN_RETRY_SECONDS``: forcing a navigation every tick at a page
        that keeps refusing it is a bot pattern of its own.
        """
        now = time.time() if now is None else now
        logger.info(f"[Douyin4u] returning the tab to the chat page from {self._page_detail or 'another page'}")
        try:
            self._page.goto(CHAT_URL, wait_until="load", timeout=_NAVIGATE_TIMEOUT_MS)
            self._page.wait_for_timeout(_SPA_SETTLE_MS)
            landed = self._page.url or ""
        except Exception as e:
            landed = ""
            logger.warning(f"[Douyin4u] could not return the tab to the chat page: {e}")
        if _on_chat_page(landed):
            return
        self._return_blocked_until = now + _RETURN_RETRY_SECONDS
        logger.warning(
            f"[Douyin4u] the tab is not back on the chat page (now {landed or 'unreadable'}); "
            f"next attempt in {int(_RETURN_RETRY_SECONDS)}s at the earliest"
        )

    def _next_tick_ms(self) -> int:
        """How long to wait before the next pass.

        Resampled every tick rather than fixed: polling on an exactly constant
        beat is a bot signature in its own right, independent of anything the
        client types. While the page is unusable the cadence drops to a
        deliberately slow constant -- there is nothing to do but wait for a
        human, and precision buys nothing.
        """
        if self._page_state != PAGE_READY:
            return int(_BLOCKED_TICK_SECONDS * 1000)
        return sample_tick_ms(self._tick_min_seconds, self._tick_max_seconds)

    def _detect_page_state(self) -> str:
        """Classify what the attached tab is currently showing.

        Every outcome is one of ``PAGE_STATES``, and only a fully rendered
        chat page is ``PAGE_READY``: anything else is a page this must not
        type into. A challenge outranks a logout because it is both the more
        urgent thing to stop for and the more informative thing to log; both
        outrank the URL, since a logout can happen on any route. Sets
        ``_page_detail`` alongside, for the states that need one.
        """
        try:
            url = self._page.url or ""
            state = json.loads(self._page.evaluate(JS_DETECT_STATE))
        except Exception as e:
            self._page_detail = str(e)
            return PAGE_UNREADABLE

        self._page_detail = ""
        if state.get("challenge"):
            return PAGE_CHALLENGE
        if state.get("logged_out"):
            return PAGE_LOGGED_OUT
        if not _on_chat_page(url):
            self._page_detail = url.split("?", 1)[0]
            return PAGE_WRONG_PAGE
        if state.get("chat_ready"):
            return PAGE_READY
        if state.get("ready_state") != "complete":
            return PAGE_LOADING
        return PAGE_CHAT_NOT_RENDERED

    def _note_page_state(self, state: str):
        """Log a page-state change once, on the transition, by its name.

        A logout persists until a human fixes it, so logging it every tick
        would bury everything else in the log within minutes.
        """
        if state == self._page_state:
            return
        # Nothing about inbound tracking is reset here. Positions used to be,
        # every time the page left "ready" (even for one unreadable tick),
        # which re-baselined every fan and silently skipped whatever arrived
        # meanwhile. A mark is a server-assigned sequence, and a re-render
        # does not change it.
        if state == PAGE_WRONG_PAGE:
            self._wrong_page_since = time.time()
        self._page_state = state
        prefix = f"[Douyin4u] page state {state}:"
        detail = self._page_detail
        if state == PAGE_READY:
            logger.info(f"{prefix} chat page is ready")
        elif state == PAGE_CHALLENGE:
            logger.error(
                f"{prefix} a bot-verification challenge is on screen -- sending is "
                "suspended. Solve it by hand in the attached browser; do not "
                "restart at it."
            )
        elif state == PAGE_LOGGED_OUT:
            logger.error(
                f"{prefix} the Douyin session is logged out -- sending is "
                "suspended until someone logs back in by hand in the attached "
                "browser."
            )
        elif state == PAGE_WRONG_PAGE:
            logger.warning(
                f"{prefix} the attached tab is on {detail or 'another page'}, not the "
                f"chat page -- returning it in about {int(_WRONG_PAGE_GRACE_SECONDS)}s"
            )
        elif state == PAGE_LOADING:
            logger.info(f"{prefix} the chat page is still loading")
        elif state == PAGE_CHAT_NOT_RENDERED:
            logger.warning(
                f"{prefix} the chat page loaded but shows no conversation list, tabs "
                "or composer -- reload the tab; if it persists, Douyin changed the "
                "page and page_scripts.py's selectors need updating"
            )
        else:
            logger.error(f"{prefix} could not read the attached tab: {detail}")

    def _teardown(self):
        """Close this process's Playwright connection, on the page thread."""
        try:
            if self._playwright is not None:
                # connect_over_cdp attached to a browser it did not launch;
                # stopping the driver closes only this process's connection,
                # never the operator's actual browser window.
                self._playwright.stop()
        except Exception as e:
            logger.debug(f"[Douyin4u] teardown: {e}")
        finally:
            self._playwright = None
            self._browser = None
            self._page = None

    # ----------------------------------------------------------- page reads
    def _read_roster(self) -> list:
        try:
            raw = self._page.evaluate(JS_READ_ROSTER)
        except Exception as e:
            logger.error(f"[Douyin4u] roster read failed: {e}")
            return []
        try:
            return json.loads(raw)
        except Exception as e:
            logger.error(f"[Douyin4u] roster JSON malformed: {e}")
            return []

    def _read_panel(self):
        """The open panel's rows, or None when the read itself failed.

        None and "an empty panel" must stay distinguishable: every caller
        treats None as "change nothing this tick", while an empty list is a
        real answer about the page.
        """
        try:
            raw = self._page.evaluate(JS_READ_PANEL)
        except Exception as e:
            logger.error(f"[Douyin4u] panel read failed: {e}")
            return None
        try:
            return json.loads(raw)
        except Exception as e:
            logger.error(f"[Douyin4u] panel JSON malformed: {e}")
            return None

    def _note_roster(self, roster: list) -> None:
        """Keep the one-to-one rows of this roster read for ``get_contacts``."""
        contacts = [
            {
                "sec_uid": row["sec_uid"],
                "nickname": row.get("nickname") or "",
                "is_stranger": bool(row.get("is_stranger")),
            }
            for row in roster
            if row.get("sec_uid") and not row.get("is_group")
        ]
        with self._roster_lock:
            self._contacts = contacts
            self._roster_version += 1

    def _open_conversation(self, index: int, sec_uid: str) -> bool:
        """Click roster row *index* and verify it is really *sec_uid* now.

        The verification re-reads the roster (cheap) rather than trusting
        that the row clicked is still the one at that position -- the list can
        reorder between reading it and clicking it as new messages arrive. A
        mismatch means "don't touch it", never "best guess and proceed":
        misrouting a reply to the wrong fan is the one mistake this client
        cannot silently make.

        The tab is brought to the front here because both directions click
        through this -- reads (``evaluate()``) keep working on a background
        tab, but a click does not: "element is not visible" the moment the
        operator has another tab active in the same window (live 2026-09-16).
        """
        try:
            self._page.bring_to_front()
        except Exception as e:
            logger.warning(f"[Douyin4u] could not bring the tab to the front: {e}")
        try:
            self._page.locator(_SEL_CONVERSATION_ROW).nth(index).click(timeout=8000)
        except Exception as e:
            logger.warning(f"[Douyin4u] could not click conversation row {index}: {e}")
            return False
        self._page.wait_for_timeout(600)
        roster = self._read_roster()
        active = next((r for r in roster if r.get("is_active")), None)
        if not active or active.get("sec_uid") != sec_uid:
            logger.warning(
                f"[Douyin4u] opened conversation does not match expected sec_uid "
                f"(wanted {sec_uid!r}, active is {active and active.get('sec_uid')!r}); skipping"
            )
            return False
        return True

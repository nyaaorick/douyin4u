# encoding:utf-8

"""Behavioural timing and send quotas for driving the Douyin chat page.

Why this module exists, concretely: during Phase 0 probing on 2026-09-15 the
test account was logged out mid-session, with a ByteDance bot-verification
widget appearing immediately beforehand. The automation's timing signature is
the near-certain cause. ``page.keyboard.type(text, delay=25)`` emits one
keystroke every 25ms with *zero* variance -- no human hand produces that --
and the channel polled the page on an exactly fixed cadence. Both are visible
to Douyin's own page telemetry, unlike the React fiber reads this channel
does through ``evaluate()``, which never leave the browser and cannot be
observed server-side at all. Timing was the cheapest thing to get wrong and
is the cheapest thing to fix, so it is fixed here first.

Everything below is a pure function of its inputs plus an injected ``rng`` or
clock: **nothing sleeps and nothing touches a page**. The caller does the
waiting through ``page.wait_for_timeout()`` so Playwright's event pump keeps
running, which also keeps every number here unit-testable without a browser --
the same split the client's own tests rely on (``tests/fakes.py``).

The distribution shapes are module constants rather than config keys on
purpose. They are the behavioural core, and an operator tuning them without
telemetry to evaluate the result is more likely to make the signature worse
than better. What is config-facing is the coarse per-account stuff an
operator genuinely needs to set -- poll cadence bounds and send quotas -- and
those are constructor arguments: the poll bounds on :class:`douyin4u.Douyin`,
the quotas on :class:`SendBudget`, which the host application owns because
only it knows which sends are autonomous replies and which a human chose.

None of this defeats a determined anti-bot system. It removes the specific
tells we have evidence for; it does not make the channel undetectable, and
nothing here changes the fact that driving the site this way is outside
Douyin's terms of service.
"""

import random
from collections import deque
from typing import Optional, Tuple

_MINUTES_PER_DAY = 24 * 60
_SECONDS_PER_HOUR = 3600
_SECONDS_PER_DAY = 86400

# Inter-keystroke interval. Mean sits in the 80-150ms band typical of an
# unhurried touch typist; the floor is deliberately well above the old fixed
# 25ms, which is faster than a human sustains.
_KEYSTROKE_MEAN_MS = 110.0
_KEYSTROKE_STDDEV_MS = 35.0
_KEYSTROKE_MIN_MS = 45
_KEYSTROKE_MAX_MS = 320

# Occasional longer "thinking" gap. People pause far more often after a
# clause ends than mid-word, so the chance is conditioned on the character
# just typed -- a uniform sprinkle of pauses is its own unnatural signature.
_PAUSE_AFTER_PUNCTUATION_CHANCE = 0.35
_PAUSE_MID_TEXT_CHANCE = 0.05
_PAUSE_MEAN_MS = 520.0
_PAUSE_STDDEV_MS = 200.0
_PAUSE_MIN_MS = 180
_PAUSE_MAX_MS = 1400
_PUNCTUATION = "。，、；：！？…—.,;:!?"

# Gap between finishing a line and pressing Enter -- the beat where a person
# re-reads what they just wrote.
_PRE_ENTER_MEAN_MS = 420.0
_PRE_ENTER_STDDEV_MS = 150.0
_PRE_ENTER_MIN_MS = 150
_PRE_ENTER_MAX_MS = 1200

# Pause between opening a conversation and starting to type: reading time.
# Scaled by the reply's length as a stand-in for how much there was to think
# about -- the channel does not know the inbound message's length at this
# point, and the agent's own LLM latency already contributes several seconds
# of genuinely variable delay ahead of this.
_READING_BASE_MS = 800
_READING_PER_CHAR_MS = 18
_READING_JITTER = 0.35
_READING_MIN_MS = 600
_READING_MAX_MS = 6000

# Gap between the separate messages of one multi-line reply, scaled by how
# much is about to be typed next.
_INTER_LINE_BASE_MS = 900
_INTER_LINE_PER_CHAR_MS = 22
_INTER_LINE_JITTER = 0.30
_INTER_LINE_MIN_MS = 700
_INTER_LINE_MAX_MS = 5000


def _clamp(value: float, low: int, high: int) -> int:
    return int(max(low, min(high, value)))


def _rng(rng):
    """Default to the module-level ``random``; tests inject a seeded Random."""
    return rng if rng is not None else random


def sample_keystroke_ms(rng=None) -> int:
    """How long to wait before typing the next character."""
    r = _rng(rng)
    return _clamp(
        r.gauss(_KEYSTROKE_MEAN_MS, _KEYSTROKE_STDDEV_MS),
        _KEYSTROKE_MIN_MS,
        _KEYSTROKE_MAX_MS,
    )


def sample_thinking_pause_ms(prev_char: str, rng=None) -> int:
    """An extra gap after *prev_char*, or 0 for no pause.

    Conditioned on the character just typed: a pause is much likelier once a
    clause has closed. Returning 0 rather than a tiny number keeps the caller
    free to skip the wait entirely.
    """
    r = _rng(rng)
    chance = (
        _PAUSE_AFTER_PUNCTUATION_CHANCE
        if prev_char and prev_char in _PUNCTUATION
        else _PAUSE_MID_TEXT_CHANCE
    )
    if r.random() >= chance:
        return 0
    return _clamp(
        r.gauss(_PAUSE_MEAN_MS, _PAUSE_STDDEV_MS), _PAUSE_MIN_MS, _PAUSE_MAX_MS
    )


def sample_pre_enter_ms(rng=None) -> int:
    """The beat between finishing a line and sending it."""
    r = _rng(rng)
    return _clamp(
        r.gauss(_PRE_ENTER_MEAN_MS, _PRE_ENTER_STDDEV_MS),
        _PRE_ENTER_MIN_MS,
        _PRE_ENTER_MAX_MS,
    )


def _scaled_with_jitter(base_ms: int, per_char_ms: int, length: int,
                        jitter: float, low: int, high: int, rng) -> int:
    r = _rng(rng)
    nominal = base_ms + per_char_ms * max(0, length)
    factor = 1.0 + r.uniform(-jitter, jitter)
    return _clamp(nominal * factor, low, high)


def sample_reading_ms(reply_length: int, rng=None) -> int:
    """Pause after opening a conversation, before typing into it."""
    return _scaled_with_jitter(
        _READING_BASE_MS, _READING_PER_CHAR_MS, reply_length,
        _READING_JITTER, _READING_MIN_MS, _READING_MAX_MS, rng,
    )


def sample_inter_line_ms(next_line_length: int, rng=None) -> int:
    """Pause between two messages of the same multi-line reply."""
    return _scaled_with_jitter(
        _INTER_LINE_BASE_MS, _INTER_LINE_PER_CHAR_MS, next_line_length,
        _INTER_LINE_JITTER, _INTER_LINE_MIN_MS, _INTER_LINE_MAX_MS, rng,
    )


def sample_tick_ms(min_seconds: float, max_seconds: float, rng=None) -> int:
    """The main loop's next sleep, in milliseconds.

    A fixed poll interval is a bot signature in its own right -- a person does
    not check their inbox every 2.000 seconds. Bounds swapped or non-positive
    are corrected rather than trusted, so a typo in config cannot turn the
    loop into a busy-wait against the live site.
    """
    r = _rng(rng)
    low = max(0.2, float(min_seconds))
    high = max(low, float(max_seconds))
    return int(r.uniform(low, high) * 1000)


def parse_quiet_hours(spec: Optional[str]) -> Optional[Tuple[int, int]]:
    """``"01:00-08:00"`` -> ``(60, 480)`` in minutes since midnight.

    Empty or unset means "no quiet hours" and returns None. A *non-empty* but
    unparseable value raises ValueError instead of being silently ignored: the
    operator asked for a restriction, and quietly dropping it would have the
    avatar replying through the night exactly as if it had never been
    configured. The channel validates this once at startup and refuses to run
    on a bad value, rather than discovering it at 3am.
    """
    if not spec or not str(spec).strip():
        return None
    text = str(spec).strip()
    try:
        start_text, end_text = text.split("-")
        start = _parse_hhmm(start_text)
        end = _parse_hhmm(end_text)
    except ValueError:
        raise ValueError(f"quiet hours must look like '01:00-08:00', got {spec!r}")
    return (start, end)


def _parse_hhmm(text: str) -> int:
    hours_text, minutes_text = text.strip().split(":")
    hours = int(hours_text)
    minutes = int(minutes_text)
    if not (0 <= hours < 24 and 0 <= minutes < 60):
        raise ValueError(f"not a valid time of day: {text!r}")
    return hours * 60 + minutes


def in_quiet_hours(minutes_since_midnight: int, window: Optional[Tuple[int, int]]) -> bool:
    """Is *minutes_since_midnight* inside *window*?

    Handles a window that wraps past midnight (``23:00-07:00``), which is the
    normal shape for a sleep schedule. A window whose ends are equal is
    treated as empty, not as "all day".
    """
    if window is None:
        return False
    start, end = window
    now = minutes_since_midnight % _MINUTES_PER_DAY
    if start == end:
        return False
    if start < end:
        return start <= now < end
    return now >= start or now < end


class SendBudget:
    """Rolling per-hour and per-day caps on outbound messages.

    A digital avatar that answers every fan instantly, around the clock, is
    not behaving like the person it stands in for -- the volume and the
    tirelessness are both signals independent of any per-keystroke timing.

    State is a single deque of send timestamps; the caller injects ``now``
    (epoch seconds) on every call, so the whole class is testable without
    sleeping or patching the clock. A cap of zero or less blocks sending
    entirely rather than meaning "unlimited": a quota accidentally set to 0
    should stop the avatar, never uncork it.
    """

    def __init__(self, max_per_hour: int, max_per_day: int):
        self.max_per_hour = int(max_per_hour)
        self.max_per_day = int(max_per_day)
        self._sent = deque()

    def _prune(self, now: float) -> None:
        cutoff = now - _SECONDS_PER_DAY
        while self._sent and self._sent[0] <= cutoff:
            self._sent.popleft()

    def allow(self, now: float) -> bool:
        """Is there room to send one more message right now?"""
        if self.max_per_hour <= 0 or self.max_per_day <= 0:
            return False
        self._prune(now)
        if len(self._sent) >= self.max_per_day:
            return False
        hour_cutoff = now - _SECONDS_PER_HOUR
        recent = sum(1 for stamp in self._sent if stamp > hour_cutoff)
        return recent < self.max_per_hour

    def record(self, now: float) -> None:
        """Count one message as sent."""
        self._prune(now)
        self._sent.append(now)

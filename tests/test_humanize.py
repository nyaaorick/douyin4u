# encoding:utf-8

"""Timing samplers and send quotas.

Every function under test is pure given an injected ``rng`` or ``now``, which
is the whole point of the module: the channel's anti-detection behaviour can
be pinned down here without a browser, a clock, or a single ``sleep``.

The assertions are about *bounds and shape*, not exact draws -- pinning an
exact millisecond from a seeded generator would just re-encode CPython's
Mersenne Twister into the test suite and break on any distribution tweak.
"""

import os
import random
import sys

import pytest


from douyin4u.humanize import (
    SendBudget,
    in_quiet_hours,
    parse_quiet_hours,
    sample_inter_line_ms,
    sample_keystroke_ms,
    sample_pre_enter_ms,
    sample_reading_ms,
    sample_thinking_pause_ms,
    sample_tick_ms,
)

_HOUR = 3600
_DAY = 86400


# ------------------------------------------------------------------ keystrokes
def test_keystroke_delays_stay_inside_their_clamp():
    rng = random.Random(7)
    samples = [sample_keystroke_ms(rng) for _ in range(2000)]
    assert all(45 <= value <= 320 for value in samples)


def test_keystroke_delays_actually_vary():
    """The defect being fixed: a fixed 25ms delay is what the risk-control
    system had to look at. A sampler that returned one value would be no
    better than the constant it replaced."""
    rng = random.Random(7)
    samples = {sample_keystroke_ms(rng) for _ in range(200)}
    assert len(samples) > 50


def test_keystroke_delays_are_never_as_fast_as_the_old_fixed_value():
    rng = random.Random(11)
    assert min(sample_keystroke_ms(rng) for _ in range(2000)) > 25


# --------------------------------------------------------------- thinking gaps
def test_a_pause_is_much_likelier_after_punctuation_than_mid_text():
    """A uniform sprinkle of pauses would be its own unnatural signature --
    people pause when a clause closes."""
    rng = random.Random(3)
    after_punctuation = sum(1 for _ in range(4000) if sample_thinking_pause_ms("。", rng))
    rng = random.Random(3)
    mid_text = sum(1 for _ in range(4000) if sample_thinking_pause_ms("好", rng))

    assert after_punctuation > mid_text * 3


def test_a_thinking_pause_is_either_zero_or_a_real_pause():
    rng = random.Random(5)
    samples = [sample_thinking_pause_ms("，", rng) for _ in range(2000)]
    assert all(value == 0 or 180 <= value <= 1400 for value in samples)


def test_an_empty_previous_character_still_samples_without_raising():
    assert sample_thinking_pause_ms("", random.Random(1)) >= 0


# ------------------------------------------------------------------- pre-Enter
def test_pre_enter_delay_stays_inside_its_clamp():
    rng = random.Random(13)
    samples = [sample_pre_enter_ms(rng) for _ in range(2000)]
    assert all(150 <= value <= 1200 for value in samples)


# --------------------------------------------------------- length-scaled gaps
def test_reading_time_grows_with_a_longer_reply():
    short = [sample_reading_ms(5, random.Random(seed)) for seed in range(60)]
    long = [sample_reading_ms(200, random.Random(seed)) for seed in range(60)]
    assert sum(long) / len(long) > sum(short) / len(short)


def test_reading_time_is_capped_for_an_enormous_reply():
    assert sample_reading_ms(100000, random.Random(2)) <= 6000


def test_reading_time_has_a_floor_for_an_empty_reply():
    assert sample_reading_ms(0, random.Random(2)) >= 600


def test_a_negative_length_is_treated_as_empty_not_as_a_negative_delay():
    assert sample_reading_ms(-50, random.Random(2)) >= 600


def test_inter_line_gap_grows_with_the_next_lines_length():
    short = [sample_inter_line_ms(3, random.Random(seed)) for seed in range(60)]
    long = [sample_inter_line_ms(150, random.Random(seed)) for seed in range(60)]
    assert sum(long) / len(long) > sum(short) / len(short)


def test_inter_line_gap_stays_inside_its_clamp():
    rng = random.Random(17)
    samples = [sample_inter_line_ms(40, rng) for _ in range(500)]
    assert all(700 <= value <= 5000 for value in samples)


# ------------------------------------------------------------------ poll cadence
def test_tick_lands_between_the_configured_bounds():
    rng = random.Random(19)
    samples = [sample_tick_ms(2.0, 5.0, rng) for _ in range(500)]
    assert all(2000 <= value <= 5000 for value in samples)


def test_tick_varies_rather_than_polling_on_a_fixed_beat():
    rng = random.Random(19)
    samples = {sample_tick_ms(2.0, 5.0, rng) for _ in range(200)}
    assert len(samples) > 50


def test_swapped_bounds_do_not_produce_a_negative_or_inverted_window():
    rng = random.Random(23)
    samples = [sample_tick_ms(5.0, 2.0, rng) for _ in range(200)]
    assert all(value >= 5000 for value in samples)


def test_a_zero_tick_bound_cannot_turn_the_loop_into_a_busy_wait():
    """A config typo must not have the channel hammering the live site."""
    rng = random.Random(29)
    samples = [sample_tick_ms(0, 0, rng) for _ in range(200)]
    assert all(value >= 200 for value in samples)


# ----------------------------------------------------------------- quiet hours
def test_a_quiet_hours_window_parses_to_minutes_since_midnight():
    assert parse_quiet_hours("01:00-08:00") == (60, 480)


def test_an_unset_quiet_hours_value_means_no_quiet_hours():
    assert parse_quiet_hours("") is None
    assert parse_quiet_hours(None) is None
    assert parse_quiet_hours("   ") is None


@pytest.mark.parametrize("spec", ["01:00", "1pm-2pm", "25:00-08:00", "01:70-08:00", "01:00/08:00"])
def test_a_malformed_quiet_hours_value_raises_rather_than_being_ignored(spec):
    """Silently dropping it would have the avatar replying all night exactly
    as if the operator had never configured anything."""
    with pytest.raises(ValueError):
        parse_quiet_hours(spec)


def test_a_time_inside_the_window_is_quiet():
    window = parse_quiet_hours("01:00-08:00")
    assert in_quiet_hours(3 * 60, window) is True


def test_a_time_outside_the_window_is_not_quiet():
    window = parse_quiet_hours("01:00-08:00")
    assert in_quiet_hours(12 * 60, window) is False


def test_the_window_boundaries_are_half_open():
    window = parse_quiet_hours("01:00-08:00")
    assert in_quiet_hours(60, window) is True
    assert in_quiet_hours(480, window) is False


def test_a_window_that_wraps_past_midnight_still_works():
    """The normal shape for a sleep schedule."""
    window = parse_quiet_hours("23:00-07:00")
    assert in_quiet_hours(23 * 60 + 30, window) is True
    assert in_quiet_hours(2 * 60, window) is True
    assert in_quiet_hours(12 * 60, window) is False


def test_a_window_with_equal_ends_is_empty_not_all_day():
    assert in_quiet_hours(5 * 60, (300, 300)) is False


def test_no_window_configured_is_never_quiet():
    assert in_quiet_hours(3 * 60, None) is False


# ---------------------------------------------------------------- send budget
def test_a_fresh_budget_allows_a_send():
    assert SendBudget(max_per_hour=5, max_per_day=50).allow(1000.0) is True


def test_the_hourly_cap_blocks_the_next_send():
    budget = SendBudget(max_per_hour=3, max_per_day=50)
    for _ in range(3):
        budget.record(1000.0)
    assert budget.allow(1000.0) is False


def test_the_hourly_cap_frees_up_once_the_hour_has_rolled_past():
    budget = SendBudget(max_per_hour=3, max_per_day=50)
    for _ in range(3):
        budget.record(1000.0)
    assert budget.allow(1000.0 + _HOUR + 1) is True


def test_the_daily_cap_still_blocks_after_the_hour_rolls():
    budget = SendBudget(max_per_hour=100, max_per_day=5)
    for index in range(5):
        budget.record(1000.0 + index * _HOUR)
    assert budget.allow(1000.0 + 5 * _HOUR) is False


def test_the_daily_cap_frees_up_once_the_day_has_rolled_past():
    budget = SendBudget(max_per_hour=100, max_per_day=5)
    for index in range(5):
        budget.record(1000.0 + index)
    assert budget.allow(1000.0 + _DAY + 1) is True


@pytest.mark.parametrize("hourly,daily", [(0, 50), (50, 0), (-1, 50), (50, -1)])
def test_a_zero_or_negative_quota_blocks_sending_rather_than_meaning_unlimited(hourly, daily):
    """Fail closed: a quota accidentally set to 0 should stop the avatar,
    never uncork it."""
    assert SendBudget(max_per_hour=hourly, max_per_day=daily).allow(1000.0) is False


def test_old_timestamps_do_not_accumulate_forever():
    budget = SendBudget(max_per_hour=100, max_per_day=1000)
    for index in range(50):
        budget.record(1000.0 + index)
    budget.allow(1000.0 + 2 * _DAY)
    assert len(budget._sent) == 0

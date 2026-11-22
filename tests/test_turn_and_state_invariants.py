# -*- coding: utf-8 -*-
"""Black-box & property-based invariant tests for pikapet.turn and pikapet.pet_state.

Mirrors the state-machine safety invariants formally verified in Lean 4 and fuzzes
edge cases (NaN, Infinity, rapid cross-head mouse jitter, 10k-step trajectories).
"""
import json
import math
import random

import pytest

from pikapet import pet_state
from pikapet.turn import TurnDirector, frame_index


def test_turn_director_10k_step_fuzz_no_mirror_teleport_and_bounds():
    """10,000-step randomized mouse trajectory fuzz test:
    1. No mirror teleportation: direction only changes when prev_pose <= SWITCH_EPS.
    2. Pose boundedness: 0.0 <= pose <= 1.0 on every frame.
    """
    rng = random.Random(20261122)
    t = 0.0
    director = TurnDirector(clock=lambda: t)

    for _ in range(10_000):
        prev_pose = director.pose
        prev_dir = director.direction

        # Mix tiny dt, normal 30fps dt, huge dt spikes, and zero dt
        dt = rng.choice([0.0, 0.001, 0.016, 0.033, 0.1, 0.5, 5.0])
        t += dt

        mx = rng.uniform(-2000.0, 2000.0)
        my = rng.uniform(-2000.0, 2000.0)
        pose, direction = director.update(mx, my, 0.0, 0.0)

        assert 0.0 <= pose <= 1.0, f"pose out of bounds: {pose}"
        assert direction in (-1, 0, 1)
        if direction != prev_dir:
            assert prev_pose <= TurnDirector.SWITCH_EPS, (
                f"Mirror teleport detected! dir {prev_dir} -> {direction} at pose={prev_pose}"
            )


def test_turn_director_hysteresis_no_jitter_across_head():
    """Once turned left, rapid left/right oscillation inside (exit_deg, enter_deg)
    must NEVER flip wanted_direction."""
    t = 0.0
    director = TurnDirector(enter_deg=22.0, exit_deg=14.0, clock=lambda: t)

    # First lock onto left (theta = 45 deg > enter_deg)
    t += 0.05
    director.update(-100.0, -100.0, 0.0, 0.0)
    assert director.wanted_direction == -1

    # Now jitter left and right at theta = 18 deg (strictly inside (14, 22))
    dx_mag = math.tan(math.radians(18.0)) * 100.0
    for step in range(200):
        t += 0.033
        dx = dx_mag if step % 2 == 0 else -dx_mag
        director.update(dx, -100.0, 0.0, 0.0)
        assert director.wanted_direction == -1


@pytest.mark.parametrize("bad_pose", [float("nan"), float("inf"), float("-inf"), -5.0, 5.0])
def test_frame_index_safe_on_non_finite_and_out_of_range_pose(bad_pose):
    idx = frame_index(bad_pose, 12)
    assert 0 <= idx < 12


@pytest.mark.parametrize(
    "raw_json",
    [
        '{"scale": "nan", "bubble_scale": 1.0}',
        '{"scale": "inf", "bubble_scale": 1.0}',
        '{"scale": "-inf", "bubble_scale": "nan"}',
        '{"scale": -999, "bubble_scale": 999, "muted": 1, "x": "12", "y": null}',
        "[]",
        "{broken json",
        '{"scale": "not-a-number"}',
    ],
)
def test_pet_state_load_never_crashes_and_always_returns_valid_bounds(tmp_path, monkeypatch, raw_json):
    monkeypatch.setenv("PIKACHU_HOME", str(tmp_path))
    state_path = pet_state.state_file()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(raw_json, encoding="utf-8")

    loaded = pet_state.load_state()
    lo_s, hi_s = pet_state.SCALE_RANGE
    lo_b, hi_b = pet_state.BUBBLE_SCALE_RANGE
    assert math.isfinite(loaded["scale"]) and lo_s <= loaded["scale"] <= hi_s
    assert math.isfinite(loaded["bubble_scale"]) and lo_b <= loaded["bubble_scale"] <= hi_b
    assert isinstance(loaded["muted"], bool)

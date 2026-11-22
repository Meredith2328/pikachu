# -*- coding: utf-8 -*-
"""Black-box & property-based invariant tests for pikapet.turn and pikapet.pet_state.

Mirrors the state-machine safety invariants formally verified in Lean 4
(formal/TurnDirector.lean) and fuzzes edge cases (NaN, Infinity, rapid
cross-head mouse jitter, 10k-step trajectories).
"""
import math
import os
import random
import tempfile
import unittest

from pikapet import pet_state
from pikapet.turn import TurnDirector, frame_index


class TestTurnAndStateInvariants(unittest.TestCase):
    def test_turn_director_10k_step_fuzz_no_mirror_teleport_and_bounds(self):
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

            dt = rng.choice([0.0, 0.001, 0.016, 0.033, 0.1, 0.5, 5.0])
            t += dt

            mx = rng.uniform(-2000.0, 2000.0)
            my = rng.uniform(-2000.0, 2000.0)
            pose, direction = director.update(mx, my, 0.0, 0.0)

            self.assertGreaterEqual(pose, 0.0)
            self.assertLessEqual(pose, 1.0)
            self.assertIn(direction, (-1, 0, 1))
            if direction != prev_dir:
                self.assertLessEqual(
                    prev_pose,
                    TurnDirector.SWITCH_EPS,
                    f"Mirror teleport detected! dir {prev_dir} -> {direction} at pose={prev_pose}",
                )

    def test_turn_director_hysteresis_no_jitter_across_head(self):
        """Once turned left, rapid left/right oscillation inside (exit_deg, enter_deg)
        must NEVER flip wanted_direction."""
        t = 0.0
        director = TurnDirector(enter_deg=22.0, exit_deg=14.0, clock=lambda: t)

        t += 0.05
        director.update(-100.0, -100.0, 0.0, 0.0)
        self.assertEqual(director.wanted_direction, -1)

        dx_mag = math.tan(math.radians(18.0)) * 100.0
        for step in range(200):
            t += 0.033
            dx = dx_mag if step % 2 == 0 else -dx_mag
            director.update(dx, -100.0, 0.0, 0.0)
            self.assertEqual(director.wanted_direction, -1)

    def test_frame_index_safe_on_non_finite_and_out_of_range_pose(self):
        for bad_pose in (float("nan"), float("inf"), float("-inf"), -5.0, 5.0):
            with self.subTest(bad_pose=bad_pose):
                idx = frame_index(bad_pose, 12)
                self.assertGreaterEqual(idx, 0)
                self.assertLess(idx, 12)

    def test_pet_state_load_never_crashes_and_always_returns_valid_bounds(self):
        samples = [
            '{"scale": "nan", "bubble_scale": 1.0}',
            '{"scale": "inf", "bubble_scale": 1.0}',
            '{"scale": "-inf", "bubble_scale": "nan"}',
            '{"scale": -999, "bubble_scale": 999, "muted": 1, "x": "12", "y": null}',
            "[]",
            "{broken json",
            '{"scale": "not-a-number"}',
        ]
        lo_s, hi_s = pet_state.SCALE_RANGE
        lo_b, hi_b = pet_state.BUBBLE_SCALE_RANGE
        old_home = os.environ.get("PIKACHU_HOME")
        try:
            for raw_json in samples:
                with self.subTest(raw_json=raw_json):
                    with tempfile.TemporaryDirectory() as tmp:
                        os.environ["PIKACHU_HOME"] = tmp
                        state_path = pet_state.state_file()
                        state_path.parent.mkdir(parents=True, exist_ok=True)
                        state_path.write_text(raw_json, encoding="utf-8")

                        loaded = pet_state.load_state()
                        self.assertTrue(math.isfinite(loaded["scale"]))
                        self.assertGreaterEqual(loaded["scale"], lo_s)
                        self.assertLessEqual(loaded["scale"], hi_s)
                        self.assertTrue(math.isfinite(loaded["bubble_scale"]))
                        self.assertGreaterEqual(loaded["bubble_scale"], lo_b)
                        self.assertLessEqual(loaded["bubble_scale"], hi_b)
                        self.assertIsInstance(loaded["muted"], bool)
        finally:
            if old_home is None:
                os.environ.pop("PIKACHU_HOME", None)
            else:
                os.environ["PIKACHU_HOME"] = old_home


if __name__ == "__main__":
    unittest.main()

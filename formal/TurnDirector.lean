/-!
# Formal Verification of `pikachu/pikapet/turn.py` (`TurnDirector`)

Models the mouse-tracking pet turning state machine in `pikachu`, using exact
scaled integer arithmetic (`pose ∈ [0, 1000]` represents `[0.0, 1.0]`,
`switchEps = 1` represents `0.001`), and proves its core visual safety invariants:

1. **No Mirror Teleportation (`no_mirror_teleport`):**
   Rendered direction `dir` can NEVER change unless `pose ≤ switchEps`
   (the pet is already at the symmetric front-facing frame).
2. **Hysteresis Anti-Jitter (`hysteresis_no_jitter`):**
   Inside the hysteresis band `exitDeg < theta < enterDeg`, the desired
   direction `want` NEVER flips even if the cursor jitters across the head.
3. **Pose Boundedness (`pose_in_bounds`):**
   For any sequence of mouse moves, `pose` stays in `[0, 1000]` forever.
4. **Monotonic Return to Front (`mismatch_drives_pose_down`):**
   Whenever `want ≠ dir`, target pose is forced to `0`, so `pose` monotonically
   decreases toward the front frame until switching is safe.
-/

namespace PikachuTurn

/-- Discrete horizontal direction: `-1` (left), `0` (center/front), `+1` (right). -/
inductive Dir where
  | left
  | center
  | right
  deriving DecidableEq, Repr

/-- Configuration matching `TurnDirector.__init__` in `pikapet/turn.py`. -/
structure TurnConfig where
  enterDeg  : Nat
  exitDeg   : Nat
  switchEps : Nat
  hHyst     : exitDeg < enterDeg

/-- State of `TurnDirector` at a given frame.
    `pose` is scaled by 1000 (`0` = front, `1000` = full turn). -/
structure TurnState where
  pose : Nat
  dir  : Dir
  want : Dir
  deriving DecidableEq, Repr

/-- Exact discrete model of `TurnDirector._update_want(theta, dx)`.
    `side` encodes the sign of `dx`: `.left` (`dx < 0`), `.center` (`dx = 0`), `.right` (`dx > 0`). -/
def updateWant (cfg : TurnConfig) (want : Dir) (theta : Nat) (side : Dir) : Dir :=
  match want with
  | .center =>
    if theta ≥ cfg.enterDeg && side ≠ .center then side else .center
  | .left =>
    if theta ≤ cfg.exitDeg then .center
    else if side == .right && theta ≥ cfg.enterDeg then .right
    else .left
  | .right =>
    if theta ≤ cfg.exitDeg then .center
    else if side == .left && theta ≥ cfg.enterDeg then .left
    else .right

/-- One frame update of `TurnDirector.update`.
    - `rawTarget ∈ [0, 1000]`: desired pose computed from `theta`
    - `kNum ∈ [0, 1000]`: smoothing factor `k * 1000` where `k = 1 - exp(-dt / tau)` -/
def step (cfg : TurnConfig) (s : TurnState)
    (theta : Nat) (side : Dir) (rawTarget : Nat) (kNum : Nat) : TurnState :=
  let want' := updateWant cfg s.want theta side
  let dir'  := if want' ≠ s.dir then (if s.pose ≤ cfg.switchEps then want' else s.dir) else s.dir
  let target := if want' == dir' && dir' ≠ .center then rawTarget else 0
  let pose' := (s.pose * (1000 - kNum) + target * kNum) / 1000
  { pose := pose', dir := dir', want := want' }

/-! ## Core Safety Theorems -/

/-- **Theorem 1 (No Mirror Teleportation):**
    Whenever the rendered direction changes (`s'.dir ≠ s.dir`), the pose before
    the transition MUST have been at or below `cfg.switchEps` (front frame). -/
theorem no_mirror_teleport (cfg : TurnConfig) (s : TurnState)
    (theta : Nat) (side : Dir) (rawTarget kNum : Nat)
    (hChanged : (step cfg s theta side rawTarget kNum).dir ≠ s.dir) :
    s.pose ≤ cfg.switchEps := by
  simp only [step] at hChanged
  split at hChanged
  · split at hChanged
    · assumption
    · contradiction
  · contradiction

/-- **Theorem 2 (Center Deadzone):**
    When facing center, any angle below `enterDeg` keeps `want = .center`. -/
theorem hysteresis_stable_from_center (cfg : TurnConfig)
    (theta : Nat) (side : Dir) (h : theta < cfg.enterDeg) :
    updateWant cfg .center theta side = .center := by
  unfold updateWant
  have hge : ¬ (theta ≥ cfg.enterDeg) := by omega
  simp [hge]

/-- **Theorem 3 (Hysteresis Anti-Jitter):**
    Once turned left or right, if the cursor stays inside the hysteresis band
    (`exitDeg < theta < enterDeg`), `want` NEVER flips—even if `side` jumps
    back and forth between `.left`, `.center`, and `.right`! -/
theorem hysteresis_no_jitter (cfg : TurnConfig) (want : Dir)
    (theta : Nat) (side : Dir)
    (hne : want ≠ .center)
    (hlo : cfg.exitDeg < theta)
    (hhi : theta < cfg.enterDeg) :
    updateWant cfg want theta side = want := by
  cases want with
  | center => contradiction
  | left =>
    unfold updateWant
    have h1 : ¬ (theta ≤ cfg.exitDeg) := by omega
    have h2 : ¬ (theta ≥ cfg.enterDeg) := by omega
    simp [h1, h2]
  | right =>
    unfold updateWant
    have h1 : ¬ (theta ≤ cfg.exitDeg) := by omega
    have h2 : ¬ (theta ≥ cfg.enterDeg) := by omega
    simp [h1, h2]

private theorem convex_le_1000 (p t k : Nat)
    (hp : p ≤ 1000) (ht : t ≤ 1000) (hk : k ≤ 1000) :
    (p * (1000 - k) + t * k) / 1000 ≤ 1000 := by
  have h1 : p * (1000 - k) ≤ 1000 * (1000 - k) := Nat.mul_le_mul_right _ hp
  have h2 : t * k ≤ 1000 * k := Nat.mul_le_mul_right _ ht
  have h3 : 1000 * (1000 - k) + 1000 * k = 1000 * 1000 := by
    rw [← Nat.mul_add, Nat.sub_add_cancel hk]
  omega

private theorem ite_le_of_le (c : Prop) [Decidable c] {a b m : Nat}
    (ha : a ≤ m) (hb : b ≤ m) : (if c then a else b) ≤ m := by
  split <;> assumption

/-- **Theorem 4 (Pose Boundedness):**
    If `s.pose ≤ 1000`, `rawTarget ≤ 1000`, and `kNum ≤ 1000`,
    then the next frame's `pose` is guaranteed to remain `≤ 1000`. -/
theorem pose_in_bounds (cfg : TurnConfig) (s : TurnState)
    (theta : Nat) (side : Dir) (rawTarget kNum : Nat)
    (hp : s.pose ≤ 1000) (ht : rawTarget ≤ 1000) (hk : kNum ≤ 1000) :
    (step cfg s theta side rawTarget kNum).pose ≤ 1000 := by
  exact convex_le_1000 _ _ _ hp (ite_le_of_le _ ht (Nat.zero_le _)) hk

/-- **Theorem 5 (Monotonic Return When Waiting to Switch):**
    If desired direction differs from current direction and `pose > switchEps`,
    the pet refuses to flip yet and `pose` strictly or weakly decreases toward 0. -/
theorem mismatch_drives_pose_down (cfg : TurnConfig) (s : TurnState)
    (theta : Nat) (side : Dir) (rawTarget kNum : Nat)
    (hmismatch : updateWant cfg s.want theta side ≠ s.dir)
    (hgt : cfg.switchEps < s.pose)
    (hk : kNum ≤ 1000) :
    (step cfg s theta side rawTarget kNum).pose ≤ s.pose := by
  simp only [step]
  have hnotle : ¬ (s.pose ≤ cfg.switchEps) := by omega
  have hbeq : (updateWant cfg s.want theta side == s.dir) = false := by
    simp [hmismatch]
  simp [hmismatch, hnotle, hbeq]
  have hmul : s.pose * (1000 - kNum) ≤ s.pose * 1000 :=
    Nat.mul_le_mul_left s.pose (by omega)
  omega

end PikachuTurn

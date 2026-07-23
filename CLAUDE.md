# CLAUDE.md — rbmk-sim

## Ground rules

- `headless_test.py` is the ground truth for "the Chernobyl scenario works".
  Probe metrics or proxies must never outrank an actual test run.
- **Do not pursue procedural/automated tuning of the physics constants again**
  (user decision, 2026-07-06). `tune.py` is committed as reference only — do not
  extend it or wire it into anything.
- The lattice `period = 4` in `core.py` (one water column per side) must not
  change unless the physics constants are re-derived. The committed constants
  only sustain the scenario at that geometry. The old `water_cols` config knob
  was removed for this reason; `tune.py` still references it and no longer runs.

## Lessons from the tuning experiment (7 overnight runs, 2026-07-06)

- At `water_cols = 2` the core is deeply subcritical; recovering criticality
  within realistic bounds forces `p_fission_thermal` to ~0.30-0.35 (vs 0.12).
- Fast fission kills xenon leverage: thermal neutrons fission immediately on
  entering fuel instead of surviving to reach poisoned cells. The stage-3
  clean/poisoned ratio peaked at 1.43x (needs 1.5x) at an *interior* optimum
  near fission 0.30 — pushing any lever further made it worse.
- Coupled knobs: `p_water_absorb` simultaneously drives criticality (down),
  void coefficient (up), and graphite-tip effect (up). Stages 2/4a/4b pull it
  up while xenon rebalancing pulls it down — a genuine tug-of-war.
- Boiling onset is knife-edge sensitive to `pump_cooling`; steam fraction can
  read 0% or 60% across small parameter changes and different heat-soak times.
- The interactive sim and the test both read defaults straight from
  `config.py`; changing defaults changes both.

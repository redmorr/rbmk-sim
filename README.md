# RBMK reactor simulation

A 2D agent-based model of RBMK (Chernobyl-type) reactor physics in Python/pygame,
inspired by Higgsino physics' *"Chernobyl Visually Explained"*. Free-moving neutrons
over a 60x40 core lattice demonstrate fission chain reactions, graphite moderation,
water absorption, the positive void coefficient, xenon-135 poisoning, and the
graphite-tipped control rod flaw — the accident dynamics emerge from local rules,
nothing is scripted.

## Run

```
pip install pygame-ce numpy
python reactor_sim.py
```

Press **S** for the guided "April 26, 1986" scenario. Full mechanics documentation
is in the module docstring of [reactor_sim.py](reactor_sim.py); every tunable
constant lives in [config.py](config.py).

## Controls

| Input | Action |
|---|---|
| SPACE / R | pause / reset |
| A | AZ-5 emergency shutdown (18 s full travel, graphite tips first) |
| M | toggle automatic power controller / manual rods |
| UP / DOWN, drag rod column | move rod bank / individual rod (manual mode) |
| +/- | simulation speed 1-10x |
| sliders | pump power, target power, sim speed, rod bank |

## Lessons learned: wider water channels

`water_cols` in [config.py](config.py) controls how many water columns sit on
each side of a fuel column. The physics constants are tuned for `water_cols = 1`
only. With 2, the core is deeply subcritical (doubled water absorption) and the
constants cannot be rescued within realistic ranges: an automated tuner
([tune.py](tune.py), kept for reference) ran feedback-controlled searches against
the staged checks and got 4 of 5 stages passing, but never the xenon-pit stage.
The blocker is a physics coupling, not a search failure: restoring criticality
against the extra water requires roughly tripling `p_fission_thermal`, and fast
fission destroys xenon's leverage — thermal neutrons die in fuel before they can
reach poisoned cells, so the clean/poisoned growth ratio peaks near 1.43x
(needed: 1.5x) at an interior optimum. Supporting the scenario at other
geometries would need a rebalanced test protocol or model changes (e.g. xenon
that survives more than one capture), not constant-tweaking.

## Verify the physics

```
python headless_test.py
```

Five staged checks: controllable chain reaction, positive void coefficient
(pump cut → 8x power runaway with rods frozen), xenon pit (poisoned core grows
~1.7x slower than a clean one), graphite-tip reactivity (tips-only insertion
beats rods-out by ~2x), and the AZ-5 paradox (a scram from fully withdrawn rods
initially *raises* power).

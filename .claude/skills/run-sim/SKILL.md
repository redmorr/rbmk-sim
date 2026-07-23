---
name: run-sim
description: Launch and drive the interactive RBMK pygame sim, with screenshots. Use when asked to run the sim, open the window, or confirm a change works in the real app rather than only in headless_test.py.
---

# Running the RBMK sim

Two ways. Pick by what you need to prove.

## Just show the user the app

```bash
py reactor_sim.py
```

Opens a real window on the user's desktop. Run it in the background so the
session isn't blocked; the user closes it with ESC or the window button. There
are no CLI flags — seed, geometry and every constant come from `config.py`.

## Confirm a change works (screenshots, no human)

```bash
py .claude/skills/run-sim/drive.py <out_dir>
```

Drives the real `App.run()` loop by posting input into pygame's event queue
from a side thread: **S** for the scenario overlay, slider clicks for sim
speed / target power / pump power, **A** for AZ-5. Saves four PNGs and prints
a state line per frame. Takes ~45 s wall. Read the PNGs — a blank or
single-color frame is a failed launch.

A healthy run reaches roughly:

| frame | t | power | temp | voids | Xe |
|---|---|---|---|---|---|
| `01_power` | ~64 s | ~3500 n | ~1950 °C | ~40% | ~75 |
| `02_voids` | ~112 s | ~2600 n | ~3000 °C | ~68% | ~30 |
| `03_az5` | ~190 s | ~1 n | ~850 °C | ~38% | ~210 |
| `04_final` | ~230 s | ~5 n | 280 °C | 0% | ~30 |

Stochastic, so these drift run to run. What must hold: power and temperature
climb, voids grow after the pump cut, AZ-5 drives rods to 100% and kills the
chain reaction, and xenon spikes *after* shutdown before decaying away.

## Gotchas that cost time

- **Slider clicks at `frac=1.0` silently miss.** `Rect.collidepoint` excludes
  the right edge. `click_slider` caps at 0.99 — keep that cap.
- **Screenshots from the driver thread can tear** (HUD or panel missing) when
  the save races `pygame.display.flip()`. That's a capture artifact, not a
  render bug; re-shoot before investigating.
- **Default sim speed is 1x**, and the interesting physics needs ~100 sim
  seconds of heat soak. Without the speed slider click the run just shows a
  cold core, which looks like a broken sim but isn't.
- **The auto controller will hold power flat.** No explosion unless you press
  M for manual and pull the rods out first — a scram from 95% insertion
  shuts down normally. That is correct behavior, not a regression.

`headless_test.py` remains the ground truth for the physics itself; this skill
only proves the app launches, renders and responds to input.

"""Drive the interactive sim through its real run() loop and save frames.

Posts input into pygame's event queue from a side thread, so App.handle_event,
App.update and App.render all run exactly as they do for a human.

Run:  py .claude/skills/run-sim/drive.py [out_dir]
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pygame

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from config import Config
from reactor_sim import GRAPH_W, HUD_H, App

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
OUT.mkdir(parents=True, exist_ok=True)  # pygame.image.save won't create it

app = App(Config())

# slider geometry, mirrored from App._build_sliders
SX = GRAPH_W + 30
SY = HUD_H + app.core_h + 30
SW = app.core_w - SX - 30


def click_slider(i: int, frac: float) -> None:
    """Slider i (0 pump, 1 target, 2 speed, 3 rod bank) to `frac` of its range.

    frac is capped at 0.99: Rect.collidepoint excludes the right edge, so a
    click at exactly frac=1.0 silently misses the slider.
    """
    pos = (int(SX + min(frac, 0.99) * SW), SY + i * 33 + 4)
    pygame.event.post(pygame.event.Event(pygame.MOUSEBUTTONDOWN, pos=pos, button=1))
    pygame.event.post(pygame.event.Event(pygame.MOUSEBUTTONUP, pos=pos, button=1))


def key(k: int) -> None:
    pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=k, mod=0, unicode=""))


def shot(name: str) -> None:
    pygame.image.save(app.screen, str(OUT / f"{name}.png"))
    r = app.reactor
    print(f"[{name}] t={r.time:6.1f}s n={r.neutron_count:5d} "
          f"temp={280 + r.avg_heat * 900:4.0f}C voids={r.steam_fraction * 100:4.1f}% "
          f"Xe={r.xenon_count:3d} rods={r.avg_insertion * 100:5.1f}% "
          f"mode={'AZ-5' if r.az5_active else ('AUTO' if r.auto_control else 'MAN')} "
          f"stage={app.scenario.stage} exploded={app.exploded}", flush=True)


def script() -> None:
    """Sleeps are WALL seconds; at ~10x sim speed 1 wall s is ~10 sim s.

    Reaching voids needs ~100 sim s of heat soak, so don't shorten these.
    """
    time.sleep(1.0)
    key(pygame.K_s)                 # guided scenario overlay
    time.sleep(0.5)
    click_slider(2, 0.99)           # SIM speed -> ~10x
    time.sleep(0.5)
    click_slider(1, 0.83)           # TARGET power -> ~2500
    time.sleep(14.0)
    shot("01_power")
    click_slider(0, 0.10)           # PUMP power -> 10%, voids grow
    time.sleep(12.0)
    shot("02_voids")
    key(pygame.K_a)                 # AZ-5
    time.sleep(8.0)
    shot("03_az5")
    time.sleep(4.0)
    shot("04_final")


def script_then_quit() -> None:
    # without the finally, any error in script() leaves run() looping forever
    try:
        script()
    finally:
        pygame.event.post(pygame.event.Event(pygame.QUIT))


threading.Thread(target=script_then_quit, daemon=True).start()
app.run()
print("run() returned cleanly; pygame shut down")

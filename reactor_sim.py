"""RBMK reactor simulation — a 2D agent-based model of the Chernobyl accident physics.

Run:  py reactor_sim.py            (interactive)
      py headless_test.py          (staged physics verification, no window)

WHAT EACH MECHANIC MODELS
=========================
Lattice (60x40 cells, cross-section of the core):
  FUEL (green)       U-235 channels. A THERMAL neutron entering may cause
                     fission: 2-3 FAST neutrons + local heat. Fast neutrons
                     almost never fission U-235 — they must be slowed first.
  MODERATOR (brown)  Graphite columns. Slow fast neutrons to thermal. This is
                     the defining RBMK trait: graphite does the moderating,
                     so losing the water does NOT stop the chain reaction.
  WATER (blue)       Coolant between channels. Weak moderator, moderate
                     absorber of thermal neutrons. Boils into...
  STEAM (pale)       ...voids that neither absorb nor moderate. Because
                     absorption is lost while graphite keeps moderating,
                     voids ADD reactivity: the positive void coefficient.
                     The feedback loop (heat -> steam -> power -> heat) is
                     emergent, not scripted.
  XENON (black)      Xe-135, the strongest known neutron absorber. Appears
                     time-delayed after fission (iodine-135 precursor decays,
                     ~30 sim-s here vs ~6.6 h in reality). Burned off by
                     neutron capture (cell reverts to fuel) or decays away.
                     After a power reduction, xenon accumulates faster than
                     it burns: the "xenon pit" that traps a low-power reactor.
  INERT (gray)       Structural filler; neutrons pass through.

Control rods (15 columns, inserted from the top):
  Boron section (dark) absorbs both fast and thermal neutrons.
  THE FLAW: the leading 15% of each rod is a GRAPHITE DISPLACER. When a
  fully-withdrawn rod is inserted, graphite enters first, displacing
  absorbing water — reactivity briefly RISES before the boron arrives.
  AZ-5 (scram) drives all rods in together at a realistic, slow 18 s —
  from a voided, rods-out core this spikes power instead of cutting it.

The April 26, 1986 sequence (press S for the guided scenario):
  full power -> power reduction (xenon builds) -> rods pulled to fight the
  pit -> pump flow reduced for the turbine test (voids grow) -> power rises
  -> AZ-5 pressed -> graphite tips add reactivity -> prompt runaway.

Controls:
  SPACE pause | R reset | A AZ-5 | M auto/manual | S scenario
  UP/DOWN rod bank (manual) | +/- sim speed | drag a rod column to move one
  rod | sliders: pump power, target power, sim speed, rod bank
"""
from __future__ import annotations

import argparse
import math
import sys
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

import pygame

from config import Config
from core import Cell, Reactor

# ----------------------------------------------------------------- layout

HUD_H = 56
PANEL_H = 160
GRAPH_W = 590

CELL_COLORS = {
    int(Cell.WATER): (24, 52, 110),
    int(Cell.STEAM): (168, 186, 200),
    int(Cell.FUEL): (36, 140, 52),
    int(Cell.INERT): (86, 86, 92),
    int(Cell.MODERATOR): (122, 88, 54),
    int(Cell.XENON): (8, 8, 10),
    int(Cell.ROD_BORON): (34, 34, 48),
    int(Cell.ROD_TIP): (190, 158, 96),
}
COL_BG = (12, 14, 18)
COL_PANEL = (22, 25, 32)
COL_TEXT = (208, 214, 222)
COL_DIM = (120, 126, 138)
COL_FAST = (255, 255, 255)
COL_THERMAL = (255, 200, 40)
COL_POWER = (255, 210, 70)
COL_TEMP = (255, 90, 60)


# ----------------------------------------------------------------- widgets

@dataclass
class Slider:
    label: str
    rect: pygame.Rect
    vmin: float
    vmax: float
    get: Callable[[], float]
    set: Callable[[float], None]
    fmt: Callable[[float], str]
    enabled: Callable[[], bool] = lambda: True

    def frac(self) -> float:
        return (self.get() - self.vmin) / (self.vmax - self.vmin)

    def apply_mouse(self, mx: int) -> None:
        f = min(1.0, max(0.0, (mx - self.rect.x) / self.rect.w))
        self.set(self.vmin + f * (self.vmax - self.vmin))

    def draw(self, surf: pygame.Surface, font: pygame.font.Font) -> None:
        on = self.enabled()
        track = self.rect
        pygame.draw.rect(surf, (50, 55, 66) if on else (36, 39, 46), track, border_radius=4)
        fill = track.copy()
        fill.w = max(4, int(track.w * self.frac()))
        pygame.draw.rect(surf, (90, 120, 190) if on else (55, 60, 72), fill, border_radius=4)
        kx = track.x + int(track.w * self.frac())
        pygame.draw.rect(surf, COL_TEXT if on else COL_DIM, (kx - 3, track.y - 3, 6, track.h + 6),
                         border_radius=3)
        txt = font.render(f"{self.label}: {self.fmt(self.get())}", True,
                          COL_TEXT if on else COL_DIM)
        surf.blit(txt, (track.x, track.y - 18))


@dataclass
class Scenario:
    """The scripted April 26, 1986 sequence, advanced by live reactor state."""
    active: bool = False
    stage: int = 0
    hold_timer: float = 0.0
    done: bool = False

    STAGES = [
        ("1/6  Bring the reactor to full power: set TARGET to ~1500 "
         "and hold it there for a few seconds.",
         lambda r, app: r.target_n >= 1300 and r.neutron_count > 1100),
        ("2/6  The safety test begins. Reduce TARGET power to ~200. "
         "Watch xenon (black cells) accumulate as power falls.",
         lambda r, app: r.target_n <= 300 and r.neutron_count < 500),
        ("3/6  Power is sagging into the xenon pit. Press M for MANUAL and "
         "pull the rod bank nearly all the way out (UP key, below 15%).",
         lambda r, app: not r.auto_control and r.avg_insertion < 0.15),
        ("4/6  Turbine coast-down: reduce PUMP power below 30%. "
         "Steam voids will start to grow.",
         lambda r, app: r.pump_power < 0.30),
        ("5/6  Voids add reactivity — power is creeping up. When it starts "
         "to run, press A: AZ-5, the emergency shutdown.",
         lambda r, app: r.az5_active),
        ("6/6  The graphite tips enter first, adding reactivity across the "
         "whole core. 18 seconds is a very long time...",
         lambda r, app: False),  # ends at the explosion
    ]

    def update(self, r: Reactor) -> None:
        if not self.active or self.done or self.stage >= len(self.STAGES):
            return
        _, cond = self.STAGES[self.stage]
        if cond(r, self):
            self.stage += 1
            if self.stage >= len(self.STAGES):
                self.done = True

    @property
    def text(self) -> str:
        if self.stage < len(self.STAGES):
            return self.STAGES[self.stage][0]
        return ""


# ----------------------------------------------------------------- app

class App:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        pygame.init()
        self.core_w = cfg.grid_w * cfg.cell_px
        self.core_h = cfg.grid_h * cfg.cell_px
        self.size = (self.core_w, HUD_H + self.core_h + PANEL_H)
        self.screen = pygame.display.set_mode(self.size)
        pygame.display.set_caption("RBMK reactor simulation")
        self.font = pygame.font.SysFont("consolas", 14)
        self.font_small = pygame.font.SysFont("consolas", 12)
        self.font_big = pygame.font.SysFont("consolas", 30, bold=True)
        self.clock = pygame.time.Clock()

        self.reactor = Reactor(cfg)
        self.paused = False
        self.sim_speed = 1.0
        self.exploded = False
        self.explosion_stats: dict[str, str] = {}
        self.peak_n = 0
        self.scenario = Scenario()
        self.dragging: Slider | None = None
        self.dragging_rod = None
        self._acc = 0.0
        self.samples: deque[tuple[float, int, float]] = deque(
            maxlen=int(cfg.graph_window_s / cfg.graph_sample_dt) + 2)
        self._next_sample = 0.0

        # translucent red heat overlays, 5 intensity levels
        self.heat_tiles = []
        for a in (36, 70, 105, 145, 190):
            t = pygame.Surface((cfg.cell_px, cfg.cell_px), pygame.SRCALPHA)
            t.fill((255, 40, 20, a))
            self.heat_tiles.append(t)

        self.az5_rect = pygame.Rect(self.core_w - 120, 8, 110, 40)
        self._build_sliders()

    def _build_sliders(self) -> None:
        r = self.reactor
        x = GRAPH_W + 30
        y0 = HUD_H + self.core_h + 30
        w = self.core_w - x - 30
        step = 33

        def mk(i: int, label: str, vmin: float, vmax: float, get, set_, fmt, enabled=lambda: True):
            return Slider(label, pygame.Rect(x, y0 + i * step, w, 8), vmin, vmax,
                          get, set_, fmt, enabled)

        def set_pump(v: float) -> None:
            r.pump_power = v

        def set_target(v: float) -> None:
            r.target_n = v

        def set_speed(v: float) -> None:
            self.sim_speed = v

        self.sliders = [
            mk(0, "PUMP power", 0.0, 1.0, lambda: r.pump_power, set_pump,
               lambda v: f"{v * 100:3.0f}%"),
            mk(1, "TARGET power", 50.0, 3000.0, lambda: r.target_n, set_target,
               lambda v: f"{v:4.0f}"),
            mk(2, "SIM speed", 1.0, 10.0, lambda: self.sim_speed, set_speed,
               lambda v: f"{v:3.1f}x"),
            mk(3, "ROD bank", 0.0, 1.0, lambda: r.bank_target,
               lambda v: r.set_bank_target(v), lambda v: f"{v * 100:3.0f}%",
               enabled=lambda: not r.auto_control and not r.az5_active),
        ]

    # ------------------------------------------------------------- input

    def handle_event(self, ev: pygame.event.Event) -> bool:
        r = self.reactor
        if ev.type == pygame.QUIT:
            return False
        if ev.type == pygame.KEYDOWN:
            if ev.key == pygame.K_ESCAPE:
                return False
            if ev.key == pygame.K_SPACE:
                self.paused = not self.paused
            elif ev.key == pygame.K_r:
                self.reset()
            elif ev.key == pygame.K_a and not self.exploded:
                r.scram()
            elif ev.key == pygame.K_m:
                r.auto_control = not r.auto_control
            elif ev.key == pygame.K_s:
                self.scenario = Scenario(active=not self.scenario.active)
            elif ev.key in (pygame.K_PLUS, pygame.K_EQUALS, pygame.K_KP_PLUS):
                self.sim_speed = min(10.0, self.sim_speed + 1.0)
            elif ev.key in (pygame.K_MINUS, pygame.K_KP_MINUS):
                self.sim_speed = max(1.0, self.sim_speed - 1.0)
        elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
            mx, my = ev.pos
            if self.az5_rect.collidepoint(mx, my) and not self.exploded:
                r.scram()
                return True
            for s in self.sliders:
                if s.enabled() and s.rect.inflate(0, 16).collidepoint(mx, my):
                    self.dragging = s
                    s.apply_mouse(mx)
                    return True
            if (not r.auto_control and not r.az5_active
                    and HUD_H <= my < HUD_H + self.core_h):
                cp = self.cfg.cell_px
                for rod in r.rods:
                    cx = rod.col * cp + cp // 2
                    if abs(mx - cx) <= cp:
                        self.dragging_rod = rod
                        rod.target = (my - HUD_H) / self.core_h
                        return True
        elif ev.type == pygame.MOUSEBUTTONUP and ev.button == 1:
            self.dragging = None
            self.dragging_rod = None
        elif ev.type == pygame.MOUSEMOTION:
            if self.dragging:
                self.dragging.apply_mouse(ev.pos[0])
            elif self.dragging_rod:
                self.dragging_rod.target = min(1.0, max(0.0, (ev.pos[1] - HUD_H) / self.core_h))
        return True

    def reset(self) -> None:
        self.reactor.reset()
        self.exploded = False
        self.explosion_stats = {}
        self.peak_n = 0
        self.samples.clear()
        self._next_sample = 0.0
        self.scenario = Scenario(active=self.scenario.active)

    # ------------------------------------------------------------- update

    def update(self, frame_dt: float) -> None:
        cfg = self.cfg
        r = self.reactor
        if self.paused or self.exploded:
            self._acc = 0.0
            return
        if not r.auto_control and not r.az5_active:
            keys = pygame.key.get_pressed()
            if keys[pygame.K_UP]:
                r.set_bank_target(r.bank_target - 0.25 * frame_dt)
            elif keys[pygame.K_DOWN]:
                r.set_bank_target(r.bank_target + 0.25 * frame_dt)
        dt = 1.0 / cfg.physics_hz
        self._acc += frame_dt * self.sim_speed
        steps = 0
        while self._acc >= dt and steps < cfg.max_steps_per_frame:
            r.step(dt)
            steps += 1
            self._acc -= dt
            self.peak_n = max(self.peak_n, r.neutron_count)
            if r.time >= self._next_sample:
                self._next_sample = r.time + cfg.graph_sample_dt
                self.samples.append((r.time, r.neutron_count, r.avg_heat))
            if r.neutron_count >= cfg.explosion_threshold:
                self._explode()
                break
        if self._acc >= dt:
            self._acc = 0.0  # can't keep up: drop time instead of spiraling
        self.scenario.update(r)

    def _explode(self) -> None:
        r = self.reactor
        self.exploded = True
        self.explosion_stats = {
            "time": f"{r.time:.1f} s",
            "peak power": f"{self.peak_n} neutrons",
            "core temp": f"{280 + r.avg_heat * self.cfg.temp_display_scale:.0f} C",
            "steam voids": f"{r.steam_fraction * 100:.0f}%",
            "xenon cells": f"{r.xenon_count}",
            "rod insertion": f"{r.avg_insertion * 100:.0f}%",
        }

    # ------------------------------------------------------------- render

    def render(self, alpha: float) -> None:
        scr = self.screen
        cfg = self.cfg
        r = self.reactor
        cp = cfg.cell_px
        scr.fill(COL_BG)

        # --- core lattice (from the effective grid, so rods are included)
        eff = r.eff
        heat = r.heat
        tiles = self.heat_tiles
        w = r.w
        i = 0
        for iy in range(r.h):
            ry = HUD_H + iy * cp
            for ix in range(w):
                c = eff[i]
                scr.fill(CELL_COLORS[c], (ix * cp, ry, cp - 1, cp - 1))
                h = heat[iy, ix]
                if h > 0.25:
                    lvl = min(4, int(h * 1.8))
                    scr.blit(tiles[lvl], (ix * cp, ry))
                i += 1

        # steam bubbles
        for iy, ix in zip(*r.steam.nonzero()):
            bx = ix * cp + (ix * 7 + iy * 13) % (cp - 6) + 2
            by = HUD_H + iy * cp + (ix * 11 + iy * 5) % (cp - 6) + 2
            pygame.draw.circle(scr, (230, 240, 248), (bx, by), 2)

        # iodine precursor markers (faint)
        for idx in r.iodine:
            ix, iy = idx % w, idx // w
            scr.fill((20, 60, 30), (ix * cp + cp // 2 - 2, HUD_H + iy * cp + cp // 2 - 2, 4, 4))

        # rod channel markers along the top edge
        for rod in r.rods:
            cx = rod.col * cp + cp // 2
            pygame.draw.polygon(scr, (150, 150, 165),
                                [(cx - 4, HUD_H), (cx + 4, HUD_H), (cx, HUD_H + 6)])

        # --- neutrons (interpolated between physics steps)
        for n in r.neutrons:
            x = n.px + (n.x - n.px) * alpha
            y = HUD_H + n.py + (n.y - n.py) * alpha
            if n.thermal:
                scr.fill(COL_THERMAL, (x - 1, y - 1, 3, 3))
            else:
                scr.fill(COL_FAST, (x - 1, y - 1, 2, 2))

        self._render_hud()
        self._render_panel()
        if self.scenario.active:
            self._render_scenario()
        if self.exploded:
            self._render_explosion()
        elif self.paused:
            t = self.font_big.render("PAUSED", True, COL_TEXT)
            scr.blit(t, t.get_rect(center=(self.core_w // 2, HUD_H + 40)))
        pygame.display.flip()

    def _render_hud(self) -> None:
        scr = self.screen
        r = self.reactor
        pygame.draw.rect(scr, COL_PANEL, (0, 0, self.core_w, HUD_H))
        temp = 280 + r.avg_heat * self.cfg.temp_display_scale
        row1 = (f"POWER {r.neutron_count:5d} n   TEMP {temp:5.0f} C   "
                f"VOIDS {r.steam_fraction * 100:4.1f}%   XENON {r.xenon_count:3d}   "
                f"RODS {r.avg_insertion * 100:5.1f}%")
        mode = "AUTO" if r.auto_control else "MANUAL"
        if r.az5_active:
            mode = "AZ-5 SCRAM"
        row2 = (f"t={r.time:7.1f}s   speed {self.sim_speed:.0f}x   mode {mode}   "
                f"fissions {r.fissions_per_s:6.0f}/s")
        scr.blit(self.font.render(row1, True, COL_TEXT), (12, 9))
        scr.blit(self.font.render(row2, True, COL_DIM), (12, 30))
        hot = r.az5_active and (pygame.time.get_ticks() // 300) % 2 == 0
        pygame.draw.rect(scr, (200, 30, 30) if not hot else (255, 90, 60),
                         self.az5_rect, border_radius=6)
        t = self.font.render("AZ-5", True, (255, 235, 235))
        scr.blit(t, t.get_rect(center=self.az5_rect.center))

    def _render_panel(self) -> None:
        scr = self.screen
        cfg = self.cfg
        y0 = HUD_H + self.core_h
        pygame.draw.rect(scr, COL_PANEL, (0, y0, self.core_w, PANEL_H))

        # --- time-series graphs
        gx, gy, gw, gh = 12, y0 + 22, GRAPH_W - 24, PANEL_H - 52
        pygame.draw.rect(scr, (14, 16, 21), (gx, gy, gw, gh))
        scr.blit(self.font_small.render(
            f"last {cfg.graph_window_s:.0f}s   power (yellow, peak {self.peak_n})   "
            "temperature (red)", True, COL_DIM), (gx, y0 + 6))
        if len(self.samples) >= 2:
            t1 = self.samples[-1][0]
            t0 = t1 - cfg.graph_window_s
            pts = [s for s in self.samples if s[0] >= t0]
            if len(pts) >= 2:
                nmax = max(max(p[1] for p in pts), 100)
                hmax = max(max(p[2] for p in pts), 0.2)
                for key, vmax, color in ((1, nmax, COL_POWER), (2, hmax, COL_TEMP)):
                    line = [(gx + gw * (p[0] - t0) / cfg.graph_window_s,
                             gy + gh - 2 - (gh - 6) * (p[key] / vmax)) for p in pts]
                    pygame.draw.lines(scr, color, False, line, 2)
        # explosion threshold marker on the power scale would move around;
        # peak label above is the reference instead

        # --- sliders + key help
        for s in self.sliders:
            s.draw(scr, self.font_small)
        help_y = y0 + PANEL_H - 20
        scr.blit(self.font_small.render(
            "SPACE pause  R reset  A az-5  M auto/manual  S scenario  "
            "UP/DOWN rods  +/- speed  drag rod columns in manual",
            True, COL_DIM), (12, help_y))

    def _render_scenario(self) -> None:
        scr = self.screen
        box = pygame.Rect(40, HUD_H + 10, self.core_w - 80, 48)
        surf = pygame.Surface(box.size, pygame.SRCALPHA)
        surf.fill((10, 12, 16, 215))
        scr.blit(surf, box.topleft)
        pygame.draw.rect(scr, (180, 160, 60), box, 1, border_radius=4)
        text = self.scenario.text or "It is 01:23:40, April 26, 1986."
        words = text.split()
        lines, cur = [], ""
        for word in words:
            if len(cur) + len(word) + 1 > 92:
                lines.append(cur)
                cur = word
            else:
                cur = f"{cur} {word}".strip()
        lines.append(cur)
        for i, ln in enumerate(lines[:2]):
            scr.blit(self.font.render(ln, True, (235, 225, 180)), (box.x + 12, box.y + 8 + i * 17))

    def _render_explosion(self) -> None:
        scr = self.screen
        veil = pygame.Surface(self.size, pygame.SRCALPHA)
        veil.fill((30, 4, 4, 170))
        scr.blit(veil, (0, 0))
        cx = self.core_w // 2
        t = self.font_big.render("REACTOR DESTROYED", True, (255, 120, 80))
        scr.blit(t, t.get_rect(center=(cx, HUD_H + 120)))
        y = HUD_H + 170
        for k, v in self.explosion_stats.items():
            line = self.font.render(f"{k:>14}: {v}", True, COL_TEXT)
            scr.blit(line, line.get_rect(center=(cx, y)))
            y += 22
        t = self.font.render("press R to reset", True, COL_DIM)
        scr.blit(t, t.get_rect(center=(cx, y + 18)))

    # ------------------------------------------------------------- loop

    def run(self, smoke_frames: int = 0, screenshot: str | None = None) -> None:
        running = True
        frames = 0
        while running:
            frame_dt = min(self.clock.tick(60) / 1000.0, 0.25)
            for ev in pygame.event.get():
                running = self.handle_event(ev)
            self.update(frame_dt)
            alpha = min(1.0, self._acc * self.cfg.physics_hz)
            self.render(alpha)
            frames += 1
            if smoke_frames and frames >= smoke_frames:
                if screenshot:
                    pygame.image.save(self.screen, screenshot)
                running = False
        pygame.quit()


def main() -> None:
    ap = argparse.ArgumentParser(description="RBMK reactor simulation")
    ap.add_argument("--seed", type=int, default=None, help="RNG seed (default from Config)")
    ap.add_argument("--smoke", type=int, default=0, metavar="N",
                    help="run N frames then exit (automated test)")
    ap.add_argument("--screenshot", type=str, default=None,
                    help="with --smoke: save final frame to this PNG")
    ap.add_argument("--inject", type=int, default=0,
                    help="inject N neutrons at start (demo/testing)")
    args = ap.parse_args()

    cfg = Config() if args.seed is None else Config(seed=args.seed)
    app = App(cfg)
    if args.inject:
        app.reactor.inject(args.inject)
    app.run(smoke_frames=args.smoke, screenshot=args.screenshot)


if __name__ == "__main__":
    main()

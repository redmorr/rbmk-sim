"""Physics core for the RBMK simulation: lattice, neutrons, rods, heat, xenon.

Pure simulation state + fixed-timestep `Reactor.step(dt)`; no pygame here.
See the module docstring in reactor_sim.py for what each mechanic models.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from enum import IntEnum

import numpy as np

from config import Config


class Cell(IntEnum):
    WATER = 0       # liquid coolant
    STEAM = 1       # voided coolant
    FUEL = 2        # U-235
    INERT = 3       # structural filler
    MODERATOR = 4   # graphite column
    XENON = 5       # Xe-135 poisoned fuel cell
    ROD_BORON = 6   # effective grid only: boron absorber section
    ROD_TIP = 7     # effective grid only: graphite displacer tip


@dataclass(slots=True)
class Neutron:
    x: float
    y: float
    vx: float
    vy: float
    thermal: bool
    px: float   # previous position, for interpolated rendering
    py: float


@dataclass(slots=True)
class Rod:
    col: int
    insertion: float = 0.0   # 0 = fully withdrawn, 1 = fully inserted
    target: float = 0.0


class Reactor:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.w = cfg.grid_w
        self.h = cfg.grid_h
        self.reset()

    # ------------------------------------------------------------- setup

    def reset(self) -> None:
        cfg = self.cfg
        self.rng = random.Random(cfg.seed)
        w, h = self.w, self.h
        rng = self.rng

        self.time = 0.0
        self.base: list[int] = []           # current visible cell type per cell
        water = np.zeros((h, w), dtype=bool)
        # repeating unit: graphite | water | fuel | water
        period = 4
        for iy in range(h):
            for ix in range(w):
                off = ix % period
                if off == 0:
                    c = Cell.MODERATOR
                elif off == 2:
                    c = Cell.INERT if rng.random() < cfg.inert_fraction else Cell.FUEL
                else:
                    c = Cell.WATER
                    water[iy, ix] = True
                self.base.append(int(c))
        self.water_mask = water             # static: which cells are coolant channels
        self.steam = np.zeros((h, w), dtype=bool)
        self.heat = np.zeros((h, w), dtype=np.float32)
        self._heat_steps = 0

        # one rod per unit, in the first water channel after each graphite column
        self.rods = [Rod(col=x) for x in range(1, w, period)]
        self.eff: list[int] = list(self.base)

        self.neutrons: list[Neutron] = []
        self.iodine: dict[int, float] = {}  # flat cell index -> time until Xe
        self.xenon: dict[int, float] = {}   # flat cell index -> time until decay

        self.pump_power = cfg.pump_power_default
        self.auto_control = True
        self.target_n = cfg.default_target_n
        self.bank_target = 0.7              # start mostly inserted, controller withdraws
        for r in self.rods:
            r.insertion = r.target = self.bank_target
        self.az5_active = False
        self._spont_acc = 0.0
        self._prev_n = 0
        self._n_rate = 0.0                  # smoothed dN/dt for the controller
        self.fissions_per_s = 0.0           # smoothed, for HUD
        self._fission_count = 0

    # ------------------------------------------------------------- stats

    @property
    def neutron_count(self) -> int:
        return len(self.neutrons)

    @property
    def avg_heat(self) -> float:
        return float(self.heat.mean())

    @property
    def steam_fraction(self) -> float:
        return float(self.steam.sum()) / max(1, int(self.water_mask.sum()))

    @property
    def xenon_count(self) -> int:
        return len(self.xenon)

    @property
    def avg_insertion(self) -> float:
        return sum(r.insertion for r in self.rods) / len(self.rods)

    # ------------------------------------------------------------- controls

    def scram(self) -> None:
        self.az5_active = True
        self.auto_control = False
        for r in self.rods:
            r.target = 1.0

    def set_bank_target(self, value: float) -> None:
        self.bank_target = min(1.0, max(0.0, value))
        if not self.az5_active:
            for r in self.rods:
                r.target = self.bank_target

    # ------------------------------------------------------------- stepping

    def step(self, dt: float) -> None:
        cfg = self.cfg
        if self.auto_control and not self.az5_active:
            self._auto_control(dt)
        self._move_rods(dt)
        self._rebuild_eff()
        self._update_neutrons(dt)
        self._update_heat(dt)
        self._update_xenon(dt)
        self._spontaneous(dt)
        # exponentially smoothed fission rate for the HUD
        self.fissions_per_s += (self._fission_count / dt - self.fissions_per_s) * min(1.0, dt * 2.0)
        self._fission_count = 0
        self.time += dt

    def _auto_control(self, dt: float) -> None:
        cfg = self.cfg
        n = len(self.neutrons)
        self._n_rate += ((n - self._prev_n) / dt - self._n_rate) * min(1.0, dt * 8.0)
        self._prev_n = n
        scale = max(self.target_n, 50.0)
        err = (n - self.target_n) / scale
        growth = self._n_rate / scale  # damping term: react to power trend, not just level
        u = cfg.auto_gain * err + cfg.auto_damping * growth
        rate = max(-1.0, min(1.0, u)) * cfg.auto_rod_speed
        self.set_bank_target(self.bank_target + rate * dt)

    def _move_rods(self, dt: float) -> None:
        speed = self.cfg.scram_speed if self.az5_active else self.cfg.rod_speed
        for r in self.rods:
            d = r.target - r.insertion
            if d:
                step = speed * dt
                r.insertion = r.target if abs(d) <= step else r.insertion + math.copysign(step, d)

    def _rebuild_eff(self) -> None:
        """Overlay rods onto the base lattice. The graphite displacer tip is at
        the leading (bottom) edge, so on insertion it enters the core first."""
        eff = list(self.base)
        w, h = self.w, self.h
        tip = self.cfg.rod_tip_fraction
        for r in self.rods:
            tip_end = int(r.insertion * h + 1e-9)
            boron_end = int(max(0.0, r.insertion - tip) * h + 1e-9)
            col = r.col
            for iy in range(boron_end):
                eff[iy * w + col] = 6
            for iy in range(boron_end, tip_end):
                eff[iy * w + col] = 7
        self.eff = eff

    def _update_neutrons(self, dt: float) -> None:
        cfg = self.cfg
        eff = self.eff
        w = self.w
        cp = cfg.cell_px
        w_px, h_px = self.w * cp, self.h * cp
        rand = self.rng.random
        ns = self.neutrons
        spawned: list[Neutron] = []
        p_ft, p_ff = cfg.p_fission_thermal, cfg.p_fission_fast
        p_mod = cfg.p_mod_thermalize
        p_wt, p_wa = cfg.p_water_thermalize, cfg.p_water_absorb
        p_st, p_sa = cfg.p_steam_thermalize, cfg.p_steam_absorb
        p_xe = cfg.p_xenon_absorb
        p_rt, p_rf = cfg.p_rod_absorb_thermal, cfg.p_rod_absorb_fast

        write = 0
        for n in ns:
            n.px = n.x
            n.py = n.y
            x = n.x + n.vx * dt
            y = n.y + n.vy * dt
            if x < 0.0 or x >= w_px or y < 0.0 or y >= h_px:
                continue  # leakage
            n.x = x
            n.y = y
            c = eff[int(y / cp) * w + int(x / cp)]
            alive = True
            if c == 2:  # FUEL
                if n.thermal:
                    if rand() < p_ft:
                        alive = False
                        self._fission(int(x / cp), int(y / cp), spawned)
                elif rand() < p_ff:
                    alive = False
                    self._fission(int(x / cp), int(y / cp), spawned)
            elif c == 4 or c == 7:  # MODERATOR / graphite ROD_TIP
                if not n.thermal and rand() < p_mod:
                    self._thermalize(n)
            elif c == 0:  # liquid WATER
                if n.thermal:
                    if rand() < p_wa:
                        alive = False
                elif rand() < p_wt:
                    self._thermalize(n)
            elif c == 1:  # STEAM: nearly transparent
                if n.thermal:
                    if rand() < p_sa:
                        alive = False
                elif rand() < p_st:
                    self._thermalize(n)
            elif c == 5:  # XENON
                if n.thermal and rand() < p_xe:
                    alive = False
                    idx = int(y / cp) * w + int(x / cp)
                    self.base[idx] = 2  # burned off -> fuel again
                    self.xenon.pop(idx, None)
            elif c == 6:  # ROD_BORON
                if rand() < (p_rt if n.thermal else p_rf):
                    alive = False
            if alive:
                ns[write] = n
                write += 1
        del ns[write:]
        room = cfg.max_neutrons - len(ns)
        if room > 0:
            ns.extend(spawned[:room])

    def _fission(self, ix: int, iy: int, spawned: list[Neutron]) -> None:
        cfg = self.cfg
        rng = self.rng
        self.heat[iy, ix] += cfg.heat_per_fission
        self._fission_count += 1
        idx = iy * self.w + ix
        if rng.random() < cfg.p_iodine and self.base[idx] == 2 and idx not in self.iodine:
            self.iodine[idx] = cfg.iodine_decay_s
        cx = (ix + 0.5) * cfg.cell_px
        cy = (iy + 0.5) * cfg.cell_px
        count = rng.randint(cfg.fission_neutrons_min, cfg.fission_neutrons_max)
        for _ in range(count):
            a = rng.random() * math.tau
            x = cx + rng.uniform(-4, 4)
            y = cy + rng.uniform(-4, 4)
            spawned.append(Neutron(x, y, math.cos(a) * cfg.fast_speed,
                                   math.sin(a) * cfg.fast_speed, False, x, y))

    def _thermalize(self, n: Neutron) -> None:
        n.thermal = True
        a = self.rng.random() * math.tau  # scattering randomizes direction
        s = self.cfg.thermal_speed
        n.vx = math.cos(a) * s
        n.vy = math.sin(a) * s

    def _update_heat(self, dt: float) -> None:
        cfg = self.cfg
        self._heat_steps += 1
        if self._heat_steps < cfg.diffusion_every_steps:
            return
        dte = dt * self._heat_steps
        self._heat_steps = 0
        h = self.heat

        lap = np.zeros_like(h)
        lap[1:, :] += h[:-1, :] - h[1:, :]
        lap[:-1, :] += h[1:, :] - h[:-1, :]
        lap[:, 1:] += h[:, :-1] - h[:, 1:]
        lap[:, :-1] += h[:, 1:] - h[:, :-1]
        h += cfg.heat_diffusion * lap

        liquid = self.water_mask & ~self.steam
        cool = np.full_like(h, cfg.passive_cooling)
        pump = cfg.pump_cooling * self.pump_power
        cool[liquid] += pump
        cool[self.steam] += pump * cfg.steam_cooling_factor
        h -= cool * dte
        np.clip(h, 0.0, cfg.max_heat, out=h)

        # boiling / condensation with hysteresis
        boil = self.water_mask & ~self.steam & (h > cfg.boil_temp)
        cond = self.steam & (h < cfg.condense_temp)
        if boil.any() or cond.any():
            self.steam |= boil
            self.steam &= ~cond
            base = self.base
            w = self.w
            for iy, ix in np.argwhere(boil):
                base[iy * w + ix] = 1
            for iy, ix in np.argwhere(cond):
                base[iy * w + ix] = 0

    @staticmethod
    def _decay(table: dict[int, float], dt: float) -> list[int]:
        """Tick every timer down; drop and return the cells that expired."""
        done = []
        for idx, t in table.items():
            t -= dt
            if t <= 0.0:
                done.append(idx)
            else:
                table[idx] = t
        for idx in done:
            del table[idx]
        return done

    def _update_xenon(self, dt: float) -> None:
        for idx in self._decay(self.iodine, dt):
            if self.base[idx] == 2:
                self.base[idx] = 5
                self.xenon[idx] = self.cfg.xenon_decay_s
        for idx in self._decay(self.xenon, dt):
            if self.base[idx] == 5:
                self.base[idx] = 2

    def inject(self, count: int) -> None:
        """Spawn `count` fast neutrons at random positions (test/scenario helper)."""
        cfg = self.cfg
        rng = self.rng
        for _ in range(count):
            if len(self.neutrons) >= cfg.max_neutrons:
                break
            x = rng.uniform(0, self.w * cfg.cell_px)
            y = rng.uniform(0, self.h * cfg.cell_px)
            a = rng.random() * math.tau
            self.neutrons.append(Neutron(x, y, math.cos(a) * cfg.fast_speed,
                                         math.sin(a) * cfg.fast_speed, False, x, y))

    def _spontaneous(self, dt: float) -> None:
        self._spont_acc += self.cfg.spontaneous_rate * dt
        while self._spont_acc >= 1.0:
            self._spont_acc -= 1.0
            self.inject(1)

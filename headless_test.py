"""Headless staged verification of the reactor physics (no pygame).

Stages mirror the build plan:
  1. chain reaction is controllable (auto controller holds a power target)
  2. positive void coefficient (losing coolant raises power with rods frozen)
  3. xenon poisoning (power drop -> poison -> rods must withdraw to recover)
  4. graphite tip flaw (tip-only insertion is MORE reactive than rods fully
     out, and a scram from a low-rod state spikes power before it falls)

Run:  py headless_test.py
"""
from __future__ import annotations

import copy
import time

from config import Config
from core import Reactor

DT = 1.0 / 120.0


def run(r: Reactor, seconds: float, report_every: float = 0.0) -> None:
    steps = int(seconds / DT)
    next_report = r.time + report_every
    for _ in range(steps):
        r.step(DT)
        if report_every and r.time >= next_report:
            next_report += report_every
            print(f"  t={r.time:6.1f}s  n={r.neutron_count:5d}  heat={r.avg_heat:.3f}  "
                  f"steam={r.steam_fraction * 100:4.1f}%  Xe={r.xenon_count:3d}  "
                  f"rods={r.avg_insertion * 100:4.1f}%")


def mean_count(r: Reactor, seconds: float) -> float:
    steps = int(seconds / DT)
    total = 0
    for _ in range(steps):
        r.step(DT)
        total += r.neutron_count
    return total / steps


def main() -> None:
    results: dict[str, bool] = {}
    t0 = time.perf_counter()
    cfg = Config()

    # ---- stage 1: controllable chain reaction -------------------------
    print("== stage 1: chain reaction under automatic control (target 700) ==")
    r = Reactor(cfg)
    run(r, 65.0, report_every=10.0)
    n1 = mean_count(r, 10.0)
    results["1 chain reaction controllable"] = 400 < n1 < 1100 and r.steam_fraction < 0.05
    print(f"  -> mean n over last 10s: {n1:.0f}, steam={r.steam_fraction * 100:.1f}%, "
          f"insertion={r.avg_insertion * 100:.1f}%\n")

    # ---- stage 2: positive void coefficient ---------------------------
    print("== stage 2: void coefficient (freeze rods, cut pumps to 10%) ==")
    r.auto_control = False  # rods frozen from here on
    base = mean_count(r, 3.0)
    r.pump_power = 0.10
    run(r, 25.0, report_every=5.0)
    voided = mean_count(r, 2.0)
    results["2 positive void coefficient"] = voided > base * 1.3 and r.steam_fraction > 0.10
    print(f"  -> power {base:.0f} -> {voided:.0f} ({voided / max(base, 1):.2f}x), "
          f"steam={r.steam_fraction * 100:.1f}%\n")

    # ---- stage 3: xenon poisoning --------------------------------------
    print("== stage 3: xenon pit (high power 90s, throttle to 50, wait for Xe) ==")
    r = Reactor(cfg)
    r.target_n = 1500.0
    run(r, 90.0, report_every=30.0)
    r.target_n = 50.0
    run(r, 45.0, report_every=15.0)
    xe_after_drop = r.xenon_count
    # same rod configuration, clean core vs poisoned core: xenon should hold
    # the poisoned one subcritical where the clean one grows
    r.auto_control = False
    r.bank_target = 0.25
    for rod in r.rods:
        rod.insertion = rod.target = 0.25
    r.neutrons.clear()
    r.inject(1000)
    run(r, 12.0)
    n_poisoned = r.neutron_count

    rc = Reactor(cfg)
    rc.auto_control = False
    rc.bank_target = 0.25
    for rod in rc.rods:
        rod.insertion = rod.target = 0.25
    rc.inject(1000)
    run(rc, 12.0)
    n_clean = rc.neutron_count
    results["3 xenon poisoning"] = xe_after_drop > 40 and n_clean > n_poisoned * 1.5
    print(f"  -> Xe cells after power drop: {xe_after_drop}; 12s growth from 1000 "
          f"at rods=25%: clean n={n_clean}, poisoned n={n_poisoned} "
          f"({n_clean / max(n_poisoned, 1):.2f}x)\n")

    # ---- stage 4a: graphite tip adds reactivity -------------------------
    print("== stage 4a: tip-only insertion vs rods fully withdrawn ==")
    ra = Reactor(cfg)
    ra.auto_control = False
    ra.set_bank_target(0.0)
    for rod in ra.rods:
        rod.insertion = 0.0
    ra.inject(800)
    run(ra, 12.0)
    na = ra.neutron_count
    rb = Reactor(cfg)
    rb.auto_control = False
    rb.set_bank_target(0.15)  # only the graphite displacer is in the core
    for rod in rb.rods:
        rod.insertion = 0.15
    rb.inject(800)
    run(rb, 12.0)
    nb = rb.neutron_count
    results["4a graphite tip adds reactivity"] = nb > na * 1.15
    print(f"  -> 12s growth from 800 injected: rods out n={na}, tips-only n={nb} "
          f"({nb / max(na, 1):.2f}x)\n")

    # ---- stage 4b: AZ-5 from rods-out initially ADDS reactivity ---------
    # Scram from fully withdrawn: boron only reaches the core after
    # d > tip_fraction, i.e. t > 0.15 * 18s = 2.7s. Until then the scram is
    # pure graphite insertion. Large population to beat stochastic noise.
    print("== stage 4b: scram spike (branch: AZ-5 vs rods frozen, n=20000) ==")
    cfg4 = Config(max_neutrons=40000)
    r = Reactor(cfg4)
    r.auto_control = False
    r.set_bank_target(0.0)
    for rod in r.rods:
        rod.insertion = 0.0
    r.inject(20000)
    run(r, 0.5)  # let the injected burst settle
    branch_scram = copy.deepcopy(r)
    branch_hold = r
    branch_scram.scram()
    sum_s = sum_h = 0
    steps = int(2.6 / DT)
    w0 = int(1.0 / DT)
    for i in range(steps):
        branch_scram.step(DT)
        branch_hold.step(DT)
        if i >= w0:
            sum_s += branch_scram.neutron_count
            sum_h += branch_hold.neutron_count
    ratio = sum_s / max(sum_h, 1)
    results["4b scram initially adds power"] = ratio > 1.005
    print(f"  -> mean n over tip-only window: scrammed {sum_s // (steps - w0)} vs "
          f"frozen {sum_h // (steps - w0)} ({ratio:.3f}x)  [tips enter before boron]\n")

    # ---- summary --------------------------------------------------------
    print(f"== summary ({time.perf_counter() - t0:.1f}s wall) ==")
    ok = True
    for name, passed in results.items():
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
        ok &= passed
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()

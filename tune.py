"""Auto-tuner for the RBMK physics constants.

Finds config values that keep the Chernobyl scenario (the five staged checks
in headless_test.py) working for the current lattice geometry (water_cols),
without hand-picking numbers.

Method: the known-good geometry (water_cols=1 with the PRISTINE constants
below) is probed to establish reference behavior bands. The target geometry
is then probed with candidate constants, and each out-of-band metric drives
the knob it most influences via a proportional step in log space (Rprop-style
adaptive gain: halve on sign flip, grow while the sign persists). All knobs
stay inside realism bounds and physical ordering constraints. When every
probe is in band, the candidate is written into config.py and the real
headless_test.py runs as the final gate.

Run:  py tune.py [--water-cols N] [--max-iters 40] [--seeds 1986,42,7]

On success config.py keeps the tuned values; on failure it is restored.
"""
from __future__ import annotations

import argparse
import math
import re
import subprocess
import sys
from pathlib import Path
from statistics import fmean

from config import Config
from core import Cell, Reactor

DT = 1.0 / 120.0
CONFIG_PATH = Path(__file__).with_name("config.py")
MAX_LOG_STEP = math.log(1.25)   # per-knob change cap per iteration
SHUTDOWN_MAX = -0.05            # growth/s at full insertion must stay below this
STEAM_EQ_MAX = 0.05             # steam fraction allowed at equilibrium power

# known-good constants (committed values that pass headless_test at water_cols=1)
PRISTINE = {
    "p_fission_thermal": 0.12,
    "p_mod_thermalize": 0.055,
    "p_water_thermalize": 0.008,
    "p_water_absorb": 0.013,
    "p_xenon_absorb": 0.30,
    "p_iodine": 0.09,
    "heat_per_fission": 0.30,
    "pump_cooling": 0.80,
}

# realism bounds, anchored roughly 0.3x-3x around PRISTINE
BOUNDS = {
    "p_fission_thermal": (0.05, 0.35),
    "p_mod_thermalize": (0.02, 0.15),
    "p_water_thermalize": (0.002, 0.025),
    "p_water_absorb": (0.004, 0.04),
    # Xe-135's capture cross-section dwarfs boron's, so unlike the other
    # probabilities it may realistically exceed p_rod_absorb_thermal
    "p_xenon_absorb": (0.10, 0.60),
    "p_iodine": (0.03, 0.25),
    "heat_per_fission": (0.10, 0.90),
    "pump_cooling": (0.30, 2.40),
}

# metric -> (knob, effect sign of knob increase on the metric, weight)
MAPPINGS = [
    ("crit", "p_fission_thermal", +1, 1.0),
    ("crit", "p_mod_thermalize", +1, 0.5),
    ("crit", "p_water_absorb", -1, 0.4),   # spillover; void metric pushes back up
    ("void", "p_water_absorb", +1, 1.0),
    ("potency", "p_xenon_absorb", +1, 1.0),
    ("xe_count", "p_iodine", +1, 0.5),  # advisory: gate stage 3 is the arbiter
    ("heat", "heat_per_fission", +1, 1.0),
    ("steam_cut", "pump_cooling", -1, 1.0),
]
FULL_ONLY = {"xe_count", "heat", "steam_cut"}  # need a near-critical core to measure


def clamp(v: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, v))


def make_config(params: dict, water_cols: int, seed: int) -> Config:
    return Config(seed=seed, water_cols=water_cols, max_neutrons=20000,
                  p_tip_thermalize=params["p_mod_thermalize"], **params)


def run_steps(r: Reactor, seconds: float) -> None:
    for _ in range(int(seconds / DT)):
        r.step(DT)


def growth(params: dict, wc: int, seed: int, insertion: float, *,
           voided: bool = False, xenon_step: int = 0,
           seconds: float = 12.0, inject: int = 1000) -> float:
    """Population growth rate (ln ratio / s) with rods frozen at `insertion`.

    Pure neutronics: phase changes are disabled so the chosen coolant phase
    persists for the whole probe (thermal-hydraulics has its own probe)."""
    cfg = make_config(params, wc, seed)
    cfg.boil_temp = 1e9
    cfg.condense_temp = -1e9
    r = Reactor(cfg)
    r.auto_control = False
    for rod in r.rods:
        rod.insertion = rod.target = insertion
    if xenon_step:  # poison every xenon_step-th fuel cell
        fuel = [i for i, c in enumerate(r.base) if c == int(Cell.FUEL)]
        for i in fuel[::xenon_step]:
            r.base[i] = int(Cell.XENON)
            r.xenon[i] = 9999.0
    if voided:  # flash all coolant to steam
        r.steam |= r.water_mask
        for i, c in enumerate(r.base):
            if c == int(Cell.WATER):
                r.base[i] = int(Cell.STEAM)
    r.inject(inject)
    run_steps(r, seconds)
    return math.log(max(r.neutron_count, 1) / inject) / seconds


def controller_probe(params: dict, wc: int, seed: int) -> tuple[float, float, float, float]:
    """60s under the auto controller at target 700, then 25s with pumps at 10%."""
    r = Reactor(make_config(params, wc, seed))
    n_fuel = sum(1 for c in r.base if c == int(Cell.FUEL))
    run_steps(r, 60.0)
    xe = r.xenon_count / n_fuel  # fraction of fuel poisoned: geometry-neutral
    heat, steam_eq = r.avg_heat, r.steam_fraction
    r.auto_control = False
    r.pump_power = 0.10
    run_steps(r, 25.0)
    return xe, heat, steam_eq, r.steam_fraction


def measure(params: dict, wc: int, seeds: list[int], full: bool) -> dict:
    m: dict[str, float] = {}
    m["crit"] = fmean(growth(params, wc, s, 0.25) for s in seeds)
    m["shutdown"] = fmean(growth(params, wc, s, 1.0, seconds=6.0) for s in seeds)
    m["void"] = fmean(
        growth(params, wc, s, 0.25, voided=True, seconds=4.0)
        - growth(params, wc, s, 0.25, seconds=4.0) for s in seeds)
    m["potency"] = m["crit"] - fmean(
        growth(params, wc, s, 0.25, xenon_step=3) for s in seeds)
    if full:
        probes = [controller_probe(params, wc, s) for s in seeds]
        m["xe_count"] = fmean(p[0] for p in probes)
        m["heat"] = fmean(p[1] for p in probes)
        m["steam_eq"] = fmean(p[2] for p in probes)
        m["steam_cut"] = fmean(p[3] for p in probes)
    return m


def make_bands(ref: dict) -> dict:
    return {
        "crit": (ref["crit"], max(0.4 * abs(ref["crit"]), 0.02)),
        "void": (ref["void"], max(0.5 * ref["void"], 0.02)),
        "potency": (ref["potency"], max(0.5 * ref["potency"], 0.01)),
        "xe_count": (ref["xe_count"], max(0.5 * ref["xe_count"], 0.02)),
        "heat": (ref["heat"], max(0.5 * ref["heat"], 0.02)),
        "steam_cut": (ref["steam_cut"], max(0.5 * ref["steam_cut"], 0.04)),
    }


def apply_constraints(p: dict) -> None:
    for k, (lo, hi) in BOUNDS.items():
        p[k] = clamp(p[k], lo, hi)
    p["p_water_thermalize"] = min(p["p_water_thermalize"], 0.8 * p["p_mod_thermalize"])
    p["p_water_absorb"] = max(p["p_water_absorb"], 10 * Config.p_steam_absorb)


def update(params: dict, metrics: dict, bands: dict, gains: dict,
           prev_step: dict, near_crit: bool) -> None:
    steps = dict.fromkeys(params, 0.0)
    for metric, knob, effect, weight in MAPPINGS:
        if metric not in metrics or (metric in FULL_ONLY and not near_crit):
            continue
        mid, hw = bands[metric]
        e = (metrics[metric] - mid) / hw
        if abs(e) <= 1.0:
            continue
        steps[knob] += -gains[knob] * weight * clamp(e, -2.0, 2.0) * effect
    for k, s in steps.items():
        if not s:
            continue
        s = clamp(s, -MAX_LOG_STEP, MAX_LOG_STEP)
        params[k] *= math.exp(s)
        if prev_step[k] * s < 0:
            gains[k] = max(0.02, gains[k] * 0.5)
        elif prev_step[k] * s > 0:
            gains[k] = min(0.4, gains[k] * 1.2)
        prev_step[k] = s
    # guards outside the band machinery
    if metrics["shutdown"] > SHUTDOWN_MAX:
        params["p_fission_thermal"] *= 0.92
    if metrics.get("steam_eq", 0.0) > STEAM_EQ_MAX:
        params["pump_cooling"] *= 1.15
    apply_constraints(params)


def converged(metrics: dict, bands: dict) -> bool:
    needed = ["crit", "void", "potency", "heat", "steam_cut"]
    if any(k not in metrics for k in needed):
        return False
    if any(abs((metrics[k] - bands[k][0]) / bands[k][1]) > 1.0 for k in needed):
        return False
    return metrics["shutdown"] <= SHUTDOWN_MAX and metrics["steam_eq"] <= STEAM_EQ_MAX


# ------------------------------------------------------------- config file I/O

def fmt_value(v: float) -> str:
    s = f"{v:.4g}"
    return s if ("." in s or "e" in s) else s + ".0"


def write_config(params: dict, wc: int) -> None:
    text = CONFIG_PATH.read_text(encoding="utf-8")
    values: dict[str, str] = {k: fmt_value(v) for k, v in params.items()}
    values["p_tip_thermalize"] = fmt_value(params["p_mod_thermalize"])
    values["water_cols"] = str(wc)
    for name, lit in values.items():
        pat = rf"^(\s*{name}: (?:float|int) = )[-+0-9.eE]+"
        text, n = re.subn(pat, lambda mo: mo.group(1) + lit, text, count=1, flags=re.M)
        if n != 1:
            raise RuntimeError(f"could not rewrite {name} in config.py")
    CONFIG_PATH.write_text(text, encoding="utf-8")


def run_gate() -> tuple[bool, list[str], str]:
    proc = subprocess.run([sys.executable, "headless_test.py"],
                          capture_output=True, text=True,
                          cwd=CONFIG_PATH.parent, timeout=600)
    out = proc.stdout + proc.stderr
    fails = re.findall(r"\[FAIL\] (\S+)", out)
    return proc.returncode == 0, fails, out


def nudge_for_gate_fails(params: dict, fails: list[str], gate_out: str) -> None:
    for stage in fails:
        if stage == "1":
            mo = re.search(r"mean n over last 10s: (\d+)", gate_out)
            n = int(mo.group(1)) if mo else 0
            if n < 400:
                params["p_fission_thermal"] *= 1.06
            elif n > 1100:
                params["p_fission_thermal"] *= 0.94
            else:
                params["pump_cooling"] *= 1.10  # steam limit was the failure
        elif stage == "2":
            mo = re.search(r"power \d+ -> \d+ .*steam=\s*([\d.]+)%", gate_out)
            if mo and float(mo.group(1)) < 10.0:
                params["pump_cooling"] *= 0.88  # coolant never boiled
            else:
                params["p_water_absorb"] *= 1.08
                params["p_water_thermalize"] *= 0.93
        elif stage == "3":
            params["p_xenon_absorb"] *= 1.05
            params["p_iodine"] *= 1.10
            # crit-neutral pair: slower fission lengthens thermal-neutron
            # lifetimes in fuel so the xenon cells actually see traffic
            params["p_fission_thermal"] *= 0.96
            params["p_water_absorb"] *= 0.95
        elif stage in ("4a", "4b"):
            params["p_water_absorb"] *= 1.05
            params["p_water_thermalize"] *= 0.95
    apply_constraints(params)


# ------------------------------------------------------------- main loop

def print_metrics(metrics: dict, bands: dict) -> None:
    for k, v in metrics.items():
        if k in bands:
            mid, hw = bands[k]
            mark = "ok " if abs((v - mid) / hw) <= 1.0 else "OUT"
            print(f"    {k:10s} {v:8.4f}  band {mid - hw:8.4f} .. {mid + hw:8.4f}  {mark}")
        else:
            print(f"    {k:10s} {v:8.4f}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Tune RBMK config constants for a geometry")
    ap.add_argument("--water-cols", type=int, default=Config.water_cols)
    ap.add_argument("--max-iters", type=int, default=40)
    ap.add_argument("--seeds", type=str, default="1986,42,7")
    args = ap.parse_args()
    seeds = [int(s) for s in args.seeds.split(",")]
    wc = args.water_cols

    print(f"== reference: water_cols=1, pristine constants, seeds {seeds} ==")
    ref = measure(dict(PRISTINE), 1, seeds, full=True)
    bands = make_bands(ref)
    print_metrics(ref, bands)

    params = dict(PRISTINE)
    gains = dict.fromkeys(params, 0.18)
    prev_step = dict.fromkeys(params, 0.0)
    original = CONFIG_PATH.read_text(encoding="utf-8")
    success = False
    try:
        gated = False  # after the first gate attempt the gate outranks all probes
        for it in range(1, args.max_iters + 1):
            if gated:
                # gate-driven endgame: only criticality must hold before regating
                metrics = {
                    "crit": fmean(growth(params, wc, s, 0.25) for s in seeds),
                    "shutdown": fmean(growth(params, wc, s, 1.0, seconds=6.0) for s in seeds),
                }
                print(f"\n== iter {it}/{args.max_iters}  water_cols={wc} (gate-driven) ==")
                print_metrics(metrics, bands)
                print("    " + "  ".join(f"{k}={v:.4g}" for k, v in params.items()))
                if (abs((metrics["crit"] - bands["crit"][0]) / bands["crit"][1]) > 1.0
                        or metrics["shutdown"] > SHUTDOWN_MAX):
                    before = dict(params)
                    update(params, metrics, bands, gains, prev_step, near_crit=False)
                    if params == before:
                        print("  no knob can move (bounds/deadband) -> giving up early")
                        break
                    continue
            else:
                near = it > 1 and abs((metrics["crit"] - bands["crit"][0]) / bands["crit"][1]) <= 1.5
                metrics = measure(params, wc, seeds, full=near)
                print(f"\n== iter {it}/{args.max_iters}  water_cols={wc} ==")
                print_metrics(metrics, bands)
                print("    " + "  ".join(f"{k}={v:.4g}" for k, v in params.items()))
                if not converged(metrics, bands):
                    near = abs((metrics["crit"] - bands["crit"][0]) / bands["crit"][1]) <= 1.5
                    before = dict(params)
                    update(params, metrics, bands, gains, prev_step, near)
                    if params == before:
                        print("  no knob can move (bounds/deadband) -> giving up early")
                        break
                    continue
                print("  all probes in band -> running headless_test.py gate")
            write_config(params, wc)
            ok, fails, out = run_gate()
            print("\n".join("    | " + ln for ln in out.strip().splitlines()))
            if ok:
                success = True
                break
            gated = True
            if "1" not in fails:
                # stage 1 passes here, so hold criticality at this level while
                # the gate nudges walk the remaining knobs
                bands["crit"] = (metrics["crit"], bands["crit"][1])
            print(f"  gate failed stages {fails}, nudging and continuing")
            before = dict(params)
            nudge_for_gate_fails(params, fails, out)
            if params == before:
                print("  gate nudges pinned at bounds -> giving up early")
                break
    finally:
        if not success:
            CONFIG_PATH.write_text(original, encoding="utf-8")

    if success:
        print("\n== tuned values written to config.py ==")
        for k in PRISTINE:
            old = float(re.search(rf"{k}: float = ([-+0-9.eE]+)", original).group(1))
            print(f"  {k:22s} {old:8.4g} -> {params[k]:8.4g}")
        print(f"  water_cols = {wc}")
    else:
        print("\n== FAILED to converge; config.py restored ==")
        print("  last candidate: " + "  ".join(f"{k}={v:.4g}" for k, v in params.items()))
        raise SystemExit(1)


if __name__ == "__main__":
    main()

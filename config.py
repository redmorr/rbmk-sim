"""All tunable constants for the RBMK simulation live here.

Probabilities marked "per step" are evaluated once per physics step
(default 120 Hz) while a neutron is inside a cell of that type.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Config:
    # --- lattice ---
    grid_w: int = 60
    grid_h: int = 40
    cell_px: int = 16
    inert_fraction: float = 0.20      # fraction of fuel-column cells that are inert
    seed: int = 1986

    # --- timing ---
    physics_hz: int = 120
    max_steps_per_frame: int = 48     # cap physics catch-up per rendered frame

    # --- neutrons ---
    fast_speed: float = 240.0         # px / s
    thermal_speed: float = 80.0
    max_neutrons: int = 8000          # hard cap (explosion threshold is below this)
    spontaneous_rate: float = 4.0     # background source, neutrons / s
    fission_neutrons_min: int = 2
    fission_neutrons_max: int = 3

    # --- interaction probabilities (per physics step) ---
    p_fission_thermal: float = 0.12   # thermal neutron in FUEL -> fission
    p_fission_fast: float = 0.001     # fast fission (tiny, RBMK is thermal)
    p_mod_thermalize: float = 0.055   # fast neutron in graphite -> thermal
                                      # (the rod's graphite displacer tip too)
    p_water_thermalize: float = 0.008 # liquid water moderates only weakly
    p_water_absorb: float = 0.013     # liquid water absorbs thermal neutrons
    p_steam_thermalize: float = 0.0004
    p_steam_absorb: float = 0.0004    # steam does almost nothing
    p_xenon_absorb: float = 0.30      # Xe-135 is a huge absorber
    p_rod_absorb_thermal: float = 0.45
    p_rod_absorb_fast: float = 0.10

    # --- heat / thermal-hydraulics ---
    heat_per_fission: float = 0.30    # heat units deposited in the fission cell
    heat_diffusion: float = 0.09      # per diffusion pass
    diffusion_every_steps: int = 4
    pump_cooling: float = 0.80        # heat/s removed from liquid water at 100% pumps
    steam_cooling_factor: float = 0.35  # steam transfers heat poorly
    passive_cooling: float = 0.02     # heat/s removed everywhere
    boil_temp: float = 1.0            # liquid -> steam above this
    condense_temp: float = 0.85       # steam -> liquid below this (hysteresis)
    max_heat: float = 6.0

    # --- xenon-135 chain ---
    p_iodine: float = 0.09            # per fission: cell marked as iodine precursor
    iodine_decay_s: float = 30.0      # iodine -> xenon (scaled ~6.6h half-life)
    xenon_decay_s: float = 80.0       # xenon natural decay (scaled ~9.2h half-life)

    # --- control rods ---
    rod_tip_fraction: float = 0.15    # graphite displacer at the leading (bottom) end
    rod_speed: float = 0.08           # manual/auto travel, fraction of core / s
    scram_speed: float = 1.0 / 18.0   # AZ-5: 18 s full travel, painfully slow

    # --- automatic power controller ---
    auto_gain: float = 0.6            # insertion response to relative power error
    auto_damping: float = 10.0        # response to power rate-of-change (stabilizer)
    auto_rod_speed: float = 0.05      # rate limit, fraction of core / s
    default_target_n: float = 700.0

    # --- misc ---
    explosion_threshold: int = 6000   # neutron count = destroyed reactor
    pump_power_default: float = 1.0
    graph_window_s: float = 60.0
    graph_sample_dt: float = 0.25
    temp_display_scale: float = 900.0  # fake degC = 280 + avg_heat * this

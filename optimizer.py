"""Cost-optimal battery schedule (no Home Assistant imports).

Given prices, expected consumption and expected PV production per time slot,
find the battery mode for every slot that minimises the electricity bill.

The three modes are the ones the HyCube can be put into:
  standard     PV surplus charges the battery, a deficit is covered from the
               battery down to the reserve, the rest comes from the grid
  hold         as standard, but the battery does not discharge
  grid_charge  the battery charges from the grid, the home runs on the grid

The schedule is found by dynamic programming over the stored energy (1 % SoC
steps, value function interpolated linearly). Energy is counted in kWh the
battery can still deliver, so discharging is loss-free in this model and all
battery losses sit on the charging side (charge_eff, grid_eff) - exactly how
the integration measures capacity and efficiency.

After "hold" the HyCube was seen not to discharge for a while. The model
therefore keeps the battery idle for `release_slots` slots after leaving
"hold", so a plan ends "hold" early enough to have the battery back in time.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .const import (
    MODE_GRID_CHARGE,
    MODE_HOLD,
    MODE_STANDARD,
    PLAN_SWITCH_COST,
    SOURCE_BATTERY,
    SOURCE_GRID,
    SOURCE_GRID_CHARGE,
    SOURCE_SOLAR,
)

_EPS = 1e-9
_INF = float("inf")
# What the battery does in a slot: one of the modes, or idle after "hold"
# (the device is in "standard" but does not discharge yet).
_IDLE = "idle"
# Planning state: (device mode, slots the battery stays idle in "standard").
_State = tuple[str, int]


@dataclass(frozen=True)
class Slot:
    """Forecast for one time slot."""

    start: float  # unix timestamp
    hours: float  # length
    price: float  # EUR per kWh from the grid
    load: float  # kWh the home consumes
    pv: float  # kWh the PV produces


@dataclass(frozen=True)
class Battery:
    """Battery model and what the plan may do with it."""

    capacity: float  # kWh delivered per 100 % SoC
    floor: float  # % SoC the battery is not discharged below
    charge_eff: float  # deliverable kWh per kWh charged
    grid_eff: float  # deliverable kWh per kWh taken from the grid
    grid_power: float  # kW drawn from the grid while grid charging
    ceiling: float = 100.0  # % SoC when full
    max_grid_soc: float = 100.0  # % SoC grid charging stops at
    export_price: float = 0.0  # EUR per kWh fed into the grid
    shift_cost: float = 0.01  # EUR per kWh held back or grid charged
    switch_cost: float = PLAN_SWITCH_COST  # EUR per mode change
    release_slots: int = 0  # slots without discharge after leaving "hold"
    allow_hold: bool = True
    allow_grid_charge: bool = True


@dataclass(frozen=True)
class PlannedSlot:
    """One slot of the schedule (energies in kWh)."""

    start: float
    hours: float
    mode: str
    source: str
    price: float
    load: float
    pv: float
    grid: float  # imported
    export: float
    battery: float  # > 0 charged, < 0 delivered to the home
    soc: float  # % at the end of the slot


@dataclass(frozen=True)
class Block:
    """Consecutive slots with the same mode and source."""

    start: float
    end: float
    mode: str
    source: str
    price: float  # time-weighted average
    load: float
    pv: float
    grid: float
    export: float
    battery: float
    soc: float  # % at the end


@dataclass(frozen=True)
class Plan:
    """Schedule with its expected cost."""

    slots: list[PlannedSlot]
    cost: float  # EUR, grid import minus export revenue
    baseline_cost: float  # EUR with "standard" throughout
    savings: float  # EUR against the baseline, leftover energy accounted for

    def blocks(self) -> list[Block]:
        """Slots merged into stretches of equal mode and source."""
        out: list[Block] = []
        for s in self.slots:
            end = s.start + s.hours * 3600
            if out and out[-1].mode == s.mode and out[-1].source == s.source:
                b = out[-1]
                hours = (b.end - b.start) / 3600
                out[-1] = Block(
                    start=b.start,
                    end=end,
                    mode=b.mode,
                    source=b.source,
                    price=(b.price * hours + s.price * s.hours) / (hours + s.hours),
                    load=b.load + s.load,
                    pv=b.pv + s.pv,
                    grid=b.grid + s.grid,
                    export=b.export + s.export,
                    battery=b.battery + s.battery,
                    soc=s.soc,
                )
            else:
                out.append(
                    Block(
                        s.start, end, s.mode, s.source, s.price, s.load, s.pv,
                        s.grid, s.export, s.battery, s.soc,
                    )
                )
        return out

    def next_grid_charge(self) -> Block | None:
        """First stretch in which the battery charges from the grid."""
        return next((b for b in self.blocks() if b.mode == MODE_GRID_CHARGE), None)


# One simulated slot: (stored kWh after, EUR paid, EUR penalty, kWh imported,
# kWh exported, kWh into (+) or out of (-) the battery).
_Step = tuple[float, float, float, float, float, float]


def _step(
    slot: Slot, e: float, mode: str, bat: Battery, e_min: float, e_max: float, e_grid: float
) -> _Step | None:
    """Run one slot in a mode or idle; None if the mode makes no sense here."""
    net = slot.load - slot.pv
    imp = exp = batt = pen = 0.0
    if net < 0:
        # Surplus charges the battery in every mode, the rest is exported.
        charged = max(min(-net, (e_max - e) / bat.charge_eff), 0.0)
        e += charged * bat.charge_eff
        exp = -net - charged
        batt = charged
    elif mode == MODE_STANDARD:
        delivered = max(min(net, e - e_min), 0.0)
        e -= delivered
        imp = net - delivered
        batt = -delivered
    else:
        imp = net
        if mode in (MODE_HOLD, _IDLE):
            # Idling after "hold" holds energy back just like "hold" itself;
            # without the margin, one slot of "hold" would buy a free wait.
            pen = bat.shift_cost * max(min(net, e - e_min), 0.0)
    if mode == MODE_GRID_CHARGE:
        taken = min(bat.grid_power * slot.hours, (e_grid - e) / bat.grid_eff)
        if taken <= 1e-6:
            return None  # nothing to charge: this would be a free "hold"
        e += taken * bat.grid_eff
        imp += taken
        batt += taken
        pen = bat.shift_cost * taken * bat.grid_eff
    return (e, imp * slot.price - exp * bat.export_price, pen, imp, exp, batt)


def _interp(values: Sequence[float], x: float) -> float:
    """Value at fractional index x, clamped to the ends."""
    if x <= 0:
        return values[0]
    last = len(values) - 1
    if x >= last:
        return values[last]
    i = int(x)
    return values[i] + (values[i + 1] - values[i]) * (x - i)


def _source(mode: str, slot: Slot, step: _Step) -> str:
    """What mainly supplies the home in a slot."""
    if mode == MODE_GRID_CHARGE:
        return SOURCE_GRID_CHARGE
    solar = min(slot.pv, slot.load)
    battery = max(-step[5], 0.0)
    grid = step[3]
    if solar >= battery and solar >= grid and solar > 0:
        return SOURCE_SOLAR
    if battery >= grid and battery > 0:
        return SOURCE_BATTERY
    return SOURCE_GRID


def _cost_to_go(
    slot: Slot,
    kind: str,
    later: Sequence[float],
    bat: Battery,
    limits: tuple[float, float, float],
    levels: int,
    step_kwh: float,
) -> list[float]:
    """Cost of a slot plus the cost after it, for every stored-energy level."""
    e_min = limits[0]
    row = []
    for i in range(levels):
        step = _step(slot, e_min + i * step_kwh, kind, bat, *limits)
        row.append(
            _INF
            if step is None
            else step[1] + step[2] + _interp(later, (step[0] - e_min) / step_kwh)
        )
    return row


def _advance(state: _State, action: str, release: int) -> tuple[str, _State]:
    """What the battery does when `action` is chosen in `state`, and the state after."""
    mode, idle = state
    if action != MODE_STANDARD:
        return action, (action, 0)
    if mode == MODE_HOLD:
        idle = release
    if idle > 0:
        return _IDLE, (MODE_STANDARD, idle - 1)
    return MODE_STANDARD, (MODE_STANDARD, 0)


def _run(
    slots: Sequence[Slot],
    e: float,
    actions: Sequence[str],
    state: _State,
    bat: Battery,
    limits: tuple[float, float, float],
) -> tuple[list[PlannedSlot], float, float]:
    """Simulate the chosen modes; returns the slots, EUR paid and kWh left."""
    planned: list[PlannedSlot] = []
    money = 0.0
    for slot, action in zip(slots, actions, strict=True):
        kind, after = _advance(state, action, bat.release_slots)
        step = _step(slot, e, kind, bat, *limits)
        if step is None:  # grid charging with a full battery
            action = MODE_STANDARD
            kind, after = _advance(state, action, bat.release_slots)
            step = _step(slot, e, kind, bat, *limits)
        assert step is not None
        state = after
        e = step[0]
        money += step[1]
        planned.append(
            PlannedSlot(
                start=slot.start,
                hours=slot.hours,
                mode=action,
                source=_source(action, slot, step),
                price=slot.price,
                load=slot.load,
                pv=slot.pv,
                grid=step[3],
                export=step[4],
                battery=step[5],
                soc=e / bat.capacity * 100,
            )
        )
    return planned, money, e


def optimize(
    slots: Sequence[Slot],
    soc: float,
    bat: Battery,
    mode: str = MODE_STANDARD,
    idle_slots: int = 0,
) -> Plan:
    """Cheapest schedule for the slots.

    Starts at `soc` % with the device in `mode`; idle_slots is how many slots
    the battery will not discharge yet because "hold" was left recently.
    """
    e_min = bat.floor / 100 * bat.capacity
    e_max = max(bat.ceiling / 100 * bat.capacity, e_min)
    e_grid = min(e_max, bat.max_grid_soc / 100 * bat.capacity)
    limits = (e_min, e_max, e_grid)
    e_start = min(max(soc / 100 * bat.capacity, e_min), e_max)
    release = max(bat.release_slots, 0)
    start: _State = (mode, min(max(idle_slots, 0), release) if mode == MODE_STANDARD else 0)

    base_slots, base_money, base_left = _run(
        slots, e_start, [MODE_STANDARD] * len(slots), start, bat, limits
    )
    baseline = Plan(base_slots, base_money, base_money, 0.0)

    actions = [MODE_STANDARD]
    if bat.allow_hold:
        actions.append(MODE_HOLD)
    if bat.allow_grid_charge and bat.grid_power > 0:
        actions.append(MODE_GRID_CHARGE)
    levels = round(bat.ceiling - bat.floor) + 1
    if not slots or len(actions) == 1 or levels < 2 or e_max - e_min < 1e-6:
        return baseline
    step_kwh = (e_max - e_min) / (levels - 1)
    states: list[_State] = [(a, 0) for a in actions]
    states += [(MODE_STANDARD, n) for n in range(1, release + 1)]
    if start not in states:  # e.g. the device is in "hold" but hold is not planned
        states.append(start)

    # Energy left at the end is worth the cheapest price seen: enough to make
    # using it now the better choice, never enough to charge for it.
    leftover = max(min(s.price for s in slots), 0.0)
    terminal = [-i * step_kwh * leftover for i in range(levels)]

    # value[t][state before slot t][level] = cheapest cost from slot t on
    value: list[dict[_State, list[float]]] = [{} for _ in range(len(slots) + 1)]
    value[-1] = {state: terminal for state in states}
    for t in range(len(slots) - 1, -1, -1):
        slot = slots[t]
        after_slot = value[t + 1]
        # Cost of the slot plus everything after it, per (battery behaviour,
        # state afterwards). Several states share an entry.
        cache: dict[tuple[str, _State], list[float]] = {}
        now: dict[_State, list[float]] = {}
        for state in states:
            options = []
            for action in actions:
                key = _advance(state, action, release)
                if (row := cache.get(key)) is None:
                    row = cache[key] = _cost_to_go(
                        slot, key[0], after_slot[key[1]], bat, limits, levels, step_kwh
                    )
                options.append((row, 0.0 if action == state[0] else bat.switch_cost))
            now[state] = [min(row[i] + extra for row, extra in options) for i in range(levels)]
        value[t] = now

    chosen: list[str] = []
    e = e_start
    state = start
    for t, slot in enumerate(slots):
        best_action = MODE_STANDARD
        best_cost = _INF
        best_e = e
        best_state = _advance(state, MODE_STANDARD, release)[1]
        for action in actions:  # "standard" first: it wins ties
            kind, after = _advance(state, action, release)
            step = _step(slot, e, kind, bat, *limits)
            if step is None:
                continue
            total = (
                step[1]
                + step[2]
                + (0.0 if action == state[0] else bat.switch_cost)
                + _interp(value[t + 1][after], (step[0] - e_min) / step_kwh)
            )
            if total < best_cost - _EPS:
                best_action, best_cost, best_e, best_state = action, total, step[0], after
        chosen.append(best_action)
        e = best_e
        state = best_state

    planned, money, left = _run(slots, e_start, chosen, start, bat, limits)
    savings = (base_money - leftover * base_left) - (money - leftover * left)
    if savings <= 1e-6:
        return baseline  # never leave "standard" without a gain
    return Plan(planned, money, base_money, savings)

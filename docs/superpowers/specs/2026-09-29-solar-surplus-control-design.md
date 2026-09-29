# Solar surplus control

Design for following solar surplus from inside the integration, rather
than from user-written automations.

Status: approved in outline, spec pending review.
Date: 2026-09-29

---

## Purpose

Charge the car from what the house would otherwise export, adjusting
the charger's limit as production and household load change, and
stopping when there is not enough surplus to charge at all.

Today this is possible only as user-written YAML
(`docs/solar-surplus-charging.md`). That works, but requires assembling
template sensors, filters, input helpers and three automations, and
substituting entity names correctly. This moves the logic into the
integration so it works after picking two sensors.

---

## Decisions taken

| Question | Decision |
|---|---|
| Available signal | Separate grid import and export sensors, both positive |
| Control scope | Limit **and** start/stop |
| Below the charger's floor | Stop. Pure solar, never import to charge |
| Manual override | Touching the control disarms solar mode |

The floor is not a constant: the charger enforces a minimum **power**
of 1500 W, so the minimum current depends on supply voltage. The
ceiling is the installation rating. Both are already computed by
`payload.py` and exposed as entity bounds, and are read from there
rather than restated.

---

## Non-goals

- **Arbitrating with a house battery.** A battery and a car compete for
  the same surplus; deciding which wins is a policy question this does
  not answer.
- **Tariff or time-of-use scheduling.** Separate concern, and better
  served by an automation that arms and disarms solar mode.
- **Knowing the car's state of charge.** Not visible through the
  charger. A full car that stops drawing is indistinguishable from a
  cloud.
- **Replacing the YAML guide.** It stays, for setups this does not fit.
- **Three-phase surplus.** Grid meters usually report net across
  phases. A three-phase supply feeding a single-phase charger can show
  surplus that exists mostly on phases the charger cannot reach, and
  following it would overload one. Rather than be quietly wrong, solar
  control refuses to arm in that combination; see Error handling.

---

## Architecture

Four components, split so that the risky logic carries no Home
Assistant coupling. This follows `payload.py` and `optimistic.py`,
which are pure and heavily tested; the defects found in review have
clustered in the Home-Assistant-coupled code.

### `solar.py` — the decision

No Home Assistant imports. One function:

```python
def decide(state: SolarState) -> SolarDecision
```

`SolarState` carries the smoothed surplus, the reserve, the charger's
floor and ceiling, whether it is charging, the present limit, whether a
command is pending, whether the charger is reachable, whether the
vendor's own eco mode is on, whether a car is connected, and the
elapsed timers.

`SolarDecision` carries an action — `start`, `stop`, `set(watts)` or
`nothing` — and a reason string. Every branch produces a reason; it
becomes both the log line and a visible attribute.

### `solar_controller.py` — the coupling

Owns a repeating timer, reads sensors from the state machine, computes
and smooths surplus, calls `decide()`, and acts through the existing
API client. Registered in `async_setup_entry` and torn down with the
entry, alongside the coordinator's own timers.

It writes through the **API client, never through the number entity**.
That makes the manual-override rule mechanical: any call arriving at
`async_set_native_value` is by definition external, so solar mode
disarms. There is no "was that me?" flag to get wrong.

A consequence worth stating plainly: a user's **own automation**
calling `number.set_value` also disarms solar mode. This is intended —
an automation is external control — but it is surprising if
undocumented.

### Entities

| Entity | Purpose |
|---|---|
| `select.<device>_solar_control` | `off` / `simulate` / `active` |
| `number.<device>_solar_reserve` | Watts to leave for the house first |
| `sensor.<device>_solar_surplus` | Smoothed surplus, for visibility |

**Chosen while writing, flagged for review:** a three-state select
rather than two switches. `simulate` is dry-run — it decides and logs
but sends nothing. A switch pair would make the illegal combination
"dry run on, solar off" representable; a select cannot.

The select restores its state across restarts, and defaults to
`simulate` the first time it is enabled.

### Configuration

The existing options flow gains two entity pickers: the grid import
sensor and the grid export sensor. Both are required before solar
control can leave `off`.

Timings are constants rather than options. They are derived from
measured charger behaviour, not preference, and exposing them invites
misconfiguration of a feature that drives hardware. The reserve is the
one genuinely site-specific value, so it is an entity.

---

## Surplus

```
surplus = car_draw + export − import
```

The car's own draw is added back because it is not surplus that has
disappeared — it is surplus already in use. Without that term the
controller would see its own consumption as a deficit and wind itself
down to zero.

Smoothed internally over a five-minute window. Raw grid readings move
with every kettle and oven cycle; acting on them would thrash a charger
that takes seconds to apply a change.

---

## Decision logic

Evaluated in order, first match wins:

1. Charger unreachable, or a command still pending → `nothing`
2. Vendor eco mode enabled, or a charger schedule is set → `nothing`,
   entity marked unavailable
3. Not charging and no car connected → `nothing`
4. Backed off after a start the car ignored → `nothing` until the
   back-off expires
5. Charging and `surplus − reserve` below floor for the stop delay → `stop`
6. Charging and minimum run time not elapsed → `nothing`
7. Not charging and `surplus − reserve` above floor for the start delay → `start` at target
8. Charging and `|target − current| ≥ deadband` → `set(target)`
9. Otherwise → `nothing`

```
target = clamp(surplus − reserve, floor, ceiling)
```

Rules 1 to 4 are guards and come first deliberately. A charger that
cannot answer must never be read as "no surplus", which would produce a
stop; this happened in practice when the wallbox lost power, and the
resulting errors blamed the cloud service rather than the power supply.

### Asymmetric timing

A drop below the floor is evaluated **immediately** on a sensor update,
bypassing the tick. Everything else waits for the next tick.

Unused cheap power costs nothing; imported expensive power is exactly
what pure-solar mode exists to avoid. A fixed tick would import for up
to two minutes after every collapse.

### The car that will not draw

When a car finishes, it stops drawing while surplus is still high. The
charger goes idle, the controller sees "not charging, plenty of
surplus", and starts again. The car takes nothing, and the cycle
repeats until sunset.

After issuing a start, the controller watches for the car to draw more
than a nominal amount within a grace period. If it does not, solar
control backs off for a long interval rather than retrying. The rate
limit would blunt this loop but is the wrong instrument: it is a
backstop against bugs, not a substitute for handling a state the design
knows about.

### Starting from an unknown state

On a Home Assistant restart the controller's timers begin at zero. If
the car was already charging, an unelapsed minimum-run-time and an
unaccumulated surplus timer could stop a perfectly good charge moments
after boot.

Timers are therefore seeded from observed state rather than zero: a
charger already charging at startup is treated as having satisfied its
minimum run time, and surplus timers begin accumulating from the first
reading rather than assuming the threshold was only just crossed.

### Constants

| Name | Default | Why |
|---|---|---|
| Tick | 120 s | Charger takes seconds to apply a change and may need retries |
| Smoothing window | 5 min | Rides out household load steps |
| Start delay | 5 min | Confirms surplus is real before starting |
| Stop delay | 10 min | Longer than start: interrupting a car is worse than riding out a cloud |
| Minimum run time | 10 min | Prevents cycling when surplus hovers at the threshold |
| Deadband | 300 W | Avoids rewriting the limit for trivial changes |
| Rate limit | 20 commands/hour | Hard ceiling regardless of what the logic decides |
| Reserve | 0 W | Site-specific; user sets it |
| Draw grace period | 5 min | How long a started car has to begin drawing |
| Ignored-start back-off | 60 min | Before retrying a car that did not draw |

---

## Error handling

| Situation | Behaviour |
|---|---|
| Import or export sensor unknown or unavailable | Skip the cycle, log once, do **not** stop charging |
| Sensor reports a non-numeric state | Same as unavailable |
| Charger unreachable | Skip. Never infer surplus state from it |
| Command fails | Hand to the existing background retry; do not retry here |
| Command still pending | Skip the cycle entirely |
| Vendor eco mode enabled | Refuse to arm; explain why |
| Charger schedule configured | Refuse to arm; explain why |
| Three-phase supply, single-phase charger | Refuse to arm; explain why |
| Car does not draw after a start | Back off; do not retry until the interval expires |
| Rate limit reached | Skip, log at warning, resume next hour |

The recurring principle: **absence of information is never grounds for
acting.** Every unknown results in doing nothing, because the failure
mode of doing nothing is unused solar, and the failure mode of guessing
is an interrupted charge or an unwanted import.

---

## Testing

**`solar.py`** — exhaustive, no Home Assistant. Every branch of the
decision table, the ordering between branches, boundary values at the
floor and ceiling, the deadband, and each timer. This is where coverage
matters most and where it is cheapest.

**`solar_controller.py`** — against the existing stub harness in
`tests/test_entities.py`, which already fakes Home Assistant and the
API client. Covers surplus arithmetic including the car-draw term,
smoothing, the fast path on a collapse, rate limiting, dry-run sending
nothing, and teardown cancelling the timer.

**Entities** — the select's three states, restoration across restart,
and that manual writes to the number entity disarm solar mode.

No hardware is required for any of it. Hardware validation is a
separate step: run in `simulate` for a day and compare the logged
decisions against actual production.

---

## Rollout

1. Ship with solar control defaulting to `off`.
2. First enable lands in `simulate`, not `active`.
3. Document the validation day in the README and in
   `docs/solar-surplus-charging.md`, which becomes the "do it yourself"
   alternative rather than the only option.

---

## Open questions for review

1. **Three-state select, or a switch plus a dry-run option?** The select
   was chosen while writing this; it is the one structural choice not
   discussed beforehand.
2. **Should `simulate` expire?** Left permanent, so nothing starts
   driving hardware without an explicit change.
3. **Reserve as a fixed watt figure, or a percentage of surplus?** Watts
   is simpler and matches how a house battery reserve is usually
   expressed.

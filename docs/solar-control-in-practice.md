# Solar control in practice

A working installation, with the numbers it was measured on.

Everything else written about solar control in this repository describes
what it is meant to do. This describes one installation where it does
it, on 2026-10-05, including the part that is not obvious and cost the
most to work out: **what to feed it when the house has a battery.**

---

## The problem a grid meter cannot solve

The integration asks for one signed grid-power sensor — positive when
importing, negative when exporting — and computes the surplus available
to the car from it. For a house without storage, the grid meter is
exactly the right source: anything spilling to the grid is surplus.

A house with a hybrid inverter and a battery is a different system. In
self-consumption mode the battery exists precisely to keep the grid near
zero: it absorbs whatever the panels produce beyond the house load, and
discharges to cover whatever they do not. The grid meter therefore reads
approximately 0 W through the entire middle of a sunny day.

Fed that signal, solar control is not wrong — it is correctly reporting
that nothing is being exported — but it will not start the car, because
by its own measure there is no surplus. The car charges only once the
battery is full and the inverter finally spills, which on a large
battery may be late afternoon or never.

The fix is to ask a different question. Not *is anything reaching the
grid*, which the battery is designed to prevent, but **is the sun
currently producing more than the house is using**. That figure is
available directly from the inverter, and it is what the rest of this
document builds.

---

## The sensor

Measured against a single-phase hybrid inverter with battery storage,
read through its own Home Assistant integration. Any inverter
integration exposing a PV production figure and a household load figure
will do the same job; the entity names will differ.

```yaml
template:
  - sensor:
      - name: "Grid Power For EV"
        unique_id: grid_power_for_ev
        unit_of_measurement: W
        device_class: power
        state_class: measurement
        state: >
          {{ states('sensor.YOUR_INVERTER_household_load_power') | float
             - states('sensor.YOUR_INVERTER_total_pv_power') | float }}
        availability: >
          {{ has_value('sensor.YOUR_INVERTER_household_load_power')
             and has_value('sensor.YOUR_INVERTER_total_pv_power') }}
```

`household load − PV`, in watts, positive when the house is consuming
more than the panels produce. It has the sign convention the integration
expects, and it deliberately ignores the battery: it reports what the
grid *would* read if the battery were not there, which is the surplus
question rather than the export question.

The `availability` template matters and is not decoration. If either
source goes unknown the helper goes unavailable, and solar control skips
the cycle rather than acting on a figure assembled from a missing
reading. A template that defaulted the missing source to `0` would
manufacture a plausible surplus out of an inverter that had stopped
reporting. Absence is not zero — that principle is load-bearing
throughout this integration, and it has to hold in the sensor feeding it
too.

Select this helper as the grid power sensor in the integration's
options.

---

## Why the car's own draw cancels

This is the part worth checking on any installation, because getting it
wrong is not a small error — it is a feedback loop.

The controller computes:

```
surplus = car_draw − grid_power
```

The car's draw is added back because it is not surplus that has
disappeared, it is surplus already being used. Substituting the template
above, where `household_load` **includes** the charger:

```
surplus = car_draw − (other_load + car_draw − PV)
        = PV − other_load
```

The car term cancels exactly, and what is left is the real surplus.

Now the same arithmetic if `household_load` **excludes** the charger:

```
surplus = car_draw − (other_load − PV)
        = PV − other_load + car_draw
```

The car's own draw is added on top of the real surplus. The controller
raises the limit, the car draws more, the reported surplus grows again,
and the limit climbs to the installation ceiling with the battery and
then the grid covering the shortfall. On a sunny day this is difficult
to tell from the feature working well, because a limit pinned at the
ceiling looks the same either way.

### Checking which one you have

Two ways, cheapest first.

**While charging, compare household load against the charger's draw.**
If household load is lower than the car is drawing, it excludes the
charger and the template needs the charger's power adding to it.

On this installation, at 10:56:53 with the car charging:

| Reading | Value |
|---|---|
| PV production | 9,080 W |
| Household load | 8,502 W |
| Charger draw | 7,400 W |
| Grid (real meter) | −210 W, exporting |

Household load exceeds the charger's draw by about 1.1 kW, which is the
site's base load. So the charger is inside the household figure, and the
cancellation holds.

Working it through as the controller does:

```
grid_power_for_ev = 8,502 − 9,080        = −578 W
surplus           = 7,400 − (−578)       =  7,978 W
cross-check       = PV − base load
                  = 9,080 − 1,102        =  7,978 W
```

Both routes agree, which is what cancellation looks like when it is
working. The 7.4 kW the charger was drawing is legitimate against about
8 kW of surplus, and the grid was exporting 210 W rather than importing
— the installation was not pulling from the battery to feed the car.

**If that is ambiguous, stop the charge.** Note `surplus_w` on the solar
control select, stop charging from the switch, wait about two minutes
for the five-minute smoothing window to move, and read it again. It
should be roughly unchanged. If it drops by about the car's draw, the
charger is excluded from household load.

---

## The settings in use

| Setting | Value | Note |
|---|---|---|
| Grid power sensor | `sensor.grid_power_for_ev` | the template above |
| Grid supply | single-phase | declared, not detected |
| Solar reserve | 0 W | the whole surplus is offered to the car |
| Stop delay | 300 s | **changed** from the 600 s default |
| Minimum run time | 600 s | default |

A reserve of 0 W means the house keeps nothing back before the car is
offered surplus. That suits a site with battery storage, which absorbs
what the car does not take and covers the house either way. On a site
without storage, a reserve is how you stop the car taking the kettle's
share.

The shortened stop delay is a deliberate trade. It is how long the
surplus must stay below the charger's floor before charging stops, so
300 s gives up on a passing cloud sooner than the 600 s default. That
means less importing through a long cloud, at the cost of more stopping
and starting on a broken-cloud day. The minimum run time of 600 s is
what stops that becoming a cycle: once a charge starts it runs ten
minutes regardless.

---

## Reading what it is doing

`select.<device>_solar_control` carries the decision as attributes:

| Attribute | Meaning |
|---|---|
| `surplus_w` | the smoothed surplus, after the five-minute window |
| `last_action` | `start`, `stop`, `set` or `nothing` |
| `last_reason` | why, in words |

`sensor.<device>_solar_surplus` carries the same figure as its state,
plus `mode`, `last_decision`, `reason` and `target_watts`.

Those entity IDs follow your device name and are not guaranteed — check
yours under **Settings → Devices & services → [your device] →
entities**.

Two things worth knowing before you read them as faults:

- **`surplus_w` lags.** It is smoothed over five minutes, so it will not
  match an instantaneous inverter reading, and it is not supposed to.
  Raw grid readings move with every kettle and oven cycle; acting on
  them would thrash a charger that takes seconds to apply a change.
- **`last_action: nothing` is the normal state.** The controller
  evaluates every two minutes and only writes to the charger when the
  target moves more than 300 W. A long run of `nothing` with a healthy
  `surplus_w` means it is tracking, not stalled — `last_reason` will say
  which.

It never imports to charge: the charger cannot run below 1,500 W, so
when surplus falls below that it stops rather than topping up from the
grid.

---

## Still to be recorded

Honest gaps, so nobody reads this as more complete than it is:

- A full-day timeline of PV, surplus, charger limit and car draw, with
  the chart behind it.
- Behaviour across broken cloud — whether the limit hunts, how often
  commands are actually sent, and whether the shortened stop delay
  causes visible stop-start.
- Whether the 1,500 W floor cuts charging awkwardly in the first and
  last hour of useful sun.
- Whether solar control has ever refused to arm when it should not
  have, which is the one guard never confirmed in its positive
  direction (see the schedule item in the project's QA notes).

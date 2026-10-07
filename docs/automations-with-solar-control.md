# Automations alongside solar control

Worked examples from a running installation, and the one interaction
that is not obvious until it costs you a day of generation.

The entity IDs below are written as `<device>` placeholders. No entity
in this integration sets a hardcoded name, so IDs follow your device
name — check yours under **Settings → Devices & services → [your
device] → entities** rather than copying these.

---

## The interaction, first

**Starting or stopping the charge from anywhere other than solar
control turns solar control off.**

That is deliberate and mechanical. Solar control reaches the charger
only through the API client, so a command arriving at the switch, the
number entities or the `daze.*` services did not come from it and is by
definition someone else taking over. Without that rule, a user or
automation pressing stop is overruled by the next tick, which sees a
connected car and unchanged surplus and starts the charge again.

The consequence worth planning for: **nothing re-arms it.** `disarm()`
sets the mode to `off` and clears the episode's clocks. The select then
reads `off` and stays there until something sets it back to `active`.
There is no paused state and no timeout.

So any automation that commands the charger ends solar operation until
you intervene. On a schedule that runs at midnight, that is harmless in
itself — there is no sun to follow at 00:00 — but it means the *next*
day is not followed either.

---

## Pattern 1 — a cheap-rate window

Starts the charge inside a tariff window, guarded by a helper so it can
be disabled without editing the automation.

```yaml
alias: EV - start charging in the cheap window
triggers:
  - trigger: state
    entity_id: input_boolean.ev_charger_override
    to: "on"
  - trigger: time
    at: "00:00:00"
  - trigger: homeassistant
    event: start
  - trigger: time_pattern
    minutes: /15
conditions:
  - condition: state
    entity_id: input_boolean.ev_charger_override
    state: "on"
  - condition: time
    after: "00:00:00"
    before: "07:00:00"
actions:
  # Without this guard the /15 re-trigger would command the charger
  # every fifteen minutes. See "Why the already-charging guard
  # matters" below — it is not only about avoiding redundant calls.
  - if:
      - condition: state
        entity_id: sensor.<device>_evse_status
        state: charging
    then:
      - stop: Already charging

  # If your charger sits behind a switched socket, power it first and
  # wait for the charger to report a state before commanding it.
  # Installations wired directly can drop this block.
  - if:
      - condition: template
        value_template: >-
          {{ not is_state('switch.your_charger_socket', 'on') }}
    then:
      - action: switch.turn_on
        target:
          entity_id: switch.your_charger_socket
      - wait_template: >-
          {{ is_state('switch.your_charger_socket', 'on') }}
        timeout: "00:00:30"
        continue_on_timeout: false

  - wait_template: >-
      {{ states('sensor.<device>_evse_status')
         in ['paused', 'charging', 'idle'] }}
    timeout: "00:03:00"
    continue_on_timeout: false

  - if:
      - condition: template
        value_template: >-
          {{ not is_state('sensor.<device>_evse_status', 'charging') }}
    then:
      - action: switch.turn_on
        target:
          entity_id: switch.<device>_charge_control

  - wait_template: >-
      {{ is_state('sensor.<device>_evse_status', 'charging') }}
    timeout: "00:02:00"
    continue_on_timeout: true
  - if:
      - condition: template
        value_template: "{{ not wait.completed }}"
    then:
      - action: persistent_notification.create
        data:
          title: EV charger
          message: >-
            Override on, but charger is '{{
            states('sensor.<device>_evse_status') }}' instead of
            charging. Is the car plugged in?
mode: single
max_exceeded: silent
```

### Why the already-charging guard matters

`switch.turn_on` on an already-charging charger is an idempotent no-op
inside the integration — it returns before doing anything, including
before disarming solar control. So the guard is not strictly required
to avoid repeated disarms.

It is still worth having. It makes the automation's intent explicit
rather than relying on that ordering, and the ordering is an
implementation detail of `switch.py` rather than a documented promise.

### Ending the window

```yaml
alias: EV - pause charging at the end of the window
triggers:
  - trigger: time
    at: "07:00:00"
conditions:
  - condition: state
    entity_id: sensor.<device>_evse_status
    state: charging
actions:
  - action: switch.turn_off
    target:
      entity_id: switch.<device>_charge_control
  - wait_template: >-
      {{ is_state('sensor.<device>_evse_status', 'paused') }}
    timeout: "00:02:00"
    continue_on_timeout: true
  - if:
      - condition: template
        value_template: "{{ not wait.completed }}"
    then:
      - action: persistent_notification.create
        data:
          title: EV charger
          message: >-
            Tried to pause charging, but charger is '{{
            states('sensor.<device>_evse_status') }}' instead of paused.
mode: single
```

This one disarms too, but by then the midnight start already has, and
`disarm()` returns immediately when the mode is already `off`.

---

## Pattern 2 — protecting a house battery

Stops the charge when storage falls below a floor, unless the override
says the tariff window takes priority.

```yaml
alias: EV - stop charging when the house battery is low
triggers:
  - trigger: numeric_state
    entity_id: sensor.your_battery_soc
    below: 30
  - trigger: state
    entity_id: input_boolean.ev_charger_override
    to: "off"
conditions:
  - condition: state
    entity_id: input_boolean.ev_charger_override
    state: "off"
  - condition: numeric_state
    entity_id: sensor.your_battery_soc
    below: 30
actions:
  - action: switch.turn_off
    target:
      entity_id: switch.<device>_charge_control
mode: single
```

**This is the one that can cost you generation**, and it is worth
understanding before you copy it.

The midnight automation disarms solar control at a time when there is
nothing to follow. This one can fire at any hour. A cloudy afternoon
where the house draws on storage for a few hours is enough:

```text
13:00  solar control active, car following the surplus
14:00  production drops, house load holds, storage covers the gap
15:00  storage crosses the floor, override is off -> automation fires
       switch.turn_off -> solar control disarms -> mode off
16:00  sun returns, storage recovers -> nothing re-arms it
```

The automation did exactly its job. It also ended solar operation for
the rest of that day and every day after, because stopping the charge
and ending solar control are the same action as far as the integration
can tell. It cannot distinguish a protective automation from a person
pressing the button, and it should not try to — the alternative is a
controller that overrules you.

---

## Pattern 3 — the missing one

Neither of the patterns above is complete without something that puts
solar control back. There is no built-in re-arm.

A time-based re-arm alone handles the tariff window but not the battery
case, which is the one that actually costs generation. Re-arm on both:
when the window ends, **and** when the condition that triggered a
protective stop has cleared.

```yaml
alias: EV - re-arm solar control
triggers:
  - trigger: time
    at: "07:05:00"
  - trigger: numeric_state
    entity_id: sensor.your_battery_soc
    above: 50
conditions:
  - condition: state
    entity_id: input_boolean.ev_charger_override
    state: "off"
  - condition: sun
    after: sunrise
    before: sunset
  - condition: not
    conditions:
      - condition: state
        entity_id: select.<device>_solar_control
        state: active
actions:
  - action: select.select_option
    target:
      entity_id: select.<device>_solar_control
    data:
      option: active
mode: single
```

Note the hysteresis: stop below 30, re-arm above 50. Re-arming at the
same threshold that stopped it produces a loop on a day that hovers
there.

`select.select_option` does **not** disarm — only commands that reach
the charger do. Setting the select is how solar control is meant to be
controlled.

---

## A trap worth knowing: entity IDs and the naming fix

Until version 0.2.3 no entity in this integration set a translation
key, so Home Assistant fell back to the device name and generated IDs
from it. Installations from that era have IDs that look nothing like
the ones above — a charge switch and a status sensor can both end up
named after the device alone.

Those IDs are kept in the entity registry and keep working. **But
removing and re-adding the integration regenerates them from the names
it now has**, and every automation referencing the old IDs breaks
silently, pointing at entities that no longer exist.

If you need to redeploy to pick up a fix, replace the files and restart
Home Assistant. Do not delete the config entry unless you are prepared
to update your automations with it.

# Charging from solar surplus

Match the charging rate to whatever your panels are producing beyond
what the house is using, so the car soaks up surplus instead of
exporting it.

This is a worked example, not part of the integration. Everything here
goes in your Home Assistant configuration.

> The integration can now do this itself — see **Solar control** in the
> README. This guide remains for setups the built-in version does not
> fit: a house battery to arbitrate with, tariff windows, or anything
> needing logic of your own.

---

## What you need

Three entities from this integration, named for your charger. Replace
`daze_homett` with whatever yours is called:

| Purpose | Entity |
|---|---|
| Charging limit in watts | `number.daze_homett_power` |
| Start and stop | `switch.daze_homett_charge_control` |
| What the charger is doing | `sensor.daze_homett_evse_status` |
| What the car is drawing now | `sensor.daze_homett_instant_power` |

And one from your own setup, which this example calls
`sensor.grid_power`: **instantaneous grid power in watts, positive when
importing and negative when exporting**. Most energy meters expose this. If yours reports import and
export as two separate positive sensors, combine them first:

```yaml
template:
  - sensor:
      - name: "Grid power"
        unique_id: grid_power_combined
        unit_of_measurement: W
        device_class: power
        state: >
          {{ states('sensor.grid_import') | float(0)
             - states('sensor.grid_export') | float(0) }}
```

---

## Why the numbers below are what they are

Three constraints come from the charger itself, measured rather than
assumed:

- **It will not charge below 1500 W.** Asking for less is rejected
  outright. So there is no point starting until the surplus can sustain
  roughly 1.6 kW, and the car must be stopped rather than turned down
  when surplus falls below that.
- **A change takes several seconds to take effect**, and the charger
  reports its own state on a delay. Adjusting every few seconds fights
  itself; every two minutes is plenty.
- **The car decides what it actually draws.** The limit is a ceiling.
  A car that wants less will take less, and raising the limit does not
  make it take more.

---

## Available surplus

Surplus is what you are exporting *plus* what the car is already
taking, because the car's own draw is not surplus that has gone away —
it is surplus you are already using.

```yaml
template:
  - sensor:
      - name: "Solar surplus for car"
        unique_id: solar_surplus_for_car
        unit_of_measurement: W
        device_class: power
        state: >
          {% set grid = states('sensor.grid_power') | float(0) %}
          {% set car = states('sensor.daze_homett_instant_power') | float(0) %}
          {# grid is negative while exporting, so subtracting adds it #}
          {{ [ (car - grid) | round(0), 0 ] | max }}
        availability: >
          {{ has_value('sensor.grid_power')
             and has_value('sensor.daze_homett_instant_power') }}
```

Smooth it, or passing clouds will have you starting and stopping all
afternoon:

```yaml
sensor:
  - platform: filter
    name: "Solar surplus smoothed"
    entity_id: sensor.solar_surplus_for_car
    filters:
      - filter: time_simple_moving_average
        window_size: "00:05"
        precision: 0
```

---

## Settings you can tune from the dashboard

```yaml
input_number:
  solar_charge_minimum:
    name: Minimum surplus to charge
    min: 1500
    max: 5000
    step: 100
    unit_of_measurement: W
    initial: 1700

  solar_charge_deadband:
    name: Ignore changes smaller than
    min: 100
    max: 1000
    step: 50
    unit_of_measurement: W
    initial: 300

input_boolean:
  solar_charging_enabled:
    name: Solar charging
    icon: mdi:solar-power
```

The minimum sits above 1500 W deliberately. Starting exactly at the
floor means the first cloud drops you below it.

---

## Follow the surplus

```yaml
automation:
  - alias: "Solar: follow surplus"
    id: solar_follow_surplus
    mode: single
    trigger:
      - platform: time_pattern
        minutes: "/2"
    condition:
      - condition: state
        entity_id: input_boolean.solar_charging_enabled
        state: "on"
      - condition: state
        entity_id: sensor.daze_homett_evse_status
        state: "charging"
    action:
      - variables:
          surplus: "{{ states('sensor.solar_surplus_smoothed') | float(0) }}"
          deadband: "{{ states('input_number.solar_charge_deadband') | float(300) }}"
          now_set: "{{ states('number.daze_homett_power') | float(0) }}"
          floor: "{{ state_attr('number.daze_homett_power', 'min') | float(1600) }}"
          ceiling: "{{ state_attr('number.daze_homett_power', 'max') | float(7400) }}"
          target: >
            {{ [ [ surplus, floor ] | max, ceiling ] | min | round(0) }}
      - condition: template
        # Only act on a change worth making. Without this the limit is
        # rewritten every two minutes for no benefit.
        value_template: "{{ (target - now_set) | abs >= deadband }}"
      - service: number.set_value
        target:
          entity_id: number.daze_homett_power
        data:
          value: "{{ target }}"
```

`min` and `max` are read from the entity rather than hardcoded. The
integration derives them from the charger's power floor at the measured
voltage and from the installation rating, so they move with conditions
and differ between installations.

---

## Start when there is enough, stop when there is not

```yaml
automation:
  - alias: "Solar: start charging"
    id: solar_start_charging
    mode: single
    trigger:
      - platform: numeric_state
        entity_id: sensor.solar_surplus_smoothed
        above: input_number.solar_charge_minimum
        for: "00:05:00"
    condition:
      - condition: state
        entity_id: input_boolean.solar_charging_enabled
        state: "on"
      - condition: state
        entity_id: sensor.daze_homett_evse_status
        state:
          - idle
          - paused
    action:
      # Set the rate before starting, so the first minutes are not
      # spent pulling from the grid at whatever the limit happened to be.
      - service: number.set_value
        target:
          entity_id: number.daze_homett_power
        data:
          value: >
            {% set surplus = states('sensor.solar_surplus_smoothed') | float(0) %}
            {% set floor = state_attr('number.daze_homett_power', 'min') | float(1600) %}
            {% set ceiling = state_attr('number.daze_homett_power', 'max') | float(7400) %}
            {{ [ [ surplus, floor ] | max, ceiling ] | min | round(0) }}
      - delay: "00:00:15"
      - service: switch.turn_on
        target:
          entity_id: switch.daze_homett_charge_control

  - alias: "Solar: stop charging"
    id: solar_stop_charging
    mode: single
    trigger:
      - platform: numeric_state
        entity_id: sensor.solar_surplus_smoothed
        below: input_number.solar_charge_minimum
        # Longer than the start delay: stopping and restarting is
        # harder on the car than riding out a cloud.
        for: "00:10:00"
    condition:
      - condition: state
        entity_id: input_boolean.solar_charging_enabled
        state: "on"
      - condition: state
        entity_id: sensor.daze_homett_evse_status
        state: "charging"
    action:
      - service: switch.turn_off
        target:
          entity_id: switch.daze_homett_charge_control
```

---

## Before you trust it

Run it with `input_boolean.solar_charging_enabled` **off** for a day and
watch `sensor.solar_surplus_smoothed` against your actual export. If the
surplus figure is wrong, everything built on it is wrong, and that is
much easier to see before the car is involved.

Then check these, in order:

1. **Does the surplus sensor go to zero at night?** If not, the sign
   convention on your grid sensor is inverted.
2. **Does it rise when the car stops charging?** It should not. If it
   does, the car's own draw is being double counted.
3. **With charging enabled, does the limit track the surplus** without
   changing more than a few times an hour? If it flaps, raise the
   deadband or lengthen the smoothing window.

---

## Known rough edges

- **A cloudy day will stop and start the car.** The ten minute delay
  helps, but nothing here can make a variable supply steady. If your car
  dislikes being interrupted, raise `solar_charge_minimum` so it only
  runs on genuinely sunny periods.
- **This ignores the battery, if you have one.** A house battery and a
  car compete for the same surplus, and deciding which wins is a policy
  question this example does not answer.
- **Nothing here reads the car's state of charge.** Home Assistant
  cannot see it through the charger, so a nearly full car that stops
  drawing looks the same as a cloud.

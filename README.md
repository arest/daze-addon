# Daze Wallbox

[![Version](https://img.shields.io/github/v/tag/tarrinho/daze-addon?label=version&sort=semver&color=blue)](https://github.com/tarrinho/daze-addon/releases)
[![HA Community](https://img.shields.io/badge/Home%20Assistant-2025.x-41BDF5?logo=homeassistant)](https://www.home-assistant.io/)
[![HACS Validation](https://github.com/tarrinho/daze-addon/actions/workflows/validate.yaml/badge.svg)](https://github.com/tarrinho/daze-addon/actions/workflows/validate.yaml)
[![GitHub](https://img.shields.io/github/license/tarrinho/daze-addon)](LICENSE)

Home Assistant integration for **Daze WallBox EV chargers**. Monitor charging metrics in real time and control your wallbox directly from your HA dashboard — no separate app required.

Daze wallboxes are managed through the [Daze web portal](https://webportal.dazeservice.com). This integration bridges the gap, bringing your wallbox into Home Assistant alongside all your other smart home devices.

> **This is a fork.** The original integration was created by **Andrea Restello** ([@arest](https://github.com/arest)) at [arest/daze-addon](https://github.com/arest/daze-addon), and all of the original design and implementation is his work. This fork, maintained by **Pedro Tarrinho** ([@tarrinho](https://github.com/tarrinho)), adds fixes found while running it against a DT01 charger — see [Changes in this fork](#changes-in-this-fork).

---

## Features

- **Real-time monitoring** — Power (W), delivered energy (Wh), charging current per phase (mA), AC voltage per phase (V), board and case temperatures (°C)
- **EVSE status** — See whether the wallbox is charging, idle, paused, or in error
- **Charge control** — Start and stop charging from HA switches, automations, or dashboards
- **Current limit** — Set the maximum charging current as a number entity (6–32 A, 0.1 A steps)
- **Operation mode** — Switch between eco, fast, scheduled, and other modes
- **Session history** — Track energy, duration, and cost per recharge session
- **Lifetime totals** — Total energy delivered and session count
- **Diagnostics** — Grid max power, photovoltaic presence, three-phase supply info
- **Fully UI-driven** — Set up entirely through the Home Assistant UI, no YAML editing required

---

## Installation

### Via HACS (recommended)

1. Make sure [HACS](https://hacs.xyz/) is installed in your Home Assistant instance
2. Go to **HACS → Integrations**
3. Click the three dots in the top-right corner and select **Custom repositories**
4. Add this repository URL:
   ```
   https://github.com/tarrinho/daze-addon
   ```
5. Select **Integration** as the category and click **Add**
6. Close the dialog — the Daze Wallbox integration should now appear in HACS
7. Click **Install** on the Daze Wallbox card
8. Restart Home Assistant

### Manual installation

1. Copy the `custom_components/daze/` directory from this repository into your Home Assistant `custom_components/` directory
2. Restart Home Assistant

---

## Configuration

1. Go to **Settings → Devices & services**
2. Click **Add integration** and search for **Daze Wallbox**
3. Enter your Daze **Access Token** and **Refresh Token**

   > **Where to find your tokens:** These are obtained from the Daze web portal ([webportal.dazeservice.com](https://webportal.dazeservice.com)) or the developer console. The integration uses a personal access token model — not email/password.

4. Click **Submit** — the integration validates your tokens
5. Select your **network** (installation location) from the list
6. Review the confirmation screen with your wallbox details
7. Click **Submit** to complete setup

The wallbox should now appear as a single device with all sensors and controls grouped under it.

### Re-authentication

If your tokens expire, the integration will automatically prompt you to re-enter them through the HA UI. You'll see a notification and a re-authentication flow.

---

## Entities

### Sensors

| Entity ID | Name | Device Class | State Class | Unit |
|-----------|------|-------------|-------------|------|
| `sensor.daze_instant_power` | Instant Power | `power` | `measurement` | W |
| `sensor.daze_delivered_energy` | Delivered Energy | `energy` | `total_increasing` | Wh |
| `sensor.daze_charging_current_l1` | Charging Current L1 | `current` | `measurement` | mA |
| `sensor.daze_charging_current_l2` | Charging Current L2 | `current` | `measurement` | mA |
| `sensor.daze_charging_current_l3` | Charging Current L3 | `current` | `measurement` | mA |
| `sensor.daze_ac_voltage_l1` | AC Voltage L1 | `voltage` | `measurement` | V |
| `sensor.daze_ac_voltage_l2` | AC Voltage L2 | `voltage` | `measurement` | V |
| `sensor.daze_ac_voltage_l3` | AC Voltage L3 | `voltage` | `measurement` | V |
| `sensor.daze_board_temperature` | Board Temperature | `temperature` | `measurement` | °C |
| `sensor.daze_case_temperature` | Case Temperature | `temperature` | `measurement` | °C |
| `sensor.daze_evse_status` | EVSE Status | `enum` | — | idle / waiting_for_ev / charging / paused / error / offline |
| `sensor.daze_last_session_energy` | Last Session Energy | `energy` | `total_increasing` | Wh |
| `sensor.daze_last_session_duration` | Last Session Duration | — | — | min |
| `sensor.daze_last_session_cost` | Last Session Cost | `monetary` | — | EUR |
| `sensor.daze_last_session_start` | Last Session Start | `timestamp` | — | |
| `sensor.daze_last_session_end` | Last Session End | `timestamp` | — | |
| `sensor.daze_lifetime_energy` | Lifetime Energy | `energy` | `total_increasing` | Wh |
| `sensor.daze_total_sessions` | Total Sessions | — | `total_increasing` | sessions |

#### Diagnostic sensors

| Entity ID | Name | Device Class | Category |
|-----------|------|-------------|----------|
| `sensor.daze_grid_max_power` | Grid Max Power | `power` | diagnostic |
| `sensor.daze_is_photovoltaic` | Photovoltaic Present | `enum` | diagnostic |
| `sensor.daze_is_three_phase` | Three-Phase Supply | `enum` | diagnostic |
| `sensor.daze_next_scheduled_charge` | Next Scheduled Charge | `timestamp` | diagnostic |

### Controls

| Platform | Entity ID | Name | Purpose |
|----------|-----------|------|---------|
| Switch | `switch.daze_charge_control` | Charge Control | Start / stop charging |
| Number | `number.daze_max_charging_current` | Max Charging Current | Set charging current limit (6–32 A) |
| Select | `select.daze_operation_mode` | Operation Mode | Switch between eco, fast, scheduled |

---

## Services

These services are available for automations and scripts:

### `daze.start_charge`

Start charging on a Daze wallbox.

```yaml
service: daze.start_charge
```

### `daze.stop_charge`

Stop charging on a Daze wallbox.

```yaml
service: daze.stop_charge
```

### `daze.set_charging_current`

Set the maximum charging current.

| Field | Required | Description |
|-------|----------|-------------|
| `current` | Yes | Maximum charging current in milliamps (mA). Range: 6000–32000, step 100. |

```yaml
service: daze.set_charging_current
data:
  current: 16000
```

---

## Automation Examples

### Stop charging when energy price is high

```yaml
automation:
  - alias: "Stop Daze charging during peak hours"
    trigger:
      - platform: time
        at: "17:00:00"
    condition:
      - condition: state
        entity_id: switch.daze_charge_control
        state: "on"
    action:
      - service: daze.stop_charge
```

### Set charging current based on solar production

```yaml
automation:
  - alias: "Adjust Daze charging to solar surplus"
    trigger:
      - platform: numeric_state
        entity_id: sensor.solar_production
        above: 3000
    action:
      - service: daze.set_charging_current
        data:
          current: 16000
```

---

## Troubleshooting

### "Invalid tokens" during setup
Make sure you've copied the full access token and refresh token — they are long strings. Tokens must be active (not expired). Obtain fresh tokens from the Daze web portal.

### Integration shows "unavailable"
- Check your internet connection — the Daze API is cloud-based
- Verify your wallbox is online (check the Daze mobile app)
- The integration automatically retries; entities become available again once the API responds

### Re-authentication required
If your refresh token has expired, the integration will trigger a re-authentication flow. Follow the prompts in **Settings → Devices & services** to enter new tokens.

### No data or stale data
- The integration polls every 30 seconds by default
- If the Daze API returns errors, the coordinator retries automatically
- Check the Home Assistant logs for Daze-related error messages

### Sensors not updating after a control command
The integration automatically refreshes data after sending a start/stop/current command. If values don't update, wait for the next scheduled poll cycle.

---

## Supported hardware

- Daze WallBox EV chargers accessible via the Daze REST API
- Tested with DT01 device profile

---

## Data & privacy

- All data flows through the Daze cloud API — no local/offline control
- The integration stores only your access token and refresh token (encrypted in HA config entry storage)
- No data is sent to third parties beyond the Daze API

---

## Development

### CI/CD

The integration is validated with:
- [ruff](https://github.com/astral-sh/ruff) for linting
- [pyright](https://github.com/microsoft/pyright) for type checking
- `hassfest` for Home Assistant integration validation
- HACS validation

---

## Credits

This integration was created by **Andrea Restello** ([@arest](https://github.com/arest)).
The upstream project is [arest/daze-addon](https://github.com/arest/daze-addon).

Everything this fork does rests on his work: the integration architecture, the
config flow, the entity model, the sensor catalog and the API client were all
written upstream. He also reverse-engineered the Daze web API, which is not
publicly documented — that is the hard part, and none of what follows would
exist without it.

### Changes in this fork

Maintained by **Pedro Tarrinho** ([@tarrinho](https://github.com/tarrinho)).

Every change below was found by running the integration against a real DT01
wallbox and measuring the API's actual responses, rather than by reading the
code alone.

**Setup**

- Authenticate through the Cognito `GetUser` operation instead of
  `/oauth2/userInfo`. The Daze portal issues access tokens scoped
  `aws.cognito.signin.user.admin` without `openid`, which `userInfo` rejects,
  so setup previously failed for every user with `invalid_token`.

**Reading data**

- Read the live metrics from where the API actually returns them. Power,
  energy, currents and voltages arrive nested under `chargeSession`, not at the
  top level, so every sensor read `Unknown` with no error logged.
- Fetch the EVSE record as well as the socket state. Temperatures, the grid
  limit, eco mode and the configured current appear only there.
- Derive the charger status from the integer `evseState` plus the pause and
  error flags. The API never returns the status string the code expected.
- Report `waiting_for_ev`, the state the charger passes through after a start
  before the car begins drawing, and hold the charge switch on through it so it
  does not appear to snap back.

**Charge control**

- Send the serial number and the session ID with `playcharge` and `stopcharge`.
  An empty body is rejected with `ErrorWrongSessionID`, and the session ID alone
  is accepted but does nothing.
- Read the session ID from the charger at command time. It changes whenever a
  session ends, so a cached copy can name one that has already closed.
- Retry commands through the Daze RPC link, which fails intermittently with
  HTTP 500 code 101. Delays grow from 1.5s to 6s across eight attempts, roughly
  33 seconds in total, because a tight burst of retries does not outlast the
  outage.
- Re-read the charger at 3, 8, 15 and 30 seconds after a command, so a start or
  pause shows up promptly instead of waiting for the next poll.

**Robustness and diagnostics**

- Treat HTTP 404 from the recharge-session endpoint as a durable condition.
  It was retried every 30 seconds and logged a warning each time.
- Throttle the session history fetch to once every five minutes instead of
  requesting up to 1000 records twice a minute.
- Log retried failures at debug and report a single warning only when a command
  genuinely gives up, instead of one warning per attempt.
- Add diagnostic tools under `tools/` for reproducing each API call outside
  Home Assistant, and tests that use captured API responses as fixtures.

These are bug fixes to someone else's design, not a redesign. If the upstream
project adopts them, this fork becomes unnecessary.

### License

This project is licensed under the [MIT License](LICENSE), Copyright (c) 2025
Andrea Restello, carried over unchanged from the upstream project.

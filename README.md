# Daze Wallbox

[![HA Community](https://img.shields.io/badge/Home%20Assistant-2025.x-41BDF5?logo=homeassistant)](https://www.home-assistant.io/)
[![HACS Validation](https://github.com/tarrinho/daze-addon/actions/workflows/validate.yaml/badge.svg)](https://github.com/tarrinho/daze-addon/actions/workflows/validate.yaml)
[![GitHub](https://img.shields.io/github/license/tarrinho/daze-addon)](LICENSE)

Home Assistant integration for **Daze WallBox EV chargers**. Monitor charging metrics in real time and control your wallbox directly from your HA dashboard — no separate app required.

Daze wallboxes are managed through the [Daze web portal](https://webportal.dazeservice.com). This integration bridges the gap, bringing your wallbox into Home Assistant alongside all your other smart home devices.

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
| `sensor.daze_evse_status` | EVSE Status | `enum` | — | idle / charging / paused / error |
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

### License

This project is licensed under the [MIT License](LICENSE).

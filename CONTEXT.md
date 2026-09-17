# Daze Wallbox Integration Context

This context defines the domain language for exposing one Daze EVSE inside Home Assistant. It keeps naming stable across onboarding, telemetry, and control.

## Language

**EVSE**:
The physical Daze charging station represented as one Home Assistant device.
_Avoid_: charger, wallbox unit

**EVSE Sensor Catalog**:
The canonical list of EVSE telemetry and diagnostic measurements exposed to Home Assistant with stable keys and value semantics.
_Avoid_: sensor list copy, runtime-only sensor map

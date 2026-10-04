# Changelog

All notable changes to this fork are documented here.

This project is based on
[arest/daze-addon](https://github.com/arest/daze-addon), created by Andrea
Restello. Fork maintenance and the changes listed below are by Pedro Tarrinho
unless otherwise noted.

## [0.2.2] - 2026-10-04

### Fixed

- Kept the recharge session history when the API returns 404 for the endpoint, instead of replacing it with an empty list and resetting lifetime energy to zero.
- Withheld the second and third phase current and voltage readings unless the charger reports a three-phase supply, so an unconnected input is not published as a measurement.

### Changed

- Removed the per-session and next-scheduled-charge sensors, which reported no value until a charge had completed.

### Added

- Added tests for the coordinator session cache and background command retries, for the solar surplus sensor attributes, for the phase filter, and for the recharge session model.

## [0.2.1] - 2026-10-03

### Changed

- Replaced the separate grid import and grid export entities with one signed grid power sensor for solar surplus control.
- Defined positive grid power as import and negative grid power as export.
- Simplified solar surplus calculation and controller configuration around the signed sensor.

## [0.2.0] - 2026-09-30

### Added

- Added solar surplus charging with off, simulate, and active modes.
- Added reserve-power and surplus entities for configuring and observing solar control.
- Added smoothing, start and stop delays, minimum-run protection, command spacing, retry backoff, and hourly command limits.
- Added safety checks for unavailable sensors, unknown charger draw, charger reachability, schedules, phase configuration, and Home Assistant restarts.
- Added automatic disarming when a manual control would conflict with solar control.
- Added solar-control design, implementation, setup, and operating documentation.
- Added extensive controller, entry-lifecycle, entity, and QA invariant tests.

### Changed

- Allowed grid power sensors to be selected through integration options.
- Made cleared sensor options remain cleared instead of being restored by option merging.

### Fixed

- Fixed controller races, stranded clocks and latches, retry-state leaks, repeated commands, and failed-setup unload handling found during review.
- Fixed a missing charging-limit report being interpreted as a zero-watt limit.

## [0.1.6] - 2026-09-29

### Added

- Added a power-based charging limit alongside the current-based control.
- Added optimistic state updates for charge, current, and operating-mode controls.
- Added configurable coordinator polling.
- Added background command retries and prompt refreshes after successful control changes.
- Added diagnostic and control tools under `tools/` for reproducing API calls and measured charger behavior.
- Added stubbed Home Assistant entity tests and QA checks for charging-current bounds.

### Changed

- Derived charging-limit bounds from the charger-reported installation rating, configured setting, minimum power, and measured voltage.
- Renamed charging limit entities to Power and Current without duplicating the device name.
- Read the current session ID when sending a command instead of relying on a cached value.
- Increased retry spacing and reduced repeated warning logs for transient Daze RPC failures.

### Fixed

- Fixed charge commands by sending the required serial number and session ID.
- Added meaningful errors for charger command refusals.
- Added handling for the waiting-for-EV state and kept controls aligned with charger transitions.
- Fixed out-of-range current requests, stale optimistic state, unload cleanup, and conflicting power/current values.
- Prevented commands while the charger is not reporting.
- Fixed current limits that excluded the charger's own configured setting or were incorrectly capped by `sccLimit`.

## [0.1.3] - 2026-09-27

### Fixed

- Read live measurements from the nested API objects that actually contain them.
- Read EVSE diagnostics and status fields from the API's real response structure.
- Derived charger status from EVSE state and pause/error flags instead of an absent status string.

## [0.1.2] - 2026-09-27

### Fixed

- Stopped repeatedly polling and warning about an unavailable recharge-session endpoint.

## [0.1.1]

### Changed

- Pointed project metadata to this fork.

### Fixed

- Switched authentication to Cognito `GetUser` for access tokens that do not carry the `openid` scope required by `/oauth2/userInfo`.

## [0.1.0] - 2026-07-29

- Initial upstream release by Andrea Restello.

[0.2.2]: https://github.com/tarrinho/daze-addon/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/tarrinho/daze-addon/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/tarrinho/daze-addon/compare/v0.1.6...v0.2.0
[0.1.6]: https://github.com/tarrinho/daze-addon/compare/v0.1.3...v0.1.6
[0.1.3]: https://github.com/tarrinho/daze-addon/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/tarrinho/daze-addon/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/tarrinho/daze-addon/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/arest/daze-addon/releases/tag/v0.1.0

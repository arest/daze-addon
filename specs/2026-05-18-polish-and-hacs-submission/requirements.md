# Requirements: Polish & HACS Submission (Pi Test First)

## Scope

Prepare the Daze Wallbox custom component for HACS default repository submission with full polish — translations, services, state restoration, documentation, and CI — **without** actually submitting to HACS yet.

**In scope:**
- Italian translation file (`translations/it.json`) covering all config flow strings and entity names
- HA service definitions for the three main controls (`set_charging_current`, `start_charge`, `stop_charge`) with proper service schema and translation support
- State restoration via `RestoreEntity` mixin for key cumulative sensors (energy, session counters)
- README rewrite as a proper HACS-friendly installation and usage guide
- GitHub Actions CI pipeline (ruff, pyright, hassfest, HACS validation)
- End-to-end testing on a Raspberry Pi running Home Assistant OS

**Out of scope:**
- Submission to HACS default repository (postponed until Pi testing passes)
- Additional sensor or entity types beyond what Phases 1–4 deliver
- Non-Italian translations (only Italian for now)
- Performance optimisation or architectural refactoring
- Support for multiple wallboxes or multiple networks

## Context

The integration has completed four phases of development:
1. **Scaffold, Auth & Config Flow** — Core package structure, Cognito OAuth, config flow with re-auth
2. **Sensor Platform** — All charging metrics as HA sensors with correct device classes
3. **Control Entities** — Switch (start/stop), Number (max current), Select (operation mode)
4. **Recharge Sessions & Diagnostics** — Session history, lifetime counters, diagnostics endpoint

Phase 5 wraps up all non-functional requirements needed for a production-quality HACS component. The existing README is raw API documentation rather than a user-facing guide. There are no HA services beyond the built-in entity controls, and Italian users would benefit from native-language UI strings. State restoration ensures sensor history survives HA restarts. CI validation catches regressions before they reach users.

The user wants to test on their Raspberry Pi before submitting to HACS, so the plan includes a dedicated testing group.

## Decisions

1. **RestoreEntity mixin over manual restore** — HA provides `RestoreEntity` (or `RestoreSensor`) as the standard way to preserve sensor values across restarts. We'll use this for cumulative sensors (`delivered_energy`, `lifetime_energy`, `total_sessions`) and best-effort for last-session sensors. Instantaneous sensors (power, current, voltage, temperature) will not restore — they'll refresh on first poll.

2. **`async_register_entity_service` over `async_register_admin_service`** — Entity-targeted services (`set_charging_current`, `start_charge`, `stop_charge`) should target specific Daze entities. Using `async_register_entity_service` keeps the service schema tied to the coordinator entity, matching HA conventions for device-bound services.

3. **services.yaml with JSON schema** — Following HA best practices, services are defined in `services.yaml` with proper field schemas (selector types, required/optional). Translations live in `strings.json` under a `services:` key.

4. **CI on push/PR to master and feature branches** — Validates code quality before merging. Separate workflow for `validate.yaml` to keep concerns isolated from any future release workflow.

5. **README as single authoritative doc** — Rather than splitting into separate files, the README serves as both the HACS description and the user-facing documentation. It replaces the current raw API dumps with structured, user-friendly content.

6. **Italian first, additional languages later** — Italian is the only non-English translation added in this phase. The translation structure supports additional languages when needed.

# Validation: Recharge Sessions & Diagnostics

## Acceptance Criteria

- [ ] Session sensors (last session energy, duration, cost, start/end time) appear in HA after setup and reflect the last completed charging session
- [ ] Lifetime energy sensor shows cumulative Wh across all sessions and increases when a new session completes
- [ ] Total session count sensor increments with each completed session
- [ ] After clearing session data or with a never-used charger, all session sensors show `None`/`0` without errors or warnings
- [ ] During an active charging session, the "last session" sensors correctly show the in-progress session data (partial data, no end time/cost)
- [ ] When the Daze API session endpoint returns an error, session sensors enter `unavailable` state (while live EVSE sensors remain operational if their endpoint is healthy)
- [ ] HA diagnostics page (`/api/diagnostics`) returns valid JSON with sections for auth metadata, API connectivity, coordinator timing, and entity counts — no errors, no token secrets exposed
- [ ] Scheduled charge diagnostic sensor appears when scheduling is active; stays `unavailable` or hidden when not supported

## Testing

### Unit tests
- Test session data parsing from mock API responses (valid data, empty list, partial/in-progress session)
- Test aggregate computation (lifetime energy sums correctly, session count increments)
- Test edge cases: never-used charger returns zeros, API error propagation, missing optional fields
- Run with: `python -m pytest tests/ -v`

### Manual testing
- Set up integration with a real Daze account that has at least one completed charging session
- Verify all session sensors show correct values matching the Daze mobile app
- Start a new charge and verify in-progress session handling
- Verify diagnostics endpoint via HA Developer Tools → Diagnostics
- If applicable, enable scheduled charging in the Daze app and verify the diagnostic sensor

## Merge Conditions

- [ ] All acceptance criteria met as verified by manual testing with a real Daze account
- [ ] Unit tests cover session parsing, aggregation, and all edge cases
- [ ] All existing tests still pass (`pytest tests/`)
- [ ] No regressions in existing sensor, switch, number, or select entities
- [ ] Code reviewed
- [ ] No `.storage/` files, no `configuration.yaml` edits, all HA async patterns followed

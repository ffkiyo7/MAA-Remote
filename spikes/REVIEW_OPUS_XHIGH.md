# Opus xhigh Review Summary

Date: 2026-07-07
Scope: `build_stage_catalog.py`, `copilot_catalog.py`, and spike fixtures.

The review found two blocking hard-filter bugs in the first spike revision:

1. `groups` were modeled as replacements for `opers`, but the real schema treats each group as an independent formation slot. The fix now checks every group slot and requires at least one owned, qualified operator.
2. `requirements.skill_level` was treated as a hard failure even though OperBox does not report skill levels. The fix now reports skill-level gaps as risk annotations instead of rejecting the candidate.

The follow-up also tightened several medium-risk areas:

- Added a passed-with-risk candidate tier.
- Fed doc signals and requirement margin into the soft score.
- Added operator skin-name normalization for common prefixed names.
- Recorded real display-id collisions in `stage_catalog.json` and let callers override with `--level-id`.
- Made explicit `requirements.elite` take priority over skill-index inference.
- Added basic network/JSON error handling and numeric defaults for nullable API fields.
- Synced the spike report with the code's scoring weights and filtering behavior.

Current status: the blocking issues reported by this review were fixed in `spikes/copilot_catalog.py` and `spikes/build_stage_catalog.py`.

# Threshold calibration

All thresholds are centralised in `config.py` (`Settings`) and overridable in `.env` (see `.env.example`):

| Setting | Variable | Default | Used by |
|---|---|---|---|
| `review_threshold` | `REVIEW_THRESHOLD` | 6.5 | review panel approval score (a profile may override via `revue.seuil`) |
| `review_votes_needed` | `REVIEW_VOTES_NEEDED` | 2 | vote 2/3 |
| `shortlist_min` / `shortlist_max` | `SHORTLIST_MIN` / `SHORTLIST_MAX` | 5 / 8 | shortlist size |
| `contrarian_quota` / `contrarian_floor` | `CONTRARIAN_QUOTA` / `CONTRARIAN_FLOOR` | 2 / 4.0 | contrarian ideas kept |
| `complexity_threshold` | `COMPLEXITY_THRESHOLD` | 5 | candidate for the **workflow escalation** (product feature) |
| `agent_seuil_caracteres` / `agent_seuil_score` | `AGENT_SEUIL_CARACTERES` / `AGENT_SEUIL_SCORE` | 6000 / 5 | simple vs hard routing of agents |

Current values are provisional: they have **not** been calibrated on real pilot runs.

## Suggested method (to run manually by the maintainer)
1. Run several pilots on fictitious data with the default values; keep the run logs (`logs/`).
2. For each run, note the review scores of ideas a human accepted vs rejected.
3. Choose `REVIEW_THRESHOLD` that best separates the two groups; adjust one variable at a time.
4. Do the same for `COMPLEXITY_THRESHOLD` with ideas that were or were not worth escalating.
5. Record the chosen values and the sample size; with few runs the result is anecdotal, not statistical.

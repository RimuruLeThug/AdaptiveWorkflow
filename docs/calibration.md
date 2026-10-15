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

## Calibration tool: `calibrate.py`

```
python calibrate.py                          # uses the configured logs/ and state/ folders
python calibrate.py --logs DIR --state DIR   # other folders (e.g. a copy of a colleague's data)
python calibrate.py --votes 2                # REVIEW_VOTES_NEEDED used for the analysis (default from config)
python calibrate.py --inclure-pilote         # also count the scripted decisions of pilot.py (state/pilot/)
```

The tool is read-only, offline (no model, no network) and **never reads or writes `.env`**: it prints a French report and the `.env` lines to copy by hand. Idea titles and reasons are never printed. Missing files and corrupted lines are skipped silently.

### What it reads
| Source (written by) | Used for |
|---|---|
| `state/**/decisions.jsonl` (`qwen_ui/app.py`, `/valider`, `/refuser` and free-language feedback) | kept vs rejected ideas, with their weighted score; the last decision per (review, idea) wins |
| `logs/review-<stamp>-<review_id>.jsonl` (`review.py`) | each reviewer's score, joined to decisions by `review_id` + `idea_id` |
| `state/**/retours.jsonl` (`qwen_ui/langage_libre.py`) | count and keep rate per producing agent (same decisions as `decisions.jsonl`, no score, not counted twice) |
| `logs/*.jsonl`, `agent_call` events (`RunLogger`, `agents.py`) | simple/hard routing: prompt length or score and the result of each first attempt |
| `logs/escalation-*.json` (`escalation.py`) | complexity, only if a `complexity_score` field is present |

Decisions under `state/pilot/` are excluded by default: the pilot answers "yes" automatically, so they are not human judgments.

### How each threshold is suggested
- **`REVIEW_THRESHOLD`**: for each decided idea the tool takes the k-th best reviewer score (k = `REVIEW_VOTES_NEEDED`), which is exactly the value compared to the threshold by the 2/3 vote. Without review logs it falls back to the weighted score from `decisions.jsonl` (approximation, stated in the report). The suggestion is the cut that best separates kept from rejected ideas (balanced accuracy, middle of the best plateau), rounded to 0.1. If kept ideas are not scored higher than rejected ones, the report says so: reviewers do not predict your choices and no threshold will fix it.
- **`AGENT_SEUIL_CARACTERES` / `AGENT_SEUIL_SCORE`**: only first attempts routed to the *simple* agent tell where it starts failing. Capability failures (`timeout`, `vide`, `hors_schema`, `erreur`) are separated from successes by length (or score); infrastructure outcomes (`indisponible`, `http_429`, quota, policy refusals) are ignored. With no failure, the tool proposes nothing: lowering is not justified and raising is not demonstrated, because tasks above the threshold were never given to the simple agent. Routing is only logged when a hard agent (`AGENT_<ROLE>_DIFFICILE`) is configured.
- **`COMPLEXITY_THRESHOLD`**: the current code does not log the complexity score (`escalation.py` only records the escalation result), so the tool reports "no suggestion". The calculation (accepted vs rejected escalations, escalation if score > threshold) is ready if a `complexity_score` field is ever recorded.

### Reading the result
- Sample size, the median of each group and an **indicative interval** (10th–90th percentile of the threshold over 300 bootstrap resamples, fixed seed) are printed for every suggestion.
- Below 20 observations the report prints a warning: **anecdotal, not statistical**; do not change a threshold on that basis alone. A group with fewer than 5 observations is flagged as fragile.
- A profile can override the review threshold (`revue.seuil` in `profil.json`); the tool pools all profiles.
- Change one variable at a time, run new sessions, then calibrate again.

## Suggested method (to run manually by the maintainer)
1. Run several real sessions on fictitious data with the default values; keep the logs (`logs/`) and `state/`.
2. After each review, decide with `/valider` / `/refuser` (or free-language feedback): these decisions are the ground truth.
3. Run `python calibrate.py` and read the sample size and warnings before the suggestions.
4. Copy one suggested line into `.env` by hand; adjust one variable at a time.
5. Record the chosen values and the sample size; with few decisions the result is anecdotal, not statistical.

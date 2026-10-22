# Configuration reference

Values come from `config.py` (`load_settings()`, defaults in `Settings`) and can be overridden in `.env`. Copy `.env.example` to `.env`; `.env` is git-ignored and must never be committed.

`.env` format: one `NAME=value` per line, `#` for comments, optional single or double quotes. Variables already set in the environment are not overridden by `.env`. `GEMINI_API_KEY` is read by `gemini_api_key()` and is never stored in the settings object, so it does not appear in logs or `repr` output.

## Gemini

| Variable | Default | Meaning |
|---|---|---|
| `GEMINI_API_KEY` | empty (optional since V3) | Gemini key. Needed only if a role is assigned to Gemini or `/organisateur gemini` is used; without it, Gemini agents are unavailable. |
| `GEMINI_MODEL` | `gemini-3.5-flash-lite` | Model of the `gemini` agent |
| `GEMINI_TIMEOUT_S` | `120` | Timeout of one request, in seconds |
| `GEMINI_MIN_INTERVAL_S` | `5` | Minimum spacing between two requests (5 s = 12 per minute) |
| `GEMINI_DAILY_ALERT` | `1200` | Daily count from which a warning is logged |
| `GEMINI_DAILY_HARD_STOP` | `1400` | Daily count at which Gemini calls stop until midnight Pacific time |
| `GEMINI_MAX_RETRIES` | `2` | Retries on HTTP 429; each retry is counted |
| `GEMINI_TEST_MODEL` | empty | Model of the `gemini_test` agent. Empty means the agent is unavailable. |

The rate limiter reads the `GEMINI_*` quota variables; the state file is `state/gemini_quota.json`.

## Orchestration and review

| Variable | Default | Meaning |
|---|---|---|
| `DELEGATION_BUDGET` | `6` | Maximum delegations per task, all attempts included (not in `.env.example`) |
| `MAX_DEPTH` | `1` | Delegation depth: the organizer delegates, workers do not (not in `.env.example`) |
| `MAX_PLAN_ATTEMPTS` | `2` | Attempts to get a valid plan from the organizer (not in `.env.example`) |
| `REVIEW_THRESHOLD` | `6.5` | Score (out of 10) a reviewer must give to approve an idea |
| `REVIEW_VOTES_NEEDED` | `2` | Approvals needed (2 of 3) |
| `SHORTLIST_MIN` / `SHORTLIST_MAX` | `5` / `8` | Size of the shortlist |
| `CONTRARIAN_QUOTA` | `2` | Contrarian ideas kept even with an average score... |
| `CONTRARIAN_FLOOR` | `4.0` | ...if their weighted score reaches this floor (not in `.env.example`) |
| `MAX_REVIEW_IDEAS` | `40` | Maximum number of ideas sent to a review (not in `.env.example`) |

A profile can override the review threshold and the shortlist size in its `revue` section (`seuil`, `shortlist_min`, `shortlist_max`). See [calibration.md](calibration.md) for how to choose values.

## Big model and escalation (product feature)

| Variable | Default | Meaning |
|---|---|---|
| `BIG_MODEL_PROVIDER` | empty | Empty: the agent of the `grand_modele` role (`claude_haiku` by default). `gemini` or `claude` forces that agent for the role (`/grand` changes it at runtime) |
| `CLAUDE_HAIKU_MODEL` | `haiku` | Claude Code alias of the `claude_haiku` agent, default agent of every role in V3 |
| `CLAUDE_MODEL` | `sonnet` | Claude Code alias of the `claude` agent: `sonnet`, `opus`, `haiku`... |
| `CLAUDE_TIMEOUT_S` | `600` | Timeout of one Claude call (not in `.env.example`) |
| `CLAUDE_ALLOW_API_BILLING` | off | `1`, `true` or `oui` accepts an API-key login, which bills the API. Leave off. |
| `COMPLEXITY_THRESHOLD` | `5` | Complexity score above which an idea is a candidate for escalation |

## Local models (Ollama)

| Variable | Default | Meaning |
|---|---|---|
| `QWEN_MODEL` | `qwen3.5:9b-q4_K_M` | The single local model (V3): fallback of every role, every role in a personal-data profile, indirect clues of the masking. Agents `qwen`, `qwen_interface`, `qwen_format` all use it. |
| `QWEN_NUM_CTX` | `16384` | Context size of the local model (one value, so Ollama does not reload the model between uses) |
| `OLLAMA_HOST_URL` | `http://localhost:11434` | Ollama address |
| `LOCAL_KEEP_ALIVE` | `2m` | How long Ollama keeps a model in VRAM after its last request. `0` unloads at once. |

Model names must match `ollama list`. If the model is missing, a startup warning says so (see [troubleshooting.md](troubleshooting.md)); the program still starts. The V2 variables `QWEN_INTERFACE_MODEL`, `QWEN_FORMAT_MODEL`, `QWEN_INTERFACE_NUM_CTX` and `QWEN_FORMAT_NUM_CTX` are ignored since V3, with a startup warning if they are still set.

## Folders and startup profile

| Variable | Default | Meaning |
|---|---|---|
| `ORCH_STATE_DIR` | `state/` at the project root | State files: quota, decisions, feedback, fiche, vault (not in `.env.example`) |
| `ORCH_LOGS_DIR` | `logs/` at the project root | Run logs (not in `.env.example`) |
| `PROFILE` | `brainstorm` | Profile loaded at startup. The profile can then change during a session. |

## Free-language mode and personal-data profile

| Variable | Default | Meaning |
|---|---|---|
| `FEEDBACK_EXEMPLES` | `5` | Number of recent feedback decisions given to Qwen as examples. `0` disables them. Never used in a personal-data profile. |
| `PROFIL_DONNEES_PERSO` | `bureautique_fictive` | Restrictive profile the code switches to when personal data is detected in free text |

## Agents

| Variable | Default | Meaning |
|---|---|---|
| `AGENT_<ROLE>` | none (role default: `claude_haiku,qwen`) | Agent(s) of a role: `agent` or `agent,fallback1,fallback2` |
| `AGENT_<ROLE>_DIFFICILE` | none | Agent used for tasks judged hard (length or score criterion) |
| `AGENT_SEUIL_CARACTERES` | `6000` | Prompt length from which a task is hard (only if a `_DIFFICILE` agent exists) |
| `AGENT_SEUIL_SCORE` | `5` | Complexity score from which a task is hard, when a score is given |
| `AGENT_DECLARATIONS` | empty | Extra agents, as a JSON object `{"name": {"fournisseur": ..., "modele": ...}}` |

Roles (`<ROLE>` in upper case): `ORGANISATEUR`, `WORKER`, `RELECTEUR`, `GRAND_MODELE`, `INTERFACE`, `CADRAGE`, `ROUTAGE`, `PROMESSE`, `FORMAT`, `DEDUP`, `REPARATION`, `REFORMULATION`. Defaults (V3): every role uses `claude_haiku`, then `qwen` as fallback. Examples: `AGENT_REFORMULATION=qwen`, `AGENT_WORKER=gemini,qwen`, `AGENT_DEDUP=qwen`. Zero-cost setup: assign the generation roles to `gemini,qwen` and the other roles to `qwen`. Any role sent to Gemini or Claude is an external send; see [agents.md](agents.md).

## Set at runtime (not in `.env`)

- `/profil <nom>`: profile, and lifts the personal-data lock.
- `/organisateur`, `/grand`: organizer and big model (`agents` = the role, default; or a direct `gemini` / `claude` client).
- `/accord oui|non`: session consent for one provider, covering rephrasing and the other free-text roles.

## Profile settings (`profiles/<nom>/profil.json`)

Keys read by `profiles/loader.py`: `nom`, `description`, `donnees_personnelles` (boolean, required), `workers` (required), `revue` (`seuil`, `shortlist_min`, `shortlist_max`), `champs_utiles`, `champs_personnels`, `exemples`, `agents`. A profile with `donnees_personnelles: true` cannot declare or assign any external agent; the default external agents are dropped at call time and every role is handled by the local Qwen.

## Claude

Claude mode needs Claude Code installed and logged in with a claude.ai account (`claude auth login`). No variable holds a Claude credential. See the Claude section of the [README](../README.md#claude-mode-optional).

Only Windows is validated.

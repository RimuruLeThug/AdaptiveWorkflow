# Configuration reference

Values come from `config.py` (defaults) and can be overridden in `.env` (copy `.env.example`; `.env` is git-ignored).
Variables are read by `load_settings()` in `config.py` (and `GEMINI_API_KEY` by `gemini_api_key()`).

| Variable | Default | Meaning |
|---|---|---|
| `GEMINI_API_KEY` | (required) | Gemini key; never stored in settings |
| `GEMINI_MODEL` | `gemini-3.5-flash-lite` | Gemini model for organizer, workers, reviewers |
| `GEMINI_MIN_INTERVAL_S` / `GEMINI_DAILY_ALERT` / `GEMINI_DAILY_HARD_STOP` / `GEMINI_MAX_RETRIES` | 5 / 1200 / 1400 / 2 | rate limiter |
| `GEMINI_TIMEOUT_S` | 120 | request timeout |
| `DELEGATION_BUDGET`, `MAX_DEPTH`, `MAX_PLAN_ATTEMPTS` | 6, 1, 2 | orchestration bus |
| `REVIEW_THRESHOLD`, `REVIEW_VOTES_NEEDED`, `SHORTLIST_MIN`, `SHORTLIST_MAX`, `CONTRARIAN_QUOTA`, `CONTRARIAN_FLOOR`, `MAX_REVIEW_IDEAS` | see `docs/calibration.md` | review and vote |
| `BIG_MODEL_PROVIDER`, `CLAUDE_MODEL`, `CLAUDE_TIMEOUT_S`, `CLAUDE_ALLOW_API_BILLING`, `COMPLEXITY_THRESHOLD` | `gemini`, `sonnet`, 600, off, 5 | **workflow escalation** (product feature) |
| `QWEN_INTERFACE_MODEL`, `QWEN_FORMAT_MODEL`, `OLLAMA_HOST_URL`, `QWEN_INTERFACE_NUM_CTX`, `QWEN_FORMAT_NUM_CTX`, `LOCAL_KEEP_ALIVE` | see `config.py` | local models (Ollama) |
| `ORCH_STATE_DIR`, `ORCH_LOGS_DIR`, `PROFILE` | `state/`, `logs/`, `brainstorm` | folders and startup profile |
| `GEMINI_TEST_MODEL` | empty | model of the `gemini_test` agent (empty = unavailable) |
| `AGENT_<ROLE>`, `AGENT_<ROLE>_DIFFICILE` | none | role reassignment, `agent[,fallback...]` |
| `AGENT_SEUIL_CARACTERES`, `AGENT_SEUIL_SCORE`, `AGENT_DECLARATIONS` | 6000, 5, empty | simple/hard routing criterion, extra agents (JSON) |

Only Windows is validated.

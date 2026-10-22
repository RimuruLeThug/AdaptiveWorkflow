# Agent interchangeability

Workflow agents (Qwen, Gemini, Claude through `claude_cli.py`) are interchangeable through `agents.py`. A role
(organizer, worker, reviewer, big model, rephrasing for free-language mode, and the Qwen roles interface/framing/routing/promise/format/dedup/repair)
is reassigned by configuration (`AGENT_<ROLE>` in `.env`) or by the `agents` key of a profile, without touching the
skeleton. Since V3 every role defaults to `claude_haiku` with the single local `qwen` model as fallback. This is unrelated to the development sub-agents used to build the project.

## Verified offline only (unit tests with fake agents: `tests/test_agents.py`, `tests/test_wiring.py`)
- Contract cases: valid reply, invalid JSON repaired by a fake local model, timeout, HTTP 429, out-of-schema reply, empty reply.
- Role replacement by configuration or profile only; routing by configured rule; fallback when an agent is unavailable.
- Each routing choice is logged on the bus log (`agent_call`: role, provider, model, duration_s, result, reason).
- Every Gemini call, whatever the agent, goes through the single rate-limiter queue.

## Replacing Qwen roles with Gemini
Test entry `gemini_test`: its model name comes only from `GEMINI_TEST_MODEL` (empty by default = agent unavailable).
Offline tests check that each Qwen role accepts it with the same output shape, that calls use the shared limiter and
that the choice is logged. Privacy tests check that a personal-data profile always refuses any external agent, that
masking and leak check run before sending, and that free text still requires user confirmation.
**Warning:** this replacement adds an external send, is reserved for fictitious data, and consumes the shared Gemini quota.

## Real calls
No comparison of agents with real models was made: no test model name was provided (`GEMINI_TEST_MODEL` empty), so the
`gemini_test` entry was never used with a real model, and no swapped role was measured.
The only real runs with the agent machinery are the ones of the `auto_projet` profile (organizer on Gemini, one short run
with Claude as organizer), described in [auto_projet.md](auto_projet.md). They are single runs, not a comparison.
Claude calls consume the subscription quota; they are not free.
Real-model comparison tables are therefore empty; run them manually if needed.

## Limits
Offline doubles do not measure real latency, JSON validity or agreement between models. Results with few trials would not be statistically meaningful.

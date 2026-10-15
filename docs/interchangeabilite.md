# Agent interchangeability

Workflow agents (Qwen, Gemini, Claude through `claude_cli.py`) are interchangeable through `agents.py`. A role
(organizer, worker, reviewer, big model, and the Qwen roles interface/framing/routing/promise/format/dedup/repair)
is reassigned by configuration (`AGENT_<ROLE>` in `.env`) or by the `agents` key of a profile, without touching the
skeleton. Defaults are unchanged: Qwen roles stay local. This is unrelated to the development sub-agents used to build the project.

## Verified offline only (unit tests with fake agents: `tests/test_agents.py`, `tests/test_wiring.py`)
- Contract cases: valid reply, invalid JSON repaired by a fake Qwen 2.5 3B, timeout, HTTP 429, out-of-schema reply, empty reply.
- Role replacement by configuration or profile only; routing by configured rule; fallback when an agent is unavailable.
- Each routing choice is logged on the bus log (`agent_call`: role, provider, model, duration, result, reason).
- Every Gemini call, whatever the agent, goes through the single rate-limiter queue.

## Replacing Qwen roles with Gemini
Test entry `gemini_test`: its model name comes only from `GEMINI_TEST_MODEL` (empty by default = agent unavailable).
Offline tests check that each Qwen role accepts it with the same output shape, that calls use the shared limiter and
that the choice is logged. Privacy tests check that a personal-data profile always refuses any external agent, that
masking and leak check run before sending, and that free text still requires user confirmation.
**Warning:** this replacement adds an external send, is reserved for fictitious data, and consumes the shared Gemini quota.

## Real calls
None were made: no test model name was provided (`GEMINI_TEST_MODEL` empty) and no Qwen/Ollama or Claude call was run.
Gemini requests used: 0. Claude calls used: 0 (a Claude call would consume the subscription, it is not free).
Real-model comparison tables are therefore empty; run them manually if needed.

## Limits
Offline doubles do not measure real latency, JSON validity or agreement between models. Results with few trials would not be statistically meaningful.

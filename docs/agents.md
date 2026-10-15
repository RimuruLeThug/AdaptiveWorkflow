# Agents, roles and profiles

This page is about the **product** (the agents used by the workflow). Only the product is described here.

## Roles

Defined in `agents.py` (`ROLES`), with the default agent of each role:

| Role | Used for | Default agent |
|---|---|---|
| `organisateur` | splits a task and delegates to workers | `gemini` |
| `worker` | executes one isolated instruction | `gemini` |
| `relecteur` | scores ideas during a review | `gemini` |
| `grand_modele` | studies a validated idea (escalation) | `gemini` (`claude` if `BIG_MODEL_PROVIDER=claude`) |
| `interface` | local interface, summaries | `qwen_interface` |
| `cadrage` | framing of the project sheet | `qwen_interface` |
| `routage` | proposes a command from free text | `qwen_interface` |
| `promesse` | promise check, closed yes/no question | `qwen_interface` |
| `format` | JSON formatting | `qwen_format` |
| `dedup` | deduplication of ideas | `qwen_format` |
| `reparation` | repair of invalid JSON | `qwen_format` |
| `reformulation` | rewrites free text into a clear request for Qwen | `gemini` |

The `reformulation` role is the only one that can use a once-per-session consent (`/accord`), and only for one provider. Roles that can carry free text require your confirmation before any external send.

## Reassign a role

In `.env`, `AGENT_<ROLE>=agent[,fallback...]` (first agent is the main one, the others are fallbacks). `AGENT_<ROLE>_DIFFICILE=agent` names the agent for hard tasks.

```
AGENT_REFORMULATION=qwen_interface
AGENT_DEDUP=qwen_format,qwen_interface
```

In a profile (`profiles/<name>/profil.json`), optional `agents` key:

```json
"agents": {"declarations": {"my_agent": {"fournisseur": "ollama", "modele": "...", "hote": "http://localhost:11434"}},
           "roles": {"dedup": "my_agent"}}
```

Known agents: `qwen_interface`, `qwen_format`, `gemini`, `gemini_test` (model only from `GEMINI_TEST_MODEL`; empty means unavailable), `claude`, and the agents declared in `AGENT_DECLARATIONS` or in a profile.

Providers for declared agents: `ollama`, `local_openai` (the `hote` of a local provider must be a loopback address: `localhost`, `127.0.0.1` or `::1`), `gemini`, `claude`. Routes are computed at startup from the environment and the startup profile.

## Rules

- Every Gemini call goes through the single rate-limiter queue (shared quota), whatever the agent.
- A role sent to Gemini or Claude is an external send: masking and leak check first; user confirmation for roles that can carry free text; refused entirely for personal-data profiles, fallbacks included.
- Each call is logged in the run log (`agent_call` events: role, provider, model, duration, result, reason, agent, external, attempt), without the sent text.
- A daily Gemini quota stop is not retried on another Gemini agent.

## Add a profile

Create `profiles/<name>/profil.json` (see `profiles/brainstorm/profil.json`; the schema is validated by `profiles/loader.py`). No business logic goes in the skeleton. Never put real personal data in a profile or in its example data.

## Workflow escalation vs development

The workflow escalation (`escalation.py`, `/escalader`) is a product feature: it sends an ambitious idea to a big model after user confirmation. It is unrelated to the development of the project.

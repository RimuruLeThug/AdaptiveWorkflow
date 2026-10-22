# Agents, roles and profiles

This page is about the **product** (the agents used by the workflow). Only the product is described here.

## Roles

Defined in `agents.py` (`ROLES`). Since V3, **every role's default agent is `claude_haiku`** (Claude Code on your claude.ai subscription, model alias `CLAUDE_HAIKU_MODEL`, `haiku` by default), with the local agent `qwen` (`QWEN_MODEL`, one single model) as fallback:

| Role | Used for | Default route (V3) | Free text |
|---|---|---|---|
| `organisateur` | splits a task and delegates to workers | `claude_haiku`, then `qwen` | no |
| `worker` | executes one isolated instruction | `claude_haiku`, then `qwen` | no |
| `relecteur` | scores ideas during a review | `claude_haiku`, then `qwen` | no |
| `grand_modele` | studies a validated idea (escalation) | `claude_haiku`, then `qwen` (`BIG_MODEL_PROVIDER=gemini\|claude` forces that agent) | no |
| `interface` | summaries, complexity estimate | `claude_haiku`, then `qwen` | yes |
| `cadrage` | framing of the project sheet | `claude_haiku`, then `qwen` | yes |
| `routage` | proposes a command from free text | `claude_haiku`, then `qwen` | yes |
| `promesse` | promise check, closed yes/no question | `claude_haiku`, then `qwen` | yes |
| `format` | JSON formatting | `claude_haiku`, then `qwen` | yes |
| `dedup` | deduplication of ideas | `claude_haiku`, then `qwen` | yes |
| `reparation` | repair of invalid JSON | `claude_haiku`, then `qwen` | yes |
| `reformulation` | rewrites free text into a clear request | `claude_haiku`, then `qwen` | yes |
| `dossier` | understands a folder of the PC, answers about it, proposes changes (V3.2) | `claude_haiku`, then `qwen` | yes, dedicated consent per folder (never covered by the session consent) |

"Free text" roles may carry text you typed: an external send needs your confirmation (masked preview, recipient named) or a session consent. The session consent is asked once, at the first free-text message, for one provider; it covers every free-text role assigned to that provider (`/accord oui|non` grants or revokes it). It is never available for the four generation roles (they are only masked and leak-checked) nor in a personal-data session.

## Reassign a role

In `.env`, `AGENT_<ROLE>=agent[,fallback...]` (first agent is the main one, the others are fallbacks). `AGENT_<ROLE>_DIFFICILE=agent` names the agent for hard tasks. A value replaces the whole default route, fallback included: write the fallback yourself if you want one.

```
AGENT_REFORMULATION=qwen           # rephrasing stays local
AGENT_WORKER=gemini,qwen           # workers on free Gemini, local fallback
AGENT_DEDUP=qwen                   # local deduplication
```

Zero-cost setup (the V2 behaviour, with one local model):

```
AGENT_ORGANISATEUR=gemini,qwen
AGENT_WORKER=gemini,qwen
AGENT_RELECTEUR=gemini,qwen
AGENT_GRAND_MODELE=gemini,qwen
AGENT_REFORMULATION=gemini,qwen
AGENT_INTERFACE=qwen
AGENT_CADRAGE=qwen
AGENT_ROUTAGE=qwen
AGENT_PROMESSE=qwen
AGENT_FORMAT=qwen
AGENT_DEDUP=qwen
AGENT_REPARATION=qwen
```

In a profile (`profiles/<name>/profil.json`), optional `agents` key:

```json
"agents": {"declarations": {"my_agent": {"fournisseur": "ollama", "modele": "...", "hote": "http://localhost:11434"}},
           "roles": {"dedup": "my_agent"}}
```

Known agents: `claude_haiku`, `qwen` (local; `qwen_interface` and `qwen_format`, the V2 names, are aliases of the same model and options, so Ollama loads it once), `gemini`, `gemini_test` (model only from `GEMINI_TEST_MODEL`; empty means unavailable), `claude` (`CLAUDE_MODEL`, `sonnet` by default), and the agents declared in `AGENT_DECLARATIONS` or in a profile.

Providers for declared agents: `ollama`, `local_openai` (the `hote` of a local provider must be a loopback address: `localhost`, `127.0.0.1` or `::1`), `gemini`, `claude`. Routes are computed at startup from the environment and the startup profile.

## Rules

- Every Gemini call goes through the single rate-limiter queue (shared quota), whatever the agent.
- A role sent to Gemini or Claude is an external send: masking and leak check first; user confirmation or session consent for free-text roles.
- Personal-data profile (or personal data detected in free text): every external agent of a role is dropped before any send, fallbacks included, and the role is handled by its local agent (`qwen`). A personal-data profile still cannot declare or assign an external agent in its own `agents` key.
- An unavailable agent is skipped before any masking or confirmation: Claude Code not found (checked without starting a process), not logged in (checked once at startup), or no Gemini key.
- Each call is logged in the run log (`agent_call` events: role, provider, model, duration, result, reason, agent, external, attempt), without the sent text.
- A daily Gemini quota stop is not retried on another Gemini agent.

## Add a profile

Create `profiles/<name>/profil.json` (see `profiles/brainstorm/profil.json`; the schema is validated by `profiles/loader.py`). No business logic goes in the skeleton. Never put real personal data in a profile or in its example data.

## Workflow escalation vs development

The workflow escalation (`escalation.py`, `/escalader`) is a product feature: it sends an ambitious idea to a big model after user confirmation. It is unrelated to the development of the project.

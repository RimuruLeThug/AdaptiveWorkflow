# Agents, roles and profiles

This page is about the **product** (agents used by the workflow), not about the tools used to develop the project.

## Roles
`organisateur`, `worker`, `relecteur`, `grand_modele` (Gemini by default, Claude possible for the big model) and the Qwen roles
`reformulation`, `interface`, `cadrage`, `routage`, `promesse` (Qwen 3.5 4B) and `format`, `dedup`, `reparation` (Qwen 2.5 3B).

The `reformulation` role (Gemini by default) rewrites free text for Qwen; it is the only role that can use a once-per-session consent (`/accord`), and only for one provider.

## Reassign a role
In `.env`: `AGENT_ROUTAGE=gemini_test,qwen_interface` (main agent, then fallbacks) and optionally `AGENT_ROUTAGE_DIFFICILE=...`.
In a profile (`profiles/<name>/profil.json`), optional `agents` key:
```json
"agents": {"declarations": {"my_agent": {"fournisseur": "ollama", "modele": "...", "hote": "http://localhost:11434"}},
           "roles": {"dedup": "my_agent"}}
```
Providers: `ollama`, `local_openai` (the `hote` of local providers must be a loopback address), `gemini`, `claude`. Routes are computed at startup from the startup profile.

## Rules
- Every Gemini call goes through the single rate-limiter queue (shared quota).
- A role sent to Gemini or Claude is an external send: masking and leak check first; user confirmation for roles that can carry free text; refused entirely for personal-data profiles, fallbacks included.
- Each call is logged on the bus log (`agent_call`: role, provider, model, duration_s, result, reason, plus agent, external and attempt), without the sent text.

## Add a profile
Create `profiles/<name>/profil.json` (see `profiles/brainstorm/profil.json`; schema validated by `profiles/loader.py`). No business logic goes in the skeleton. Never put real personal data in a profile or its example data.

## Workflow escalation vs development
The workflow escalation (`escalation.py`, `/escalader`) is a product feature: it sends an ambitious idea to a big model after user confirmation. It is unrelated to development tooling.

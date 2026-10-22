# Architecture

## Principle

The code decides, the models propose. Models never call each other, never call Gemini on their own, and never act on files or accounts. Every model output is parsed and checked by code before it is used, and every decision is taken by code (votes, scores, budgets, routes, policy).

## Modules

| Module | Role |
|---|---|
| `config.py` | Settings from environment and defaults; `.env` loader; Gemini key check. Keys are never stored in `Settings`. |
| `rate_limiter.py` | The single Gemini queue: spacing, retries on 429, persistent daily counter, alert and hard stop, Pacific-time reset. |
| `gemini_client.py` | Gemini REST client (standard library). Every request goes through the limiter. The key is sent in a header. |
| `local_llm.py` | Local model interface (`LocalLLM`) and the Ollama backend. Local models do not use the Gemini limiter. |
| `claude_cli.py` | Claude through Claude Code (`claude -p`), no tools, empty temporary directory, login checked, daily call counter. |
| `agents.py` | Swappable agents: roles, registry, routing with fallback, difficulty criterion, external-send rules, call logging. |
| `orchestrator.py` | The bus: plan validation, isolated delegations, output checks, optional synthesis, run log. |
| `organizers/` | Organizer implementations (`gemini.py`, `claude.py`) behind one base class. |
| `workers/` | Worker contract (`base.py`), Gemini worker (`gemini.py`), default workers (`defaults.py`), deduplication (`dedup.py`). |
| `review.py` | Three reviewers, batch scoring, validation of each review, approval, 2-of-3 vote, weighted score, shortlist. |
| `escalation.py` | Complexity estimate, idea contract, big-model call, diff checks, promise check, escalation log. |
| `verify/` | `schema.py` (JSON schema check), `repair.py` (deterministic repair, then Qwen 2.5 3B with a fidelity check), `promise.py` (promise check), `basic.py` (output check of workers), `haiku.py` (Claude verification, disabled). |
| `privacy/` | `detectors.py` (deterministic detection), `masker.py` (masking, preview, leak check, re-injection), `vault.py` (encrypted token vault: DPAPI on Windows, Fernet elsewhere), `policy.py` (provider policy), `__main__.py` (`selftest`, `apercu`). |
| `profiles/` | `loader.py` validates `profil.json` and `donnees.json`; one folder per profile. |
| `qwen_ui/` | Terminal. `app.py` (commands, free text, confirmations), `router.py` (commands, whitelist, routing prompts), `langage_libre.py` (rephrasing, implicit profile, feedback journal), `fiche.py` (project sheet), `presenter.py` (display), `bootstrap.py` (assembly, startup checks), `__main__.py` (entry point). |
| `pilot.py` | Replays a scenario with automatic "o" answers and writes a transcript and measurements to `logs/`. |
| `calibrate.py` | Reads logs and decisions, suggests thresholds. Read-only. |

Assembly is done in `qwen_ui/bootstrap.py`. Since V3 every role goes through the agent router: by default each role uses `claude_haiku` (Claude Code) with the single local `qwen` model as fallback. `/organisateur gemini|claude` and `/grand gemini|claude` can still force a direct client.

## Startup

1. `python -m qwen_ui` loads `.env` and the settings.
2. The Gemini key is optional: without it, Gemini agents are unavailable (a warning is shown if a role uses Gemini).
3. Ollama and the local model (`QWEN_MODEL`) are checked; if they are missing, a warning is shown (no local fallback, no personal-data processing) but the program starts.
4. The startup profile (`PROFILE`) is loaded and validated.
5. The shared objects are built: one rate limiter on `state/gemini_quota.json`, one Gemini client (if a key exists), one Claude client, the agents and their routes. If a route uses Claude, `claude auth status` is checked once; on failure a warning is shown and the Claude agents are marked unavailable for the session, so roles go straight to their fallback.
6. The loop starts. On exit (normal exit, Ctrl+C or error), the project's models are unloaded from VRAM.

## Free-text request

Text that does not start with `/` goes through these steps. Each one is decided by code; the models only propose.

1. **Personal data check (code).** The deterministic detectors look for e-mail addresses, IBANs, French social-security numbers, phone numbers, birth dates, SIRET and announced SIREN numbers, postal addresses, postal codes followed by a town, declared values and person names (title + name, "Firstname LASTNAME", common acronyms excluded). If one is found, the session switches to the restrictive profile, the session consent is revoked, and every role is handled by the local Qwen until an explicit `/profil`.
2. **Explicit profile.** If the message names a profile ("passe en auto_projet"), it is noted. A personal-data profile is loaded at once, before any send.
3. **Rephrasing.** The `reformulation` role turns the message into a clear request. Default: `claude_haiku`, after a single consent for the session; this consent also covers the other free-text roles assigned to the same provider. Masking and the leak check run before each external send. If the external agent fails, a local fallback (Qwen) is used. In a personal-data session the rephrasing is local only.
4. **Routing.** The `routage` agent receives the rephrased request, the ideas of the last review and recent feedback examples, and proposes one action from a whitelist, plus a profile name. The code checks the action and the profile name, and rejects anything else.
5. **Feedback branch.** If the action is `retour`, the feedback is interpreted by Qwen and checked by the code (numbers must exist in the last review). It is applied only after your `o`. Profiles are not changed by feedback.
6. **Profile choice.** The proposed profile is used only if it exists, and never to leave a personal-data profile.
7. **Confirmation.** `tache`, `revue`, `fiche` and `organisateur` need your `o` before they run. Other actions (`dedup`, `detail`, `resume`, `quota`, `workers`, `aide`) run directly, and they are local or read-only.
8. **Dispatch.** The action runs through the same handler as the matching explicit command.

## Explicit commands

- `/tache`: checks the policy for the organizer and the workers (with the default `agents` organizer, the router applies it per role), warns if a role uses Gemini and the remaining Gemini quota is below the worst case (plans + delegations + synthesis), then calls the orchestrator with the project sheet and the refusals of earlier sessions (never personal ones).
- `/dedup`: deduplicates the ideas of the last run. Exact duplicates are grouped without a model. The `dedup` agent may propose more groups, and each group is checked (existing ids, at least two ideas, no mixing of contrarian ideas with others, no single group absorbing more than half the ideas when there are six or more). If nothing is valid, all ideas are kept.
- `/revue`: checks the policy, checks the remaining Gemini quota if reviewers use Gemini (three requests), then the three reviewers each score all deduplicated ideas in one request, without seeing the others. Each review is validated (JSON, schema, unknown or duplicate ids, missing ideas counted as abstentions). Approval is `score >= threshold`; an idea is approved if at least 2 of 3 approve. The shortlist takes the approved ideas first (at most `SHORTLIST_MAX`, 8 by default), completes with the best non-approved ideas up to `SHORTLIST_MIN` (5 by default), then keeps at least `CONTRARIAN_QUOTA` contrarian ideas whose weighted score reaches `CONTRARIAN_FLOOR`, even at an average score; on a full list they replace the weakest non-contrarian ideas.
- `/valider`, `/refuser`: append a decision to `state/decisions.jsonl`. Refusals of non-personal ideas are passed to the organizer at the next `/tache`.
- `/escalader`: checks the policy for the big model, requires a validated and approved idea, has the `interface` agent estimate the complexity, computes the score in code, builds the contract in code and freezes it after your `o`, shows the cost, asks for a second `o`, then calls the big model. The result is checked: JSON and schema, non-negotiable constraints present, no out-of-scope item added, every change justified, promise unchanged (the `promesse` agent answers one closed question). Any failure rejects the version.

## Orchestration bus (`/tache`)

1. **Plan.** The organizer (the `organisateur` role, or a direct Gemini or Claude client) returns a JSON plan. The bus checks it against the closed list of workers of the profile and against the delegation budget. An invalid plan is rejected as a whole; the organizer may correct it once (`MAX_PLAN_ATTEMPTS`).
2. **Delegations.** Each task goes to one worker. A worker sees only its own instruction and the project sheet. Workers do not delegate (`MAX_DEPTH=1`). Each delegation counts against `DELEGATION_BUDGET`, including retries.
3. **Verification.** Each output is checked (`verify/basic.py`) before it is used. A failed output is not returned as valid; the failure is logged.
4. **Synthesis.** If at least one output is accepted, the organizer may write a synthesis, which is checked the same way.
5. **Log.** Every step is written as an event in `logs/run-<date>-<id>.jsonl`.

## Journals and state

| Path | Written by | Content | Git-ignored |
|---|---|---|---|
| `logs/run-*.jsonl` | `RunLogger` (orchestrator, and agent routing) | run events: plan accepted or rejected, delegations, synthesis, `agent_call` events without sent text | yes |
| `logs/review-*.jsonl` | `review.py` | each reviewer's scores and each review's validation; used by `calibrate.py` | yes |
| `logs/escalation-<id>.json` | `escalation.py` | escalation report, contract, checks, result | yes |
| `logs/pilot-*.txt`, `logs/pilot-*.json` | `pilot.py` | transcript and measurements of a scripted run | yes |
| `state/gemini_quota.json` | `rate_limiter.py` | daily Gemini counter (Pacific day) | yes |
| `state/claude_calls.json` | `claude_cli.py` | daily Claude call counter | yes |
| `state/fiche_projet.json` | `qwen_ui/fiche.py` | the validated project sheet | yes |
| `state/decisions.jsonl` | `qwen_ui/app.py` | every `/valider` and `/refuser` decision, with score and votes; personal ones are masked | yes |
| `state/retours.jsonl` | `qwen_ui/langage_libre.py` | free-language feedback, masked, used as examples for Qwen | yes |
| `state/coffre.bin` | `privacy/vault.py` | encrypted token vault (DPAPI on Windows; `vault.key` elsewhere) | yes |
| `state/escalations/<id>.json` | `escalation.py` | copy of each escalation report | yes |
| `state/pilot/` | `pilot.py` | the scripted run's own sheet and decisions | yes |

`logs/` and `state/` are created at run time. Nothing in them is sent anywhere by the project.

## Quotas

- **Gemini**: one queue for every Gemini call (organizer, workers, reviewers, rephrasing, escalation, and any agent that uses Gemini). Spacing 5 s by default. Two retries on HTTP 429, each counted and each going through the queue. The counter is incremented before each call. Warning at 1,200 per day, hard stop at 1,400 per day (`DailyQuotaExhausted`). Reset at midnight Pacific time. A Gemini error stops the run; it is reported and the program stays open.
- **Claude**: no limiter in the project. Each call is counted per day. The limit is the claude.ai subscription, managed by Anthropic; a Claude call is never free.
- **Ollama**: no quota. Concurrency is handled by Ollama (`OLLAMA_NUM_PARALLEL`, VRAM); `LOCAL_KEEP_ALIVE` controls how long models stay loaded.

The limiter's state file must be used by one process at a time.

## Data flow with personal data

A personal-data profile or a session lock makes every external agent refuse the send (`PolicyError`), before any masking or network call, including fallbacks. Without that, external sends are masked, checked for leaks, and shown to you with the recipient named. Real values are re-injected locally by code only.

## Related pages

- [configuration.md](configuration.md): every variable.
- [agents.md](agents.md): roles and profiles.
- [troubleshooting.md](troubleshooting.md): messages and fixes.
- [calibration.md](calibration.md): thresholds and `calibrate.py`.
- [interchangeabilite.md](interchangeabilite.md): what was checked for swappable agents.
- [auto_projet.md](auto_projet.md): the one real run of the project on itself.

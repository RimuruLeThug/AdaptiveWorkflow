# AdaptiveWorkflow

> **Version 2.0.0.** Tested on Windows only. The logic is covered by unit tests that need no network and no real model. The real-model use so far is one documented run ([docs/auto_projet.md](docs/auto_projet.md)). Read the [Roadmap](#roadmap) before relying on it.

**A multi-agent workflow for the terminal, with no mandatory cost.** Local models (Qwen through Ollama) and the free Gemini API work together under plain Python code that makes the decisions. Each user uses their own Gemini key and their own local models.

The core provides the machinery (job queue and budgets, organizer and workers, output checks, review and voting, escalation, personal-data masking, logs). Each use case is a *profile* in `profiles/`. The interface text and the prompts are in French; this page is in English, and the French version is [README.fr.md](README.fr.md).

**Contents:** [Scope](#scope) · [Quick start](#quick-start) · [Tested configuration](#tested-configuration) · [Detailed installation](#detailed-installation) · [Usage](#usage) · [Free-language mode](#free-language-mode) · [Commands](#commands) · [Swappable agents](#swappable-agents) · [Profiles](#profiles) · [Gemini quotas](#gemini-quotas) · [Claude mode (optional)](#claude-mode-optional) · [Workflow escalation](#workflow-escalation) · [Personal data and GDPR](#personal-data-and-gdpr) · [Calibration](#calibration) · [Architecture](#architecture) · [Tests](#tests) · [Known limits](#known-limits) · [Roadmap](#roadmap) · [Legal note on profiles](#legal-note-on-profiles) · [License](#license)

## Scope

The project takes a project description (`/fiche`), asks an organizer to split a task between workers (`/tache`), groups duplicate ideas (`/dedup`), has three reviewers score them (`/revue`) and records your decisions (`/valider`, `/refuser`). For an idea you validated and reviewers approved, a large model can study it (`/escalader`), after a frozen contract and your confirmation.

Principles:

- **The organizer proposes, the code decides.** Closed list of workers, delegation budget, maximum depth, invalid plans rejected as a whole, every output verified.
- **No groupthink.** Each worker and each reviewer works without seeing the others' answers.
- **The code computes** votes, scores and shortlists. Models only propose.
- **No free-text request leaves for an external model without your confirmation.** The only exception is the rephrasing role, which needs one consent per session (see [Free-language mode](#free-language-mode)).
- **One rate-limited queue** for every Gemini call.

What the project does **not** do:

- It does not run code, change files or act on accounts. Models produce text only; Claude is called with no tools.
- It has no web or graphical interface. It is a terminal application.
- It is not for personal data in V1: the free Gemini tier and a Claude subscription are excluded for customer data (see [Personal data and GDPR](#personal-data-and-gdpr)).
- Its thresholds are not calibrated. The default values are provisional.

## Quick start

Windows only, validated:

1. Download the archive `AdaptiveWorkflow-vX.Y.Z.zip` from the **Releases** page of the GitHub repository, and unzip it into an empty folder.
2. Double-click **`install.bat`**. It checks Python 3.11 or newer, installs `tzdata`, checks Ollama and offers to download the two models `qwen3.5:4b-q8_0` and `qwen2.5:3b-instruct-q4_K_M`. It creates `.env` from `.env.example`, without any key.
3. Open `.env` and set `GEMINI_API_KEY=` to your key. A free key is available in [Google AI Studio](https://aistudio.google.com) (API keys).
4. Double-click **`start.bat`** to open the terminal. Type `/aide` for the command list.

Optional: `tests.bat` runs the test suite; the result is written to `logs\tests.txt`.

## Tested configuration

| | |
|---|---|
| OS | Windows, Python 3.12 |
| CPU | AMD Ryzen 7 7800X3D |
| GPU | AMD Radeon RX 7800 XT, 16 GB (Ollama at 100 % GPU) |
| RAM | 32 GB |
| Local models | `qwen3.5:4b-q8_0` (5.2 GB), `qwen2.5:3b-instruct-q4_K_M` (1.9 GB) |
| API | Gemini `gemini-3.5-flash-lite`, free tier |

The core uses only the Python standard library (3.11+). `tzdata` is recommended. Other platforms may work, but only Windows has been tested. The encrypted masking vault uses Windows DPAPI; elsewhere it needs the optional `cryptography` package.

## Detailed installation

Manual alternative to `install.bat`:

1. **Python 3.11 or newer** (tested with 3.12). Then `pip install -r requirements.txt` (installs `tzdata`).
2. **Ollama** ([ollama.com](https://ollama.com)), then:
   ```
   ollama pull qwen3.5:4b-q8_0
   ollama pull qwen2.5:3b-instruct-q4_K_M
   ```
   After a first call, `ollama ps` should show `100% GPU` in the PROCESSOR column.
3. **Gemini key.** Copy `.env.example` to `.env` (exact name, at the project root) and set `GEMINI_API_KEY`. The program refuses to start without it.
4. **Checks:**
   ```
   python -m gemini_client      # one real Gemini request (counted in the quota): answers "OK"
   python -m local_llm          # both local models answer
   python -m privacy selftest   # encrypted masking vault
   python -m unittest discover -s tests -t .
   ```
5. **Run:** `python -m qwen_ui` (or `start.bat`).

Optional, for better detection of French names: `pip install spacy` then `python -m spacy download fr_core_news_md`.

### About `.env` and secrets

Your API key lives **only** in your local `.env` file. It is listed in `.gitignore` and must never be committed. The repository ships only `.env.example`, with an empty key. Local state (`state/`: quota counters, masking vault, decisions) and logs (`logs/`) are git-ignored too. No key is written anywhere in the code, and the key is sent in a request header, never in a URL.

## Usage

Start the terminal (`python -m qwen_ui` or `start.bat`). The program refuses to start if the Gemini key is missing or if the Qwen 3.5 4B interface model is not available in Ollama. On exit, the project's models are unloaded from VRAM.

Typical session: describe the project with `/fiche`, validate it, run `/tache <instruction>`, then `/dedup`, `/revue`, `/valider` or `/refuser`. For a validated idea, `/escalader <n>` is the optional last step.

### Free-language mode

Without a leading `/`, you can talk to Qwen freely.

1. **Rephrasing.** A `reformulation` agent rewrites your message into a clear request for Qwen. By default it is Gemini; `AGENT_REFORMULATION=qwen_interface` keeps everything local. Gemini is asked **once per session** for permission. Refusing, or `/accord non`, keeps rephrasing local. Every external send is masked and checked for leaks first.
2. **Routing.** Qwen proposes one action from a whitelist (`tache`, `fiche`, `dedup`, `revue`, `retour`, `detail`, `resume`, `organisateur`, `quota`, `workers`, `aide`, `discussion`). The code validates it. Costly or state-changing actions (`tache`, `revue`, `fiche`, `organisateur`) are shown and need your `o`.
3. **Implicit profile.** You can name a profile ("passe en auto_projet"), or Qwen proposes one from the existing list. The code checks the name. A personal-data profile is loaded immediately, before any send.
4. **Personal data.** The code (not the model) detects identifiers such as e-mail addresses, IBANs, French social-security numbers, phone numbers and birth dates. If it finds one, it switches to the restrictive profile (`PROFIL_DONNEES_PERSO`, default `bureautique_fictive`), revokes the rephrasing consent and blocks external sends for the session. Only an explicit `/profil <name>` lifts this lock.
5. **Feedback.** You can answer results in plain words ("keep 2, 1 is too generic"). Qwen interprets it, the code checks the numbers, and you confirm. A reason is asked once and is optional. Decisions go through the same mechanism as `/valider` and `/refuser`. Each decision is stored in `state/retours.jsonl` (masked, local). The last few decisions (`FEEDBACK_EXEMPLES`, default 5) are given to Qwen as examples, never in a personal-data profile. Feedback applies only to the last review.

Not validated with real models; covered by unit tests with doubles.

### Commands

| Command | Purpose | Gemini requests |
|---|---|---|
| `/aide` | show the help | 0 |
| `/fiche <description>` | Qwen drafts the project sheet, you validate it (`/fiche` alone shows it, `/fiche effacer` deletes it) | 0 |
| `/tache <instruction>` | the organizer splits the task and delegates to workers; at most 9 requests by default (2 plans, 6 delegations, 1 synthesis), plus retries | up to 9 |
| `/detail <n>` | full output of one worker from the last run | 0 |
| `/dedup` | groups duplicate ideas (exact duplicates, then Qwen 2.5 3B proposals checked by code) | 0 |
| `/revue` | 3 reviewers score the ideas (one request each); 2 of 3 must approve; shortlist | 3 |
| `/valider <n> [...]` | validates ideas of the last review | 0 |
| `/refuser <n> [...] [reason]` | refuses ideas; the refusal is sent to the organizer at the next `/tache` (unless it contains personal data) | 0 |
| `/escalader <n> [force]` | a large model studies a validated, approved idea, after a frozen contract and confirmation; `force` bypasses the complexity threshold | 1 Gemini request, or 1 Claude call |
| `/organisateur gemini\|claude` | choose the organizer (`claude` is checked and needs confirmation) | 0 |
| `/grand gemini\|claude` | choose the big model | 0 |
| `/profil [name]` | show, or force, the profile (optional, the code chooses it) | 0 |
| `/accord [oui\|non]` | show, grant or revoke the session consent for external rephrasing | 0 |
| `/masquer <text>` | local masking preview and leak check; nothing is sent | 0 |
| `/donnees` | the fictional data of the profile, minimised and masked | 0 |
| `/resume` | local summary (Qwen) of the last result | 0 |
| `/workers` | list the closed list of workers | 0 |
| `/quota` | Gemini requests used today and remaining | 0 |
| `/quitter` | quit | 0 |

Aliases: `/task`, `/tâche`, `/help`, `/org`, `/review`, `/doublons`, `/détail`, `/résumé`, `/q`, `/exit`, `/quit`.

Without a leading `/`, the commands above can also be requested in free language (see above). `/escalader`, `/grand`, `/masquer`, `/profil`, `/valider`, `/refuser`, `/accord`, `/donnees` and `/quitter` are only available as explicit commands (decisions can still be given in plain language after a review).

## Swappable agents

Each role of the workflow can be assigned to another agent, by `.env` or by a profile, without changing the code (`agents.py`). The roles are `organisateur`, `worker`, `relecteur`, `grand_modele`, `interface`, `cadrage`, `routage`, `promesse`, `format`, `dedup`, `reparation`, `reformulation`.

```
AGENT_REFORMULATION=qwen_interface        # rephrasing stays local (no external send)
AGENT_DEDUP=qwen_format,qwen_interface    # main agent, then fallback
```

Agents: `qwen_interface`, `qwen_format` (local), `gemini`, `gemini_test` (its model comes only from `GEMINI_TEST_MODEL`, empty means unavailable), `claude`, and agents declared in `AGENT_DECLARATIONS` or in a profile (providers `ollama`, `local_openai` on a loopback address, `gemini`, `claude`). Any role sent to Gemini or Claude is an external send: it is masked, checked for leaks, and confirmed by you, except the rephrasing role under session consent. A personal-data profile refuses every external agent, fallbacks included. Every Gemini call, whatever the agent, uses the same queue and quota.

Verified with unit tests and test doubles only. Comparisons of agents with real models have not been run. Details: [docs/agents.md](docs/agents.md) and [docs/interchangeabilite.md](docs/interchangeabilite.md).

## Profiles

A profile is a folder `profiles/<name>/` containing `profil.json` (workers, instructions, output schemas, review thresholds, fields) and optionally `donnees.json` (**fictional** example data). The code validates the profile when it loads it. A new use case is added without modifying the skeleton.

| Profile | Purpose | Personal data | Workers |
|---|---|---|---|
| `brainstorm` (default) | ideas and analyses | no | `generateur`, `analyste` |
| `bureautique_fictive` | back-office example (reminders, letters) on **fictional** data; shows minimisation, masking and the refusal of external providers | yes | `redacteur` |
| `auto_projet` | the project used on its own public code and documentation | no | `idees_projet`, `relecteur_projet` |

Use `/profil` to see the list, or `/profil <name>` to force one. The startup profile is set by `PROFILE`.

## Gemini quotas

The rate limiter applies conservative defaults, configurable in `.env`:

| Rule | Value |
|---|---|
| Spacing between two requests | 5 s (12 per minute) |
| Warning | 1,200 requests per day |
| Hard stop | 1,400 requests per day (no more Gemini call that day) |
| Retries on HTTP 429 | 2, each one counted; the delay requested by Google is respected |
| Reset | midnight, Pacific time |

All Gemini requests (organizer, workers, reviewers, rephrasing, escalation) go through **one single queue** and share this budget. The counter is written to disk before each call and survives restarts, so a request that was sent is counted even if the program crashes. `/quota` shows the count.

**Exact limits vary by model and over time**: check them for your key in AI Studio ([rate limits page](https://aistudio.google.com/rate-limit)).

## Claude mode (optional)

Claude is an option for the organizer and the big model. It goes through **Claude Code** (`claude -p`), logged in with **your claude.ai subscription**. It **consumes your subscription quota**; it is never free, and the program never presents it as free.

- Install Claude Code and log in: `claude auth login` (claude.ai account).
- The program checks the login (`claude auth status`). An API-key login is **refused**, because it would bill the API, unless you explicitly allow it with `CLAUDE_ALLOW_API_BILLING=1`. The `ANTHROPIC_API_KEY` variable is removed from the environment of the call.
- Claude is called with no tools (no files, no commands), from an empty temporary directory. Calls are counted per day in `state/claude_calls.json`.

## Workflow escalation

Escalation is a product feature: it sends an ambitious idea to a large model. It is not related to the development of the project.

An idea is a candidate if you validated it, 2 of 3 reviewers approved it, and its complexity score exceeds `COMPLEXITY_THRESHOLD` (default 5). Qwen estimates the components (files touched, new dependencies, regression risk); the code computes the score. Then:

1. the code builds a **contract** (promise, scope, out-of-scope, non-negotiable constraints) that you validate and that is then frozen;
2. cost and duration are displayed; **nothing is called without your confirmation**;
3. the large model may reduce the scope, change the approach or stage the work, but may not change the promise, lift a constraint or add out-of-scope items;
4. the code checks the answer (JSON, schema, constraints, scope), then Qwen answers a closed question ("is the promise identical?"). At the slightest doubt the version is rejected, and a new attempt restarts from the original contract.

The report and the result are written to `logs/escalation-<id>.json` and `state/escalations/<id>.json`.

## Personal data and GDPR

The `privacy/` module masks data before anything is sent out: field minimisation, deterministic detection (e-mail addresses, phone numbers, IBANs, French social-security numbers, SIRET/SIREN, postal addresses, postal codes, birth dates, names), indirect clues proposed by Qwen and kept only if they can be verified in the text, a mandatory preview, a **blocking leak check**, a local encrypted vault (DPAPI on Windows) with stable tokens, and local re-injection of the real values by the code.

The detector is **deliberately lax** in V2: only e-mail addresses, IBANs, French social-security numbers, phone numbers and birth dates switch a session to the restrictive profile. Names, addresses and postal codes are still masked before any external send, but they do not switch the profile. This is to be tightened in V3.

**Limits you should know about:**

- Pseudonymized data is still **personal data** (GDPR, recital 26). Masking reduces risk; it does not take you out of the GDPR.
- **Free-tier Gemini and a Claude subscription are excluded for customer data** (no data-processing agreement). A personal-data profile therefore refuses every external provider in V1; only local masking is available. Real use requires a paid offer with a processing agreement and probably a data protection impact assessment (DPIA).
- Automatic masking is not perfect: a combination of details can remain identifying, and a model can copy or invent a value. Without spaCy (`fr_core_news_md`), a name without title or capitalization is only caught if it is in the declared fields. For highly sensitive data, only fully local processing is safe.
- **Test with fictional data only.**

## Calibration

All thresholds are centralised in `config.py` and can be overridden in `.env`. Their current values (6.5/10 for approval, 2 of 3 votes, shortlist of 5 to 8, complexity threshold 5) are provisional: they have **not** been calibrated on real sessions.

`calibrate.py` reads your own logs and decisions and suggests values, with sample sizes and warnings. It is read-only and never writes `.env`:

```
python calibrate.py                 # configured logs/ and state/ folders
python calibrate.py --votes 2       # votes needed (default from config)
python calibrate.py --inclure-pilote  # also count the scripted decisions of pilot.py
```

Below 20 observations the report calls the result anecdotal. Method and details: [docs/calibration.md](docs/calibration.md).

## Architecture

```
config.py            configuration: quotas, models, providers (conservative defaults)
rate_limiter.py      single queue, spacing, retries, persistent daily counter
gemini_client.py     Gemini REST API (standard library), always through the limiter
local_llm.py         local models behind one interface (Ollama in V2)
claude_cli.py        Claude through Claude Code, on a subscription
agents.py            swappable agents: roles, registry, routing, fallback, privacy checks
orchestrator.py      bus: validated plan, isolated delegations, verification, synthesis, log
organizers/          Gemini and Claude organizers
workers/             worker contract, deduplication, default workers
review.py            reviewers, 2-of-3 vote, shortlist
escalation.py        contract, big model, checks
verify/              JSON schema, repair, promise check; haiku.py (verification by Claude Haiku, disabled)
privacy/             detection, masking, leak check, encrypted vault, provider policy
profiles/            one folder per use case, loader.py validates them
qwen_ui/             terminal: app.py, router.py, langage_libre.py, fiche.py, presenter.py, bootstrap.py
pilot.py             replays a scenario without input and measures it
calibrate.py         threshold suggestions from your own runs (read-only)
tests/               19 test modules, no network and no real model required
docs/                detailed documentation (English)
README.md, README.fr.md, CHANGELOG.md, GUIDE_PROMPTS_QWEN_V1.md (how to write sheets and instructions, in French), LICENSE
requirements.txt     optional dependencies (tzdata; spaCy and cryptography are optional)
.env.example         template for .env (no key)
*.bat                install.bat, start.bat, tests.bat, pilot.bat, pilot_dedup.bat, pilot_escalade.bat, privacy_selftest.bat, liberer_vram.bat
pilot_dedup.txt, pilot_escalade.txt   example scenarios for pilot.py
state/, logs/        created at run time, git-ignored
```

The request flow, the journals and the quotas are described in [docs/architecture.md](docs/architecture.md). Common problems and their messages are in [docs/troubleshooting.md](docs/troubleshooting.md). Variables are listed in [docs/configuration.md](docs/configuration.md).

## Tests

```
python -m unittest discover -s tests -t .
```

Or `tests.bat`, which writes the output to `logs\tests.txt`. The suite has about 470 test functions (`def test_`) in 19 modules. It needs no network and no real model: Ollama, Gemini and Claude are replaced by test doubles. Passing tests show the logic with doubles; they do not measure real model behaviour.

## Known limits

- Only Windows is validated.
- Swappable agents and free-language mode are validated by unit tests with doubles. The only real-model run is the one documented in [docs/auto_projet.md](docs/auto_projet.md); it is anecdotal, not a benchmark.
- Thresholds are not calibrated.
- The personal-data detector is deliberately lax (see above); names are masked but do not trigger the restrictive profile.
- Free-tier Gemini and Claude are excluded for personal data; in practice only local processing is possible for such data.
- The Gemini rate limiter assumes a single process using the state file at a time. Concurrency of local models (VRAM, Ollama parallelism) is not managed by the project.
- Ollama is the only local backend in use. The `local_openai` backend exists but is tested with doubles only.
- The optional Claude verification (`verify/haiku.py`) is disabled.

## Roadmap

V1 was deliberately limited. Status of V2 and the next steps:

- **V2**
  - [x] Swappable agents (`agents.py`: roles, registry, routing with fallback, `gemini_test` entry). Validated with unit tests and doubles only.
  - [x] Simpler interface driven by Qwen: free-language mode. Validated with unit tests and doubles only.
  - [x] The project used on itself: profile `auto_projet`, run once with real models; see [docs/auto_projet.md](docs/auto_projet.md).
  - [~] Calibration of thresholds from real runs: the values are centralised and `calibrate.py` suggests values from your runs (see [docs/calibration.md](docs/calibration.md)). The real calibration has not been done; it needs many runs.
  - [~] Backends other than Ollama: an abstraction and a local OpenAI-compatible backend exist, tested with doubles only. Platforms other than Windows: the code has no Windows-specific calls, but only Windows is validated.
- **V3 (planned):** tighten the personal-data detector.

## Legal note on profiles

Analyzing a binary is only allowed if you own it or have the right to study it. No profile is provided to decompile commercial software or to bypass a protection (DRM, anti-cheat).

## License

MIT, see [LICENSE](LICENSE).

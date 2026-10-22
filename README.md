# AdaptiveWorkflow

> **Version 3.2.0.** Tested on Windows only (V2). The logic is covered by unit tests that need no network and no real model. V3 (roles on Claude Haiku, a single local Qwen) has **not** been run with real models yet. Read the [Roadmap](#roadmap) before relying on it.

**A multi-agent workflow for the terminal.** AI agents (Claude Haiku, Gemini, a local Qwen through Ollama) work together under plain Python code that makes the decisions. **Since V3, every role is assigned to Claude Haiku by default** (Claude Code logged in with your claude.ai subscription: it consumes the subscription quota, it is not free), with **a single local Qwen model** as fallback. Every role can still be reassigned (`AGENT_<ROLE>`, or the `agents` key of a profile) to free Gemini or the local Qwen to get back to zero cost.

The core provides the machinery (job queue and budgets, organizer and workers, output checks, review and voting, escalation, personal-data masking, logs). Each use case is a *profile* in `profiles/`. The interface text and the prompts are in French; this page is in English, and the French version is [README.fr.md](README.fr.md).

**Contents:** [Scope](#scope) · [Quick start](#quick-start) · [Tested configuration](#tested-configuration) · [Detailed installation](#detailed-installation) · [Usage](#usage) · [Free-language mode](#free-language-mode) · [Commands](#commands) · [Swappable agents](#swappable-agents) · [Profiles](#profiles) · [Gemini quotas](#gemini-quotas) · [Claude mode (optional)](#claude-mode-optional) · [Workflow escalation](#workflow-escalation) · [Personal data and GDPR](#personal-data-and-gdpr) · [Calibration](#calibration) · [Architecture](#architecture) · [Tests](#tests) · [Known limits](#known-limits) · [Roadmap](#roadmap) · [Legal note on profiles](#legal-note-on-profiles) · [License](#license)

## Scope

The project takes a project description (`/fiche`), asks an organizer to split a task between workers (`/tache`), groups duplicate ideas (`/dedup`), has three reviewers score them (`/revue`) and records your decisions (`/valider`, `/refuser`). For an idea you validated and reviewers approved, a large model can study it (`/escalader`), after a frozen contract and your confirmation.

Principles:

- **The organizer proposes, the code decides.** Closed list of workers, delegation budget, maximum depth, invalid plans rejected as a whole, every output verified.
- **No groupthink.** Each worker and each reviewer works without seeing the others' answers.
- **The code computes** votes, scores and shortlists. Models only propose.
- **No free-text request leaves for an external model without your consent**: confirmation of each send, or one session consent for one provider (see [Free-language mode](#free-language-mode)). Every external send is masked and checked for leaks.
- **Personal data stays local.** A personal-data profile (or personal data detected in your message) drops every external agent; each role is then handled by the local Qwen.
- **One rate-limited queue** for every Gemini call.

What the project does **not** do:

- It does not run code, change files or act on accounts. Models produce text only; Claude is called with no tools.
- It has no web or graphical interface. It is a terminal application.
- It never sends personal data to an external provider: the free Gemini tier and a Claude subscription are excluded for customer data; such data is only handled by the local Qwen (see [Personal data and GDPR](#personal-data-and-gdpr)).
- Its thresholds are not calibrated. The default values are provisional.

## Quick start

Windows only, validated:

1. Download the archive `AdaptiveWorkflow-vX.Y.Z.zip` from the **Releases** page of the GitHub repository, and unzip it into an empty folder.
2. Double-click **`install.bat`**. It checks Python 3.11 or newer, installs `tzdata`, checks Claude Code and Ollama and offers to download the local model `qwen3.5:9b-q4_K_M` (about 6.6 GB). It creates `.env` from `.env.example`, without any key.
3. Install **Claude Code** and log in with your claude.ai account: `claude auth login`. It is the default agent of every role.
4. Optional: set `GEMINI_API_KEY=` in `.env` if you assign a role to Gemini (free key in [Google AI Studio](https://aistudio.google.com)). Without a key, Gemini agents are simply unavailable.
5. Double-click **`start.bat`** to open the terminal. Type `/aide` for the command list.

Optional: `tests.bat` runs the test suite; the result is written to `logs\tests.txt`.

## Tested configuration

| | |
|---|---|
| OS | Windows, Python 3.12 |
| CPU | AMD Ryzen 7 7800X3D |
| GPU | AMD Radeon RX 7800 XT, 16 GB (Ollama at 100 % GPU) |
| RAM | 32 GB |
| Local models | V2: `qwen3.5:4b-q8_0` (5.2 GB), `qwen2.5:3b-instruct-q4_K_M` (1.9 GB). V3: one model, `qwen3.5:9b-q4_K_M` (6.6 GB), not yet measured on this machine |
| API | Gemini `gemini-3.5-flash-lite`, free tier; Claude Code (claude.ai subscription) |

The core uses only the Python standard library (3.11+). `tzdata` is recommended. Other platforms may work, but only Windows has been tested. The encrypted masking vault uses Windows DPAPI; elsewhere it needs the optional `cryptography` package.

## Detailed installation

Manual alternative to `install.bat`:

1. **Python 3.11 or newer** (tested with 3.12). Then `pip install -r requirements.txt` (installs `tzdata`).
2. **Claude Code**, logged in with your subscription: `claude auth login` (claude.ai account). Default agent of every role.
3. **Ollama** ([ollama.com](https://ollama.com)), then the single local model (fallback, personal-data profiles, masking):
   ```
   ollama pull qwen3.5:9b-q4_K_M
   ```
   After a first call, `ollama ps` should show `100% GPU` in the PROCESSOR column. Another model: `QWEN_MODEL` in `.env`.
4. **Gemini key (optional).** Copy `.env.example` to `.env` (exact name, at the project root) and set `GEMINI_API_KEY` if a role is assigned to Gemini.
5. **Checks:**
   ```
   python -m gemini_client      # one real Gemini request (counted in the quota): answers "OK"
   python -m local_llm          # the local model answers
   python -m privacy selftest   # encrypted masking vault
   python -m unittest discover -s tests -t .
   ```
6. **Run:** `python -m qwen_ui` (or `start.bat`).

Optional, for better detection of French names: `pip install spacy` then `python -m spacy download fr_core_news_md`.

### About `.env` and secrets

Your API key lives **only** in your local `.env` file. It is listed in `.gitignore` and must never be committed. The repository ships only `.env.example`, with an empty key. Local state (`state/`: quota counters, masking vault, decisions) and logs (`logs/`) are git-ignored too. No key is written anywhere in the code, and the key is sent in a request header, never in a URL.

## Usage

Start the terminal (`python -m qwen_ui` or `start.bat`). At startup the program checks Claude Code, Ollama and the Gemini key, and only **warns** when one is missing: an unavailable agent is skipped in favour of its fallback (no Claude: everything goes to the local Qwen; no Qwen: no local fallback and no personal-data profile). On exit, the local model is unloaded from VRAM.

By default you **talk to the assistant normally, without any command** (conversation mode, below). The `/…` commands still work.

### Conversation mode (default)

You write as you would to a person (in French); the assistant understands the intent, remembers the last exchanges and asks before any costly work.

- **One call per message.** The `routage` role agent (Claude Haiku by default, local Qwen as fallback) gets the message, the recent history (12 turns, in memory only, never written to disk) and a code-built summary of the work state (sheet, ideas, scores, decisions). It proposes **one** whitelisted action (reply or ask a question, describe the project, ask for ideas, deduplicate, review, give feedback, show, summarise, escalate, quota, privacy); the code checks it (existing numbers, arguments).
- **Natural confirmations.** "oui", "ok", "vas-y", "non", "laisse tomber"… Any other answer is taken as a correction ("plutôt 15 idées"): the step is dropped and the answer handled as a new message that sees the previous proposal (at most two chained corrections). The exact instruction sent to the agents is always shown before "On y va ?".
- **Session consent.** If the conversation agent is external (Claude Haiku by default) and available, the question is asked **once**, on the first message. "non": the local Qwen understands you, without a question per message; "garde tout en local" says it later, "tu peux utiliser Claude" asks again. No Claude Code: nothing is asked, Qwen answers.
- **Personal data.** Same rule as below: code-side detection, restrictive profile, everything local. Leaving that profile (`/profil <name>`) clears the conversation history.
- **No command shown.** System messages that quoted a command are rewritten as phrases to say. Typed commands (`/aide`, `/quota`…) work and display as before.
- "aide" and "au revoir" (and variants) are recognised by the code, without a model call. `MODE_CONVERSATION=0` restores the free-language mode below.

Validated with unit tests and doubles (including a full scenario without any command); not yet with real models: run `pilot.bat`, whose default scenario is now a conversation (`pilot.bat --commandes` for the old path).

### Working on a folder of your PC

The workflow can open a folder of your computer, understand it, answer questions about it and work on it when you ask. **Nothing is ever written to the folder without your explicit "oui"** to the question asked by the program.

```
> regarde le dossier MonAppli sur mon bureau
Je regarde le dossier C:\Users\you\Desktop\MonAppli — lecture seule, rien n'y est modifié.
… D'accord pour l'envoyer ? (oui/non) oui
Voici ce que je comprends du dossier : …
> corrige les fautes du README
… On y va ? (oui/non) oui
Modifications proposées (rien n'est encore écrit) : • README.md (+3 / -3 ligne(s)) … (diff)
J'écris ces 1 modification(s) dans C:\…\MonAppli ? … (oui/non) oui
> finalement annule
```

- **Choosing the folder**: full path, path relative to the home, Desktop or Documents folder ("Bureau/MonAppli"), a plain name (searched near the home folder; the program asks which one if several match), or "choisis un dossier" / `/dossier choisir`, which opens the Windows folder picker. Drive roots, the whole home folder and system folders are refused.
- **Read-only, by code.** The inventory (types, sizes, project kind) is made by code. Technical folders (`.git`, `node_modules`…) and simple `.gitignore` patterns are skipped, symbolic links are never followed, and **sensitive files** (`.env`, private keys, credentials, databases…) are listed but **never read nor sent**. Text is extracted from Word, PowerPoint, Excel and OpenDocument files (PDF with `pypdf` if installed); they are never modified. The program's own `state/` and `logs/` folders are excluded, even when you open the project itself.
- **Dedicated consent.** The `dossier` role (Claude Haiku by default, local Qwen fallback) receives the file list and the content of the main files, up to `DOSSIER_BUDGET_CARACTERES` per request, masked and leak-checked. Before the first send, the program asks for your consent **for this folder** (the conversation consent does not cover it) and reports detected personal data. Refused, personal-data profile, `AGENT_DOSSIER=qwen` or external agent unavailable: everything is processed on the PC, **conversation included**, until the folder is closed. The conversation history only gets the folder name and file names, never their content.
- **Changes checked by code.** The model proposes; the code only accepts paths inside the folder, never sensitive or technical ones, modifies an existing file only if it was read for this request ("remplacer": excerpt present exactly once; "reecrire": file shown in full), refuses deletions, renames, binaries and documents, and shows the diff. The full diff and proposed files are also kept in `state/dossiers/`.
- **Writing.** Only after your "oui": each file must be identical to what was read (otherwise nothing is written), originals are backed up in `state/dossiers/<folder>/sauvegardes/`, writes are atomic with the same encoding and line endings (CRLF included), and a failure halfway restores the folder. "annule" (`/annuler`) restores the last change, without overwriting a file edited since.

Equivalent commands: `/dossier <path>`, `/dossier choisir`, `/dossier` (show), `/dossier relire`, `/dossier fermer`, `/question <question>`, `/travail <instruction>`, `/diff`, `/appliquer`, `/annuler`. Details: [docs/dossier.md](docs/dossier.md). Validated by unit tests with doubles (including a byte-for-byte check that the folder stays unchanged); not yet with real models.

### Free-language mode (`MODE_CONVERSATION=0`)

Previous mode, kept: without a leading `/`, the message is rephrased and a command is proposed.

1. **Rephrasing and session consent.** A `reformulation` agent (Claude Haiku by default; `AGENT_REFORMULATION=qwen` keeps everything local) rewrites your message into a clear request. On the first message, the program asks **once** for a session consent for that provider: it covers rephrasing **and the other free-text roles** assigned to it (routing, framing, summaries, deduplication, repair, promise check), without a confirmation for each send. Refusing, or `/accord non`, keeps rephrasing local and makes every external send of the other roles ask for confirmation (y/n, with a masked preview). Every external send is masked and checked for leaks first, consent or not.
2. **Routing.** The routing agent proposes one action from a whitelist (`tache`, `fiche`, `dedup`, `revue`, `retour`, `detail`, `resume`, `organisateur`, `quota`, `workers`, `aide`, `discussion`). The code validates it. Costly or state-changing actions (`tache`, `revue`, `fiche`, `organisateur`) are shown and need your `o`.
3. **Implicit profile.** You can name a profile ("passe en auto_projet"), or Qwen proposes one from the existing list. The code checks the name. A personal-data profile is loaded immediately, before any send.
4. **Personal data.** The code (not the model) detects e-mail addresses, IBANs, French social-security numbers, phone numbers, birth dates, SIRET (and announced SIREN) numbers, postal addresses, postal codes followed by a town, values declared by the profile and person names (title + name, "Firstname LASTNAME"). If it finds one, it switches to the restrictive profile (`PROFIL_DONNEES_PERSO`, default `bureautique_fictive`), revokes the session consent and handles everything with the local Qwen for the session. Only an explicit `/profil <name>` lifts this lock.
5. **Feedback.** You can answer results in plain words ("keep 2, 1 is too generic"). Qwen interprets it, the code checks the numbers, and you confirm. A reason is asked once and is optional. Decisions go through the same mechanism as `/valider` and `/refuser`. Each decision is stored in `state/retours.jsonl` (masked, local). The last few decisions (`FEEDBACK_EXEMPLES`, default 5) are given to Qwen as examples, never in a personal-data profile. Feedback applies only to the last review.

Not validated with real models; covered by unit tests with doubles.

### Commands

Calls with the V3 defaults (every role on Claude Haiku); a role reassigned to Gemini uses Gemini requests instead, and a local role (or a personal-data profile) calls nothing external.

| Command | Purpose | External calls (V3 default) |
|---|---|---|
| `/aide` | show the help | 0 |
| `/fiche <description>` | the framing agent drafts the project sheet, you validate it (`/fiche` alone shows it, `/fiche effacer` deletes it) | 1 |
| `/tache <instruction>` | the organizer splits the task and delegates to workers; at most 9 calls by default (2 plans, 6 delegations, 1 synthesis) | up to 9 |
| `/detail <n>` | full output of one worker from the last run | 0 |
| `/dedup` | groups duplicate ideas (exact duplicates, then proposals of the deduplication agent checked by code) | 1 |
| `/revue` | 3 reviewers score the ideas (one call each); 2 of 3 must approve; shortlist | 3 |
| `/valider <n> [...]` | validates ideas of the last review | 0 |
| `/refuser <n> [...] [reason]` | refuses ideas; the refusal is sent to the organizer at the next `/tache` (unless it contains personal data) | 0 |
| `/escalader <n> [force]` | a large model studies a validated, approved idea, after a frozen contract and confirmation; `force` bypasses the complexity threshold | 1, plus 1 complexity estimate and 1 promise check |
| `/organisateur agents\|gemini\|claude` | choose the organizer: `agents` (default, `organisateur` role), or a direct client (`claude` is checked and needs confirmation) | 0 |
| `/grand agents\|gemini\|claude` | choose the big model (same logic) | 0 |
| `/profil [name]` | show, or force, the profile (optional, the code chooses it) | 0 |
| `/accord [oui\|non]` | show, grant or revoke the session consent (rephrasing and free-text roles, one provider) | 0 |
| `/masquer <text>` | local masking preview and leak check; nothing is sent | 0 |
| `/donnees` | the fictional data of the profile, minimised and masked | 0 |
| `/resume` | summary of the last result by the agent of the `interface` role | 1 |
| `/workers` | list the closed list of workers | 0 |
| `/quota` | Gemini requests used today and remaining, Claude calls of the day | 0 |
| `/quitter` | quit | 0 |
| `/dossier <path>` | opens a folder of your PC (read-only), inventories it and has it analysed; `/dossier choisir` opens the folder picker, `/dossier` shows it, `/dossier relire`, `/dossier fermer` | 1 (after the folder's dedicated consent) |
| `/question <question>` | answers a question about the open folder | 1 or 2 |
| `/travail <instruction>` | proposes changes in the open folder, shows the diff and asks before writing | 1 or 2 |
| `/diff` | full diff of the proposed changes | 0 |
| `/appliquer` | writes the proposed changes, after confirmation (originals backed up) | 0 |
| `/annuler` | restores the files as before the last written change, after confirmation | 0 |

Aliases: `/task`, `/tâche`, `/help`, `/org`, `/review`, `/doublons`, `/détail`, `/résumé`, `/q`, `/exit`, `/quit`.

Without a leading `/`, the commands above can also be requested in free language (see above). `/escalader`, `/grand`, `/masquer`, `/profil`, `/valider`, `/refuser`, `/accord`, `/donnees` and `/quitter` are only available as explicit commands (decisions can still be given in plain language after a review).

## Swappable agents

Each role of the workflow is assigned to an agent, by default, by `.env` or by a profile, without changing the code (`agents.py`). The roles are `organisateur`, `worker`, `relecteur`, `grand_modele`, `interface`, `cadrage`, `routage`, `promesse`, `format`, `dedup`, `reparation`, `reformulation`, `dossier` (V3.2: work on a folder of your PC, dedicated consent).

**V3 defaults:** every role → `claude_haiku` (Claude Code, model `CLAUDE_HAIKU_MODEL`, alias `haiku` by default), then fallback to `qwen` (the single local model). To change it:

```
AGENT_REFORMULATION=qwen                  # rephrasing stays local (no external send)
AGENT_WORKER=gemini,qwen                  # workers on free Gemini, local fallback
AGENT_DEDUP=qwen                          # local deduplication
BIG_MODEL_PROVIDER=claude                 # escalation on the "claude" agent (CLAUDE_MODEL, sonnet by default)
```

Agents: `claude_haiku`, `qwen` (local; `qwen_interface` and `qwen_format`, the V2 names, are aliases of the same model), `gemini`, `gemini_test` (its model comes only from `GEMINI_TEST_MODEL`, empty means unavailable), `claude`, and agents declared in `AGENT_DECLARATIONS` or in a profile (providers `ollama`, `local_openai` on a loopback address, `gemini`, `claude`). Any role sent to Gemini or Claude is an external send: it is masked and checked for leaks; for a free-text role it also needs your confirmation or the session consent. An unavailable agent (Claude Code missing or logged out, no Gemini key) is skipped without sending anything. In a personal-data profile, external agents are dropped, fallbacks included, and the role is handled by the local Qwen. Every Gemini call, whatever the agent, uses the same queue and quota.

Verified with unit tests and test doubles only. Comparisons of agents with real models have not been run. Details: [docs/agents.md](docs/agents.md) and [docs/interchangeabilite.md](docs/interchangeabilite.md).

## Profiles

A profile is a folder `profiles/<name>/` containing `profil.json` (workers, instructions, output schemas, review thresholds, fields) and optionally `donnees.json` (**fictional** example data). The code validates the profile when it loads it. A new use case is added without modifying the skeleton.

| Profile | Purpose | Personal data | Workers |
|---|---|---|---|
| `brainstorm` (default) | ideas and analyses | no | `generateur`, `analyste` |
| `bureautique_fictive` | back-office example (reminders, letters) on **fictional** data; shows minimisation, masking and fully local processing (Qwen) | yes | `redacteur` |
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

All Gemini requests (whatever role is assigned to Gemini) go through **one single queue** and share this budget. The counter is written to disk before each call and survives restarts, so a request that was sent is counted even if the program crashes. `/quota` shows the count.

**Exact limits vary by model and over time**: check them for your key in AI Studio ([rate limits page](https://aistudio.google.com/rate-limit)).

## Claude mode (optional)

Since V3, Claude Haiku (agent `claude_haiku`) is the default agent of every role; the `claude` agent (`CLAUDE_MODEL`, `sonnet` by default) is still available for the organizer or the big model. Both go through **Claude Code** (`claude -p`), logged in with **your claude.ai subscription**. They **consume your subscription quota**; it is never free, and the program never presents it as free. A `/tache` + `/dedup` + `/revue` run is about fifteen calls.

- Install Claude Code and log in: `claude auth login` (claude.ai account).
- The program checks the login (`claude auth status`). An API-key login is **refused**, because it would bill the API, unless you explicitly allow it with `CLAUDE_ALLOW_API_BILLING=1`. The `ANTHROPIC_API_KEY` variable is removed from the environment of the call.
- Claude is called with no tools (no files, no commands), from an empty temporary directory. Calls are counted per day in `state/claude_calls.json` (`/quota`).
- Claude Code missing or logged out: a warning is shown at startup and roles go straight to their fallback (local Qwen), with no useless confirmation request.

## Workflow escalation

Escalation is a product feature: it sends an ambitious idea to a large model. It is not related to the development of the project.

An idea is a candidate if you validated it, 2 of 3 reviewers approved it, and its complexity score exceeds `COMPLEXITY_THRESHOLD` (default 5). The agent of the `interface` role estimates the components (files touched, new dependencies, regression risk); the code computes the score. Then:

1. the code builds a **contract** (promise, scope, out-of-scope, non-negotiable constraints) that you validate and that is then frozen;
2. cost and duration are displayed; **nothing is called without your confirmation**;
3. the large model may reduce the scope, change the approach or stage the work, but may not change the promise, lift a constraint or add out-of-scope items;
4. the code checks the answer (JSON, schema, constraints, scope), then the agent of the `promesse` role answers a closed question ("is the promise identical?"). At the slightest doubt the version is rejected, and a new attempt restarts from the original contract.

The report and the result are written to `logs/escalation-<id>.json` and `state/escalations/<id>.json`.

## Personal data and GDPR

The `privacy/` module masks data before anything is sent out: field minimisation, deterministic detection (e-mail addresses, phone numbers, IBANs, French social-security numbers, SIRET/SIREN, postal addresses, postal codes, birth dates, names), indirect clues proposed by Qwen and kept only if they can be verified in the text, a mandatory preview, a **blocking leak check**, a local encrypted vault (DPAPI on Windows) with stable tokens, and local re-injection of the real values by the code.

The detector that switches a session to the restrictive profile is **tightened in V3**: on top of e-mail addresses, IBANs, social-security numbers, phone numbers and birth dates, it is triggered by SIRET numbers (and SIREN numbers preceded by "SIREN", "SIRET" or "RCS"), postal addresses ("12 rue …"), postal codes followed by a town ("75011 Paris"), values declared by the profile and person names (title + name, "Firstname LASTNAME"). Two filters limit false positives: a postal code alone ("budget 15000 euros") does not count, and a common upper-case acronym ("Android TV", "API REST", "CRM") is not taken for a name. A name without title or capitals ("relance dupont") only triggers if the profile declares it. Everything detected is masked before any external send anyway.

**Limits you should know about:**

- Pseudonymized data is still **personal data** (GDPR, recital 26). Masking reduces risk; it does not take you out of the GDPR.
- **Free-tier Gemini and a Claude subscription are excluded for customer data** (no data-processing agreement). A personal-data profile therefore drops every external provider: each role is handled by the local Qwen (V3). Real use requires a paid offer with a processing agreement and probably a data protection impact assessment (DPIA).
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
local_llm.py         local model behind one interface (Ollama; a single Qwen model in V3)
claude_cli.py        Claude through Claude Code, on a subscription
agents.py            swappable agents: roles (V3 default: claude_haiku, qwen fallback), registry, routing, fallback, privacy
orchestrator.py      bus: validated plan, isolated delegations, verification, synthesis, log
organizers/          Gemini and Claude organizers
workers/             worker contract, deduplication, default workers
review.py            reviewers, 2-of-3 vote, shortlist
escalation.py        contract, big model, checks
verify/              JSON schema, repair, promise check; haiku.py (verification by Claude Haiku, disabled)
privacy/             detection, masking, leak check, encrypted vault, provider policy
profiles/            one folder per use case, loader.py validates them
qwen_ui/             terminal: app.py, conversation.py, dossier_app.py, router.py, langage_libre.py, fiche.py, presenter.py, bootstrap.py
dossier/             folder of your PC: read-only inventory, analysis, checked changes, apply and undo
pilot.py             replays a scenario without input and measures it
calibrate.py         threshold suggestions from your own runs (read-only)
tests/               21 test modules, no network and no real model required (guard in tests/__init__.py)
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

Or `tests.bat`, which writes the output to `logs\tests.txt`. The suite has about 575 test functions (`def test_`) in 21 modules. It needs no network and no real model: Ollama, Gemini and Claude are replaced by test doubles, and `tests/__init__.py` prevents any real call to Claude Code or Ollama even if a test forgot a double. Passing tests show the logic with doubles; they do not measure real model behaviour.

## Known limits

- Only Windows is validated.
- Swappable agents and free-language mode are validated by unit tests with doubles. The only real-model run is the V2 one documented in [docs/auto_projet.md](docs/auto_projet.md); it is anecdotal, not a benchmark. The V3 defaults (Claude Haiku everywhere, `qwen3.5:9b-q4_K_M`) have not run for real yet: Haiku's JSON quality, the duration of each `claude -p` call and the quota use are still to be measured.
- Folder work has not run with real models yet: the quality of Claude Haiku's proposals ("remplacer" excerpts copied exactly) and how much of the local Qwen context a large folder takes (`DOSSIER_BUDGET_CARACTERES`) are still to be observed. Only the root `.gitignore` is read, without "!" negations.
- Thresholds are not calibrated.
- The personal-data detector is tightened but heuristic (see above): false positives are possible (a brand name written "Word UPPERCASE" switches to local), and so are false negatives (an undeclared lower-case name).
- Every external send first runs, locally, Qwen's indirect-clue detection (masking); without Ollama this step is skipped with a warning.
- Free-tier Gemini and Claude are excluded for personal data; in practice only local processing is possible for such data.
- The Gemini rate limiter assumes a single process using the state file at a time. Concurrency of local models (VRAM, Ollama parallelism) is not managed by the project.
- Ollama is the only local backend in use. The `local_openai` backend exists but is tested with doubles only.
- The optional Claude verification (`verify/haiku.py`) is disabled.

## Roadmap

V1 was deliberately limited. Status of the following versions:

- **V2**
  - [x] Swappable agents (`agents.py`: roles, registry, routing with fallback, `gemini_test` entry). Validated with unit tests and doubles only.
  - [x] Simpler interface driven by Qwen: free-language mode. Validated with unit tests and doubles only.
  - [x] The project used on itself: profile `auto_projet`, run once with real models; see [docs/auto_projet.md](docs/auto_projet.md).
  - [~] Calibration of thresholds from real runs: the values are centralised and `calibrate.py` suggests values from your runs (see [docs/calibration.md](docs/calibration.md)). The real calibration has not been done; it needs many runs.
  - [~] Backends other than Ollama: an abstraction and a local OpenAI-compatible backend exist, tested with doubles only. Platforms other than Windows: the code has no Windows-specific calls, but only Windows is validated.
- **V3**
  - [x] Every role on Claude Haiku by default (agent `claude_haiku`), local Qwen fallback, every role still reassignable. Unit tests and doubles only.
  - [x] A single local Qwen model (`QWEN_MODEL`, `qwen3.5:9b-q4_K_M` by default).
  - [x] Personal-data profiles: automatic local Qwen fallback instead of a refusal.
  - [x] Tightened personal-data detector.
  - [x] Gemini key and Ollama optional at startup.
  - [x] Real V3 run (3.1.0): conversation scenario run end to end on Windows with Claude Haiku.
  - [x] Conversation mode: talk to the assistant without any command (3.1.0). Unit tests and doubles, one real run.
  - [x] Work on a folder of your PC: choice, read-only analysis, questions, changes written only after "oui", backup and undo (3.2.0). Unit tests and doubles only.

## Legal note on profiles

Analyzing a binary is only allowed if you own it or have the right to study it. No profile is provided to decompile commercial software or to bypass a protection (DRM, anti-cheat).

## License

MIT, see [LICENSE](LICENSE).

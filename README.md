# AdaptiveWorkflow

> **Version 1 (V1).** This is the first public release. It works and is tested (about 290 unit tests, no network or real model needed), but it is an early version: expect rough edges, and expect thresholds and behavior to be tuned in later versions. See the [Roadmap](#roadmap).

**A zero-cost multi-agent orchestration skeleton.** Local models (Qwen through Ollama) and the free Gemini API work together, under the control of plain Python code that makes the decisions. Each user plugs in their own Gemini key and their own local models.

The skeleton provides the machinery — job queue and budgets, organizer and workers, output verification, review and voting, escalation, personal-data masking, logging — and **contains no business logic**: every use case is a *profile* (`profiles/`).

> The interface text, prompts and some internal identifiers are in French (the author's language). The documentation below is in English; a French version is available in [README.fr.md](README.fr.md).

## Why

Most "multi-agent" setups let one model decide everything and hope for the best. AdaptiveWorkflow takes the opposite approach:

- **The organizer proposes, the code decides.** Closed list of workers, delegation budget, maximum depth, invalid plans rejected as a whole, every output verified.
- **No groupthink.** Each worker and each reviewer works without seeing the others' answers.
- **The code computes** votes, scores and shortlists. Models only propose.
- **Nothing leaves for an external model without your confirmation** when the request comes from free text.
- **Cheap by construction.** A single rate-limited queue, conservative daily quotas, and local models for everything that does not need a large model.

## How it works

```
You ──> Qwen 3.5 4B (local): interface, framing, routing, promise check
              │  command confirmed by you
              ▼
         Orchestration bus (Python code): decides, verifies, logs
              │  single queue, 12 requests/min, hard stop at 1,400/day
              ▼
         Gemini Flash-Lite (free): organizer, workers, 3 reviewers
              │
         Qwen 2.5 3B (local): deduplication, repair of invalid JSON
```

A typical brainstorm run (project sheet → generation → deduplication → review) takes about 50 seconds and 6–7 Gemini requests.

## Tested configuration

| | |
|---|---|
| OS | Windows, Python 3.12 |
| CPU | AMD Ryzen 7 7800X3D |
| GPU | AMD Radeon RX 7800 XT, 16 GB (Ollama at 100 % GPU) |
| RAM | 32 GB |
| Local models | `qwen3.5:4b-q8_0` (5.2 GB), `qwen2.5:3b-instruct-q4_K_M` (1.9 GB) |
| API | Gemini `gemini-3.5-flash-lite`, free tier |

The core is pure Python standard library (3.11+), so other platforms should work, but only Windows has been tested in V1 (the encrypted privacy vault uses Windows DPAPI; on other systems it falls back to the optional `cryptography` package).

## Installation

1. **Python 3.11 or newer.** No mandatory dependency. Recommended: `pip install -r requirements.txt`.
2. **Ollama** ([ollama.com](https://ollama.com)), then:
   ```
   ollama pull qwen3.5:4b-q8_0
   ollama pull qwen2.5:3b-instruct-q4_K_M
   ```
   Check that the models run on the GPU: `ollama ps` should show `100% GPU`.
3. **Gemini key.** Get one from [Google AI Studio](https://aistudio.google.com) (API keys). Copy `.env.example` to `.env` (exact name, at the project root) and fill in `GEMINI_API_KEY`. The program refuses to start without a key.
4. **Checks:**
   ```
   python -m unittest discover -s tests -t .
   python -m gemini_client      # 1 Gemini request: should answer "OK"
   python -m local_llm          # both local models should answer
   python -m privacy selftest   # encrypted masking vault
   ```
5. **Run:** `python -m qwen_ui`

### About `.env` and secrets

Your API key lives **only** in your local `.env` file. It is listed in `.gitignore` and must never be committed. The repository ships only `.env.example`, with an empty key. Local state (`state/`: quota counters, masking vault) and logs (`logs/`) are git-ignored too. No key is written anywhere in the code.

## Terminal commands

| Command | Purpose | Gemini requests |
|---|---|---|
| `/fiche <description>` | Qwen drafts the project sheet, you validate it | 0 |
| `/tache <instruction>` | the organizer splits the task and delegates to workers | 3–9 |
| `/dedup` | groups duplicate ideas (Qwen 2.5 3B) | 0 |
| `/revue` | 3 reviewers score the ideas; 2/3 vote; shortlist | 3 |
| `/valider <n>`, `/refuser <n> [reason]` | your decisions; refusals are fed back to the organizer | 0 |
| `/escalader <n>` | study by the large model, after a frozen contract and confirmation | 1 (Gemini) or 1 Claude call |
| `/grand gemini\|claude`, `/organisateur gemini\|claude` | choose the provider | 0 |
| `/profil [name]`, `/masquer <text>`, `/donnees` | profiles and local masking | 0 |
| `/detail <n>`, `/resume`, `/workers`, `/quota`, `/aide`, `/quitter` | inspection | 0 |

Without a leading `/`, Qwen analyzes your message and proposes a command, which you confirm (y/n). [`GUIDE_PROMPTS_QWEN_V1.md`](GUIDE_PROMPTS_QWEN_V1.md) explains how to write good project sheets and instructions (in French).

## Gemini quotas (free tier)

The rate limiter applies conservative defaults, configurable in `.env`:

| Rule | Value |
|---|---|
| Spacing between two requests | 5 s (12 per minute) |
| Warning | 1,200 requests per day |
| Hard stop | 1,400 requests per day |
| Retries on HTTP 429 | 2, each one counted; the delay requested by Google is respected |
| Reset | midnight, Pacific time |

All Gemini requests (organizer, workers, reviewers, escalation) go through **one single queue** and share this budget. The counter is persisted to disk and survives restarts.

**Exact limits vary by model and over time**: check them for your key in AI Studio ([rate limits page](https://aistudio.google.com/rate-limit)).

## Escalation to a large model

An idea is a candidate if you validated it, 2 of 3 reviewers approved it, and its complexity score (computed by the code from Qwen's estimate: files touched, dependencies, risk) exceeds the threshold. Then:

1. the code builds a **contract** (promise, scope, out-of-scope, non-negotiable constraints) that you validate and that is then frozen;
2. cost and duration are displayed; **nothing is called without your confirmation**;
3. the large model may reduce the scope, change the approach or stage the work, but may not change the promise, lift a constraint or add out-of-scope items;
4. the code checks the diff, Qwen answers a closed question ("is the promise identical?"); at the slightest doubt the version is rejected and a new attempt restarts from the original contract.

### Optional Claude mode (uses your subscription)

Claude mode goes through **Claude Code** (`claude -p`), logged in with **your claude.ai subscription**. It **consumes your subscription quota**; it is never free and the program never presents it as such.

- Install Claude Code and log in: `claude auth login` (claude.ai account).
- An API-key login is **refused** (it would bill the API) unless you explicitly allow it with `CLAUDE_ALLOW_API_BILLING=1`.
- Claude is called with no tools at all (no files, no commands), in an empty temporary directory.

## Personal data and GDPR

The `privacy/` module masks data before anything is sent out: field minimization, deterministic detection (emails, phone numbers, IBAN, French social-security number, SIRET/SIREN, addresses, postal codes, birth dates, names), indirect clues proposed by Qwen and kept only if verifiable, mandatory preview, **blocking leak check**, local encrypted vault (DPAPI on Windows) with stable tokens, and local re-injection of the real values.

**Limits you should know about:**

- Pseudonymized data is still **personal data** (GDPR, recital 26). Masking reduces risk; it does not take you out of the GDPR.
- **Free-tier Gemini and a Claude subscription are excluded for customer data** (no data-processing agreement). A "personal data" profile therefore refuses every external provider in V1; only local masking is available. Real use requires a paid offer with a processing agreement and probably a DPIA.
- Automatic masking is not perfect: a combination of details can remain identifying, and a model can copy or invent a value. Without spaCy (`fr_core_news_md`), an isolated name without title or capitalization is only caught if it is in the declared fields. For highly sensitive data, only fully local processing is safe.
- **Test with fictional data only.**

## Profiles

A profile is a folder `profiles/<name>/` containing `profil.json` (workers, instructions, output schemas, thresholds, fields) and optionally `donnees.json` (**fictional** data). The code validates the profile at load time.

- `brainstorm` — default profile, ideas and analyses.
- `bureautique_fictive` — a "personal data" example on fictional data; demonstrates minimization, masking and the refusal of external providers.

A new use case is added **without modifying the skeleton** (a test enforces this).

## Pilot and measurements

`python pilot.py [scenario.txt]` replays a scenario without any input (automatic confirmations) and writes a transcript and measurements (durations, requests per command) to `logs/`. On Windows, `pilot.bat` runs it with a double-click. Thresholds (shortlist size, 6.5/10 approval, complexity) are meant to be calibrated on your first projects.

## Architecture

```
config.py            quotas, models, providers (conservative defaults)
rate_limiter.py      single queue, spacing, retries, persistent daily counter
gemini_client.py     Gemini API (REST, standard library), always via the limiter
local_llm.py         local models, single interface (Ollama in V1)
claude_cli.py        Claude through Claude Code on a subscription
orchestrator.py      bus: validated plan, isolated delegations, verification, synthesis, log
review.py            reviewers, 2/3 vote, shortlist
escalation.py        contract, large model, diff, checks
organizers/          Gemini and Claude organizers
workers/             worker contract, deduplication, default workers
verify/              JSON schema, repair, promise check, haiku.py (disabled)
privacy/             detection, masking, leak check, encrypted vault, policy
qwen_ui/             terminal, project sheet, routing, presentation
profiles/            one folder per use case
tests/               ~290 tests, no network and no real model required
```

## Roadmap

V1 is deliberately limited. Planned for the next versions:

- **V2:**
  - [x] Freely swap the AI agents (`agents.py`: roles, registry, routing with fallback, `gemini_test` test entry; see `docs/interchangeabilite.md`). Validated by unit tests with test doubles only, not with real models.
  - [ ] A simpler interface driven by Qwen.
  - [~] Using the project on itself: profile `auto_projet` and `docs/auto_projet.md` prepared, never run on real models.
- [~] Calibrating thresholds from real pilot runs: thresholds are centralised and configurable, method in `docs/calibration.md`; no real calibration done yet.
- [~] Backends other than Ollama: an abstraction and a local OpenAI-compatible backend exist, tested with doubles only. Platforms other than Windows: not validated, only Windows is tested.

## Legal note on profiles

Analyzing a binary is only allowed if you own it or have the right to study it. No profile is provided to decompile commercial software or to bypass a protection (DRM, anti-cheat).

## License

MIT — see [LICENSE](LICENSE).

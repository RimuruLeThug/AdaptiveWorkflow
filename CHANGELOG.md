# Changelog

## 3.1.0
- **Conversation mode (default)**: talk to the assistant like to a person, without any command. One call per message to the `routage` role (Claude Haiku by default, local Qwen as fallback) with the in-memory history (12 turns, never written to disk) and a code-built work state; the model proposes one whitelisted action, the code validates it. Natural confirmations ("oui", "vas-y", "non"...); any other answer is a correction handled as a new message (at most two in a row). Session consent asked once, only if the conversation agent is external and available; refused: the local Qwen answers without per-message questions. "garde tout en local" revokes it. Escalation reachable by phrase ("approfondis la 2", with its existing confirmations). System messages no longer quote `/…` commands during a conversation turn; typed commands still work. `MODE_CONVERSATION=0` restores the previous free-language mode. `pilot.py` default scenario is now a conversation (`--commandes` for the old one).
- **Fix (seen in a real run, "envoi bloqué ... (VALEUR_CONNUE)")**: masking now re-masks, with their existing token, values already in the vault (an old indirect hint, a client seen earlier) instead of letting the leak check block every send that contains them; the leak check matches whole words only (a vault value "Ali" no longer blocks "réalisable"). The invariant is unchanged: no vault value is ever sent.
- **Fix (first real conversation run)**: an indirect hint kept during a masking call is now also re-masked where it appears with another case or spacing in the same text; the organiser synthesis was blocked ("VALEUR_CONNUE") and the run ended without a synthesis.
- **Fix (first real conversation run)**: feedback such as "je garde la 1, elle me plaît bien" is recognised by the code when scored ideas exist (decision verb + known idea number), instead of relying on the model, which answered with a question. Few-shot examples added to the conversation prompt.
- The full instruction sent to the agents is shown before "On y va ?" (it was cut at 400 characters). The review header counts calls to the reviewers instead of "Gemini requests" (it said "3 requêtes Gemini" with reviewers on Claude).
- Free-text reasons "o", "oui", "ok", "n" (reflex answers seen in a real run) are treated as "no reason".
- Real run (Windows, Claude Haiku on every role): 527 tests OK; the conversation scenario ran end to end (about 3 to 4 s per understanding call, 48 s for 12 ideas, 58 s for the review).
- Tests: `tests/test_conversation.py` (36 tests, full scenario without any command); about 530 tests.

## 3.0.0
- **Every role now defaults to Claude Haiku** (new built-in agent `claude_haiku`, Claude Code on the user's claude.ai subscription, alias `CLAUDE_HAIKU_MODEL=haiku`), with the local Qwen as fallback. Every role is still reassignable (`AGENT_<ROLE>`, profile `agents` key); `docs/agents.md` gives a zero-cost setup. Claude calls consume the subscription quota; they are not free.
- **One local model**: `QWEN_MODEL` (default `qwen3.5:9b-q4_K_M`, about 6.6 GB) and `QWEN_NUM_CTX` (16384) replace the two V2 models. New agent `qwen`; `qwen_interface` and `qwen_format` stay as aliases of the same model. The V2 variables `QWEN_INTERFACE_MODEL`, `QWEN_FORMAT_MODEL`, `QWEN_*_NUM_CTX` are ignored, with a startup warning. (Qwen 3.8 exists on Ollama only as 27B, 18 GB and up, so the 9B of the Qwen 3.5 family was chosen to stay near 8 GB.)
- **Personal data, automatic local fallback**: in a personal-data profile (or after personal data is detected), external agents are dropped before any send, fallbacks included, and the role is handled by the local Qwen, instead of a refusal. `/tache`, `/revue` and `/escalader` therefore work fully locally in such a session.
- **Tightened personal-data detector** (planned for V3): SIRET (and announced SIREN), postal addresses, postal codes followed by a town, profile-declared values and person names (title + name, "Firstname LASTNAME", common acronyms excluded) now switch to the restrictive profile. A postal code alone and amounts are not triggers.
- **Session consent**: one consent per session for one provider now covers rephrasing and the other free-text roles assigned to that provider (`/accord oui|non`), so free-text roles on Claude do not ask for every send. Masking and the leak check still run before each send; never for the four generation roles nor in a personal-data session.
- **Startup**: the Gemini key and Ollama are optional (warnings only). Claude Code is checked once; if it is missing or logged out, Claude agents are marked unavailable and roles go straight to their fallback, without useless confirmation requests (missing `claude` command detected without starting a process).
- `/organisateur` and `/grand` accept `agents` (default: the role) besides `gemini` and `claude` (direct clients). `BIG_MODEL_PROVIDER` defaults to empty (the `grand_modele` role).
- Messages name the agent that will actually work (no more "Qwen local, 0 requête Gemini" when a role is on Claude); Gemini quota warnings only when a role uses Gemini.
- `install.bat` pulls the single model and checks Claude Code; `liberer_vram.bat` unloads the single model.
- Tests: `tests/__init__.py` blocks any real Claude Code or Ollama call even if a test forgets a double; about 490 tests; 12 deliberate bugs on the V3 safety points, all caught.
- Not done: no real run of V3 yet (Haiku JSON quality, `claude -p` duration per call, quota use, `qwen3.5:9b-q4_K_M` VRAM and speed are to be measured).

## 2.0.1
- Easy install on Windows: `install.bat` (checks Python 3.11+, installs `tzdata`, creates `.env` from `.env.example`, offers to pull the two Qwen models) and `start.bat`; each release ships a ready-to-unzip archive `AdaptiveWorkflow-vX.Y.Z.zip`.
- Documentation rewritten (README EN/FR, `docs/architecture.md`, `docs/troubleshooting.md`, `docs/configuration.md`, `docs/agents.md`), checked against the code.
- `.gitattributes`: `.bat` files are checked out with CRLF line endings.

## 2.0.0
- V2 complete: swappable agents, free-language interface, using the project on itself (profile `auto_projet`, one real run with Gemini and local Qwen, short Claude Haiku test).
- `calibrate.py`: suggests thresholds from your own review logs and decisions (warns on small samples; never writes `.env`).
- Personal-data trigger relaxed on purpose (only e-mail, IBAN, NIR, phone, birth date switch to the restrictive profile; everything is still masked before any external send). To be tightened in V3.
- Fix found in a real run: the saved fiche is no longer given to the free-text router (it leaked an old topic into the proposed command).
- Cleanup: unused imports, stale comments, `brief` references and old "Qwen 5 Go / 2 Go" labels removed.
- Not done: real threshold calibration (needs many runs), validation on other platforms, real tests of other local backends, Gemini-for-Qwen-role comparison with real calls (`GEMINI_TEST_MODEL` not provided).

## 1.2.0
- Free-language mode: `reformulation` role (Gemini by default, swappable; one consent per session; local Qwen fallback), whitelisted routing of free text, implicit profile choice with code-side personal-data detection and session lock, free-language feedback with optional reasons stored in `state/retours.jsonl` and reinjected (masked, bounded) as examples; `/accord` command.
- Personal-data check on feedback reasons; feedback always confirmed; implicit profile switches keep results.
- New variables: `AGENT_REFORMULATION`, `PROFIL_DONNEES_PERSO`, `FEEDBACK_EXEMPLES`. ~445 tests.
- Not validated with real models; the personal-data detector can have false positives.

## 1.1.0
- Swappable agents (`agents.py`): roles, registry, routing with fallback and bus logging, `gemini_test` test entry, local OpenAI-compatible backend (tested with doubles only). Opt-in: defaults unchanged.
- External sends from any Qwen role require user confirmation with a masked preview naming the recipient; personal-data profiles refuse external agents.
- Daily Gemini quota stop propagates through routed agents.
- Profile `auto_projet` (use the project on itself; prepared and unit-tested only, not yet run on real models).
- Docs: configuration, agents, calibration method, interchangeability report (offline only).
- `.gitignore` completed. ~380 unit tests.
- Not done: simpler Qwen-driven interface, real threshold calibration, real-model validation of agent swapping, non-Windows validation.

## 1.0.0
- First public release (V1).

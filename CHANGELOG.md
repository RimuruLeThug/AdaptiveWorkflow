# Changelog

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

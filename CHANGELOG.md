# Changelog

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

# Changelog

## 1.1.0
- Swappable agents (`agents.py`): roles, registry, routing with fallback and bus logging, `gemini_test` test entry, local OpenAI-compatible backend (tested with doubles only). Opt-in: defaults unchanged.
- External sends from any Qwen role require user confirmation with a masked preview naming the recipient; personal-data profiles refuse external agents.
- Daily Gemini quota stop propagates through routed agents.
- Profile `auto_projet` (use the project on itself; never run on real models).
- Docs: configuration, agents, calibration method, interchangeability report (offline only).
- `.gitignore` completed. ~380 unit tests.
- Not done: simpler Qwen-driven interface, real threshold calibration, real-model validation of agent swapping, non-Windows validation.

## 1.0.0
- First public release (V1).

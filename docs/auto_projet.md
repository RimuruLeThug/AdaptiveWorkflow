# Using AdaptiveWorkflow on itself (profile `auto_projet`)

The `auto_projet` profile (`profiles/auto_projet/profil.json`) lets the project be used on its own
code base: improvement ideas and code/documentation review. It declares no personal data
(`donnees_personnelles: false`) and must only receive public project material.

Usage (manual, never run by the automated build): start the terminal UI, then `/profil auto_projet`,
`/fiche ...`, `/tache ...`, `/revue`. This consumes the shared Gemini quota.

## Real run (2026-10-15)
One scripted pilot (`pilot.py`) with Gemini as organizer/reviewers and local Qwen: free-text request, `/fiche`, `/tache`, `/dedup`, `/revue`, free-language feedback; 13 Gemini requests, no exception. One short extra test with Claude Haiku as organizer (2 Claude calls, subscription quota, 1 Gemini request). Observed: an old saved fiche from a previous pilot leaked into the first request (prompt hardened afterwards); the pilot answers "o" to every question, so stored reasons are just "o". Single run: anecdotal, not a benchmark.

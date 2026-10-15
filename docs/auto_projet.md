# Using AdaptiveWorkflow on itself (profile `auto_projet`)

The `auto_projet` profile (`profiles/auto_projet/profil.json`) lets the project be used on its own
code base: improvement ideas and code/documentation review. It declares no personal data
(`donnees_personnelles: false`) and must only receive public project material.

Usage (manual, never run by the automated build): start the terminal UI, then `/profil auto_projet`,
`/fiche ...`, `/tache ...`, `/revue`. This consumes the shared Gemini quota. The profile was validated by the
unit tests only; it has not been run against real models.

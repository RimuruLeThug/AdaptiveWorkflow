# Troubleshooting

Messages in quotes are the French texts the program prints. They are quoted as they appear in the code, so you can search for them in this page.

## The program does not start

### "GEMINI_API_KEY est absente"

The key is missing or empty.

- The file must be named exactly `.env` (not `.env.txt`: Windows hides known extensions, so check with `dir /a` or in the file properties).
- It must be at the project root, next to `config.py`.
- The line must be `GEMINI_API_KEY=` followed by your key, on one line. Spaces around `=` are accepted.

The message says which file it found under another name, if any, and how to rename it.

### "Interface Qwen indisponible : Ollama ne répond pas"

Ollama is not running, or is not at the address the project uses.

- Start the Ollama application, then check with `ollama list`.
- If Ollama listens elsewhere, set `OLLAMA_HOST_URL` in `.env` (default `http://localhost:11434`).

### "Interface Qwen indisponible : Modèle « ... » absent d'Ollama"

The interface model (default `qwen3.5:4b-q8_0`) is not installed under that name.

- Install it: `ollama pull qwen3.5:4b-q8_0`.
- Or, if the message lists an installed variant ("Variante déjà installée : ..."), set `QWEN_INTERFACE_MODEL=<that name>` in `.env`.
- Names must match `ollama list` exactly.

### "Attention : Qwen 2.5 3B indisponible, réparation JSON et déduplication limitées."

This is a warning: the program still starts. The format model (default `qwen2.5:3b-instruct-q4_K_M`) is missing or unreachable.

- Install it: `ollama pull qwen2.5:3b-instruct-q4_K_M`, or set `QWEN_FORMAT_MODEL`.
- Without it, `/dedup` only groups exact duplicates and invalid JSON is repaired only by deterministic rules, not by a model.

### "Profil de démarrage invalide" or "Configuration des agents invalide"

- `PROFILE` names a folder that does not exist or whose `profil.json` is invalid. The program does not start in that case: fix `PROFILE` in `.env`. Valid names are the folders of `profiles/` that contain a `profil.json`.
- `AGENT_<ROLE>` or `AGENT_DECLARATIONS` contains an unknown role, an unknown agent, or an invalid value. The message names the field. See [agents.md](agents.md) and [configuration.md](configuration.md).

## During a session

### "Modèle local indisponible : ..."

A call to Ollama failed (Ollama stopped, model unloaded because of a restart, HTTP error). Check `ollama ps` and restart Ollama if needed; then repeat the command.

### "Qwen a renvoyé une réponse mal formée (...)"

Qwen did not return the expected JSON, even after the repair step. Retry the request, or use an explicit command (for example `/tache <consigne>` instead of free text). Repeated failures on the same request usually mean the request is too vague: rephrase it.

### "Commande proposée : ..." then nothing happens

Free text only proposes a command. It runs only if you answer `o` to "Exécuter cette commande ? (o/n)". Anything else cancels it.

### "Aucun run pour l'instant", "Aucune revue pour l'instant", "Aucune revue en cours"

The command needs a previous step: `/tache` first, then `/dedup` and `/revue`. Results are kept in memory for the session; they are lost when the program stops (the decisions already recorded stay in `state/decisions.jsonl`).

## Gemini quota and API

### "Quota Gemini épuisé pour aujourd'hui : Arrêt dur : N/1400 requêtes ..."

The hard stop of the project was reached for the current Pacific day. No further Gemini call is made until midnight Pacific time. Check the count with `/quota`. Local commands (`/dedup`, `/valider`, `/masquer`, `/resume`...) still work.

Lowering `GEMINI_DAILY_HARD_STOP` only makes the stop earlier. Raising it is not recommended: the free-tier limits are set by Google and vary; check them in AI Studio.

### "Attention : il reste X requêtes Gemini aujourd'hui ; un run peut en utiliser jusqu'à N"

Shown before `/tache` when the remaining count is lower than the worst case of a run (plans, delegations and synthesis). The run may stop with the quota error part way through.

### "Quota insuffisant : X requêtes restantes, la revue en demande 3."

`/revue` needs three requests (one per reviewer). Wait until the next day, or run it with a higher remaining count.

### "Quota Gemini épuisé pour aujourd'hui : Google signale le quota journalier épuisé (429 PerDay)."

Google's own daily limit was reached, which can be lower than the project's hard stop. The project's counter may still show requests left. Check AI Studio.

### "Erreur Gemini : 429 persistant après N tentatives"

Google kept answering "too many requests" in a short window, after the allowed retries. Wait a few minutes. Each retry is counted in the daily total.

### "Erreur Gemini : Réseau : impossible de joindre l'API Gemini"

No connection, a proxy or a firewall blocks the request, or the API is unreachable. Check the connection and try again. The request was counted if it reached the queue.

### "Erreur Gemini : Réponse vide de Gemini (...)" or "Réponse sans texte de Gemini (finishReason=...)"

The model returned nothing usable, for a reason given in the message (often a safety filter). Rephrase the request or retry. The project does not retry this kind of answer by itself.

### "Erreur Gemini : HTTP 400 ..." or other HTTP errors

Google returned the error text in the message. A 400 or 403 usually means an invalid, disabled or restricted key, or a model name that does not exist: check `GEMINI_API_KEY` and `GEMINI_MODEL`.

### "Quota Gemini : N requêtes aujourd'hui (alerte), arrêt à 1400."

A log warning, not an error: the alert threshold (`GEMINI_DAILY_ALERT`, 1,200 by default) was passed.

### "Fichier d'état du limiteur illisible"

`state/gemini_quota.json` is unreadable, has an unknown version, or holds an impossible value. The limiter does not guess the count; the message gives the path. Check the file. Deleting it resets the daily count to zero, which may take you over Google's real quota that day. Only delete it if you accept that.

## Rephrasing (free-language mode)

### "Reformulation externe bloquée (...) : reformulation locale par Qwen."

The leak check found a personal-data value in the text that was about to be sent, so nothing was sent. The request continues with local rephrasing.

### "Reformulation externe indisponible (...) : reformulation locale par Qwen."

Gemini failed (quota, empty answer, timeout). The local model takes over. Nothing else changes.

### "Pas d'accord de session : vos messages sont reformulés localement par Qwen, rien n'est envoyé."

Expected when you refused the consent, or revoked it. `/accord oui` grants it again (not in a personal-data session).

## Claude

Claude mode needs Claude Code installed and logged in with a claude.ai account.

- "Claude Code n'est pas installé (commande « claude » introuvable)" : install Claude Code, then open a new terminal so the `claude` command is found.
- "Claude Code n'est pas connecté. Lancez : claude auth login (compte Claude Pro)" : run `claude auth login`.
- "Claude Code est connecté par « ... », ce qui facturerait l'API." : the login uses an API key, which bills the API. Log in with the claude.ai account. `CLAUDE_ALLOW_API_BILLING=1` allows it anyway; do that only if you accept API billing.
- "Claude Code a renvoyé une erreur : ... limite d'usage de l'abonnement atteinte ?" : the subscription limit was reached. Wait, or use Gemini (`/organisateur gemini`, `/grand gemini`).
- "Claude Code n'a pas répondu en 600 s" : the call timed out (`CLAUDE_TIMEOUT_S`). Retry, or raise the timeout.
- "Claude Code a renvoyé une réponse vide" or "réponse illisible" : retry. If it repeats, check `claude auth status` and the Claude Code version.

Claude calls are always counted in `state/claude_calls.json`; `/quota` shows the day's count. They are not free: they use your subscription quota.

## Personal data

### "Donnée personnelle détectée (...) : passage automatique au profil restrictif"

The code found an e-mail address, IBAN, French social-security number, phone number or birth date in your message or in a feedback reason. The session switches to the restrictive profile (default `bureautique_fictive`). External sends are blocked and the rephrasing consent is revoked.

Only an explicit `/profil <nom>` lifts this lock. Be aware:

- `/profil` clears the current results (run, dedup, review).
- The decisions recorded during a personal session are kept with `perso: true` and are never passed to the organizer.

If the detection is a false positive, see the next section, then rewrite the message without the value, or use `/profil brainstorm` to leave the restrictive profile.

### "profil restrictif « ... » indisponible ; tout envoi externe est bloqué pour la session"

The personal-data profile named by `PROFIL_DONNEES_PERSO` cannot be loaded. The session stays local until `/profil` is used. Fix the profile folder or the variable.

### False positives of the personal-data detector

The detector is deliberately broad, and some ordinary text matches it:

| Type | What it matches | Example |
|---|---|---|
| `TELEPHONE` | ten digits in pairs starting with `0[1-9]`, or an international form | a reference code written as `03 12 45 67 89` |
| `NIR` | 13 to 15 digits starting with 1 or 2, with optional spaces or dots | an order or invoice number of that shape |
| `IBAN` | two letters, two digits, then groups of four characters | some internal codes |
| `EMAIL` | `name@host.tld` | an e-mail address pasted in a log |
| `NAISSANCE` | a date right after "né le", "née le", "date de naissance", "naissance" | "née le 3 mars 1980" in a pasted text |

Names, addresses and postal codes are masked but do **not** trigger the restrictive profile, so they cannot cause a switch.

To check what will be sent, use `/masquer <texte>`: it shows the masked text and the leak check, and sends nothing. `python -m privacy apercu <fichier.txt>` does the same for a file, with the vault in memory only.

Real identifiers that are not detected (a name without a title, an address without a street type) are masked only if they are in the declared fields of the profile, or if spaCy is installed (see [README](../README.md#detailed-installation)). Do not rely on the detector for sensitive data.

### "Contrôle de fuite : BLOQUÉ, n donnée(s) restante(s)"

The masked text still contains a value from the vault or a detected identifier. It must not be sent. Edit the text and check again.

### "Refusé par la politique de données : ..."

A personal-data profile refuses any external provider. This is by design. Use local models for such a profile (or a non-personal profile with fictional data).

### "Coffre chiffré indisponible ... jetons en mémoire seulement"

On Windows, the vault uses DPAPI and should not fail. On other systems, install the optional package: `pip install cryptography`. Without it, tokens are kept in memory only and are not saved to disk.

### "Attention : coffre chiffré indisponible (coffre illisible (...))"

The vault file cannot be decrypted, usually because it was copied from another Windows account (DPAPI is bound to the account) or is damaged. The program does not reset it, because the existing tokens would no longer match. It continues with tokens in memory only. Keep the file as it is (make a copy first if you intend to change anything). Deleting it is a decision you must take knowingly.

## Local models and VRAM

### Slow answers, or the model runs on the CPU

- Run `ollama ps`. In the PROCESSOR column, `100% GPU` is expected. A split between CPU and GPU means the models do not fit in VRAM alongside other applications.
- Close applications that use the GPU, or reduce `QWEN_INTERFACE_NUM_CTX` and `QWEN_FORMAT_NUM_CTX` (the reference setup uses 8192 and 16384).
- The tested setup is a 16 GB GPU. On a smaller GPU, choose smaller model variants (`QWEN_INTERFACE_MODEL`, `QWEN_FORMAT_MODEL`); this has not been tested.

### Models stay in VRAM after I stop using the project

- The project unloads its own models when it exits normally, on Ctrl+C or after an error. It prints "VRAM libérée : ..." when it does.
- To unload them at any time, run `liberer_vram.bat` or `python -m local_llm unload`. Only the project's two models are unloaded.
- `LOCAL_KEEP_ALIVE` (default `2m`) sets how long Ollama keeps a model after its last request. `0` unloads at once, at the cost of a reload for each call.

### `python -m local_llm` reports "ÉCHEC"

Each model is checked with a short request. The message after ÉCHEC gives the cause (Ollama unreachable, model missing). Fix that, then run the check again.

Parallel requests to Ollama are controlled on the Ollama side (`OLLAMA_NUM_PARALLEL`); the project does not manage them.

## Project tests

- `tests.bat` writes its output to `logs\tests.txt`. Open that file to see the failing test names and tracebacks.
- Run one module with `python -m unittest tests.test_<name> -v`.
- The tests use test doubles: they need no network, no Ollama and no API key. A failure in them is a code problem, not a model or quota problem.

## Terminal display

- Strange characters: the program replaces characters the console cannot show (`safe_console`). To run the project with UTF-8 output as the batch files do, set `PYTHONIOENCODING=utf-8` before `python -m qwen_ui`.
- A line such as `[WARNING]` is a log message from the program, not necessarily an error.

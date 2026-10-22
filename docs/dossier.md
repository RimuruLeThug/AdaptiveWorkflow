# Working on a folder of your PC (V3.2)

The workflow can open a folder of the user's computer, understand it, answer questions about it and propose changes. The rule is the project's usual one: **the model proposes, the code decides**, and **nothing is written to the folder without the user's explicit "oui"** to a question asked by the code.

## Modules

| Module | Role |
|---|---|
| `dossier/inventaire.py` | Folder choice (`resolve_folder`, `find_folders`, `check_folder`), inventory (`inventorier`) and reading (`lire`, `extraire_document`). Read-only: files are only opened in `rb` mode. |
| `dossier/analyse.py` | Prompts and schemas of the `dossier` role: understand the folder, choose the files a request needs, answer a question, propose changes. Every path quoted by the model is checked against the inventory. |
| `dossier/modifications.py` | `construire` turns a raw proposal into checked changes (no write); `appliquer` and `annuler` are the only functions of the project that write to the user's folder. |
| `qwen_ui/dossier_app.py` | Application side (mixed into `App`): commands, dedicated consent, confirmations, conversation state. |

## Flow

1. **Choice.** A full path, a path relative to the home, Desktop ("Bureau") or Documents folder, a plain name (searched two levels deep near the home folder; several matches: the user picks one), or the Windows folder picker (`tkinter`, `/dossier choisir`; without a display the path is asked in the terminal). Drive roots, the whole home folder and system folders (Windows, Program Files, AppData...) are refused.
2. **Inventory (code only).** `os.walk` without following symbolic links. Skipped: technical folders (`.git`, `node_modules`, `__pycache__`, virtual environments, caches...), simple patterns of the root `.gitignore` (no `!` negations: more is skipped, never less), the program's own `state/` and `logs/`. Each file gets a nature: `texte` (sniffed: no NUL byte, decodes as UTF-8 or Windows-1252), `document` (Word, PowerPoint, Excel, OpenDocument, PDF: text extracted, never modified), `binaire`, `trop_gros` (over `DOSSIER_MAX_OCTETS_FICHIER`), `sensible` (`.env`, keys, credentials, password files, databases, the vault...: listed, **never read, sent or modified**). Over `DOSSIER_MAX_FICHIERS` files, the inventory is partial and says so.
3. **Dedicated consent.** If the `dossier` agent is external and available, the code first scans the files that would be sent for personal data (`personal_kinds`, the same detector as free text) and asks once **for this folder**, naming the provider and the volume, and recommending to keep everything local if personal data was found. The session consent of the conversation never covers this role (`RoleInfo.accord_dedie`). Refused, personal-data profile, `AGENT_DOSSIER=qwen`, or external agent unavailable: `App.dossier_local` is set, the session behaves like a personal-data session (every role local, conversation included) until the folder is closed; closing clears the conversation history.
4. **Analysis.** One call: the code's inventory summary, the file list (paths, sizes, natures) and the content of key files (README, manifests, entry points, then the shallowest files) up to `DOSSIER_BUDGET_CARACTERES`; a file that does not fit is shown as an excerpt and marked as such. The answer (nature, summary, important files, points of attention, possible next steps) is validated by the schema and the paths are checked.
5. **Questions and work.** If all readable files fit in the budget they are all sent; otherwise one call chooses the files (validated paths). A question gets one more call. A work request gets a proposal: `remplacer` (exact excerpt present once, replacement), `reecrire` (full content of a file shown in full), `creer` (new file).
6. **Checks by code** (`construire`): path inside the folder, normalised, no `..`, no absolute path or drive, no Windows-reserved name, no symbolic link, never sensitive or technical, never in the program's folders; an existing file is changed only if it was read for this request; no binary or document; excerpt found exactly once; no masking token left in the result; size limit. Refused changes are listed with their reason; the others are shown as a diff. The full diff and the proposed files are saved in `state/dossiers/<folder>/propositions/<id>/`.
7. **Writing** (`appliquer`), only after "oui": every file must still match the hash taken when it was read (otherwise nothing is written); originals and a manifest are saved in `state/dossiers/<folder>/sauvegardes/<date>/` before any write; each file is written atomically (temporary file in the same folder, then `os.replace`) with its encoding (UTF-8, UTF-8 with BOM, Windows-1252) and dominant line ending (CRLF kept); a failure halfway puts back what was already written.
8. **Undo** (`annuler`), after "oui": restores the originals of the last application and removes the files and folders it created, but leaves alone any file changed since (hash differs).

## What the conversation model sees

The conversation state and history only get the folder name, the file count, the nature found by the analysis, important file names and the names of files changed. Never the absolute path (it contains the Windows user name) nor file contents: the conversation agent is not necessarily the one the user allowed for the folder.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `AGENT_DOSSIER` | `claude_haiku,qwen` | Agent of the `dossier` role. `qwen` keeps file contents on the machine. |
| `DOSSIER_BUDGET_CARACTERES` | `24000` | File content sent per call at most. Keep it under the local Qwen context (`QWEN_NUM_CTX`) for fallbacks. |
| `DOSSIER_MAX_FICHIERS` | `5000` | Files walked at most. |
| `DOSSIER_MAX_OCTETS_FICHIER` | `300000` | Larger text files are listed, never read nor changed. |

## Limits

- No deletion, rename or move of files, no change to documents or binaries.
- Only the root `.gitignore` is read, without negations.
- PDF text needs `pypdf` (optional).
- Not yet run with real models: Haiku's ability to copy `remplacer` excerpts exactly and the context use of large folders with the local Qwen are to be observed.

"""Point d'entrée : python -m qwen_ui

Refuse de démarrer si la clé Gemini manque ou si Qwen 5 Go n'est pas disponible.
"""

from __future__ import annotations

import logging
import sys

from local_llm import release_project_vram
from qwen_ui.bootstrap import StartupError, build_app, safe_console


def main() -> int:
    safe_console()
    logging.basicConfig(level=logging.WARNING, format="[%(levelname)s] %(message)s")
    try:
        app = build_app()
    except StartupError as exc:
        print(exc)
        return 1
    try:
        app.loop()
    finally:
        # Quitter normalement, Ctrl+C ou une exception : on rend la VRAM dans tous les cas.
        released = release_project_vram()
        if released:
            print("VRAM libérée : " + ", ".join(released))
    return 0


if __name__ == "__main__":
    sys.exit(main())

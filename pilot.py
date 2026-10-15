"""Pilote : rejoue un scénario complet sans saisie et mesure.

    python pilot.py                      # scénario par défaut (données fictives)
    python pilot.py mon_scenario.txt     # une commande par ligne, # = commentaire

Toutes les confirmations (o/n) reçoivent « o ». Dans une commande, {premier}
est remplacé par le numéro de la première idée de la shortlist de la dernière revue. La fiche et les décisions du
pilote sont isolées dans state/pilot/ ; le compteur de quota Gemini est le
compteur commun (le pilote ne contourne pas le budget).

Sorties : logs/pilot-AAAAMMJJ-HHMMSS.txt (transcription) et .json (mesures).
"""

from __future__ import annotations

import json
import logging
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

import config
from local_llm import release_project_vram
from qwen_ui.bootstrap import StartupError, build_app, safe_console

DEFAULT_SCENARIO = """\
# Scénario fictif : brainstorm complet
bonjour, que sais-tu faire ?
/fiche Objectif : trouver des idées d'application mobile pour réduire le gaspillage alimentaire à la maison. Contraintes : gratuite ; fonctionne hors ligne ; développable seul en 3 mois. Périmètre : application Android ; suggestions de recettes avec les restes. Hors périmètre : livraison ; paiement ; version web.
/tache Utilise le generateur pour proposer 12 idées de fonctionnalités, dont au moins 2 contrariennes et 3 réalisables en une semaine.
/dedup
/revue
/quota
"""


def main() -> int:
    try:
        return _run()
    finally:
        # Fin normale, Ctrl+C ou exception : on rend la VRAM dans tous les cas.
        released = release_project_vram()
        if released:
            print("VRAM libérée : " + ", ".join(released), flush=True)


def _run() -> int:
    safe_console()
    s = config.load_settings()
    s.logs_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    txt_path = s.logs_dir / f"pilot-{stamp}.txt"
    json_path = s.logs_dir / f"pilot-{stamp}.json"
    scenario = Path(sys.argv[1]).read_text(encoding="utf-8") if len(sys.argv) > 1 else DEFAULT_SCENARIO
    commands = [l.strip() for l in scenario.splitlines() if l.strip() and not l.strip().startswith("#")]

    out = open(txt_path, "w", encoding="utf-8")

    def emit(text: str) -> None:
        out.write(text + "\n")
        out.flush()
        print(text, flush=True)

    class LogToTranscript(logging.Handler):
        def emit(self, record):  # avertissements (429, plans refusés...) dans la transcription
            emit(f"[{record.levelname}] {record.getMessage()}")

    logging.basicConfig(level=logging.WARNING, handlers=[LogToTranscript()])

    def auto_yes(prompt: str) -> str:
        emit(f"{prompt}o   (réponse automatique du pilote)")
        return "o"

    emit(f"=== Pilote {stamp} — {len(commands)} commandes ===")
    try:
        app = build_app(input_fn=auto_yes, output_fn=emit, app_state_dir=s.state_dir / "pilot")
    except StartupError as exc:
        emit(f"DÉMARRAGE REFUSÉ : {exc}")
        out.close()
        return 1

    measures = []
    used_start = app._quota_info()[0]
    for cmd in commands:
        if "{premier}" in cmd:
            if not app.last_review or not app.last_review.shortlist:
                emit(f"\n> {cmd}\n(ignorée : pas de shortlist)")
                continue
            cmd = cmd.replace("{premier}", str(app.last_review.shortlist[0].idea.id))
        emit(f"\n> {cmd}")
        used_before = app._quota_info()[0]
        t0 = time.monotonic()
        try:
            app.handle(cmd)
            error = None
        except Exception as exc:  # le pilote doit tout consigner, même un bug
            error = f"{type(exc).__name__}: {exc}"
            emit(f"EXCEPTION NON GÉRÉE : {error}")
        duration = time.monotonic() - t0
        used_after = app._quota_info()[0]
        measures.append({"command": cmd.split(" ")[0] if cmd.startswith("/") else "(texte libre)",
                         "duration_s": round(duration, 2), "gemini_requests": used_after - used_before,
                         "error": error})
        emit(f"   [mesure] {duration:.1f} s, {used_after - used_before} requête(s) Gemini")

    durations = [m["duration_s"] for m in measures]
    summary = {
        "stamp": stamp,
        "commands": measures,
        "gemini_requests_total": app._quota_info()[0] - used_start,
        "duration_total_s": round(sum(durations), 1),
        "duration_median_s": round(statistics.median(durations), 2) if durations else None,
        "errors": sum(1 for m in measures if m["error"]),
    }
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    emit(f"\n=== Fin : {summary['gemini_requests_total']} requêtes Gemini, {summary['duration_total_s']} s, "
         f"{summary['errors']} exception(s). Mesures : {json_path.name} ===")
    out.close()
    return 0 if summary["errors"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())

"""Outils hors ligne du module de masquage (aucun appel réseau).

    python -m privacy selftest
        Vérifie le coffre chiffré sur cette machine (DPAPI sous Windows) avec des
        données fictives : chiffrement, relecture, jetons stables, réinjection.

    python -m privacy apercu <fichier.txt> [--declare "Dupont,Martin"]
        Affiche le texte masqué et le résultat du contrôle de fuite. Coffre en
        mémoire : rien n'est écrit sur disque.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from privacy.detectors import spacy_name_detector
from privacy.masker import Masker, render_preview
from privacy.vault import Vault, VaultError, default_cipher

SAMPLE = ("Dossier fictif : M. Jean DUPONT (jean.dupont@exemple.fr, 06 12 34 56 78), né le 3 mars 1980, "
          "IBAN FR76 3000 6000 0112 3456 7890 189, 12 rue des Lilas 75011 Paris. Rappeler M. Jean DUPONT demain.")


def selftest() -> int:
    ok = True
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "coffre.bin"
        try:
            cipher = default_cipher(Path(d))
        except VaultError as exc:
            print(f"ÉCHEC : chiffrement indisponible : {exc}")
            return 1
        m = Masker(Vault(path, cipher))
        res = m.mask(SAMPLE)
        raw = path.read_bytes()
        checks = {
            f"chiffrement « {cipher.name} » disponible": True,
            "coffre écrit sur disque": path.exists() and len(raw) > 0,
            "aucune valeur en clair dans le fichier": all(v.encode() not in raw for v in ("DUPONT", "jean.dupont", "FR76")),
            "même personne = même jeton": res.text.count("[PERSONNE_1]") == 2,
            "contrôle de fuite propre": not m.leak_check(res.text),
        }
        reloaded = Masker(Vault(path, cipher))
        restored, unknown = reloaded.unmask(res.text)
        checks["relecture et réinjection exactes"] = restored == SAMPLE and not unknown
        for label, passed in checks.items():
            print(f"{'OK    ' if passed else 'ÉCHEC '} {label}")
            ok = ok and passed
        print("\nTexte masqué :\n" + res.text)
    return 0 if ok else 1


def apercu(args) -> int:
    if not args:
        print(__doc__)
        return 2
    text = Path(args[0]).read_text(encoding="utf-8")
    declared = []
    if "--declare" in args:
        declared = [x.strip() for x in args[args.index("--declare") + 1].split(",") if x.strip()]
    m = Masker(Vault(None), name_detector=spacy_name_detector())
    res = m.mask(text, declared=declared)
    print(render_preview(res))
    leaks = m.leak_check(res.text, declared=declared)
    print("\nContrôle de fuite : " + ("OK" if not leaks else f"BLOQUÉ ({len(leaks)} donnée(s) restante(s))"))
    return 0 if not leaks else 1


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    args = sys.argv[1:]
    if args[:1] == ["selftest"]:
        return selftest()
    if args[:1] == ["apercu"]:
        return apercu(args[1:])
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())

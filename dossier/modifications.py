"""Modifications proposées sur un dossier : contrôle par le code, diff, application, annulation.

construire() ne fait que calculer (aucune écriture). appliquer() et annuler() sont les SEULES
fonctions du projet qui écrivent dans le dossier de l'utilisateur ; l'application les appelle
uniquement après son « oui » explicite. Garanties :

- chemins : dans le dossier, jamais sensibles ni techniques, jamais par un lien symbolique,
  jamais dans les dossiers d'état ou de journaux du programme ;
- un fichier existant n'est modifié que s'il a été lu pour cette demande (« remplacer » :
  extrait présent une seule fois ; « reecrire » : fichier montré en entier au modèle) ;
- aucune suppression ni renommage ; aucun fichier binaire ou document ;
- avant d'écrire : chaque fichier doit être identique à ce qui a été lu (empreinte), sinon
  RIEN n'est écrit ; sauvegarde des originaux et manifeste écrits AVANT toute écriture ;
- écriture atomique (fichier temporaire puis remplacement), même encodage et mêmes fins de
  ligne qu'avant ; en cas d'échec en cours de route, retour à l'état d'origine ;
- annulation : restaure les originaux, sauf les fichiers modifiés depuis par quelqu'un d'autre.
"""

from __future__ import annotations

import difflib
import json
import os
import secrets
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from dossier.inventaire import (BINARY_EXT, DOCUMENT_EXT, DossierError, Inventaire, Lecture, chemin_sur, empreinte,
                                lire)
from privacy.detectors import TOKEN_RE

MAX_CONTENU = 500_000  # caractères d'un fichier créé ou réécrit


@dataclass
class Changement:
    chemin: str
    avant: Optional[str]  # None : création
    apres: str
    encodage: str
    fin_ligne: str
    empreinte_avant: Optional[str]
    explications: List[str] = field(default_factory=list)

    @property
    def creation(self) -> bool:
        return self.avant is None

    def lignes(self) -> Tuple[int, int]:
        """(lignes ajoutées, lignes retirées)."""
        plus = minus = 0
        for line in difflib.ndiff((self.avant or "").splitlines(), self.apres.splitlines()):
            if line.startswith("+ "):
                plus += 1
            elif line.startswith("- "):
                minus += 1
        return plus, minus


@dataclass
class Proposition:
    id: str
    consigne: str
    resume: str
    changements: List[Changement] = field(default_factory=list)
    refus: List[str] = field(default_factory=list)  # modifications écartées par le code, avec la raison
    question: str = ""

    @property
    def vide(self) -> bool:
        return not self.changements

    def render_resume(self) -> str:
        lines = []
        for c in self.changements:
            plus, minus = c.lignes()
            what = "nouveau fichier" if c.creation else f"+{plus} / -{minus} ligne(s)"
            lines.append(f"  • {c.chemin} ({what})" + (f" — {'; '.join(c.explications)}" if c.explications else ""))
        return "\n".join(lines)

    def diff(self) -> str:
        out: List[str] = []
        for c in self.changements:
            before = (c.avant or "").splitlines(keepends=True)
            after = c.apres.splitlines(keepends=True)
            src = "/dev/null" if c.creation else f"a/{c.chemin}"
            out.extend(difflib.unified_diff(before, after, fromfile=src, tofile=f"b/{c.chemin}", n=2))
            if out and not out[-1].endswith("\n"):
                out[-1] += "\n"
        return "".join(out)

    def render_diff(self, max_lignes: int = 120) -> Tuple[str, int]:
        """(diff affichable, nombre de lignes masquées)."""
        lines = self.diff().splitlines()
        if len(lines) <= max_lignes:
            return "\n".join(lines), 0
        return "\n".join(lines[:max_lignes]), len(lines) - max_lignes


def _norm(text: str) -> str:
    return str(text).replace("\r\n", "\n").replace("\r", "\n")


def _forbidden(root: Path, rel: str, interdits: Sequence[Path]) -> bool:
    target = (root / Path(*rel.split("/"))).resolve()
    for d in interdits:
        try:
            target.relative_to(Path(d).resolve())
            return True
        except (ValueError, OSError):
            continue
    return False


def construire(doc: Mapping, inv: Inventaire, *, consigne: str, lectures: Mapping[str, Lecture],
               complets: Iterable[str] = (), defaut_fin: str = "\n", interdits: Sequence[Path] = (),
               ident: Optional[str] = None) -> Proposition:
    """Proposition brute du modèle -> changements vérifiés par le code. N'écrit RIEN."""
    root = inv.racine
    complets = set(complets)
    lus: Dict[str, Lecture] = dict(lectures)
    prop = Proposition(ident or datetime.now().strftime("%Y%m%d-%H%M%S"), consigne,
                       str(doc.get("resume", "")).strip(), question=str(doc.get("question", "") or "").strip())
    by_path: Dict[str, Changement] = {}
    for i, mod in enumerate(doc.get("modifications", []), 1):
        action = mod.get("action", "")
        raw_path = str(mod.get("chemin", ""))
        try:
            rel = chemin_sur(root, raw_path)
            if _forbidden(root, rel, interdits):
                raise DossierError(f"« {rel} » est un dossier du programme lui-même : jamais modifié")
            ext = os.path.splitext(rel)[1].lower()
            if ext in BINARY_EXT or ext in DOCUMENT_EXT:
                raise DossierError(f"« {rel} » : fichier binaire ou document, jamais modifié")
            entry = inv.get(rel)
            current = by_path.get(entry.chemin if entry else rel)
            explication = " ".join(str(mod.get("explication", "")).split())
            if action == "creer":
                if current is not None or entry is not None or (root / Path(*rel.split("/"))).exists():
                    raise DossierError(f"« {rel} » existe déjà : je ne le crée pas par-dessus")
                content = _norm(mod.get("contenu", ""))
                _check_size(rel, content)
                by_path[rel] = Changement(rel, None, content, "utf-8", defaut_fin, None, [explication] if explication else [])
                continue
            if entry is None:
                raise DossierError(f"« {rel} » n'existe pas dans le dossier")
            rel = entry.chemin
            lec = lus.get(rel)
            if lec is None:
                lec = lire(inv, rel, defaut_fin=defaut_fin)  # lève DossierError si non texte
                lus[rel] = lec
            base = current.apres if current else lec.texte
            if action == "remplacer":
                old, new = _norm(mod.get("ancien", "")), _norm(mod.get("nouveau", ""))
                if not old.strip():
                    raise DossierError(f"« {rel} » : extrait à remplacer vide")
                count = base.count(old)
                if count == 0:
                    raise DossierError(f"« {rel} » : extrait à remplacer introuvable (le modèle l'a mal recopié)")
                if count > 1:
                    raise DossierError(f"« {rel} » : extrait à remplacer présent {count} fois, ambigu")
                after = base.replace(old, new, 1)
            elif action == "reecrire":
                if rel not in complets:
                    raise DossierError(f"« {rel} » : le modèle n'a pas vu ce fichier en entier, réécriture refusée")
                after = _norm(mod.get("contenu", ""))
            else:
                raise DossierError(f"action inconnue « {action} »")
            _check_size(rel, after)
            if current is None:
                current = Changement(rel, lec.texte, after, lec.encodage, lec.fin_ligne, lec.empreinte)
                by_path[rel] = current
            current.apres = after
            if explication:
                current.explications.append(explication)
        except DossierError as exc:
            prop.refus.append(f"modification {i} ({action or '?'} {raw_path or '?'}) : {exc}")
    for c in by_path.values():
        # Jeton de masquage resté dans le texte (non réinjecté) : jamais écrit dans un fichier.
        new_tokens = set(TOKEN_RE.findall(c.apres)) - set(TOKEN_RE.findall(c.avant or ""))
        if new_tokens:
            prop.refus.append(f"« {c.chemin} » : jeton(s) de masquage non réinjecté(s) ({', '.join(sorted(new_tokens))})")
            continue
        if c.avant is not None and c.apres == c.avant:
            continue
        prop.changements.append(c)
    return prop


def _check_size(rel: str, content: str) -> None:
    if len(content) > MAX_CONTENU:
        raise DossierError(f"« {rel} » : contenu trop long ({len(content)} caractères)")


# -------------------------------------------------------------- application


@dataclass
class Application:
    manifeste: Path
    fichiers: List[str]


def _encode(c: Changement) -> bytes:
    text = c.apres.replace("\n", c.fin_ligne) if c.fin_ligne != "\n" else c.apres
    try:
        return text.encode(c.encodage)
    except (UnicodeEncodeError, LookupError):
        raise DossierError(f"« {c.chemin} » : caractère impossible à écrire dans l'encodage du fichier "
                           f"({c.encodage})") from None


def _atomic_write(path: Path, data: bytes, *, mode_from: Optional[Path] = None) -> None:
    tmp = path.with_name(f".{path.name}.cheapworkflow-{secrets.token_hex(4)}.tmp")
    try:
        with open(tmp, "xb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if mode_from is not None and mode_from.exists():
            shutil.copymode(mode_from, tmp)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def appliquer(prop: Proposition, root: Path, sauvegardes: Path, *, interdits: Sequence[Path] = ()) -> Application:
    """Écrit la proposition dans le dossier. À n'appeler qu'après le « oui » de l'utilisateur.

    Lève DossierError (et n'écrit rien) si un fichier a changé depuis sa lecture."""
    if prop.vide:
        raise DossierError("aucune modification à appliquer")
    root = Path(root).resolve()
    plan: List[Tuple[Changement, Path, bytes, Optional[bytes]]] = []
    # 1. Vérifications, AVANT toute écriture : chemins, état actuel, encodage.
    for c in prop.changements:
        rel = chemin_sur(root, c.chemin)
        if _forbidden(root, rel, interdits):
            raise DossierError(f"« {rel} » : dossier du programme, jamais modifié")
        path = root / Path(*rel.split("/"))
        if c.creation:
            if path.exists():
                raise DossierError(f"« {rel} » existe maintenant : rien n'a été écrit. Redemande-moi la modification.")
            original = None
        else:
            try:
                original = path.read_bytes()
            except OSError as exc:
                raise DossierError(f"« {rel} » illisible ({exc}) : rien n'a été écrit") from None
            if empreinte(original) != c.empreinte_avant:
                raise DossierError(f"« {rel} » a changé depuis que je l'ai lu : rien n'a été écrit. "
                                   "Redemande-moi la modification.")
        plan.append((c, path, _encode(c), original))

    # 2. Sauvegarde des originaux et manifeste, AVANT d'écrire dans le dossier.
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
    backup = Path(sauvegardes) / stamp
    (backup / "fichiers").mkdir(parents=True, exist_ok=False)
    entries = []
    for c, path, data, original in plan:
        if original is not None:
            dest = backup / "fichiers" / Path(*c.chemin.split("/"))
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(original)
        missing_dirs = []
        parent = path.parent
        while parent != root and not parent.exists():
            missing_dirs.append(parent.relative_to(root).as_posix())
            parent = parent.parent
        entries.append({"chemin": c.chemin, "existait": original is not None, "empreinte_apres": empreinte(data),
                        "dossiers_crees": missing_dirs})
    manifest = {"id": prop.id, "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "racine": str(root), "consigne": prop.consigne, "resume": prop.resume, "fichiers": entries,
                "annulee": False}
    manifest_path = backup / "manifeste.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    # 3. Écriture atomique ; en cas d'échec, retour à l'état d'origine.
    done: List[Tuple[Changement, Path, Optional[bytes], List[str]]] = []
    try:
        for (c, path, data, original), entry in zip(plan, entries):
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(path, data, mode_from=path if original is not None else None)
            done.append((c, path, original, entry["dossiers_crees"]))
    except OSError as exc:
        for c, path, original, dirs in reversed(done):
            try:
                if original is None:
                    path.unlink()
                else:
                    _atomic_write(path, original, mode_from=path)
            except OSError:
                pass
            _remove_empty(root, dirs)
        manifest["annulee"] = True
        manifest["echec"] = str(exc)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        raise DossierError(f"écriture impossible ({exc}) — le fichier est peut-être ouvert dans un autre programme. "
                           "Le dossier a été remis dans son état d'origine.") from None
    return Application(manifest_path, [c.chemin for c in prop.changements])


def _remove_empty(root: Path, dirs: Sequence[str]) -> None:
    for d in dirs:  # du plus profond au moins profond
        p = root / Path(*d.split("/"))
        try:
            p.rmdir()  # seulement s'il est vide
        except OSError:
            pass


def derniere_application(sauvegardes: Path) -> Optional[Path]:
    """Manifeste de la dernière application non annulée, ou None."""
    base = Path(sauvegardes)
    if not base.is_dir():
        return None
    for d in sorted((p for p in base.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True):
        m = d / "manifeste.json"
        try:
            data = json.loads(m.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not data.get("annulee"):
            return m
    return None


def lire_manifeste(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def annuler(manifest_path: Path, root: Path) -> Tuple[List[str], List[str]]:
    """Restaure les fichiers d'avant l'application. Retourne (restaurés, laissés tels quels).

    À n'appeler qu'après le « oui » de l'utilisateur. Un fichier modifié depuis l'application
    (empreinte différente) n'est jamais écrasé."""
    manifest_path = Path(manifest_path)
    data = lire_manifeste(manifest_path)
    if data.get("annulee"):
        raise DossierError("ces modifications ont déjà été annulées")
    root = Path(root).resolve()
    if Path(data.get("racine", "")).resolve() != root:
        raise DossierError("cette sauvegarde concerne un autre dossier")
    backup = manifest_path.parent / "fichiers"
    restored, skipped = [], []
    for entry in data["fichiers"]:
        rel = chemin_sur(root, entry["chemin"])
        path = root / Path(*rel.split("/"))
        try:
            current = path.read_bytes() if path.exists() else None
        except OSError:
            current = None
        if current is None or empreinte(current) != entry["empreinte_apres"]:
            skipped.append(rel)
            continue
        if entry["existait"]:
            _atomic_write(path, (backup / Path(*rel.split("/"))).read_bytes(), mode_from=path)
        else:
            path.unlink()
            _remove_empty(root, entry.get("dossiers_crees", []))
        restored.append(rel)
    data["annulee"] = True
    data["annulation"] = {"date": datetime.now(timezone.utc).isoformat(timespec="seconds"), "restaures": restored,
                          "laisses": skipped}
    manifest_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return restored, skipped


def snapshot(root: Path) -> Dict[str, Tuple[int, str]]:
    """État d'un dossier (taille, empreinte de chaque fichier) : utilisé par les tests de lecture seule."""
    out: Dict[str, Tuple[int, str]] = {}
    root = Path(root)
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            p = Path(dirpath) / name
            raw = p.read_bytes()
            out[p.relative_to(root).as_posix()] = (len(raw), empreinte(raw))
    return out


__all__ = ["Changement", "Proposition", "Application", "construire", "appliquer", "annuler", "derniere_application",
           "lire_manifeste", "snapshot"]

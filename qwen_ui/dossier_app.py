"""Travail sur un dossier du PC, côté application (V3.2) : mélangé à App (qwen_ui/app.py).

Parcours : choisir un dossier (chemin dit ou tapé, nom cherché près du dossier personnel, ou
fenêtre de choix) -> inventaire par le CODE, en lecture seule -> accord DÉDIÉ avant d'envoyer
son contenu à un agent externe (refus : tout reste sur le PC, Qwen local, jusqu'à la fermeture
du dossier) -> analyse par le rôle « dossier » -> questions, ou demandes de travail : le modèle
PROPOSE des modifications, le code les vérifie et montre le diff, et RIEN n'est écrit dans le
dossier sans un « oui » explicite de l'utilisateur à la question posée par le code. Les
originaux sont sauvegardés (state/dossiers/...) et la dernière application peut être annulée.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

from dossier.analyse import Analyse, analyser, fichiers_cles, preparer, proposer, repondre
from dossier.inventaire import (DossierError, Inventaire, check_folder, find_folders, inventorier, lire,
                                resolve_folder, taille_lisible)
from dossier.modifications import (Proposition, annuler, appliquer, construire, derniere_application,
                                   lire_manifeste)
from local_llm import LocalLLMError
from privacy.policy import PolicyError
from qwen_ui.langage_libre import PROVIDER_LABELS, personal_kinds, short
from verify.schema import SchemaError


class PickerUnavailable(Exception):
    """Pas de fenêtre de choix de dossier sur cette machine (tkinter absent, pas d'écran)."""


def tk_folder_picker(initial: Optional[Path] = None) -> Optional[str]:
    """Fenêtre native de choix de dossier (tkinter, fourni avec Python sous Windows).

    Retourne le chemin choisi, None si l'utilisateur annule ; PickerUnavailable sinon."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError:
        raise PickerUnavailable("tkinter absent") from None
    try:
        root = tk.Tk()
    except Exception as exc:  # tk.TclError : pas d'écran
        raise PickerUnavailable(str(exc)) from None
    try:
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askdirectory(parent=root, mustexist=True, initialdir=str(initial or Path.home()),
                                       title="Choisis le dossier à analyser (lecture seule)")
    finally:
        root.destroy()
    return path or None


def folder_slug(root: Path) -> str:
    name = re.sub(r"[^\w.-]+", "_", root.name or "racine")[:40]
    return f"{name}-{hashlib.sha1(str(root).casefold().encode('utf-8')).hexdigest()[:8]}"


@dataclass
class DossierOuvert:
    inv: Inventaire
    base: Path  # state/dossiers/<slug> : propositions et sauvegardes (jamais dans le dossier lui-même)
    defaut_fin: str = "\n"
    analyse: Optional[Analyse] = None
    proposition: Optional[Proposition] = None
    externe: Optional[str] = None  # fournisseur autorisé pour CE dossier (accord dédié)
    journal: List[str] = field(default_factory=list)

    @property
    def racine(self) -> Path:
        return self.inv.racine

    @property
    def sauvegardes(self) -> Path:
        return self.base / "sauvegardes"

    def peut_annuler(self) -> bool:
        return derniere_application(self.sauvegardes) is not None


class DossierMixin:
    """Méthodes de App consacrées au dossier ouvert. Attributs attendus (posés par App) :
    dossier, dossier_local, _dossier_limites, _folder_picker, _home, _protected_dirs."""

    # ---------------------------------------------------------------- outils

    def _dossier_router(self):
        return self._ll.reformulator.router if getattr(self, "_ll", None) is not None else None

    def _dossier_route(self):
        """(fournisseur, libellé) si l'agent du rôle « dossier » est externe ET disponible, sinon None."""
        router = self._dossier_router()
        if router is None or "dossier" not in router.routes:
            return None
        route = router.routes["dossier"]
        spec = router.registry.spec(route.agent)
        if not spec.external:
            return None
        try:
            if router.registry.get(route.agent).unavailable_reason():
                return None
        except Exception:  # agent impossible à construire : traité comme indisponible
            return None
        return spec.policy_provider, self._external_label(spec.policy_provider)

    def _need_folder(self) -> Optional[DossierOuvert]:
        if self.dossier is None:
            self._out("Aucun dossier ouvert. Dis-moi lequel regarder (son chemin), ou /dossier choisir.")
        return self.dossier

    def _budget(self) -> int:
        return self._dossier_limites.budget_caracteres

    def _resume_dossier(self) -> str:
        d = self.dossier
        if d is None:
            return ""
        return d.analyse.to_context() if d.analyse else d.inv.resume_code()

    def _dossier_state(self) -> List[str]:
        """Lignes de l'état du travail (conversation) : jamais le chemin complet ni le contenu."""
        d = self.dossier
        if d is None:
            return ["Dossier du PC ouvert : aucun"]
        inv = d.inv
        lines = [f"Dossier du PC ouvert : « {inv.nom} » ({len(inv.entrees)} fichier(s)"
                 + (", tout reste sur le PC" if self.dossier_local else "") + ")"]
        if d.analyse:  # nature et noms de fichiers seulement : jamais le contenu
            lines.append(f"  nature : {short(d.analyse.nature, 120)}")
            if d.analyse.elements:
                lines.append("  fichiers importants : " + ", ".join(c for c, _ in d.analyse.elements[:8]))
        if d.proposition is not None:
            lines.append(f"  modifications proposées en attente : {len(d.proposition.changements)} fichier(s) "
                         f"({short(d.proposition.consigne, 80)}) — rien n'est encore écrit")
        if d.peut_annuler():
            lines.append("  dernière modification appliquée : annulable")
        return lines

    # ------------------------------------------------------------ commandes

    def _cmd_dossier(self, arg: str) -> None:
        arg = arg.strip()
        low = arg.lower()
        if low in ("fermer", "ferme", "close"):
            self._close_folder()
        elif low in ("", "voir") and self.dossier is not None:
            self._show_folder()
        elif low in ("relire", "actualiser", "rafraichir", "rafraîchir") and self.dossier is not None:
            self._open_folder(str(self.dossier.racine), refresh=True)
        elif low in ("", "choisir", "choix", "parcourir"):
            self._open_folder("")
        else:
            self._open_folder(arg)

    def _cmd_question(self, arg: str) -> None:
        if not arg.strip():
            self._out("Usage : /question <ta question sur le dossier ouvert>")
            return
        self._ask_folder(arg.strip())

    def _cmd_travail(self, arg: str) -> None:
        if not arg.strip():
            self._out("Usage : /travail <ce qu'il faut faire dans le dossier ouvert>")
            return
        self._work_folder(arg.strip())

    def _cmd_diff(self, arg: str) -> None:
        d = self._need_folder()
        if d is None:
            return
        if d.proposition is None:
            self._out("Aucune modification en attente.")
            return
        text, hidden = d.proposition.render_diff(max_lignes=2000)
        self._out(text + (f"\n… {hidden} ligne(s) de plus : voir {self._proposal_dir(d.proposition)}" if hidden else ""))

    def _cmd_appliquer(self, arg: str) -> None:
        self._apply_pending()

    def _cmd_annuler(self, arg: str) -> None:
        self._undo_last()

    # ------------------------------------------------------------ ouverture

    def _pick_path(self) -> Optional[str]:
        if self._folder_picker is not None:
            try:
                self._out("J'ouvre une fenêtre pour choisir le dossier…")
                chosen = self._folder_picker()
                if chosen:
                    return chosen
                self._out("Aucun dossier choisi.")
                return None
            except PickerUnavailable:
                pass
        try:
            typed = self._in("Quel dossier ? (chemin complet, ex. C:\\Users\\toi\\Desktop\\MonProjet ; Entrée pour annuler) ")
        except (EOFError, KeyboardInterrupt):
            return None
        return typed.strip() or None

    def _locate(self, raw: str) -> Optional[Path]:
        """Chemin donné -> dossier, avec recherche par nom près du dossier personnel."""
        try:
            return resolve_folder(raw, home=self._home)
        except DossierError as exc:
            error = str(exc)
        found = find_folders(raw, home=self._home)
        if not found:
            self._out(f"{error[:1].upper()}{error[1:]} Donne-moi son chemin complet, ou /dossier choisir pour "
                      "ouvrir une fenêtre.")
            return None
        if len(found) == 1:
            self._out(f"J'ai trouvé : {found[0]}")
            return found[0]
        listing = "\n".join(f"  {i}. {p}" for i, p in enumerate(found, 1))
        self._out(f"Plusieurs dossiers s'appellent « {raw.strip()} » :\n{listing}")
        try:
            answer = self._in(f"Lequel ? (numéro de 1 à {len(found)}, Entrée pour annuler) ").strip()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer.isdigit() and 1 <= int(answer) <= len(found):
            return found[int(answer) - 1]
        self._out("D'accord, je n'ouvre rien.")
        return None

    def _open_folder(self, raw: str, *, refresh: bool = False) -> None:
        raw = raw or (self._pick_path() or "")
        if not raw:
            return
        path = self._locate(raw)
        if path is None:
            return
        try:
            root = check_folder(path, home=self._home)
        except DossierError as exc:
            self._out(f"Je ne peux pas ouvrir ce dossier : {exc}")
            return
        same = self.dossier is not None and self.dossier.racine == root
        if self.dossier is not None and not same:
            self._close_folder(quiet=True)
        self._out(f"{'Je relis' if same else 'Je regarde'} le dossier {root} — lecture seule, rien n'y est modifié.")
        inv = inventorier(root, self._dossier_limites, exclus=self._protected_dirs)
        self._out(inv.resume_code())
        if same:
            self.dossier.inv = inv
            self.dossier.proposition = None
        else:
            base = self._dossier_state_dir / folder_slug(root)
            self.dossier = DossierOuvert(inv, base, defaut_fin=self._guess_newline(inv))
        d = self.dossier
        self._history_note(f"Dossier ouvert : « {inv.nom} », {len(inv.entrees)} fichier(s).")
        if not any(e.lisible for e in inv.entrees):
            self._out("Je n'y trouve aucun fichier lisible (texte ou document) : je ne peux que te décrire la liste.")
            return
        if self._dossier_consent(first=not same) is None:
            return
        self._out(f"Je lis les principaux fichiers et je les fais analyser ({self._role_label('dossier')})…")
        try:
            d.analyse = analyser(self._llm("dossier"), inv, self._budget(), defaut_fin=d.defaut_fin)
        except (LocalLLMError, SchemaError) as exc:
            self._out(f"Je n'ai pas pu faire analyser le contenu ({exc}). Le dossier reste ouvert : tu peux "
                      "me poser des questions ou réessayer avec /dossier relire.")
            return
        self._out("Voici ce que je comprends du dossier :\n" + d.analyse.render())
        # Historique de conversation : nature et noms de fichiers seulement, jamais le contenu (il peut
        # partir vers l'agent de conversation, qui n'est pas forcément celui autorisé pour le dossier).
        self._history_note(f"Dossier « {inv.nom} » analysé : {short(d.analyse.nature, 120)} ; fichiers importants : "
                           + (", ".join(c for c, _ in d.analyse.elements[:8]) or "(aucun)") + ".")
        self._out("Je n'ai rien modifié. Pose-moi tes questions sur ce dossier, ou demande-moi d'y travailler "
                  "(« corrige les fautes du README »…) : je te montrerai chaque changement avant d'écrire quoi que "
                  "ce soit.")

    def _guess_newline(self, inv: Inventaire) -> str:
        """Fin de ligne des nouveaux fichiers : celle des fichiers texte du dossier (les 5 premiers)."""
        crlf = lf = 0
        for e in inv.de_nature("texte")[:5]:
            try:
                fin = lire(inv, e.chemin).fin_ligne
            except (DossierError, OSError):
                continue
            crlf += fin == "\r\n"
            lf += fin == "\n"
        if crlf or lf:
            return "\r\n" if crlf >= lf else "\n"
        return os.linesep

    def _history_note(self, text: str) -> None:
        if getattr(self, "_history", None) is not None:
            self._history.assistant(text)

    # --------------------------------------------------------------- accord

    def _scan_personal(self, inv: Inventaire) -> Dict[str, List[str]]:
        """Données personnelles repérées par le CODE dans les fichiers qui seraient envoyés."""
        found: Dict[str, List[str]] = {}
        budget = self._budget()
        declared = self._declared()
        for chemin in fichiers_cles(inv):
            if budget <= 0:
                break
            e = inv.get(chemin)
            if e is None or e.nature != "texte":
                continue
            try:
                text = lire(inv, chemin).texte[:budget]
            except (DossierError, OSError):
                continue
            budget -= len(text)
            for kind in personal_kinds(text, declared):
                found.setdefault(kind, []).append(chemin)
        return found

    def _dossier_consent(self, *, first: bool = True) -> Optional[bool]:
        """Accord DÉDIÉ avant d'envoyer le contenu du dossier à un agent externe.

        True : envoi externe autorisé pour ce dossier ; False : tout reste sur le PC ; None :
        l'utilisateur a répondu autre chose (correction), le dossier est refermé."""
        d = self.dossier
        if self._personal():
            if first:
                self._out("Données personnelles : le contenu du dossier reste sur ton PC (Qwen local).")
            return False
        route = self._dossier_route()
        if route is None:
            # Contenu traité sur le PC (AGENT_DOSSIER local, ou agent externe indisponible) : ce qui en
            # découle ne doit pas partir non plus par la conversation, jusqu'à la fermeture du dossier.
            if not self.dossier_local:
                self.dossier_local = True
                self._out("(Le contenu de ce dossier est traité sur ton PC ; la conversation aussi, tant qu'il "
                          "est ouvert.)")
            return False
        provider, label = route
        router = self._dossier_router()
        if router.has_session_consent("dossier", provider):
            return True
        found = self._scan_personal(d.inv)
        n = sum(1 for e in d.inv.entrees if e.lisible)
        question = (f"Pour comprendre ce dossier, je vais envoyer à {label} la liste de ses fichiers et le contenu des "
                    f"principaux (au plus {taille_lisible(self._budget())} de texte par demande, sur {n} fichier(s) "
                    "lisibles). Les fichiers sensibles (.env, clés…) ne sont jamais lus, et les données personnelles "
                    "repérées sont masquées avant l'envoi.")
        if found:
            where = "; ".join(f"{k} : {', '.join(v[:3])}{' …' if len(v) > 3 else ''}" for k, v in sorted(found.items()))
            question += (f"\nAttention : données personnelles possibles dans ces fichiers ({where}). Je te conseille "
                         "de tout garder sur ton PC.")
        question += "\nD'accord pour l'envoyer ? (sinon tout reste sur ton PC avec Qwen, plus lentement)"
        ok = self._confirm(question)
        if not ok and getattr(self, "_carry", None) is not None:
            self._close_folder(quiet=True)
            return None
        if ok:
            try:
                router.grant_session_consent("dossier", provider)
            except PolicyError:
                ok = False
        router.log_choice("dossier", router.routes["dossier"].agent,
                          "accord_dossier_donne" if ok else "accord_dossier_refuse", "accord dédié au dossier ouvert")
        if ok:
            d.externe = provider
            self._out(f"C'est noté pour ce dossier : je m'appuie sur {label}.")
            return True
        self.dossier_local = True
        self._out("D'accord : tant que ce dossier est ouvert, tout reste sur ton PC (Qwen local), conversation "
                  "comprise.")
        return False

    # --------------------------------------------------------------- lecture

    def _show_folder(self) -> None:
        d = self._need_folder()
        if d is None:
            return
        self._out(f"Dossier ouvert : {d.racine}\n{d.inv.resume_code()}")
        if d.analyse:
            self._out(d.analyse.render())
        if d.proposition is not None:
            self._out(f"Modifications en attente (rien n'est encore écrit) :\n{d.proposition.render_resume()}\n"
                      "/appliquer pour les écrire, /diff pour tout voir.")

    def _ask_folder(self, question: str) -> None:
        d = self._need_folder()
        if d is None or self._dossier_consent(first=False) is None:
            return
        self._out(f"Je cherche dans le dossier ({self._role_label('dossier')})…")
        llm = self._llm("dossier")
        ctx = preparer(llm, d.inv, question, self._budget(), resume=self._resume_dossier(), defaut_fin=d.defaut_fin)
        answer, files = repondre(llm, d.inv, question, ctx, resume=self._resume_dossier())
        text = answer + (f"\n(Fichiers : {', '.join(files[:8])})" if files else "")
        self._out(text)
        self._history_note("(Réponse donnée sur le dossier" + (f", fichiers : {', '.join(files[:6])}" if files else "")
                           + ".)")

    # ------------------------------------------------------------- travail

    def _proposal_dir(self, prop: Proposition) -> Path:
        return self.dossier.base / "propositions" / prop.id

    def _save_proposal(self, prop: Proposition) -> Path:
        """Diff et fichiers proposés gardés dans state/ (jamais dans le dossier), pour les relire."""
        out = self._proposal_dir(prop)
        (out / "fichiers").mkdir(parents=True, exist_ok=True)
        (out / "modifications.diff").write_text(prop.diff(), encoding="utf-8")
        for c in prop.changements:
            dest = out / "fichiers" / Path(*c.chemin.split("/"))
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(c.apres, encoding="utf-8")
        (out / "proposition.json").write_text(json.dumps({
            "id": prop.id, "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "consigne": prop.consigne, "resume": prop.resume, "refus": prop.refus,
            "fichiers": [c.chemin for c in prop.changements]}, ensure_ascii=False, indent=2), encoding="utf-8")
        return out

    def _work_folder(self, consigne: str) -> None:
        d = self._need_folder()
        if d is None:
            return
        if self._talking:
            if not self._confirm(f"Je prépare des modifications dans « {d.inv.nom} » pour :\n  « {short(consigne, 1500)} »\n"
                                 "Rien ne sera écrit sans ton accord. On y va ?"):
                if self._carry is None:
                    self._say("D'accord, je ne prépare rien.")
                return
        if self._dossier_consent(first=False) is None:
            return
        self._out(f"Je lis les fichiers utiles et je prépare les modifications ({self._role_label('dossier')})…")
        llm = self._llm("dossier")
        resume = self._resume_dossier()
        ctx = preparer(llm, d.inv, consigne, self._budget(), resume=resume, defaut_fin=d.defaut_fin)
        doc = proposer(llm, d.inv, consigne, ctx, resume=resume)
        prop = construire(doc, d.inv, consigne=consigne, lectures=ctx.lectures, complets=ctx.complets,
                          defaut_fin=d.defaut_fin, interdits=self._protected_dirs)
        refus = ("\nÉcarté par le programme :\n" + "\n".join(f"  • {r}" for r in prop.refus)) if prop.refus else ""
        if prop.vide:
            d.proposition = None
            msg = prop.question or prop.resume or "Je n'ai trouvé aucune modification à faire."
            self._out(msg + refus)
            self._history_note("Aucune modification proposée" + (" (question posée à l'utilisateur)." if prop.question
                                                                  else "."))
            return
        d.proposition = prop
        saved = self._save_proposal(prop)
        diff, hidden = prop.render_diff()
        self._out(f"{prop.resume}\n\nModifications proposées (rien n'est encore écrit) :\n{prop.render_resume()}"
                  f"{refus}\n\n{diff}"
                  + (f"\n… {hidden} ligne(s) de plus (/diff pour tout voir)." if hidden else "")
                  + f"\n(Diff complet et fichiers proposés : {saved})")
        self._history_note(f"Modifications proposées dans « {d.inv.nom} » : "
                           + ", ".join(c.chemin for c in prop.changements[:8]) + ". En attente du oui de l'utilisateur.")
        self._apply_pending()

    def _apply_pending(self) -> None:
        d = self._need_folder()
        if d is None:
            return
        prop = d.proposition
        if prop is None:
            self._out("Aucune modification en attente.")
            return
        n = len(prop.changements)
        # Le SEUL chemin vers une écriture : la question du code et un « oui » explicite.
        if not self._confirm(f"J'écris ces {n} modification(s) dans {d.racine} ? (une copie des fichiers d'origine "
                             "est gardée pour pouvoir annuler)"):
            if getattr(self, "_carry", None) is None:
                self._out("Rien n'a été écrit dans le dossier. /appliquer pour le faire plus tard, ou "
                          "redemande-moi autre chose.")
            return
        try:
            done = appliquer(prop, d.racine, d.sauvegardes, interdits=self._protected_dirs)
        except DossierError as exc:
            d.proposition = None
            self._out(f"Je n'ai rien écrit : {exc}")
            return
        d.proposition = None
        d.inv = inventorier(d.racine, self._dossier_limites, exclus=self._protected_dirs)
        self._out(f"C'est fait : {len(done.fichiers)} fichier(s) écrit(s) ({', '.join(done.fichiers[:6])}"
                  f"{' …' if len(done.fichiers) > 6 else ''}). Originaux gardés dans {done.manifeste.parent}. "
                  "/annuler pour revenir en arrière.")
        self._history_note(f"Modifications appliquées dans « {d.inv.nom} » : {', '.join(done.fichiers[:6])}.")

    def _undo_last(self) -> None:
        d = self._need_folder()
        if d is None:
            return
        m = derniere_application(d.sauvegardes)
        if m is None:
            self._out("Rien à annuler dans ce dossier.")
            return
        data = lire_manifeste(m)
        files = [f["chemin"] for f in data["fichiers"]]
        when = data.get("date", "")[:16].replace("T", " ")
        listing = "\n".join(f"  • {c}" + ("" if f["existait"] else " (créé : sera retiré)")
                            for c, f in zip(files, data["fichiers"]))
        if not self._confirm(f"Je remets ces fichiers comme avant la modification du {when} (UTC) "
                             f"« {short(data.get('consigne', ''), 120)} » ?\n{listing}\n"):
            if getattr(self, "_carry", None) is None:
                self._out("D'accord, je ne touche à rien.")
            return
        try:
            restored, skipped = annuler(m, d.racine)
        except (DossierError, OSError) as exc:
            self._out(f"Annulation impossible : {exc}")
            return
        d.inv = inventorier(d.racine, self._dossier_limites, exclus=self._protected_dirs)
        msg = f"C'est annulé : {len(restored)} fichier(s) remis comme avant."
        if skipped:
            msg += (f" Laissés tels quels car modifiés depuis (ou supprimés) : {', '.join(skipped)}.")
        self._out(msg)
        self._history_note(msg)

    # ------------------------------------------------------------ fermeture

    def _close_folder(self, *, quiet: bool = False) -> None:
        d = self.dossier
        if d is None:
            if not quiet:
                self._out("Aucun dossier ouvert.")
            return
        router = self._dossier_router()
        if router is not None:
            router.revoke_session_consent("dossier")
        pending = d.proposition is not None
        self.dossier = None
        was_local = self.dossier_local
        self.dossier_local = False
        if was_local and getattr(self, "_history", None) is not None:
            self._history.clear()  # échanges sur un dossier resté local : jamais envoyés ensuite
        if not quiet:
            self._out(f"Dossier « {d.inv.nom} » fermé." + (" Les modifications proposées sont abandonnées (rien n'a "
                                                           "été écrit)." if pending else ""))


def protected_dirs(*dirs: Optional[Path]) -> List[Path]:
    """Dossiers du programme jamais inventoriés ni modifiés (état, journaux)."""
    return [Path(d) for d in dirs if d is not None]


PickerFn = Callable[[], Optional[str]]

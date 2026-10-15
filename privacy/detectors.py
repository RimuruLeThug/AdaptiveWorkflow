"""Détection déterministe des données identifiantes (brief, section 7, étape 2).

Principe : en matière de données personnelles, un faux positif (masquer un
nombre anodin) coûte peu ; un faux négatif (laisser passer un IBAN) est grave.
Les détecteurs sont donc larges : un IBAN ou un NIR à la clé invalide est
masqué quand même.

Détecteurs : emails, téléphones (FR et internationaux), IBAN, numéro de
sécurité sociale (NIR), SIRET, SIREN, adresses postales simples, codes postaux,
valeurs déclarées par le profil (noms, prénoms... des champs personnels), noms
précédés d'une civilité, motifs « Prénom NOM », et noms trouvés par spaCy
(modèle français) s'il est installé.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Sequence


@dataclass(frozen=True)
class Finding:
    kind: str
    start: int
    end: int
    value: str


TOKEN_RE = re.compile(r"\[[A-Z]+(?:_[A-Z]+)*_\d+\]")  # jetons posés par le masquage : jamais re-détectés

_UP = "A-ZÀÂÄÇÉÈÊËÎÏÔÖÙÛÜŸ"
_LOW = "a-zàâäçéèêëîïôöùûüÿ"
_NAME_WORD = rf"[{_UP}][{_LOW}]+(?:[-'’][{_UP}]?[{_LOW}]+)*"
_CAPS_WORD = rf"[{_UP}](?:[{_UP}'’-]*[{_UP}])+"  # DUPONT, N'DIAYE, MARTIN-LEROY (2 lettres ou plus)

PATTERNS: Dict[str, re.Pattern] = {
    "EMAIL": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "IBAN": re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,4})?\b"),
    # NIR : sexe, année, mois, département (dont 2A/2B), commune, ordre, clé optionnelle.
    "NIR": re.compile(r"\b[12][ .]?\d{2}[ .]?(?:0[1-9]|1[0-2]|[2-9]\d)[ .]?(?:\d{2}|2[AB])[ .]?\d{3}[ .]?\d{3}(?:[ .]?\d{2})?\b"),
    "SIRET": re.compile(r"\b\d{3}[ .]?\d{3}[ .]?\d{3}[ .]?\d{5}\b"),
    "TELEPHONE": re.compile(r"(?<![\w+])(?:(?:\+|00)\d{2,3}[ .-]?(?:\(0\)[ .-]?)?[1-9]|0[1-9])(?:[ .-]?\d{2}){4}\b"),
    "SIREN": re.compile(r"\b\d{3}[ .]?\d{3}[ .]?\d{3}\b"),
    "ADRESSE": re.compile(
        r"\b\d{1,4}(?:[ ]?(?:bis|ter))?,?[ ]+(?:rue|avenue|av\.|boulevard|bd|chemin|impasse|allée|allee|place|"
        r"route|quai|cours|square|lieu-dit|résidence|residence)\b[ ]+[^\n,;.\d]{0,40}[^\s,;.\d]",
        re.IGNORECASE),
    "CODE_POSTAL": re.compile(r"\b(?:0[1-9]|[1-8]\d|9[0-8])\d{3}\b"),
    # Date de naissance uniquement (les autres dates restent : échéances, délais...).
    "NAISSANCE": re.compile(
        r"\b(?:née?s?|date de naissance|naissance)\b\s*(?:le|:|en)?\s*"
        r"(\d{1,2}(?:er)?[ /.-](?:\d{1,2}|janvier|février|fevrier|mars|avril|mai|juin|juillet|août|aout|"
        r"septembre|octobre|novembre|décembre|decembre)[ /.-]\d{2,4})",
        re.IGNORECASE),
    "PERSONNE_CIVILITE": re.compile(
        rf"\b(?:M\.|Mme|Mlle|Monsieur|Madame|Mademoiselle|Dr|Docteur|Me|Maître|Pr)\.?[ ]+((?:{_NAME_WORD}|{_CAPS_WORD})(?:[ ]+(?:{_NAME_WORD}|{_CAPS_WORD})){{0,2}})(?!\w)"),
    # « Prénom NOM » et noms composés en majuscules : « Hervé LE GALL », « Inès N'DIAYE ».
    "PERSONNE_NOM_MAJ": re.compile(rf"\b{_NAME_WORD}(?:[ ]+{_CAPS_WORD})+(?!\w)"),
}

# Ordre de priorité en cas de chevauchement de même longueur.
PRIORITY = ["EMAIL", "IBAN", "NIR", "SIRET", "TELEPHONE", "SIREN", "ADRESSE", "NAISSANCE", "DECLARE", "PERSONNE",
            "CODE_POSTAL", "INDICE"]


def iban_valid(value: str) -> bool:
    """Contrôle mod 97 (utilisé pour information : un IBAN invalide est masqué quand même)."""
    s = value.replace(" ", "").upper()
    if len(s) < 15:
        return False
    rearranged = s[4:] + s[:4]
    digits = "".join(str(int(c, 36)) for c in rearranged)
    return int(digits) % 97 == 1


def luhn_valid(number: str) -> bool:
    digits = [int(c) for c in number if c.isdigit()]
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2:
            d *= 2
            d -= 9 if d > 9 else 0
        total += d
    return total % 10 == 0


def _norm(s: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", s).casefold().split())


NameDetector = Callable[[str], List[Finding]]


def spacy_name_detector() -> Optional[NameDetector]:
    """Détecteur de noms spaCy (modèle français), ou None s'il n'est pas installé."""
    try:
        import spacy  # type: ignore
    except ImportError:
        return None
    for model in ("fr_core_news_lg", "fr_core_news_md", "fr_core_news_sm"):
        try:
            nlp = spacy.load(model)
            break
        except OSError:
            continue
    else:
        return None

    def detect(text: str) -> List[Finding]:
        return [Finding("PERSONNE", e.start_char, e.end_char, e.text) for e in nlp(text).ents if e.label_ == "PER"]

    return detect


def detect(text: str, *, declared: Iterable[str] = (), name_detector: Optional[NameDetector] = None) -> List[Finding]:
    """Toutes les données identifiantes du texte, sans chevauchement, triées par position."""
    found: List[Finding] = []
    protected = [(m.start(), m.end()) for m in TOKEN_RE.finditer(text)]

    def add(kind: str, start: int, end: int) -> None:
        if start >= end or any(s < end and start < e for s, e in protected):
            return
        found.append(Finding(kind, start, end, text[start:end]))

    for kind, pattern in PATTERNS.items():
        for m in pattern.finditer(text):
            if kind == "PERSONNE_CIVILITE":
                add("PERSONNE", m.start(1), m.end(1))
            elif kind == "NAISSANCE":
                add("NAISSANCE", m.start(1), m.end(1))
            elif kind == "PERSONNE_NOM_MAJ":
                add("PERSONNE", m.start(), m.end())
            else:
                add(kind, m.start(), m.end())

    # Valeurs déclarées (champs personnels du profil) : toutes leurs occurrences.
    for value in sorted({v.strip() for v in declared if v and len(v.strip()) >= 2}, key=len, reverse=True):
        for m in re.finditer(rf"(?<!\w){re.escape(value)}(?!\w)", text, re.IGNORECASE):
            add("DECLARE", m.start(), m.end())

    if name_detector is not None:
        for f in name_detector(text):
            add("PERSONNE", f.start, f.end)

    return _resolve_overlaps(found)


def _resolve_overlaps(found: List[Finding]) -> List[Finding]:
    rank = {k: i for i, k in enumerate(PRIORITY)}
    # La plus longue d'abord, puis la plus prioritaire.
    ordered = sorted(found, key=lambda f: (-(f.end - f.start), rank.get(f.kind, 99), f.start))
    kept: List[Finding] = []
    for f in ordered:
        if all(f.end <= k.start or f.start >= k.end for k in kept):
            kept.append(f)
    return sorted(kept, key=lambda f: f.start)

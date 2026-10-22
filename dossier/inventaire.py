"""Choix et inventaire d'un dossier du PC, en LECTURE SEULE.

Aucune fonction de ce module n'écrit dans le dossier (ouvertures en mode « rb » seulement) :
l'écriture est réservée à dossier/modifications.appliquer(), après le « oui » explicite de
l'utilisateur. Les fichiers sensibles (.env, clés privées, identifiants...) sont listés mais
jamais lus. Les dossiers techniques (.git, node_modules...) et les motifs simples du
.gitignore de la racine sont ignorés ; les liens symboliques ne sont jamais suivis.
"""

from __future__ import annotations

import fnmatch
import hashlib
import html
import os
import re
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


class DossierError(Exception):
    """Dossier ou chemin refusé par le code (message affichable tel quel)."""


@dataclass(frozen=True)
class Limites:
    max_fichiers: int = 5000  # fichiers parcourus au plus (au-delà : inventaire partiel)
    max_octets_fichier: int = 300_000  # fichier texte plus gros : listé, jamais lu ni modifié
    budget_caracteres: int = 24_000  # contenu de fichiers envoyé au plus par appel au rôle « dossier »


# Dossiers techniques jamais parcourus.
IGNORED_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", ".tox", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".next", ".nuxt", ".cache", ".gradle", ".idea", ".terraform",
    "$RECYCLE.BIN", "System Volume Information",
})

# Fichiers sensibles : listés, JAMAIS lus, envoyés ni modifiés.
SENSITIVE_PATTERNS = (
    ".env", ".env.*", "*.env", "*.pem", "*.key", "*.p12", "*.pfx", "*.kdbx", "*.keystore", "*.jks", "*.ppk",
    "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*", "*.secret", "*secret*.json", "*secret*.yml",
    "*secret*.yaml", "*secret*.toml", "secrets.*", "credentials*", ".npmrc", ".pypirc", ".netrc", "_netrc",
    ".git-credentials", "*password*", "*passwd*", "*mot_de_passe*", "*motdepasse*", "coffre.bin", "*.sqlite",
    "*.db", "*.pst", "*.ost", "wallet.dat",
)
SENSITIVE_ALLOWED = frozenset({".env.example", ".env.sample", ".env.template", ".env.dist"})

BINARY_EXT = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".tif", ".tiff", ".heic", ".psd", ".svgz",
    ".mp3", ".wav", ".flac", ".ogg", ".m4a", ".mp4", ".mkv", ".avi", ".mov", ".webm", ".zip", ".7z", ".rar",
    ".gz", ".tgz", ".bz2", ".xz", ".tar", ".exe", ".dll", ".so", ".dylib", ".bin", ".msi", ".iso", ".img",
    ".pyc", ".pyo", ".class", ".jar", ".o", ".obj", ".lib", ".a", ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".doc", ".xls", ".ppt", ".odp", ".ods", ".rtf", ".lnk", ".dat", ".pkl", ".npy", ".parquet", ".onnx",
    ".gguf", ".safetensors", ".pt", ".pth",
})
# Documents dont le texte peut être EXTRAIT (lecture seule, jamais modifiés).
DOCUMENT_EXT = {".docx": "Word", ".pptx": "PowerPoint", ".xlsx": "Excel", ".odt": "OpenDocument", ".pdf": "PDF"}

# Marqueurs de nature du projet, repérés par le code.
MARKERS = (
    ("pyproject.toml", "projet Python"), ("requirements.txt", "projet Python"), ("setup.py", "projet Python"),
    ("package.json", "projet JavaScript / Node"), ("Cargo.toml", "projet Rust"), ("go.mod", "projet Go"),
    ("pom.xml", "projet Java (Maven)"), ("build.gradle", "projet Java / Kotlin (Gradle)"),
    ("composer.json", "projet PHP"), ("Gemfile", "projet Ruby"), ("CMakeLists.txt", "projet C / C++ (CMake)"),
    ("Makefile", "Makefile"), ("Dockerfile", "conteneur Docker"), ("index.html", "site web"),
    ("pubspec.yaml", "projet Flutter"), ("AndroidManifest.xml", "application Android"),
)

_WINDOWS_RESERVED = re.compile(r"^(?:con|prn|aux|nul|com\d|lpt\d)(?:\..*)?$", re.IGNORECASE)
_BAD_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')


@dataclass(frozen=True)
class Entree:
    chemin: str  # relatif à la racine, séparateur « / »
    taille: int
    nature: str  # texte | document | binaire | sensible | trop_gros

    @property
    def extension(self) -> str:
        return os.path.splitext(self.chemin)[1].lower()

    @property
    def lisible(self) -> bool:
        return self.nature in ("texte", "document")


@dataclass(frozen=True)
class Lecture:
    """Contenu d'un fichier texte tel que lu : de quoi le réécrire à l'identique."""

    chemin: str
    texte: str  # fins de ligne normalisées en « \n »
    encodage: str  # utf-8, utf-8-sig, cp1252, latin-1
    fin_ligne: str  # « \r\n » ou « \n » (dominante dans le fichier)
    empreinte: str  # sha256 des octets lus


@dataclass
class Inventaire:
    racine: Path
    entrees: List[Entree] = field(default_factory=list)
    nb_dossiers: int = 0
    ignores: List[str] = field(default_factory=list)  # dossiers ignorés (chemins relatifs)
    liens: int = 0  # liens symboliques non suivis
    illisibles: int = 0  # entrées refusées par le système (droits)
    partiel: bool = False

    def __post_init__(self) -> None:
        self._index: Dict[str, Entree] = {}

    def _reindex(self) -> None:
        self._index = {e.chemin.casefold(): e for e in self.entrees}

    def get(self, chemin: str) -> Optional[Entree]:
        if len(self._index) != len(self.entrees):
            self._reindex()
        return self._index.get(chemin.replace("\\", "/").strip("/").casefold())

    @property
    def nom(self) -> str:
        return self.racine.name or str(self.racine)

    def de_nature(self, *natures: str) -> List[Entree]:
        return [e for e in self.entrees if e.nature in natures]

    def taille_totale(self) -> int:
        return sum(e.taille for e in self.entrees)

    def natures_projet(self) -> List[str]:
        names = {e.chemin.rsplit("/", 1)[-1].casefold(): e for e in self.entrees}
        found: List[str] = []
        for marker, label in MARKERS:
            if marker.casefold() in names and label not in found:
                found.append(label)
        if any(e.extension in (".csproj", ".sln") for e in self.entrees):
            found.append("projet .NET")
        docs = self.de_nature("document")
        if docs and len(docs) >= max(3, len(self.entrees) // 3):
            found.append("documents bureautiques")
        images = [e for e in self.entrees if e.extension in (".png", ".jpg", ".jpeg", ".heic", ".gif", ".webp")]
        if images and len(images) >= max(5, len(self.entrees) // 2):
            found.append("photos / images")
        if (self.racine / ".git").is_dir():
            found.append("dépôt git")
        return found

    def resume_code(self) -> str:
        """Résumé établi par le CODE seul (aucun modèle) : affiché et transmis au modèle."""
        n = len(self.entrees)
        lines = [f"{n} fichier(s), {taille_lisible(self.taille_totale())}, dans {self.nb_dossiers} sous-dossier(s)"
                 + (" — inventaire PARTIEL (trop de fichiers)" if self.partiel else "") + "."]
        texts = self.de_nature("texte")
        if texts:
            lines.append(f"Texte / code : {len(texts)} ({_top_ext(texts)}).")
        for nature, label in (("document", "Documents (texte extrait, jamais modifiés)"),
                              ("binaire", "Binaires / médias (non lus)"),
                              ("trop_gros", "Trop gros pour être lus")):
            items = self.de_nature(nature)
            if items:
                lines.append(f"{label} : {len(items)} ({_top_ext(items)}).")
        sens = self.de_nature("sensible")
        if sens:
            shown = ", ".join(e.chemin for e in sens[:6]) + (" …" if len(sens) > 6 else "")
            lines.append(f"Sensibles, JAMAIS lus ni envoyés : {len(sens)} ({shown}).")
        natures = self.natures_projet()
        if natures:
            lines.append("Nature probable : " + ", ".join(natures) + ".")
        if self.ignores:
            shown = ", ".join(self.ignores[:8]) + (" …" if len(self.ignores) > 8 else "")
            lines.append(f"Ignorés (techniques ou .gitignore) : {shown}.")
        if self.liens:
            lines.append(f"Liens symboliques non suivis : {self.liens}.")
        if self.illisibles:
            lines.append(f"Inaccessibles (droits) : {self.illisibles}.")
        return "\n".join(lines)

    def arbre(self, max_lignes: int = 300) -> str:
        """Liste des fichiers (chemin, taille, nature) ; au-delà de max_lignes, les plus profonds
        sont résumés par dossier."""
        ordered = sorted(self.entrees, key=lambda e: (e.chemin.count("/"), e.chemin.casefold()))
        shown = ordered[:max_lignes]
        rest = ordered[max_lignes:]
        tags = {"texte": "", "document": " (document, texte extrait)", "binaire": " (binaire)",
                "sensible": " (SENSIBLE : jamais lu)", "trop_gros": " (trop gros, non lu)"}
        lines = [f"{e.chemin} — {taille_lisible(e.taille)}{tags.get(e.nature, '')}"
                 for e in sorted(shown, key=lambda e: e.chemin.casefold())]
        if rest:
            by_dir = Counter(e.chemin.rsplit("/", 1)[0] if "/" in e.chemin else "." for e in rest)
            lines.append(f"… et {len(rest)} autre(s) fichier(s) : "
                         + ", ".join(f"{d}/ ({c})" for d, c in by_dir.most_common(12)))
        return "\n".join(lines)


def taille_lisible(n: int) -> str:
    if n < 1024:
        return f"{n} o"
    for unit in ("Ko", "Mo", "Go"):
        n /= 1024
        if n < 1024 or unit == "Go":
            return f"{n:.1f} {unit}".replace(".", ",")
    return f"{n} o"


def _top_ext(items: Sequence[Entree], n: int = 5) -> str:
    c = Counter(e.extension or "(sans extension)" for e in items)
    return ", ".join(f"{ext} ×{k}" for ext, k in c.most_common(n)) + (" …" if len(c) > n else "")


# ------------------------------------------------------------ choix du dossier

FOLDER_ALIASES = {
    "bureau": ("Desktop", "Bureau", "OneDrive/Bureau", "OneDrive/Desktop"),
    "desktop": ("Desktop", "OneDrive/Desktop"),
    "documents": ("Documents", "OneDrive/Documents"),
    "mes documents": ("Documents", "OneDrive/Documents"),
    "téléchargements": ("Downloads", "Téléchargements"),
    "telechargements": ("Downloads", "Téléchargements"),
    "downloads": ("Downloads",),
    "images": ("Pictures", "Images", "OneDrive/Images"),
    "musique": ("Music", "Musique"),
    "vidéos": ("Videos", "Vidéos"),
}


def _forbidden_roots(home: Path) -> List[Path]:
    roots = [Path("/"), home]
    for var in ("SystemRoot", "windir", "ProgramFiles", "ProgramFiles(x86)", "ProgramData", "APPDATA",
                "LOCALAPPDATA"):
        v = os.environ.get(var)
        if v:
            roots.append(Path(v))
    roots += [Path(p) for p in ("/bin", "/boot", "/dev", "/etc", "/lib", "/proc", "/sbin", "/sys", "/usr", "/var",
                                "/System", "/Library", "/Applications")]
    roots += [home / "AppData", home / "Library"]
    return roots


def check_folder(path: Path, *, home: Path) -> Path:
    """Dossier accepté pour l'analyse (résolu) ; DossierError sinon."""
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise DossierError(f"dossier introuvable : {path}") from None
    if not resolved.is_dir():
        raise DossierError(f"ce n'est pas un dossier : {resolved}")
    if resolved.parent == resolved or re.fullmatch(r"[A-Za-z]:\\?", str(resolved)):
        raise DossierError(f"{resolved} est la racine d'un disque : choisis un dossier plus précis.")
    for root in _forbidden_roots(home):
        try:
            same = resolved == root.resolve()
        except OSError:
            continue
        if same:
            what = "ton dossier personnel entier" if root == home else "un dossier système"
            raise DossierError(f"{resolved} est {what} : choisis un dossier plus précis (un projet, un dossier "
                               "de documents...).")
    sysroot = os.environ.get("SystemRoot") or os.environ.get("windir")
    if sysroot:
        try:
            resolved.relative_to(Path(sysroot).resolve())
            raise DossierError(f"{resolved} fait partie de Windows : je n'y travaille pas.")
        except ValueError:
            pass
    return resolved


def resolve_folder(raw: str, *, home: Path, cwd: Optional[Path] = None) -> Path:
    """Chemin tapé ou dit par l'utilisateur -> dossier existant. DossierError si introuvable.

    Accepte un chemin absolu, ~, les variables d'environnement, et un chemin relatif au dossier
    personnel, au Bureau, aux Documents (« Bureau/MonProjet », « MonProjet »...)."""
    text = (raw or "").strip().strip("\"'«» ").strip()
    if not text:
        raise DossierError("aucun chemin donné.")
    text = os.path.expandvars(os.path.expanduser(text))
    p = Path(text)
    candidates: List[Path] = []
    if p.is_absolute() or re.match(r"^[A-Za-z]:[\\/]", text):
        candidates.append(p)
    else:
        parts = re.split(r"[\\/]+", text)
        head, tail = parts[0].casefold(), parts[1:]
        for alias in FOLDER_ALIASES.get(head, ()):
            candidates.append(home.joinpath(*alias.split("/"), *tail))
        for base in (home, home / "Desktop", home / "Bureau", home / "OneDrive" / "Bureau", home / "OneDrive" / "Desktop",
                     home / "Documents", home / "OneDrive" / "Documents", home / "Downloads", cwd or Path.cwd()):
            candidates.append(base / text)
    for c in candidates:
        if c.is_dir():
            return c
    raise DossierError(f"je ne trouve pas le dossier « {raw.strip()} ».")


def find_folders(name: str, *, home: Path, limit: int = 5) -> List[Path]:
    """Dossiers portant ce nom (insensible à la casse) près du dossier personnel (profondeur 2)."""
    target = name.strip().strip("\"'«» ").casefold()
    if not target or re.search(r"[\\/]", target):
        return []
    found: List[Path] = []
    bases = [home / b for b in ("Desktop", "Bureau", "Documents", "Downloads", "OneDrive", "OneDrive/Bureau",
                                "OneDrive/Desktop", "OneDrive/Documents")] + [home]
    seen = set()
    for base in bases:
        if not base.is_dir():
            continue
        for depth_dir in _dirs_up_to(base, 2):
            if depth_dir.name.casefold() == target:
                key = str(depth_dir.resolve()).casefold()
                if key not in seen:
                    seen.add(key)
                    found.append(depth_dir)
                    if len(found) >= limit:
                        return found
    return found


def _dirs_up_to(base: Path, depth: int) -> Iterable[Path]:
    level = [base]
    for _ in range(depth):
        nxt = []
        for d in level:
            try:
                children = sorted((c for c in d.iterdir() if c.is_dir() and not c.is_symlink()
                                   and not c.name.startswith(".") and c.name not in IGNORED_DIRS
                                   and c.name != "AppData"), key=lambda c: c.name.casefold())
            except OSError:
                continue
            yield from children
            nxt.extend(children)
        level = nxt[:200]


# ------------------------------------------------------------------ inventaire


@dataclass(frozen=True)
class _Rule:
    pattern: str
    dir_only: bool
    anchored: bool


def gitignore_rules(root: Path) -> List[_Rule]:
    """Motifs simples du .gitignore de la racine (les négations « ! » sont ignorées : on ignore plus)."""
    path = root / ".gitignore"
    try:
        raw = path.read_bytes() if path.is_file() and not path.is_symlink() else b""
    except OSError:
        return []
    rules = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        dir_only = line.endswith("/")
        core = line.strip("/")
        if not core:
            continue
        rules.append(_Rule(core, dir_only, line.startswith("/") or "/" in core))
    return rules


def _ignored(rel: str, is_dir: bool, rules: Sequence[_Rule]) -> bool:
    name = rel.rsplit("/", 1)[-1]
    for r in rules:
        if r.dir_only and not is_dir:
            continue
        if r.anchored:
            if fnmatch.fnmatchcase(rel, r.pattern):
                return True
        elif fnmatch.fnmatchcase(name, r.pattern):
            return True
    return False


def est_sensible(name: str) -> bool:
    low = name.casefold()
    if low in SENSITIVE_ALLOWED:
        return False
    return any(fnmatch.fnmatchcase(low, p) for p in SENSITIVE_PATTERNS)


def _sniff_text(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            head = f.read(8192)
    except OSError:
        return False
    if b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
        return True
    except UnicodeDecodeError as exc:
        if exc.start >= len(head) - 4:  # caractère coupé par la limite de lecture
            return True
    try:
        head.decode("cp1252")
        return True
    except UnicodeDecodeError:
        return False


def inventorier(root: Path, limites: Limites = Limites(), *, exclus: Sequence[Path] = ()) -> Inventaire:
    """Parcourt le dossier SANS RIEN MODIFIER (aucun suivi de lien symbolique)."""
    root = Path(root).resolve()
    inv = Inventaire(root)
    rules = gitignore_rules(root)
    excluded = []
    for e in exclus:
        try:
            excluded.append(Path(e).resolve())
        except OSError:
            continue

    def onerror(_exc: OSError) -> None:
        inv.illisibles += 1

    for dirpath, dirnames, filenames in os.walk(root, topdown=True, onerror=onerror, followlinks=False):
        d = Path(dirpath)
        rel_dir = d.relative_to(root).as_posix()
        rel_dir = "" if rel_dir == "." else rel_dir
        keep = []
        for name in sorted(dirnames, key=str.casefold):
            rel = f"{rel_dir}/{name}" if rel_dir else name
            full = d / name
            if full.is_symlink():
                inv.liens += 1
                continue
            if name in IGNORED_DIRS or _ignored(rel, True, rules) or any(_same(full, x) for x in excluded):
                inv.ignores.append(rel + "/")
                continue
            keep.append(name)
        dirnames[:] = keep
        inv.nb_dossiers += len(keep)
        for name in sorted(filenames, key=str.casefold):
            if len(inv.entrees) >= limites.max_fichiers:
                inv.partiel = True
                dirnames[:] = []
                break
            rel = f"{rel_dir}/{name}" if rel_dir else name
            full = d / name
            try:
                st = full.lstat()
            except OSError:
                inv.illisibles += 1
                continue
            if full.is_symlink():
                inv.liens += 1
                continue
            if est_sensible(name):
                inv.entrees.append(Entree(rel, st.st_size, "sensible"))
                continue
            if _ignored(rel, False, rules):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext in DOCUMENT_EXT:
                nature = "document" if st.st_size <= 20 * limites.max_octets_fichier else "trop_gros"
            elif ext in BINARY_EXT:
                nature = "binaire"
            elif st.st_size > limites.max_octets_fichier:
                nature = "trop_gros"
            else:
                nature = "texte" if _sniff_text(full) else "binaire"
            inv.entrees.append(Entree(rel, st.st_size, nature))
    inv._reindex()
    return inv


def _same(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b
    except OSError:
        return False


# ----------------------------------------------------------------- lecture


def chemin_sur(root: Path, chemin: str) -> str:
    """Chemin relatif proposé (par un modèle ou l'utilisateur) -> forme normalisée « a/b.txt ».

    Refuse tout ce qui sortirait du dossier ou viserait un fichier sensible ou technique :
    chemin absolu, lecteur, « .. », caractères interdits sous Windows, lien symbolique."""
    c = (chemin or "").strip().replace("\\", "/")
    while c.startswith("./"):
        c = c[2:]
    if not c or c.startswith("/") or re.match(r"^[A-Za-z]:", c):
        raise DossierError(f"chemin refusé (absolu ou vide) : « {chemin} »")
    parts = c.split("/")
    for p in parts:
        if p in ("", ".", "..") or _BAD_CHARS.search(p) or _WINDOWS_RESERVED.match(p) or p != p.rstrip(". "):
            raise DossierError(f"chemin refusé : « {chemin} »")
        if p in IGNORED_DIRS:
            raise DossierError(f"chemin refusé (dossier technique {p}) : « {chemin} »")
    if est_sensible(parts[-1]):
        raise DossierError(f"fichier sensible, jamais lu ni modifié : « {chemin} »")
    root = Path(root).resolve()
    cur = root
    for p in parts:
        cur = cur / p
        if cur.is_symlink():
            raise DossierError(f"chemin refusé (lien symbolique) : « {chemin} »")
    try:
        (root / Path(*parts)).resolve().relative_to(root)
    except ValueError:
        raise DossierError(f"chemin refusé (hors du dossier) : « {chemin} »") from None
    return "/".join(parts)


def decoder(raw: bytes) -> Tuple[str, str]:
    """(texte, encodage) : UTF-8 (avec ou sans BOM), sinon Windows-1252, sinon Latin-1."""
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", errors="strict"), "utf-8-sig"
    for enc in ("utf-8", "cp1252"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1"), "latin-1"


def fin_de_ligne(raw: bytes, defaut: str = "\n") -> str:
    crlf = raw.count(b"\r\n")
    lf = raw.count(b"\n") - crlf
    if not crlf and not lf:
        return defaut
    return "\r\n" if crlf >= lf else "\n"


def empreinte(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def lire(inv: Inventaire, chemin: str, *, defaut_fin: str = "\n") -> Lecture:
    """Lit un fichier TEXTE de l'inventaire (mode « rb » : rien n'est modifié)."""
    rel = chemin_sur(inv.racine, chemin)
    e = inv.get(rel)
    if e is None:
        raise DossierError(f"fichier absent de l'inventaire : « {chemin} »")
    if e.nature != "texte":
        raise DossierError(f"« {e.chemin} » n'est pas un fichier texte lisible ({e.nature})")
    with open(inv.racine / Path(*e.chemin.split("/")), "rb") as f:
        raw = f.read()
    text, enc = decoder(raw)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return Lecture(e.chemin, text, enc, fin_de_ligne(raw, defaut_fin), empreinte(raw))


_XML_TEXT = {
    ".docx": ("word/document.xml", r"<w:t(?:\s[^>]*)?>([^<]*)</w:t>", r"</w:p>"),
    ".odt": ("content.xml", r">([^<]+)<", r"</text:p>|</text:h>"),
}
_MAX_XML = 20_000_000


def extraire_document(inv: Inventaire, chemin: str, max_caracteres: int = 20_000) -> str:
    """Texte d'un document Word, PowerPoint, Excel, OpenDocument ou PDF (lecture seule).

    Lève DossierError si l'extraction est impossible (format abîmé, PDF sans pypdf...)."""
    e = inv.get(chemin)
    if e is None or e.nature != "document":
        raise DossierError(f"« {chemin} » n'est pas un document lisible")
    path = inv.racine / Path(*e.chemin.split("/"))
    ext = e.extension
    try:
        if ext == ".pdf":
            try:
                from pypdf import PdfReader  # facultatif
            except ImportError:
                raise DossierError("lecture des PDF : installe « pypdf » (pip install pypdf)") from None
            with open(path, "rb") as f:
                reader = PdfReader(f)
                out, total = [], 0
                for page in reader.pages:
                    t = page.extract_text() or ""
                    out.append(t)
                    total += len(t)
                    if total >= max_caracteres:
                        break
            return "\n".join(out)[:max_caracteres]
        with zipfile.ZipFile(path) as z:
            if ext in _XML_TEXT:
                member, text_re, para_re = _XML_TEXT[ext]
                xml = _zip_read(z, member)
                paras = [" ".join(html.unescape(t) for t in re.findall(text_re, p))
                         for p in re.split(para_re, xml)]
                return "\n".join(p.strip() for p in paras if p.strip())[:max_caracteres]
            if ext == ".pptx":
                slides = sorted((n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                                key=lambda n: int(re.findall(r"\d+", n)[-1]))
                out = []
                for i, n in enumerate(slides, 1):
                    words = [html.unescape(t) for t in re.findall(r"<a:t>([^<]*)</a:t>", _zip_read(z, n))]
                    out.append(f"[diapo {i}] " + " ".join(words))
                return "\n".join(out)[:max_caracteres]
            if ext == ".xlsx":
                try:
                    xml = _zip_read(z, "xl/sharedStrings.xml")
                except KeyError:
                    return ""
                cells = [html.unescape("".join(re.findall(r"<t(?:\s[^>]*)?>([^<]*)</t>", si)))
                         for si in re.findall(r"<si>(.*?)</si>", xml, re.S)]
                return "\n".join(c for c in cells if c.strip())[:max_caracteres]
    except (OSError, zipfile.BadZipFile, KeyError, ValueError) as exc:
        raise DossierError(f"texte illisible dans « {e.chemin} » ({exc})") from None
    except Exception as exc:  # pypdf : erreurs propres à la bibliothèque
        raise DossierError(f"texte illisible dans « {e.chemin} » ({type(exc).__name__})") from None
    raise DossierError(f"format non pris en charge : {ext}")


def _zip_read(z: zipfile.ZipFile, member: str) -> str:
    info = z.getinfo(member)
    if info.file_size > _MAX_XML:  # archive piégée (« zip bomb ») : jamais décompressée
        raise ValueError("contenu trop volumineux")
    with z.open(info) as f:
        return f.read(_MAX_XML).decode("utf-8", errors="replace")

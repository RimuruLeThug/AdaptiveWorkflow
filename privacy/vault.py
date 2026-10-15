"""Coffre local jeton → valeur réelle.

- local, chiffré, jamais transmis : le fichier n'est lu que par ce module ;
- un même client garde le même jeton (normalisation de la valeur) ; à la
  réinjection, c'est la forme vue en premier qui revient (« N'DIAYE » pour
  « N'Diaye ») : compromis assumé au profit de la stabilité du jeton ;
- chiffrement :
  * Windows : DPAPI (CryptProtectData), lié au compte Windows, sans dépendance ;
  * ailleurs : Fernet si le paquet « cryptography » est installé (clé locale) ;
  * sinon : refus d'enregistrer sur disque (coffre en mémoire seulement).
- un fichier illisible ou indéchiffrable bloque (pas de remise à zéro silencieuse :
  les jetons existants deviendraient incohérents).
"""

from __future__ import annotations

import json
import os
import sys
import unicodedata
from pathlib import Path
from typing import Dict, Optional, Protocol, Tuple

VAULT_VERSION = 1
NUMERIC_KINDS = {"TELEPHONE", "IBAN", "NIR", "SIRET", "SIREN", "CODE_POSTAL"}


class VaultError(Exception):
    """Coffre indisponible, illisible ou chiffrement absent."""


class Cipher(Protocol):
    name: str

    def encrypt(self, data: bytes) -> bytes: ...

    def decrypt(self, data: bytes) -> bytes: ...


class DPAPICipher:
    """Chiffrement Windows lié au compte de l'utilisateur (DPAPI)."""

    name = "dpapi"

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise VaultError("DPAPI n'existe que sous Windows")
        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

        self._ct, self._BLOB = ctypes, DATA_BLOB
        self._crypt32 = ctypes.windll.crypt32
        self._kernel32 = ctypes.windll.kernel32
        P = ctypes.POINTER(DATA_BLOB)
        self._crypt32.CryptProtectData.argtypes = [P, wintypes.LPCWSTR, P, ctypes.c_void_p, ctypes.c_void_p,
                                                   wintypes.DWORD, P]
        self._crypt32.CryptProtectData.restype = wintypes.BOOL
        self._crypt32.CryptUnprotectData.argtypes = [P, ctypes.c_void_p, P, ctypes.c_void_p, ctypes.c_void_p,
                                                     wintypes.DWORD, P]
        self._crypt32.CryptUnprotectData.restype = wintypes.BOOL
        self._kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        self._kernel32.LocalFree.restype = ctypes.c_void_p

    def _call(self, func, data: bytes, *extra) -> bytes:
        ct = self._ct
        buf = ct.create_string_buffer(data, len(data))
        blob_in = self._BLOB(len(data), ct.cast(buf, ct.POINTER(ct.c_char)))
        blob_out = self._BLOB()
        CRYPTPROTECT_UI_FORBIDDEN = 0x1
        if not func(ct.byref(blob_in), *extra, None, None, None, CRYPTPROTECT_UI_FORBIDDEN, ct.byref(blob_out)):
            raise VaultError(f"DPAPI : échec ({ct.GetLastError()})")
        try:
            return ct.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            self._kernel32.LocalFree(ct.cast(blob_out.pbData, ct.c_void_p))

    def encrypt(self, data: bytes) -> bytes:
        return self._call(self._crypt32.CryptProtectData, data, "orchestrateur-coffre")

    def decrypt(self, data: bytes) -> bytes:
        return self._call(self._crypt32.CryptUnprotectData, data, None)


class FernetCipher:
    name = "fernet"

    def __init__(self, key_path: Path) -> None:
        try:
            from cryptography.fernet import Fernet  # type: ignore
        except ImportError:
            raise VaultError("paquet « cryptography » absent : pip install cryptography") from None
        key_path = Path(key_path)
        if not key_path.exists():
            key_path.parent.mkdir(parents=True, exist_ok=True)
            key_path.write_bytes(Fernet.generate_key())
            try:
                os.chmod(key_path, 0o600)
            except OSError:
                pass
        self._f = Fernet(key_path.read_bytes())

    def encrypt(self, data: bytes) -> bytes:
        return self._f.encrypt(data)

    def decrypt(self, data: bytes) -> bytes:
        return self._f.decrypt(data)


def default_cipher(state_dir: Path) -> Cipher:
    if sys.platform == "win32":
        return DPAPICipher()
    return FernetCipher(Path(state_dir) / "vault.key")


def normalize(kind: str, value: str) -> str:
    v = unicodedata.normalize("NFKC", value).casefold().strip()
    if kind in NUMERIC_KINDS:
        return "".join(c for c in v if c.isalnum())
    if kind == "EMAIL":
        return v
    return " ".join(v.split())


TOKEN_KIND = {"DECLARE": "PERSONNE"}  # une valeur déclarée (nom de client) devient un jeton PERSONNE


class Vault:
    """Correspondance jeton ↔ valeur. path=None : coffre en mémoire (rien sur disque)."""

    def __init__(self, path: Optional[Path] = None, cipher: Optional[Cipher] = None) -> None:
        if path is not None and cipher is None:
            raise VaultError("un coffre enregistré sur disque doit être chiffré")
        self.path = Path(path) if path else None
        self.cipher = cipher
        self._by_value: Dict[Tuple[str, str], str] = {}
        self._by_token: Dict[str, str] = {}
        self._counters: Dict[str, int] = {}
        if self.path and self.path.exists():
            self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.cipher.decrypt(self.path.read_bytes()).decode("utf-8"))
            if raw.get("version") != VAULT_VERSION:
                raise ValueError(f"version {raw.get('version')!r}")
            for kind, norm, token, original in raw["entries"]:
                self._by_value[(kind, norm)] = token
                self._by_token[token] = original
            self._counters = {k: int(v) for k, v in raw["counters"].items()}
        except Exception as exc:  # indéchiffrable, corrompu : on bloque
            raise VaultError(f"coffre illisible ({self.path}) : {type(exc).__name__}: {exc}") from None

    def save(self) -> None:
        if self.path is None:
            return
        entries = [[kind, norm, token, self._by_token[token]] for (kind, norm), token in self._by_value.items()]
        data = json.dumps({"version": VAULT_VERSION, "entries": entries, "counters": self._counters},
                          ensure_ascii=False).encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_bytes(self.cipher.encrypt(data))
        os.replace(tmp, self.path)

    def token_for(self, kind: str, value: str) -> str:
        kind = TOKEN_KIND.get(kind, kind)
        key = (kind, normalize(kind, value))
        if key not in self._by_value:
            n = self._counters.get(kind, 0) + 1
            self._counters[kind] = n
            token = f"[{kind}_{n}]"
            self._by_value[key] = token
            self._by_token[token] = value
        return self._by_value[key]

    def value_for(self, token: str) -> Optional[str]:
        return self._by_token.get(token)

    def originals(self) -> Dict[str, str]:
        return dict(self._by_token)

    def __len__(self) -> int:
        return len(self._by_token)

    def __repr__(self) -> str:  # ne jamais afficher les valeurs
        return f"Vault({len(self)} entrées, chiffrement={getattr(self.cipher, 'name', 'mémoire')})"

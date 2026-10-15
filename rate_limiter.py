"""Limiteur de débit Gemini.

Garanties :
- file unique FIFO pour tous les appels Gemini (organisateur, workers, reviewers) ;
- espacement minimal entre deux appels (5 s par défaut, soit 12 RPM), y compris
  après un redémarrage du programme ;
- 2 retries maximum sur 429 ; chaque tentative repasse par la file (donc par
  l'espacement) et compte dans le budget ; un délai Retry-After fourni par
  l'API est respecté s'il est plus long ;
- compteur journalier persistant, alerte à 1200, arrêt dur à 1400 ;
- remise à zéro à minuit, heure du Pacifique.

Le compteur est incrémenté AVANT l'appel : une requête partie compte, même si
le programme plante pendant qu'elle s'exécute. On préfère sous-utiliser le
quota plutôt que le dépasser.

Limite connue : un seul processus à la fois doit utiliser le fichier d'état.

La concurrence des modèles locaux (VRAM, parallélisme Ollama) n'est pas gérée
ici.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Callable, Optional, TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from config import GeminiQuota

log = logging.getLogger(__name__)
T = TypeVar("T")

STATE_VERSION = 1


class USPacific(tzinfo):
    """Heure du Pacifique (America/Los_Angeles) sans base de fuseaux.

    Repli utilisé quand la base tzdata est absente (cas fréquent sous Windows).
    Règle américaine en vigueur depuis 2007 : heure d'été du 2e dimanche de mars
    à 2 h (10 h UTC) au 1er dimanche de novembre à 2 h (9 h UTC).
    """

    _STD = timedelta(hours=-8)
    _DST = timedelta(hours=-7)

    @staticmethod
    def _nth_sunday(year: int, month: int, n: int) -> datetime:
        first = datetime(year, month, 1)
        offset = (6 - first.weekday()) % 7  # lundi = 0, dimanche = 6
        return first + timedelta(days=offset + 7 * (n - 1))

    def _is_dst_utc(self, utc_naive: datetime) -> bool:
        y = utc_naive.year
        start = self._nth_sunday(y, 3, 2) + timedelta(hours=10)
        end = self._nth_sunday(y, 11, 1) + timedelta(hours=9)
        return start <= utc_naive < end

    def fromutc(self, dt: datetime) -> datetime:
        utc_naive = dt.replace(tzinfo=None)
        offset = self._DST if self._is_dst_utc(utc_naive) else self._STD
        local = utc_naive + offset
        # Heure doublée de novembre : la seconde occurrence porte fold=1 (PEP 495).
        fold = 1 if offset == self._STD and self._is_dst_utc(local - self._DST) else 0
        return local.replace(tzinfo=self, fold=fold)

    def utcoffset(self, dt: Optional[datetime]) -> timedelta:
        if dt is None:
            return self._STD
        local = dt.replace(tzinfo=None)
        dst_ok = self._is_dst_utc(local - self._DST)
        std_ok = not self._is_dst_utc(local - self._STD)
        if dst_ok and std_ok:  # heure ambiguë (novembre)
            return self._DST if dt.fold == 0 else self._STD
        if dst_ok:
            return self._DST
        return self._STD  # heure normale, ou heure inexistante de mars

    def dst(self, dt: Optional[datetime]) -> timedelta:
        return self.utcoffset(dt) - self._STD

    def tzname(self, dt: Optional[datetime]) -> str:
        return "PDT" if self.dst(dt) else "PST"

    def __repr__(self) -> str:
        return "USPacific()"


def get_timezone(name: str) -> tzinfo:
    """ZoneInfo si disponible ; sinon repli intégré pour l'heure du Pacifique."""
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name in ("America/Los_Angeles", "US/Pacific"):
            log.info("Base tzdata absente : calcul intégré de l'heure du Pacifique.")
            return USPacific()
        if name in ("UTC", "Etc/UTC"):
            return timezone.utc
        raise ZoneInfoNotFoundError(
            f"Fuseau « {name} » introuvable. Installez la base : pip install tzdata"
        ) from None


class RateLimiterError(Exception):
    """Base des erreurs du limiteur."""


class DailyQuotaExhausted(RateLimiterError):
    """Arrêt dur atteint : plus aucun appel Gemini jusqu'à minuit (Pacifique)."""


class RetriesExhausted(RateLimiterError):
    """429 persistant après le nombre maximal de retries."""


class CorruptStateError(RateLimiterError):
    """Fichier d'état illisible : le limiteur refuse de deviner le compteur."""


class RateLimited(Exception):
    """À lever par le client Gemini quand l'API répond 429.

    retry_after_s : délai demandé par l'API (en-tête Retry-After), si connu.
    """

    def __init__(self, message: str = "429 Too Many Requests", retry_after_s: Optional[float] = None):
        super().__init__(message)
        self.retry_after_s = retry_after_s


@dataclass
class _State:
    day: str  # date AAAA-MM-JJ dans le fuseau de remise à zéro
    count: int
    last_call_wall: float  # horodatage (time.time) du dernier appel parti


class RateLimiter:
    """File unique, espacement, retries et compteur journalier pour un compartiment Gemini."""

    def __init__(
        self,
        quota: GeminiQuota,
        state_path: Path,
        *,
        wall_clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        on_alert: Optional[Callable[[int], None]] = None,
    ) -> None:
        if quota.min_interval_s < 0 or quota.max_retries < 0:
            raise ValueError("min_interval_s et max_retries doivent être positifs ou nuls")
        if quota.daily_alert > quota.daily_hard_stop:
            raise ValueError("daily_alert doit être inférieur ou égal à daily_hard_stop")
        self.quota = quota
        self.state_path = Path(state_path)
        self._clock = wall_clock
        self._sleep = sleep
        self._on_alert = on_alert
        self._tz = get_timezone(quota.reset_timezone)
        # File FIFO : chaque appelant prend un ticket et attend son tour.
        self._cond = threading.Condition()
        self._next_ticket = 0
        self._serving = 0
        # Délai supplémentaire imposé par un Retry-After.
        self._not_before_wall = 0.0
        self._state = self._load()
        self._roll_day(self._clock())
        if self._state.count >= self.quota.daily_alert:
            log.warning("Quota Gemini déjà à %d/%d aujourd'hui.", self._state.count, self.quota.daily_hard_stop)

    # ------------------------------------------------------------------ état

    def _today(self, wall: float) -> str:
        return datetime.fromtimestamp(wall, tz=self._tz).date().isoformat()

    def _load(self) -> _State:
        if not self.state_path.exists():
            return _State(day=self._today(self._clock()), count=0, last_call_wall=0.0)
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            if raw.get("version") != STATE_VERSION:
                raise ValueError(f"version inconnue : {raw.get('version')!r}")
            state = _State(day=str(raw["day"]), count=int(raw["count"]), last_call_wall=float(raw["last_call_wall"]))
            datetime.strptime(state.day, "%Y-%m-%d")
            if state.count < 0:
                raise ValueError("compteur négatif")
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise CorruptStateError(
                f"Fichier d'état du limiteur illisible ({self.state_path}) : {exc}. "
                "Vérifiez-le, ou supprimez-le en connaissance de cause (le compteur du jour serait perdu)."
            ) from exc
        return state

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_name(self.state_path.name + ".tmp")
        payload = {
            "version": STATE_VERSION,
            "day": self._state.day,
            "count": self._state.count,
            "last_call_wall": self._state.last_call_wall,
        }
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.state_path)  # remplacement atomique, y compris sous Windows

    def _roll_day(self, wall: float) -> None:
        today = self._today(wall)
        if today != self._state.day:
            log.info("Nouveau jour de quota (%s) : compteur remis à zéro (était %d).", today, self._state.count)
            self._state = _State(day=today, count=0, last_call_wall=self._state.last_call_wall)
            self._save()

    # ------------------------------------------------------------- lecture

    @property
    def used_today(self) -> int:
        with self._cond:
            self._roll_day(self._clock())
            return self._state.count

    @property
    def remaining_today(self) -> int:
        return max(0, self.quota.daily_hard_stop - self.used_today)

    # ------------------------------------------------------------ file d'attente

    def acquire(self) -> int:
        """Attend son tour et l'espacement, puis réserve une requête dans le budget.

        Retourne le numéro de la requête dans la journée (1, 2, ...).
        Lève DailyQuotaExhausted si l'arrêt dur est atteint ; rien n'est alors consommé.
        """
        with self._cond:
            ticket = self._next_ticket
            self._next_ticket += 1
            while self._serving != ticket:
                self._cond.wait()
            try:
                return self._acquire_turn()
            finally:
                self._serving += 1
                self._cond.notify_all()

    def _acquire_turn(self) -> int:
        # Appelé avec le verrou et le tour. Pendant l'attente, le verrou est
        # relâché (les autres appelants prennent leur ticket dans l'ordre
        # d'arrivée) mais le tour reste à nous : personne ne passe devant.
        self._roll_day(self._clock())
        self._check_budget()
        while True:
            earliest = max(self._state.last_call_wall + self.quota.min_interval_s, self._not_before_wall)
            wait = earliest - self._clock()
            if wait <= 0:
                break
            self._cond.release()
            try:
                self._sleep(wait)
            finally:
                self._cond.acquire()
        # L'attente a pu franchir minuit : on revérifie le jour et le budget.
        now = self._clock()
        self._roll_day(now)
        self._check_budget()
        self._state.count += 1
        self._state.last_call_wall = now
        self._save()
        n = self._state.count
        if n == self.quota.daily_alert:
            log.warning("Quota Gemini : %d requêtes aujourd'hui (alerte), arrêt à %d.", n, self.quota.daily_hard_stop)
            if self._on_alert:
                self._on_alert(n)
        return n

    def _check_budget(self) -> None:
        if self._state.count >= self.quota.daily_hard_stop:
            raise DailyQuotaExhausted(
                f"Arrêt dur : {self._state.count}/{self.quota.daily_hard_stop} requêtes Gemini aujourd'hui "
                f"({self._state.day}, heure du Pacifique). Reprise après minuit, heure du Pacifique."
            )

    # ------------------------------------------------------------- appel complet

    def call(self, fn: Callable[[], T]) -> T:
        """Exécute fn() à travers la file, avec retries sur RateLimited (429).

        Seule l'exception RateLimited déclenche un retry. Toute autre erreur est
        propagée telle quelle (la tentative reste comptée).
        """
        attempts = 1 + self.quota.max_retries
        last: Optional[RateLimited] = None
        for attempt in range(1, attempts + 1):
            self.acquire()
            try:
                return fn()
            except RateLimited as exc:
                last = exc
                log.warning("429 reçu (tentative %d/%d).", attempt, attempts)
                if exc.retry_after_s is not None and exc.retry_after_s > 0:
                    with self._cond:
                        self._not_before_wall = max(self._not_before_wall, self._clock() + exc.retry_after_s)
        raise RetriesExhausted(f"429 persistant après {attempts} tentatives") from last

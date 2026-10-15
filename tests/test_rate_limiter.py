"""Étape 2 : limiteur de débit, testé avec appels simulés (brief, section 10).

Scénarios exigés : rafale, 429 simulé, redémarrage en cours de journée,
changement de jour. Aucun appel réseau.
"""

import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from config import GeminiQuota
from rate_limiter import (
    CorruptStateError,
    DailyQuotaExhausted,
    RateLimited,
    RateLimiter,
    RetriesExhausted,
    USPacific,
    get_timezone,
)

PACIFIC = get_timezone("America/Los_Angeles")


def zone_or_skip(name):
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        raise unittest.SkipTest(f"base tzdata absente : {name}")


def pacific_ts(y, mo, d, h=12, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=PACIFIC).timestamp()


class FakeClock:
    """Horloge simulée : sleep() fait avancer le temps instantanément."""

    def __init__(self, start):
        self.now = float(start)
        self.sleeps = []
        self._lock = threading.Lock()

    def time(self):
        with self._lock:
            return self.now

    def sleep(self, dt):
        with self._lock:
            self.sleeps.append(dt)
            self.now += max(0.0, dt)

    def advance(self, dt):
        with self._lock:
            self.now += dt


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name) / "state" / "gemini.json"
        self.clock = FakeClock(pacific_ts(2026, 10, 15, 9))

    def tearDown(self):
        self._tmp.cleanup()

    def make(self, quota=None, **kw):
        return RateLimiter(quota or GeminiQuota(), self.state, wall_clock=self.clock.time, sleep=self.clock.sleep, **kw)


class SpacingTest(Base):
    def test_rafale_espacee_de_5s(self):
        rl = self.make()
        starts = []
        for _ in range(10):
            rl.call(lambda: starts.append(self.clock.time()))
        gaps = [b - a for a, b in zip(starts, starts[1:])]
        self.assertTrue(all(g >= 5.0 - 1e-9 for g in gaps), gaps)
        self.assertAlmostEqual(starts[-1] - starts[0], 45.0)
        self.assertEqual(rl.used_today, 10)

    def test_jamais_plus_de_12_par_minute(self):
        rl = self.make()
        starts = []
        for _ in range(40):
            rl.call(lambda: starts.append(self.clock.time()))
        for i, t in enumerate(starts):
            in_window = [u for u in starts if t <= u < t + 60]
            self.assertLessEqual(len(in_window), 12, f"fenêtre démarrant à l'appel {i}")

    def test_premier_appel_immediat(self):
        rl = self.make()
        rl.call(lambda: None)
        self.assertEqual(self.clock.sleeps, [])

    def test_pas_d_attente_si_deja_espace(self):
        rl = self.make()
        rl.call(lambda: None)
        self.clock.advance(10)
        rl.call(lambda: None)
        self.assertEqual(self.clock.sleeps, [])

    def test_temps_d_execution_compte_dans_l_espacement(self):
        rl = self.make()
        rl.call(lambda: self.clock.advance(3))  # l'appel dure 3 s
        rl.call(lambda: None)
        self.assertEqual(len(self.clock.sleeps), 1)
        self.assertAlmostEqual(self.clock.sleeps[0], 2.0)

    def test_espacement_configurable(self):
        rl = self.make(GeminiQuota(min_interval_s=1.0))
        starts = []
        for _ in range(3):
            rl.call(lambda: starts.append(self.clock.time()))
        self.assertAlmostEqual(starts[-1] - starts[0], 2.0)


class RetryTest(Base):
    def test_429_puis_succes(self):
        rl = self.make()
        attempts = []

        def fn():
            attempts.append(self.clock.time())
            if len(attempts) < 3:
                raise RateLimited()
            return "ok"

        self.assertEqual(rl.call(fn), "ok")
        self.assertEqual(len(attempts), 3)
        self.assertEqual(rl.used_today, 3)  # chaque tentative compte
        gaps = [b - a for a, b in zip(attempts, attempts[1:])]
        self.assertTrue(all(g >= 5.0 for g in gaps), gaps)  # les retries repassent par l'espacement

    def test_429_persistant_epuise_les_retries(self):
        rl = self.make()
        calls = []

        def fn():
            calls.append(1)
            raise RateLimited()

        with self.assertRaises(RetriesExhausted):
            rl.call(fn)
        self.assertEqual(len(calls), 3)  # 1 tentative + 2 retries, pas plus
        self.assertEqual(rl.used_today, 3)

    def test_retry_after_respecte(self):
        rl = self.make()
        attempts = []

        def fn():
            attempts.append(self.clock.time())
            if len(attempts) == 1:
                raise RateLimited(retry_after_s=30)
            return "ok"

        rl.call(fn)
        self.assertGreaterEqual(attempts[1] - attempts[0], 30.0)

    def test_retry_after_court_n_annule_pas_l_espacement(self):
        rl = self.make()
        attempts = []

        def fn():
            attempts.append(self.clock.time())
            if len(attempts) == 1:
                raise RateLimited(retry_after_s=1)
            return "ok"

        rl.call(fn)
        self.assertGreaterEqual(attempts[1] - attempts[0], 5.0)

    def test_autre_erreur_sans_retry_mais_comptee(self):
        rl = self.make()
        calls = []

        def fn():
            calls.append(1)
            raise ValueError("réponse invalide")

        with self.assertRaises(ValueError):
            rl.call(fn)
        self.assertEqual(len(calls), 1)
        self.assertEqual(rl.used_today, 1)

    def test_retries_configurables(self):
        rl = self.make(GeminiQuota(max_retries=0))
        with self.assertRaises(RetriesExhausted):
            rl.call(lambda: (_ for _ in ()).throw(RateLimited()))
        self.assertEqual(rl.used_today, 1)

    def test_retry_bloque_par_l_arret_dur(self):
        rl = self.make(GeminiQuota(daily_alert=2, daily_hard_stop=2))
        calls = []

        def fn():
            calls.append(1)
            raise RateLimited()

        with self.assertRaises(DailyQuotaExhausted):
            rl.call(fn)
        self.assertEqual(len(calls), 2)
        self.assertEqual(rl.used_today, 2)


class BudgetTest(Base):
    def test_arret_dur_a_1400(self):
        rl = self.make(GeminiQuota(min_interval_s=0))
        for _ in range(1400):
            rl.call(lambda: None)
        executed = []
        with self.assertRaises(DailyQuotaExhausted):
            rl.call(lambda: executed.append(1))
        self.assertEqual(executed, [])  # l'appel refusé n'est jamais exécuté
        self.assertEqual(rl.used_today, 1400)
        self.assertEqual(rl.remaining_today, 0)

    def test_alerte_a_1200_une_seule_fois(self):
        alerts = []
        rl = self.make(GeminiQuota(min_interval_s=0), on_alert=alerts.append)
        with self.assertLogs("rate_limiter", level="WARNING") as logs:
            for _ in range(1300):
                rl.call(lambda: None)
        self.assertEqual(alerts, [1200])
        self.assertTrue(any("alerte" in m for m in logs.output))

    def test_compteur_incremente_avant_l_appel(self):
        rl = self.make()
        seen = []
        rl.call(lambda: seen.append(json.loads(self.state.read_text())["count"]))
        self.assertEqual(seen, [1])  # déjà persisté pendant l'exécution

    def test_alerte_superieure_a_l_arret_refusee(self):
        with self.assertRaises(ValueError):
            self.make(GeminiQuota(daily_alert=1500, daily_hard_stop=1400))


class RestartTest(Base):
    def test_redemarrage_conserve_le_compteur(self):
        rl = self.make()
        for _ in range(5):
            rl.call(lambda: None)
        self.clock.advance(3600)
        rl2 = self.make()  # nouveau processus, même journée
        self.assertEqual(rl2.used_today, 5)
        rl2.call(lambda: None)
        self.assertEqual(rl2.used_today, 6)

    def test_redemarrage_respecte_l_espacement(self):
        rl = self.make()
        rl.call(lambda: None)
        t0 = self.clock.time()
        self.clock.advance(1)
        rl2 = self.make()  # redémarrage 1 s après le dernier appel
        starts = []
        rl2.call(lambda: starts.append(self.clock.time()))
        self.assertGreaterEqual(starts[0] - t0, 5.0)

    def test_redemarrage_apres_arret_dur(self):
        rl = self.make(GeminiQuota(min_interval_s=0, daily_alert=3, daily_hard_stop=3))
        for _ in range(3):
            rl.call(lambda: None)
        rl2 = self.make(GeminiQuota(min_interval_s=0, daily_alert=3, daily_hard_stop=3))
        with self.assertRaises(DailyQuotaExhausted):
            rl2.call(lambda: None)

    def test_fichier_corrompu_refuse(self):
        self.state.parent.mkdir(parents=True)
        for content in ["{pas du json", '{"version": 1, "day": "2026-10-15"}',
                        '{"version": 99, "day": "2026-10-15", "count": 1, "last_call_wall": 0}',
                        '{"version": 1, "day": "2026-10-15", "count": -4, "last_call_wall": 0}',
                        '[]']:
            with self.subTest(content=content):
                self.state.write_text(content, encoding="utf-8")
                with self.assertRaises(CorruptStateError):
                    self.make()

    def test_pas_de_fichier_temporaire_residuel(self):
        rl = self.make()
        rl.call(lambda: None)
        self.assertEqual(sorted(p.name for p in self.state.parent.iterdir()), ["gemini.json"])


class DayChangeTest(Base):
    def test_remise_a_zero_a_minuit_pacifique(self):
        self.clock.now = pacific_ts(2026, 10, 15, 23, 59, 50)
        rl = self.make(GeminiQuota(min_interval_s=0, daily_alert=2, daily_hard_stop=2))
        rl.call(lambda: None)
        rl.call(lambda: None)
        with self.assertRaises(DailyQuotaExhausted):
            rl.call(lambda: None)
        self.clock.now = pacific_ts(2026, 10, 16, 0, 0, 1)
        self.assertEqual(rl.used_today, 0)
        rl.call(lambda: None)
        self.assertEqual(rl.used_today, 1)

    def test_minuit_paris_ne_remet_pas_a_zero(self):
        paris = zone_or_skip("Europe/Paris")
        self.clock.now = datetime(2026, 10, 15, 23, 59, tzinfo=paris).timestamp()
        rl = self.make()
        rl.call(lambda: None)
        self.clock.now = datetime(2026, 10, 16, 0, 1, tzinfo=paris).timestamp()  # 15:01 en Californie
        self.assertEqual(rl.used_today, 1)
        self.clock.now = datetime(2026, 10, 16, 9, 1, tzinfo=paris).timestamp()  # 00:01 en Californie
        self.assertEqual(rl.used_today, 0)

    def test_changement_de_jour_pendant_l_attente(self):
        self.clock.now = pacific_ts(2026, 10, 15, 23, 59, 58)
        q = GeminiQuota(daily_alert=1, daily_hard_stop=1)
        rl = self.make(q)
        rl.call(lambda: None)  # 1/1 le 15
        # Appel suivant : budget épuisé le 15 -> refusé immédiatement.
        with self.assertRaises(DailyQuotaExhausted):
            rl.call(lambda: None)
        self.clock.now = pacific_ts(2026, 10, 16, 0, 0, 0)
        rl.call(lambda: None)
        self.assertEqual(rl.used_today, 1)
        self.assertEqual(json.loads(self.state.read_text())["day"], "2026-10-16")

    def test_redemarrage_un_autre_jour(self):
        rl = self.make()
        for _ in range(7):
            rl.call(lambda: None)
        self.clock.now = pacific_ts(2026, 10, 17, 8)
        rl2 = self.make()
        self.assertEqual(rl2.used_today, 0)

    def test_passage_heure_d_hiver(self):
        # 1er novembre 2026 : retour à l'heure normale du Pacifique (jour de 25 h).
        self.clock.now = pacific_ts(2026, 11, 1, 0, 30)
        rl = self.make()
        rl.call(lambda: None)
        self.clock.now = pacific_ts(2026, 11, 1, 23, 59)
        self.assertEqual(rl.used_today, 1)
        self.clock.now = pacific_ts(2026, 11, 2, 0, 0, 1)
        self.assertEqual(rl.used_today, 0)


class FallbackTimezoneTest(unittest.TestCase):
    """Repli sans tzdata (cas Windows) : doit donner exactement l'heure du Pacifique."""

    def test_identique_a_zoneinfo(self):
        z = zone_or_skip("America/Los_Angeles")
        u = USPacific()
        start = int(datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp())
        end = int(datetime(2032, 1, 1, tzinfo=timezone.utc).timestamp())
        for ts in range(start, end, 3600):
            a, b = datetime.fromtimestamp(ts, z), datetime.fromtimestamp(ts, u)
            self.assertEqual((a.isoformat(), a.fold), (b.isoformat(), b.fold), ts)
            self.assertEqual(b.timestamp(), ts)

    def test_transitions_2026(self):
        u = USPacific()
        cases = {
            "2026-03-08T09:59:59+00:00": "2026-03-08T01:59:59-08:00",
            "2026-03-08T10:00:00+00:00": "2026-03-08T03:00:00-07:00",
            "2026-11-01T08:59:59+00:00": "2026-11-01T01:59:59-07:00",
            "2026-11-01T09:00:00+00:00": "2026-11-01T01:00:00-08:00",
            "2026-10-15T07:00:00+00:00": "2026-10-15T00:00:00-07:00",
        }
        for utc, local in cases.items():
            self.assertEqual(datetime.fromtimestamp(datetime.fromisoformat(utc).timestamp(), u).isoformat(), local)

    def test_repli_utilise_sans_tzdata(self):
        def missing(name):
            raise ZoneInfoNotFoundError(name)

        with mock.patch("rate_limiter.ZoneInfo", side_effect=missing):
            self.assertIsInstance(get_timezone("America/Los_Angeles"), USPacific)
            self.assertIs(get_timezone("UTC"), timezone.utc)
            with self.assertRaisesRegex(ZoneInfoNotFoundError, "pip install tzdata"):
                get_timezone("Europe/Paris")
            with tempfile.TemporaryDirectory() as d:
                rl = RateLimiter(GeminiQuota(min_interval_s=0), Path(d) / "q.json")
                rl.call(lambda: None)
                self.assertEqual(rl.used_today, 1)


class ConcurrencyTest(unittest.TestCase):
    """Threads réels, horloge réelle, espacement réduit pour un test rapide."""

    def test_threads_servis_un_par_un_et_espaces(self):
        # On mesure l'instant où le limiteur autorise chaque appel (horodatage
        # persisté), pas le moment où le thread se réveille, qui dépend de
        # l'ordonnanceur du système.
        granted = []

        class Spy(RateLimiter):
            def _save(self):
                if not granted or granted[-1] != self._state.last_call_wall:
                    granted.append(self._state.last_call_wall)
                super()._save()

        with tempfile.TemporaryDirectory() as d:
            rl = Spy(GeminiQuota(min_interval_s=0.05), Path(d) / "g.json")
            threads = [threading.Thread(target=rl.call, args=(lambda: None,)) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)
            self.assertEqual(len(granted), 8)
            gaps = [b - a for a, b in zip(granted, granted[1:])]
            self.assertTrue(all(g >= 0.05 - 1e-9 for g in gaps), gaps)
            self.assertEqual(rl.used_today, 8)

    def test_ordre_fifo(self):
        with tempfile.TemporaryDirectory() as d:
            rl = RateLimiter(GeminiQuota(min_interval_s=0.03), Path(d) / "g.json")
            order = []
            rl.call(lambda: None)  # occupe la file : les suivants doivent attendre
            threads = []
            for i in range(6):
                t = threading.Thread(target=rl.call, args=(lambda i=i: order.append(i),))
                t.start()
                threads.append(t)
                time.sleep(0.005)  # arrivées échelonnées pendant l'attente
            for t in threads:
                t.join(timeout=10)
            self.assertEqual(order, list(range(6)))

    def test_lecture_du_compteur_non_bloquee_pendant_l_attente(self):
        with tempfile.TemporaryDirectory() as d:
            rl = RateLimiter(GeminiQuota(min_interval_s=0.5), Path(d) / "g.json")
            rl.call(lambda: None)
            t = threading.Thread(target=rl.call, args=(lambda: None,))
            t.start()
            time.sleep(0.05)
            t0 = time.monotonic()
            self.assertEqual(rl.used_today, 1)
            self.assertLess(time.monotonic() - t0, 0.1)
            t.join(timeout=5)


if __name__ == "__main__":
    unittest.main()

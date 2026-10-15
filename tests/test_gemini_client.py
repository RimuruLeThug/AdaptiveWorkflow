"""Client Gemini : requêtes simulées, aucun appel réseau."""

import json
import tempfile
import unittest
from pathlib import Path

from config import GeminiQuota
from gemini_client import GeminiClient, GeminiDailyQuotaError, GeminiError, HttpResponse
from rate_limiter import RateLimiter, RetriesExhausted

KEY = "test-key-0123456789"


def ok_body(text="bonjour", finish="STOP", usage=None):
    return json.dumps({
        "candidates": [{"content": {"parts": [{"text": text}], "role": "model"}, "finishReason": finish}],
        "usageMetadata": usage or {"promptTokenCount": 12, "candidatesTokenCount": 3},
    }).encode()


def err_body(code, status, message="erreur", details=None):
    return json.dumps({"error": {"code": code, "status": status, "message": message, "details": details or []}}).encode()


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, url, headers, body, timeout):
        self.requests.append({"url": url, "headers": headers, "body": json.loads(body), "timeout": timeout})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0
        self.sleeps = []

    def time(self):
        return self.t

    def sleep(self, dt):
        self.sleeps.append(dt)
        self.t += dt


class ClientTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.limiter = RateLimiter(GeminiQuota(), Path(self._tmp.name) / "q.json",
                                   wall_clock=self.clock.time, sleep=self.clock.sleep)

    def tearDown(self):
        self._tmp.cleanup()

    def client(self, responses):
        self.transport = FakeTransport(responses)
        return GeminiClient(KEY, "gemini-3.5-flash-lite", self.limiter, transport=self.transport)

    def test_requete_bien_formee(self):
        c = self.client([HttpResponse(200, {}, ok_body())])
        r = c.generate("salut", system="sys", temperature=0.3, json_output=True, max_output_tokens=50)
        req = self.transport.requests[0]
        self.assertEqual(req["url"], "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent")
        self.assertEqual(req["headers"]["x-goog-api-key"], KEY)
        self.assertNotIn(KEY, req["url"])  # clé jamais dans l'URL
        self.assertEqual(req["body"]["contents"][0]["parts"][0]["text"], "salut")
        self.assertEqual(req["body"]["systemInstruction"]["parts"][0]["text"], "sys")
        self.assertEqual(req["body"]["generationConfig"],
                         {"temperature": 0.3, "responseMimeType": "application/json", "maxOutputTokens": 50})
        self.assertEqual((r.text, r.input_tokens, r.output_tokens, r.attempts), ("bonjour", 12, 3, 1))
        self.assertEqual(c.requests_sent, 1)
        self.assertEqual(self.limiter.used_today, 1)

    def test_parties_de_reflexion_ignorees(self):
        body = json.dumps({"candidates": [{"content": {"parts": [
            {"text": "je réfléchis", "thought": True}, {"text": "réponse"}]}, "finishReason": "STOP"}]}).encode()
        r = self.client([HttpResponse(200, {}, body)]).generate("x")
        self.assertEqual(r.text, "réponse")

    def test_429_puis_succes_avec_retry_info(self):
        details = [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "17s"}]
        c = self.client([HttpResponse(429, {}, err_body(429, "RESOURCE_EXHAUSTED", details=details)),
                         HttpResponse(200, {}, ok_body())])
        r = c.generate("x")
        self.assertEqual(r.attempts, 2)
        self.assertEqual(c.requests_sent, 2)
        self.assertEqual(self.limiter.used_today, 2)
        self.assertGreaterEqual(sum(self.clock.sleeps), 17.0)  # retryDelay respecté

    def test_429_en_tete_retry_after(self):
        c = self.client([HttpResponse(429, {"retry-after": "40"}, b""), HttpResponse(200, {}, ok_body())])
        c.generate("x")
        self.assertGreaterEqual(sum(self.clock.sleeps), 40.0)

    def test_429_persistant(self):
        c = self.client([HttpResponse(429, {}, b"")] * 3)
        with self.assertRaises(RetriesExhausted):
            c.generate("x")
        self.assertEqual(c.requests_sent, 3)
        self.assertEqual(self.limiter.used_today, 3)

    def test_429_quota_journalier_pas_de_retry(self):
        details = [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                    "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]
        c = self.client([HttpResponse(429, {}, err_body(429, "RESOURCE_EXHAUSTED", details=details))])
        with self.assertRaises(GeminiDailyQuotaError):
            c.generate("x")
        self.assertEqual(c.requests_sent, 1)

    def test_erreur_http_sans_fuite_de_cle(self):
        c = self.client([HttpResponse(400, {}, err_body(400, "INVALID_ARGUMENT", f"API key {KEY} not valid"))])
        with self.assertRaises(GeminiError) as cm:
            c.generate("x")
        self.assertNotIn(KEY, str(cm.exception))
        self.assertIn("HTTP 400", str(cm.exception))
        self.assertEqual(c.requests_sent, 1)  # pas de retry sur 400

    def test_erreur_reseau(self):
        c = self.client([GeminiError("Réseau : coupé")])
        with self.assertRaises(GeminiError):
            c.generate("x")

    def test_reponse_bloquee(self):
        body = json.dumps({"promptFeedback": {"blockReason": "SAFETY"}}).encode()
        with self.assertRaisesRegex(GeminiError, "SAFETY"):
            self.client([HttpResponse(200, {}, body)]).generate("x")

    def test_reponse_sans_texte(self):
        body = json.dumps({"candidates": [{"content": {"parts": []}, "finishReason": "MAX_TOKENS"}]}).encode()
        with self.assertRaises(GeminiError):
            self.client([HttpResponse(200, {}, body)]).generate("x")

    def test_troncature_signalee(self):
        r = self.client([HttpResponse(200, {}, ok_body("début", "MAX_TOKENS"))]).generate("x")
        self.assertTrue(r.truncated)

    def test_corps_non_json(self):
        with self.assertRaises(GeminiError):
            self.client([HttpResponse(502, {}, b"<html>Bad gateway</html>")]).generate("x")

    def test_repr_sans_cle(self):
        self.assertNotIn(KEY, repr(self.client([])))

    def test_cle_et_modele_obligatoires(self):
        with self.assertRaises(ValueError):
            GeminiClient("", "m", self.limiter)
        with self.assertRaises(ValueError):
            GeminiClient(KEY, "", self.limiter)

    def test_arret_dur_aucune_requete_envoyee(self):
        limiter = RateLimiter(GeminiQuota(daily_alert=1, daily_hard_stop=1), Path(self._tmp.name) / "q2.json",
                              wall_clock=self.clock.time, sleep=self.clock.sleep)
        t = FakeTransport([HttpResponse(200, {}, ok_body())] * 2)
        c = GeminiClient(KEY, "m", limiter, transport=t)
        c.generate("x")
        from rate_limiter import DailyQuotaExhausted
        with self.assertRaises(DailyQuotaExhausted):
            c.generate("x")
        self.assertEqual(len(t.requests), 1)


class KeyLoadingTest(unittest.TestCase):
    def test_refus_sans_cle(self):
        import os
        import config
        old = os.environ.pop("GEMINI_API_KEY", None)
        try:
            with self.assertRaises(config.MissingKeyError):
                config.gemini_api_key()
        finally:
            if old is not None:
                os.environ["GEMINI_API_KEY"] = old

    def test_indice_si_fichier_mal_nomme(self):
        import os
        from unittest import mock
        import config
        old = os.environ.pop("GEMINI_API_KEY", None)
        try:
            with tempfile.TemporaryDirectory() as d:
                (Path(d) / "keys.env").write_text("GEMINI_API_KEY=x", encoding="utf-8")
                with mock.patch.object(config, "PROJECT_ROOT", Path(d)):
                    with self.assertRaisesRegex(config.MissingKeyError, "Rename-Item keys.env .env"):
                        config.gemini_api_key()
                    (Path(d) / ".env").write_text("AUTRE=1", encoding="utf-8")
                    with self.assertRaisesRegex(config.MissingKeyError, "ne contient pas"):
                        config.gemini_api_key()
        finally:
            if old is not None:
                os.environ["GEMINI_API_KEY"] = old

    def test_dotenv(self):
        import os
        import config
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".env"
            p.write_text('﻿# commentaire\nT_A=1\nT_B="deux mots"\nexport T_C=3 # note\nT_D=\nT_E=garde\n', encoding="utf-8")
            os.environ["T_E"] = "existant"
            try:
                config.load_dotenv(p)
                self.assertEqual([os.environ.get(k) for k in ("T_A", "T_B", "T_C", "T_D", "T_E")],
                                 ["1", "deux mots", "3", "", "existant"])
            finally:
                for k in ("T_A", "T_B", "T_C", "T_D", "T_E"):
                    os.environ.pop(k, None)


if __name__ == "__main__":
    unittest.main()

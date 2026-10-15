"""Backend Ollama : requêtes simulées, aucun serveur requis."""

import json
import unittest

from local_llm import LocalLLMError, Message, OllamaBackend, release_vram
from verify.schema import SchemaError


class FakeOllama:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, method, url, body, timeout):
        self.requests.append({"method": method, "url": url, "body": json.loads(body) if body else None})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        status, payload = r
        return status, json.dumps(payload).encode() if not isinstance(payload, bytes) else payload


def chat_ok(content, **extra):
    return 200, {"message": {"role": "assistant", "content": content}, "done": True,
                 "prompt_eval_count": 20, "eval_count": 5, **extra}


class OllamaTest(unittest.TestCase):
    def make(self, responses, **kw):
        self.t = FakeOllama(responses)
        return OllamaBackend("qwen3.5:4b", transport=self.t, **kw)

    def test_requete_chat(self):
        llm = self.make([chat_ok("bonjour")])
        r = llm.chat([Message("system", "sys"), Message("user", "salut")], temperature=0.3)
        req = self.t.requests[0]
        self.assertEqual((req["method"], req["url"]), ("POST", "http://localhost:11434/api/chat"))
        b = req["body"]
        self.assertEqual(b["model"], "qwen3.5:4b")
        self.assertFalse(b["stream"])
        self.assertFalse(b["think"])  # raisonnement désactivé par défaut pour l'interface
        self.assertEqual(b["options"], {"temperature": 0.3, "num_ctx": 8192})
        self.assertEqual(b["messages"], [{"role": "system", "content": "sys"}, {"role": "user", "content": "salut"}])
        self.assertNotIn("format", b)
        self.assertEqual((r.text, r.input_tokens, r.output_tokens), ("bonjour", 20, 5))

    def test_balises_think_retirees(self):
        r = self.make([chat_ok("<think>hmm</think>\nRéponse")]).ask("x")
        self.assertEqual(r.text, "Réponse")

    def test_sortie_json_contrainte(self):
        schema = {"type": "object", "required": ["a"], "properties": {"a": {"type": "integer"}}}
        llm = self.make([chat_ok('{"a": 3}')])
        doc, _ = llm.ask_json("x", schema)
        self.assertEqual(doc, {"a": 3})
        self.assertEqual(self.t.requests[0]["body"]["format"], schema)
        self.assertEqual(self.t.requests[0]["body"]["options"]["temperature"], 0.0)

    def test_json_invalide(self):
        with self.assertRaises(SchemaError):
            self.make([chat_ok('{"a": "trois"}')]).ask_json(
                "x", {"type": "object", "properties": {"a": {"type": "integer"}}})

    def test_modele_sans_thinking(self):
        llm = self.make([(400, {"error": '"qwen2.5:3b" does not support thinking'}), chat_ok("ok"), chat_ok("ok2")])
        self.assertEqual(llm.ask("x").text, "ok")
        self.assertNotIn("think", self.t.requests[1]["body"])
        llm.ask("y")
        self.assertNotIn("think", self.t.requests[2]["body"])  # mémorisé

    def test_think_none_pas_envoye(self):
        llm = self.make([chat_ok("ok")], think=None)
        llm.ask("x")
        self.assertNotIn("think", self.t.requests[0]["body"])

    def test_modele_absent(self):
        with self.assertRaisesRegex(LocalLLMError, "ollama pull qwen3.5:4b"):
            self.make([(404, {"error": "model 'qwen3.5:4b' not found"})]).ask("x")

    def test_erreur_serveur(self):
        with self.assertRaisesRegex(LocalLLMError, "HTTP 500"):
            self.make([(500, {"error": "boom"})]).ask("x")

    def test_ollama_eteint(self):
        with self.assertRaisesRegex(LocalLLMError, "Ollama"):
            self.make([LocalLLMError("Ollama ne répond pas")]).ask("x")

    def test_check_available(self):
        tags = (200, {"models": [{"name": "qwen3.5:4b"}, {"name": "qwen2.5:3b-instruct-q4_K_M"}]})
        self.make([tags]).check_available()
        self.assertEqual(self.t.requests[0]["url"], "http://localhost:11434/api/tags")
        with self.assertRaisesRegex(LocalLLMError, "installés : qwen2.5"):
            self.make([(200, {"models": [{"name": "qwen2.5:3b"}]})]).check_available()

    def test_suggestion_de_variante(self):
        t = FakeOllama([(200, {"models": [{"name": "qwen3.5:4b-q8_0"}, {"name": "qwen2.5:3b-instruct-q4_K_M"}]})])
        with self.assertRaisesRegex(LocalLLMError, "Variante déjà installée : qwen3.5:4b-q8_0"):
            OllamaBackend("qwen3.5:4b", transport=t).check_available()

    def test_check_available_tag_latest(self):
        t = FakeOllama([(200, {"models": [{"name": "monmodele:latest"}]})])
        OllamaBackend("monmodele", transport=t).check_available()

    def test_modele_vide_refuse(self):
        with self.assertRaises(ValueError):
            OllamaBackend("")

    def test_keep_alive_court_par_defaut(self):
        llm = self.make([chat_ok("ok")])
        llm.ask("x")
        self.assertEqual(self.t.requests[0]["body"]["keep_alive"], "2m")
        llm2 = self.make([chat_ok("ok")], keep_alive="30s")
        llm2.ask("x")
        self.assertEqual(self.t.requests[0]["body"]["keep_alive"], "30s")

    def test_unload_envoie_keep_alive_zero(self):
        llm = self.make([(200, {"done": True})])
        self.assertTrue(llm.unload())
        req = self.t.requests[0]
        self.assertEqual((req["method"], req["url"]), ("POST", "http://localhost:11434/api/generate"))
        self.assertEqual(req["body"], {"model": "qwen3.5:4b", "keep_alive": 0})

    def test_unload_ne_leve_jamais(self):
        self.assertFalse(self.make([LocalLLMError("Ollama ne répond pas")]).unload())
        self.assertFalse(self.make([(500, {"error": "boom"})]).unload())


class ReleaseVramTest(unittest.TestCase):
    def test_decharge_seulement_les_modeles_du_projet(self):
        t = FakeOllama([
            (200, {"models": [{"name": "qwen3.5:4b-q8_0"}, {"name": "llama3:8b"}, {"name": "qwen2.5:3b"}]}),
            (200, {"done": True}),
        ])
        done = release_vram(["qwen3.5:4b-q8_0", "qwen2.5:3b-instruct-q4_K_M"], transport=t)
        self.assertEqual(done, ["qwen3.5:4b-q8_0"])
        self.assertEqual([r["url"] for r in t.requests],
                         ["http://localhost:11434/api/ps", "http://localhost:11434/api/generate"])
        self.assertEqual(t.requests[1]["body"]["model"], "qwen3.5:4b-q8_0")  # llama3:8b intact

    def test_tag_latest_implicite(self):
        t = FakeOllama([(200, {"models": [{"name": "monmodele:latest"}]}), (200, {"done": True})])
        self.assertEqual(release_vram(["monmodele"], transport=t), ["monmodele:latest"])

    def test_rien_de_charge(self):
        t = FakeOllama([(200, {"models": []})])
        self.assertEqual(release_vram(["qwen3.5:4b"], transport=t), [])
        self.assertEqual(len(t.requests), 1)  # aucune requête de déchargement inutile

    def test_ollama_eteint_ne_leve_pas(self):
        t = FakeOllama([LocalLLMError("Ollama ne répond pas")])
        self.assertEqual(release_vram(["qwen3.5:4b"], transport=t), [])

    def test_liste_vide(self):
        t = FakeOllama([])
        self.assertEqual(release_vram([], transport=t), [])
        self.assertEqual(t.requests, [])


if __name__ == "__main__":
    unittest.main()

"""Client Claude Code : aucun vrai appel, le processus est simulé."""

import json
import os
import subprocess
import tempfile
import unittest
from datetime import date
from pathlib import Path

from claude_cli import COST_NOTE, ClaudeCodeClient, ClaudeCodeError


class FakeRunner:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, cmd, **kw):
        cwd = kw.get("cwd")
        sys_file = None
        if "--system-prompt-file" in cmd:
            path = Path(cmd[cmd.index("--system-prompt-file") + 1])
            sys_file = path.read_text(encoding="utf-8")
        self.calls.append({"cmd": cmd, "input": kw.get("input"), "env": kw.get("env"), "cwd": cwd,
                           "cwd_listing": sorted(os.listdir(cwd)) if cwd else None, "system": sys_file})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        code, stdout = r
        return subprocess.CompletedProcess(cmd, code, stdout=stdout, stderr="")


AUTH_OK = (0, json.dumps({"loggedIn": True, "authMethod": "claude.ai"}))


def result(text="réponse", is_error=False, **extra):
    return (0, json.dumps({"type": "result", "subtype": "success", "is_error": is_error, "result": text,
                           "total_cost_usd": 0.01, "usage": {"input_tokens": 10, "output_tokens": 5}, **extra}))


class ClaudeCliTest(unittest.TestCase):
    def client(self, responses, **kw):
        self.runner = FakeRunner(responses)
        kw.setdefault("which", lambda name: "/usr/bin/claude")
        return ClaudeCodeClient(runner=self.runner, **kw)

    def test_appel_sans_outils_sur_abonnement(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-test"
        try:
            c = self.client([AUTH_OK, result("bonjour")], model="opus")
            r = c.generate("question", system="Tu es X", json_output=True)
        finally:
            del os.environ["ANTHROPIC_API_KEY"]
        self.assertEqual(r.text, "bonjour")
        call = self.runner.calls[1]
        cmd = call["cmd"]
        self.assertEqual(cmd[:2], ["/usr/bin/claude", "-p"])
        self.assertNotIn("--bare", cmd)  # --bare n'utilise pas l'abonnement
        self.assertEqual(cmd[cmd.index("--disallowedTools") + 1], "*")  # aucun outil
        self.assertEqual(cmd[cmd.index("--model") + 1], "opus")
        for flag in ("--strict-mcp-config", "--no-session-persistence"):
            self.assertIn(flag, cmd)
        self.assertEqual(cmd[cmd.index("--output-format") + 1], "json")
        self.assertEqual(call["input"], "question")  # le prompt passe par stdin
        self.assertNotIn("ANTHROPIC_API_KEY", call["env"])  # jamais l'API par accident
        self.assertEqual(call["cwd_listing"], ["system.txt"])  # dossier temporaire vide
        self.assertIn("Tu es X", call["system"])
        self.assertIn("JSON", call["system"])
        self.assertEqual((r.input_tokens, r.output_tokens), (10, 5))

    def test_connexion_par_cle_api_refusee(self):
        c = self.client([(0, json.dumps({"authMethod": "api_key"}))])
        with self.assertRaisesRegex(ClaudeCodeError, "facturerait l'API"):
            c.generate("x")
        self.assertEqual(len(self.runner.calls), 1)  # aucun appel de génération

    def test_facturation_api_autorisee_explicitement(self):
        c = self.client([(0, json.dumps({"authMethod": "api_key"})), result()], allow_api_billing=True)
        self.assertEqual(c.generate("x").text, "réponse")

    def test_non_connecte(self):
        c = self.client([(1, json.dumps({"loggedIn": False, "authMethod": "none"}))])
        with self.assertRaisesRegex(ClaudeCodeError, "claude auth login"):
            c.check_available()

    def test_non_installe(self):
        c = self.client([], which=lambda name: None)
        with self.assertRaisesRegex(ClaudeCodeError, "pas installé"):
            c.check_available()

    def test_erreurs(self):
        cases = [
            result("Claude usage limit reached", is_error=True),
            (1, ""),
            (0, "pas du json"),
            result("   "),
            subprocess.TimeoutExpired("claude", 600),
        ]
        for case in cases:
            with self.subTest(case=str(case)[:40]):
                c = self.client([AUTH_OK, case])
                with self.assertRaises(ClaudeCodeError):
                    c.generate("x")

    def test_limite_d_usage_signalee(self):
        c = self.client([AUTH_OK, result("Claude usage limit reached", is_error=True)])
        with self.assertRaisesRegex(ClaudeCodeError, "limite d'usage"):
            c.generate("x")

    def test_verification_faite_une_seule_fois(self):
        c = self.client([AUTH_OK, result(), result()])
        c.generate("a")
        c.generate("b")
        self.assertEqual(len(self.runner.calls), 3)

    def test_compteur_journalier(self):
        with tempfile.TemporaryDirectory() as d:
            day = {"v": date(2026, 10, 15)}
            c = self.client([AUTH_OK, result(), result(), result()], counter_path=Path(d) / "c.json",
                            today=lambda: day["v"])
            c.generate("a")
            c.generate("b")
            self.assertEqual(c.calls_today(), 2)
            day["v"] = date(2026, 10, 16)
            self.assertEqual(c.calls_today(), 0)
            c.generate("c")
            self.assertEqual(c.calls_today(), 1)

    def test_jamais_presente_comme_gratuit(self):
        self.assertIn("quota", COST_NOTE)
        for word in ("gratuit", "sans coût", "sans cout"):
            self.assertNotIn(word, COST_NOTE.lower())


if __name__ == "__main__":
    unittest.main()

"""Étape 1 : la structure du dépôt existe et s'importe, sans logique métier."""

import importlib
import os
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class StructureTest(unittest.TestCase):
    def test_modules_importables(self):
        for name in [
            "config", "rate_limiter", "qwen_ui", "organizers", "organizers.base",
            "organizers.gemini", "organizers.claude", "workers", "verify",
            "verify.haiku", "privacy", "profiles",
        ]:
            with self.subTest(module=name):
                importlib.import_module(name)

    def test_haiku_desactive(self):
        self.assertFalse(importlib.import_module("verify.haiku").ENABLED)

    def test_quotas_par_defaut(self):
        from config import load_settings
        s = load_settings()
        self.assertEqual(s.gemini.min_interval_s, 5.0)
        self.assertEqual(s.gemini.daily_alert, 1200)
        self.assertEqual(s.gemini.daily_hard_stop, 1400)
        self.assertEqual(s.gemini.max_retries, 2)
        self.assertEqual(s.gemini.reset_timezone, "America/Los_Angeles")
        self.assertEqual(s.big_model_provider, "gemini")

    def test_quotas_configurables(self):
        from config import load_settings
        os.environ["GEMINI_DAILY_HARD_STOP"] = "100"
        try:
            self.assertEqual(load_settings().gemini.daily_hard_stop, 100)
        finally:
            del os.environ["GEMINI_DAILY_HARD_STOP"]

    def test_env_ignore_par_git(self):
        lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn(".env", lines)
        self.assertIn("state/", lines)

    def test_fichiers_de_cles_ignores(self):
        from fnmatch import fnmatch
        lines = [l.strip() for l in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()]
        patterns = [l for l in lines if l and not l.startswith(("#", "!"))]
        negations = [l[1:] for l in lines if l.startswith("!")]

        def ignored(name):
            return any(fnmatch(name, p) for p in patterns) and not any(fnmatch(name, n) for n in negations)

        for name in [".env", "keys.env", "gemini.env", ".env.local", ".env.txt", "keys.env.txt"]:
            with self.subTest(name=name):
                self.assertTrue(ignored(name))
        self.assertFalse(ignored(".env.example"))

    def test_aucune_cle_dans_le_code(self):
        for py in ROOT.rglob("*.py"):
            text = py.read_text(encoding="utf-8")
            with self.subTest(file=str(py)):
                self.assertIsNone(re.search(r"AIza[0-9A-Za-z_\-]{30,}", text))   # clé Google
                self.assertIsNone(re.search(r"sk-ant-[0-9A-Za-z_\-]{20,}", text))  # clé Anthropic


if __name__ == "__main__":
    unittest.main()

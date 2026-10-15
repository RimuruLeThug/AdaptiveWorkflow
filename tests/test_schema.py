"""Validateur JSON maison (verify/schema.py)."""

import unittest

from verify.schema import SchemaError, parse_json, validate

IDEA = {
    "type": "object",
    "required": ["titre", "effort"],
    "additionalProperties": False,
    "properties": {
        "titre": {"type": "string", "minLength": 1, "maxLength": 20},
        "effort": {"type": "string", "enum": ["faible", "moyen", "fort"]},
        "score": {"type": "number", "minimum": 0, "maximum": 10},
        "tags": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 2},
        "n": {"type": "integer"},
        "note": {"type": ["string", "null"]},
    },
}


class SchemaTest(unittest.TestCase):
    def ok(self, v):
        validate(v, IDEA)

    def ko(self, v):
        with self.assertRaises(SchemaError):
            validate(v, IDEA)

    def test_valide(self):
        self.ok({"titre": "a", "effort": "faible", "score": 7.5, "tags": ["x"], "n": 3, "note": None})

    def test_invalides(self):
        for v in [
            [],  # mauvais type racine
            {"effort": "faible"},  # champ obligatoire manquant
            {"titre": "a", "effort": "énorme"},  # hors enum
            {"titre": "", "effort": "faible"},  # trop court
            {"titre": "x" * 21, "effort": "faible"},  # trop long
            {"titre": "a", "effort": "faible", "score": 11},
            {"titre": "a", "effort": "faible", "score": -1},
            {"titre": "a", "effort": "faible", "score": "7"},
            {"titre": "a", "effort": "faible", "tags": []},
            {"titre": "a", "effort": "faible", "tags": ["a", "b", "c"]},
            {"titre": "a", "effort": "faible", "tags": [1]},
            {"titre": "a", "effort": "faible", "n": 1.5},
            {"titre": "a", "effort": "faible", "n": True},  # booléen n'est pas un entier
            {"titre": "a", "effort": "faible", "intrus": 1},  # additionalProperties
            {"titre": "a", "effort": "faible", "note": 3},
        ]:
            with self.subTest(v=v):
                self.ko(v)

    def test_parse_json(self):
        self.assertEqual(parse_json('{"a": 1}'), {"a": 1})
        self.assertEqual(parse_json('```json\n{"a": 1}\n```'), {"a": 1})
        for bad in ["", "pas du json", '{"a": 1', "Voici : {\"a\": 1}", "{'a': 1}"]:
            with self.subTest(bad=bad), self.assertRaises(SchemaError):
                parse_json(bad)

    def test_mot_cle_inconnu_refuse(self):
        with self.assertRaises(ValueError):
            validate("x", {"type": "string", "pattern": "^a"})


if __name__ == "__main__":
    unittest.main()

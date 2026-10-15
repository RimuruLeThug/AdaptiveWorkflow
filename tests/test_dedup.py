"""Déduplication : Qwen propose, le code décide, rien n'est perdu silencieusement."""

import json
import unittest

from local_llm import LocalLLMError
from tests.test_promise import FakeLLM
from workers.dedup import Idea, collect_ideas, deduplicate, render_dedup, validate_groups


def idea(i, titre, kind="simple", approche="approche"):
    return Idea(i, {"titre": titre, "probleme": "p", "approche": approche, "effort": "moyen", "type": kind})


def groups(*gs):
    return json.dumps({"groupes": [{"ids": list(g), "raison": "même idée"} for g in gs]})


IDEAS = [
    idea(1, "Scan du frigo par photo"),
    idea(2, "Photographier le réfrigérateur"),
    idea(3, "Planning de repas", "ambitieuse"),
    idea(4, "Ne rien jeter : manger moins", "contrarienne"),
    idea(5, "Partage entre voisins"),
    idea(6, "Liste de courses partagée"),
    idea(7, "Compost connecté", "ambitieuse"),
    idea(8, "Rappels de péremption"),
]


class DedupTest(unittest.TestCase):
    def test_doublons_de_sens(self):
        llm = FakeLLM([groups((1, 2))])
        r = deduplicate(IDEAS, llm)
        self.assertEqual([i.id for i in r.kept], [1, 3, 4, 5, 6, 7, 8])
        self.assertEqual(r.kept[0].merged_ids, [2])
        self.assertEqual(r.removed_count, 1)
        self.assertEqual(llm.calls[0]["temperature"], 0.0)
        self.assertIn("1. [simple] Scan du frigo par photo", llm.calls[0]["messages"][-1].content)

    def test_doublons_exacts_sans_modele(self):
        ideas = [idea(1, "Scan du frigo !"), idea(2, "scan du FRIGO"), idea(3, "Scan du frígo")]
        r = deduplicate(ideas, None)
        self.assertEqual([i.id for i in r.kept], [1])
        self.assertEqual(sorted(r.kept[0].merged_ids), [2, 3])
        self.assertTrue(r.warnings)  # signale que seule la dédup exacte a tourné

    def test_aucun_doublon(self):
        r = deduplicate(IDEAS, FakeLLM([groups()]))
        self.assertEqual(len(r.kept), 8)
        self.assertEqual(r.groups, [])

    def test_contrarienne_jamais_fusionnee_avec_autre_type(self):
        r = deduplicate(IDEAS, FakeLLM([groups((3, 4))]))
        self.assertIn(4, [i.id for i in r.kept])
        self.assertEqual(len(r.kept), 8)

    def test_contrarienne_protegee_dans_groupe_mixte(self):
        r = deduplicate(IDEAS, FakeLLM([groups((1, 2, 4))]))
        kept = [i.id for i in r.kept]
        self.assertIn(4, kept)
        self.assertNotIn(2, kept)

    def test_contrariennes_identiques_fusionnees(self):
        ideas = [idea(1, "A", "contrarienne"), idea(2, "B", "contrarienne"), idea(3, "C")]
        r = deduplicate(ideas, FakeLLM([groups((1, 2))]))
        self.assertEqual([i.id for i in r.kept], [1, 3])

    def test_contrarienne_et_titre_exact_identique_non_fusionnees(self):
        r = deduplicate([idea(1, "Moins acheter"), idea(2, "Moins acheter", "contrarienne")], None)
        self.assertEqual(len(r.kept), 2)

    def test_propositions_invalides_tout_est_garde(self):
        for bad in [groups((1, 99)), groups((1, 2), (2, 3)), groups((1, 2, 3, 5, 6)),
                    "pas du json", json.dumps({"groupes": [{"ids": [1], "raison": "x"}]}),
                    LocalLLMError("Ollama éteint")]:
            with self.subTest(bad=str(bad)[:40]):
                r = deduplicate(IDEAS, FakeLLM([bad]))
                self.assertEqual(len(r.kept), 8)
                self.assertTrue(r.warnings)

    def test_groupe_trop_grand_refuse_avec_trace(self):
        ideas = [idea(i, f"Idée {i}") for i in range(1, 21)]
        r = deduplicate(ideas, FakeLLM([groups((1, 2, 3, 4, 5))]))
        self.assertEqual(len(r.kept), 20)
        self.assertIn("maximum 4", r.warnings[0])
        self.assertIn("Proposition brute", r.warnings[0])

    def test_schema_impose_petits_groupes(self):
        llm = FakeLLM([groups()])
        deduplicate(IDEAS, llm)
        self.assertEqual(llm.calls[0]["schema"]["properties"]["groupes"]["items"]["properties"]["ids"]["maxItems"], 4)

    def test_garde_fou_inactif_sur_petite_liste(self):
        r = deduplicate([idea(1, "A"), idea(2, "B"), idea(3, "C")], FakeLLM([groups((1, 2))]))
        self.assertEqual([i.id for i in r.kept], [1, 3])

    def test_identifiants_repetes_dans_un_groupe(self):
        r = deduplicate(IDEAS, FakeLLM([groups((1, 2, 2))]))
        self.assertEqual(r.kept[0].merged_ids, [2])

    def test_traces_transitives(self):
        ideas = [idea(1, "Photo du frigo"), idea(2, "Scan"), idea(3, "Scan"), idea(4, "X"), idea(5, "Y"), idea(6, "Z")]
        r = deduplicate(ideas, FakeLLM([groups((1, 2))]))  # 3 déjà absorbé par 2 (exact), puis 2 absorbé par 1
        self.assertEqual(sorted(r.kept[0].merged_ids), [2, 3])
        self.assertEqual(len(r.kept) + r.removed_count, 6)  # rien de perdu

    def test_entree_non_modifiee(self):
        before = [i.merged_ids[:] for i in IDEAS]
        deduplicate(IDEAS, FakeLLM([groups((1, 2))]))
        self.assertEqual([i.merged_ids for i in IDEAS], before)

    def test_validate_groups_direct(self):
        by_id = {i.id: i for i in IDEAS}
        self.assertEqual(validate_groups([{"ids": [2, 1], "raison": ""}], by_id), [[1, 2]])
        with self.assertRaises(ValueError):
            validate_groups([{"ids": [1, 2, 3, 5, 6], "raison": ""}], by_id)

    def test_collect_ideas(self):
        outs = [json.dumps({"idees": [IDEAS[0].data, IDEAS[1].data]}), "texte libre", json.dumps({"idees": [IDEAS[2].data]})]
        ideas = collect_ideas(outs)
        self.assertEqual([i.id for i in ideas], [1, 2, 3])
        self.assertEqual(ideas[2].titre, "Planning de repas")

    def test_collect_ideas_bloc_markdown(self):
        fenced = "```json\n" + json.dumps({"idees": [IDEAS[0].data]}) + "\n```"
        self.assertEqual(len(collect_ideas([fenced])), 1)

    def test_rendu(self):
        r = deduplicate(IDEAS, FakeLLM([groups((1, 2))]))
        text = render_dedup(r)
        self.assertIn("7 (1 doublon(s) regroupé(s))", text)
        self.assertIn("1 <- 2 (qwen)", text)
        self.assertIn("(+ doublons 2)", text)


if __name__ == "__main__":
    unittest.main()

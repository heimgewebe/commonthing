from __future__ import annotations

from pathlib import Path
import unittest


REPO = Path(__file__).resolve().parents[3]


class ProductionDomainTruthDocsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.compose_prod = (REPO / "infra" / "compose" / "compose.prod.yml").read_text(encoding="utf-8")
        self.env_example = (REPO / ".env.example").read_text(encoding="utf-8")
        self.data_model = (REPO / "docs" / "datenmodell.md").read_text(encoding="utf-8")
        self.deploy_readme = (REPO / "docs" / "deploy" / "README.md").read_text(encoding="utf-8")
        self.orientation = (REPO / "docs" / "policies" / "orientierung.md").read_text(encoding="utf-8")

    def test_production_compose_defaults_domain_and_passkeys_to_postgres(self) -> None:
        required = [
            "WELTGEWEBE_DOMAIN_READ_SOURCE: ${WELTGEWEBE_DOMAIN_READ_SOURCE:-postgres}",
            "WELTGEWEBE_DOMAIN_ACCOUNT_WRITE_SOURCE: ${WELTGEWEBE_DOMAIN_ACCOUNT_WRITE_SOURCE:-postgres}",
            "WELTGEWEBE_DOMAIN_NODE_WRITE_SOURCE: ${WELTGEWEBE_DOMAIN_NODE_WRITE_SOURCE:-postgres}",
            "WELTGEWEBE_DOMAIN_EDGE_WRITE_SOURCE: ${WELTGEWEBE_DOMAIN_EDGE_WRITE_SOURCE:-postgres}",
            "WELTGEWEBE_PASSKEY_CREDENTIAL_SOURCE: ${WELTGEWEBE_PASSKEY_CREDENTIAL_SOURCE:-postgres}",
        ]
        for phrase in required:
            with self.subTest(required=phrase):
                self.assertIn(phrase, self.compose_prod)

    def test_local_example_keeps_jsonl_reads_but_retires_edge_writes(self) -> None:
        required = [
            "WELTGEWEBE_DOMAIN_READ_SOURCE=jsonl",
            "WELTGEWEBE_DOMAIN_ACCOUNT_WRITE_SOURCE=jsonl",
            "WELTGEWEBE_DOMAIN_NODE_WRITE_SOURCE=jsonl",
            "WELTGEWEBE_DOMAIN_EDGE_WRITE_SOURCE=read_only",
        ]
        for phrase in required:
            with self.subTest(required=phrase):
                self.assertIn(phrase, self.env_example)

    def test_canonical_docs_distinguish_local_defaults_from_production_truth(self) -> None:
        self.assertIn("Lokaler/Legacy-Default", self.data_model)
        self.assertIn("Produktionsvertrag", self.data_model)
        self.assertIn("keine Aussage über die Produktionsarchitektur", self.data_model)
        self.assertIn("lokal `jsonl`, Produktion `postgres`", self.deploy_readme)
        self.assertIn(
            "WELTGEWEBE_DOMAIN_EDGE_WRITE_SOURCE**: lokal `read_only`, Produktion `postgres`",
            self.deploy_readme,
        )
        self.assertIn("Produktionsvertrag nutzt PostgreSQL", self.orientation)
        self.assertIn("Liveaussagen brauchen datierte Runtime-Evidence", self.orientation)

    def test_obsolete_unqualified_jsonl_truth_claims_do_not_return(self) -> None:
        forbidden_by_file = {
            "docs/datenmodell.md": ["Domänendaten bleibt JSONL der Standard"],
            "docs/deploy/README.md": ["WELTGEWEBE_DOMAIN_READ_SOURCE**: Default `jsonl`"],
            "docs/policies/orientierung.md": ["**Datenwahrheit:** JSONL bleibt Standard"],
        }
        contents = {
            "docs/datenmodell.md": self.data_model,
            "docs/deploy/README.md": self.deploy_readme,
            "docs/policies/orientierung.md": self.orientation,
        }
        for filename, forbidden_phrases in forbidden_by_file.items():
            for phrase in forbidden_phrases:
                with self.subTest(filename=filename, forbidden=phrase):
                    self.assertNotIn(phrase, contents[filename])


if __name__ == "__main__":
    unittest.main()

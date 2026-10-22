import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gearfire_compliance import (
    Rule,
    crawl_site,
    load_rules,
    main,
    scan_catalog,
)


class ComplianceCliTests(unittest.TestCase):
    def test_scan_flags_exact_upc_and_keyword_matches_with_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = root / "catalog.csv"
            catalog.write_text(
                "UPC,Name,Description\n"
                "001234567890,Item A,Ordinary item\n"
                "987654321098,Item B,capacity item\n"
                ",Item C,Restricted feature\n"
                "111111111111,Item D,Ordinary item\n",
                encoding="utf-8",
            )
            output = root / "candidates.csv"
            rules = [
                Rule(
                    "UPC-1",
                    "upc",
                    "001234567890",
                    "UPC needs review",
                    "https://example.test/source-1",
                ),
                Rule(
                    "TEXT-1",
                    "keyword",
                    "restricted feature",
                    "Description needs review",
                    "https://example.test/source-2",
                ),
            ]

            summary = scan_catalog(
                catalog,
                rules,
                output,
                text_columns=["Name", "Description"],
            )

            self.assertEqual((summary.products_scanned, summary.candidates_flagged), (4, 2))
            self.assertEqual(summary.candidates_without_upc, 1)
            with output.open(encoding="utf-8", newline="") as report_file:
                rows = list(csv.DictReader(report_file))

            self.assertEqual([row["UPC"] for row in rows], ["001234567890", ""])
            self.assertEqual(rows[0]["review_status"], "REVIEW_REQUIRED")
            self.assertEqual(rows[0]["matched_rule_ids"], "UPC-1")
            self.assertIn("source-1", rows[0]["sources"])
            self.assertEqual(rows[1]["matched_rule_ids"], "TEXT-1")

    def test_upc_matching_ignores_hyphens_and_spaces_but_preserves_zeroes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = root / "catalog.csv"
            catalog.write_text("UPC,Name\n00-123 4567890,Item\n", encoding="utf-8")
            output = root / "candidates.csv"
            rules = [
                Rule(
                    "UPC-1",
                    "upc",
                    "001234567890",
                    "UPC needs review",
                    "WA RCW citation",
                )
            ]

            summary = scan_catalog(catalog, rules, output)

            self.assertEqual(summary.candidates_flagged, 1)

    def test_load_rules_rejects_missing_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            rules_path = Path(directory) / "rules.csv"
            rules_path.write_text(
                "rule_id,match_type,pattern,reason,source\n"
                "R1,keyword,restricted,Check,\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "fill in every required column"):
                load_rules(rules_path)

    def test_output_must_not_overwrite_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = Path(directory) / "catalog.csv"
            catalog.write_text("UPC,Name\n123,Item\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "must not overwrite"):
                scan_catalog(catalog, [], catalog)

    def test_cli_does_not_overwrite_rules_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = root / "catalog.csv"
            catalog.write_text("UPC,Name\n123,Item\n", encoding="utf-8")
            rules = root / "rules.csv"
            rules.write_text(
                "rule_id,match_type,pattern,reason,source\n"
                "R1,upc,123,Review,WA RCW citation\n",
                encoding="utf-8",
            )
            original_rules = rules.read_text(encoding="utf-8")

            result = main(
                [
                    "--catalog",
                    str(catalog),
                    "--rules",
                    str(rules),
                    "--output",
                    str(rules),
                ]
            )

            self.assertEqual(result, 2)
            self.assertEqual(rules.read_text(encoding="utf-8"), original_rules)

    def test_cli_runs_scan_and_writes_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = root / "catalog.csv"
            catalog.write_text("UPC,Name\n123,Item\n", encoding="utf-8")
            rules = root / "rules.csv"
            rules.write_text(
                "rule_id,match_type,pattern,reason,source\n"
                "R1,upc,123,Review,WA RCW citation\n",
                encoding="utf-8",
            )
            output = root / "report.csv"

            result = main(
                [
                    "--catalog",
                    str(catalog),
                    "--rules",
                    str(rules),
                    "--output",
                    str(output),
                ]
            )

            self.assertEqual(result, 0)
            with output.open(encoding="utf-8", newline="") as report_file:
                rows = list(csv.DictReader(report_file))
            self.assertEqual(rows[0]["UPC"], "123")
            self.assertEqual(rows[0]["review_status"], "REVIEW_REQUIRED")

    def test_washington_rules_load_and_match_capacity_and_assault_features(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rules = load_rules(Path(__file__).parents[1] / "washington-rules.csv")
            catalog = root / "catalog.csv"
            catalog.write_text(
                "UPC,Name,Description\n"
                '123,Semiautomatic center fire rifle,"Detachable magazine, pistol grip"\n'
                "456,Magazine,30 round magazine\n"
                "789,Norinco SKS 7.62x39,Semi-auto rifle\n"
                "012,5.56 NATO FMJ,Box of 20 rounds\n",
                encoding="utf-8",
            )
            output = root / "candidates.csv"

            summary = scan_catalog(catalog, rules, output)

            self.assertEqual(summary.candidates_flagged, 3)
            with output.open(encoding="utf-8", newline="") as report_file:
                rows = list(csv.DictReader(report_file))
            self.assertIn("WA-AW-039", rows[0]["matched_rule_ids"])
            self.assertIn("WA-MAG-002", rows[1]["matched_rule_ids"])
            self.assertIn("WA-AW-031", rows[2]["matched_rule_ids"])

    def test_crawl_uses_sitemap_and_extracts_product_json_ld(self):
        with tempfile.TemporaryDirectory() as directory:
            inventory = Path(directory) / "inventory.csv"
            sitemap = (
                b'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                b"<url><loc>https://store.test/products/item-a</loc></url></urlset>"
            )
            product = b"""
                <html><head>
                <script type="application/ld+json">
                {"@type":"Product","name":"Item A","description":"A magazine",
                 "gtin12":"001234567890","category":"Accessories"}
                </script></head><body></body></html>
            """

            def fake_fetch(url, user_agent, timeout):
                if url == "https://store.test/robots.txt":
                    return "text/plain", b"User-agent: *\nAllow: /\n"
                if url == "https://store.test/sitemap.xml":
                    return "application/xml", sitemap
                if url == "https://store.test/products/item-a":
                    return "text/html", product
                raise AssertionError(f"unexpected URL: {url}")

            with patch("gearfire_compliance._fetch_url", side_effect=fake_fetch):
                summary = crawl_site(
                    "https://store.test",
                    inventory,
                    delay=0,
                    timeout=1,
                )

            self.assertEqual(
                (summary.pages_discovered, summary.pages_fetched, summary.products_written),
                (1, 1, 1),
            )
            with inventory.open(encoding="utf-8", newline="") as inventory_file:
                rows = list(csv.DictReader(inventory_file))
            self.assertEqual(rows[0]["UPC"], "001234567890")
            self.assertEqual(rows[0]["Name"], "Item A")
            self.assertEqual(rows[0]["URL"], "https://store.test/products/item-a")


if __name__ == "__main__":
    unittest.main()

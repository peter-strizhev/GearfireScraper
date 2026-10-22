import json
import unittest
from pathlib import Path

from crawl import build_report, decode_sveltekit
from gearfire_compliance import load_rules

RULES = load_rules(Path(__file__).resolve().parents[1] / "washington-rules.csv")


def row(name, category, specs="", description="", firearm=True, upc="1"):
    return {"UPC": upc, "Name": name, "Manufacturer": "", "MPN": "", "Category": category,
            "Specs": specs, "Description": description, "In Stock": "yes",
            "Is Firearm": "yes" if firearm else "", "URL": ""}


def flagged(*rows):
    return {r["Name"]: r["Rule IDs"] for r in build_report(list(rows), RULES)}


class DecodeTests(unittest.TestCase):
    def test_decodes_flattened_data_and_streamed_promises(self):
        main = {"type": "data", "nodes": [{"type": "skip"}, {"type": "data", "data": [
            {"product": 1, "ads": 4}, {"modelName": 2, "tags": 3}, "SKS", [2], ["Promise", 5], 7]}]}
        chunk = {"type": "chunk", "id": 7, "data": [[1], "ad"]}
        nodes = decode_sveltekit(json.dumps(main) + "\n" + json.dumps(chunk))
        self.assertIsNone(nodes[0])
        self.assertEqual(nodes[1], {"product": {"modelName": "SKS", "tags": ["SKS"]}, "ads": ["ad"]})


class ClassifyTests(unittest.TestCase):
    def test_firearm_rules(self):
        hits = flagged(
            row("Patriot 5.56", "rifles/semi-automatic", "Caliber: 5.56 NATO; Threaded: Yes; Capacity: 10 Round"),
            row("Rimfire Carbine", "rifles/semi-automatic", "Caliber: 22 Long Rifle; Threaded: Yes; Stock Type: Folding"),
            row("Fixed Mag Rifle", "rifles/semi-automatic", "Caliber: 308; Threaded: Yes", "Fixed magazine rifle"),
            row("Norinco SKS", "rifles/semi-automatic", "Caliber: 7.62x39"),
            row("Compact 9", "handguns/semi-automatic", "Caliber: 9mm; Capacity: 15 Round; Threaded: No"),
            row("Threaded 9", "handguns/semi-automatic", "Caliber: 9mm; Capacity: 10 Round; Threaded: Yes"),
            row("Lever 30-30", "rifles/lever-action", "Caliber: 30-30; Capacity: 11 Round"),
        )
        self.assertIn("WA-AW-SPEC-RIFLE", hits["Patriot 5.56"])
        self.assertNotIn("Rimfire Carbine", hits)
        self.assertNotIn("Fixed Mag Rifle", hits)
        self.assertIn("WA-AW-031", hits["Norinco SKS"])
        self.assertEqual(hits["Compact 9"], "WA-MAG-SPEC-FIREARM")
        self.assertEqual(hits["Threaded 9"], "WA-AW-SPEC-PISTOL")
        self.assertNotIn("Lever 30-30", hits)

    def test_magazine_and_accessory_rules(self):
        hits = flagged(
            row("PMAG 30 GEN M3", "magazines/rifle", "Capacity: 30 Round", firearm=False),
            row("PMAG 10 GEN M3", "magazines/rifle", "Capacity: 10 Round", firearm=False),
            row("+5 Base Pad", "magazines/magazine-accessories", "", "Adds 5 rounds", firearm=False),
            row("Double Mag Pouch", "holster-magazine-pouches/mag-pouches", "",
                "Holds two AR-15 30 round magazines", firearm=False),
        )
        self.assertIn("WA-MAG-SPEC", hits["PMAG 30 GEN M3"])
        self.assertNotIn("PMAG 10 GEN M3", hits)
        self.assertIn("WA-MAG-SPEC-EXT", hits["+5 Base Pad"])
        self.assertNotIn("Double Mag Pouch", hits)


if __name__ == "__main__":
    unittest.main()

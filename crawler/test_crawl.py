import json
import tempfile
import unittest
from pathlib import Path

from crawl import build_report, crawl, decode_sveltekit
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
            row("T3x Ace Target", "rifles/bolt-action", "Caliber: 308", "Accepts AR-10 and AR-15 magazines"),
            row("Outfitter G3", "rifles/single-shot", "Caliber: 350 Legend"),
            row("1911-A1", "handguns/semi-automatic", "Caliber: 45 ACP", "Auto-Ordnance 1911"),
            row("Thompson 1927A1", "rifles/semi-automatic", "Caliber: 45 ACP"),
            row("DS-15 MOE", "rifles/semi-automatic", "Caliber: 5.56 NATO; Threaded: Yes; Capacity: 10 Round",
                "AR-15 pattern Fixed Magazine Rifle"),
            row("Fixed Mag 20", "rifles/semi-automatic", "Caliber: 5.56 NATO; Capacity: 20 Round",
                "AR-15 fixed magazine rifle"),
            row("DS-15 Fixed Mag Lower", "receivers-frames/lower", "", "AR-15 Fixed Magazine lower", firearm=True),
            row("AR-15 Stripped Lower", "receivers-frames/lower", "", "Mil-spec AR-15 lower", firearm=True),
            row("AR-15 Stripped Upper", "receivers-frames/upper", "", "Mil-spec AR-15 upper", firearm=False),
            row("A5 Upland", "shotguns/semi-automatic", "Stock Type: Adjustable; Capacity: 4 Round"),
            row("Tactical 12ga", "shotguns/semi-automatic", "Stock Type: Pistol Grip; Capacity: 6 Round"),
        )
        self.assertNotIn("DS-15 MOE", hits)
        self.assertIn("WA-AW-004", hits["Fixed Mag 20"])
        self.assertNotIn("DS-15 Fixed Mag Lower", hits)
        self.assertIn("WA-AW-004", hits["AR-15 Stripped Lower"])
        self.assertNotIn("AR-15 Stripped Upper", hits)
        self.assertNotIn("A5 Upland", hits)
        self.assertIn("WA-AW-SPEC-SHOTGUN", hits["Tactical 12ga"])
        self.assertNotIn("T3x Ace Target", hits)
        self.assertNotIn("Outfitter G3", hits)
        self.assertNotIn("1911-A1", hits)
        self.assertIn("WA-AW-008", hits["Thompson 1927A1"])
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
            row("Mark IV 22/45 Magazine", "magazines/handgun", "Capacity: 10 Round", "22/45", firearm=False),
            row("Drum (no spec)", "magazines/rifle", "", "50 round drum magazine", firearm=False),
            row("+5 Base Pad", "magazines/magazine-accessories", "", "Adds 5 rounds", firearm=False),
            row("Magazine Spacer Glock", "magazines/magazine-parts", "", "Fits Glock 26/27, extended grip",
                firearm=False),
            row("Desert Eagle Caliber Conversion", "parts-gunsmithing/conversion-kits", "",
                "Caliber conversion kit", firearm=False),
            row("Micro Conversion Kit", "parts-gunsmithing/conversion-kits", "",
                "Glock conversion kit with stock and shroud", firearm=False),
            row("Double Mag Pouch", "holster-magazine-pouches/mag-pouches", "",
                "Holds two AR-15 30 round magazines", firearm=False),
        )
        self.assertIn("WA-MAG-SPEC", hits["PMAG 30 GEN M3"])
        self.assertNotIn("PMAG 10 GEN M3", hits)
        self.assertNotIn("Mark IV 22/45 Magazine", hits)
        self.assertIn("WA-MAG-002", hits["Drum (no spec)"])
        self.assertIn("WA-MAG-SPEC-EXT", hits["+5 Base Pad"])
        self.assertNotIn("Magazine Spacer Glock", hits)
        self.assertNotIn("Desert Eagle Caliber Conversion", hits)
        self.assertNotIn("Micro Conversion Kit", hits)  # store policy: non-receiver parts
        self.assertNotIn("Double Mag Pouch", hits)


class FakeSite:
    """Two categories of two products each; can 'lose power' after N requests."""
    site = "https://example.test"

    def __init__(self, fail_after=None):
        self.calls, self.fail_after = [], fail_after

    def data(self, path):
        if self.fail_after is not None and len(self.calls) >= self.fail_after:
            raise KeyboardInterrupt("power lost")
        self.calls.append(path)
        if path.startswith("/home"):
            subs = [{"subCategoryId": s, "subCategoryName": s} for s in ("one", "two")]
            return [{"initData": {"navList": [{"departmentId": "d", "departmentName": "Shop",
                     "categories": [{"categoryId": "c", "categoryName": "Rifles", "subCategories": subs}]}]}}]
        if "/detail/" in path:
            pid = path.split("/detail/")[1].split("/")[0]
            return [{"product": {"productId": pid, "modelName": pid, "subProducts": [
                {"subProductId": "s" + pid, "upc": pid, "attributes": [], "isFirearm": True}]}}]
        sub = "one" if "/one/" in path else "two"
        return [{"products": [{"productId": f"{sub}-{i}", "subProductId": "x", "inStock": True} for i in (1, 2)],
                 "pageSize": 24, "totalCount": 2}]


class ResumeTests(unittest.TestCase):
    def test_interrupted_crawl_resumes_without_refetching(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            # nav, listing one, two products, listing two, one product, then the power dies
            with self.assertRaises(KeyboardInterrupt):
                crawl(FakeSite(fail_after=6), out, [], None)
            (out / "cache" / "two-2.json").write_text('{"productId": "tw')  # half-written file

            site = FakeSite()
            rows = crawl(site, out, [], None)

            self.assertEqual(sorted(r["UPC"] for r in rows), ["one-1", "one-2", "two-1", "two-2"])
            fetched = [c for c in site.calls if not c.startswith("/home")]
            self.assertEqual(len(fetched), 1)  # only the corrupt product; listings came from disk
            self.assertIn("/detail/two-2/", fetched[0])


if __name__ == "__main__":
    unittest.main()

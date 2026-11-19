#!/usr/bin/env python3
"""Crawl a Gearfire storefront in a real Chrome window and flag Washington-restricted items.

The storefront (SvelteKit) serves every page's data as `<page>/__data.json`. CloudFront
rejects non-browser clients, so all requests are made with `fetch()` from inside a real
Chrome tab. Product JSON is cached on disk, so an interrupted crawl resumes where it stopped.
"""

from __future__ import annotations

import argparse
import csv
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))
from gearfire_compliance import _matching_rules, load_rules  # noqa: E402
from scrape import (INVENTORY_FIELDS, Browser, _read_json, _write_json,  # noqa: E402
                    crawl, decode_sveltekit, write_csv)

RCW_010 = "https://app.leg.wa.gov/RCW/default.aspx?cite=9.41.010"
RCW_370 = "https://app.leg.wa.gov/RCW/default.aspx?cite=9.41.370"
LCM_NOTE = ("Large capacity magazine (>10 rounds) under RCW 9.41.010; sale/distribution "
            "prohibited by RCW 9.41.370.")

REPORT_FIELDS = ["UPC", "Name", "Manufacturer", "MPN", "Category", "Specs",
                 "Rule IDs", "Reasons", "Sources", "In Stock", "URL"]

# Categories whose items can fall under the assault-weapon or magazine provisions at all.
# Everything else (slings, optics, holsters, mag pouches...) is not restricted by them, and
# scanning its text would flag every accessory that says "fits AR-15".
AW_SCOPE = ("receivers-frames/", "parts-gunsmithing/conversion-kits", "magazines/magazine-parts")
MAG_SCOPE = ("magazines/handgun", "magazines/rifle", "magazines/shotgun",
             "magazines/magazine-parts", "magazines/magazine-accessories")

# Store policy: only the serialized receiver is a regulated part, so stand-alone uppers and
# gunsmithing parts (incl. conversion kits) are not treated as assault-weapon parts.
NON_RECEIVER_PARTS = ("receivers-frames/upper", "parts-gunsmithing/")
# Store policy: a firearm with a permanently fixed magazine of 10 or fewer rounds (e.g. Dark
# Storm DS-15/DS-25) does not accept a detachable magazine and is not treated as an assault weapon.
FIXED_MAGAZINE = re.compile(r"(?i)fixed[\s-]+mag(?:azine)?\b|non-?detachable|retained\s+(?:\d+[\s-]round\s+)?mag")
MANUAL_ACTIONS = ("bolt-action", "lever-action", "pump-action", "single-shot", "over-under",
                  "side-by-side", "break-action", "revolver", "derringer", "combos")
RIMFIRE = re.compile(r"(?i)rimfire|\.?22\s*(?:lr|l\.r|long|short|wmr|win(?:chester)?\s*mag|mag)\b"
                     r"|\.?17\s*(?:hmr|wsm|hm2|mach)")
SHOTGUN_STOCK = re.compile(r"(?i)folding|telescop|collapsib|thumbhole|pistol\s*grip")
STOCK_FEATURE = re.compile(r"(?i)adjustable|folding|telescop|collapsib|thumbhole|pistol\s*grip")


# ---------------------------------------------------------------- classification

def _specs(row: dict) -> dict[str, str]:
    return {k.strip().casefold(): v.strip() for k, _, v in
            (part.partition(":") for part in row["Specs"].split(";")) if k.strip()}


def _first_int(value: str) -> int | None:
    match = re.search(r"\d+", value or "")
    return int(match.group()) if match else None


def spec_flags(row: dict) -> list[tuple[str, str, str]]:
    """Rules driven by the store's structured specs (more reliable than free text)."""
    flags = []
    cat, specs = row["Category"], _specs(row)
    text = f"{row['Name']} {row['Description']}".casefold()
    caliber = specs.get("caliber", "")
    rimfire = bool(RIMFIRE.search(caliber or text))
    capacity = _first_int(specs.get("capacity", ""))
    threaded = specs.get("threaded", "").casefold() == "yes"
    muzzle = specs.get("muzzle device", "").casefold()
    has_muzzle = muzzle not in ("", "no", "none", "n/a", "plain", "crown", "target crown")
    stock = f"{specs.get('stock type', '')} {'adjustable' if specs.get('adjustable stock', '').casefold() == 'yes' else ''}"
    fixed_mag = "fixed magazine" in text or "non-detachable" in text

    if cat in ("rifles/semi-automatic", "nfa-suppressors/short-barreled-rifles",
               "nfa-suppressors/firearms-suppressor-packages") and not rimfire and not fixed_mag:
        features = [n for n, hit in (("threaded barrel", threaded), (f"muzzle device ({muzzle})", has_muzzle),
                                     (f"stock ({stock.strip()})", bool(STOCK_FEATURE.search(stock)))) if hit]
        if features:
            flags.append(("WA-AW-SPEC-RIFLE",
                          "Semiautomatic center-fire rifle with " + ", ".join(features)
                          + " (assault weapon, RCW 9.41.010 (a)(iv)); sale prohibited by RCW 9.41.390. "
                          "Verify it accepts a detachable magazine.", RCW_010))
    if cat == "handguns/semi-automatic" and threaded:
        flags.append(("WA-AW-SPEC-PISTOL", "Semiautomatic pistol with threaded barrel (assault weapon, "
                      "RCW 9.41.010 (a)(vi)(A)); sale prohibited by RCW 9.41.390.", RCW_010))
    if cat == "handguns/large-format-pistols":
        flags.append(("WA-AW-SPEC-LFP", "Large-format semiautomatic pistol: magazine typically inserts outside "
                      "the pistol grip and/or has a barrel shroud (assault weapon, RCW 9.41.010 (a)(vi)(C)/(D)). "
                      "Verify configuration.", RCW_010))
    if cat == "shotguns/semi-automatic":
        # "Adjustable" on a shotgun usually means comb/shims, not the statute's folding or telescoping stock.
        shotgun_stock = specs.get("stock type", "")
        features = [n for n, hit in ((f"stock ({shotgun_stock})", bool(SHOTGUN_STOCK.search(shotgun_stock))),
                                     (f"capacity {capacity} (>7)", capacity is not None and capacity > 7)) if hit]
        if features:
            flags.append(("WA-AW-SPEC-SHOTGUN", "Semiautomatic shotgun with " + ", ".join(features)
                          + " (assault weapon, RCW 9.41.010 (a)(vii)); sale prohibited by RCW 9.41.390.", RCW_010))

    tube_exempt = "lever-action" in cat or (rimfire and "tube" in text)
    if capacity is not None and capacity > 10 and not tube_exempt:
        if cat.startswith("magazines/"):
            flags.append(("WA-MAG-SPEC", f"Magazine capacity {capacity} rounds. {LCM_NOTE}", RCW_370))
        elif row["Is Firearm"]:
            flags.append(("WA-MAG-SPEC-FIREARM", f"Firearm listed with {capacity}-round capacity, i.e. sold with "
                          f"a large capacity magazine. {LCM_NOTE} Can only be sold in WA with magazines of 10 rounds "
                          "or fewer.", RCW_370))
    if cat.startswith("magazines/magazine-") and re.search(
            r"\+\s?[1-9]\b|\b(?:magazine|mag|tube)\s+extension|extension\s+(?:kit|tube)"
            r"|(?:adds?|additional|extra)\s+\d+\s*(?:round|rd)", text):
        flags.append(("WA-MAG-SPEC-EXT", "Magazine extension/base pad that adds capacity: a part from which a "
                      f"large capacity magazine can be assembled. {LCM_NOTE} Verify resulting capacity.", RCW_370))
    return flags


def classify(row: dict, rules: list) -> list[tuple[str, str, str]]:
    cat = row["Category"]
    hits = spec_flags(row)
    capacity = _first_int(_specs(row).get("capacity", ""))
    fixed_ok = bool(FIXED_MAGAZINE.search(f"{row['Name']} {row['Description']}")) \
        and (capacity is None or capacity <= 10)
    if fixed_ok:
        hits = [h for h in hits if not h[0].startswith("WA-AW")]
    # RCW 9.41.010 excludes manually operated firearms from "assault weapon"; suppressors
    # are a feature of one, not one themselves.
    manual = any(action in cat for action in MANUAL_ACTIONS)
    aw_scope = (bool(row["Is Firearm"]) or cat.startswith(AW_SCOPE)) and not manual and not fixed_ok \
        and cat != "nfa-suppressors/suppressors" and not cat.startswith(NON_RECEIVER_PARTS)
    # A structured Capacity spec beats free text ("22/45", "fits Glock 17, 19").
    mag_scope = cat.startswith(MAG_SCOPE) and not row["Is Firearm"] and "capacity" not in _specs(row)
    fields = ["Name", "Manufacturer", "Description", "Specs", "Category"]
    for rule in _matching_rules(row, "UPC", fields, rules):
        if rule.rule_id == "WA-AW-004" and cat.startswith("shotguns/"):
            continue  # AR15/M16/M4 are rifles; "M4" in a shotgun listing is the Benelli M4
        if rule.match_type == "upc" or (rule.rule_id.startswith("WA-AW") and aw_scope) \
                or (rule.rule_id.startswith("WA-MAG") and mag_scope):
            hits.append((rule.rule_id, rule.reason, rule.source))
    if "lever-action" in cat and all(h[0].startswith("WA-MAG") for h in hits):
        return []  # lever-action tubes are excluded from the LCM definition
    return hits



def build_report(inventory: list[dict], rules: list) -> list[dict]:
    report = []
    for row in inventory:
        hits = classify(row, rules)
        if hits:
            report.append({**row,
                           "Rule IDs": "; ".join(h[0] for h in hits),
                           "Reasons": " | ".join(f"{h[0]}: {h[1]}" for h in hits),
                           "Sources": " | ".join(dict.fromkeys(h[2] for h in hits))})
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site-url", default="https://panteltactical.com")
    parser.add_argument("--rules", type=Path, default=ROOT.parent / "washington-rules.csv")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "output")
    parser.add_argument("--delay", type=float, default=0.5, help="Seconds between requests")
    parser.add_argument("--category", action="append", default=[],
                        help="Only crawl categories containing this text, e.g. 'rifles/semi-automatic' (repeatable)")
    parser.add_argument("--exact-category", action="store_true",
                        help="Match category values exactly instead of as substrings")
    parser.add_argument("--max-products", type=int, help="Stop after N products (for a quick test)")
    parser.add_argument("--show-window", action="store_true", help="Keep the Chrome window on screen")
    parser.add_argument("--report-only", action="store_true",
                        help="Skip crawling; rebuild the report from the existing inventory CSV")
    parser.add_argument("--resume", action="store_true",
                        help="Continue an interrupted crawl with the same options it was started with")
    args = parser.parse_args(argv)

    state_path = args.out_dir / "run.json"
    if args.resume and not args.report_only:
        state = _read_json(state_path)
        if not state or state.get("complete"):
            print(f"Nothing to resume: no unfinished crawl recorded in {state_path}")
            return 1
        args = parser.parse_args(state["argv"] + ["--resume"])
        print("Resuming: " + " ".join(state["argv"] or ["(full crawl)"]))

    rules = load_rules(args.rules)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    inventory_path = args.out_dir / "inventory.csv"

    progress_path = args.out_dir / "progress.json"
    sections_started: list[str] = []
    progress = {"status": "running", "current_section": "", "current_item": "",
                "sections_started": sections_started}

    def section_started(section: str) -> None:
        progress["current_section"] = section
        progress["current_item"] = ""
        sections_started.append(section)
        _write_json(progress_path, progress)

    def item_started(product_id: str, name: str) -> None:
        progress["current_item"] = f"{name} (ID: {product_id})" if name else f"ID: {product_id}"
        _write_json(progress_path, progress)

    if not args.report_only:
        _write_json(progress_path, progress)
    completed = False
    try:
        if args.report_only:
            with inventory_path.open(encoding="utf-8-sig", newline="") as f:
                inventory = list(csv.DictReader(f))
        else:
            if not args.resume:  # a new crawl re-lists categories to pick up catalog changes
                shutil.rmtree(args.out_dir / "listings", ignore_errors=True)
                saved = argv if argv is not None else sys.argv[1:]
                _write_json(state_path, {"argv": [a for a in saved if a != "--resume"], "complete": False})
            browser = Browser(args.site_url, args.delay, hide_window=not args.show_window)
            try:
                inventory = crawl(browser, args.out_dir, args.category, args.max_products,
                                  exact_categories=args.exact_category, on_section=section_started,
                                  on_item=item_started)
            finally:
                browser.close()
            if args.exact_category and args.max_products is None:
                missing = sorted(set(args.category) - set(sections_started))
                if missing:
                    raise ValueError(f"Storefront sections not found: {', '.join(missing)}; refresh the section list")
            write_csv(inventory_path, INVENTORY_FIELDS, inventory)
            _write_json(state_path, {**(_read_json(state_path) or {}), "complete": True})

        report = build_report(inventory, rules)
        report_path = args.out_dir / "wa-restricted-items.csv"
        write_csv(report_path, REPORT_FIELDS, report)
        completed = True
    finally:
        if not args.report_only:
            progress["status"] = "complete" if completed else "failed"
            _write_json(progress_path, progress)
    print(f"Inventory: {len(inventory)} UPC rows -> {inventory_path}")
    print(f"Flagged:   {len(report)} rows -> {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

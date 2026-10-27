#!/usr/bin/env python3
"""Crawl a Gearfire storefront in a real Chrome window and flag Washington-restricted items.

The storefront (SvelteKit) serves every page's data as `<page>/__data.json`. CloudFront
rejects non-browser clients, so all requests are made with `fetch()` from inside a real
Chrome tab. Product JSON is cached on disk, so an interrupted crawl resumes where it stopped.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))
from gearfire_compliance import _matching_rules, load_rules  # noqa: E402

RCW_010 = "https://app.leg.wa.gov/RCW/default.aspx?cite=9.41.010"
RCW_370 = "https://app.leg.wa.gov/RCW/default.aspx?cite=9.41.370"
LCM_NOTE = ("Large capacity magazine (>10 rounds) under RCW 9.41.010; sale/distribution "
            "prohibited by RCW 9.41.370.")

INVENTORY_FIELDS = ["UPC", "Name", "Manufacturer", "MPN", "Category", "Specs",
                    "Description", "In Stock", "Is Firearm", "URL"]
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


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")


# ---------------------------------------------------------------- SvelteKit payload decoding

def _unflatten(values: list, chunks: dict) -> object:
    """Decode a `devalue`-flattened array (SvelteKit's __data.json format)."""
    specials = {-1: None, -2: None, -3: float("nan"), -4: float("inf"), -5: float("-inf"), -6: -0.0}
    cache: dict[int, object] = {}

    def hydrate(i: int) -> object:
        if i in specials:
            return specials[i]
        if i in cache:
            return cache[i]
        value = values[i]
        if isinstance(value, list) and value and isinstance(value[0], str):
            kind = value[0]
            if kind == "Promise":
                chunk = chunks.get(hydrate(value[1]))
                result = _unflatten(chunk, chunks) if chunk is not None else None
            elif kind in ("Set",):
                result = [hydrate(x) for x in value[1:]]
            elif kind in ("Map", "null"):
                result = {str(hydrate(value[k]) if kind == "Map" else value[k]): hydrate(value[k + 1])
                          for k in range(1, len(value), 2)}
            else:  # Date, BigInt, RegExp, URL: keep the raw string
                result = value[1] if len(value) > 1 else None
            cache[i] = result
            return result
        if isinstance(value, list):
            result: object = []
            cache[i] = result
            result.extend(hydrate(x) for x in value)
            return result
        if isinstance(value, dict):
            result = {}
            cache[i] = result
            for key, x in value.items():
                result[key] = hydrate(x)
            return result
        cache[i] = value
        return value

    return hydrate(0)


def decode_sveltekit(text: str) -> list:
    lines = [json.loads(line) for line in text.splitlines() if line.strip()]
    if not lines or "nodes" not in lines[0]:
        raise ValueError("Not a SvelteKit data payload")
    chunks = {l["id"]: l["data"] for l in lines[1:] if l.get("type") == "chunk"}
    return [_unflatten(n["data"], chunks) if n.get("type") == "data" else None
            for n in lines[0]["nodes"]]


# ---------------------------------------------------------------- browser fetching

class Browser:
    def __init__(self, site: str, delay: float, hide_window: bool):
        from playwright.sync_api import sync_playwright
        self.site, self.delay = site.rstrip("/"), delay
        # Headless Chrome is blocked by the site's CDN; a normal window is not.
        self._args = ["--window-position=-3000,-3000"] if hide_window else []
        self._pw = sync_playwright().start()
        self._browser = None
        self._start()

    def _start(self) -> None:
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass
        self._browser = self._pw.chromium.launch(channel="chrome", headless=False, args=self._args)
        self.page = self._browser.new_page()
        self.page.goto(self.site + "/home", wait_until="domcontentloaded")

    def data(self, path: str) -> list:
        from playwright.sync_api import Error as PlaywrightError
        url = self.site + path
        status = None
        for attempt in range(5):
            time.sleep(self.delay)
            try:
                status, text = self.page.evaluate(
                    "u => fetch(u, {credentials: 'include'}).then(r => r.text().then(t => [r.status, t]))", url)
            except PlaywrightError as error:  # window closed, crashed, or tab navigated away
                print(f"  browser error ({error.message.splitlines()[0]}); relaunching", file=sys.stderr)
                time.sleep(5)
                self._start()
                continue
            if status == 200:
                return decode_sveltekit(text)
            if status == 404:
                raise FileNotFoundError(url)
            wait = 30 * (attempt + 1)
            print(f"  HTTP {status} for {url}; waiting {wait}s", file=sys.stderr)
            time.sleep(wait)
            if status == 403:  # re-establish the browser session before retrying
                self._start()
        raise RuntimeError(f"Giving up on {url} (last HTTP status {status})")

    def close(self) -> None:
        try:
            self._browser.close()
        finally:
            self._pw.stop()


# ---------------------------------------------------------------- crash-safe state

def _write_json(path: Path, value: object) -> None:
    """Write atomically, so a power loss never leaves a half-written file behind."""
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(value, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _read_json(path: Path) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, UnicodeDecodeError):  # written by an older, non-atomic run
        path.unlink()
        return None


# ---------------------------------------------------------------- crawling

def crawl(browser: Browser, out_dir: Path, categories: list[str], max_products: int | None) -> list[dict]:
    """Return one inventory row per sub-product (UPC/variant).

    Product JSON lands in out_dir/cache and each category's product list in
    out_dir/listings, so a rerun after a crash skips everything already fetched.
    """
    cache_dir, listings_dir = out_dir / "cache", out_dir / "listings"
    cache_dir.mkdir(parents=True, exist_ok=True)
    listings_dir.mkdir(parents=True, exist_ok=True)
    nav = browser.data("/home/__data.json")[0]["initData"]["navList"]
    rows: list[dict] = []
    fetched = 0
    for dept in nav:
        for cat in dept["categories"]:
            for sub in cat["subCategories"]:
                key = f"{slugify(cat['categoryName'])}/{slugify(sub['subCategoryName'])}"
                if categories and not any(c in key for c in categories):
                    continue
                base = (f"/{slugify(dept['departmentName'])}/{key}/"
                        f"{dept['departmentId']}/{cat['categoryId']}/{sub['subCategoryId']}")
                listing_file = listings_dir / f"{sub['subCategoryId']}.json"
                listed = _read_json(listing_file)
                if listed is None:
                    listed = _list_products(browser, base)
                    _write_json(listing_file, listed)
                print(f"{key}: {len(listed)} products", flush=True)
                for product_id, sub_id, in_stock in listed:
                    if max_products is not None and fetched >= max_products:
                        return rows
                    cache_file = cache_dir / f"{product_id}.json"
                    product = _read_json(cache_file)
                    if product is None:
                        try:
                            node = browser.data(f"/{slugify(dept['departmentName'])}/{key}/p"
                                                f"/detail/{product_id}/{sub_id}/__data.json")[-1]
                        except FileNotFoundError:
                            print(f"  missing product {product_id}", file=sys.stderr)
                            continue
                        product = node["product"]
                        _write_json(cache_file, product)
                    fetched += 1
                    rows.extend(_product_rows(product, key, browser.site, dept, in_stock))
    return rows


def _list_products(browser: Browser, base: str) -> list[tuple[str, str, bool]]:
    # The default listing order is unstable between requests, so a single pass over the
    # pages repeats some products and skips others. Union passes over several sort orders
    # until every product in totalCount has been seen.
    found: dict[str, tuple[str, str, bool]] = {}
    total = 0
    for sort_by in (2, 3, 6, 0, 1, 4, 5):
        page = 0
        while True:
            node = browser.data(f"{base}/__data.json?page={page}&sortBy={sort_by}")[-1]
            total = node["totalCount"]
            for item in node["products"]:
                found.setdefault(item["productId"],
                                 (item["productId"], item["subProductId"], bool(item.get("inStock"))))
            page += 1
            if not node["products"] or page * node["pageSize"] >= total:
                break
        if len(found) >= total:
            break
    if len(found) < total:
        print(f"  WARNING: listed {len(found)} of {total} products for {base}", file=sys.stderr)
    return list(found.values())


def _html_to_text(value: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", value or "").replace("&nbsp;", " ").split())


def _product_rows(product: dict, category: str, base_url: str, dept: dict, in_stock: bool) -> list[dict]:
    rows = []
    for sub in product.get("subProducts") or []:
        specs = "; ".join(f"{a['attributeName']}: {a['valueName']}" for a in sub.get("attributes") or [])
        rows.append({
            "UPC": (sub.get("upc") or "").strip(),
            "Name": product.get("modelName") or "",
            "Manufacturer": product.get("manufacturerName") or "",
            "MPN": sub.get("manufacturerPartNumber") or "",
            "Category": category,
            "Specs": specs,
            "Description": _html_to_text(sub.get("description") or product.get("modelDescription") or ""),
            "In Stock": "yes" if in_stock else "no",
            "Is Firearm": "yes" if sub.get("isFirearm") else "",
            "URL": (f"{base_url}/{slugify(dept['departmentName'])}/{category}/{slugify(product.get('modelName') or 'p')}"
                    f"/detail/{product['productId']}/{sub['subProductId']}"),
        })
    return rows


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
        if rule.match_type == "upc" or (rule.rule_id.startswith("WA-AW") and aw_scope) \
                or (rule.rule_id.startswith("WA-MAG") and mag_scope):
            hits.append((rule.rule_id, rule.reason, rule.source))
    if "lever-action" in cat and all(h[0].startswith("WA-MAG") for h in hits):
        return []  # lever-action tubes are excluded from the LCM definition
    return hits


def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8-sig") as f:  # BOM so Excel keeps UTF-8
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)


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
            inventory = crawl(browser, args.out_dir, args.category, args.max_products)
        finally:
            browser.close()
        write_csv(inventory_path, INVENTORY_FIELDS, inventory)
        _write_json(state_path, {**(_read_json(state_path) or {}), "complete": True})

    report = build_report(inventory, rules)
    report_path = args.out_dir / "wa-restricted-items.csv"
    write_csv(report_path, REPORT_FIELDS, report)
    print(f"Inventory: {len(inventory)} UPC rows -> {inventory_path}")
    print(f"Flagged:   {len(report)} rows -> {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Scrape a Gearfire storefront's full catalog into an inventory CSV (one row per UPC).

The storefront (SvelteKit) serves every page's data as `<page>/__data.json`. CloudFront
rejects non-browser clients, so all requests are made with `fetch()` from inside a real
Chrome tab. Product JSON is cached on disk, so rerunning after a crash resumes where it stopped.

    pip install playwright && python scrape.py --site-url https://panteltactical.com
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent

INVENTORY_FIELDS = ["UPC", "Name", "Manufacturer", "MPN", "Category", "Specs",
                    "Description", "In Stock", "Is Firearm", "URL"]


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


def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8-sig") as f:  # BOM so Excel keeps UTF-8
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site-url", default="https://panteltactical.com")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "output")
    parser.add_argument("--delay", type=float, default=0.5, help="Seconds between requests")
    parser.add_argument("--category", action="append", default=[],
                        help="Only scrape categories containing this text, e.g. 'rifles/semi-automatic' (repeatable)")
    parser.add_argument("--max-products", type=int, help="Stop after N products (for a quick test)")
    parser.add_argument("--show-window", action="store_true", help="Keep the Chrome window on screen")
    args = parser.parse_args(argv)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    browser = Browser(args.site_url, args.delay, hide_window=not args.show_window)
    try:
        inventory = crawl(browser, args.out_dir, args.category, args.max_products)
    finally:
        browser.close()
    path = args.out_dir / "inventory.csv"
    write_csv(path, INVENTORY_FIELDS, inventory)
    print(f"Inventory: {len(inventory)} UPC rows -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Flag catalog rows that match a locally maintained compliance rule list."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.robotparser import RobotFileParser


RULE_FIELDS = ("rule_id", "match_type", "pattern", "reason", "source")
OUTPUT_FIELDS = (
    "review_status",
    "matched_rule_ids",
    "match_reasons",
    "sources",
)
DEFAULT_UPC_COLUMN = "UPC"
INVENTORY_FIELDS = ("UPC", "Name", "Description", "Category", "URL", "source_type")
DEFAULT_USER_AGENT = "PantelTacticalComplianceInventory/1.0 (+authorized internal audit)"


@dataclass(frozen=True)
class Rule:
    rule_id: str
    match_type: str
    pattern: str
    reason: str
    source: str


@dataclass(frozen=True)
class ScanSummary:
    products_scanned: int
    candidates_flagged: int
    candidates_without_upc: int


@dataclass(frozen=True)
class CrawlSummary:
    pages_discovered: int
    pages_fetched: int
    products_written: int


class CrawlAccessError(RuntimeError):
    """Raised when a site refuses or does not authorize a crawl."""


class ProductPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.h1_parts: list[str] = []
        self.meta: dict[str, str] = {}
        self._json_ld_blocks: list[str] = []
        self._in_title = False
        self._in_h1 = False
        self._in_json_ld = False
        self._json_ld_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.casefold(): value or "" for key, value in attrs}
        normalized_tag = tag.casefold()
        if normalized_tag == "title":
            self._in_title = True
        elif normalized_tag == "h1":
            self._in_h1 = True
        elif normalized_tag == "meta":
            key = attributes.get("name") or attributes.get("property")
            value = attributes.get("content")
            if key and value:
                self.meta[key.casefold()] = value.strip()
        elif normalized_tag == "script" and (
            "ld+json" in attributes.get("type", "").casefold()
        ):
            self._in_json_ld = True
            self._json_ld_parts = []

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.casefold()
        if normalized_tag == "title":
            self._in_title = False
        elif normalized_tag == "h1":
            self._in_h1 = False
        elif normalized_tag == "script" and self._in_json_ld:
            self._in_json_ld = False
            self._json_ld_blocks.append("".join(self._json_ld_parts))

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        if self._in_h1:
            self.h1_parts.append(data)
        if self._in_json_ld:
            self._json_ld_parts.append(data)

    def product_json_ld(self) -> dict[str, object]:
        for raw_json in self._json_ld_blocks:
            if not raw_json.strip():
                continue
            try:
                parsed = json.loads(raw_json)
            except json.JSONDecodeError:
                continue
            product = _find_product_object(parsed)
            if product is not None:
                return product
        return {}


def _find_product_object(value: object) -> dict[str, object] | None:
    if isinstance(value, dict):
        types = value.get("@type")
        if (
            types == "Product"
            or isinstance(types, list)
            and "Product" in types
        ):
            return value
        for nested in value.values():
            product = _find_product_object(nested)
            if product is not None:
                return product
    elif isinstance(value, list):
        for nested in value:
            product = _find_product_object(nested)
            if product is not None:
                return product
    return None


def _clean_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())


def _extract_product_row(url: str, html: str) -> dict[str, str]:
    parser = ProductPageParser()
    parser.feed(html)
    product = parser.product_json_ld()
    name = _clean_text(product.get("name"))
    if not name:
        name = _clean_text(parser.meta.get("og:title"))
    if not name:
        name = _clean_text(" ".join(parser.h1_parts))
    if not name:
        name = _clean_text(" ".join(parser.title_parts))

    description = _clean_text(product.get("description"))
    if not description:
        description = _clean_text(
            parser.meta.get("description") or parser.meta.get("og:description")
        )

    upc = ""
    for field in ("gtin12", "gtin13", "gtin14", "gtin", "upc"):
        candidate = normalize_upc(str(product.get(field, "")))
        if candidate:
            upc = candidate
            break

    category = _clean_text(product.get("category"))
    source_type = "Product structured data" if product else "Page metadata"
    return {
        "UPC": upc,
        "Name": name,
        "Description": description,
        "Category": category,
        "URL": url,
        "source_type": source_type,
    }


def normalize_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().casefold()).strip("_")


def normalize_upc(value: str) -> str:
    normalized = re.sub(r"[\s-]", "", value)
    return normalized if normalized.isdigit() else ""


def _same_origin(url: str, root_url: str) -> bool:
    parsed_url = urllib.parse.urlparse(url)
    parsed_root = urllib.parse.urlparse(root_url)
    return (
        parsed_url.scheme in {"http", "https"}
        and parsed_url.netloc.casefold() == parsed_root.netloc.casefold()
    )


def _fetch_url(
    url: str,
    user_agent: str,
    timeout: float,
) -> tuple[str, bytes]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": user_agent, "Accept": "text/html,application/xml,*/*"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.headers.get_content_type(), response.read()
    except urllib.error.HTTPError as error:
        if error.code in {401, 403, 429}:
            raise CrawlAccessError(
                f"Site denied access to {url} with HTTP {error.code}; "
                "the crawler will not bypass access controls"
            ) from error
        raise
    except urllib.error.URLError as error:
        raise CrawlAccessError(f"Could not fetch {url}: {error.reason}") from error


def _load_robots(
    root_url: str,
    user_agent: str,
    timeout: float,
) -> tuple[RobotFileParser, list[str]]:
    robots_url = urllib.parse.urljoin(root_url, "/robots.txt")
    try:
        content_type, body = _fetch_url(robots_url, user_agent, timeout)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            parser = RobotFileParser()
            parser.set_url(robots_url)
            parser.parse([])
            return parser, []
        raise CrawlAccessError(
            f"Could not read {robots_url} (HTTP {error.code}); "
            "the crawler stops instead of guessing permission"
        ) from error
    if "text" not in content_type and "xml" not in content_type:
        raise CrawlAccessError(
            f"{robots_url} did not return a robots.txt text response"
        )

    text = body.decode("utf-8", errors="replace")
    parser = RobotFileParser()
    parser.set_url(robots_url)
    parser.parse(text.splitlines())
    sitemap_urls = [
        value.strip()
        for line in text.splitlines()
        if line.casefold().startswith("sitemap:")
        for value in [line.split(":", 1)[1]]
        if value.strip()
    ]
    return parser, sitemap_urls


def _sitemap_locations(
    sitemap_url: str,
    root_url: str,
    robot_parser: RobotFileParser,
    user_agent: str,
    timeout: float,
    delay: float,
    visited: set[str],
) -> list[str]:
    if sitemap_url in visited:
        return []
    visited.add(sitemap_url)
    if not _same_origin(sitemap_url, root_url):
        return []
    if not robot_parser.can_fetch(user_agent, sitemap_url):
        raise CrawlAccessError(f"robots.txt disallows sitemap URL: {sitemap_url}")

    if delay:
        time.sleep(delay)
    _, body = _fetch_url(sitemap_url, user_agent, timeout)
    try:
        root = ET.fromstring(body)
    except ET.ParseError as error:
        raise ValueError(f"Invalid XML sitemap: {sitemap_url}") from error

    locations = [
        (element.text or "").strip()
        for element in root.iter()
        if element.tag.casefold().endswith("loc") and (element.text or "").strip()
    ]
    root_tag = root.tag.casefold()
    if root_tag.endswith("sitemapindex"):
        urls: list[str] = []
        for location in locations:
            urls.extend(
                _sitemap_locations(
                    location,
                    root_url,
                    robot_parser,
                    user_agent,
                    timeout,
                    delay,
                    visited,
                )
            )
        return urls
    return [
        location
        for location in locations
        if _same_origin(location, root_url)
        and robot_parser.can_fetch(user_agent, location)
    ]


def crawl_site(
    site_url: str,
    inventory_path: Path,
    *,
    max_pages: int = 1000,
    delay: float = 1.0,
    timeout: float = 20.0,
    user_agent: str = DEFAULT_USER_AGENT,
) -> CrawlSummary:
    parsed_site = urllib.parse.urlparse(site_url)
    if parsed_site.scheme not in {"http", "https"} or not parsed_site.netloc:
        raise ValueError("site URL must be an absolute http(s) URL")
    if max_pages < 1:
        raise ValueError("max-pages must be at least 1")
    if delay < 0:
        raise ValueError("delay must not be negative")
    if timeout <= 0:
        raise ValueError("timeout must be greater than zero")
    if not inventory_path.parent.is_dir():
        raise ValueError(f"Output directory does not exist: {inventory_path.parent}")

    root_url = f"{parsed_site.scheme}://{parsed_site.netloc}"
    robot_parser, declared_sitemaps = _load_robots(root_url, user_agent, timeout)
    sitemap_urls = declared_sitemaps or [
        urllib.parse.urljoin(root_url, "/sitemap.xml")
    ]
    page_urls: list[str] = []
    visited_sitemaps: set[str] = set()
    for sitemap_url in sitemap_urls:
        page_urls.extend(
            _sitemap_locations(
                sitemap_url,
                root_url,
                robot_parser,
                user_agent,
                timeout,
                delay,
                visited_sitemaps,
            )
        )
        if len(page_urls) >= max_pages:
            break

    unique_page_urls = list(dict.fromkeys(page_urls))[:max_pages]
    temporary_path: Path | None = None
    fetched = 0
    written = 0
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            prefix=f".{inventory_path.name}.",
            suffix=".tmp",
            dir=inventory_path.parent,
            delete=False,
        ) as inventory_file:
            temporary_path = Path(inventory_file.name)
            writer = csv.DictWriter(inventory_file, fieldnames=INVENTORY_FIELDS)
            writer.writeheader()
            for index, page_url in enumerate(unique_page_urls):
                if not robot_parser.can_fetch(user_agent, page_url):
                    continue
                if index or visited_sitemaps:
                    time.sleep(delay)
                content_type, body = _fetch_url(page_url, user_agent, timeout)
                if "html" not in content_type:
                    continue
                fetched += 1
                row = _extract_product_row(
                    page_url, body.decode("utf-8", errors="replace")
                )
                if row["Name"] or row["Description"] or row["UPC"]:
                    writer.writerow(row)
                    written += 1

        os.replace(temporary_path, inventory_path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass

    return CrawlSummary(len(unique_page_urls), fetched, written)


def crawl_and_scan(
    site_url: str,
    rules_path: Path,
    output_path: Path,
    *,
    inventory_path: Path | None = None,
    max_pages: int = 1000,
    delay: float = 1.0,
    timeout: float = 20.0,
    user_agent: str = DEFAULT_USER_AGENT,
) -> tuple[CrawlSummary, ScanSummary]:
    rules = load_rules(rules_path)
    if output_path.resolve() == rules_path.resolve():
        raise ValueError("Output path must not overwrite the rules file")
    if inventory_path is None:
        with tempfile.TemporaryDirectory(prefix="gearfire-inventory-") as directory:
            temporary_inventory = Path(directory) / "inventory.csv"
            crawl_summary = crawl_site(
                site_url,
                temporary_inventory,
                max_pages=max_pages,
                delay=delay,
                timeout=timeout,
                user_agent=user_agent,
            )
            scan_summary = scan_catalog(temporary_inventory, rules, output_path)
            return crawl_summary, scan_summary

    if inventory_path.resolve() in {output_path.resolve(), rules_path.resolve()}:
        raise ValueError("Inventory path must not overwrite the rules or output file")
    crawl_summary = crawl_site(
        site_url,
        inventory_path,
        max_pages=max_pages,
        delay=delay,
        timeout=timeout,
        user_agent=user_agent,
    )
    scan_summary = scan_catalog(inventory_path, rules, output_path)
    return crawl_summary, scan_summary


def load_rules(path: Path) -> list[Rule]:
    rules: list[Rule] = []
    seen_ids: set[str] = set()

    with path.open("r", encoding="utf-8-sig", newline="") as rules_file:
        reader = csv.DictReader(rules_file)
        if not reader.fieldnames:
            raise ValueError(f"Rules file has no header: {path}")

        header_map = {normalize_header(field): field for field in reader.fieldnames}
        missing = [field for field in RULE_FIELDS if field not in header_map]
        if missing:
            raise ValueError(
                f"Rules file is missing required columns: {', '.join(missing)}"
            )

        for line_number, row in enumerate(reader, start=2):
            values = {
                field: (row.get(header_map[field]) or "").strip()
                for field in RULE_FIELDS
            }
            if not any(values.values()):
                continue
            if not all(values.values()):
                raise ValueError(
                    f"Rules file row {line_number} must fill in every required column"
                )

            rule_id = values["rule_id"]
            if rule_id in seen_ids:
                raise ValueError(f"Duplicate rule_id on rules file row {line_number}: {rule_id}")
            seen_ids.add(rule_id)

            match_type = values["match_type"].casefold()
            if match_type not in {"upc", "keyword", "regex"}:
                raise ValueError(
                    f"Unsupported match_type on rules file row {line_number}: "
                    f"{values['match_type']} (use 'upc', 'keyword', or 'regex')"
                )

            pattern = values["pattern"]
            if match_type == "upc" and not normalize_upc(pattern):
                raise ValueError(
                    f"UPC rule on rules file row {line_number} must contain digits"
                )
            if match_type == "regex":
                try:
                    re.compile(pattern)
                except re.error as error:
                    raise ValueError(
                        f"Invalid regex on rules file row {line_number}: {error}"
                    ) from error

            rules.append(
                Rule(
                    rule_id=rule_id,
                    match_type=match_type,
                    pattern=pattern,
                    reason=values["reason"],
                    source=values["source"],
                )
            )

    if not rules:
        raise ValueError(f"Rules file contains no rules: {path}")
    return rules


def _field_map(fieldnames: list[str] | None) -> dict[str, str]:
    if not fieldnames:
        raise ValueError("Catalog file has no header")

    mapping: dict[str, str] = {}
    for field in fieldnames:
        normalized = normalize_header(field)
        if normalized in mapping:
            raise ValueError(f"Catalog has ambiguous duplicate column: {field}")
        mapping[normalized] = field
    return mapping


def _matching_rules(
    row: dict[str, str],
    upc_field: str,
    text_fields: Iterable[str],
    rules: list[Rule],
) -> list[Rule]:
    product_upc = normalize_upc(row.get(upc_field) or "")
    searchable_text = " ".join(
        (row.get(field) or "").casefold() for field in text_fields
    )
    matches: list[Rule] = []

    for rule in rules:
        if rule.match_type == "upc":
            if product_upc and product_upc == normalize_upc(rule.pattern):
                matches.append(rule)
        elif rule.match_type == "keyword" and " ".join(
            rule.pattern.casefold().split()
        ) in " ".join(searchable_text.split()):
            matches.append(rule)
        elif rule.match_type == "regex" and re.search(rule.pattern, searchable_text):
            matches.append(rule)
    return matches


def scan_catalog(
    catalog_path: Path,
    rules: list[Rule],
    output_path: Path,
    upc_column: str = DEFAULT_UPC_COLUMN,
    text_columns: list[str] | None = None,
) -> ScanSummary:
    catalog_resolved = catalog_path.resolve()
    output_resolved = output_path.resolve()
    if catalog_resolved == output_resolved:
        raise ValueError("Output path must not overwrite the catalog file")
    if not output_path.parent.is_dir():
        raise ValueError(f"Output directory does not exist: {output_path.parent}")

    temporary_path: Path | None = None
    scanned = 0
    flagged = 0
    missing_upc = 0

    try:
        with catalog_path.open("r", encoding="utf-8-sig", newline="") as catalog_file:
            reader = csv.DictReader(catalog_file)
            field_map = _field_map(reader.fieldnames)

            normalized_upc_column = normalize_header(upc_column)
            if normalized_upc_column not in field_map:
                raise ValueError(
                    f"UPC column '{upc_column}' was not found in the catalog"
                )
            upc_field = field_map[normalized_upc_column]

            if text_columns:
                missing_columns = [
                    name
                    for name in text_columns
                    if normalize_header(name) not in field_map
                ]
                if missing_columns:
                    raise ValueError(
                        "Text column(s) not found in the catalog: "
                        + ", ".join(missing_columns)
                    )
                text_fields = [
                    field_map[normalize_header(name)] for name in text_columns
                ]
            else:
                text_fields = [
                    field
                    for field in reader.fieldnames or []
                    if field != upc_field
                ]

            original_fields = reader.fieldnames or []
            collisions = set(original_fields).intersection(OUTPUT_FIELDS)
            if collisions:
                raise ValueError(
                    "Catalog contains reserved output column(s): "
                    + ", ".join(sorted(collisions))
                )

            writer_fields = original_fields + list(OUTPUT_FIELDS)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="",
                prefix=f".{output_path.name}.",
                suffix=".tmp",
                dir=output_path.parent,
                delete=False,
            ) as output_file:
                temporary_path = Path(output_file.name)
                writer = csv.DictWriter(output_file, fieldnames=writer_fields)
                writer.writeheader()

                for line_number, row in enumerate(reader, start=2):
                    if None in row:
                        raise ValueError(
                            f"Catalog row {line_number} has more values than headers"
                        )
                    scanned += 1
                    matches = _matching_rules(row, upc_field, text_fields, rules)
                    if not matches:
                        continue

                    flagged += 1
                    if not normalize_upc(row.get(upc_field) or ""):
                        missing_upc += 1

                    output_row = dict(row)
                    output_row.update(
                        {
                            "review_status": "REVIEW_REQUIRED",
                            "matched_rule_ids": "; ".join(
                                rule.rule_id for rule in matches
                            ),
                            "match_reasons": " | ".join(
                                f"{rule.rule_id}: {rule.reason}" for rule in matches
                            ),
                            "sources": " | ".join(
                                dict.fromkeys(rule.source for rule in matches)
                            ),
                        }
                    )
                    writer.writerow(output_row)

        os.replace(temporary_path, output_path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass

    return ScanSummary(scanned, flagged, missing_upc)


def build_scan_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Flag products in an authorized catalog export that match your "
            "reviewed Washington compliance rules."
        )
    )
    parser.add_argument("--catalog", required=True, type=Path, help="Catalog CSV export")
    parser.add_argument(
        "--rules",
        type=Path,
        default=Path("washington-rules.csv"),
        help="Reviewed rules CSV (default: washington-rules.csv)",
    )
    parser.add_argument("--output", required=True, type=Path, help="Candidate report CSV")
    parser.add_argument(
        "--upc-column",
        default=DEFAULT_UPC_COLUMN,
        help=f"Catalog UPC column name (default: {DEFAULT_UPC_COLUMN})",
    )
    parser.add_argument(
        "--text-column",
        action="append",
        dest="text_columns",
        help=(
            "Catalog column to search for keyword rules; may be repeated. "
            "By default, all columns except the UPC column are searched."
        ),
    )
    return parser


def build_scrape_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gearfire_compliance.py scrape",
        description=(
            "Crawl a permitted public sitemap and scan the discovered pages "
            "against reviewed compliance rules."
        ),
    )
    parser.add_argument("--site-url", required=True, help="Storefront root URL")
    parser.add_argument(
        "--rules",
        type=Path,
        default=Path("washington-rules.csv"),
        help="Reviewed rules CSV (default: washington-rules.csv)",
    )
    parser.add_argument("--output", required=True, type=Path, help="Candidate report CSV")
    parser.add_argument(
        "--inventory-output",
        type=Path,
        help="Optional path to save the scraped inventory CSV",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=1000,
        help="Maximum same-origin sitemap URLs to fetch (default: 1000)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Seconds between sitemap/page requests (default: 1.0)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="Per-request timeout in seconds (default: 20)",
    )
    parser.add_argument(
        "--user-agent",
        default=DEFAULT_USER_AGENT,
        help="Descriptive User-Agent sent to the site",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "scrape":
        parser = build_scrape_parser()
        args = parser.parse_args(arguments[1:])
        try:
            crawl_summary, scan_summary = crawl_and_scan(
                site_url=args.site_url,
                rules_path=args.rules,
                output_path=args.output,
                inventory_path=args.inventory_output,
                max_pages=args.max_pages,
                delay=args.delay,
                timeout=args.timeout,
                user_agent=args.user_agent,
            )
        except (OSError, UnicodeError, csv.Error, ValueError, CrawlAccessError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 2

        print(
            f"Discovered {crawl_summary.pages_discovered} URLs; fetched "
            f"{crawl_summary.pages_fetched} HTML pages and wrote "
            f"{crawl_summary.products_written} inventory rows."
        )
        print(
            f"Flagged {scan_summary.candidates_flagged} review candidates "
            f"({scan_summary.candidates_without_upc} without a UPC)."
        )
        print(f"Candidate report: {args.output}")
        if args.inventory_output:
            print(f"Inventory: {args.inventory_output}")
        return 0

    parser = build_scan_parser()
    args = parser.parse_args(arguments)

    try:
        if args.output.resolve() == args.rules.resolve():
            raise ValueError("Output path must not overwrite the rules file")
        rules = load_rules(args.rules)
        summary = scan_catalog(
            catalog_path=args.catalog,
            rules=rules,
            output_path=args.output,
            upc_column=args.upc_column,
            text_columns=args.text_columns,
        )
    except (OSError, UnicodeError, csv.Error, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    print(
        f"Scanned {summary.products_scanned} products; flagged "
        f"{summary.candidates_flagged} review candidates "
        f"({summary.candidates_without_upc} without a UPC)."
    )
    print(f"Candidate report: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

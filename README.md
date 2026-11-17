# Gearfire catalog compliance checker

This command-line tool checks a catalog against a reviewed rules CSV and writes
a CSV of candidate products to review. It supports either a **locally exported
catalog CSV** or a **permitted public sitemap crawl**. The crawler does not
evade bot protections, access controls, authentication, or CAPTCHA; it reads
`robots.txt`, follows only same-origin sitemap URLs allowed for its declared
user agent, uses a conservative delay, and stops on HTTP 401, 403, or 429.
Use a Gearfire-supported export, authorized API, or public crawl that your
organization is authorized to perform.

The tool does not determine whether a product is legally prohibited. Have
qualified counsel or your compliance lead validate the rules against current
Washington law before relying on the results.

## Requirements

- Python 3.10 or later
- A rules CSV with the columns shown in [`rules.template.csv`](./rules.template.csv)

No third-party Python packages are required.

The repository includes [`washington-rules.csv`](./washington-rules.csv), an
initial statute-derived screening set for the Washington assault-weapon and
large-capacity-magazine provisions. It is used by default when `--rules` is
omitted. It is not legal advice or a substitute for counsel review; confirm
each rule against the current RCW and your transaction facts before using it.

## Rule file

Each non-empty rules row must include:

| Column | Meaning |
| --- | --- |
| `rule_id` | A unique identifier for the rule |
| `match_type` | `upc` for an exact UPC match, `keyword` for a literal text match, or `regex` for a Python regular expression |
| `pattern` | The UPC, case-insensitive text phrase, or regular expression to look for |
| `reason` | Why the rule is being applied; describe it as a review reason, not a legal conclusion |
| `source` | An official source URL or citation supporting the reviewed rule |

UPC matching ignores spaces and hyphens but preserves leading zeroes. Keyword
matching is a case-insensitive literal substring search. Regex matching is
performed against the combined catalog text. Neither is a legal classifier;
both may produce false positives or miss products described in unexpected
ways. Rules are intentionally supplied and maintained by the operator rather
than treating product text as a complete legal determination.

For current requirements, consult official Washington sources such as the
[Revised Code of Washington, chapter 9.41](https://app.leg.wa.gov/RCW/default.aspx?cite=9.41)
and the [Washington State Attorney General's firearms information](https://www.atg.wa.gov/firearms).
Confirm the specific provision and its current applicability with a qualified
professional before adding a rule.

## Run

### Crawl a permitted public storefront

The `scrape` mode discovers URLs from `robots.txt` and its sitemap
declarations, or from `/sitemap.xml` when no sitemap is declared. It fetches
same-origin URLs only. The crawler extracts product JSON-LD, Open Graph/title
metadata, descriptions, categories, and `gtin` fields when present. A
`gtin`/UPC is not guaranteed to exist on a storefront page; missing values are
left blank and should be resolved through an authorized product feed.

```sh
python3 gearfire_compliance.py scrape \
  --site-url https://panteltactical.com \
  --rules washington-rules.csv \
  --output review-candidates.csv \
  --inventory-output scraped-inventory.csv \
  --max-pages 1000 \
  --delay 1.0
```

The default user agent identifies the tool as
`PantelTacticalComplianceInventory/1.0 (+authorized internal audit)`. You can
change it with `--user-agent`, but do not impersonate a browser or use it to
evade a site policy. If `robots.txt`, the sitemap, or a product page returns
401, 403, or 429, the command stops with an error. Do not add retry loops,
proxy rotation, CAPTCHA workarounds, or other bypasses. If the site is
protected, request an authorized feed/export from Gearfire or the site
administrator instead.

The scrape command first writes an inventory CSV and then applies the rules to
it. `--inventory-output` is optional; omit it to use a temporary inventory that
is removed after the candidate report is created.

### Scan an existing catalog export

```sh
python3 gearfire_compliance.py \
  --catalog gearfire-export.csv \
  --rules washington-rules.csv \
  --output review-candidates.csv \
  --text-column Name \
  --text-column Description \
  --text-column Category
```

The UPC column defaults to `UPC`. If your export uses a different header, pass
`--upc-column "Product UPC"`. If no `--text-column` options are given, every
catalog column other than the UPC column is searched for keyword rules.

The output contains only matched catalog rows, preserves the original columns,
and adds `review_status`, `matched_rule_ids`, `match_reasons`, and `sources`.
Products without a UPC are still included when a keyword rule matches and are
counted in the command summary. Every output row is labeled
`REVIEW_REQUIRED`; do not automatically remove or disable listings based only
on a match. Review the product, source, current law, and local requirements
before taking action.

The report is written to a temporary file first, then replaces the requested
output path after a successful scan. The catalog file itself is never modified.
The crawler uses the same safe temporary-file behavior for its inventory.

## Tests

Run the standard-library test suite with:

```sh
python3 -m unittest discover -s tests -v
```

## Container deployment

The [`deploy/`](./deploy/) folder contains a Docker deployment for the
structured crawler. It keeps the container running and runs weekly on Sunday
at noon Pacific time by default. Configure the persistent schedule and output
location, then start it with:

```sh
docker compose -f deploy/compose.yaml run --rm gearfire \
  configure --output-dir /data/output
docker compose -f deploy/compose.yaml up -d --build
```

See [`deploy/README.md`](./deploy/README.md) for volume and timezone options.

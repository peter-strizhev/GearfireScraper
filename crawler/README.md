# Pantel Tactical storefront crawler

Crawls the live Gearfire storefront in a real Chrome window, records every product
variant (UPC, name, manufacturer, part number, category, specs, description), and writes
the items that the Washington assault-weapon (RCW 9.41.010 / 9.41.390) and
large-capacity-magazine (RCW 9.41.370) provisions may cover.

The site's CDN blocks scripts and headless browsers, so this opens a normal Chrome window
(moved off-screen) and makes its requests from inside that tab. It reads the same
`__data.json` endpoints the storefront uses for its own page loads, so it gets structured
specs (Capacity, Threaded, Stock Type, Muzzle Device) and UPCs rather than scraped HTML.

## Setup

```sh
cd crawler
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Google Chrome must be installed.

## Run

```sh
.venv/bin/python crawl.py                                   # whole catalog, ~3 hours
.venv/bin/python crawl.py --category rifles/semi-automatic  # one category
.venv/bin/python crawl.py --report-only                     # re-score after editing rules
.venv/bin/python crawl.py --resume                          # continue an interrupted crawl
```

### If the crawl is interrupted

Battery dies, laptop sleeps, Chrome crashes: run `crawl.py --resume`. It reuses the
options the crawl was started with, skips every product and category listing already on
disk, and carries on. Files are written atomically, so a power cut can't leave a corrupt
one behind. A crash of the browser alone doesn't need a restart; the crawler relaunches
Chrome itself. Use `caffeinate -i` in front of the command to stop the Mac from sleeping.

Output goes in `crawler/output/`:

- `wa-restricted-items.csv`: flagged UPCs with `Rule IDs`, `Reasons` (statute clause), and `Sources`
- `inventory.csv`: every crawled UPC
- `cache/`, `listings/`, `run.json`: saved progress used by `--resume`

## How items are flagged

1. **Named firearms.** Text rules from `../washington-rules.csv` (AK, AR-15, SKS, MP5...).
   They are applied only to firearms, receivers, conversion kits, and magazine parts, so
   accessories that just say "fits AR-15" are not flagged.
2. **Feature tests from the store's specs.** Semi-auto center-fire rifles with a threaded
   barrel, muzzle device, or folding/adjustable/thumbhole stock. Semi-auto pistols with a
   threaded barrel. Large-format pistols. Semi-auto shotguns with those stocks or more than
   7 rounds. Rimfire rifles and fixed-magazine rifles are excluded from the rifle feature test.
3. **Magazines.** Magazines with Capacity over 10, capacity-adding extensions and base pads,
   and firearms listed with more than 10 rounds (sold with a large capacity magazine).
   Lever-action and .22 tube magazines are excluded.

This is a screening list built on distributor data, which has some errors. Review each
row (the reason says which spec triggered it) and confirm with counsel before delisting.

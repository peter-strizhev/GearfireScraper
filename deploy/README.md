# Container deployment

This deployment runs the structured crawler continuously and starts one crawl
each Sunday at noon in `America/Los_Angeles`. The schedule is stored in
`deploy/data/schedule.json`, and crawl state/results are written to the output
directory configured by the operator.

The image runs the crawler's headed Chrome session inside Xvfb, a virtual
display required for unattended containers. On Apple Silicon hosts, Compose
uses Linux AMD64 emulation because the deployed Chrome package is AMD64.

## Configure and start

From the repository root:

```sh
docker compose -f deploy/compose.yaml run --rm gearfire \
  configure --output-dir /data/output
docker compose -f deploy/compose.yaml up -d --build
docker compose -f deploy/compose.yaml logs -f gearfire
```

Open <http://localhost:8080/> to select a section (or all sections), start a
scan, and download the resulting inventory and review report CSVs. The page
shows the latest scan's status and offers its log. Dashboard scans run in
separate directories under `/data/output/dashboard/`, so they do not overwrite
scheduled results. Only one dashboard scan can run at a time; a scheduled run
may run separately. Results persist across container restarts. A scan that was
running during a restart is marked interrupted.

The section activity panel displays the current and recent sections for both
dashboard and scheduled/CLI scans, along with the current item name and ID
(or just the ID while its details are loading). It updates every few seconds
and shows the last reported state if a crawl is interrupted. Activity messages from both
types of scan also appear in `docker compose -f deploy/compose.yaml logs -f`.

The dashboard saves discovered sections in `/data/output/dashboard/sections.json`.
They are reused after container restarts; use **Refresh sections** to fetch an
updated list. A failed refresh leaves the previous list in place and shows an
error. A saved list belongs to its configured storefront and is not reused
for a different site.

The dashboard has no user authentication and is bound to the Docker host's
loopback interface by default. Do not expose it directly to the internet;
use an authenticated, TLS-protected reverse proxy or VPN for remote access.
The weekly scheduler runs in the same container and continues as configured.

The default container paths are `/data/schedule.json` and `/data/output`.
Because `/data` is mounted from `deploy/data`, both the persistent schedule and
the crawl output survive container replacement. To choose another host output
folder for both dashboard and scheduled results, set `GEARFIRE_OUTPUT_DIR`
for **every** Compose command, and configure the container path `/data/output`:

```sh
export GEARFIRE_OUTPUT_DIR="$PWD/results"
docker compose -f deploy/compose.yaml run --rm gearfire configure --output-dir /data/output
docker compose -f deploy/compose.yaml up -d --build
```

The `--timezone` option accepts an IANA timezone. Use
`--timezone America/Los_Angeles` for Pacific time; this handles PST/PDT
transitions while retaining Sunday at 12:00 local time. Use `run --once` for a
one-off smoke test and `show` to inspect the saved configuration and next run.

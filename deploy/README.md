# Container deployment

This deployment runs the structured crawler continuously and starts one crawl
each Sunday at noon in `America/Los_Angeles`. The schedule is stored in
`deploy/data/schedule.json`, and crawl state/results are written to the output
directory configured by the operator.

## Configure and start

From the repository root:

```sh
docker compose -f deploy/compose.yaml run --rm gearfire \
  configure --output-dir /data/output
docker compose -f deploy/compose.yaml up -d --build
docker compose -f deploy/compose.yaml logs -f gearfire
```

The default container paths are `/data/schedule.json` and `/data/output`.
Because `/data` is mounted from `deploy/data`, both the persistent schedule and
the crawl output survive container replacement. To choose another host output
folder, mount it to `/data/output` and configure that container path:

```sh
docker compose -f deploy/compose.yaml run --rm \
  -v "$PWD/results:/results" gearfire \
  configure --output-dir /results
```

The `--timezone` option accepts an IANA timezone. Use
`--timezone America/Los_Angeles` for Pacific time; this handles PST/PDT
transitions while retaining Sunday at 12:00 local time. Use `run --once` for a
one-off smoke test and `show` to inspect the saved configuration and next run.

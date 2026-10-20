# Test phase (no demo data, no simulator)

From the test phase on, VIASafe runs with **real data only**:

- no unit simulator (the `simulator` service is in the compose profile `demo`
  and is only started on request);
- no seeded data: `deploy.sh` no longer loads the demo data by default;
- the web console shows only real data (unbuilt modules show a
  "module in development" state), and it offers only the AI models that are
  installed (weights file present in `/models`).

## Server (deploy kit `deploy/server-v2`)

From your PC:

```bash
cd ~/road-risk-analysis/infra/deploy/server-v2 && ./push.sh
```

On the server:

```bash
cd ~/viasafe-v2/infra/deploy/server-v2
./deploy.sh               # rebuild + start in TEST mode (simulator stopped, nothing seeded)
./deploy.sh --purge-demo  # stop the simulator, remove ALL demo/seeded data, revoke the 'simulator' API key
```

`--purge-demo` runs `python -m app.demo.purge_demo --yes --revoke-simulator`.
To preview what it would remove first:

```bash
docker compose -p viasafe-v2 -f docker-compose.v2.yml --env-file .env exec -T api python -m app.demo.purge_demo
```

The purge only removes rows carrying the demo tags/prefixes listed in
`app/demo/purge_demo.py` (seeded ids `…-5eed7e575eed` / `…-9e5e47a7104e`,
`demo-seed` jobs and reports, demo historical-event prefixes, `seed-web-test`
cluster zones, the test users `testeur@viasafe.local` and
`chauffeur{1,2,3}.webtest@viasafe.local`, simulator vehicles `sim-*`, and their
MinIO images). Real data is never touched.

## Local stack (`roaddemo_*` containers)

```bash
infra/start-local-test.sh                  # (from road-risk-analysis/) starts everything except the simulator
docker exec -w /app -e PYTHONPATH=/app roaddemo_api_1 python -m app.demo.purge_demo        # dry run
docker exec -w /app -e PYTHONPATH=/app roaddemo_api_1 python -m app.demo.purge_demo --yes  # purge
```

The simulator container was set to `--restart=no` and stopped
(`docker update --restart=no roaddemo_simulator_1 && docker stop roaddemo_simulator_1`).
In `infra/docker-compose.demo.yml` it is also under the `demo` profile.

**Models in the local API container.** `roaddemo_api_1` mounts `ai/models`
read-only on `/models` (`infra/docker-compose.demo.yml`), so installing a model
is just copying its `.pt` + `.json` card into `ai/models/`:

```bash
docker exec roaddemo_api_1 ls /models
```

Currently installed: pothole detector + pothole classifier, traffic signs,
speed bumps, parked vehicles. The traffic-sign classifier
(`model_signs_classifier.pt`) is not trained yet, so it is hidden in the
console (shown as "1 model not installed") and the signs run single-stage.
Restart the worker after adding a model so it reloads the weights.

## Going back to demo mode

Server:

```bash
./deploy.sh --with-demo   # starts the simulator (profile demo), re-keys the 'simulator' API client,
                          # loads seed_presentation + seed_web_test
./deploy.sh --refresh     # just before a demo: re-date reports / parked vehicles
```

Local:

```bash
infra/start-local-test.sh --demo --seed   # start the simulator + load the web test data
docker exec -w /app -e PYTHONPATH=/app roaddemo_api_1 python -m app.demo.seed_presentation
```

The other seed scripts (`seed_mobile_demo`, `seed_demo_exceptions`) still work
as documented in their docstrings; `purge_demo` removes their data too.

# Presentation: the Bafoussam demo

Local demo stack `roaddemo`. The phone runs the mobile app (`mobile/`) against this machine.
All commands run from `road-risk-analysis/backend/` (the `docker run` below mounts `$PWD/app`);
the start script is `../infra/start-local-test.sh`.

## Before the talk (about 15–30 min before)

1. Stack up, plus the worker and scheduler:
   ```bash
   ../infra/start-local-test.sh
   ```
2. Make the presentation data fresh. Community reports expire after 1–3 h depending on their type, and parked vehicles after 2 h (cars in the lane) to 24 h (trucks).
   ```bash
   docker exec -w /app -e PYTHONPATH=/app roaddemo_api_1 python -m app.demo.seed_presentation --refresh
   ```
   The command re-dates the reports and parked vehicles, then prints what the app will get along the three routes. Check that each route shows about 36–46 reports, 66–79 presentation detections and the risk zones. The shortest-lived reports (embouteillage, animaux: 1 h) stay visible for about 50 min after the refresh.

   To rebuild everything from scratch (purge + seed + verify, about 15 s), run the same command without `--refresh`.
3. The simulator is already in presentation mode. Check it with
   `docker logs --tail 5 roaddemo_simulator_1`. You should see `following phone <id>` and a `wave (...)` line every 40 s (every 25 s while the phone drives).

## What the audience sees

### Route planning to Bafoussam

The seeded routes are the OSRM routes the app also gets from router.project-osrm.org:
- Kambo (N5, north of Bonabéri, 4.2414, 9.6118): 218 km;
- Akwa: 251 km;
- the phone's Douala-Est position (4.0413, 9.8105): 264 km.

All go through Mbanga, Njombé, Penja, Loum, Manjo, Nkongsamba, Melong, Kékem, Bafang, Baham and Bandjoun.

- **Community reports, every ~4–8 km**: police, embouteillage, accident, travaux, nid_de_poule, danger, obstacle, vehicule_panne, route_glissante, animaux, inondation, dos_d_ane, eboulement, vehicule_dangereux. Most have a photo and a few confirmations.
- **AI detections**, validated and grouped into active hazards:
  - speed_limit_50 entering each town and speed_limit_80 leaving the big ones;
  - speed bumps, pedestrian crossings, children signs and stop / give way in towns;
  - curves, double bend, no overtaking, speed_limit_60 and falling rocks between Nkongsamba and Bafang;
  - potholes;
  - parked vehicles: taxis on the roadside in towns, and trucks blocking a lane on the open road.
- **Historical risk zones**, recent incidents spaced 15–35 km apart. The route line changes colour at each zone. The entry banner appears only at score ≥ 0.5.

  | km (Kambo route) | score | level | dominant cause |
  |---|---|---|---|
  | 18 (before Mbanga) | 0.75 | high | hard braking |
  | 40 (Mbanga–Njombé) | 0.62 | medium | speeding |
  | 59 (Penja–Loum) | 0.95 | high | accidents |
  | 80 (Loum–Manjo) | 0.62 | medium | hard braking + accident |
  | 114 (after Nkongsamba) | 0.42 | moderate (line colour only, no banner) | harsh cornering |
  | 142 (Kékem cliff) | 0.78 | high | harsh cornering + accident |
  | 158 (Bafang) | 0.80 | high | accidents |
  | 180 (plateau) | 0.57 | medium | speeding |
  | 201 (Bandjoun) | 0.80 | high | accidents |

  On the Akwa / Douala-Est routes there is also a 0.70 hard-braking zone at the Bonabéri bridge. Older zones from other seeders show up too (km 15.6, 127, 190).

### Démo drive and collision alerts

- **Showing the collision module: set the DÉMO chip to "Lent"** (~25 m/s, 90 km/h). Each wave puts 4 simulated units on the phone's road:
  - MONITOR: in front, stops short about 90 m out;
  - WARNING: closes in from behind;
  - HIGH_WARNING: in front, holds about 6 s to conflict;
  - CRITICAL: head-on, passes the phone.

  In tests at 25 m/s along the N5 (`presentation_probe --drive 25`), every level appeared within ~20 s. Up to 5 alerts were live at once (the grouped alarm), and units broke off and their alerts cleared. Waves repeat every 25 s.
- At **"Rapide"** (80 m/s) the phone reports 40 m/s. Units still spawn ahead and behind on its road each wave, and WARNING, HIGH_WARNING and CRITICAL appear. The phone outruns them quickly, so MONITOR is rare.
- **Phone standing still on a real road** (e.g. on the N5 at Kambo): a wave every 40 s, tested. MONITOR at ~6 s, WARNING at ~10 s, HIGH_WARNING at ~22 s, CRITICAL at ~28 s. A group of 3–4 alerts lasts ~20 s, then all units leave and the alerts resolve.
- **Limitation:** if the phone stands in a side street off the main road, as at the Douala-Est home position, which is ~80 m from the main road, the backend predicts the units along the main road. You then mostly get MONITOR alerts. For the collision part, stand on a through road or use the "Lent" Démo drive.

## Simulator modes

The container `roaddemo_simulator_1` runs `app/demo/vehicle_simulator.py`.

| env | default | meaning |
|---|---|---|
| `SIM_MODE` | `presentation` (container) / `roam` (script) | `roam` = the original wandering units |
| `SIM_WAVE_SECONDS` / `SIM_WAVE_SECONDS_MOVING` | 40 / 25 | wave interval, phone still / driving |
| `SIM_UNITS_PER_WAVE` | 4 | MONITOR, WARNING, HIGH, CRITICAL (more → extra WARNING/HIGH) |
| `SIM_POOL` | 8 | registered units (ids `sim-…`) |
| `SIM_TICK_SECONDS` | 1 | ping period per unit |
| `SIM_CRUISE_MPS` / `SIM_PASS_MPS` | 11 / 13 | approach / CRITICAL closing speed |
| `SIM_HOLD_SECONDS` | 13 | time a unit holds its level before breaking off |
| `SIM_TARGET_VEHICLE_ID` | – | follow this phone (default: the latest pinging non-`sim-` vehicle) |

docker-compose 1.29 cannot recreate this container on this Docker version (`KeyError: 'ContainerConfig'`, and it deletes the old container). Switch modes with `docker run` instead.

```bash
# back to the original roaming simulator
docker rm -f roaddemo_simulator_1
docker run -d --name roaddemo_simulator_1 --network roaddemo_default --restart unless-stopped \
  -v "$PWD/app:/app/app:ro" -e API_BASE_URL=http://api:8080 -e SIM_OSRM_URL=http://osrm:5000 \
  -e SIM_MODE=roam -e SIM_NUM_VEHICLES=6 -e SIM_TICK_SECONDS=2 \
  road-analysis-api_api:latest python -u -m app.demo.vehicle_simulator
# presentation mode again: same command with  -e SIM_MODE=presentation -e SIM_TICK_SECONDS=1
```

Rehearse without the phone: a fake phone that pings like the app and prints its alerts.
```bash
docker exec -w /app -e PYTHONPATH=/app roaddemo_api_1 python -m app.demo.presentation_probe            # standing at Kambo (N5)
docker exec -w /app -e PYTHONPATH=/app roaddemo_api_1 python -m app.demo.presentation_probe --drive 25 # driving the N5
```
The simulator follows the probe only while the real phone is silent, or when it is started with `SIM_TARGET_VEHICLE_ID=<probe id>`.

## After the talk

```bash
docker exec -w /app -e PYTHONPATH=/app roaddemo_api_1 python -m app.demo.seed_presentation --purge   # removes only the presentation seed
```
Then switch the simulator back to `roam` with the command above, if wanted.

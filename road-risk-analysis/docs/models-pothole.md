# Pothole models — "pothole v1" pipeline

Status: installed 2026-10-19. Code: `app/workers/processor.py`, `app/services/model_registry.py`,
`app/services/static_check.py`, `app/services/ai_validation.py`; settings section `ai_validation`.

## 1. What is installed

| Slot (GET /models/) | File (`/models` = `ai/models`) | Card | Role |
|---|---|---|---|
| `pothole` | `model_pothole.pt` (YOLOv8s, 22 MB, sha256 `f73664cd…5a95`) | `model_pothole.json` — `pothole-det legacy-yolov8 (v1 pipeline):v1` | detector, single class `pothole`, imgsz 640 |
| `pothole_classifier` | `model_pothole_classifier.pt` (YOLOv8n-cls, sha256 `c8590e40…869e`) | `model_pothole_classifier.json` — `pothole_cls:v1` | crop classifier `not_pothole` / `pothole`, imgsz 224, `crop_pad_ratio` 0.15 |

Official "pothole v1" = the **legacy detector** (same file as `ai/models/backup-before-v1/model_pothole.pt`,
trained 2026-05-07 with ultralytics 8.4.46 from `yolov8s.pt`, 100 epochs, imgsz 640) **+ the new crop
classifier v1**. Decision of the user after a side-by-side test on real Cameroon dashcam video
(`tools/pothole_v1/out-old/` vs `out/`): the legacy detector finds 2–3× more real potholes than the
newly trained 960 px detector. Licence of both weights: AGPL-3.0 (Ultralytics).

The newly trained detector v1 stays available as `models/alt/pothole_det_v1_960.pt` + `.json`
(`pothole_det:v1`, imgsz 960, TEST-calibrated thresholds 0.7634 / 0.0568). `ai/models/backup-before-v1/`
keeps the pre-v1 detector and classifier.

### Thresholds (card → `GET /models/` `thresholds.pothole`, overridable in `ai_validation.type_overrides.pothole`)

| Field | Value | Source | Meaning |
|---|---|---|---|
| `detector_accept` | 1.0 | detector card | the detector never auto-accepts alone (no calibrated precision for this model) |
| `detector_reject` | 0.10 | detector card | sanity floor on detector confidence. The worker only keeps boxes ≥ `CONF_THRESHOLD` (0.25) anyway, so it only matters if that cut is lowered; under 0.10 this model's boxes are almost always noise |
| `auto_accept` | 1.0 | classifier card | never (95 % precision was not reached on TEST) |
| `auto_reject` | 0.0738 | classifier card | P(pothole) ≤ 0.0738 → auto-reject `ai_verifier` (TEST: −1.9 pt recall) |

Net effect in assisted / automatic mode: **no pothole is auto-accepted**; potholes are auto-rejected
by the classifier or by the false-positive rules below; everything else goes to human review.

Processing uses the card values: the detector runs at the card `imgsz` (640) — `processor._predict_kwargs`;
the classifier crop is the box padded by `crop_pad_ratio` × box size (0.15) — `model_registry.crop_box` —
and resized to the card `imgsz` (224).

## 2. False-positive rules (settings section `ai_validation`)

Observed false positives of the legacy detector: large boxes over the roadside (red earth, bushes,
building edge) whose centre is often low in the image, and trees / sky / OSD text above the horizon.
The classifier does **not** catch them (it gives P(pothole) 0.6–0.97 to many tree crops), hence rules
on the geometry of the stored (best) box of a finished track — `static_check.pothole_rule_check`:

| Setting | Default | Rule |
|---|---|---|
| `pothole_min_y_ratio` | 0.45 | road-area line: box centre above it (0 = top, 1 = bottom of the image) |
| `pothole_off_road_action` | `reject` | above the line → `reject`: auto-reject **`ai_off_road`**; `review`: only blocks auto-accept (accept blocker `off_road_area`, as before) |
| `pothole_max_width_ratio` | 0.40 | box wider than 40 % of the frame → auto-reject **`ai_box_too_large`** (1 = off) |
| `pothole_max_area_ratio` | 0.12 | box area larger than 12 % of the frame → `ai_box_too_large` (1 = off) |
| `pothole_reject_edge_touching` | true | box touching the left / right border (within 1 %) **and** wider than 25 % → `ai_box_too_large` |

Mode handling (`ai_validation.rule_reject`, same as the parked-vehicle rules):
- **assisted / automatic**: `rejected`, `ai_decision = auto_reject`, `rejection_code` = the first rule
  code, `rejection_source = auto`, actor `system`; reversible via reopen. Audit sampling applies (a
  sampled one stays pending with the rejection as suggestion).
- **manual**: no automatic decision — the detection waits for a human with the reason
  `manual mode — AI suggests reject (ai_box_too_large: …)` (review hint).

Recorded: `ai_decision_reason` (`"<code>: <detail>[; <code>: <detail>]"`), the creation history entry
(`details.rule_reject`, `details.verdict`), and `ai_details` =
`{pipeline: "pothole_rules", verdict: <code>, reasons: [{code: pothole_road_area | pothole_box_width |
pothole_box_area | pothole_side_edge, value, threshold, passed, detail}], features: {width_ratio,
area_ratio, centre_y_ratio, touches_side}}` (only when a rule fires). The classifier score is still
stored. Rejection reasons `ai_off_road` and `ai_box_too_large` are seeded for type `pothole` at startup.

Web console: Settings → AI detection → Potholes → "Decision thresholds" (types/defaults in
`lib/ai-models-api.ts` `POTHOLE_RULE_DEFAULTS`; EN/FR/ES strings in `lib/i18n/extra/`).

Known gap: narrow tall roadside boxes at the image side (≈ 20 % wide, ~8 % area, e.g. LTTR 0:25–0:29)
pass every rule and go to review. Lowering `pothole_max_area_ratio` to ~0.06 would catch some of them,
at the risk of rejecting close real potholes (the near-camera ones are wide).

## 3. Swapping back to the 960 px detector

```bash
cd ai/models
cp alt/pothole_det_v1_960.pt   model_pothole.pt
cp alt/pothole_det_v1_960.json model_pothole.json
# restart the worker (and the API so GET /models/ re-reads the card), e.g.
docker restart roaddemo_api_1            # local demo stack
docker compose restart api worker        # infra/docker-compose.yml stack (from infra/)
```
To return to the legacy detector: `cp backup-before-v1/model_pothole.pt model_pothole.pt && cp
alt/pothole_det_legacy_640.json model_pothole.json` (copy of its card; `models/` is git-ignored).
The worker reads the card `imgsz` (960 for the alt model) and its thresholds automatically. Nothing
else changes; the false-positive rules apply to any pothole detector.

## 4. Installing a v2

1. Train in `ai/training/pothole/`; it writes `model_pothole.pt` + `model_pothole.json` (detector) and
   `model_pothole_classifier.pt` + `.json` (classifier) — card format in `app/services/model_registry.py`
   (`model_type`, `name`, `version`, `classes`, `imgsz`, `thresholds.{auto_accept, auto_reject,
   target_precision}`, `sha256`, classifier `crop_pad_ratio`).
2. Compare on real video before installing (`tools/pothole_v1/run_test.sh`, `OUT=… --det …`).
3. Move the current files to `models/backup-before-v2/`, copy the new ones into `models/`.
4. Restart, then check `GET /models/`: both slots `present`, `card_error` null,
   `sha256_matches_card` true, `thresholds.pothole` the expected values.
5. Review the `ai_validation.pothole_*` rules against the new detector's false positives (they are
   model-independent and may be relaxed — ratio 1 / `review` / false — if v2 no longer needs them).

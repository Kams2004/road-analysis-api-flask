# Training data from validation decisions

During the test phase every decision taken on a detection — by a validator or by the AI validation mode — feeds a
clean, organised training dataset. Nothing has to be exported by hand: the platform keeps it up to date, a human can
adjust the boxes in Label Studio, and the result is a group-split YOLO dataset for the fine-tune recipe.

```
video job ──▶ detection (annotated frame + clean frame + crop)
                 │ accept / correct (class, box, location) / reject (reason) / reopen   ── human or automatic
                 ▼
training_samples (1 per detection)  +  training_frames (1 per type × video frame)   ── PostgreSQL
                 │ sync (after the decision · end of job · beat task every 2 min)
                 ▼
MinIO bucket `datasets`:  <type>/detector/{images,labels}/   <type>/classifier/<class>/
                 │ GET /datasets/export  (manifest | zip)
                 ▼
dataset kit:  platform-pull ─▶ Label Studio (adjust boxes) ─▶ export (group split) ─▶ fine-tune
                                     └──▶ platform-push (frames → box_reviewed)
```

## 1. What the processor stores per detection

| field | content |
|---|---|
| `image_url` | annotated frame (box + label drawn) — shown to validators, never used for training |
| `frame_clean_url` | **clean full frame**, no drawing — `detections/{job}/frames/f{frame:06d}_clean.jpg` (once per job frame, JPEG q92) |
| `frame_width`, `frame_height` | size of that frame (box coordinates are pixels of it) |
| `crop_url` | clean crop with 20 px padding (legacy) |
| `bbox_x1..y2` | the model's box (kept for traceability) |
| `corrected_bbox` | `[x1,y1,x2,y2]` when a validator moved / resized the box (`null` otherwise) |

Detections stored before this existed have no clean frame: they still give a classifier crop (copied from `crop_url`)
but no detector image (`training_frames.status = no_image`).

## 2. What happens on each decision

| decision | training sample (`training_samples`, id = detection id) | detector frame | classifier crop |
|---|---|---|---|
| accept (`validated`) | `decision=accepted`, class from the detector card | box added to the frame label | `<type>/classifier/<class>/<id>.jpg` |
| accept with `bbox` / correct (class, box or location) (`corrected`) | `decision=corrected`, `corrections=["box","class","location"]`; the **corrected box overrides** the predicted one; a class correction moves the sample to the new type | box (corrected) in the label of the new type | crop of the corrected box, in the new class folder |
| reject, reason of kind `false_positive` | `decision=rejected`, `reason_code`, `rejection_kind=false_positive`, no detector class | contributes **no box**: a frame with only such rejections = **background** (empty `.txt`); if other accepted boxes exist on the same frame they stay | `<type>/classifier/not_<type>/<id>.jpg` (`not_pothole`, `not_speed_bump`, signs: `not_a_sign`, or the classifier card's reject class) |
| reject, reason of kind `real_redundant` | `decision=rejected`, `rejection_kind=real_redundant`, detector class like an accepted one | **positive**: its box (the corrected one if any) is added to the frame label | `<type>/classifier/<class>/<id>.jpg` — the positive class |
| reject, reason of kind `unusable` | `decision=rejected`, `rejection_kind=unusable`, `excluded=true` (listed in the manifest for traceability) | **nothing**: no box, and no frame is created just for it (a frame left with only unusable rejections is removed) | none (an earlier crop is deleted) |
| automatic decision (AI mode) | same, `decided_by=system`, `reviewer=system` | same | same |
| reopen | `state=superseded` | box removed; a frame with no active decision left is **deleted** from the bucket | crop deleted |
| resolve / unresolve | unchanged (still accepted) | — | — |

* **Rejection kinds.** Every rejection reason (`rejection_reasons.kind`, `GET /rejection-reasons`) says what a
  rejection teaches the models:

  | kind | meaning | built-in reasons |
  |---|---|---|
  | `false_positive` — *AI mistake (teaches the model)* | the AI was wrong: a **negative** example | not_road_side, ai_verifier, ai_off_road, ai_box_too_large, shadow, water_reflection, manhole, road_marking, dirt_patch, crack_or_tyre_marks, road_patch, broken_edge, vegetation, debris, ai_not_a_sign, person_as_sign, bike_as_stop (*Vehicle / motorbike part*), pole_as_sign, street_light, billboard, vehicle_sticker, tree_as_sign, road_edge, ditch, pothole_or_damage, marking_or_crossing, ai_not_grounded, ai_traffic_queue, ai_moving_vehicle (parked vehicles: "not parked" → negative of the parked-vehicle classifier), not_a_vehicle |
  | `real_redundant` — *Real but redundant* | the object is real, the detection is only redundant: a **positive** sample | duplicate, speed_bump_ok |
  | `unusable` — *Can't judge* | the object cannot be judged: **excluded** from training | image_quality, out_of_frame, low_confidence, sign_not_visible |

  Custom reasons default to `false_positive`; their kind can be set when created and changed later
  (`PATCH /rejection-reasons/{id}`, Settings → Hazards → Rejection reasons), which re-files the past rejections with
  that code. The built-in table lives in `app/modules/m06_hazards/rejection_seeds.py` (seeded idempotently at start).
  Samples filed before kinds existed are re-filed once at API start; to recompute all of them (idempotent):
  `python -m app.cli resync-training-rejections` (prints crops per class / frames / excluded before and after and
  writes the bucket objects: redundant crops move to the positive folder, unusable crops and frames are removed).
* **Several detections on the same frame** (same type) → **one** detector sample whose label holds every
  accepted / corrected box. Each type has its own detector dataset (a pothole frame and a sign frame of the same video
  frame are two samples).
* Classes: index = position in the detector card's `classes` (`pothole` → 0; signs two-stage detector:
  `traffic_sign`=0, `traffic_light`=1; parked vehicles: `car, motorcycle, bus, truck`); written to
  `<type>/detector/classes.txt`. Classifier folders: the accepted class (traffic signs: the sign code) or the reject class.
* Crops use the classifier card's padding (`crop_pad_ratio`, pothole 0.15), else 20 px — the same as the live pipeline
  (`model_registry.crop_box`).
* **Box review state** per frame: `needs_box_review` (label built from the decisions: other objects of the frame may
  be unboxed) → `box_reviewed` once the whole frame was reviewed in Label Studio and pushed back. Reviewed boxes
  override the decisions' boxes; if an earlier decision of that frame changes later (reopen, re-decision) the frame goes
  back to `needs_box_review` with the decisions' boxes.
* Recording is done in the decision's transaction (`app.modules.m06_hazards.training_data.record_decision`, in a SAVEPOINT:
  it can never block a validation); the MinIO writes happen after the commit (API background task, worker at the end
  of a job, Celery beat `sync_training_data` every 2 min retries failures, also on API start). Everything is
  idempotent (deterministic keys).

## 3. Where it is stored

**MinIO bucket `datasets`** (next to the immutable `<type>/v<N>/` snapshots of `POST /datasets/export`):
```
<type>/detector/classes.txt
<type>/detector/images/<frame_key>.jpg          clean frame
<type>/detector/labels/<frame_key>.txt          YOLO "<class> <cx> <cy> <w> <h>" normalised; empty = background
<type>/classifier/<class>/<detection_id>.jpg    padded crop
```
`frame_key` = `<video slug>-<job id[:8]>__z<minute>__t<ms>`, e.g. `LTTR_458_AV_24_04_2-fa79181b__z002__t00151200`
(minute 2, t = 151.200 s). It is a dataset-kit stem: everything before the second `__` (`video + job + minute`) is the
**leakage group** (`group_key`), so all frames of the same minute of the same video go to the same split.

**PostgreSQL**: `training_samples` (decision, state active/superseded, decided_by human/system, reviewer, reason,
corrections, class / class_index / classifier_class, effective box + box_corrected, frame_key, group_key, job, video
name, frame number, video time, recorded_at, GPS, confidence, model version + card, crop_key, sync status) and
`training_frames` (type, frame_key, group, size, image/label keys, current `labels`, n_boxes, status
active/empty/no_image, review_status, reviewed boxes + reviewer, dirty / sync error). Schema: `migrations/add_training_data.sql`
(applied automatically by `app/core/db/upgrade.py`).

**API** (permission `datasets`; Swagger tag *Datasets*):
| | |
|---|---|
| `GET /datasets/training/summary` | per type: accepted / corrected / rejected (of which `excluded` = unusable, `redundant_positive` = real_redundant), detector frames (positive / background), crops per class, box_reviewed / needs_box_review, sync state |
| `GET /datasets/samples?detection_type=&decision=&review_status=&state=&decided_by=&since=&skip=&limit=` | the samples (paginated, `total`) |
| `GET /datasets/export?type=pothole&format=manifest\|yolo[&since=&review_status=&decided_by=]` | JSON manifest with download paths, or a zip (bucket layout + `data.yaml`, `frames.csv`, `samples.csv`, `manifest.json`) |
| `GET /datasets/files/<key>` | one object of the bucket |
| `PUT /datasets/frames/<type>/<frame_key>/labels` (datasets *edit*) | reviewed boxes `{boxes:[{class_name, cx, cy, w, h}], reviewer}` → `box_reviewed` |
| `POST /datasets/training/sync` (datasets *add*) | write pending objects now |
| `POST /detections/{id}/accept` / `correct` with `"bbox": [x1,y1,x2,y2]` | correct the box (pixels of the frame) |

**Web console**: *Validations → Training data* (`/training-data`): counts per type, last sync, *Download training
export* (the zip), *Write pending now*, and the Label Studio box review (§4a).

## 4. Adjusting the boxes in Label Studio

### 4a. From the web console (server Label Studio) — the normal way

The stack runs its own Label Studio (`labelstudio` service: server `http://<host>:18085`, local
`http://localhost:8086`). The API talks to it with a token; nobody copies session or Label Studio tokens.

*Training data* page, per detection type (column *Box review*):

1. **Send to Label Studio** — creates the project `VIASafe – <type>` (classes of the installed model card) and one
   task per frame still `needs_box_review` whose image is written, with the frame's current boxes as
   pre-annotations and a header line (frame, video time, decisions). Re-running adds only new frames; a frame
   whose review was reset by a later decision gets a fresh task.
2. **Open Label Studio** / the *Label Studio: N tasks · …* link — log in with the account in
   `infra/secrets/labelstudio.env` (`LABEL_STUDIO_USERNAME` / `LABEL_STUDIO_PASSWORD`), correct the boxes,
   **Submit** every frame (empty = no object = background), **Skip** when you cannot judge.
3. **Fetch corrections** — every annotation submitted or edited since the last fetch replaces its frame's boxes
   (`box_reviewed`, label file rewritten; same write-back as `PUT /datasets/frames/…/labels`). Skipped tasks are
   ignored. Safe to click any time; an annotation is applied once (`labelstudio_tasks.applied_key`).

Images: tasks point at `GET /labelstudio/image/<type>/<frame_key>.jpg?s=<HMAC(SECRET_KEY)>` on the public API, so
the annotator's browser loads them without a console session; any other `datasets` object stays private.

Configuration — `infra/secrets/labelstudio.env` (chmod 600, never committed, never pushed by `push.sh`):
generated by `deploy.sh` on the server; it is both the `labelstudio` container's `env_file` (admin account,
legacy API token, sign-up disabled) and read by the API (`VIASAFE_LS_URL`, `VIASAFE_LS_PUBLIC_URL`,
`VIASAFE_PUBLIC_API_URL`, `LABEL_STUDIO_USER_TOKEN`). Endpoints: `GET /labelstudio/status`,
`POST /labelstudio/{type}/send` (datasets:add), `POST /labelstudio/{type}/fetch` (datasets:edit).

### 4b. Offline, with the dataset kit

From `ai/training/pothole-dataset` (the dataset kit, Label Studio runs in Docker on http://localhost:8085,
`--cpus 2 -m 2g`):
```bash
cd ai/training/pothole-dataset
API=http://localhost:8080      # or the server URL
# a session token (not needed when the API runs with AUTH_ENFORCED=false):
TOKEN=$(curl -s -X POST $API/auth/login -H 'Content-Type: application/json' \
        -d '{"email":"you@viasafe.cm","password":"..."}' | python3 -c 'import json,sys;print(json.load(sys.stdin)["token"])')

./run.sh platform-pull --api $API --token $TOKEN --type pothole          # [--since 2026-10-20] [--review-status needs_box_review]
#   -> work/platform/pothole/{images,labels,crops/<class>}/  classes.txt  manifest.json  frames.csv  samples.csv
#      labelstudio_tasks.json (one task per frame, the platform boxes as predictions)  label_config.xml
./run.sh labelstudio up                                                  # serves work/frames and work/platform
#   first time: open http://localhost:8085, sign up (local admin), Account & Settings -> Personal Access Token
./run.sh labelstudio setup --source platform --type pothole --token <LS token>
#   project "VIASafe pothole - platform review" (classes of the type), local storage /label-studio/files/platform
#   (not synced), imports the tasks not yet in the project (re-run after every pull)
```
In Label Studio: for each frame fix / delete / **add** boxes for every object of the type (the platform label only has
the boxes the validators decided; other potholes on the frame must be added), **Submit** (also with no box: reviewed
background). Then *Export → JSON* to e.g. `work/ls_platform_pothole.json`.
```bash
./run.sh platform-push work/ls_platform_pothole.json --api $API --token $TOKEN --type pothole   # [--dry-run]
#   PUT /datasets/frames/pothole/<frame_key>/labels for each annotated frame -> box_reviewed, label file rewritten
./run.sh labelstudio down                                                # stop (data kept in work/labelstudio)
```

## 5. Building the training dataset

```bash
# reviewed platform frames (+ the kit's own video frames for pothole: give both Label Studio exports, they are merged)
./run.sh export work/ls_export.json work/ls_platform_pothole.json --type pothole --out work/export/cameroon
#   {train,val,test}/{images,labels}, data.yaml, manifest.csv; split by group (video+job+minute, or --by video)
# other types: --type traffic_sign | speed_bump (classes from work/platform/<type>/classes.txt)
```
Without Label Studio (decisions only, boxes not reviewed): download *Training export* in the web console or
`curl -H "Authorization: Bearer $TOKEN" -o pothole.zip "$API/datasets/export?type=pothole&format=yolo"` — the zip is
not split; split it by `group_key` (frames.csv) or use it as a `yolo_dirs` source with
`group_regex: '^(.+?__[zn]\d+)__t\d+'`.
Classifier data: `work/platform/<type>/crops/<class>/` (or `<type>/classifier/` in the zip) — `pothole` /
`not_pothole` folders for the crop classifier.

## 6. Next: fine-tune, compare, install

```bash
# from this machine (see ai/training/pothole-dataset/README.md §6)
rsync -a --delete ai/training/pothole-dataset/work/export/cameroon/ user@server:~/viasafe-pothole/work/data/cameroon_src/
# on the server
cd ~/viasafe-pothole && C=/project/overlays/finetune_cameroon.yaml
rm -rf work/data/raw/cameroon && ./run.sh reset verify_convert --config $C --purge   # when re-exporting
./run.sh download --config $C && ./run.sh verify_convert --config $C && ./run.sh train_detector --config $C
./run.sh all --config $C          # optional: calibrated cards + install bundle in models_ft_cm/install/
```
Compare before installing: `./run.sh scan VIDEO --det <runs_ft_cm/.../best.pt> --work work_ft` (kit) against the
current model on the same videos (zones / kept boxes in `report.html`), and the test metrics in the training report.
Install: copy the new `model_pothole.pt` **and its card** `model_pothole.json` (classes, imgsz, thresholds, sha256) into
the API's model directory (`/models`), check *AI models* in the web console (card present, sha256 matches), then new
jobs use it; detections keep `model_version` / `model_card_version`, so the samples of each model stay traceable.

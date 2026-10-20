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
| reject (with reason) | `decision=rejected`, `reason_code`, no detector class | contributes **no box**: a frame with only rejections = **background** (empty `.txt`); if other accepted boxes exist on the same frame they stay | `<type>/classifier/not_<type>/<id>.jpg` (`not_pothole`, `not_speed_bump`, signs: `not_a_sign`, or the classifier card's reject class) |
| automatic decision (AI mode) | same, `decided_by=system`, `reviewer=system` | same | same |
| reopen | `state=superseded` | box removed; a frame with no active decision left is **deleted** from the bucket | crop deleted |
| resolve / unresolve | unchanged (still accepted) | — | — |

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
* Recording is done in the decision's transaction (`app.services.training_data.record_decision`, in a SAVEPOINT:
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
(applied automatically by `app/db/upgrade.py`).

**API** (permission `datasets`; Swagger tag *Datasets*):
| | |
|---|---|
| `GET /datasets/training/summary` | per type: accepted / corrected / rejected, detector frames (positive / background), crops per class, box_reviewed / needs_box_review, sync state |
| `GET /datasets/samples?detection_type=&decision=&review_status=&state=&decided_by=&since=&skip=&limit=` | the samples (paginated, `total`) |
| `GET /datasets/export?type=pothole&format=manifest\|yolo[&since=&review_status=&decided_by=]` | JSON manifest with download paths, or a zip (bucket layout + `data.yaml`, `frames.csv`, `samples.csv`, `manifest.json`) |
| `GET /datasets/files/<key>` | one object of the bucket |
| `PUT /datasets/frames/<type>/<frame_key>/labels` (datasets *edit*) | reviewed boxes `{boxes:[{class_name, cx, cy, w, h}], reviewer}` → `box_reviewed` |
| `POST /datasets/training/sync` (datasets *add*) | write pending objects now |
| `POST /detections/{id}/accept` / `correct` with `"bbox": [x1,y1,x2,y2]` | correct the box (pixels of the frame) |

**Web console**: *Validations → Training data* (`/training-data`): counts per type, last sync, *Download training
export* (the zip), *Write pending now*.

## 4. Adjusting the boxes in Label Studio

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

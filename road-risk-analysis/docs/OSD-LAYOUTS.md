# Dashcam overlay (OSD) layouts

Requirement **RRAP-05-014**: *"Overlay region and GPS text format shall be configurable by
supported camera/source type."*

Dashcams burn the recording date-time, the GPS fix (NMEA `ddmm.mmmm`), the speed, the plate…
into the video (the *overlay*, *OSD*). The processor reads it by OCR (PaddleOCR) on a few
regions of each processed frame. Where those regions are depends on the **equipment**: each
model has its own OSD configuration screen where every text field is placed by the X / Y of
its top-left corner on a **0–1000 grid** (per-mille of the frame width / height).

Code: `backend/app/modules/m05_ai_detection/`
- `osd_layouts.py` — built-in profiles, region derivation, pure text parsing, auto-detection
  scoring (standard library only; a verbatim copy lives in the pothole dataset kit,
  `ai/training/pothole-dataset/potholeds/osd/layouts.py` — keep both identical);
- `osd_parser.py` — OCR, `OsdReader` (one per job), image variants, neighbour-frame retries;
- `osd_settings.py` — settings section `osd`, job layout resolution.

## Built-in profiles

### `total` — "TOTAL" equipment (aliases `camtrack_top`, `camtrack`)

Small text at the top. Videos: `LTTR 458 AV_24_04 (2).mp4`, `LT-183-GO_3.mp4`, `TEMCM111_*`,
`TEMCM127_*` (some recorded 720×288 = one field per frame; the OCR stretches the regions back
to the 720×576 aspect). Line height 50 ‰.

| Field (device screen) | Kind          | Enabled | X   | Y   | Example                            |
|-----------------------|---------------|---------|-----|-----|------------------------------------|
| Date & time           | `date_time`   | yes     | 50  | 50  | `24/04/2026 08:33:09[ACC:ON]`      |
| GPS                   | `gps`         | yes     | 500 | 50  | `0515.3490,N,01013.5519,E,016KM/H` |
| Vehicle No.           | `vehicle_no`  | yes     | 50  | 100 | `LTTR458AV`                        |
| Driver ID             | `driver_id`   | **no**  | 150 | 100 |                                    |
| User Def              | `user_def`    | yes     | 500 | 100 | `CAMTRAD` (company)                |
| RPM                   | `rpm`         | yes     | 50  | 150 | `RPM:0r/min`                       |
| Driver name           | `driver_name` | yes     | 500 | 150 | `BOUBAKARI_BABIGUIR.CAMTRAD`       |

### `besafe` — "Besafe" equipment (aliases `camaco_big`, `camaco`)

Large text: GPS on top, date-time and plate at the bottom. Videos: `Mint0639_ch2_*` (704×576).
Line height 65 ‰.

| Field (device screen) | Kind          | Enabled | X   | Y   | Example                                    |
|-----------------------|---------------|---------|-----|-----|--------------------------------------------|
| GPS                   | `gps`         | yes     | 20  | 40  | `0348.2130,N,01014.8332,E,051KM/H ACC:ON`  |
| Driver information    | `driver_name` | yes     | 20  | 100 | `ID: Name:` (usually empty)                |
| Date & time           | `date_time`   | yes     | 20  | 900 | `20-10-2026 14:29:42`                      |
| Vehicle No.           | `vehicle_no`  | yes     | 500 | 900 | `LTTR704AC CAMACO` (plate + company)       |

### `ola_energy` — dashcam mainly used at Ola Energy (alias `ola`)

Besafe-like; the bottom-right field is the camera channel instead of the plate. **No sample
video verified yet** (unit tests on synthetic OCR text only). Line height 65 ‰.

| Field (device screen) | Kind          | Enabled | X   | Y   | Example               |
|-----------------------|---------------|---------|-----|-----|-----------------------|
| GPS                   | `gps`         | yes     | 20  | 50  | `0348.2130,N,…,E,051KM/H ACC:ON` |
| Driver information    | `driver_name` | yes     | 20  | 100 | `ID: Name:`           |
| Date & time           | `date_time`   | yes     | 20  | 900 | `20-10-2026 14:29:42` |
| Ch                    | `channel`     | yes     | 500 | 900 | `CH2` (never used as vehicle id) |

## From anchors to OCR regions

For every **enabled** field (`derive_rois`, mirrored in the web preview `deriveOsdRois`):

```
x0 = x − x_margin (20)          y0 = y − y_margin (15)
x1 = x of the next enabled field on the same line (|Δy| < line_height / 2), else 1000,
     or x + width when the field has a width
y1 = y + line_height (field's, else the profile's)
```

Disabled fields are ignored (the disabled *Driver ID* of `total` does not cut the plate region).

## Adding a new equipment

1. Open the dashcam's OSD configuration screen and note, for every field shown, X, Y and
   whether it is enabled.
2. Web console → **Settings → Jobs → Dashcam overlay layouts**: choose the closest profile,
   **New profile from this one**, give it a name (`a-z 0-9 _ -`), then copy the X / Y / enabled
   values field by field. Adjust **Line height** to the font (small ≈ 50, large ≈ 65).
3. **Frame preview → Choose an image…**: pick a screenshot of a video of that equipment; the
   regions are drawn over it. Each coloured box must contain its whole text line (and only it).
4. Optional: date formats (`DD/MM/YYYY`, `DD-MM-YYYY`, `YYYY-MM-DD`, `MM/DD/YYYY` …), camera
   clock time zone, a custom `gps_regex` (named groups `lat`, `ns`, `lon`, `ew`, optional
   `speed`) or `plate_regex` (API only).
5. Save. New jobs include the profile in the automatic detection (unless *Candidate of the
   automatic detection* is unticked). To force it: `by_vehicle` (plate → layout), `by_source`
   (`upload` | `google_drive` | `onedrive` | `sftp` | `stream`, or a stream / connection id →
   layout), the **Layout of new jobs**, or `osd_layout=<name>` on `POST /jobs/`.

API: `GET /jobs/osd-layouts` lists every profile with its fields and derived regions;
`PUT /platform-settings/osd` updates the section:

```json
{
  "default_layout": "auto",
  "profiles": {
    "acme": {"label": "ACME DVR", "line_height": 55,
             "fields": [{"field": "date_time", "enabled": true, "x": 30, "y": 30},
                        {"field": "gps", "enabled": true, "x": 30, "y": 940},
                        {"field": "vehicle_no", "enabled": true, "x": 600, "y": 30}],
             "date_formats": ["YYYY-MM-DD"], "timezone": "Africa/Douala"}
  },
  "by_vehicle": {"LTTR704AC": "besafe"},
  "by_source": {"sftp": "total"},
  "gps_box": [1.0, 13.5, 8.0, 16.5],
  "probe_frames": 5, "full_frame_fallback": true, "neighbour_retry": true
}
```

A profile with the name (or an alias) of a built-in one replaces it; removing it restores the
built-in.

## Layout of a job

`osd_layout` job option (`auto` | profile | `none`) → `by_source` (stream / connection id, then
source type) → `default_layout`. With `auto`, the first `probe_frames` processed frames **with
content** (black / uniform start-up frames skipped) are read with every candidate profile; each
parse scores 1 for a valid GPS fix + 1 for a valid date-time + 0.25 for a plate or a channel
(which tells `besafe` from `ola_energy`); the best total wins (ties → profile order). A plate
listed in `by_vehicle` forces its layout as soon as it is read. Nothing parses → `full_frame`
(whole-frame OCR + regex search); still nothing → `none` (OCR stopped for the job).

The decision is recorded as job event `osd_layout` (`layout`, `method` = `auto` | `job` |
`source` | `default` | `vehicle` | `fallback`, detection scores, regions) and in
`jobs.osd_layout`; the `done` event carries `osd` read statistics. The job detail page shows it.

## Reading robustness

- OCR text normalisation: `O`/`Q`/`D` ↔ `0`, split numbers, dots or nothing as separators,
  dropped decimal dot (`05156127N`), lost leading 0 (`516.2659`), leading 0 read as 9 or 4
  (`9407.0866` — only accepted when the repaired fix is inside `gps_box`), minutes ≥ 60
  rejected, fixes outside `gps_box` (Cameroon with margin by default) rejected, the speed must
  have its 3 zero-padded digits (`016KM/H`, `KM H`, `KM/`, `HMH` variants accepted).
- Date-time: separators lost or misread (`24/04/20260833.30`, `20-10-202614:34:39`);
  a reading must agree with the video clock (± 3 s from the last good one); a misread day is
  repaired from the majority date; a date in the future is rejected; when only the time of
  day is readable it is combined with the current date; two mutually consistent readings that
  disagree with the anchor re-anchor it.
- Plate: letters / digits fixed by position (`LITR7Q4AC` → `LTTR704AC`, Cameroon region code
  check); plate, company and driver are majority-voted and stop being OCR'd once stable.
- **Colour transitions** (white overlay text over the sky…): when GPS or date-time is
  missing, of low OCR confidence, outside the box or rejected by the GPS jump check, the region
  is re-read on image variants (2× upscale, per-channel max, inverted), then on the frames
  −1 s, +1 s, −2 s, +2 s (`neighbour_retry`, at most every 2 s of video). A GPS fix found on
  both sides is interpolated to the frame time; one side only → nearest. The source is stored:
  `detections.location_source = ocr_neighbour` (confirmed), `job_track_points.source`.
  Otherwise the GPS voter window gives an `interpolated` (approximate) position.

## Recording time (`recorded_at`)

The OSD date-time is the camera's local clock: it is stored timezone-aware with the profile's
`timezone`, else `settings.OSD_TIMEZONE` (`Africa/Douala`). Frames whose time is unreadable get
the nearest OSD time ± the video-time difference (pipeline `_JobContext.recorded_at`).

*Root cause of the empty `recorded_at` before this change*: the date-time parser required a
space between date and time, but its own normalisation removed spaces between digits
(`24/04/2026 08:33:09` → `24/04/202608:33:09`), and PaddleOCR on the small font usually loses
the space and colons anyway (`24/04/20260833.30`). No OSD time was ever parsed, so every
detection and track point had `recorded_at = NULL`.

## Context clips

Detections whose location is not confirmed (`location_pending`) get a ±`validation.clip_seconds`
clip so the validator sees the overlay before / after the detection. Clips are encoded as
H.264 MP4 through `ffmpeg` (installed in the API image) — the OpenCV pip wheel has no H.264
encoder (`avc1` failed with `codec_id=27`, so no clip was ever written) — else VP8 WebM through
OpenCV; both play in the browser and are uploaded with their video content type.

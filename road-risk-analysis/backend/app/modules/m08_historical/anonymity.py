"""
The driving events are anonymous: the platform keeps what happened, where, when and how fast —
never who drove or which vehicle. Nothing identifying a driver or a vehicle is stored: no
driver / vehicle column in `historical_events` or its daily summaries, and the source payload
kept in `raw` is reduced to the whitelisted fields below (every source, every import path).

RAW_KEYS are the non-identifying fields of the provider exports (daily CSV files —
sources/ymane_csv.py — and the Ymane API — sources/ymane.py): exception type and id, times,
positions, speeds and thresholds, level, durations / distances, the customer affiliate
(country) and the transporter (the fleet operator — a company, not a vehicle or a person), and
the provider's processing flags. Everything else is dropped — in particular `vehicledesc`
(plate), `driver_name`, `unitid` / `vehicleid` / `driverid`, `speedid` and `evidence` (both
carry the unit id), `usercoments` (free text), `restoreduserid` and `aidetectiondetails`.

app/core/db/upgrade.py applies the same whitelist to the events stored before (ANONYMISE_*).
"""
from typing import Any, Dict, Optional

RAW_KEYS = (
    # daily exception files
    "exceptionid", "exception_type_name", "startdatetime", "enddatetime", "exceptionday",
    "startgps", "endgps", "vmax", "threshold", "maxvalue", "level",
    "totalduration", "totaldistance", "distanceunderexception", "timeexceeded",
    "status", "requiredbreak", "numberofbreak", "maxbreak", "invaliddata", "datasource",
    "treated", "mergetreated", "preventivedatetime", "preventivethreshold",
    "customer_affiliate_name", "transporter_name", "isrestored", "restauredate",
    "aireason", "aidetectionvalid",
    # Ymane API records
    "exceptiontype", "affiliateid", "transporterid",
)
_KEYS = frozenset(RAW_KEYS)


def clean_raw(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The whitelisted, non-empty fields of a source payload (keys compared in lower case)."""
    out: Dict[str, Any] = {}
    for k, v in (raw or {}).items():
        key = str(k).strip().lower() if k is not None else ""
        if key in _KEYS and v not in (None, ""):
            out[key] = v
    return out

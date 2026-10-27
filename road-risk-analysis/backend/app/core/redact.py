"""
Secrets out of error answers: a 422 from FastAPI echoes the offending `input`, which for a
password field (or a body-level error) would send the password back and into any log that
keeps response bodies. `validation_errors` drops those values.
"""
from __future__ import annotations

import re
from typing import Any

SECRET_NAME = re.compile(r"pass(word|phrase)?|secret|token|otp|api_?key|credential", re.I)
REDACTED = "[redacted]"


def is_secret_name(name: Any) -> bool:
    return isinstance(name, str) and bool(SECRET_NAME.search(name)) and not name.endswith("_key_fp")


def scrub(value: Any) -> Any:
    """Copy of `value` with every secret-named key's value replaced."""
    if isinstance(value, dict):
        return {k: (REDACTED if is_secret_name(k) and v not in (None, "") else scrub(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v) for v in value]
    return value


def validation_errors(errors: list[dict]) -> list[dict]:
    out = []
    for e in errors:
        e = dict(e)
        if any(is_secret_name(part) for part in e.get("loc", ())):
            e.pop("input", None)
            e.pop("ctx", None)
        elif "input" in e:
            e["input"] = scrub(e["input"])
        out.append(e)
    return out

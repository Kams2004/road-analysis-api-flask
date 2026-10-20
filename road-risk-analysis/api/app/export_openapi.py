"""
Write the OpenAPI document of the API to docs/openapi.json (RRAP-12-014).

    python -m app.export_openapi                 # → docs/openapi.json
    python -m app.export_openapi path/to/out.json

The live document is served at /openapi.json (Swagger UI /docs, ReDoc
/redoc); this committed copy lets the API be reviewed or used to generate
clients without a running server. tests/test_openapi.py fails when it is
out of date.
"""
import json
import sys
from pathlib import Path

DEFAULT_OUT = Path(__file__).resolve().parent.parent / "docs" / "openapi.json"


def render() -> str:
    from app.main import app
    return json.dumps(app.openapi(), indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str]) -> int:
    out = Path(argv[0]) if argv else DEFAULT_OUT
    text = render()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    doc = json.loads(text)
    ops = sum(1 for item in doc["paths"].values() for m in item if m in
              ("get", "put", "post", "delete", "patch", "head", "options"))
    print(f"Wrote {out} — {len(doc['paths'])} paths, {ops} operations, OpenAPI {doc['openapi']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

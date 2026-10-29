"""One-off UEX API 2.0 exploration: fetch each endpoint and save the raw JSON.

Usage: python tools/explore_api.py [endpoint?query ...]

Reads UEX_TOKEN from .env if set (sent as a Bearer header, never printed).
Full responses go to fixtures/raw/ (gitignored, can be large); a trimmed
sample (first 5 records) goes to fixtures/samples/<name>.json for the repo.
"""
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "https://api.uexcorp.uk/2.0/"
ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "fixtures" / "raw"
SAMPLE = ROOT / "fixtures" / "samples"

DEFAULT = [
    "star_systems",
    "terminals",
    "commodities",
    "commodities_prices_all",
    "vehicles",
    "fuel_prices_all",
]


def token() -> str:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("UEX_TOKEN="):
                return line.split("=", 1)[1].strip()
    return ""


def fetch(path: str) -> tuple[int, dict | None, str]:
    req = urllib.request.Request(BASE + path, headers={"User-Agent": "stanton-trade-optimizer/explore"})
    tok = token()
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read().decode("utf-8")
            return r.status, json.loads(body), body
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body), body
        except ValueError:
            return e.code, None, body


def safe_name(path: str) -> str:
    return path.replace("?", "__").replace("&", "_").replace("=", "-")


def main() -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    paths = sys.argv[1:] or DEFAULT
    print(f"token present: {bool(token())}")
    for path in paths:
        code, data, body = fetch(path)
        name = safe_name(path)
        (RAW / f"{name}.json").write_text(body, encoding="utf-8")
        status = data.get("status") if isinstance(data, dict) else None
        rows = data.get("data") if isinstance(data, dict) else None
        n = len(rows) if isinstance(rows, list) else ("obj" if isinstance(rows, dict) else None)
        print(f"{path}: http={code} status={status} rows={n} bytes={len(body)}")
        if isinstance(data, dict):
            sample = dict(data)
            if isinstance(rows, list):
                sample["data"] = rows[:5]
            (SAMPLE / f"{name}.json").write_text(json.dumps(sample, indent=2), encoding="utf-8")
        time.sleep(1.0)


if __name__ == "__main__":
    main()

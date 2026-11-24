#!/usr/bin/env python3
"""Fetch the latest BoM Cunderdin Airport observation and write observations.json.

Uses a normal browser User-Agent and does not send an Origin header (BoM/Akamai
returns 403 when Origin is present). Stdlib only.

Default output: observations.json at the repository root (parent of scripts/).
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SOURCE_URL = "https://www.bom.gov.au/fwo/IDW60801/IDW60801.95625.json"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
# Official BoM station details (010286 / WMO 95625). Observation JSON lat/lon are 1-decimal.
STATION = "Cunderdin Airport"
STATION_ID = "95625"
STATION_BOM_ID = "010286"
STATION_LAT = -31.6219
STATION_LON = 117.2217
STATION_ELEV_M = 216.7
CARDINALS = [
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
]
TZ = ZoneInfo("Australia/Perth")
ROOT = Path(__file__).resolve().parent.parent


def compass_to_deg(compass: str | None) -> float | None:
    if not compass:
        return None
    token = str(compass).strip().upper()
    if token in ("", "CALM", "-", "NULL"):
        return None
    try:
        return CARDINALS.index(token) * 22.5
    except ValueError:
        return None


def parse_local_full(value: str) -> datetime:
    # local_date_time_full is AWST / WST, e.g. 20261125033000
    raw = str(value).strip()
    dt = datetime.strptime(raw, "%Y%m%d%H%M%S")
    return dt.replace(tzinfo=TZ)


def observation_payload(data: dict, fetched_at: datetime) -> dict:
    rows = (data.get("observations") or {}).get("data") or []
    if not rows:
        raise RuntimeError("BoM JSON contained no observation rows")
    row = rows[0]
    when = parse_local_full(row["local_date_time_full"])
    compass = row.get("wind_dir")
    compass = None if compass in (None, "", "-") else str(compass)
    return {
        "station": STATION,
        "station_id": STATION_ID,
        "bom_id": STATION_BOM_ID,
        "lat": STATION_LAT,
        "lon": STATION_LON,
        "elev_m": STATION_ELEV_M,
        "obs_time_awst": when.isoformat(timespec="seconds"),
        "wind_kmh": row.get("wind_spd_kmh"),
        "gust_kmh": row.get("gust_kmh"),
        "dir_compass": compass,
        "dir_deg": compass_to_deg(compass),
        "temp_c": row.get("air_temp"),
        "source_url": SOURCE_URL,
        "fetched_at": fetched_at.isoformat(timespec="seconds"),
    }


def same_reading(a: dict, b: dict) -> bool:
    keys = ("obs_time_awst", "wind_kmh", "gust_kmh", "dir_compass", "dir_deg", "temp_c")
    return all(a.get(k) == b.get(k) for k in keys)


def fetch_json() -> dict:
    req = urllib.request.Request(
        SOURCE_URL,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        method="GET",
    )
    # Do not set Origin — BoM/Akamai 403s requests that include it.
    with urllib.request.urlopen(req, timeout=30) as resp:
        if resp.status != 200:
            raise RuntimeError(f"BoM HTTP {resp.status}")
        return json.loads(resp.read().decode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    out = ROOT / "observations.json"
    if argv and len(argv) > 1:
        out = Path(argv[1])
    fetched_at = datetime.now(TZ)
    try:
        raw = fetch_json()
        payload = observation_payload(raw, fetched_at)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, RuntimeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"fetch_obs failed: {exc}", file=sys.stderr)
        return 1
    if out.exists():
        try:
            existing = json.loads(out.read_text(encoding="utf-8"))
            if same_reading(existing, payload):
                print(f"unchanged {payload['obs_time_awst']} {payload['dir_compass']} {payload['wind_kmh']} km/h")
                return 0
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(
        f"wrote {out}  {payload['obs_time_awst']}  "
        f"{payload['temp_c']} C  {payload['dir_compass']} {payload['dir_deg']}  "
        f"{payload['wind_kmh']} g={payload['gust_kmh']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

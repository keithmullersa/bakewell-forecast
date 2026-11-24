#!/usr/bin/env python3
"""Fetch the latest live observation and write observations.json.

Primary source: Skyshelter, a private Weather Underground PWS (IBURGE35)
about 117 m south of the Mount Bakewell top launch. The public website
apiKey is extracted at runtime from the station dashboard page and is
never hardcoded, committed, or printed.

Fallback / backup: BoM Cunderdin Airport AWS is fetched on every run.
It is used as the primary reading if Skyshelter fails, is missing values,
or is older than 30 minutes; otherwise it is stored in a 'backup' block.

Uses a normal browser User-Agent and does not send an Origin header
(BoM/Akamai returns 403 when Origin is present). Stdlib only.

Default output: observations.json at the repository root (parent of scripts/).

CLI: optional output path, and --fail-skyshelter to simulate a primary
failure (used to test the BoM fallback).
"""
from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
PAGE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-AU,en;q=0.9",
}
JSON_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json",
}

LAUNCH_LAT = -31.85229
LAUNCH_LON = 116.76261
LAUNCH_ELEV_M = 432

# Skyshelter (WU PWS IBURGE35). WU listed elevation (~256 m) is inconsistent
# with launch (432 m) and is not written as a number.
SKY_STATION = "Skyshelter"
SKY_STATION_ID = "IBURGE35"
SKY_LAT = -31.853339
SKY_LON = 116.762605
SKY_DISTANCE_M = 117
SKY_BEARING = "S"
SKY_ELEV_NOTE = (
    "on the hill, ~117 m S of launch, sensor height/mounting unverified"
)
SKY_PAGE_URL = "https://www.wunderground.com/dashboard/pws/IBURGE35"
SKY_API_BASE = "https://api.weather.com/v2/pws/observations/current"
SKY_STALE_MIN = 30

# Official BoM station details (010286 / WMO 95625). Observation JSON lat/lon are 1-decimal.
BOM_SOURCE_URL = "https://www.bom.gov.au/fwo/IDW60801/IDW60801.95625.json"
BOM_STATION = "Cunderdin Airport"
BOM_STATION_ID = "95625"
BOM_ID = "010286"
BOM_LAT = -31.6219
BOM_LON = 117.2217
BOM_ELEV_M = 216.7
BOM_DISTANCE_M = 50400
BOM_BEARING = "ENE"
BOM_ELEV_NOTE = "215 m below launch, 10 m mast"

CARDINALS = [
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
]
TZ = ZoneInfo("Australia/Perth")
ROOT = Path(__file__).resolve().parent.parent

PRIMARY_KEYS = (
    "source",
    "obs_time_awst",
    "wind_kmh",
    "gust_kmh",
    "dir_compass",
    "dir_deg",
    "temp_c",
    "fallback_reason",
)
BACKUP_KEYS = (
    "source",
    "obs_time_awst",
    "wind_kmh",
    "gust_kmh",
    "dir_compass",
    "dir_deg",
    "temp_c",
)


def redact(text: object) -> str:
    """Strip API keys and query strings that might contain one, for logs."""
    s = str(text)
    s = re.sub(r"([?&]apiKey=)[^&\s'\"\\]+", r"\1REDACTED", s, flags=re.I)
    s = re.sub(r'("API_KEY"\s*:\s*")[^"]+', r"\1REDACTED", s, flags=re.I)
    s = re.sub(r'(apiKey["\s:=]+["\'])[^"\']+', r"\1REDACTED", s, flags=re.I)
    return s


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


def deg_to_compass(deg: float | None) -> str | None:
    if deg is None:
        return None
    try:
        d = float(deg)
    except (TypeError, ValueError):
        return None
    if d != d:  # NaN
        return None
    return CARDINALS[int((d % 360) / 22.5 + 0.5) % 16]


def parse_local_full(value: str) -> datetime:
    # local_date_time_full is AWST / WST, e.g. 20261125033000
    raw = str(value).strip()
    dt = datetime.strptime(raw, "%Y%m%d%H%M%S")
    return dt.replace(tzinfo=TZ)


def as_number(value) -> float | int | None:
    if value is None or value == "" or value == "-":
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if n != n:  # NaN
        return None
    if n == int(n):
        return int(n)
    return n


def http_get(url: str, headers: dict, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise RuntimeError(redact(reason)) from None


def extract_api_key(html: str) -> str:
    """Pull the public website apiKey associated with api.weather.com."""
    patterns = (
        r'"API_KEY"\s*:\s*"([A-Za-z0-9]{16,64})"\s*,\s*"API_URL"\s*:\s*"https://api\.weather\.com"',
        r'"API_URL"\s*:\s*"https://api\.weather\.com"[\s\S]{0,400}?"API_KEY"\s*:\s*"([A-Za-z0-9]{16,64})"',
        r'api\.weather\.com[^"\s]{0,300}?[?&]apiKey=([A-Za-z0-9]{16,64})',
    )
    for pat in patterns:
        match = re.search(pat, html, flags=re.I)
        if match:
            return match.group(1)
    raise RuntimeError("could not extract WU website apiKey from public page")


def parse_wu_time(row: dict) -> datetime:
    utc = row.get("obsTimeUtc")
    if utc:
        raw = str(utc).strip().replace("Z", "+00:00")
        return datetime.fromisoformat(raw).astimezone(TZ)
    local = row.get("obsTimeLocal")
    if local:
        dt = datetime.fromisoformat(str(local).strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        return dt.astimezone(TZ)
    raise RuntimeError("skyshelter observation had no timestamp")


def values_ok(payload: dict) -> bool:
    return payload.get("obs_time_awst") and payload.get("wind_kmh") is not None


def age_minutes(payload: dict, now: datetime) -> float | None:
    raw = payload.get("obs_time_awst")
    if not raw:
        return None
    when = datetime.fromisoformat(str(raw))
    if when.tzinfo is None:
        when = when.replace(tzinfo=TZ)
    return (now - when).total_seconds() / 60.0


def bom_core(data: dict, fetched_at: datetime) -> dict:
    rows = (data.get("observations") or {}).get("data") or []
    if not rows:
        raise RuntimeError("BoM JSON contained no observation rows")
    row = rows[0]
    when = parse_local_full(row["local_date_time_full"])
    compass = row.get("wind_dir")
    compass = None if compass in (None, "", "-") else str(compass)
    return {
        "source": "bom_cunderdin",
        "station": BOM_STATION,
        "station_id": BOM_STATION_ID,
        "bom_id": BOM_ID,
        "lat": BOM_LAT,
        "lon": BOM_LON,
        "elev_m": BOM_ELEV_M,
        "distance_m": BOM_DISTANCE_M,
        "bearing": BOM_BEARING,
        "elev_note": BOM_ELEV_NOTE,
        "obs_time_awst": when.isoformat(timespec="seconds"),
        "wind_kmh": row.get("wind_spd_kmh"),
        "gust_kmh": row.get("gust_kmh"),
        "dir_compass": compass,
        "dir_deg": compass_to_deg(compass),
        "temp_c": row.get("air_temp"),
        "source_url": BOM_SOURCE_URL,
        "fetched_at": fetched_at.isoformat(timespec="seconds"),
    }


def sky_core(data: dict, fetched_at: datetime) -> dict:
    rows = data.get("observations") or []
    if not rows:
        raise RuntimeError("WU JSON contained no observation rows")
    row = rows[0]
    metric = row.get("metric") or {}
    when = parse_wu_time(row)
    dir_deg = as_number(row.get("winddir"))
    return {
        "source": "skyshelter",
        "station": SKY_STATION,
        "station_id": SKY_STATION_ID,
        "lat": SKY_LAT,
        "lon": SKY_LON,
        "distance_m": SKY_DISTANCE_M,
        "bearing": SKY_BEARING,
        "elev_note": SKY_ELEV_NOTE,
        "obs_time_awst": when.isoformat(timespec="seconds"),
        "wind_kmh": as_number(metric.get("windSpeed")),
        "gust_kmh": as_number(metric.get("windGust")),
        "dir_compass": deg_to_compass(dir_deg),
        "dir_deg": dir_deg,
        "temp_c": as_number(metric.get("temp")),
        "source_url": SKY_PAGE_URL,
        "fetched_at": fetched_at.isoformat(timespec="seconds"),
    }


def backup_block(payload: dict) -> dict:
    keep = (
        "source", "station", "station_id", "bom_id", "lat", "lon", "elev_m",
        "distance_m", "bearing", "elev_note", "obs_time_awst", "wind_kmh",
        "gust_kmh", "dir_compass", "dir_deg", "temp_c", "source_url",
        "fetched_at",
    )
    return {k: payload[k] for k in keep if k in payload}


def same_reading(a: dict, b: dict) -> bool:
    if not all(a.get(k) == b.get(k) for k in PRIMARY_KEYS):
        return False
    ba = a.get("backup") or {}
    bb = b.get("backup") or {}
    if not ba and not bb:
        return True
    return all(ba.get(k) == bb.get(k) for k in BACKUP_KEYS)


def fetch_bom(fetched_at: datetime) -> dict:
    raw = http_get(BOM_SOURCE_URL, JSON_HEADERS)
    return bom_core(json.loads(raw.decode("utf-8")), fetched_at)


def fetch_skyshelter(fetched_at: datetime) -> dict:
    html = http_get(SKY_PAGE_URL, PAGE_HEADERS).decode("utf-8", "replace")
    try:
        key = extract_api_key(html)
    finally:
        html = ""
    query = urllib.parse.urlencode(
        {
            "stationId": SKY_STATION_ID,
            "format": "json",
            "units": "m",
            "apiKey": key,
        }
    )
    key = ""
    try:
        raw = http_get(f"{SKY_API_BASE}?{query}", JSON_HEADERS)
    finally:
        query = ""
    return sky_core(json.loads(raw.decode("utf-8")), fetched_at)


def build_payload(fetched_at: datetime, fail_skyshelter: bool) -> dict:
    bom_payload = None
    bom_error = None
    try:
        bom_payload = fetch_bom(fetched_at)
        if not values_ok(bom_payload):
            bom_error = "BoM Cunderdin values missing"
            bom_payload = None
    except (RuntimeError, ValueError, KeyError, TypeError, json.JSONDecodeError, TimeoutError) as exc:
        bom_error = redact(exc)
        print(f"BoM Cunderdin fetch failed: {bom_error}", file=sys.stderr)

    sky_payload = None
    sky_error = None
    if fail_skyshelter:
        sky_error = "skyshelter unavailable (forced failure)"
        print("skyshelter skipped: --fail-skyshelter", file=sys.stderr)
    else:
        try:
            sky_payload = fetch_skyshelter(fetched_at)
            if not values_ok(sky_payload):
                sky_error = "skyshelter values missing"
                sky_payload = None
            else:
                age = age_minutes(sky_payload, fetched_at)
                if age is not None and age > SKY_STALE_MIN:
                    sky_error = f"skyshelter stale ({int(round(age))} min)"
                    sky_payload = None
        except (RuntimeError, ValueError, KeyError, TypeError, json.JSONDecodeError, TimeoutError) as exc:
            sky_error = redact(exc)
            print(f"skyshelter failed: {sky_error}", file=sys.stderr)

    if sky_payload is not None:
        payload = sky_payload
        if bom_payload is not None:
            payload["backup"] = backup_block(bom_payload)
        return payload

    if bom_payload is not None:
        payload = bom_payload
        payload["fallback_reason"] = sky_error or "skyshelter unavailable"
        print(f"fallback to BoM Cunderdin: {payload['fallback_reason']}", file=sys.stderr)
        return payload

    parts = [p for p in (f"skyshelter: {sky_error}" if sky_error else None,
                         f"BoM: {bom_error}" if bom_error else None) if p]
    raise RuntimeError("; ".join(parts) or "no observation available")


def parse_args(argv: list[str]) -> tuple[Path, bool]:
    fail_skyshelter = False
    out = ROOT / "observations.json"
    for arg in argv[1:]:
        if arg in ("--fail-skyshelter", "--force-bom"):
            fail_skyshelter = True
        elif arg.startswith("-"):
            raise SystemExit(f"unknown option: {arg}")
        else:
            out = Path(arg)
    return out, fail_skyshelter


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    try:
        out, fail_skyshelter = parse_args(argv)
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 2
    fetched_at = datetime.now(TZ)
    try:
        payload = build_payload(fetched_at, fail_skyshelter)
    except (RuntimeError, ValueError, KeyError, TypeError, json.JSONDecodeError, TimeoutError) as exc:
        print(f"fetch_obs failed: {redact(exc)}", file=sys.stderr)
        return 1
    if out.exists():
        try:
            existing = json.loads(out.read_text(encoding="utf-8"))
            if same_reading(existing, payload):
                print(
                    f"unchanged {payload['source']} {payload['obs_time_awst']} "
                    f"{payload['dir_compass']} {payload['wind_kmh']} km/h"
                )
                return 0
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(
        f"wrote {out}  source={payload['source']}  {payload['obs_time_awst']}  "
        f"{payload['temp_c']} C  {payload['dir_compass']} {payload['dir_deg']}  "
        f"{payload['wind_kmh']} g={payload['gust_kmh']}"
        + (f"  backup={payload['backup']['obs_time_awst']}" if payload.get("backup") else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

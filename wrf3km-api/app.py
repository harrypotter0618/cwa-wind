
import datetime as dt
import math
import os
import threading
import time
from pathlib import Path
from typing import Optional

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from eccodes import (
    codes_get,
    codes_get_api_version,
    codes_grib_find_nearest,
    codes_grib_new_from_file,
    codes_release,
)

VERSION = "0.1.0"
MODEL_NAME = "CWA WRF-3KM"
MAX_FH = 84
STEP_H = 6
AVAILABLE_HOURS = list(range(0, MAX_FH + STEP_H, STEP_H))

DIRECT_BASE = os.getenv(
    "WRF_DIRECT_BASE",
    "https://cwaopendata.s3.ap-northeast-1.amazonaws.com/Model"
).rstrip("/")
FILEAPI = "https://opendata.cwa.gov.tw/fileapi/v1/opendataapi/{dataid}"

CWA_API_KEY = os.getenv("CWA_API_KEY", "").strip()
CACHE_DIR = Path(os.getenv("CACHE_DIR", "/tmp/wrf3km-cache"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_TTL = int(os.getenv("CACHE_TTL_SECONDS", "1200"))
MAX_CACHE_MB = int(os.getenv("MAX_CACHE_MB", "450"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "180"))

origins = [x.strip() for x in os.getenv("CORS_ORIGINS", "*").split(",") if x.strip()]

app = FastAPI(
    title="CWA WRF3KM Wind API",
    version=VERSION,
    description="Render backend for CWA WRF-3KM 10 m wind."
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)

download_lock = threading.Lock()


def data_id(fh: int) -> str:
    return f"M-A0064-{fh:03d}"


def cache_path(fh: int) -> Path:
    return CACHE_DIR / f"{data_id(fh)}.grb2"


def validate_fh(fh: int):
    if fh not in AVAILABLE_HOURS:
        raise HTTPException(
            400,
            detail={
                "code": "INVALID_FORECAST_HOUR",
                "message": f"forecast_hour must be one of {AVAILABLE_HOURS}",
            },
        )


def _stream_download(url: str, dest: Path, params=None) -> tuple[bool, str]:
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        with requests.get(
            url,
            params=params,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
            stream=True,
            headers={"User-Agent": f"wrf3km-render/{VERSION}"},
        ) as r:
            if r.status_code != 200:
                return False, f"HTTP {r.status_code}"

            with open(tmp, "wb") as f:
                first = True
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    if first:
                        first = False
                        # A GRIB file should start with GRIB.
                        if not chunk.startswith(b"GRIB"):
                            sample = chunk[:160].decode("utf-8", "ignore")
                            tmp.unlink(missing_ok=True)
                            return False, f"unexpected payload: {sample[:100]}"
                    f.write(chunk)

        if not tmp.exists() or tmp.stat().st_size < 100_000:
            tmp.unlink(missing_ok=True)
            return False, "payload too small"

        tmp.replace(dest)
        return True, "ok"
    except Exception as e:
        tmp.unlink(missing_ok=True)
        return False, str(e)


def cleanup_cache(keep: Optional[Path] = None):
    files = [p for p in CACHE_DIR.glob("M-A0064-*.grb2") if p.is_file()]
    total = sum(p.stat().st_size for p in files)
    limit = MAX_CACHE_MB * 1024 * 1024
    if total <= limit:
        return

    files.sort(key=lambda p: p.stat().st_mtime)
    for p in files:
        if keep and p == keep:
            continue
        try:
            total -= p.stat().st_size
            p.unlink(missing_ok=True)
        except Exception:
            pass
        if total <= limit:
            break


def download_grib(fh: int) -> Path:
    validate_fh(fh)
    p = cache_path(fh)

    if p.exists() and time.time() - p.stat().st_mtime < CACHE_TTL:
        p.touch()
        return p

    with download_lock:
        if p.exists() and time.time() - p.stat().st_mtime < CACHE_TTL:
            p.touch()
            return p

        did = data_id(fh)

        # First try the direct official CWA S3 resource exposed by the dataset.
        direct_url = f"{DIRECT_BASE}/{did}.grb2"
        ok, why = _stream_download(direct_url, p)

        # Fallback to CWA file API when direct resource is unavailable.
        if not ok and CWA_API_KEY:
            fileapi_url = FILEAPI.format(dataid=did)
            ok, why2 = _stream_download(
                fileapi_url,
                p,
                params={"Authorization": CWA_API_KEY}
            )
            if not ok:
                raise HTTPException(
                    502,
                    detail={
                        "code": "CWA_DOWNLOAD_FAILED",
                        "dataid": did,
                        "direct_error": why,
                        "fileapi_error": why2,
                    },
                )

        elif not ok:
            raise HTTPException(
                502,
                detail={
                    "code": "CWA_DOWNLOAD_FAILED",
                    "dataid": did,
                    "direct_error": why,
                    "message": "Direct CWA file failed and CWA_API_KEY is not configured for fallback.",
                },
            )

        cleanup_cache(keep=p)
        return p


def safe_get(gid, key, default=None):
    try:
        return codes_get(gid, key)
    except Exception:
        return default


def point_attr(pt, name: str):
    if hasattr(pt, name):
        return getattr(pt, name)
    if isinstance(pt, dict):
        return pt[name]
    raise KeyError(name)


def idw4(gid, lat: float, lon: float):
    try:
        pts = codes_grib_find_nearest(gid, lat, lon, is_lsm=False, npoints=4)
    except Exception:
        pts = codes_grib_find_nearest(gid, lat, lon, is_lsm=False, npoints=1)

    if not pts:
        raise RuntimeError("ecCodes returned no nearest grid point")

    rows = []
    for p in pts:
        rows.append({
            "lat": float(point_attr(p, "lat")),
            "lon": float(point_attr(p, "lon")),
            "value": float(point_attr(p, "value")),
            "distance_km": float(point_attr(p, "distance")),
        })

    rows.sort(key=lambda x: x["distance_km"])

    if rows[0]["distance_km"] < 1e-6 or len(rows) == 1:
        value = rows[0]["value"]
    else:
        # Inverse-distance-squared interpolation over four nearest model points.
        weights = [1.0 / max(r["distance_km"], 1e-6) ** 2 for r in rows]
        value = sum(w * r["value"] for w, r in zip(weights, rows)) / sum(weights)

    return value, rows


def classify_uv_message(gid) -> Optional[str]:
    short = str(safe_get(gid, "shortName", "") or "").lower()
    name = str(safe_get(gid, "name", "") or "").lower()
    tol = str(safe_get(gid, "typeOfLevel", "") or "")
    level = safe_get(gid, "level", None)

    try:
        lev = float(level)
    except Exception:
        lev = None

    # Common ecCodes encoding.
    if short in {"10u", "u10"}:
        return "u"
    if short in {"10v", "v10"}:
        return "v"

    # Alternate encoding: u/v at 10 m AGL.
    is_10m = lev is not None and abs(lev - 10.0) < 0.1
    is_agl = "heightaboveground" in tol.lower()

    if is_10m and is_agl:
        if short == "u" or "u component of wind" in name:
            return "u"
        if short == "v" or "v component of wind" in name:
            return "v"

    return None


def read_uv(path: Path, lat: float, lon: float):
    meta = None
    found = {}

    with open(path, "rb") as f:
        while True:
            gid = codes_grib_new_from_file(f)
            if gid is None:
                break
            try:
                if meta is None:
                    meta = {
                        "dataDate": int(safe_get(gid, "dataDate", 0) or 0),
                        "dataTime": int(safe_get(gid, "dataTime", 0) or 0),
                        "forecastTime": int(safe_get(gid, "forecastTime", 0) or 0),
                    }

                comp = classify_uv_message(gid)
                if comp and comp not in found:
                    value, points = idw4(gid, lat, lon)
                    found[comp] = {
                        "value": value,
                        "points": points,
                    }

                if "u" in found and "v" in found:
                    break
            finally:
                codes_release(gid)

    if not meta:
        raise HTTPException(
            502,
            detail={"code": "EMPTY_GRIB", "message": f"No GRIB messages in {path.name}."},
        )

    if "u" not in found or "v" not in found:
        raise HTTPException(
            502,
            detail={
                "code": "UV10_NOT_FOUND",
                "message": f"10 m U/V wind fields were not found in {path.name}.",
            },
        )

    return meta, found["u"], found["v"]


def init_epoch(meta: dict) -> int:
    date = str(meta["dataDate"])
    tm = int(meta["dataTime"])
    if len(date) != 8:
        raise HTTPException(502, detail={"code": "BAD_GRIB_TIME", "meta": meta})

    hh = tm // 100
    mm = tm % 100
    d = dt.datetime(
        int(date[:4]), int(date[4:6]), int(date[6:8]),
        hh, mm, tzinfo=dt.timezone.utc
    )
    return int(d.timestamp())


def iso_utc(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc).isoformat(timespec="minutes")


def iso_taipei(epoch: float) -> str:
    tz = dt.timezone(dt.timedelta(hours=8))
    return dt.datetime.fromtimestamp(epoch, tz=tz).isoformat(timespec="minutes")


def parse_requested_time(time_epoch: Optional[int], time_iso: Optional[str]) -> int:
    if time_epoch is not None:
        return int(time_epoch)

    if time_iso:
        s = time_iso.strip().replace("Z", "+00:00")
        try:
            d = dt.datetime.fromisoformat(s)
        except ValueError:
            raise HTTPException(
                400,
                detail={"code": "BAD_TIME", "message": "time must be ISO-8601."},
            )

        # If no timezone is supplied, interpret it as Taiwan local time.
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone(dt.timedelta(hours=8)))
        return int(d.timestamp())

    return int(time.time())


def wind_from_uv(u: float, v: float):
    speed = math.hypot(u, v)
    # Meteorological direction: where the wind COMES FROM.
    direction = (math.degrees(math.atan2(-u, -v)) + 360.0) % 360.0
    return speed, direction


DIR16 = [
    "北","北北東","東北","東北東","東","東南東","東南","南南東",
    "南","南南西","西南","西南西","西","西北西","西北","北北西"
]


def dir_text(deg: float) -> str:
    return DIR16[round(deg / 22.5) % 16]


def read_model_init(lat: float = 23.5, lon: float = 121.0):
    p0 = download_grib(0)
    meta0, _, _ = read_uv(p0, lat, lon)
    return init_epoch(meta0)


@app.get("/")
def root():
    return {
        "ok": True,
        "service": "wrf3km-api",
        "version": VERSION,
        "docs": "/docs",
        "health": "/health",
        "range": "/range",
        "wind_example": "/wind?lat=24.15&lon=120.68",
    }


@app.get("/health")
def health():
    try:
        ecc_ver = codes_get_api_version()
    except Exception as e:
        ecc_ver = f"error: {e}"

    return {
        "ok": True,
        "service": "wrf3km-api",
        "version": VERSION,
        "model": MODEL_NAME,
        "eccodes_api_version": ecc_ver,
        "forecast_hours": {
            "min": 0,
            "max": MAX_FH,
            "step": STEP_H,
        },
        "cache_dir": str(CACHE_DIR),
        "cwa_fileapi_fallback_configured": bool(CWA_API_KEY),
        "note": "Health check does not download a GRIB2 file.",
    }


@app.get("/range")
def forecast_range():
    init = read_model_init()
    return {
        "ok": True,
        "model": MODEL_NAME,
        "initial_time_utc": iso_utc(init),
        "initial_time_taipei": iso_taipei(init),
        "first_valid_time_taipei": iso_taipei(init),
        "last_valid_time_taipei": iso_taipei(init + MAX_FH * 3600),
        "forecast_hours": AVAILABLE_HOURS,
        "output_interval_hours": STEP_H,
    }


@app.get("/wind")
def wind(
    lat: float = Query(..., ge=14.0, le=32.2),
    lon: float = Query(..., ge=105.0, le=141.0),
    time_epoch: Optional[int] = Query(None, description="Unix seconds UTC"),
    time: Optional[str] = Query(None, description="ISO-8601. Without timezone = Taiwan local time."),
):
    requested = parse_requested_time(time_epoch, time)

    # fh=0 establishes the latest model cycle.
    init = read_model_init(lat, lon)
    requested_fh = (requested - init) / 3600.0

    if requested_fh < 0:
        raise HTTPException(
            422,
            detail={
                "code": "BEFORE_FORECAST_RANGE",
                "initial_time_taipei": iso_taipei(init),
                "requested_time_taipei": iso_taipei(requested),
            },
        )
    if requested_fh > MAX_FH:
        raise HTTPException(
            422,
            detail={
                "code": "AFTER_FORECAST_RANGE",
                "last_valid_time_taipei": iso_taipei(init + MAX_FH * 3600),
                "requested_time_taipei": iso_taipei(requested),
            },
        )

    lo = int(math.floor(requested_fh / STEP_H) * STEP_H)
    hi = int(math.ceil(requested_fh / STEP_H) * STEP_H)
    lo = max(0, min(MAX_FH, lo))
    hi = max(0, min(MAX_FH, hi))

    meta_lo, u_lo, v_lo = read_uv(download_grib(lo), lat, lon)
    init_lo = init_epoch(meta_lo)

    if init_lo != init:
        raise HTTPException(
            503,
            detail={
                "code": "MODEL_FILES_NOT_SYNCHRONIZED",
                "message": f"{data_id(lo)} belongs to a different model cycle. Retry later.",
            },
        )

    if hi == lo:
        u = u_lo["value"]
        v = v_lo["value"]
        alpha = 0.0
    else:
        meta_hi, u_hi, v_hi = read_uv(download_grib(hi), lat, lon)
        init_hi = init_epoch(meta_hi)
        if init_hi != init:
            raise HTTPException(
                503,
                detail={
                    "code": "MODEL_FILES_NOT_SYNCHRONIZED",
                    "message": f"{data_id(hi)} belongs to a different model cycle. Retry later.",
                },
            )

        alpha = (requested_fh - lo) / (hi - lo)
        u = u_lo["value"] + alpha * (u_hi["value"] - u_lo["value"])
        v = v_lo["value"] + alpha * (v_hi["value"] - v_lo["value"])

    speed, direction = wind_from_uv(u, v)

    representative_points = u_lo["points"]
    nearest = min(representative_points, key=lambda x: x["distance_km"])

    return {
        "ok": True,
        "model": MODEL_NAME,
        "requested_time_utc": iso_utc(requested),
        "requested_time_taipei": iso_taipei(requested),
        "initial_time_utc": iso_utc(init),
        "initial_time_taipei": iso_taipei(init),
        "last_valid_time_taipei": iso_taipei(init + MAX_FH * 3600),
        "requested_forecast_hour": round(requested_fh, 3),
        "source_hours": [lo, hi],
        "time_interpolation_alpha": round(alpha, 4),
        "spatial_interpolation": "IDW over 4 nearest model grid points",
        "nearest_model_point": {
            "lat": round(nearest["lat"], 5),
            "lon": round(nearest["lon"], 5),
            "distance_km": round(nearest["distance_km"], 3),
        },
        "u10_mps": round(u, 3),
        "v10_mps": round(v, 3),
        "wind_speed_mps": round(speed, 3),
        "wind_direction_deg": round(direction, 1),
        "wind_direction_text": dir_text(direction),
    }

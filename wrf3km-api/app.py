
import datetime as dt
import math
import logging
import json
import hashlib
import os
import threading
import tempfile
import time
from pathlib import Path
from typing import Optional

import requests
from fastapi import FastAPI, HTTPException, Query, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from eccodes import (
    codes_get,
    codes_get_array,
    codes_get_api_version,
    codes_grib_find_nearest,
    codes_grib_new_from_file,
    codes_release,
    codes_write,
)

VERSION = "0.2.9"
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
UV_CACHE_TTL = int(os.getenv("UV_CACHE_TTL_SECONDS", "1200"))
KEEP_FULL_GRIB = os.getenv("KEEP_FULL_GRIB", "false").strip().lower() in {"1","true","yes","on"}
UV_CACHE_DIR = CACHE_DIR / "uv10"
UV_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# V0.2.3: cache route-coordinate -> WRF grid indices + IDW weights.
# This is independent of forecast hour/model cycle as long as grid geometry is unchanged.
GRIDMAP_CACHE_DIR = CACHE_DIR / "gridmap"
GRIDMAP_CACHE_DIR.mkdir(parents=True, exist_ok=True)
GRIDMAP_CACHE_FILE = GRIDMAP_CACHE_DIR / "grid_index_v1.json"
GRIDMAP_VERSION = 1
_gridmap_lock = threading.Lock()

CURRENT_ROUTE_URL = os.getenv(
    "CURRENT_ROUTE_URL",
    "https://raw.githubusercontent.com/harrypotter0618/cwa-wind/main/ride-api/routes/current.gpx"
).strip()
ROUTE_CACHE_SECONDS = int(os.getenv("ROUTE_CACHE_SECONDS", "300"))
MAX_GPX_UPLOAD_MB = int(os.getenv("MAX_GPX_UPLOAD_MB", "12"))
MAX_REST_POINTS = int(os.getenv("MAX_REST_POINTS", "30"))
RANGE_PROBE_CACHE_SECONDS = int(os.getenv("RANGE_PROBE_CACHE_SECONDS", "300"))
RANGE_PROBE_MAX_FIRST_MESSAGE_MB = int(os.getenv("RANGE_PROBE_MAX_FIRST_MESSAGE_MB", "64"))
_range_probe_cache = {"ts": 0.0, "result": None}
_route_cache = {"ts": 0.0, "route": None, "source": None}

origins = [x.strip() for x in os.getenv("CORS_ORIGINS", "*").split(",") if x.strip()]

app = FastAPI(
    title="CWA WRF3KM Wind API",
    version=VERSION,
    description="Render backend for CWA WRF-3KM 10 m wind."
)

logger = logging.getLogger("wrf3km")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    logger.addHandler(handler)
logger.setLevel(logging.INFO)

def diag(msg: str):
    logger.info(msg)

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

download_lock = threading.Lock()

# V0.2.4: prevent two simultaneous requests from building the same
# compact U/V cache file at the same time.
_uv_build_locks_guard = threading.Lock()
_uv_build_locks = {}


def get_uv_build_lock(fh: int):
    with _uv_build_locks_guard:
        lock = _uv_build_locks.get(fh)
        if lock is None:
            lock = threading.Lock()
            _uv_build_locks[fh] = lock
        return lock



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
            headers={
                "User-Agent": f"wrf3km-render/{VERSION}",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            },
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


def download_grib(fh: int, force: bool = False) -> Path:
    validate_fh(fh)
    p = cache_path(fh)

    if (not force) and p.exists() and time.time() - p.stat().st_mtime < CACHE_TTL:
        p.touch()
        return p

    with download_lock:
        if (not force) and p.exists() and time.time() - p.stat().st_mtime < CACHE_TTL:
            p.touch()
            return p

        did = data_id(fh)
        t0 = time.time()
        diag(f"[GRIB] start fh={fh:03d} force={force} file={did}")

        if force:
            p.unlink(missing_ok=True)

        # First try the direct official CWA S3 resource.
        direct_url = f"{DIRECT_BASE}/{did}.grb2"
        # A cache-busting query is used only for forced refreshes.
        direct_params = {"_": int(time.time())} if force else None
        ok, why = _stream_download(direct_url, p, params=direct_params)

        # Fallback to CWA file API when direct resource is unavailable.
        if not ok and CWA_API_KEY:
            fileapi_url = FILEAPI.format(dataid=did)
            params = {"Authorization": CWA_API_KEY}
            if force:
                params["_"] = int(time.time())
            ok, why2 = _stream_download(fileapi_url, p, params=params)
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
        diag(f"[GRIB] ready fh={fh:03d} size_mb={p.stat().st_size/1024/1024:.1f} elapsed={time.time()-t0:.1f}s")
        return p


def uv_cache_paths(fh: int):
    stem = f"{data_id(fh)}_uv10"
    return (
        UV_CACHE_DIR / f"{stem}.grb2",
        UV_CACHE_DIR / f"{stem}.json",
    )


def _read_uv_meta(meta_path: Path):
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _uv_cache_is_fresh(uv_path: Path, meta_path: Path, fh: int):
    # Reject partial/corrupt cache files before they can be reused.
    if not uv_path.exists() or not meta_path.exists():
        return False
    try:
        if uv_path.stat().st_size < 100_000:
            return False
    except Exception:
        return False

    meta = _read_uv_meta(meta_path)
    if not meta or "init_epoch" not in meta:
        return False

    # FH000 is our "is there a new model cycle?" probe and keeps the TTL.
    # Other hours stay cached until FH000 reveals a newer cycle.
    if fh != 0:
        return True

    age = time.time() - min(uv_path.stat().st_mtime, meta_path.stat().st_mtime)
    return age < UV_CACHE_TTL


def build_uv10_cache(fh: int, force: bool = False):
    """
    Extract only 10 m U/V from the large CWA GRIB2 file.

    V0.2.4 concurrency safety:
    - one in-process builder per forecast hour
    - cache is re-checked after acquiring the lock
    - unique temporary files avoid tmp-path collisions
    - compact GRIB and metadata are atomically replaced
    - undersized/corrupt cache files are never accepted
    """
    validate_fh(fh)
    uv_path, meta_path = uv_cache_paths(fh)

    # Fast path before locking.
    if (not force) and _uv_cache_is_fresh(uv_path, meta_path, fh):
        meta = _read_uv_meta(meta_path)
        diag(
            f"[UV_CACHE] hit fh={fh:03d} "
            f"size_mb={uv_path.stat().st_size/1024/1024:.2f}"
        )
        return uv_path, meta

    build_lock = get_uv_build_lock(fh)

    diag(f"[UV_CACHE] waiting build lock fh={fh:03d}")
    with build_lock:
        # A concurrent request may have completed while we waited.
        if (not force) and _uv_cache_is_fresh(uv_path, meta_path, fh):
            meta = _read_uv_meta(meta_path)
            diag(
                f"[UV_CACHE] hit-after-wait fh={fh:03d} "
                f"size_mb={uv_path.stat().st_size/1024/1024:.2f}"
            )
            return uv_path, meta

        # Remove stale/partial leftovers before a rebuild.
        try:
            if uv_path.exists() and uv_path.stat().st_size < 100_000:
                diag(
                    f"[UV_CACHE] removing corrupt cache fh={fh:03d} "
                    f"size_bytes={uv_path.stat().st_size}"
                )
                uv_path.unlink(missing_ok=True)
                meta_path.unlink(missing_ok=True)
        except Exception:
            uv_path.unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)

        diag(f"[UV_CACHE] miss fh={fh:03d} force={force}; extracting 10m U/V")
        source = download_grib(fh, force=force)

        unique = f"{os.getpid()}.{threading.get_ident()}.{int(time.time()*1000)}"
        tmp = uv_path.with_name(f"{uv_path.name}.{unique}.tmp")
        meta_tmp = meta_path.with_name(f"{meta_path.name}.{unique}.tmp")
        tmp.unlink(missing_ok=True)
        meta_tmp.unlink(missing_ok=True)

        found = set()
        meta = None
        t0 = time.time()

        try:
            with open(source, "rb") as src, open(tmp, "wb") as out:
                while True:
                    gid = codes_grib_new_from_file(src)
                    if gid is None:
                        break
                    try:
                        if meta is None:
                            meta = {
                                "dataDate": int(safe_get(gid, "dataDate", 0) or 0),
                                "dataTime": int(safe_get(gid, "dataTime", 0) or 0),
                                "forecastTime": int(safe_get(gid, "forecastTime", fh) or fh),
                            }

                        comp = classify_uv_message(gid)
                        if comp and comp not in found:
                            codes_write(gid, out)
                            found.add(comp)

                        if "u" in found and "v" in found:
                            break
                    finally:
                        codes_release(gid)

            if "u" not in found or "v" not in found:
                raise HTTPException(
                    502,
                    detail={
                        "code": "UV10_NOT_FOUND",
                        "message": f"10 m U/V wind fields were not found in {source.name}.",
                    },
                )

            if not tmp.exists() or tmp.stat().st_size < 100_000:
                raise HTTPException(
                    502,
                    detail={
                        "code": "UV10_CACHE_INVALID",
                        "message": "Extracted U/V cache file is missing or unexpectedly small.",
                        "size_bytes": tmp.stat().st_size if tmp.exists() else 0,
                    },
                )

            if not meta:
                raise RuntimeError("No GRIB metadata found while extracting U/V")

            meta["init_epoch"] = init_epoch(meta)
            meta["forecast_hour"] = fh
            meta["created_at_epoch"] = int(time.time())

            meta_tmp.write_text(
                json.dumps(meta, ensure_ascii=False),
                encoding="utf-8",
            )

            # Publish data first, metadata second. A cache is only considered
            # valid when both exist and the data file passes minimum-size checks.
            tmp.replace(uv_path)
            meta_tmp.replace(meta_path)

            diag(
                f"[UV_CACHE] built fh={fh:03d} "
                f"uv_size_mb={uv_path.stat().st_size/1024/1024:.2f} "
                f"elapsed={time.time()-t0:.1f}s"
            )

            if not KEEP_FULL_GRIB:
                try:
                    source.unlink(missing_ok=True)
                    diag(f"[GRIB] released full source fh={fh:03d} after U/V extraction")
                except Exception:
                    pass

            return uv_path, meta

        except Exception:
            tmp.unlink(missing_ok=True)
            meta_tmp.unlink(missing_ok=True)
            raise


def invalidate_uv_cache(fh: int):
    uv_path, meta_path = uv_cache_paths(fh)
    uv_path.unlink(missing_ok=True)
    meta_path.unlink(missing_ok=True)



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


def grid_fingerprint(gid) -> str:
    """Stable identifier for the WRF grid geometry."""
    keys = [
        "gridType", "numberOfPoints", "Ni", "Nj", "Nx", "Ny",
        "latitudeOfFirstGridPointInDegrees",
        "longitudeOfFirstGridPointInDegrees",
        "latitudeOfLastGridPointInDegrees",
        "longitudeOfLastGridPointInDegrees",
        "DxInMetres", "DyInMetres",
        "LaDInDegrees", "LoVInDegrees",
    ]
    payload = {k: safe_get(gid, k, None) for k in keys}
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def coord_cache_key(lat: float, lon: float) -> str:
    return f"{lat:.6f},{lon:.6f}"


def _load_gridmap_store():
    try:
        obj = json.loads(GRIDMAP_CACHE_FILE.read_text(encoding="utf-8"))
        if obj.get("version") == GRIDMAP_VERSION:
            return obj
    except Exception:
        pass
    return {"version": GRIDMAP_VERSION, "grids": {}}


def _save_gridmap_store(store):
    tmp = GRIDMAP_CACHE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(store, ensure_ascii=False), encoding="utf-8")
    tmp.replace(GRIDMAP_CACHE_FILE)


def _mapping_from_nearest_points(pts):
    rows = []
    for p in pts:
        rows.append({
            "index": int(point_attr(p, "index")),
            "lat": float(point_attr(p, "lat")),
            "lon": float(point_attr(p, "lon")),
            "distance_km": float(point_attr(p, "distance")),
        })

    rows.sort(key=lambda x: x["distance_km"])
    if not rows:
        raise RuntimeError("ecCodes returned no nearest grid point")

    if rows[0]["distance_km"] < 1e-6 or len(rows) == 1:
        weights = [1.0] + [0.0] * (len(rows) - 1)
    else:
        raw = [1.0 / max(r["distance_km"], 1e-6) ** 2 for r in rows]
        total = sum(raw)
        weights = [w / total for w in raw]

    return {
        "indices": [r["index"] for r in rows],
        "weights": weights,
        "points": rows,
    }


def get_grid_mappings(coords, reference_fh: int = 0):
    """
    Find 4 nearest WRF grid cells ONCE per coordinate and cache the
    cell indices + IDW weights. The mapping is then reused by U/V and
    every forecast hour.
    """
    uv_path, _ = build_uv10_cache(reference_fh)
    t0 = time.time()

    with open(uv_path, "rb") as f:
        ref_gid = None
        while True:
            gid = codes_grib_new_from_file(f)
            if gid is None:
                break
            comp = classify_uv_message(gid)
            if comp == "u":
                ref_gid = gid
                break
            codes_release(gid)

        if ref_gid is None:
            raise RuntimeError(f"No U10 field found in {uv_path.name}")

        try:
            fp = grid_fingerprint(ref_gid)

            with _gridmap_lock:
                store = _load_gridmap_store()
                grid_store = store["grids"].setdefault(fp, {})

                mappings = [None] * len(coords)
                misses = []

                for i, (lat, lon) in enumerate(coords):
                    key = coord_cache_key(lat, lon)
                    cached = grid_store.get(key)
                    if cached:
                        mappings[i] = cached
                    else:
                        misses.append((i, lat, lon, key))

                diag(
                    f"[GRIDMAP] grid={fp} coords={len(coords)} "
                    f"hit={len(coords)-len(misses)} miss={len(misses)}"
                )

                if misses:
                    build_t0 = time.time()
                    for n, (i, lat, lon, key) in enumerate(misses, start=1):
                        try:
                            pts = codes_grib_find_nearest(
                                ref_gid, lat, lon, is_lsm=False, npoints=4
                            )
                        except Exception:
                            pts = codes_grib_find_nearest(
                                ref_gid, lat, lon, is_lsm=False, npoints=1
                            )

                        mapping = _mapping_from_nearest_points(pts)
                        mappings[i] = mapping
                        grid_store[key] = mapping
                        diag(
                            f"[GRIDMAP] built {n}/{len(misses)} "
                            f"lat={lat:.5f} lon={lon:.5f}"
                        )

                    _save_gridmap_store(store)
                    diag(
                        f"[GRIDMAP] build complete new={len(misses)} "
                        f"elapsed={time.time()-build_t0:.1f}s"
                    )

            diag(f"[GRIDMAP] ready elapsed={time.time()-t0:.1f}s")
            return {"fingerprint": fp, "mappings": mappings}
        finally:
            codes_release(ref_gid)


def apply_grid_mappings(values, gridmap):
    out = []
    point_sets = []
    n = len(values)

    for mapping in gridmap["mappings"]:
        indices = mapping["indices"]
        weights = mapping["weights"]

        if any(idx < 0 or idx >= n for idx in indices):
            raise RuntimeError("cached WRF grid index out of range")

        value = sum(
            float(values[idx]) * float(weight)
            for idx, weight in zip(indices, weights)
        )
        out.append(value)
        point_sets.append(mapping["points"])

    return out, point_sets



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



def _read_limited_response_bytes(url: str, params, byte_count: int):
    """
    Read only the first byte_count bytes and close the response.
    If the origin ignores HTTP Range and returns the full ~170 MB object,
    we still stop reading after byte_count bytes.
    """
    headers = {
        "User-Agent": f"wrf3km-render/{VERSION}",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Accept-Encoding": "identity",
        "Range": f"bytes=0-{max(0, byte_count-1)}",
    }
    with requests.get(
        url,
        params=params,
        timeout=min(REQUEST_TIMEOUT, 45),
        allow_redirects=True,
        stream=True,
        headers=headers,
    ) as r:
        if r.status_code not in (200, 206):
            raise RuntimeError(f"HTTP {r.status_code}")
        data = r.raw.read(byte_count)
        if len(data) < byte_count:
            # A complete first GRIB message can legitimately be smaller only
            # when byte_count is the requested total length. The caller validates.
            return data
        return data


def _fetch_first_grib_message(fh: int):
    """
    Fetch only the first GRIB2 message from an official CWA file.
    This is used to read model-cycle metadata without downloading the
    whole 170+ MB forecast file.
    """
    validate_fh(fh)
    did = data_id(fh)
    cache_buster = int(time.time() // 60)

    sources = [
        (
            f"{DIRECT_BASE}/{did}.grb2",
            {"_rangeprobe": cache_buster},
            "direct",
        )
    ]
    if CWA_API_KEY:
        sources.append(
            (
                FILEAPI.format(dataid=did),
                {"Authorization": CWA_API_KEY, "_rangeprobe": cache_buster},
                "fileapi",
            )
        )

    errors = []
    for url, params, source_name in sources:
        try:
            head = _read_limited_response_bytes(url, params, 32)
            if len(head) < 16 or not head.startswith(b"GRIB"):
                raise RuntimeError("response does not begin with a GRIB message")
            edition = head[7]
            if edition != 2:
                raise RuntimeError(f"unsupported GRIB edition {edition}")

            total_len = int.from_bytes(head[8:16], "big")
            max_len = RANGE_PROBE_MAX_FIRST_MESSAGE_MB * 1024 * 1024
            if total_len < 16 or total_len > max_len:
                raise RuntimeError(f"unexpected first GRIB message length={total_len}")

            message = _read_limited_response_bytes(url, params, total_len)
            if len(message) < total_len:
                raise RuntimeError(
                    f"incomplete first GRIB message {len(message)}/{total_len} bytes"
                )
            if not message.startswith(b"GRIB") or message[-4:] != b"7777":
                raise RuntimeError("first GRIB message failed integrity markers")

            return message, source_name
        except Exception as e:
            errors.append(f"{source_name}: {e}")

    raise RuntimeError("; ".join(errors))


def _probe_remote_grib_meta(fh: int):
    """
    Return metadata from the first GRIB message of a forecast-hour file.
    No full-file download and no U/V extraction are performed.
    """
    message, source_name = _fetch_first_grib_message(fh)

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            suffix=".grb2",
            prefix=f"rangeprobe_{fh:03d}_",
            dir=str(CACHE_DIR),
            delete=False,
        ) as tmp:
            tmp.write(message)
            tmp_path = Path(tmp.name)

        with open(tmp_path, "rb") as f:
            gid = codes_grib_new_from_file(f)
            if gid is None:
                raise RuntimeError("ecCodes could not decode first GRIB message")
            try:
                meta = {
                    "dataDate": int(safe_get(gid, "dataDate", 0) or 0),
                    "dataTime": int(safe_get(gid, "dataTime", 0) or 0),
                    "forecastTime": int(safe_get(gid, "forecastTime", fh) or fh),
                }
            finally:
                codes_release(gid)

        meta["init_epoch"] = init_epoch(meta)
        meta["source"] = source_name
        return meta
    finally:
        if tmp_path:
            tmp_path.unlink(missing_ok=True)


def _cached_uv_cycle(fh: int):
    _, meta_path = uv_cache_paths(fh)
    meta = _read_uv_meta(meta_path)
    if not meta:
        return None
    try:
        return int(meta.get("init_epoch"))
    except Exception:
        return None


def _forecast_hour_matches_cycle(fh: int, expected_init: int, checked: dict):
    if fh == 0:
        checked[fh] = {
            "status": "match",
            "init_epoch": expected_init,
            "source": "fh000_probe",
        }
        return True

    # Reuse a compact U/V cache if it already proves this file belongs to
    # the current model cycle.
    cached_init = _cached_uv_cycle(fh)
    if cached_init == expected_init:
        checked[fh] = {
            "status": "match",
            "init_epoch": cached_init,
            "source": "uv_cache",
        }
        return True

    try:
        meta = _probe_remote_grib_meta(fh)
        got_init = int(meta["init_epoch"])
        ok = got_init == expected_init
        checked[fh] = {
            "status": "match" if ok else "older_cycle",
            "init_epoch": got_init,
            "source": meta.get("source", "remote"),
        }
        return ok
    except Exception as e:
        checked[fh] = {
            "status": "probe_error",
            "error": str(e),
        }
        return None


def get_confirmed_forecast_range(force: bool = False):
    """
    Discover how far the newest CWA WRF-3KM cycle is ACTUALLY published.

    We read only the first GRIB message of selected forecast-hour files.
    Publication is expected to be contiguous in 6-hour steps, so a binary
    search finds the current boundary with only a few lightweight probes.
    """
    now = time.time()
    if (
        not force
        and _range_probe_cache["result"] is not None
        and now - _range_probe_cache["ts"] < RANGE_PROBE_CACHE_SECONDS
    ):
        return _range_probe_cache["result"]

    t0 = time.time()
    checked = {}

    # FH000 itself is probed remotely so /range does not need to download
    # the full ~170 MB file merely to identify the newest model cycle.
    meta0 = _probe_remote_grib_meta(0)
    latest_init = int(meta0["init_epoch"])
    checked[0] = {
        "status": "match",
        "init_epoch": latest_init,
        "source": meta0.get("source", "remote"),
    }

    # If FH000 compact cache is from an older cycle, invalidate it now so
    # the next actual route calculation refreshes safely.
    cached0 = _cached_uv_cycle(0)
    if cached0 is not None and cached0 != latest_init:
        invalidate_uv_cache(0)
        diag(
            f"[RANGE] newer FH000 detected; invalidated stale FH000 U/V cache "
            f"old={iso_taipei(cached0)} new={iso_taipei(latest_init)}"
        )

    hours = AVAILABLE_HOURS
    highest_idx = len(hours) - 1
    top_status = _forecast_hour_matches_cycle(hours[highest_idx], latest_init, checked)

    if top_status is True:
        latest_confirmed = hours[highest_idx]
        complete = True
    else:
        # Binary-search publication boundary. Index 0 is known-good.
        lo = 0
        hi = highest_idx
        while hi - lo > 1:
            mid = (lo + hi) // 2
            status = _forecast_hour_matches_cycle(hours[mid], latest_init, checked)
            if status is True:
                lo = mid
            else:
                # An older cycle OR an unverified probe is treated conservatively
                # as "not confirmed" for front-end time limits.
                hi = mid

        latest_confirmed = hours[lo]
        complete = latest_confirmed >= MAX_FH

    result = {
        "ok": True,
        "model": MODEL_NAME,
        "version": VERSION,
        "initial_time_utc": iso_utc(latest_init),
        "initial_time_taipei": iso_taipei(latest_init),
        "first_valid_time_taipei": iso_taipei(latest_init),

        # Backward-compatible field now means actually confirmed range.
        "last_valid_time_taipei": iso_taipei(
            latest_init + latest_confirmed * 3600
        ),

        "confirmed_last_valid_time_taipei": iso_taipei(
            latest_init + latest_confirmed * 3600
        ),
        "theoretical_last_valid_time_taipei": iso_taipei(
            latest_init + MAX_FH * 3600
        ),
        "latest_confirmed_hour": latest_confirmed,
        "theoretical_max_forecast_hour": MAX_FH,
        "publishing_complete": complete,
        "forecast_hours": AVAILABLE_HOURS,
        "output_interval_hours": STEP_H,
        "verification_method": "remote_first_grib_cycle_probe_binary_search",
        "checked_hours": {
            str(k): v for k, v in sorted(checked.items())
        },
        "probe_elapsed_seconds": round(time.time() - t0, 2),
        "range_cache_seconds": RANGE_PROBE_CACHE_SECONDS,
    }

    _range_probe_cache["ts"] = time.time()
    _range_probe_cache["result"] = result

    diag(
        f"[RANGE] cycle={iso_taipei(latest_init)} "
        f"confirmed_fh={latest_confirmed:03d}/{MAX_FH:03d} "
        f"complete={complete} checked={sorted(checked)} "
        f"elapsed={time.time()-t0:.1f}s"
    )
    return result


def read_model_init(lat: float = 23.5, lon: float = 121.0):
    _, meta0 = build_uv10_cache(0)
    return int(meta0["init_epoch"])



# ---------- Route forecast helpers ----------

EARTH_KM = 6371.0


def haversine_km(lat1, lon1, lat2, lon2):
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_KM * math.asin(math.sqrt(a))


def bearing_deg(lat1, lon1, lat2, lon2):
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def parse_gpx(data: bytes):
    import xml.etree.ElementTree as ET

    root = ET.fromstring(data)
    pts = []
    for el in root.iter():
        if el.tag.endswith("trkpt") or el.tag.endswith("rtept"):
            try:
                pts.append({
                    "lat": float(el.attrib["lat"]),
                    "lon": float(el.attrib["lon"]),
                })
            except Exception:
                pass

    if len(pts) < 2:
        raise ValueError("GPX contains fewer than 2 route points")

    cum = 0.0
    for i, pt in enumerate(pts):
        if i > 0:
            prev = pts[i - 1]
            cum += haversine_km(prev["lat"], prev["lon"], pt["lat"], pt["lon"])
        pt["cum"] = cum

    for i, pt in enumerate(pts):
        a = pts[max(0, i - 1)]
        b = pts[min(len(pts) - 1, i + 1)]
        pt["heading"] = bearing_deg(a["lat"], a["lon"], b["lat"], b["lon"])

    return pts


def load_current_route(force=False):
    global _route_cache

    if (
        not force
        and _route_cache["route"] is not None
        and time.time() - _route_cache["ts"] < ROUTE_CACHE_SECONDS
    ):
        return _route_cache

    if not CURRENT_ROUTE_URL:
        raise HTTPException(
            500,
            detail={"code": "CURRENT_ROUTE_URL_MISSING"}
        )

    try:
        t0 = time.time()
        diag(f"[ROUTE] fetch start url={CURRENT_ROUTE_URL}")
        r = requests.get(
            CURRENT_ROUTE_URL,
            timeout=30,
            headers={
                "User-Agent": f"wrf3km-render/{VERSION}",
                "Cache-Control": "no-cache",
            },
        )
        r.raise_for_status()
        route = parse_gpx(r.content)
        diag(f"[ROUTE] fetch done points={len(route)} distance_km={route[-1]['cum']:.2f} elapsed={time.time()-t0:.1f}s")
    except Exception as e:
        raise HTTPException(
            502,
            detail={
                "code": "ROUTE_DOWNLOAD_FAILED",
                "message": str(e),
                "route_url": CURRENT_ROUTE_URL,
            },
        )

    _route_cache = {
        "ts": time.time(),
        "route": route,
        "source": CURRENT_ROUTE_URL,
    }
    return _route_cache


def route_point_at_km(route, target_km: float):
    target_km = max(0.0, min(float(target_km), route[-1]["cum"]))

    lo = 0
    hi = len(route) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if route[mid]["cum"] < target_km:
            lo = mid + 1
        else:
            hi = mid

    i2 = lo
    if i2 == 0:
        return {
            "km": 0.0,
            "lat": route[0]["lat"],
            "lon": route[0]["lon"],
            "heading": route[0]["heading"],
        }

    i1 = i2 - 1
    a = route[i1]
    b = route[i2]
    span = b["cum"] - a["cum"]
    t = 0.0 if span <= 1e-9 else (target_km - a["cum"]) / span

    lat = a["lat"] + t * (b["lat"] - a["lat"])
    lon = a["lon"] + t * (b["lon"] - a["lon"])
    heading = bearing_deg(a["lat"], a["lon"], b["lat"], b["lon"])

    return {
        "km": target_km,
        "lat": lat,
        "lon": lon,
        "heading": heading,
    }


def idw4_many(gid, coords):
    values = []
    point_sets = []
    for lat, lon in coords:
        value, points = idw4(gid, lat, lon)
        values.append(value)
        point_sets.append(points)
    return values, point_sets


def read_uv_many_indexed(path: Path, coords, gridmap):
    """
    Fast path. U/V value arrays are read once; route values are taken
    directly from cached grid indices. No nearest-grid search here.
    """
    meta = None
    found = {}
    t0 = time.time()
    diag(f"[ECCODES_FAST] read start file={path.name} coords={len(coords)}")

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
                if not comp or comp in found:
                    continue

                fp = grid_fingerprint(gid)
                if fp != gridmap["fingerprint"]:
                    raise RuntimeError(
                        f"WRF grid changed: cached={gridmap['fingerprint']} current={fp}"
                    )

                values = codes_get_array(gid, "values")
                vals, point_sets = apply_grid_mappings(values, gridmap)
                found[comp] = {"values": vals, "points": point_sets}

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

    diag(
        f"[ECCODES_FAST] read done file={path.name} "
        f"elapsed={time.time()-t0:.2f}s"
    )
    return meta, found["u"], found["v"]



def read_uv_many(path: Path, coords):
    meta = None
    found = {}
    t0 = time.time()
    diag(f"[ECCODES] scan start file={path.name} coords={len(coords)}")

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
                    values, point_sets = idw4_many(gid, coords)
                    found[comp] = {
                        "values": values,
                        "points": point_sets,
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

    diag(f"[ECCODES] scan done file={path.name} elapsed={time.time()-t0:.1f}s")
    return meta, found["u"], found["v"]


def load_uv_many_for_cycle(fh: int, coords, expected_init: int, gridmap=None):
    uv_path, meta = build_uv10_cache(fh)
    got_init = int(meta["init_epoch"])

    if gridmap is not None:
        try:
            _, u, v = read_uv_many_indexed(uv_path, coords, gridmap)
        except Exception as e:
            diag(
                f"[ECCODES_FAST] fallback to V0.2.2 nearest search "
                f"fh={fh:03d}: {e}"
            )
            _, u, v = read_uv_many(uv_path, coords)
    else:
        _, u, v = read_uv_many(uv_path, coords)

    if got_init != expected_init:
        invalidate_uv_cache(fh)
        uv_path, meta = build_uv10_cache(fh, force=True)
        got_init = int(meta["init_epoch"])

        if gridmap is not None:
            try:
                _, u, v = read_uv_many_indexed(uv_path, coords, gridmap)
            except Exception as e:
                diag(
                    f"[ECCODES_FAST] fallback after refresh "
                    f"fh={fh:03d}: {e}"
                )
                _, u, v = read_uv_many(uv_path, coords)
        else:
            _, u, v = read_uv_many(uv_path, coords)

    return {
        "fh": fh,
        "init": got_init,
        "u": u,
        "v": v,
        "matches_latest_cycle": got_init == expected_init,
    }


def wind_effect(wd: float, ws: float, heading: float):
    """
    Cycling-oriented wind interpretation.

    delta is the angle between meteorological wind FROM direction and rider
    heading:
      0°   = direct headwind
      90°  = pure crosswind
      180° = direct tailwind

    V0.2.9 geometric direction bands:
      0–22.5°        逆風
      22.5–67.5°     側逆風
      67.5–112.5°    側風
      112.5–157.5°   側順風
      157.5–180°     順風

    Geometric direction and perceived along-road effect are kept separate.
    The signed along-road component is:
      + = tailwind assistance
      - = headwind resistance

    UI-oriented along-road effect bands (not a meteorological standard):
      |along| < 0.3 m/s  幾乎無感
      0.3–1.0            輕微
      1.0–2.0            明顯
      2.0–3.0            強
      >=3.0              很強

    Total wind below 0.8 m/s is additionally flagged as 近無風 so a tiny
    tailwind component is not visually overstated.
    """
    delta = abs(((wd - heading + 180.0) % 360.0) - 180.0)

    raw = ws * math.cos(math.radians(delta))
    head = max(0.0, raw)
    tail = max(0.0, -raw)
    cross = abs(ws * math.sin(math.radians(delta)))
    along_signed = tail - head

    if delta <= 22.5:
        typ = "逆風"
    elif delta < 67.5:
        typ = "側逆風"
    elif delta <= 112.5:
        typ = "側風"
    elif delta < 157.5:
        typ = "側順風"
    else:
        typ = "順風"

    # Within the broad crosswind band, expose whether the small along-road
    # component is slightly head- or tail-biased.
    if typ == "側風":
        if delta < 90.0:
            direction_detail = "側風偏逆"
        elif delta > 90.0:
            direction_detail = "側風偏順"
        else:
            direction_detail = "純側風"
    else:
        direction_detail = typ

    along_abs = abs(along_signed)
    if along_abs < 0.3:
        strength = "幾乎無感"
    elif along_abs < 1.0:
        strength = "輕微"
    elif along_abs < 2.0:
        strength = "明顯"
    elif along_abs < 3.0:
        strength = "強"
    else:
        strength = "很強"

    near_calm = ws < 0.8

    if near_calm:
        display = "近無風"
    elif along_abs < 0.3 and typ == "側風":
        display = direction_detail
    else:
        display = typ

    return {
        "type": typ,
        "direction_detail": direction_detail,
        "display": display,
        "head_mps": head,
        "tail_mps": tail,
        "cross_mps": cross,
        "along_signed_mps": along_signed,
        "along_effect_strength": strength,
        "near_calm": near_calm,
        "relative_angle_deg": delta,
    }


def parse_departure(value: Optional[str]):
    if not value:
        return int(time.time())
    return parse_requested_time(None, value)


@app.get("/")
def root():
    return {
        "ok": True,
        "service": "wrf3km-api",
        "version": VERSION,
        "docs": "/docs",
        "health": "/health",
        "range": "/range",
        "route": "/route",
        "wind_example": "/wind?lat=24.15&lon=120.68",
        "route_forecast_example": "/route-forecast?speed_kmh=25&step_km=10",
        "optimization": "cached WRF grid indices + direct value-array lookup",
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
        "uv_cache_dir": str(UV_CACHE_DIR),
        "uv_cache_ttl_seconds_fh0_probe": UV_CACHE_TTL,
        "nonzero_uv_cache_policy": "keep_until_model_cycle_changes",
        "grid_index_cache": True,
        "concurrency_safe_uv_cache": True,
        "temporary_gpx_upload": True,
        "confirmed_range_probe": True,
        "route_geometry": True,
        "rest_point_planning": True,
        "wind_effect_v2": True,
        "max_rest_points": MAX_REST_POINTS,
        "max_gpx_upload_mb": MAX_GPX_UPLOAD_MB,
        "gridmap_cache_file": str(GRIDMAP_CACHE_FILE),
        "keep_full_grib": KEEP_FULL_GRIB,
        "cwa_fileapi_fallback_configured": bool(CWA_API_KEY),
        "note": "Health check does not download a GRIB2 file.",
    }


@app.get("/range")
def forecast_range(
    refresh: bool = Query(False, description="Force a fresh publication-range probe.")
):
    try:
        return get_confirmed_forecast_range(force=refresh)
    except Exception as e:
        # Conservative fallback: expose the theoretical model horizon but make
        # it explicit that publication verification failed.
        init = read_model_init()
        diag(f"[RANGE] confirmed-range probe failed; fallback to theoretical: {e}")
        return {
            "ok": True,
            "model": MODEL_NAME,
            "version": VERSION,
            "initial_time_utc": iso_utc(init),
            "initial_time_taipei": iso_taipei(init),
            "first_valid_time_taipei": iso_taipei(init),
            "last_valid_time_taipei": iso_taipei(init + MAX_FH * 3600),
            "confirmed_last_valid_time_taipei": None,
            "theoretical_last_valid_time_taipei": iso_taipei(init + MAX_FH * 3600),
            "latest_confirmed_hour": None,
            "theoretical_max_forecast_hour": MAX_FH,
            "publishing_complete": None,
            "forecast_hours": AVAILABLE_HOURS,
            "output_interval_hours": STEP_H,
            "verification_method": "verification_failed_theoretical_fallback",
            "verification_error": str(e),
            "range_cache_seconds": RANGE_PROBE_CACHE_SECONDS,
        }



@app.get("/diagnostics")
def diagnostics():
    return {
        "ok": True,
        "version": VERSION,
        "diagnostic_logging": True,
        "uv10_compact_cache": True,
        "grid_index_cache": True,
        "fast_value_array_lookup": True,
        "concurrency_safe_uv_cache": True,
        "temporary_gpx_upload": True,
        "confirmed_range_probe": True,
        "route_geometry": True,
        "rest_point_planning": True,
        "wind_effect_v2": True,
        "message": "Logs include U/V cache locks, GRIDMAP hits/misses, ECCODES_FAST and RANGE timings.",
    }


@app.get("/route")
def route_info():
    rc = load_current_route(force=True)
    route = rc["route"]
    return {
        "ok": True,
        "route_id": "current",
        "distance_km": round(route[-1]["cum"], 2),
        "points": len(route),
        "source": rc["source"],
    }


@app.get("/route-geometry")
def route_geometry(
    max_points: int = Query(2500, ge=100, le=5000),
):
    rc = load_current_route()
    route = rc["route"]
    geometry = simplified_route_geometry(route, max_points=max_points)
    return {
        "ok": True,
        "route_id": "current",
        "source": rc["source"],
        "distance_km": round(route[-1]["cum"], 2),
        "original_points": len(route),
        "geometry_points": len(geometry),
        "points": geometry,
    }



def parse_rest_points(raw: Optional[str]):
    """Parse a small JSON list of planned rests supplied by the forecast UI."""
    if raw is None or str(raw).strip() == "":
        return []

    try:
        items = json.loads(raw)
    except Exception as e:
        raise HTTPException(
            400,
            detail={"code": "REST_POINTS_JSON_INVALID", "message": str(e)},
        )

    if not isinstance(items, list):
        raise HTTPException(
            400,
            detail={"code": "REST_POINTS_NOT_LIST"},
        )
    if len(items) > MAX_REST_POINTS:
        raise HTTPException(
            400,
            detail={
                "code": "TOO_MANY_REST_POINTS",
                "max_rest_points": MAX_REST_POINTS,
            },
        )

    out = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise HTTPException(
                400,
                detail={"code": "REST_POINT_INVALID", "index": i},
            )
        try:
            km = float(item.get("km"))
            minutes = int(round(float(item.get("minutes"))))
        except Exception:
            raise HTTPException(
                400,
                detail={"code": "REST_POINT_VALUE_INVALID", "index": i},
            )

        if not math.isfinite(km) or km < 0:
            raise HTTPException(
                400,
                detail={"code": "REST_POINT_KM_INVALID", "index": i},
            )
        if minutes < 1 or minutes > 360:
            raise HTTPException(
                400,
                detail={
                    "code": "REST_POINT_MINUTES_INVALID",
                    "index": i,
                    "min": 1,
                    "max": 360,
                },
            )

        name = str(item.get("name") or f"休息點 {i+1}").strip()[:80]
        row = {"km": km, "minutes": minutes, "name": name}

        # Coordinates are informational only; ETA calculations use route km.
        for key in ("lat", "lon"):
            if item.get(key) is not None:
                try:
                    v = float(item.get(key))
                    if math.isfinite(v):
                        row[key] = v
                except Exception:
                    pass
        out.append(row)

    out.sort(key=lambda x: x["km"])
    return out


def simplified_route_geometry(route, max_points: int = 2500):
    """
    Return enough GPX geometry for a browser map without shipping every point.
    cum km is preserved so forecast samples and rest points can be mapped.
    """
    max_points = max(100, min(int(max_points), 5000))
    n = len(route)
    if n <= max_points:
        chosen = route
    else:
        stride = max(1, math.ceil((n - 1) / (max_points - 1)))
        chosen = route[::stride]
        if chosen[-1] is not route[-1]:
            chosen = chosen + [route[-1]]

    return [
        [round(p["lat"], 6), round(p["lon"], 6), round(p["cum"], 3)]
        for p in chosen
    ]


def _calculate_route_forecast(
    route,
    route_source: str,
    route_id: str,
    departure: Optional[str],
    speed_kmh: float,
    step_km: float,
    start_km: float,
    end_km: Optional[float],
    rest_points=None,
):
    req_t0 = time.time()
    diag(
        f"[ROUTE_FORECAST] start departure={departure or 'now'} "
        f"speed_kmh={speed_kmh} step_km={step_km} "
        f"start_km={start_km} end_km={end_km} "
        f"rest_points={len(rest_points or [])}"
    )
    route_distance = route[-1]["cum"]

    if start_km > route_distance:
        raise HTTPException(
            400,
            detail={
                "code": "START_KM_OUT_OF_ROUTE",
                "route_distance_km": round(route_distance, 2),
            },
        )

    final_km = route_distance if end_km is None else min(end_km, route_distance)
    if final_km < start_km:
        raise HTTPException(
            400,
            detail={"code": "END_KM_BEFORE_START_KM"},
        )

    rest_points = list(rest_points or [])
    for r in rest_points:
        if r["km"] > route_distance + 1e-6:
            raise HTTPException(
                400,
                detail={
                    "code": "REST_POINT_OUT_OF_ROUTE",
                    "rest_km": r["km"],
                    "route_distance_km": round(route_distance, 2),
                },
            )

    # A stop exactly at the starting point is already reflected by the chosen
    # departure time; a stop at the finish has no effect on riding ETA.
    active_rests = [
        r for r in rest_points
        if start_km < r["km"] < final_km
    ]
    active_rests.sort(key=lambda x: x["km"])
    total_rest_minutes = sum(r["minutes"] for r in active_rests)

    departure_epoch = parse_departure(departure)

    kms = []
    k = start_km
    while k < final_km - 1e-6:
        kms.append(round(k, 6))
        k += step_km
    if not kms or abs(kms[-1] - final_km) > 1e-6:
        kms.append(final_km)

    route_samples = [route_point_at_km(route, k) for k in kms]
    diag(
        f"[ROUTE_FORECAST] route ready samples={len(route_samples)} "
        f"route_distance_km={route_distance:.2f} "
        f"forecast_segment={start_km:.1f}-{final_km:.1f}km"
    )

    # ETA is based on distance travelled plus all completed planned rests.
    # At the exact rest-point km, ETA means arrival at that stop; the stop
    # delay is applied to downstream samples.
    for sample in route_samples:
        travelled = sample["km"] - start_km
        prior_rest_minutes = sum(
            r["minutes"] for r in active_rests
            if r["km"] < sample["km"] - 1e-6
        )
        sample["rest_delay_minutes"] = prior_rest_minutes
        sample["eta_epoch"] = (
            departure_epoch
            + travelled / speed_kmh * 3600.0
            + prior_rest_minutes * 60.0
        )

    timed_rests = []
    prior_minutes = 0
    for r in active_rests:
        arrival_epoch = (
            departure_epoch
            + (r["km"] - start_km) / speed_kmh * 3600.0
            + prior_minutes * 60.0
        )
        depart_epoch = arrival_epoch + r["minutes"] * 60.0
        timed_rests.append({
            **r,
            "km": round(r["km"], 2),
            "arrival_taipei": iso_taipei(arrival_epoch),
            "departure_taipei": iso_taipei(depart_epoch),
        })
        prior_minutes += r["minutes"]

    coords = [(x["lat"], x["lon"]) for x in route_samples]

    # Establish latest model cycle from FH0.
    diag("[ROUTE_FORECAST] reading latest model cycle from FH000")
    init = read_model_init(
        route_samples[0]["lat"],
        route_samples[0]["lon"]
    )
    model_end = init + MAX_FH * 3600
    diag(
        f"[ROUTE_FORECAST] model cycle init={iso_taipei(init)} "
        f"valid_until={iso_taipei(model_end)}"
    )

    needed_hours = set()
    time_specs = []

    for sample in route_samples:
        requested = sample["eta_epoch"]
        requested_fh = (requested - init) / 3600.0

        if requested_fh < 0:
            time_specs.append({
                "available": False,
                "reason": "BEFORE_FORECAST_RANGE",
                "requested_fh": requested_fh,
            })
            continue

        if requested_fh > MAX_FH:
            time_specs.append({
                "available": False,
                "reason": "AFTER_FORECAST_RANGE",
                "requested_fh": requested_fh,
            })
            continue

        lo = int(math.floor(requested_fh / STEP_H) * STEP_H)
        hi = int(math.ceil(requested_fh / STEP_H) * STEP_H)
        lo = max(0, min(MAX_FH, lo))
        hi = max(0, min(MAX_FH, hi))

        needed_hours.add(lo)
        needed_hours.add(hi)
        time_specs.append({
            "available": True,
            "requested_fh": requested_fh,
            "lo": lo,
            "hi": hi,
        })

    sorted_hours = sorted(needed_hours)
    diag(f"[ROUTE_FORECAST] needed forecast hours={sorted_hours}")

    # V0.2.3: nearest-grid search is done once for these route coordinates.
    # If mapping creation fails on an unexpected ecCodes build, preserve
    # accuracy/functionality by falling back to the proven V0.2.2 path.
    gridmap = None
    if sorted_hours:
        diag("[ROUTE_FORECAST] preparing reusable WRF grid-index map")
        try:
            gridmap = get_grid_mappings(coords, reference_fh=0)
        except Exception as e:
            diag(f"[GRIDMAP] unavailable; using legacy nearest search: {e}")

    fields = {}
    for fh in sorted_hours:
        fh_t0 = time.time()
        diag(f"[ROUTE_FORECAST] fh={fh:03d} begin")
        fields[fh] = load_uv_many_for_cycle(fh, coords, init, gridmap)
        diag(
            f"[ROUTE_FORECAST] fh={fh:03d} done "
            f"cycle_ok={fields[fh]['matches_latest_cycle']} "
            f"elapsed={time.time()-fh_t0:.1f}s"
        )

    output = []
    degraded_count = 0
    unavailable_count = 0

    for i, (sample, spec) in enumerate(zip(route_samples, time_specs)):
        base = {
            "km": round(sample["km"], 2),
            "lat": round(sample["lat"], 6),
            "lon": round(sample["lon"], 6),
            "heading_deg": round(sample["heading"], 1),
            "heading_text": dir_text(sample["heading"]),
            "eta_taipei": iso_taipei(sample["eta_epoch"]),
            "rest_delay_minutes": sample.get("rest_delay_minutes", 0),
        }

        if not spec["available"]:
            unavailable_count += 1
            output.append({
                **base,
                "available": False,
                "reason": spec["reason"],
            })
            continue

        lo = spec["lo"]
        hi = spec["hi"]
        rf = spec["requested_fh"]

        low = fields[lo]
        high = fields[hi]
        low_ok = low["matches_latest_cycle"]
        high_ok = high["matches_latest_cycle"]

        degraded = False
        fallback_reason = None

        if lo == hi and low_ok:
            u = low["u"]["values"][i]
            v = low["v"]["values"][i]
            used = [lo]
            alpha = 0.0
            point_sets = low["u"]["points"][i]

        elif low_ok and high_ok:
            alpha = (rf - lo) / (hi - lo)
            u = low["u"]["values"][i] + alpha * (
                high["u"]["values"][i] - low["u"]["values"][i]
            )
            v = low["v"]["values"][i] + alpha * (
                high["v"]["values"][i] - low["v"]["values"][i]
            )
            used = [lo, hi]
            point_sets = low["u"]["points"][i]

        elif low_ok or high_ok:
            degraded = True
            degraded_count += 1
            fallback_reason = "MODEL_CYCLE_ROLLOUT"

            choices = []
            if low_ok:
                choices.append((abs(rf - lo), lo, low))
            if high_ok:
                choices.append((abs(rf - hi), hi, high))

            _, chosen_fh, chosen = min(choices, key=lambda x: x[0])
            u = chosen["u"]["values"][i]
            v = chosen["v"]["values"][i]
            used = [chosen_fh]
            alpha = None
            point_sets = chosen["u"]["points"][i]

        else:
            unavailable_count += 1
            output.append({
                **base,
                "available": False,
                "reason": "MODEL_FILES_NOT_SYNCHRONIZED",
                "requested_source_hours": [lo, hi],
            })
            continue

        ws, wd = wind_from_uv(u, v)
        effect = wind_effect(wd, ws, sample["heading"])
        nearest = min(point_sets, key=lambda x: x["distance_km"])

        output.append({
            **base,
            "available": True,
            "degraded": degraded,
            "fallback_reason": fallback_reason,
            "wind_speed_mps": round(ws, 3),
            "wind_direction_deg": round(wd, 1),
            "wind_direction_text": dir_text(wd),
            "headwind_mps": round(effect["head_mps"], 3),
            "tailwind_mps": round(effect["tail_mps"], 3),
            "crosswind_mps": round(effect["cross_mps"], 3),
            "wind_effect": effect["type"],
            "wind_effect_detail": effect["direction_detail"],
            "wind_effect_display": effect["display"],
            "along_effect_mps": round(effect["along_signed_mps"], 3),
            "along_effect_strength": effect["along_effect_strength"],
            "near_calm": effect["near_calm"],
            "relative_wind_angle_deg": round(effect["relative_angle_deg"], 1),
            "requested_forecast_hour": round(rf, 3),
            "source_hours_used": used,
            "time_interpolation_alpha": None if alpha is None else round(alpha, 4),
            "nearest_model_point_distance_km": round(nearest["distance_km"], 3),
        })

    available = [x for x in output if x.get("available")]
    max_head = max(available, key=lambda x: x["headwind_mps"], default=None)
    max_tail = max(available, key=lambda x: x["tailwind_mps"], default=None)

    # Do not report a meaningless "max headwind = 0.0 m/s".
    if max_head and max_head["headwind_mps"] <= 0.05:
        max_head = None
    if max_tail and max_tail["tailwind_mps"] <= 0.05:
        max_tail = None

    summary = {
        "samples_total": len(output),
        "samples_available": len(available),
        "samples_degraded": degraded_count,
        "samples_unavailable": unavailable_count,
        "max_headwind": None if not max_head else {
            "km": max_head["km"],
            "eta_taipei": max_head["eta_taipei"],
            "headwind_mps": max_head["headwind_mps"],
            "wind_direction_text": max_head["wind_direction_text"],
        },
        "max_tailwind": None if not max_tail else {
            "km": max_tail["km"],
            "eta_taipei": max_tail["eta_taipei"],
            "tailwind_mps": max_tail["tailwind_mps"],
            "wind_direction_text": max_tail["wind_direction_text"],
        },
    }

    diag(
        f"[ROUTE_FORECAST] complete total={len(output)} "
        f"available={len(available)} degraded={degraded_count} "
        f"unavailable={unavailable_count} "
        f"elapsed={time.time()-req_t0:.1f}s"
    )

    return {
        "ok": True,
        "service": "wrf3km-api",
        "version": VERSION,
        "model": MODEL_NAME,
        "performance": {
            "uv10_compact_cache": True,
            "grid_index_cache": True,
            "fast_value_array_lookup": True,
            "concurrency_safe_uv_cache": True,
            "wind_effect_classification": "angle_bands_v2_plus_along_effect_strength",
            "temporary_gpx_upload": True,
            "rest_point_planning": True,
            "route_geometry": True,
            "uv_cache_ttl_seconds_fh0_probe": UV_CACHE_TTL,
            "nonzero_uv_cache_policy": "keep_until_model_cycle_changes",
            "full_grib_retained": KEEP_FULL_GRIB,
        },
        "route": {
            "route_id": route_id,
            "source": route_source,
            "distance_km": round(route_distance, 2),
            "forecast_start_km": round(start_km, 2),
            "forecast_end_km": round(final_km, 2),
        },
        "ride": {
            "departure_taipei": iso_taipei(departure_epoch),
            "speed_kmh": speed_kmh,
            "step_km": step_km,
            "moving_minutes": round(
                (final_km - start_km) / speed_kmh * 60.0, 1
            ),
            "rest_minutes": total_rest_minutes,
            "total_minutes": round(
                (final_km - start_km) / speed_kmh * 60.0
                + total_rest_minutes, 1
            ),
            "rest_points": timed_rests,
            "estimated_arrival_taipei": iso_taipei(
                departure_epoch
                + (final_km - start_km) / speed_kmh * 3600.0
                + total_rest_minutes * 60.0
            ),
        },
        "model_cycle": {
            "initial_time_taipei": iso_taipei(init),
            "last_valid_time_taipei": iso_taipei(model_end),
        },
        "summary": summary,
        "samples": output,
    }



@app.get("/route-forecast")
def route_forecast(
    departure: Optional[str] = Query(
        None,
        description="ISO-8601 departure time. No timezone = Taiwan local time. Omit = now."
    ),
    speed_kmh: float = Query(25.0, ge=5.0, le=60.0),
    step_km: float = Query(10.0, ge=2.0, le=50.0),
    start_km: float = Query(0.0, ge=0.0),
    end_km: Optional[float] = Query(None, ge=0.0),
    rest_points: Optional[str] = Query(
        None,
        description="JSON list: [{km, minutes, name, lat?, lon?}]"
    ),
):
    rc = load_current_route()
    rests = parse_rest_points(rest_points)
    return _calculate_route_forecast(
        route=rc["route"],
        route_source=rc["source"],
        route_id="current",
        departure=departure,
        speed_kmh=speed_kmh,
        step_km=step_km,
        start_km=start_km,
        end_km=end_km,
        rest_points=rests,
    )


@app.post("/route-forecast-upload")
def route_forecast_upload(
    file: UploadFile = File(..., description="Temporary GPX file. Parsed in memory and not saved."),
    departure: Optional[str] = Form(None),
    speed_kmh: float = Form(25.0, ge=5.0, le=60.0),
    step_km: float = Form(10.0, ge=2.0, le=50.0),
    start_km: float = Form(0.0, ge=0.0),
    end_km: Optional[float] = Form(None, ge=0.0),
    rest_points: Optional[str] = Form(None),
):
    """Forecast a one-off GPX without changing current.gpx."""
    raw_name = (file.filename or "temporary.gpx").strip()
    safe_name = Path(raw_name).name

    if not safe_name.lower().endswith(".gpx"):
        raise HTTPException(
            400,
            detail={
                "code": "INVALID_GPX_FILENAME",
                "message": "Please upload a .gpx file.",
            },
        )

    max_bytes = MAX_GPX_UPLOAD_MB * 1024 * 1024
    data = file.file.read(max_bytes + 1)

    if len(data) > max_bytes:
        raise HTTPException(
            413,
            detail={
                "code": "GPX_TOO_LARGE",
                "message": f"GPX upload exceeds {MAX_GPX_UPLOAD_MB} MB.",
            },
        )
    if len(data) < 32:
        raise HTTPException(
            400,
            detail={"code": "EMPTY_GPX", "message": "GPX file is empty or invalid."},
        )

    try:
        route = parse_gpx(data)
    except Exception as e:
        raise HTTPException(
            400,
            detail={
                "code": "GPX_PARSE_FAILED",
                "message": str(e),
            },
        )

    rests = parse_rest_points(rest_points)

    diag(
        f"[ROUTE_UPLOAD] temporary file={safe_name} "
        f"bytes={len(data)} points={len(route)} distance_km={route[-1]['cum']:.2f} "
        f"rest_points={len(rests)}"
    )

    return _calculate_route_forecast(
        route=route,
        route_source=f"upload:{safe_name}",
        route_id="temporary_upload",
        departure=departure,
        speed_kmh=speed_kmh,
        step_km=step_km,
        start_km=start_km,
        end_km=end_km,
        rest_points=rests,
    )


def load_uv_for_cycle(fh: int, lat: float, lon: float, expected_init: int):
    """
    Read one forecast-hour from the compact two-message U/V cache.
    If its model cycle does not match FH0, force-refresh source + cache once.
    """
    uv_path, meta = build_uv10_cache(fh)
    got_init = int(meta["init_epoch"])
    meta_read, u, v = read_uv(uv_path, lat, lon)

    if got_init != expected_init:
        invalidate_uv_cache(fh)
        uv_path, meta = build_uv10_cache(fh, force=True)
        got_init = int(meta["init_epoch"])
        meta_read, u, v = read_uv(uv_path, lat, lon)

    return {
        "fh": fh,
        "meta": meta_read,
        "init": got_init,
        "u": u,
        "v": v,
        "matches_latest_cycle": got_init == expected_init,
    }


@app.get("/wind")
def wind(
    lat: float = Query(..., ge=14.0, le=32.2),
    lon: float = Query(..., ge=105.0, le=141.0),
    time_epoch: Optional[int] = Query(None, description="Unix seconds UTC"),
    time: Optional[str] = Query(None, description="ISO-8601. Without timezone = Taiwan local time."),
):
    requested = parse_requested_time(time_epoch, time)

    # FH0 establishes the newest model cycle currently published.
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

    degraded = False
    warning = None
    fallback_reason = None

    low = load_uv_for_cycle(lo, lat, lon, init)

    if hi == lo:
        high = low
    else:
        high = load_uv_for_cycle(hi, lat, lon, init)

    low_ok = low["matches_latest_cycle"]
    high_ok = high["matches_latest_cycle"]

    if lo == hi and low_ok:
        u = low["u"]["value"]
        v = low["v"]["value"]
        alpha = 0.0
        used_hours = [lo]

    elif low_ok and high_ok:
        alpha = (requested_fh - lo) / (hi - lo)
        u = low["u"]["value"] + alpha * (high["u"]["value"] - low["u"]["value"])
        v = low["v"]["value"] + alpha * (high["v"]["value"] - low["v"]["value"])
        used_hours = [lo, hi]

    elif low_ok or high_ok:
        # CWA is likely rolling from one model cycle to the next.
        # Do not mix cycles. Use the closest source hour that already belongs
        # to the latest cycle, and mark the response as degraded.
        degraded = True
        fallback_reason = "MODEL_CYCLE_ROLLOUT"
        warning = (
            "CWA model files are being updated. Time interpolation was disabled "
            "and the nearest forecast hour from the latest model cycle was used."
        )

        candidates = []
        if low_ok:
            candidates.append((abs(requested_fh - lo), low))
        if high_ok:
            candidates.append((abs(requested_fh - hi), high))
        _, chosen = min(candidates, key=lambda x: x[0])

        u = chosen["u"]["value"]
        v = chosen["v"]["value"]
        alpha = None
        used_hours = [chosen["fh"]]

    else:
        # Neither required source hour belongs to the newest cycle even after
        # a forced refresh. There is no safe same-cycle value to return.
        raise HTTPException(
            503,
            detail={
                "code": "MODEL_FILES_NOT_SYNCHRONIZED",
                "message": (
                    "CWA is still publishing the new WRF-3KM cycle. "
                    "Both required forecast-hour files are stale even after refresh."
                ),
                "latest_cycle_taipei": iso_taipei(init),
                "requested_source_hours": [lo, hi],
                "source_cycles_taipei": {
                    str(lo): iso_taipei(low["init"]),
                    str(hi): iso_taipei(high["init"]),
                },
                "retry_later": True,
            },
        )

    speed, direction = wind_from_uv(u, v)

    representative_points = low["u"]["points"] if low_ok else high["u"]["points"]
    nearest = min(representative_points, key=lambda x: x["distance_km"])

    return {
        "ok": True,
        "model": MODEL_NAME,
        "version": VERSION,
        "degraded": degraded,
        "warning": warning,
        "fallback_reason": fallback_reason,
        "requested_time_utc": iso_utc(requested),
        "requested_time_taipei": iso_taipei(requested),
        "initial_time_utc": iso_utc(init),
        "initial_time_taipei": iso_taipei(init),
        "last_valid_time_taipei": iso_taipei(init + MAX_FH * 3600),
        "requested_forecast_hour": round(requested_fh, 3),
        "requested_source_hours": [lo, hi],
        "source_hours_used": used_hours,
        "time_interpolation_alpha": None if alpha is None else round(alpha, 4),
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

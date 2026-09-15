
import math, os, time
from pathlib import Path
import requests
from fastapi import FastAPI, File, UploadFile, HTTPException

APP_DIR = Path(__file__).resolve().parent
ROUTES_DIR = APP_DIR / "routes"
ROUTES_DIR.mkdir(exist_ok=True)

CWA_KEY = os.getenv("CWA_API_KEY", "").strip()
CURRENT_ROUTE_URL = os.getenv(
    "CURRENT_ROUTE_URL",
    "https://raw.githubusercontent.com/harrypotter0618/cwa-wind/main/ride-api/routes/current.gpx"
).strip()

WIND_URL = "https://opendata.cwa.gov.tw/api/v1/rest/datastore/O-A0003-001"
RAIN_URL = "https://opendata.cwa.gov.tw/api/v1/rest/datastore/O-A0002-001"

CWA_CACHE_SECONDS = int(os.getenv("CWA_CACHE_SECONDS", "300"))
ROUTE_CACHE_SECONDS = int(os.getenv("ROUTE_CACHE_SECONDS", "300"))

ROUTE_BACK_KM = float(os.getenv("ROUTE_BACK_KM", "5"))
ROUTE_AHEAD_KM = float(os.getenv("ROUTE_AHEAD_KM", "10"))
ROUTE_CROSS_KM = float(os.getenv("ROUTE_CROSS_KM", "8"))
MAX_STATIONS = int(os.getenv("MAX_STATIONS", "5"))

app = FastAPI(title="CWA Ride API", version="0.3.0")

R = 6371.0
DIR16 = ["北","北北東","東北","東北東","東","東南東","東南","南南東",
         "南","南南西","西南","西南西","西","西北西","西北","北北西"]

_cwa_cache = {"ts":0, "wind":[], "rain":[]}
_route_cache = {"ts":0, "route":None, "source":None}

def rad(x): return math.radians(x)

def hav(a,b,c,d):
    A=rad(c-a); B=rad(d-b)
    q=math.sin(A/2)**2 + math.cos(rad(a))*math.cos(rad(c))*math.sin(B/2)**2
    return 2*R*math.asin(math.sqrt(q))

def bearing(a,b,c,d):
    y=math.sin(rad(d-b))*math.cos(rad(c))
    x=math.cos(rad(a))*math.sin(rad(c))-math.sin(rad(a))*math.cos(rad(c))*math.cos(rad(d-b))
    return (math.degrees(math.atan2(y,x))+360)%360

def dir_text(d): return DIR16[round(d/22.5)%16]

def valid_num(x):
    try:
        n=float(x)
        return n if -90 < n < 1000 else None
    except:
        return None

def get_wgs84(st):
    for c in st.get("GeoInfo",{}).get("Coordinates",[]):
        if str(c.get("CoordinateName","")).upper()=="WGS84":
            lat=valid_num(c.get("StationLatitude"))
            lon=valid_num(c.get("StationLongitude"))
            if lat is not None and lon is not None:
                return lat,lon
    return None

def rain_value(x):
    if isinstance(x,dict):
        x=x.get("Precipitation")
    if x is None: return None
    s=str(x).strip()
    if s in ("T","-98"): return 0.0
    if s in ("X","-99",""): return None
    try:
        n=float(s)
        return n if 0 <= n < 1000 else None
    except:
        return None

def wind_parts(wd,ws,h):
    x=ws*math.cos(rad(wd-h))
    head=max(0,x); tail=max(0,-x); cross=abs(ws*math.sin(rad(wd-h)))
    typ="側風"
    if head>max(tail,0.7): typ="逆風"
    elif tail>max(head,0.7): typ="順風"
    elif tail>0.35: typ="側順風"
    elif head>0.35: typ="側逆風"
    return {"head":head,"tail":tail,"cross":cross,"type":typ}

def parse_gpx(data: bytes):
    import xml.etree.ElementTree as ET
    root=ET.fromstring(data)
    pts=[]
    for el in root.iter():
        if el.tag.endswith("trkpt") or el.tag.endswith("rtept"):
            try:
                pts.append({"lat":float(el.attrib["lat"]),"lon":float(el.attrib["lon"])})
            except:
                pass
    if len(pts)<2:
        raise ValueError("GPX points < 2")
    cum=0.0
    for i,p in enumerate(pts):
        if i:
            cum += hav(pts[i-1]["lat"],pts[i-1]["lon"],p["lat"],p["lon"])
        p["cum"]=cum
        j=min(i+1,len(pts)-1)
        k=max(i-1,0)
        p["heading"]=bearing(pts[k]["lat"],pts[k]["lon"],pts[j]["lat"],pts[j]["lon"])
    return pts

def load_current_route(force=False):
    global _route_cache
    if (not force and _route_cache["route"] and
        time.time()-_route_cache["ts"] < ROUTE_CACHE_SECONDS):
        return _route_cache

    if CURRENT_ROUTE_URL:
        try:
            r=requests.get(CURRENT_ROUTE_URL,timeout=20,headers={"Cache-Control":"no-cache"})
            if r.ok:
                route=parse_gpx(r.content)
                _route_cache={"ts":time.time(),"route":route,"source":"github"}
                return _route_cache
        except:
            pass

    f=ROUTES_DIR/"current.gpx"
    if f.exists():
        route=parse_gpx(f.read_bytes())
        _route_cache={"ts":time.time(),"route":route,"source":"runtime"}
        return _route_cache

    raise HTTPException(404,"current.gpx not found")

def locate(route,lat,lon):
    best=None
    for i,p in enumerate(route):
        d=hav(lat,lon,p["lat"],p["lon"])
        if best is None or d<best["offroute_km"]:
            best={"route_km":p["cum"],"heading":p["heading"],"offroute_km":d,
                  "lat":p["lat"],"lon":p["lon"],"index":i}
    return best

def route_projection(route, station, current_km):
    lo=current_km-ROUTE_BACK_KM
    hi=current_km+ROUTE_AHEAD_KM
    best=None
    for p in route:
        if p["cum"] < lo or p["cum"] > hi:
            continue
        d=hav(station["lat"],station["lon"],p["lat"],p["lon"])
        if best is None or d<best["cross_km"]:
            best={
                "cross_km":d,
                "route_km":p["cum"],
                "along_km":p["cum"]-current_km,
                "heading":p["heading"]
            }
    return best

def select_route_stations(route, current, wind):
    candidates=[]
    for s in wind:
        pr=route_projection(route,s,current["route_km"])
        if not pr or pr["cross_km"] > ROUTE_CROSS_KM:
            continue
        gps_d=hav(current["lat"],current["lon"],s["lat"],s["lon"])
        score = pr["cross_km"]*2.0 + abs(pr["along_km"])*0.35 + gps_d*0.05
        candidates.append({**s,
                           "gps_dist_km":gps_d,
                           "route_cross_km":pr["cross_km"],
                           "route_along_km":pr["along_km"],
                           "route_heading":pr["heading"],
                           "score":score})
    candidates.sort(key=lambda x:x["score"])

    selected=[]
    for s in candidates:
        duplicate=any(hav(s["lat"],s["lon"],q["lat"],q["lon"]) < 1.0 for q in selected)
        if not duplicate:
            selected.append(s)
        if len(selected)>=MAX_STATIONS:
            break

    if len(selected)<3:
        fallback=[]
        for s in wind:
            gps_d=hav(current["lat"],current["lon"],s["lat"],s["lon"])
            if gps_d<=12:
                fallback.append({**s,
                                 "gps_dist_km":gps_d,
                                 "route_cross_km":None,
                                 "route_along_km":None,
                                 "route_heading":current["heading"],
                                 "score":100+gps_d})
        fallback.sort(key=lambda x:x["score"])
        existing={x["id"] for x in selected}
        for s in fallback:
            if s["id"] not in existing:
                selected.append(s)
                existing.add(s["id"])
            if len(selected)>=MAX_STATIONS:
                break
    return selected

def fetch_cwa():
    global _cwa_cache
    if time.time()-_cwa_cache["ts"] < CWA_CACHE_SECONDS and _cwa_cache["wind"]:
        return _cwa_cache["wind"], _cwa_cache["rain"]

    if not CWA_KEY:
        raise HTTPException(500,"CWA_API_KEY missing")

    wr=requests.get(WIND_URL,params={"Authorization":CWA_KEY},timeout=20)
    rr=requests.get(RAIN_URL,params={"Authorization":CWA_KEY},timeout=20)
    wr.raise_for_status(); rr.raise_for_status()
    wj=wr.json(); rj=rr.json()

    rain=[]
    for st in rj.get("records",{}).get("Station",[]):
        c=get_wgs84(st)
        if not c: continue
        rv=rain_value(st.get("RainfallElement",{}).get("Past1hr",{}).get("Precipitation"))
        if rv is None: continue
        rain.append({"name":st.get("StationName",""),
                     "lat":c[0],"lon":c[1],"rain1h":rv})

    wind=[]
    for st in wj.get("records",{}).get("Station",[]):
        c=get_wgs84(st)
        if not c: continue
        we=st.get("WeatherElement",{})
        ws=valid_num(we.get("WindSpeed"))
        wd=valid_num(we.get("WindDirection"))
        if ws is None or wd is None: continue

        nearest=None; nd=999
        for r in rain:
            d=hav(c[0],c[1],r["lat"],r["lon"])
            if d<nd and d<=10:
                nearest=r; nd=d

        wind.append({
            "name":st.get("StationName",""),
            "id":st.get("StationId",""),
            "lat":c[0],"lon":c[1],
            "ws":ws,"wd":wd,
            "rain1h":nearest["rain1h"] if nearest else None,
            "rain_source":nearest["name"] if nearest else ""
        })

    _cwa_cache={"ts":time.time(),"wind":wind,"rain":rain}
    return wind,rain

def route_aware_summary(stations):
    if not stations:
        return None
    sx=sy=0.0; heads=[]; tails=[]; crosses=[]; rains=[]
    for s in stations:
        to=rad((s["wd"]+180)%360)
        sx += math.sin(to)*s["ws"]
        sy += math.cos(to)*s["ws"]
        p=wind_parts(s["wd"],s["ws"],s["route_heading"])
        heads.append(p["head"]); tails.append(p["tail"]); crosses.append(p["cross"])
        if s.get("rain1h") is not None:
            rains.append(s["rain1h"])

    n=len(stations)
    vx=sx/n; vy=sy/n
    avg=(vx*vx+vy*vy)**0.5
    wd=((math.degrees(math.atan2(vx,vy))+360)%360+180)%360
    head=sum(heads)/n; tail=sum(tails)/n; cross=sum(crosses)/n

    typ="側風"
    if head>max(tail,0.7): typ="逆風"
    elif tail>max(head,0.7): typ="順風"
    elif tail>0.35: typ="側順風"
    elif head>0.35: typ="側逆風"

    return {"wd":wd,"avg":avg,"p":{"head":head,"tail":tail,"cross":cross,"type":typ},
            "rain":max(rains) if rains else None}

@app.get("/health")
def health():
    try:
        rc=load_current_route()
        route_ok=True; source=rc["source"]
    except:
        route_ok=False; source=None
    return {
        "ok":True,
        "version":"0.3.0",
        "cwa_key_configured":bool(CWA_KEY),
        "current_route_available":route_ok,
        "current_route_source":source,
        "station_selection":"route-aware"
    }

@app.get("/route")
def route_info():
    rc=load_current_route(force=True)
    route=rc["route"]
    return {
        "ok":True,
        "route_id":"current",
        "distance_km":round(route[-1]["cum"],2),
        "points":len(route),
        "source":rc["source"]
    }

@app.post("/route/current")
async def upload_current(file: UploadFile = File(...)):
    global _route_cache
    data=await file.read()
    try:
        route=parse_gpx(data)
    except Exception as e:
        raise HTTPException(400,f"Invalid GPX: {e}")
    (ROUTES_DIR/"current.gpx").write_bytes(data)
    _route_cache={"ts":time.time(),"route":route,"source":"runtime"}
    return {
        "ok":True,
        "route_id":"current",
        "distance_km":round(route[-1]["cum"],2),
        "points":len(route),
        "source":"runtime",
        "note":"For permanent use, replace GitHub ride-api/routes/current.gpx"
    }

@app.get("/status")
def status(lat:float, lon:float, route_id:str="current"):
    if route_id!="current":
        raise HTTPException(400,"V0.3 fixed route_id is current")

    rc=load_current_route()
    route=rc["route"]
    here=locate(route,lat,lon)
    current={**here,"lat":lat,"lon":lon}

    wind,_=fetch_cwa()
    selected=select_route_stations(route,current,wind)
    if not selected:
        raise HTTPException(503,"No usable route-representative wind stations")

    sm=route_aware_summary(selected)
    wd=sm["wd"]; avg=sm["avg"]; p=sm["p"]; rain=sm["rain"]

    speech=(f"目前附近實況。主風{dir_text(wd)}，{wd:.0f}度，"
            f"平均風速{avg:.1f}公尺每秒。目前為{p['type']}。")
    if p["head"]>0.05: speech+=f"逆風分量{p['head']:.1f}公尺每秒。"
    if p["tail"]>0.05: speech+=f"順風分量{p['tail']:.1f}公尺每秒。"
    if p["cross"]>0.2: speech+=f"側風{p['cross']:.1f}公尺每秒。"
    if rain is None:
        speech+="近一小時雨量資料不明。"
    elif rain<=0:
        speech+="近一小時無雨。"
    else:
        speech+=f"近一小時雨量{rain:.1f}毫米。"

    return {
        "ok":True,
        "route_id":"current",
        "route_source":rc["source"],
        "route":{
            "km":round(here["route_km"],2),
            "heading_deg":round(here["heading"],1),
            "heading_text":dir_text(here["heading"]),
            "offroute_km":round(here["offroute_km"],2)
        },
        "station_selection":{
            "mode":"route-aware",
            "window_back_km":ROUTE_BACK_KM,
            "window_ahead_km":ROUTE_AHEAD_KM,
            "max_cross_km":ROUTE_CROSS_KM,
            "selected_count":len(selected)
        },
        "wind":{
            "direction_deg":round(wd,1),
            "direction_text":dir_text(wd),
            "speed_ms":round(avg,2),
            "type":p["type"],
            "head_ms":round(p["head"],2),
            "tail_ms":round(p["tail"],2),
            "cross_ms":round(p["cross"],2)
        },
        "rain":{"past_1h_mm":None if rain is None else round(rain,2)},
        "stations":[
            {
                "name":s["name"],
                "id":s["id"],
                "wind_direction_deg":s["wd"],
                "wind_speed_ms":s["ws"],
                "gps_distance_km":round(s["gps_dist_km"],2),
                "route_cross_km":None if s["route_cross_km"] is None else round(s["route_cross_km"],2),
                "route_along_km":None if s["route_along_km"] is None else round(s["route_along_km"],2),
                "route_heading_deg":round(s["route_heading"],1),
                "route_heading_text":dir_text(s["route_heading"]),
                "rain_1h_mm":s.get("rain1h"),
                "rain_source":s.get("rain_source")
            } for s in selected
        ],
        "speech":speech
    }

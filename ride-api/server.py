
import json, math, os, time
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

app = FastAPI(title="CWA Ride API", version="0.2.0")

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
        j=min(i+1,len(pts)-1); k=max(i-1,0)
        p["heading"]=bearing(pts[k]["lat"],pts[k]["lon"],pts[j]["lat"],pts[j]["lon"])
    return pts

def load_current_route(force=False):
    global _route_cache
    if (not force and _route_cache["route"] and
        time.time()-_route_cache["ts"] < ROUTE_CACHE_SECONDS):
        return _route_cache

    # persistent source: GitHub raw current.gpx
    if CURRENT_ROUTE_URL:
        try:
            r=requests.get(CURRENT_ROUTE_URL, timeout=20, headers={"Cache-Control":"no-cache"})
            if r.ok:
                route=parse_gpx(r.content)
                _route_cache={"ts":time.time(),"route":route,"source":"github"}
                return _route_cache
        except:
            pass

    # fallback: runtime upload
    f=ROUTES_DIR/"current.gpx"
    if f.exists():
        route=parse_gpx(f.read_bytes())
        _route_cache={"ts":time.time(),"route":route,"source":"runtime"}
        return _route_cache

    raise HTTPException(404,"current.gpx not found")

def locate(route,lat,lon):
    best=None
    for p in route:
        d=hav(lat,lon,p["lat"],p["lon"])
        if best is None or d<best["offroute_km"]:
            best={"route_km":p["cum"],"heading":p["heading"],"offroute_km":d}
    return best

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
        rain.append({"name":st.get("StationName",""),"lat":c[0],"lon":c[1],"rain1h":rv})

    wind=[]
    for st in wj.get("records",{}).get("Station",[]):
        c=get_wgs84(st)
        if not c: continue
        we=st.get("WeatherElement",{})
        ws=valid_num(we.get("WindSpeed")); wd=valid_num(we.get("WindDirection"))
        if ws is None or wd is None: continue
        nearest=None; nd=999
        for r in rain:
            d=hav(c[0],c[1],r["lat"],r["lon"])
            if d < nd and d <= 10:
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

def vector_summary(stations,heading):
    if not stations: return None
    sx=sy=0.0; comps=[]; rains=[]
    for s in stations:
        to=rad((s["wd"]+180)%360)
        sx += math.sin(to)*s["ws"]
        sy += math.cos(to)*s["ws"]
        comps.append(wind_parts(s["wd"],s["ws"],heading))
        if s.get("rain1h") is not None:
            rains.append(s["rain1h"])
    n=len(stations)
    vx=sx/n; vy=sy/n
    avg=(vx*vx+vy*vy)**0.5
    wd=((math.degrees(math.atan2(vx,vy))+360)%360+180)%360
    p=wind_parts(wd,avg,heading)
    p["head"]=sum(x["head"] for x in comps)/n
    p["tail"]=sum(x["tail"] for x in comps)/n
    p["cross"]=sum(x["cross"] for x in comps)/n
    return {"wd":wd,"avg":avg,"p":p,"rain":max(rains) if rains else None}

@app.get("/health")
def health():
    try:
        rc=load_current_route()
        route_ok=True; source=rc["source"]
    except:
        route_ok=False; source=None
    return {
        "ok":True,
        "version":"0.2.0",
        "cwa_key_configured":bool(CWA_KEY),
        "current_route_available":route_ok,
        "current_route_source":source
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
        raise HTTPException(400,"V0.2 fixed route_id is current")

    rc=load_current_route()
    route=rc["route"]
    here=locate(route,lat,lon)
    heading=here["heading"]

    wind,_=fetch_cwa()
    near=[{**s,"dist_km":hav(lat,lon,s["lat"],s["lon"])} for s in wind]
    near=sorted(near,key=lambda x:x["dist_km"])[:5]

    sm=vector_summary(near,heading)
    if not sm:
        raise HTTPException(503,"No usable wind stations")

    wd=sm["wd"]; avg=sm["avg"]; p=sm["p"]; rain=sm["rain"]
    speech=f"目前附近實況。主風{dir_text(wd)}，{wd:.0f}度，平均風速{avg:.1f}公尺每秒。目前為{p['type']}。"
    if p["head"]>0.05: speech+=f"逆風分量{p['head']:.1f}公尺每秒。"
    if p["tail"]>0.05: speech+=f"順風分量{p['tail']:.1f}公尺每秒。"
    if p["cross"]>0.2: speech+=f"側風{p['cross']:.1f}公尺每秒。"
    speech += "近一小時雨量資料不明。" if rain is None else ("近一小時無雨。" if rain<=0 else f"近一小時雨量{rain:.1f}毫米。")

    return {
        "ok":True,
        "route_id":"current",
        "route_source":rc["source"],
        "route":{
            "km":round(here["route_km"],2),
            "heading_deg":round(heading,1),
            "heading_text":dir_text(heading),
            "offroute_km":round(here["offroute_km"],2)
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
        "speech":speech
    }

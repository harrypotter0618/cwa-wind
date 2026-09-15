# WRF3KM Render V0.2.2 — 效能快取 + 摘要修正

V0.2.2 延續 V0.2.1 的 GPX + ETA 沿線預報與完整 Render 診斷 Log，
這一版主要解決已經實測確認的效能瓶頸。

## 已確認的舊瓶頸

CWA 每個 WRF3KM forecast-hour GRIB2 約 170 MB。

V0.2.1 實測：
- 下載約 7–14 秒 / 檔
- ecCodes 掃完整 GRIB 約 29–30 秒 / 檔

因此即使只是改均速、出發時間或取樣間距，
如果又重新掃完整 170 MB GRIB，會非常浪費時間。

## V0.2.2 核心改良：10 m U/V compact cache

第一次碰到某個 forecast hour：

```text
170 MB 完整 GRIB
  ↓ 掃一次
只萃取 10 m U + 10 m V
  ↓
寫成小型 *_uv10.grb2 快取
```

之後 `/wind` 與 `/route-forecast` 都直接讀這個只含兩個訊息的 compact GRIB，
不再每次重新掃 170 MB 原始檔。

Render Log 會看到：

```text
[UV_CACHE] miss fh=006 ... extracting 10m U/V
[UV_CACHE] built fh=006 ...
```

之後同一 forecast hour 再查：

```text
[UV_CACHE] hit fh=006 ...
```

### 完整 GRIB 預設不保留

萃取成功後，預設刪除 170 MB 原始檔，只保留小型 U/V cache，
避免 Render 暫存空間被 3～5 個大型 GRIB 撐滿。

環境變數：

```text
KEEP_FULL_GRIB=false
UV_CACHE_TTL_SECONDS=1200
```

如果未來需要保留完整 GRIB 才改成 true。

## 模式換輪防呆仍保留

V0.1.1 的功能全部保留：

- FH0/FH6 等不同 model cycle → 強制重新抓一次
- 至少一個需要的 forecast hour 已更新 → degraded fallback
- 不會把不同輪資料硬混在一起
- 完全沒有安全資料才回 503

compact U/V cache 也會跟著 model cycle 驗證，
發現 cycle 不一致會清掉該 forecast hour 的 U/V cache 再重建。

## 摘要修正

舊版在整段路完全沒有逆風時可能顯示：

```text
max_headwind = 0.0 m/s at KM 0
```

V0.2.2 改成：

```json
"max_headwind": null
```

同理，如果整段沒有任何順風分量：

```json
"max_tailwind": null
```

避免把 0.0 m/s 當成有意義的最大值。

## 部署

直接覆蓋 GitHub：

```text
wrf3km-api/
├─ app.py
├─ Dockerfile
├─ requirements.txt
├─ render.yaml
└─ README_部署說明.md
```

不需要建立新的 Render Web Service。
Commit 後讓原本 `wrf3km-api` Auto Deploy。

## 測試

### 1. Health

```text
/health
```

應看到：

```json
"version": "0.2.2"
```

並且：

```json
"uv_cache_dir": "/tmp/wrf3km-cache/uv10",
"keep_full_grib": false
```

### 2. Diagnostics

```text
/diagnostics
```

應看到：

```json
"version": "0.2.2",
"uv10_compact_cache": true
```

### 3. 前 50 km

```text
/route-forecast?speed_kmh=25&step_km=10&start_km=0&end_km=50
```

第一次仍需要對 FH0/FH6/FH12 各掃一次完整 GRIB，
所以第一次不會瞬間完成。

但第一次建立 U/V cache 後，馬上用相同 forecast hours 再測一次：

```text
/route-forecast?speed_kmh=26&step_km=10&start_km=0&end_km=50
```

Log 應看到多個：

```text
[UV_CACHE] hit ...
```

第二次速度應大幅縮短。

## 目前架構

```text
CWA WRF3KM full GRIB
        ↓ 首次
ecCodes 找 10m U/V
        ↓
compact U/V GRIB cache
        ↓
GPX 座標空間插值
        ↓
ETA 時間插值
        ↓
順 / 逆 / 側風
```

下一階段才適合接前端地圖與出發時間、均速、休息策略的 UI。

# WRF3KM Render V0.2 — GPX + ETA 沿線預報

V0.2 延續 V0.1.1 已驗證成功的：
- CWA WRF-3KM GRIB2
- ecCodes
- 10 m U/V
- 4 近鄰格點 IDW
- 6 小時預報間 U/V 時間插值
- model cycle 換輪 fallback

新增：
- 讀取 GitHub 固定 `current.gpx`
- GPX 里程與局部騎乘方向
- 出發時間 + 平均速度 → 每個路段 ETA
- 一次批次讀取需要的 GRIB forecast-hour，避免每 10 km 重掃一次檔案
- 每個採樣點計算順風 / 逆風 / 側風
- 整條路摘要（最大逆風、最大順風、降級點數、不可用點數）

## 路線來源

預設直接共用你現有的：

```text
https://raw.githubusercontent.com/harrypotter0618/cwa-wind/main/ride-api/routes/current.gpx
```

所以即時觀測 API 與 WRF3KM 預報 API 使用同一份 `current.gpx`。

Render Environment Variable：

```text
CURRENT_ROUTE_URL=https://raw.githubusercontent.com/harrypotter0618/cwa-wind/main/ride-api/routes/current.gpx
ROUTE_CACHE_SECONDS=300
```

## 更新部署

把 V0.2 的檔案覆蓋 GitHub：

```text
wrf3km-api/
├─ app.py
├─ Dockerfile
├─ requirements.txt
├─ render.yaml
└─ README_部署說明.md
```

Commit 後等同一個 `wrf3km-api` Render Service 自動部署。
不用建立新的 Web Service。

## 測試

### 1. Health

```text
/health
```

應看到：

```json
"version": "0.2.0"
```

### 2. Route

```text
/route
```

會回：
- current.gpx 總長
- GPX 點數
- 路線來源

### 3. 整條路線預報

現在出發、平均 25 km/h、每 10 km 一點：

```text
/route-forecast?speed_kmh=25&step_km=10
```

指定出發時間（未寫 timezone 時視為台灣時間）：

```text
/route-forecast?departure=2026-09-16T04:00:00&speed_kmh=25&step_km=10
```

如果 URL 中使用 `+08:00`，`+` 建議 URL encode 成 `%2B`：

```text
/route-forecast?departure=2026-09-16T04:00:00%2B08:00&speed_kmh=25&step_km=10
```

只預報一段，例如 100–200 km：

```text
/route-forecast?departure=2026-09-16T04:00:00&speed_kmh=25&step_km=10&start_km=100&end_km=200
```

## 每個 sample 回傳

- `km`
- `lat`, `lon`
- `heading_deg`
- `eta_taipei`
- `wind_speed_mps`
- `wind_direction_deg`
- `wind_direction_text`
- `headwind_mps`
- `tailwind_mps`
- `crosswind_mps`
- `wind_effect`
- `source_hours_used`
- `degraded`
- `nearest_model_point_distance_km`

## 效能設計

整條 360 km 若 `step_km=10` 約 37 個採樣點。

V0.2 不會對 37 個點各自重複打 `/wind`。
它會先算出所有 ETA 所需的 forecast-hour，再讓每個 GRIB 檔只掃一次 U/V，
同一份 U/V 場一次處理全部路線座標。

這是後續做沿線地圖時必要的效能改善。

## 下一步

V0.2 API 測通後，可直接做前端：
- 上方輸入出發時間 / 平均速度
- GPX 地圖
- 每 10/20/30 km 風箭頭
- 順風綠、側風橘、逆風紅
- 點測站/路段顯示 ETA 與 WRF3KM 預報

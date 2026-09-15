# WRF3KM Render V0.2.3 — 格點索引快取版

這版專門處理 V0.2.2 實測確認的最大瓶頸。

## V0.2.2 實測瓶頸

compact U/V GRIB 已經成功把檔案從約 170 MB 縮到約 4.46 MB，
但 6 個路線點每個 forecast hour 仍需約 28 秒。

原因不是檔案大小，而是：

```text
每個座標 × U/V × 每個 forecast hour
→ 重複 codes_grib_find_nearest()
```

## V0.2.3 新流程

第一次遇到一組 GPX 取樣座標：

```text
GPX 座標
  ↓
用 FH000 U10 找 4 個最近 WRF 格點
  ↓
保存：
- grid index
- 格點經緯度
- 距離
- IDW 權重
```

之後：

```text
FH000 / FH006 / FH012 / ...
        ↓
直接 codes_get_array("values")
        ↓
用已保存的 4 個 index + 權重取 U/V
```

所以：

- U / V 共用同一組格點索引
- 不同 forecast hour 共用
- 改出發時間共用
- 改均速共用
- 同一 `start_km / end_km / step_km` 共用
- `step_km=20` 若落在已計算過的 10 km 點，也可直接命中既有座標快取

## 準確度

計算方式沒有改：

- 同一份 CWA WRF-3KM
- 同一個 10 m U/V
- 同樣 4 個最近格點
- 同樣 inverse-distance-squared 權重
- 同樣 6 小時 U/V 時間插值
- 同樣 GPX heading 與順逆側風計算

V0.2.3 只是把「4 個格點是哪四個、權重多少」記住，
避免每個 forecast hour 重複找。

若 grid fingerprint 或 index 出現異常，
程式會自動 fallback 回 V0.2.2 的 `codes_grib_find_nearest()`，
優先保證結果正確。

## 另外修正

### 1. U/V cache 更合理

FH000 仍以 `UV_CACHE_TTL_SECONDS` 定期重新確認新 model cycle。

但 FH006 / FH012 / FH018 等非 0 小時檔：
- 不再固定每 20 分鐘失效
- 保存到 FH000 偵測出新 model cycle 為止
- cycle 不一致時才重建

因此同一輪模式重算不會一直重新下載大型 GRIB。

### 2. 修正 `/` 隱藏錯誤

V0.2.2 有一段完成診斷訊息誤放進 `/` root handler。
V0.2.3 已移回 `/route-forecast` 正確位置。

### 3. 保留 V0.2.2 摘要修正

整段沒有逆風時：

```json
"max_headwind": null
```

不會再顯示無意義的 0.0 m/s。

## 部署

直接用 V0.2.3 覆蓋 GitHub：

```text
wrf3km-api/
├─ app.py
├─ Dockerfile
├─ requirements.txt
├─ render.yaml
└─ README_部署說明.md
```

不用建立新的 Render Web Service。

## 測試

### Health

```text
/health
```

應看到：

```json
"version": "0.2.3",
"grid_index_cache": true
```

### 前 50 km

```text
/route-forecast?speed_kmh=25&step_km=10&start_km=0&end_km=50
```

第一次應看到：

```text
[GRIDMAP] coords=6 hit=0 miss=6
[GRIDMAP] built ...
[ECCODES_FAST] read ...
```

第二次改均速：

```text
/route-forecast?speed_kmh=26&step_km=10&start_km=0&end_km=50
```

應看到：

```text
[GRIDMAP] coords=6 hit=6 miss=0
[UV_CACHE] hit ...
[ECCODES_FAST] read done ... 
```

理想狀況下第二次應比 V0.2.2 的每 FH 約 28 秒快非常多。

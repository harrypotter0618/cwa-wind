# WRF3KM Render V0.1

獨立的 Render Web Service，專門把中央氣象署 WRF-3KM GRIB2 轉成簡單的 JSON 風場 API。

## GitHub 位置

建議放在原本 repo：

```text
cwa-wind/
├─ ride-api/       # 原本即時觀測 API，不要動
└─ wrf3km-api/     # 把本資料夾內容放這裡
   ├─ app.py
   ├─ Dockerfile
   ├─ requirements.txt
   └─ render.yaml
```

## Render 新增服務

建立「新的」Web Service，不要覆蓋 cwa-ride-api。

建議設定：

```text
Name: wrf3km-api
Repository: harrypotter0618/cwa-wind
Branch: main
Root Directory: wrf3km-api
Runtime: Docker
Health Check Path: /health
```

Environment Variables：

```text
CWA_API_KEY = 你的 CWA API key
CORS_ORIGINS = *
CACHE_TTL_SECONDS = 1200
MAX_CACHE_MB = 450
```

CWA_API_KEY 在 V0.1 是「備援下載」使用。
程式會先嘗試官方資料集提供的 CWA S3 GRIB2 URI，失敗時才改用 fileapi。

## 部署後依序測試

### 1. Health

```text
https://wrf3km-api.onrender.com/health
```

成功應看到：

```json
{
  "ok": true,
  "service": "wrf3km-api",
  "version": "0.1.0",
  "model": "CWA WRF-3KM"
}
```

這一步不下載大型 GRIB2，只確認 Render + Python + ecCodes 有正常啟動。

### 2. Forecast range

```text
https://wrf3km-api.onrender.com/range
```

第一次會下載 M-A0064-000.grb2，所以會比 /health 慢。

成功後會看到：
- 最新 model initial time
- 可預報起始時間
- 最後有效時間
- 0, 6, 12 ... 84 小時

### 3. Wind

例如台中：

```text
https://wrf3km-api.onrender.com/wind?lat=24.15&lon=120.68
```

沒有指定時間時，以「目前時間」為目標預報時間。

也可以指定台灣時間：

```text
https://wrf3km-api.onrender.com/wind?lat=24.15&lon=120.68&time=2026-09-16T12:00:00%2B08:00
```

或 Unix epoch：

```text
/wind?lat=24.15&lon=120.68&time_epoch=1789521600
```

## V0.1 已做的事

- CWA WRF-3KM GRIB2 下載與快取
- ecCodes 解析
- 尋找 10 m U/V wind
- 空間：4 個最近格點 IDW 加權
- 時間：相鄰 6 小時預報 U/V 線性插值
- U/V → 風速 + 氣象風向（FROM）
- 模式切換期間偵測不同 cycle，避免混用兩次模式結果
- CORS，可供 GitHub Pages 前端呼叫
- 快取容量限制，避免 Render 暫存空間一直堆積

## 目前刻意還沒做

V0.1 先驗證「Render 能否穩定下載 + 解析 WRF3KM」。

等 /range 與 /wind 都通過，再做下一版：
1. GPX + 出發時間 + ETA
2. 沿路每 10/20/30 km 取樣
3. 回傳整條路的風向、風速、順逆風
4. 接到既有的 WRF3KM 預報地圖

## 常見錯誤

### UV10_NOT_FOUND
GRIB2 的 10 m U/V 欄位命名與預期不同。把 Render log 貼回來即可再修欄位判讀。

### CWA_DOWNLOAD_FAILED
官方 S3 與 fileapi 都無法取得資料。檢查 CWA_API_KEY，以及 Render log 的 HTTP 回應。

### MODEL_FILES_NOT_SYNCHRONIZED
中央氣象署正在更新 6 小時一輪的新模式資料，不同 forecast-hour 檔案暫時屬於不同 cycle。
這時不要硬混資料，幾分鐘後再試。

### Render 第一次很慢
正常。GRIB2 比你現在 cwa-ride-api 使用的 JSON 大很多，而且 Free service 冷啟動後還要重新下載暫存資料。

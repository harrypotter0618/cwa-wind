# CWA Ride API V0.2

## 核心改動
- route_id 固定為 `current`
- 正式路線固定放在 GitHub：
  `ride-api/routes/current.gpx`
- 換路線時只需要覆蓋同名 `current.gpx`
- iPhone 捷徑不用再改 route_id
- Render 重啟後會重新從 GitHub 取得 current.gpx

## 升級方式
1. 用 V0.2 的 `server.py`、`Dockerfile`、`render.yaml` 覆蓋 GitHub `ride-api/` 舊檔。
2. 建立：
   `ride-api/routes/`
3. 將目前要使用的 GPX 改名：
   `current.gpx`
4. 上傳到：
   `ride-api/routes/current.gpx`
5. Commit。
6. Render 會自動部署。

## 測試
- `/health`
- `/route`

正常時 `/health` 應出現：
- version = 0.2.0
- current_route_available = true
- current_route_source = github

## iPhone 捷徑固定網址
`https://cwa-ride-api.onrender.com/status?route_id=current&lat=[緯度]&lon=[經度]`

## 更換路線
只需在 GitHub 用新的 GPX 覆蓋：
`ride-api/routes/current.gpx`

不用再修改捷徑。

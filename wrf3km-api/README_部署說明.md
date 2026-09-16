# WRF3KM API V0.2.6

## 不變
- `GET /route-forecast` 仍使用 GitHub 的 `ride-api/routes/current.gpx`
- `current.gpx` 仍是三套系統共用的正式路線
- 不修改即時地圖與 Siri 即時語音

## 新增
- `POST /route-forecast-upload`
- 可暫時上傳 `.gpx` 做一次預報
- 上傳 GPX 只在本次 request 記憶體中解析
- 不寫入 GitHub、不覆蓋 `current.gpx`
- 預設上限 12 MB
- CORS 支援 POST
- requirements 新增 `python-multipart`

部署後 `/health` 應看到：
- `"version":"0.2.6"`
- `"temporary_gpx_upload":true`

固定路線仍使用：
`GET /route-forecast`

臨時路線由 V0.3.1 前端使用：
`POST /route-forecast-upload`

# 風況 API V0.1

用途：讓 iPhone 捷徑在鎖屏時取得目前位置後，呼叫 API 並朗讀 `speech`。

## 端點
- `GET /health`
- `POST /routes`：上傳 GPX，取得 route_id
- `GET /status?route_id=...&lat=...&lon=...`

## 本機測試
1. 安裝 Python 3.12
2. `pip install -r requirements.txt`
3. 設定環境變數 `CWA_API_KEY`
4. `uvicorn server:app --host 0.0.0.0 --port 8000`
5. 開啟 `http://127.0.0.1:8000/docs`

## 上傳 GPX
在 `/docs` 展開 `POST /routes` → Try it out → 選 GPX → Execute。
記下回傳的 `route_id`。

## 查詢
`/status?route_id=你的route_id&lat=24.1048&lon=120.6058`

回傳 JSON 裡的 `speech` 就是 iPhone 要朗讀的文字。

## Render 部署
- 將本資料夾放進 GitHub repo
- Render 建立 Docker Web Service
- Environment Variables 新增 `CWA_API_KEY`
- 部署完成後，用 HTTPS 網址測 `/health`

注意：V0.1 的 GPX 暫存在伺服器本機 `routes/`。某些免費雲端重建後可能消失，這版先用來驗證「iPhone 捷徑 → API → 語音」流程。

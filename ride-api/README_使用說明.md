# CWA Ride API V0.3 — 沿 GPX 路段挑代表測站

V0.3 不再單純取 GPS 最近 5 站，而是優先挑目前 GPX 路段附近的代表站。

預設：
- 後方 5 km ～ 前方 10 km
- 測站離該路段 <= 8 km
- 最多 5 站
- 若不足 3 站，才用 GPS 12 km 內測站補足

每個測站的順/逆/側風分量，使用該測站對應的 GPX 局部 heading 計算，
適合彎曲路線，避免用單一 heading 或平均 heading 造成誤判。

升級方式：
1. 用 V0.3 的 server.py、Dockerfile、render.yaml 覆蓋 GitHub ride-api/ 舊檔。
2. current.gpx 不用更換。
3. Commit 後等 Render Auto-Deploy。
4. 測 /health，應看到 version 0.3.0 與 station_selection route-aware。

iPhone 捷徑不用改：
https://cwa-ride-api.onrender.com/status?route_id=current&lat=[緯度]&lon=[經度]

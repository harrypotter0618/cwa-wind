# WRF3KM Render V0.2.5 — 風況文字分類小修

這版只修改 `wind_effect()` 的文字分類邏輯，不改：
- CWA WRF-3KM 資料來源
- 10 m U/V
- 4 格點 IDW
- 時間插值
- GRIDMAP
- ECCODES_FAST
- U/V cache
- ETA 計算

因此數值準確度與 V0.2.4 相同。

## 新分類

使用「風從哪裡吹來」與「騎乘 heading」的相對角度：

- 0–30°：逆風
- 30–84°：側逆風
- 84–96°：側風
- 96–150°：側順風
- 150–180°：順風

其中：
- 0° = 正面逆風
- 90° = 純側風
- 180° = 正後方順風

### 例子
原本 KM130：
- heading 295.3°
- wind FROM 17.5°
- headwind 1.316 m/s
- crosswind 9.573 m/s

舊版會標「逆風」。
V0.2.5 會標成「側逆風」。

JSON 另外新增：
`relative_wind_angle_deg`

方便之後 UI 判斷與除錯。

## 部署後測試

原本網址直接重跑：
`/route-forecast?speed_kmh=25&step_km=10&start_km=0&end_km=352`

熱快取速度應與 V0.2.4 幾乎相同。

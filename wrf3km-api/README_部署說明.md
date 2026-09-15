# WRF3KM Render V0.2.3 FIXED

本版是真正的格點索引快取版。

重點：
- 新增 codes_get_array
- 新增 GRIDMAP_CACHE
- 每個 GPX 取樣座標只建立一次 4 格點 index + IDW 權重
- U/V 與 FH000/FH006/FH012... 共用相同格點 mapping
- 每個 forecast hour 直接讀 values array，不再重跑 nearest search
- 若 indexed fast path 異常，自動 fallback 到 V0.2.2 nearest search
- FH000 以 TTL 檢查新模式輪次；非 0 小時 U/V cache 保存到 model cycle 改變
- 修正舊版 root handler 引用未定義 output/available/req_t0 的問題

部署後 /health 必須看到：
- version: 0.2.3
- grid_index_cache: true

前 50 km 第一次測試應看到：
[GRIDMAP] ... miss=6
[GRIDMAP] built ...
[ECCODES_FAST] read done ...

同樣座標第二次測試應看到：
[GRIDMAP] ... hit=6 miss=0
[ECCODES_FAST] read done ...

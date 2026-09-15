# WRF3KM Render V0.2.4 — 併發安全修正版

V0.2.3 的 GRIDMAP / ECCODES_FAST 已成功生效，但實測同時有兩個
`/route-forecast` 請求時，兩個 thread 會同時建立同一個
`M-A0064-006_uv10.grb2.tmp`，造成：

- 一個 request 把 tmp replace 掉
- 另一個 request 再 replace 時找不到 tmp
- 或兩邊同時 truncate/write，產生 0 MB compact GRIB
- 最後 500 Internal Server Error

V0.2.4 修正：

1. 每個 forecast hour 有自己的 build lock。
2. 等 lock 後重新檢查 cache；前一個 request 建好後，後一個直接 hit-after-wait。
3. 每次建立使用唯一 tmp 檔名（pid + thread id + timestamp）。
4. compact GRIB 小於 100 KB 一律視為損壞，不允許 cache hit。
5. data/meta 都用 atomic replace。
6. 保留 V0.2.3 的 GRIDMAP index cache 與 ECCODES_FAST。
7. 計算公式與準確度不變。

成功時可能看到：

[UV_CACHE] waiting build lock fh=006
[UV_CACHE] built fh=006 ...
[UV_CACHE] hit-after-wait fh=006 ...
[ECCODES_FAST] read done ...

部署後先測：
/health

再測：
/route-forecast?speed_kmh=25&step_km=10&start_km=0&end_km=50

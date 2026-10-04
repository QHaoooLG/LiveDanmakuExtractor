# assets/

打包与界面用的**生成型资源**。入库的是产物（不是源图），这样打包时不必依赖
Pillow 也能拿到图标。

| 文件 | 来源 | 用途 |
|------|------|------|
| `app.ico` | `tools/make_icon.py`（源图见下） | exe 图标（`--icon`）与托盘图标（`LoadImageW`）共用 |

## 重新生成

```powershell
.\.venv\Scripts\python.exe tools\make_icon.py          # 生成 + 校验尺寸
.\.venv\Scripts\python.exe tools\make_icon.py --check  # 只校验
```

源图：`pics/B站弹幕提取姬_团子形象2.png`（1060×1034，RGBA，四角全透明）。
**换图标**：改 `tools/make_icon.py` 里的 `DEFAULT_SOURCE` → 重新生成 → 重新打包 exe
（托盘图标读的是同一个 `app.ico`，所以一次替换两处都生效）。

## 为什么是 `.ico` 而不是直接用 PNG

* Windows 的 **exe 图标只吃 `.ico`**（`--icon` 不接受 png）；
* 托盘图标用 `LoadImageW` + `LR_LOADFROMFILE` 读 ico 最省事，也便于按 DPI 选档；
* 源图 1060×1034 直接缩到 16×16 会糊，所以**预生成多档尺寸**
  （16/20/24/32/40/48/64/128/256），让 Windows 自己挑合适的那档。

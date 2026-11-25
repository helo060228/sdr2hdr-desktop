# 算法与输出规格

## 处理流程

SDR 转 HDR10 提供三种独立管线（显卡有损编码默认经 Vulkan + `libplacebo` GLSL GPU 着色器执行；CPU / 无损编码经 `zscale` + LUT 浮点管线执行）：

- **影院均衡高光（`balanced`，1000 nit，默认）**：解析输入颜色信息 → `Gamma 2.2` 线性解码 → `BT.709→BT.2020` 线性色域变换 → 零凹陷通透暗部与 1000 nit 高光余量曲线 → PQ / BT.2020 → 10-bit YUV 4:2:0 → HEVC / MP4·MKV（`MaxCLL = 1000 / MaxFALL = 260`）。
- **柔和暗室保护（`natural`，450 nit）**：解析输入颜色信息 → `BT.1886` 线性解码 → `BT.709→BT.2020` 线性色域变换 → 保守等色度高光保护曲线（峰值 `450 nits`）→ PQ / BT.2020 → 10-bit YUV 4:2:0 → HEVC / MP4（`MaxCLL = 450 / MaxFALL = 200`）。
- **高对比风格（`vivid`，原版）**：解析输入颜色信息 → 浮点非线性 RGB → 色差增益（`U/V × 0.56`）→ 65,536 点一维对数曲线 / 等价着色器 → PQ / BT.2020 → 10-bit YUV 4:2:0 → HEVC / MP4（`MaxCLL = 1000 / MaxFALL = 1000`）。

三者均为确定性数学规则，不包含场景识别或凭空生成原片已丢失的高光细节。

## 颜色与亮度

### 输入解析

读取输入视频的颜色矩阵、传递函数、原色和量化范围，按输入信息显式完成 YUV / RGB 转换。当前接受逐行扫描 BT.709 / BT.601 SDR；缺失颜色标记时会使用推定值并记录在 `.hdr.json` 转换计划中。

### 风格一：影院均衡高光（`balanced`，默认，推荐电影·1000 nit）

兼顾纯黑不泛灰、通透暗部细节、自然中间调亮度、清晰云层／雪地亮部层次、肤色不发白过曝与 1000 nits 极亮高光冲击力：

1. **线性解码与色域矩阵**：按 **Gamma 2.2**（贴合 PC 与客厅 SDR 观感，避免全屏 HDR 分区控光压黑暗部细节）解码至相对线性光并使用对应原色矩阵变换到线性 BT.2020 `(R_2020, G_2020, B_2020)`，保持零色度偏移。
2. **零凹陷单调 1000 nit 高光曲线**：按 BT.2020 相对亮度 `Y = 0.2627·R_2020 + 0.6780·G_2020 + 0.0593·B_2020` 计算目标尼特亮度（漫反射基准 `235 nits`、拐点 `Y_k = 0.58`、指数 `2.5`、峰值 `1000 nits`，一阶导数全程连续且无高亮对比度凹陷）：
   ```text
   L(Y) = 235 · Y                                       ,  0 ≤ Y ≤ 0.58
   L(Y) = 235 · Y + 765 · ((Y - 0.58) / 0.42)^2.5       ,  0.58 < Y ≤ 1.0
   ```
   对三通道施加**同一亮度增益** `gain = L(Y) / Y`（峰值按 `1000 / max(R, G, B)` 等比例限幅）。纯黑锚定 `0.00 nits`，暗木与室内阴影（`v = 0.05～0.20`）位于 `0.32～6.82 nits` 贴齐 Gamma 2.2，受光肤色稳在约 `174 / 1000 nits`（5.7 倍 PQ 余量），云层与雪地高亮段（`v = 0.80～0.90`）`dPQ/dv` 保持饱满陡峭，极亮灯光平滑冲至 `1000 nits`。
3. **执行路径与元数据**：GPU 路径由 `//!HOOK MAIN` GLSL 着色器直接解析计算；CPU / 无损路径烘焙为缓存的 `49³` `.cube` 3D LUT 并经 `lut3d=interp=tetrahedral` 与 `spline36 + error_diffusion` 输出有限范围 `yuv420p10le`，写入 **`MaxCLL = 1000 / MaxFALL = 260`**。

### 风格二：柔和暗室保护（`natural`，450 nit）

为全黑暗室观影提供更保守的高光保护，漫反射基准 `190 nits`，拐点 `Y_k = 0.62`：

1. **输入标准化与线性解码**：按 **BT.1886（Gamma 2.4）** 解码至线性光并映射至 BT.2020 `(R_2020, G_2020, B_2020)`：
   ```text
   R_lin, G_lin, B_lin = clamp(R, 0, 1)^2.4, clamp(G, 0, 1)^2.4, clamp(B, 0, 1)^2.4
   ```
2. **等色度亮度高光保护曲线**：按 BT.2020 相对亮度 `Y` 计算目标尼特亮度（峰值上限 `450 nits`）：
   ```text
   L(Y) = 190 · Y                                       ,  0 ≤ Y ≤ 0.62
   L(Y) = 190 · Y + 260 · ((Y - 0.62) / 0.38)^2         ,  0.62 < Y ≤ 1.0
   ```
3. **执行路径与元数据**：CPU / 无损路径采用 `49³` 四面体 `lut3d` 与 `spline36 + error_diffusion`；GPU 路径由 `//!HOOK MAIN` 着色器直接计算。写入 **`MaxCLL = 450 / MaxFALL = 200`**。

### 风格三：高对比风格（`vivid`，原版，适合动漫与暗景）

保留 v1.4 及以前版本的非线性风格化管线：

- **色差增益**：在非线性浮点 RGB 中对居中 U/V 色差应用增益 `0.56`（合并为 3×3 `colorchannelmixer` 浮点矩阵）。
- **固定对数曲线**：每个通道使用 65,536 点一维曲线线性插值：
  ```text
  q(x) = 0.65 × ln(1 + 5 × max(x, 0)) / ln(6)
  ```
  上限截断在 1000 尼特对应 PQ 码值；中性白 `x = 1` 得到 `q = 0.65`（约 390.5 尼特）。
- **输出与元数据**：按 BT.2020 / PQ 解释并以 `bilinear + error_diffusion` 转为有限范围 `yuv420p10le`，写入 `MaxCLL / MaxFALL = 1000 / 1000` 预设上界标记。它不是以保持 SDR 色度学外观为目标的母版转换，暗部更沉、亮部更艳丽。

## 杜比视界（DV）转 HDR10

工具自动识别杜比视界 Profile，并区分两种处理路径：

### 1. 无损直剥（Profile 8.1 / Profile 7）

- 当底层轨道为 HEVC 10-bit `yuv420p10le`、PQ（`smpte2084`）、BT.2020 原色（`bt2020nc` 或部分蓝光 Remux 缺失的未标注矩阵）且保持原尺寸时启用。
- Profile 8.1 采用 `-c:v copy -bsf:v dovi_rpu=strip=1 -tag:v hvc1`；Profile 7（含单轨 EL NAL 63 与双轨蓝光 Remux）采用 `-c:v copy -bsf:v dovi_split=mode=bl,dovi_rpu=strip=1 -tag:v hvc1`（旧版 FFmpeg 回退 `filter_units=remove_types=63,dovi_rpu=strip=1`）。
- 导出校验强制确认输出容器与首帧均无任何残留的 `DOVI` / `Dolby Vision` 元数据或 `dvhe` / `dvh1` 标签；对于原片基础层未携带 `MaxCLL/MaxFALL` 或矩阵标记的蓝光片源不作误报拦截。

### 2. 色彩还原重编码（Profile 5 / Profile 8.4 及其他无 HDR10 兼容层 DV）

- 通过 `libplacebo` 执行杜比视界 RPU 重塑与色彩空间映射（有损模式支持 Vulkan 硬解 `-hwaccel vulkan -hwaccel_output_format vulkan` 零拷贝直入 `libplacebo`，实测与 CPU 软解逐像素一致）：
  `libplacebo=w=<W>:h=<H>:format=<pix_fmt>:colorspace=bt2020nc:color_primaries=bt2020:color_trc=smpte2084:range=tv:apply_dolbyvision=true:peak_detect=false:tonemapping=clip:gamut_mode=clip:dithering=ordered_fixed,setsar=1`
- 禁用动态峰值检测（`peak_detect=false`）并采用 `tonemapping=clip:gamut_mode=clip`，在保持原始 HDR 动态范围的同时避免逐帧亮度呼吸效应。
- 显卡有损编码直接让 `libplacebo` 输出 `p010le` 以减少 CPU 格式转换瓶颈；CPU 编码与无损档使用确定性的 `yuv420p10le` 输出并通过逐帧 SHA-256 校验。
- 静态 HDR10 元数据优先参考首帧 RPU 的 `source_max_pq`（映射到 1000～4000 尼特），并在编码输出端附加 `-bsf:v dovi_rpu=strip=1` 避免残留杜比视界头信息。

## 编码策略

支持 CPU 软件编码（`libx265`）与三种 Windows 显卡硬件编码器（`hevc_nvenc`、`hevc_qsv`、`hevc_amf`），默认按 `nvenc > qsv > amf > cpu` 自动选用通过自检的编码器。

### CPU 软件编码（`libx265`）

- 快速：`ultrafast`，CRF 21。
- 标准：`fast`，CRF 19。
- 精细：`medium`，CRF 18。
- 画面无损：`lossless=1`（`fast` 预设），不传 CRF 参数。
- 关键帧间隔统一为 `4 秒`（`-g round(fps * 4)`），HDR10 颜色标记与静态元数据通过 `-x265-params` 写入码流。

### 显卡硬件编码（10-bit Main 10 / `p010le`）

- **NVIDIA NVENC（`hevc_nvenc`）**：快速 `p2 / VBR CQ 25`；标准 `p5 / VBR CQ 23`；精细默认单遍 `p6 / VBR CQ 21 / spatial-aq`（启用 `--multipass` 时切换 `p7 / multipass qres`）；画面无损 `-preset p4 -tune lossless`（若本地显卡自检通过）。
- **Intel QSV（`hevc_qsv`）**：针对 10-bit HDR10 校准为快速 `faster / ICQ 23`、标准 `medium / ICQ 21`、精细 `slow / ICQ 19`，并在运行时自检通过时自动启用 `-extbrc 1 -mbbrc 1 -adaptive_i 1 -adaptive_b 1`（不支持无损档；不兼容 ExtBRC 的驱动自动回退纯 ICQ）。
- **AMD AMF（`hevc_amf`）**：启用 `-bitdepth 10`，快速 `speed / CQP 25`；标准 `balanced / CQP 23`；精细 `quality / CQP 21`（不支持无损档）。

### 滤镜衔接、GPU 色彩加速与元数据传播

- **默认路径**（`--gpu-filter auto`）优先使用通过自检的 Vulkan/libplacebo GPU 滤镜。输入或设备不支持时回退 SIMD 色彩处理。自动模式按逻辑处理器数为软件解码和滤镜分别分配 1～4 个线程。
- `--gpu-filter simd` 显式启用多线程 zscale、LUT、ordered 抖动和 p010le 输出，交由显卡编码。`--gpu-filter on` 保留现有 Vulkan 请求语义。HDR 色彩着色器支持的输入与硬解能力分别检查，H.264 使用软件解码。
- **精确参考浮点路径（CPU 编码 / 画面无损 / `--gpu-filter off`）**：使用 `zscale`（`spline36 + error_diffusion`）+ LUT 输出确定性 `yuv420p10le`（硬件无损档追加 `,format=p010le`），保证逐帧 SHA-256 校验 100% 确定一致。
- 硬件编码通过输入流选项 `-mastering_display:<stream>` 与 `-content_light:<stream>` 传入 HDR10 静态元数据（放在 `-i` 之前），并配合输出 VUI 标记（`bt2020` / `smpte2084` / `bt2020nc` / `tv`）和 `hvc1` 标签。
- 硬件编码限用于 `160×128` 至 `8192×8192` 的输出分辨率；超出范围或显卡不支持无损时，“自动”模式会回退到 `libx265`。

前三档根据画质目标分配码率，输出大小取决于内容复杂度。无损档保留进入编码器的 10-bit 4:2:0 像素，可能产生很大的码率。

无损校验重新计算滤镜输出，再解码生成文件，对全片逐帧比较字节数和 SHA-256 摘要。它只验证编码阶段，不声称颜色映射、重采样和量化过程可逆。

## 双声道空间效果

空间效果组合使用：

- 声道差分增强，系数 `1.35`。
- `12 ms` 交叉延迟，配合较小的反馈与交叉混合。
- 增益调整与峰值限幅，避免处理产生的过载。
- 输出 48 kHz 双声道 AAC。

这是双声道声场处理，不包含 HRTF 个性化建模、离散多声道编码或物体音频元数据。

## 边界

原片已经剪裁或压缩丢失的内容不会被恢复。增加输出位深不等于增加原始拍摄信息。显示端与平台后续处理也不属于本工具的像素校验范围。

参考：[ITU-R BT.2100](https://www.itu.int/rec/R-REC-BT.2100)、[FFmpeg 滤镜文档](https://ffmpeg.org/ffmpeg-filters.html)、[x265 编码说明](https://x265.readthedocs.io/en/stable/cli.html)。

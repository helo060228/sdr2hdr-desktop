# 算法与输出规格

## 处理流程

SDR 转 HDR10 提供三种独立管线（显卡有损编码默认经 Vulkan + `libplacebo` GLSL GPU 着色器执行；CPU / 无损编码经 `zscale` + LUT 浮点管线执行）：

- **影院均衡高光（`balanced`，750 nit，默认）**：解析输入颜色信息 → `BT.1886` 线性解码 → `BT.709→BT.2020` 线性色域变换 → 深黑位 + 中间调平滑抬升 + 高光余量曲线（峰值 `750 nits`）→ PQ / BT.2020 → 10-bit YUV 4:2:0 → HEVC / MP4（`MaxCLL = 750 / MaxFALL = 260`）。
- **柔和暗室保护（`natural`，450 nit）**：解析输入颜色信息 → `BT.1886` 线性解码 → `BT.709→BT.2020` 线性色域变换 → 保守等色度高光保护曲线（峰值 `450 nits`）→ PQ / BT.2020 → 10-bit YUV 4:2:0 → HEVC / MP4（`MaxCLL = 450 / MaxFALL = 200`）。
- **高对比风格（`vivid`，原版）**：解析输入颜色信息 → 浮点非线性 RGB → 色差增益（`U/V × 0.56`）→ 65,536 点一维对数曲线 / 等价着色器 → PQ / BT.2020 → 10-bit YUV 4:2:0 → HEVC / MP4（`MaxCLL = 1000 / MaxFALL = 1000`）。

三者均为确定性数学规则，不包含场景识别或凭空生成原片已丢失的高光细节。

## 颜色与亮度

### 输入解析

读取输入视频的颜色矩阵、传递函数、原色和量化范围，按输入信息显式完成 YUV / RGB 转换。当前接受逐行扫描 BT.709 / BT.601 SDR；缺失颜色标记时会使用推定值并记录在 `.hdr.json` 转换计划中。

### 风格一：影院均衡高光（`balanced`，默认，推荐电影·750 nit）

兼顾深邃暗部黑位、饱满中间调亮度、肤色纹理不发白过曝与 750 nits 极亮高光冲击力：

1. **线性解码与色域矩阵**：按 **BT.1886（Gamma 2.4）** 解码至相对线性光并使用对应原色矩阵变换到线性 BT.2020 `(R_2020, G_2020, B_2020)`，保持零色度偏移。
2. **深黑锚定 + 中间调恢复 + 750 nit 高光余量曲线**：按 BT.2020 相对亮度 `Y = 0.2627·R_2020 + 0.6780·G_2020 + 0.0593·B_2020` 计算目标尼特亮度（暗部斜坡 `0.08`、漫反射基准 `255 nits`、中间调平滑增益 `0.22`、拐点 `Y_k = 0.68`）：
   ```text
   s        = clamp(Y / 0.08, 0, 1)
   smooth_s = s^2 · (3 - 2s)
   L_diff(Y)= 255 · Y · (1 + 0.88 · Y · (1 - Y) · smooth_s)
   L(Y)     = L_diff(Y)                                       ,  0 ≤ Y ≤ 0.68
   L(Y)     = L_diff(Y) + 495 · ((Y - 0.68) / 0.32)^2.3       ,  0.68 < Y < 1.0
   ```
   对三通道施加**同一亮度增益** `gain = L(Y) / Y`（峰值按 `750 / max(R, G, B)` 等比例限幅）。极暗部受 `smooth_s` 锚定停在约 `1.3 nits`，灰衬衫中间调回升至约 `71 nits`，受光额头稳在约 `220 / 750 nits`（PQ 余量大于旧版 3 倍），极亮灯光平滑冲至 `750 nits`。
3. **执行路径与元数据**：GPU 路径由 `//!HOOK MAIN` GLSL 着色器直接解析计算；CPU / 无损路径烘焙为缓存的 `49³` `.cube` 3D LUT 并经 `lut3d=interp=tetrahedral` 与 `spline36 + error_diffusion` 输出有限范围 `yuv420p10le`，写入 **`MaxCLL = 750 / MaxFALL = 260`**。

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

- 快速：`ultrafast`，CRF 17。
- 标准：`fast`，CRF 17。
- 精细：`medium`，CRF 16。
- 画面无损：`lossless=1`（`fast` 预设），不传 CRF 参数。
- HDR10 颜色标记与静态元数据通过 `-x265-params` 写入码流。

### 显卡硬件编码（10-bit Main 10 / `p010le`）

- **NVIDIA NVENC（`hevc_nvenc`）**：快速 `p2 / VBR CQ 22`；标准 `p5 / VBR CQ 20`；精细默认单遍 `p6 / VBR CQ 18 / spatial-aq`（启用 `--multipass` 时切换 `p7 / multipass qres`）；画面无损 `-preset p4 -tune lossless`（若本地显卡自检通过）。
- **Intel QSV（`hevc_qsv`）**：针对 10-bit HDR10 校准为快速 `faster / ICQ 19`、标准 `medium / ICQ 16`、精细 `slow / ICQ 14`，并在运行时自检通过时自动启用 `-extbrc 1 -mbbrc 1 -adaptive_i 1 -adaptive_b 1`（不支持无损档；不兼容 ExtBRC 的驱动自动回退纯 ICQ）。
- **AMD AMF（`hevc_amf`）**：启用 `-bitdepth 10`，快速 `speed / CQP 22`；标准 `balanced / CQP 20`；精细 `quality / CQP 18`（不支持无损档）。

### 滤镜衔接、GPU 色彩加速与元数据传播

- **GPU 色彩加速路径（默认自动启用）**：在显卡有损编码（`NVENC` / `QSV` / `AMF`）且通过运行时 Vulkan / `libplacebo` 自检时，SDR 转 HDR 的三种风格均通过自包含 `//!HOOK MAIN` GLSL 着色器在 GPU 执行（关闭 `libplacebo` 内置色调映射与传递转换 `color_primaries=auto:color_trc=auto:disable_linear=true`，由着色器直接执行精确的 `balanced` / `natural` BT.1886→BT.2020→PQ 公式或 `vivid` 色差+对数公式，随后在 GPU 内完成 BT.2020nc 转换与 10-bit 抖动）；输入为 H.264 8-bit 或 HEVC / AV1 8/10-bit 且支持 Vulkan Video 时自动启用 `-hwaccel vulkan -hwaccel_output_format vulkan` 零拷贝硬解，实测与 CPU 管线在平场色块误差 `≤ 1 / 1023`。
- **CPU 浮点滤镜回退路径**：当使用 `libx265` 软件编码、选择“画面无损编码”、使用 `--gpu-filter off` 或本地环境无 Vulkan / `libplacebo` 支持时，自动使用 `zscale` + `balanced.cube` / `natural.cube` / `tone.cube` 浮点管线，末尾输出 `yuv420p10le`、`setsar=1`（显卡路径追加 `,format=p010le`）。
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

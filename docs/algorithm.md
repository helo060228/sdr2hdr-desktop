# 算法与输出规格

## 处理流程

解析输入颜色信息 → 浮点非线性 RGB → 色差增益 → 对数曲线 → PQ / BT.2020 → 10-bit YUV 4:2:0 → HEVC / MP4。

这是固定规则的风格化增强，不包含场景识别或高光细节生成。

## 颜色与亮度

### 输入解析

读取颜色矩阵、传递函数、原色和量化范围，按输入信息完成 YUV / RGB 转换。处理使用非线性编码值，避免额外引入一次与设计不符的伽马变换。

当前接受逐行扫描 BT.709 / BT.601 SDR。代码对已知颜色参数进行显式处理；缺失信息会使用推定值并记录在转换计划中。

### 色差增益

对居中的 U/V 色差应用增益 `0.56`。实现以 RGB 矩阵合并 YUV 变换、色差缩放和逆变换，保留浮点精度。

### 固定曲线

每个颜色通道使用：

```text
q(x) = 0.65 × ln(1 + 5 × max(x, 0)) / ln(6)
```

输出限制在零与 1000 尼特对应的 PQ 码值之间。普通中性白 `x = 1` 得到 `q = 0.65`，按 ST 2084 解释约为 390.5 尼特。

为了降低每像素运算量，程序使用 65,536 点一维曲线和线性插值，并保留超白输入的处理范围。

### 输出

处理后的 RGB 数值按 BT.2020 原色、PQ 传递函数解释，再转换为 BT.2020 非恒亮度 YUV。它不是以保持 SDR 色度学外观为目标的母版转换，颜色和对比度会有意发生变化。

输出为有限范围、`yuv420p10le`、HEVC Main 10、MP4 / hvc1。静态元数据采用虚拟 BT.2020 / D65 / 1000 尼特编码目标。

MaxCLL / MaxFALL 的 `1000 / 1000` 是预设上界标记，不是逐片测量结果，也不代表视频每帧均达到该亮度。显示端仍可能进行自身的色调映射。

## 杜比视界（DV）转 HDR10

工具自动识别杜比视界 Profile，并区分两种处理路径：

### 1. 无损直剥（Profile 8.1 / Profile 7）

- 仅在底层轨道为标准 HEVC 10-bit `yuv420p10le`、PQ（`smpte2084`）、BT.2020（`bt2020nc`）且保持原尺寸时启用。
- Profile 8.1 采用 `-c:v copy -bsf:v dovi_rpu=strip=1 -tag:v hvc1`；Profile 7（含单轨 EL NAL 63 与双轨蓝光 Remux）采用 `-c:v copy -bsf:v dovi_split=mode=bl,dovi_rpu=strip=1 -tag:v hvc1`（旧版 FFmpeg 回退 `filter_units=remove_types=63,dovi_rpu=strip=1`）。
- 导出校验强制确认输出容器与首帧均无任何残留的 `DOVI` / `Dolby Vision` 元数据或 `dvhe` / `dvh1` 标签；对于原片基础层未携带 `MaxCLL/MaxFALL` 的蓝光片源不作误报拦截。

### 2. 色彩还原重编码（Profile 5 / Profile 8.4 及其他无 HDR10 兼容层 DV）

- 通过 `libplacebo` 执行杜比视界 RPU 重塑与色彩空间映射：
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

- **NVIDIA NVENC（`hevc_nvenc`）**：快速 `p2 / VBR CQ 22`；标准 `p5 / VBR CQ 20`；精细 `p7 / multipass qres / VBR CQ 18 / spatial-aq`；画面无损 `-preset p4 -tune lossless`（若本地显卡自检通过）。
- **Intel QSV（`hevc_qsv`）**：快速 `faster / ICQ 22`；标准 `medium / ICQ 20`；精细 `slow / ICQ 18`（不支持无损档）。
- **AMD AMF（`hevc_amf`）**：启用 `-bitdepth 10`，快速 `speed / CQP 22`；标准 `balanced / CQP 20`；精细 `quality / CQP 18`（不支持无损档）。

### 滤镜衔接与元数据传播

- 所有编码路径均保持相同的浮点色彩与 `zscale` 处理链，末尾输出 `yuv420p10le`、`setsar=1`。硬件编码路径仅在其后追加 `,format=p010le` 做平面到半平面打包转换，不修改前序抖动与色彩参数。
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

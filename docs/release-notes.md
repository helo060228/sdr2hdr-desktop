# SDR2HDR Desktop v1.5.1

Windows 离线 **SDR / 杜比视界（Dolby Vision）转 HDR10** 工具，支持双风格 SDR 映射（自然高光保护 / 高对比原版）、Vulkan + GPU 色彩加速管线、停止并保留可播放片段、显卡硬件加速与杜比视界无损直剥。

## v1.5.1 本次优化

- **Intel 核显 / 锐炫 (`hevc_qsv`) 10-bit HDR10 画质校准**：针对同等档位下 Intel QSV `ICQ` 刻度偏保守导致文件体积小一半、暗部发糊的实测问题，校准快速 / 标准 / 精细为 **`ICQ 19 / 16 / 14`**，并在驱动自检通过时自动启用 `-extbrc 1 -mbbrc 1 -adaptive_i 1 -adaptive_b 1`（老驱动自动兼容回退）。
- **“NVENC 双遍分析 (Multipass)”改为可选复选框**：桌面端新增开关（命令行 `--multipass`），“精细画质”默认采用高速单遍 `p6 / CQ 18 / spatial-aq`，不再强制双遍拖慢速度；需要极限码率分配时再按需勾选。
- **杜比视界 Profile 5 / 8.4 新增 Vulkan 硬件解码加速**：重编码路径自动启用 `-hwaccel vulkan` 零拷贝硬解直入 `libplacebo` 还原色彩（实测与 CPU 软解逐像素一致），大幅降低 4K 10-bit 片源 CPU 占用并提速。
- **杜比视界 Profile 7 / 8.1 无损直剥判定更宽容**：兼容部分省略容器 `color_space` 标记的蓝光 MKV Remux，确保能走秒级无损直剥而不误走重编码。

## v1.5.0 以来完整特性

- **SDR 转 HDR10“自然高光保护（推荐电影）”默认模式** + 保留“高对比风格（原版·适合动漫）”。
- **Vulkan 硬解 + `libplacebo` GPU 色彩管线加速**，以及 **“停止并保留”优雅封包可播放 `*_已转片段.mp4`**。
- **杜比视界（DV）转 HDR10**：Profile 8.1/7 秒级无损直剥，Profile 5/8.4 色彩还原重编码。

## 下载与使用

下载并解压 Windows ZIP，运行 `SDR2HDR.exe`。FFmpeg 和 ffprobe 需单独准备，可放入程序旁的 `bin` 文件夹。建议使用较新的完整 FFmpeg 构建（包含 `zscale`、`lut1d`、`lut3d`、`libx265`、`dovi_rpu`、`dovi_split`、`libplacebo` 及显卡编码器）。

包内提供 `SHA256SUMS.txt` 供校验。

## 使用范围

- 无论选择“自然高光保护”还是“高对比风格”，都不会凭空恢复原 SDR 片源已经剪裁或压缩丢失的高光细节。
- 仅含单一杜比视界流且无 HDR10 兼容层的 Profile 5 / Profile 8.4 片源必须经 `libplacebo` 色彩还原后重编码，无法像 Profile 8.1 / Profile 7 那样无损直剥；Profile 7 无损直剥保留原生 HDR10 基础层，不烘焙 FEL 增强层的额外亮度增量。
- 画面无损仅指重编码路径中 HDR 处理完成后的 10-bit 4:2:0 视频编码，不保证音频无损，也不保证平台转码后无损。QSV 与 AMF 不支持 10-bit HEVC 无损，“自动”模式会回退 CPU (`libx265`)。
- 视频平台的接收和转码结果需要实际投稿验证。程序不会自动上传或发布视频。

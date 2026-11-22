# SDR2HDR Desktop v1.4.0

Windows 离线 **SDR / 杜比视界（Dolby Vision）转 HDR10** 工具，支持显卡硬件加速与杜比视界无损直剥。

## 本次发布

- **新增杜比视界（DV）转 HDR10**：自动识别 Profile 5 / 7 / 8.1 / 8.4：
  - **Profile 8.1 / Profile 7**：保留原生 10-bit HDR10 基础层并无损剥离 RPU／增强层（`dovi_rpu=strip=1` / `dovi_split=mode=bl`），秒级完成、零重编码画质损失。
  - **Profile 5（偏绿偏紫片源）与 Profile 8.4（iPhone HLG 杜比视界）**：自动通过 `libplacebo` 色彩还原为标准 10-bit PQ / BT.2020，并复用 NVIDIA NVENC / Intel QSV / AMD AMF 显卡硬编或 CPU 导出。
- 导出检查新增 `no_dovi` 验证，确保输出文件不再残留杜比视界配置盒或 RPU 元数据。
- 启动自动检测显卡硬编与 `dovi_rpu` / `libplacebo`（Vulkan）可用性，界面根据输入视频类型自动切换按钮与选项锁定状态。
- 命令行新增 `--dv-mode {auto,strip,reencode}` 与 `--list-dv-tools`。

## 下载与使用

下载并解压 Windows ZIP，运行 `SDR2HDR.exe`。FFmpeg 和 ffprobe 需单独准备，可放入程序旁的 `bin` 文件夹。使用显卡硬编和杜比视界转换时，建议安装较新的完整 FFmpeg 构建（包含 `zscale`、`libx265`、`dovi_rpu`、`dovi_split`、`libplacebo` 及显卡编码器）。

包内提供 `SHA256SUMS.txt` 供校验。

## 使用范围

- 杜比视界 Profile 7 无损直剥保留原生 HDR10 基础层，不烘焙 FEL 增强层的额外亮度增量；Profile 5 / 8.4 转换依赖 `libplacebo` 与支持 Vulkan 的显卡驱动。
- 画面无损仅指重编码路径中 HDR 处理完成后的 10-bit 4:2:0 视频编码，不保证音频无损，也不保证平台转码后无损。QSV 与 AMF 不支持 10-bit HEVC 无损，“自动”模式会回退 CPU (`libx265`)。
- 视频平台的接收和转码结果需要实际投稿验证。程序不会自动上传或发布视频。

# SDR2HDR Desktop v1.3.0

Windows 离线 SDR 转 HDR10 工具，提供图形界面和命令行入口，现已支持显卡硬件加速。

## 本次发布

- 新增 NVIDIA NVENC、Intel QSV、AMD AMF 10-bit HEVC HDR10 硬件加速编码，自动选择并支持无缝回退 CPU (`libx265`)。
- 启动与转换前自动对显卡硬编执行 2 帧 HDR10 元数据实测，确保 `PQ / BT.2020`、`hvc1`、`Mastering display` 与 `Content light` 写入合规。
- 界面新增“编码器”选项和硬件支持状态展示；命令行新增 `--encoder` 与 `--list-encoders`。
- 支持通过自检的 NVIDIA NVENC 进行画面无损编码，并继续执行全片逐帧 SHA-256 校验。
- 保留 10 秒试转、已有 HDR 保留画面加环绕音效、防覆盖及 `.hdr.json` 转换记录。

## 下载与使用

下载并解压 Windows ZIP，运行 `SDR2HDR.exe`。FFmpeg 和 ffprobe 需单独准备，可放入程序旁的 `bin` 文件夹。使用显卡硬编写入 HDR10 元数据时，需较新的完整 FFmpeg 构建（如包含 `-mastering_display` / `-content_light` 与硬件编码器的版本）。

包内提供 `SHA256SUMS.txt` 供校验。

## 使用范围

画面无损仅指 HDR 处理完成后的 10-bit 4:2:0 视频编码，不保证音频无损，也不保证平台转码后无损。QSV 与 AMF 不支持 10-bit HEVC 无损，使用“自动”模式时会自动切换为 CPU (`libx265`)；无损文件体积较大，部分播放器需要软件解码。

视频平台的接收和转码结果需要实际投稿验证。程序不会自动上传或发布视频。

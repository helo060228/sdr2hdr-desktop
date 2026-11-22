# SDR2HDR Desktop

一个面向 Windows 的离线视频转换工具：将普通 SDR 视频处理为 HDR10，支持 NVIDIA / Intel / AMD 显卡硬件加速与 CPU 自动回退，提供四档画面编码、双声道虚拟环绕，以及自动逐帧校验的画面无损编码。

![SDR2HDR Desktop 界面](assets/app.png)

## 功能

- 输出 HEVC Main 10、10-bit、PQ / BT.2020、MP4 / hvc1 视频。
- 支持 **NVIDIA NVENC / Intel QSV / AMD AMF** 10-bit 显卡硬编加速；启动时自动进行实测与 HDR10 元数据自检，不可用时无缝回退至 **CPU (libx265)**。
- 保持原尺寸和帧率，或选择短边 1440 / 2160 的输出尺寸。
- 提供快速、标准、精细、画面无损四种编码方式。
- 无损档自动比较编码前后每一帧的像素摘要，全部一致才保存最终文件。
- 可选双声道虚拟环绕；也可只给已有 HDR 视频加音效，直接保留原视频轨道。
- 支持先试转 10 秒、取消任务、导出检查和转换记录。
- 离线运行，不自动上传视频，保留原文件。

## 开始使用

1. 在仓库的 **Releases** 页面下载 Windows 压缩包，解压后运行 `SDR2HDR.exe`。
2. 准备包含 `zscale`、`libx265`、`lut1d` 等组件的较新完整 FFmpeg（若使用显卡硬编并写入 HDR10 静态元数据，需 FFmpeg 支持 `-mastering_display` / `-content_light` 及对应 `hevc_nvenc` / `hevc_qsv` / `hevc_amf`）。将其加入 `PATH`，或把 `ffmpeg.exe`、`ffprobe.exe` 放入程序旁的 `bin` 文件夹。
3. 选择 SDR 视频、导出位置、编码器（默认“自动（优先显卡加速）”）、转换方式和音效，先点击 **先试转 10 秒** 检查效果。
4. 点击 **导出完整 HDR**，等待转换和文件检查完成。

Windows 程序包已包含 Python 运行环境，无需另装 Python；FFmpeg 需要单独准备。

[完整使用指南](docs/usage.md) · [算法与输出规格](docs/algorithm.md) · [更新记录](CHANGELOG.md)

## 转换原理

程序先读取输入视频的颜色矩阵和量化范围，在浮点 RGB 中进行色差调整与固定对数映射，再输出 PQ / BT.2020 的 10-bit 画面。

核心处理包含 U/V 色差增益 `0.56` 和逐通道曲线：

```text
q = 0.65 × ln(1 + 5 × max(x, 0)) / ln(6)
```

结果按 PQ 码值解释，并设置 1000 尼特的处理上限。普通中性白的输出约为 PQ `0.65`，对应约 390 尼特参考亮度。最后通过 HEVC 编码保存画面，并写入 HDR10 颜色标记和静态元数据。

这是一种风格化 HDR 增强。它不会恢复原片已经丢失的高光或细节，也不保证与原始 SDR 制作意图严格一致。

## 四档编码与硬件加速

程序默认自动选择已通过自检的显卡硬件编码器（优先级：`NVIDIA NVENC` > `Intel QSV` > `AMD AMF` > `CPU x265`），也可手动指定编码器：

- **快速试转**：CPU `x265 ultrafast / CRF 17`；NVENC `p2 / VBR CQ 22`；QSV `faster / ICQ 22`；AMF `speed / CQP 22`。
- **标准画质**：CPU `x265 fast / CRF 17`；NVENC `p5 / VBR CQ 20`；QSV `medium / ICQ 20`；AMF `balanced / CQP 20`。
- **精细画质**：CPU `x265 medium / CRF 16`；NVENC `p7 / multipass qres / VBR CQ 18 / spatial-aq`；QSV `slow / ICQ 18`；AMF `quality / CQP 18`。
- **画面无损编码**：CPU 使用 `x265 lossless=1`；经自检支持的 NVIDIA NVENC 使用 `-tune lossless`；QSV 与 AMF 不支持 10-bit HEVC 无损，在“自动”模式下会自动切换为 CPU (`x265`)。无损档都会在导出后自动逐帧核对像素摘要。

四档采用相同的 HDR 像素处理规则。显卡硬编可显著缩短 2K / 4K 编码耗时；CPU `libx265` 在同体积下通常具有更高的压缩效率。无损档的文件可能明显大于原片，校验也需要更多时间。

**无损的范围是：HDR 处理后、进入编码器前的 10-bit YUV 4:2:0 画面，与导出视频解码后的画面逐帧一致。**它不表示 SDR 到 HDR 的全部处理可逆，也不保证音频无损。

## 播放与投稿

- 在支持 HDR 的屏幕和播放器中观看，按设备要求开启系统 HDR。
- 输出文件适合用作 HDR 视频素材；平台接收、转码和最终 HDR 档位需要实际投稿验证。
- 视频平台可能再次有损压缩，因此不能保证站内播放版本无损。
- HEVC 无损文件可能超出部分硬件解码器的支持范围，需要软件解码。
- 虚拟环绕通过声场扩展、短延迟和限幅处理双声道声音，输出仍是双声道 AAC。

## 源码运行

推荐使用 Python 3.13，最低 Python 版本为 3.10。运行时仅使用 Python 标准库；转换依赖外部 FFmpeg。

```powershell
python source/app.py
python source/hdr_core.py --list-encoders
python source/hdr_core.py "input.mp4" "output_HDR.mp4" --encoder auto --quality fine
python source/hdr_core.py "input.mp4" "output_HDR_cpu.mp4" --encoder cpu --quality fine
python source/hdr_core.py "input.mp4" "output_lossless.mp4" --quality lossless
python source/hdr_core.py "input.mp4" "output_HDR_surround.mp4" --audio-effect surround
```

## 测试与构建

```powershell
python -m unittest discover -s source -p test_conversion.py -v
python -m pip install -r requirements-build.txt
python scripts/build_release.py
```

构建脚本会先运行集成测试，再生成 Windows 程序包及 SHA-256 校验文件。运行测试需要完整 FFmpeg；构建需要 Windows 和 PyInstaller。

## 许可证

项目代码采用 [MIT License](LICENSE)。Python、Tcl/Tk、PyInstaller 及外部音视频组件适用各自许可证，详见 [第三方说明](THIRD_PARTY_NOTICES.md)。

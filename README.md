# SDR2HDR Desktop

一个面向 Windows 的离线视频转换工具：将普通 SDR 视频处理为 HDR10，提供四档画面编码、双声道虚拟环绕，以及自动逐帧校验的画面无损编码。

![SDR2HDR Desktop 界面](assets/app.png)

## 功能

- 输出 HEVC Main 10、10-bit、PQ / BT.2020、MP4 / hvc1 视频。
- 保持原尺寸和帧率，或选择短边 1440 / 2160 的输出尺寸。
- 提供快速、标准、精细、画面无损四种编码方式。
- 无损档自动比较编码前后每一帧的像素摘要，全部一致才保存最终文件。
- 可选双声道虚拟环绕；也可只给已有 HDR 视频加音效，直接保留原视频轨道。
- 支持先试转 10 秒、取消任务、导出检查和转换记录。
- 离线运行，不自动上传视频，保留原文件。

## 开始使用

1. 在仓库的 **Releases** 页面下载 Windows 压缩包，解压后运行 `SDR2HDR.exe`。
2. 准备包含 `zscale`、`libx265`、`lut1d` 等组件的完整 FFmpeg。将其加入 `PATH`，或把 `ffmpeg.exe`、`ffprobe.exe` 放入程序旁的 `bin` 文件夹。
3. 选择 SDR 视频、导出位置、转换档位和音效，先点击 **先试转 10 秒** 检查效果。
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

## 四档编码

- **快速试转**：`ultrafast / CRF 17`，速度优先。
- **标准画质**：`fast / CRF 17`，平衡速度、体积与画质。
- **精细画质**：`medium / CRF 16`，进一步减少压缩损失。
- **画面无损编码**：`lossless=1`，不以 CRF 限制码率，自动逐帧校验，适合保存母版。

四档采用相同的 HDR 处理规则。无损档的文件可能明显大于原片，编码和校验也需要更多时间。

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
python source/hdr_core.py "input.mp4" "output_HDR.mp4" --quality fine
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

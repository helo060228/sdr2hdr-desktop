# SDR2HDR Desktop

一个面向 Windows 的离线视频转换工具：支持将普通 **SDR 视频** 和 **杜比视界（Dolby Vision Profile 5 / 7 / 8.1 / 8.4）** 转换为标准 **HDR10**，支持 NVIDIA / Intel / AMD 显卡硬件加速与 CPU 自动回退，提供四档画面编码、双声道虚拟环绕，以及自动逐帧校验的画面无损编码。

![SDR2HDR Desktop 界面](assets/app.png)

## 功能

- **SDR 转 HDR10（三档风格可选）**：输出 HEVC Main 10、10-bit、PQ / BT.2020、MP4 / MKV 视频——
  - **影院均衡高光（推荐电影·1000 nit，默认）**：采用 `Gamma 2.2` 线性光与标准 `BT.709 → BT.2020` 等色度无凹陷曲线，纯黑锚定 `0 nit` 不泛灰，暗部通透（`v = 0.05～0.20`），去除中间调凸起导致的云层／雪地层次发平问题，受光肤色留足动态余量（~174 / 1000 nits），同时极亮高光平滑冲至 **`1000 nits`**（`MaxCLL/MaxFALL = 1000/260`）；
  - **柔和暗室保护（450 nit）**：沿用 `BT.1886 (Gamma 2.4)`、`190 nits` 漫反射基准与 `450 nits` 峰值（`MaxCLL/MaxFALL = 450/200`），适合全黑暗室参考观影；
  - **高对比风格（原版·动漫）**：保留 v1.4 及以前的对数高对比曲线（`UV × 0.56`，暗部更沉、亮部更艳丽）。
- **杜比视界（DV）转 HDR10**：自动识别 Profile 5 / 7 / 8.1 / 8.4——
  - **Profile 8.1 / Profile 7（含 HDR10 兼容层）**：秒级无损直剥（不重编码，保留原生 10-bit HDR10 基础层并剥离 RPU／增强层，零画质损失，并兼容未标注容器矩阵的蓝光 Remux）；
  - **Profile 5（IPTPQc2，普通屏幕偏绿偏紫）与 Profile 8.4（HLG 杜比视界）**：通过 **Vulkan 硬解 + `libplacebo`** 进行杜比视界色彩还原，再由显卡硬编或 CPU 导出标准 HDR10。
- 支持 **NVIDIA NVENC / Intel QSV / AMD AMF** 10-bit 显卡硬编加速，并在显卡有损编码时自动启用 **Vulkan 硬解 + `libplacebo` GPU 色彩管线**（将色域转换、高光保护曲线与 10-bit 抖动移入 GPU 执行，不支持或无损/CPU 编码时无缝回退至 CPU 浮点滤镜）。
- 保持原尺寸和帧率，或选择短边 1440 / 2160 的输出尺寸。
- 提供快速、标准、精细、画面无损四种编码方式。
- 无损档自动比较编码前后每一帧的像素摘要，全部一致才保存最终文件。
- **同封装默认导出（MKV→MKV / MP4→MP4）、音轨勾选与内嵌字幕保留**：导入 `.mkv` 默认导 `.mkv`、导入 `.mp4` 默认导 `.mp4`；支持像 DVDFab 一样逐条勾选保留／丢弃音轨与内嵌字幕（完整保留语言和标题标签）。选“原声（保留）”时，MKV 可无损直拷 `TrueHD / DTS-HD / EAC3 Atmos / PCM` 与 `SRT / ASS / 蓝光 PGS` 字幕，MP4 可直拷 `AAC / AC3 / EAC3 / DTS / FLAC / ALAC / Opus` 多声道并转封装文本字幕，遇到 MP4 不兼容轨道时会在转换前弹窗提醒一键改存 MKV。仍可选双声道虚拟环绕，或只给已有 HDR10 视频加音效。
- **批量任务列表 + 暂停／继续 + 自定义试转片段 + 完成后自动关机／待机**：
  - **任务列表**：选好参数后既可直接点击 **导出完整 HDR** 立刻转换，也可点击 **加入任务列表**（或 **批量添加视频…**），无论 1 项还是多项任务均可点击 **开始队列** 依次顺序执行，支持上移／下移／移除／清理已完成；
  - **暂停 / 继续**：编码中可随时点击 **暂停** 挂起进程（冻结用时与 ETA），点击 **继续** 原地恢复编码；
  - **自定义试转片段**：点击 **自定义试转…** 拖动时间轴或输入起点／时长（默认定位视频正中 30 秒，提供 0%/25%/50%/75% 快捷定位与 10/15/30/60 秒常用时长）；
  - **完成后动作**：支持勾选 **完成后自动关机** 或 **完成后进入待机（睡眠）**，全片或队列顺利完成后弹出 **60 秒安全倒计时确认框**（可随时取消），长时间挂机无需值守；
  - **无留白等比例全屏适配**：最大化或按 `F11` 全屏时，所有字体、按钮、输入框、进度条与任务列表按窗口尺寸等比例平滑放大并铺满整屏，告别四周空白与多余滚动条。
- 支持 **停止并保留已转片段**（优雅封包为可正常播放的 `*_已转片段`；在无损档 99% 逐帧校验阶段点击也可立即跳过耗时校验并保留完整无损文件）、彻底取消任务、导出检查和转换记录；实时展示进度百分比、速率倍率、已用时间与预计剩余时间（ETA）。
- 离线运行，不自动上传视频，保留原文件。

## 开始使用

1. 在仓库的 **Releases** 页面下载 Windows 压缩包，解压后运行 `SDR2HDR.exe`。
2. 准备包含 `zscale`、`libx265`、`lut1d`、`lut3d`、`dovi_rpu`、`libplacebo` 等组件的较新完整 FFmpeg（若使用显卡硬编并写入 HDR10 静态元数据，需 FFmpeg 支持 `-mastering_display` / `-content_light` 及对应 `hevc_nvenc` / `hevc_qsv` / `hevc_amf`）。将其加入 `PATH`，或把 `ffmpeg.exe`、`ffprobe.exe` 放入程序旁的 `bin` 文件夹。
3. 选择 SDR 或杜比视界视频、导出位置、HDR 风格（默认“影院均衡高光（推荐电影·1000 nit）”）、编码器（默认“自动（优先显卡加速）”）、转换方式和音效，先点击 **自定义试转…** 从正片中段抽检效果。
4. 点击 **导出完整 HDR** 立刻转换，或点击 **加入任务列表** 排队后点 **开始队列** 批量执行；长时间任务可按需设置 **完成后自动关机／待机**。

Windows 程序包已包含 Python 运行环境，无需另装 Python；FFmpeg 需要单独准备。

[完整使用指南](docs/usage.md) · [算法与输出规格](docs/algorithm.md) · [更新记录](CHANGELOG.md)

## 转换原理

SDR 转 HDR10 提供三种独立处理风格：

1. **影院均衡高光（`balanced`，1000 nit，默认）**：按 `Gamma 2.2` 解码至线性光并做标准 `BT.709 → BT.2020` 色域矩阵变换，依据 BT.2020 相对亮度 `Y` 计算零凹陷等色度单调曲线（`Y ≤ 0.58` 采用 `235·Y` 漫反射基准，高于拐点 `Y = 0.58` 按 `235·Y + 765·((Y - 0.58)/0.42)^2.5` 平滑冲至 `1000 nits`）——纯黑锚定 `0 nit`、暗部细节通透、受光肤色稳在约 `174 / 1000 nits`（5.7× 余量不发白）、云层与雪地亮部层次（`v = 0.80～0.90`）较旧版提升 2.3×～2.7×，并写入 **`MaxCLL = 1000 / MaxFALL = 260`** 静态元数据。显卡路径直接由 `libplacebo` GLSL 着色器在 GPU 执行，CPU/无损路径使用 `49³` 四面体 3D LUT（`lut3d`）+ `spline36`。
2. **柔和暗室保护（`natural`，450 nit）**：采用 `BT.1886 (Gamma 2.4)` 线性光 BT.709→BT.2020 等色度映射，漫反射基准 `190 nits`（拐点 `Y = 0.62` 以上平滑抬升至 `450 nits` 峰值，`MaxCLL = 450 / MaxFALL = 200`），观感更柔和，适合全黑暗室。
3. **高对比风格（`vivid`，原版）**：在非线性浮点 RGB 中施加 `U/V × 0.56` 色差增益与逐通道固定对数曲线 `q = 0.65 × ln(1 + 5 × max(x, 0)) / ln(6)`（上限 1000 尼特，中性白约 390.5 尼特），适合动漫与偏暗的高对比风格化呈现。

无论哪种风格，都不会凭空恢复原 SDR 片源已经剪裁或丢失的高光细节。

## 四档编码与硬件加速

程序默认自动选择已通过自检的显卡硬件编码器（优先级：`NVIDIA NVENC` > `Intel QSV` > `AMD AMF` > `CPU x265`），统一采用 4 秒关键帧间隔（`-g fps×4`）兼顾压缩率与拖动／平台兼容，各档码率已针对 10-bit HDR10 完成均衡校准：

- **快速试转**：CPU `x265 ultrafast / CRF 21`；NVENC `p2 / VBR CQ 25`；QSV `faster / ICQ 23`（支持时自动开启 `extbrc/mbbrc`）；AMF `speed / CQP 25`。
- **标准画质**：CPU `x265 fast / CRF 19`；NVENC `p5 / VBR CQ 23`；QSV `medium / ICQ 21`（支持时自动开启 `extbrc/mbbrc`）；AMF `balanced / CQP 23`。
- **精细画质**：CPU `x265 medium / CRF 18`；NVENC 默认高速单遍 `p6 / VBR CQ 21 / spatial-aq`（勾选“NVENC 双遍分析”或传 `--multipass` 时启用 `p7 + multipass qres`）；QSV `slow / ICQ 19`（支持时自动开启 `extbrc/mbbrc`）；AMF `quality / CQP 21`。
- **画面无损编码**：CPU 使用 `x265 lossless=1`；经自检支持的 NVIDIA NVENC 使用 `-tune lossless`；QSV 与 AMF 不支持 10-bit HEVC 无损，在“自动”模式下会自动切换为 CPU (`x265`)。无损档默认在导出后自动逐帧核对像素摘要（体积通常为原片 5～15 倍，校验阶段可点击“停止并保留”跳过校验并保留完整文件）。

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
python source/hdr_core.py --list-gpu-filter
python source/hdr_core.py --list-dv-tools
python source/hdr_core.py "input.mp4" "output_HDR.mp4" --style balanced --encoder auto --quality standard
python source/hdr_core.py "input.mp4" "output_HDR_soft.mp4" --style natural --encoder auto --quality standard
python source/hdr_core.py "input.mp4" "output_HDR_mp.mp4" --style balanced --encoder nvenc --quality fine --multipass
python source/hdr_core.py "anime.mp4" "output_HDR_vivid.mp4" --style vivid --encoder auto --quality fine
python source/hdr_core.py "dolby_vision.mkv" "output_HDR10.mp4" --dv-mode auto
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

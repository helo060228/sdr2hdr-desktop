# 更新记录

## 1.4.0 — 2026-11-22

- 新增 **杜比视界（Dolby Vision）转 HDR10** 支持，拖入/选择视频自动识别 Profile 5 / 7 / 8.1 / 8.4。
- **Profile 8.1 / Profile 7（含 HDR10 兼容层）**：自动走秒级无损直剥（`-c:v copy` + `dovi_rpu=strip=1` / `dovi_split=mode=bl`），零重编码、零画质损失，并支持同步添加双声道虚拟环绕音效。
- **Profile 5（IPTPQc2）与 Profile 8.4（HLG 杜比视界）**：自动走 `libplacebo`（`apply_dolbyvision=true`）还原为标准 10-bit PQ / BT.2020，复用显卡硬件加速或 CPU 编码导出，并支持无损逐帧校验与彻底清除残留 DV 元数据。
- 新增启动期 `dovi_rpu` / `dovi_split` / `libplacebo`（Vulkan）自检与命令行参数 `--dv-mode`、`--list-dv-tools`。

## 1.3.0 — 2026-11-22

- 新增显卡硬件加速编码支持：`NVIDIA NVENC`（`hevc_nvenc`）、`Intel QSV`（`hevc_qsv`）与 `AMD AMF`（`hevc_amf`）。
- 新增运行时 2 帧硬件编码自检，验证 10-bit Main 10、`PQ / BT.2020` 及 HDR10 静态元数据完整写入；不可用、尺寸超限或显卡不支持无损时自动回退 CPU (`libx265`)。
- 桌面界面新增“编码器”下拉选择与显卡加速可用性状态提示；命令行新增 `--encoder` 与 `--list-encoders`。
- 支持经自检通过的 NVIDIA NVENC 硬件无损编码，并继续执行逐帧 SHA-256 像素校验。

## 1.2.1 — 2026-11-05

- 整理桌面程序、命令行、使用指南、算法说明和可复现构建流程。
- 提供四档 HEVC 编码、画面无损逐帧校验和双声道空间音效。
- 支持为已有 HDR 保留视频轨道并单独处理音频。
- 包含进度不可用、缺失媒体数值、取消和防覆盖的处理与集成测试。

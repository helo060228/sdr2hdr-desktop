"""Small local desktop front end. All video processing stays on the computer."""
import concurrent.futures
import json
import os
from pathlib import Path
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from hdr_core import (
    Cancelled,
    ConversionError,
    HARDWARE_PRIORITY,
    Options,
    VERSION,
    convert,
    detect_dv_tools,
    detect_encoders,
    detect_gpu_filter,
    inspect_input,
)


ENCODER_LABELS = {
    "自动（优先显卡加速）": "auto",
    "NVIDIA 显卡 (NVENC)": "nvenc",
    "Intel 显卡 (QSV)": "qsv",
    "AMD 显卡 (AMF)": "amf",
    "CPU 软件编码 (x265)": "cpu",
}

STYLE_LABELS = {
    "自然高光保护（推荐电影）": "natural",
    "高对比风格（原版·适合动漫）": "vivid",
}


class App:
    def __init__(self, root):
        self.root = root
        root.title(f"SDR / 杜比视界转 HDR10 · 离线转换器 {VERSION}")
        root.geometry(f"{min(840, root.winfo_screenwidth()-80)}x{min(820, root.winfo_screenheight()-90)}")
        root.minsize(710, 580)
        root.configure(bg="#f4f5f7")
        self.queue = queue.Queue()
        self.cancel = threading.Event()
        self.keep_partial = threading.Event()
        self.running = False
        self.input = tk.StringVar()
        self.output = tk.StringVar()
        self.resolution = tk.StringVar(value="保持原尺寸")
        self.hdr_style = tk.StringVar(value="自然高光保护（推荐电影）")
        self.quality = tk.StringVar(value="标准画质")
        self.encoder = tk.StringVar(value="自动（优先显卡加速）")
        self.audio_effect = tk.StringVar(value="原声（保留）")
        self.detected_encoders = None
        self.dv_tools = None
        self.gpu_filter_caps = None
        self.input_kind = "sdr"
        self.input_dv = None
        self.status = tk.StringVar(value="选择 SDR、杜比视界或 HDR 视频开始处理。")
        self.info = tk.StringVar(value="支持 MP4、MOV、MKV 等本地视频；自动识别 SDR 与杜比视界 Profile 5 / 7 / 8.1 / 8.4。")
        self.encoder_status = tk.StringVar(value="正在检测显卡硬件加速与杜比视界组件…")
        self.progress = tk.DoubleVar()
        self.detail = tk.StringVar(value="")
        self.controls = []
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TFrame", background="#f4f5f7")
        style.configure("TLabel", background="#f4f5f7", font=("Microsoft YaHei UI", 10))
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 23, "bold"), foreground="#172132")
        style.configure("Sub.TLabel", foreground="#566273")
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=(13, 7))
        style.configure("Main.TButton", font=("Microsoft YaHei UI", 12, "bold"), padding=(20, 11))
        style.configure("TEntry", padding=7)
        style.configure("TCombobox", padding=5)
        viewport = tk.Canvas(root, bg="#f4f5f7", highlightthickness=0)
        scrollbar = ttk.Scrollbar(root, orient="vertical", command=viewport.yview)
        scrollbar.pack(side="right", fill="y")
        viewport.pack(side="left", fill="both", expand=True)
        viewport.configure(yscrollcommand=scrollbar.set)
        box = ttk.Frame(viewport, padding=26)
        window_id = viewport.create_window((0, 0), window=box, anchor="nw")
        box.bind("<Configure>", lambda e: viewport.configure(scrollregion=viewport.bbox("all")))
        viewport.bind("<Configure>", lambda e: viewport.itemconfigure(window_id, width=e.width))
        def wheel(event):
            bounds = viewport.bbox("all")
            if bounds and bounds[3] > viewport.winfo_height():
                viewport.yview_scroll(-int(event.delta / 120), "units")
        root.bind_all("<MouseWheel>", wheel)
        ttk.Label(box, text="把画质和音效，一起写进视频", style="Title.TLabel").pack(anchor="w")
        ttk.Label(box, text="SDR 转 HDR10 · 杜比视界 (DV) 转 HDR10 · 已有 HDR10 无损加音效。", style="Sub.TLabel").pack(anchor="w", pady=(9, 23))
        self.path_row(box, "原始视频", self.input, self.pick_input, "选择视频")
        ttk.Label(box, textvariable=self.info, style="Sub.TLabel", wraplength=700).pack(anchor="w", pady=(2, 17))
        self.path_row(box, "导出文件", self.output, self.pick_output, "保存到…")
        row = ttk.Frame(box)
        row.pack(fill="x", pady=(14, 10))
        ttk.Label(row, text="输出尺寸").grid(row=0, column=0, sticky="w")
        c1 = ttk.Combobox(row, textvariable=self.resolution, values=["保持原尺寸", "2K · 短边 1440", "4K · 短边 2160"], state="readonly", width=28)
        c1.grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Label(row, text="HDR 风格").grid(row=0, column=1, sticky="w", padx=(28, 0))
        c_style = ttk.Combobox(row, textvariable=self.hdr_style, values=list(STYLE_LABELS.keys()), state="readonly", width=28)
        c_style.grid(row=1, column=1, sticky="w", padx=(28, 0), pady=(6, 0))
        ttk.Label(row, text="转换方式").grid(row=2, column=0, sticky="w", pady=(12, 0))
        c2 = ttk.Combobox(row, textvariable=self.quality, values=["快速试转", "标准画质", "精细画质", "画面无损编码"], state="readonly", width=28)
        c2.grid(row=3, column=0, sticky="w", pady=(6, 0))
        ttk.Label(row, text="编码器").grid(row=2, column=1, sticky="w", padx=(28, 0), pady=(12, 0))
        c3 = ttk.Combobox(row, textvariable=self.encoder, values=list(ENCODER_LABELS.keys()), state="readonly", width=28)
        c3.grid(row=3, column=1, sticky="w", padx=(28, 0), pady=(6, 0))
        ttk.Label(row, text="音效").grid(row=4, column=0, sticky="w", pady=(12, 0))
        c4 = ttk.Combobox(row, textvariable=self.audio_effect, values=["原声（保留）", "虚拟环绕（双声道）"], state="readonly", width=28)
        c4.grid(row=5, column=0, sticky="w", pady=(6, 0))
        self.resolution_combo, self.style_combo, self.quality_combo, self.encoder_combo, self.audio_combo = c1, c_style, c2, c3, c4
        self.controls += [c1, c_style, c2, c3, c4]
        self.encoding_note = tk.StringVar()
        ttk.Label(box, textvariable=self.encoder_status, style="Sub.TLabel", wraplength=710).pack(anchor="w", pady=(0, 4))
        ttk.Label(box, textvariable=self.encoding_note, style="Sub.TLabel", wraplength=710).pack(anchor="w", pady=(0, 6))
        c_style.bind("<<ComboboxSelected>>", lambda e: self.update_encoding_note())
        c2.bind("<<ComboboxSelected>>", lambda e: self.update_encoding_note())
        c3.bind("<<ComboboxSelected>>", lambda e: self.update_encoding_note())
        ttk.Label(box, text="虚拟环绕通过声场扩展和短延迟增加空间感，输出双声道。\n原声模式尽可能保留原音轨；HDR 画面不会重复增强。", style="Sub.TLabel", wraplength=710).pack(anchor="w", pady=(0, 14))
        actions = ttk.Frame(box)
        actions.pack(fill="x")
        self.start_button = ttk.Button(actions, text="导出完整 HDR", style="Main.TButton", command=self.start)
        self.start_button.pack(side="left")
        self.preview_button = ttk.Button(actions, text="先试转 10 秒", command=lambda: self.start(preview=True))
        self.preview_button.pack(side="left", padx=10)
        self.stop_button = ttk.Button(actions, text="取消", command=self.stop, state="disabled")
        self.stop_button.pack(side="right")
        self.save_partial_button = ttk.Button(actions, text="停止并保留", command=self.stop_and_keep, state="disabled")
        self.save_partial_button.pack(side="right", padx=(0, 8))
        ttk.Progressbar(box, variable=self.progress, maximum=100).pack(fill="x", pady=(20, 12))
        ttk.Label(box, textvariable=self.status, wraplength=710).pack(anchor="w")
        ttk.Label(box, textvariable=self.detail, style="Sub.TLabel", wraplength=710).pack(anchor="w", pady=(5, 10))
        self.open_button = ttk.Button(box, text="打开导出文件夹", command=self.open_folder)
        self.open_button.pack(anchor="w")
        ttk.Label(box, text="B 站转码后仍需检查 HDR 档位；不同屏幕和平台的显示可能存在差异。\n本工具离线处理，不会自动上传或发布。", style="Sub.TLabel", wraplength=710).pack(anchor="w", pady=(17, 0))
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.poll)
        self.update_encoding_note()
        self.detect_encoders_async()
        if len(sys.argv) > 1 and Path(sys.argv[1]).is_file():
            root.after(200, lambda: self.set_input(sys.argv[1]))

    def detect_encoders_async(self):
        def probe_worker():
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                    f_enc = pool.submit(detect_encoders)
                    f_dv = pool.submit(detect_dv_tools)
                    f_gpu = pool.submit(detect_gpu_filter)
                    encoders = f_enc.result()
                    dv_tools = f_dv.result()
                    gpu_filter = f_gpu.result()
                self.queue.put(dict(stage="encoders", encoders=encoders, dv_tools=dv_tools, gpu_filter=gpu_filter))
            except Exception as e:
                self.queue.put(dict(stage="encoders", error=str(e)))
        threading.Thread(target=probe_worker, daemon=True).start()

    def update_encoding_note(self):
        notes = []
        if self.input_kind == "dv_strip":
            if self.dv_tools is not None and not self.dv_tools.get("dovi_rpu"):
                notes.append("提示：" + (self.dv_tools.get("strip_reason") or "缺少 dovi_rpu 滤镜，无法无损直剥。"))
            else:
                notes.append("杜比视界无损直剥：画面直接复制不重编码，输出尺寸／HDR 风格／画质／编码器已自动锁定；仍可选音效。")
            self.encoding_note.set("\n".join(notes))
            return
        if self.input_kind == "hdr10":
            notes.append("已有标准 HDR10：将直接保留原视频轨道，仅按音效设置处理音频。")
            self.encoding_note.set("\n".join(notes))
            return
        if self.input_kind == "dv_reencode" and self.dv_tools is not None and not self.dv_tools.get("libplacebo"):
            notes.append("提示：" + (self.dv_tools.get("libplacebo_reason") or "当前环境不支持 libplacebo／Vulkan。"))
        if self.input_kind == "sdr":
            style_key = STYLE_LABELS[self.hdr_style.get()]
            if style_key == "natural":
                notes.append("自然高光保护：线性光 BT.709→BT.2020 色域映射，稳住肤色与额头／白衣高光细节，同时保持深邃黑位（峰值 450 nits）。")
            else:
                notes.append("高对比风格：原版对数曲线，暗部更沉、色彩更艳丽，适合动漫与暗景；明亮实拍人脸可能偏亮。")
        enc_key = ENCODER_LABELS[self.encoder.get()]
        is_lossless = self.quality.get() == "画面无损编码"
        if self.detected_encoders is not None and enc_key in HARDWARE_PRIORITY:
            info = self.detected_encoders.get(enc_key, {})
            if not info.get("available"):
                notes.append(f"提示：{info.get('label', enc_key)} 当前不可用（{info.get('reason') or '未检测到兼容硬件'}），请改用“自动”或“CPU 软件编码”。")
            elif is_lossless and not info.get("lossless"):
                notes.append(f"提示：{info.get('label', enc_key)} 不支持 10-bit HEVC 无损编码，画面无损档请改用“自动”或“CPU 软件编码”。")
        if is_lossless:
            notes.append("画面无损：文件较大，导出后逐帧核对；声音按音效设置处理。")
        self.encoding_note.set("\n".join(notes))

    def path_row(self, parent, title, var, command, label):
        ttk.Label(parent, text=title).pack(anchor="w", pady=(0, 6))
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(0, 8))
        entry = ttk.Entry(row, textvariable=var)
        entry.pack(side="left", fill="x", expand=True)
        button = ttk.Button(row, text=label, command=command)
        button.pack(side="right", padx=(9, 0))
        self.controls += [entry, button]

    def pick_input(self):
        p = filedialog.askopenfilename(title="选择 SDR／杜比视界／HDR 视频", filetypes=[("视频", "*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.ts"), ("所有文件", "*.*")])
        if p:
            self.set_input(p)

    def _next_output_path(self, source: Path, suffix: str) -> str:
        dest = source.with_name(source.stem + suffix)
        count = 2
        while dest.exists():
            dest = source.with_name(source.stem + suffix.replace(".mp4", f"_{count}.mp4"))
            count += 1
        return str(dest)

    def set_input(self, path):
        self.input.set(path)
        source = Path(path)
        initial_output = self._next_output_path(source, "_HDR.mp4")
        self.output.set(initial_output)
        self.info.set("正在读取视频信息…")
        def read():
            try:
                inspected = inspect_input(source)
                kind = inspected["kind"]
                p = inspected["properties"]
                dv = inspected["dv"]
                duration_label = f"{p['duration']:.1f} 秒" if p["duration"] else "时长未提供"
                base = f"{p['width']} × {p['height']}  ·  {p['fps']:g} 帧/秒  ·  {duration_label}  ·  "
                if kind == "dv_strip":
                    msg = base + f"杜比视界 {dv['profile_label']} → 秒级无损直剥 HDR10"
                    if dv.get("dv_profile") == 7:
                        msg += "\n说明：保留原生 10-bit HDR10 基础层并剥离增强层与 RPU，零重编码损失（不烘焙 FEL 增量）。"
                    else:
                        msg += "\n说明：直接剥离杜比视界 RPU 并保留原生 HDR10 码流，零重编码、零画质损失。"
                elif kind == "dv_reencode":
                    msg = base + f"杜比视界 {dv['profile_label']} → libplacebo 还原 + HDR10"
                elif kind == "hdr10":
                    msg = base + "HDR10 · 画面直拷（仅加音效）"
                else:
                    msg = base + "SDR → HDR10"
                    if p["guessed"]:
                        msg += "\n" + "；".join(p["guessed"])
                self.queue.put(dict(stage="input", path=path, message=msg, kind=kind, dv=dv, initial_output=initial_output))
            except Exception as e:
                self.queue.put(dict(stage="input", path=path, message=str(e), kind="sdr", dv=None))
        threading.Thread(target=read, daemon=True).start()

    def pick_output(self):
        p = filedialog.asksaveasfilename(title="保存 HDR 副本", defaultextension=".mp4", initialfile=Path(self.output.get()).name,
                                       filetypes=[("HDR 视频", "*.mp4")], confirmoverwrite=False)
        if p:
            self.output.set(p)

    def start(self, preview=False):
        if self.running:
            return
        source = self.input.get().strip().strip('"')
        output = self.output.get().strip().strip('"')
        if not source or not output:
            messagebox.showinfo("选择视频", "请先选择输入视频和导出位置。")
            return
        if preview:
            p = Path(output)
            p = p.with_name(p.stem + "_10秒试转.mp4")
            n = 2
            while p.exists():
                p = Path(output).with_name(Path(output).stem + f"_10秒试转_{n}.mp4")
                n += 1
            output = str(p)
        resolution = {"保持原尺寸": "original", "2K · 短边 1440": "1440", "4K · 短边 2160": "2160"}[self.resolution.get()]
        hdr_style = STYLE_LABELS[self.hdr_style.get()]
        quality = {"快速试转": "fast", "标准画质": "standard", "精细画质": "fine", "画面无损编码": "lossless"}[self.quality.get()]
        encoder = ENCODER_LABELS[self.encoder.get()]
        audio_effect = {"原声（保留）": "original", "虚拟环绕（双声道）": "surround"}[self.audio_effect.get()]
        opts = Options(source, output, resolution=resolution, quality=quality, duration=10 if preview else None,
                       audio_effect=audio_effect, encoder=encoder, hdr_style=hdr_style)
        self.running = True
        self.cancel.clear()
        self.keep_partial.clear()
        self.progress.set(0)
        self.status.set("正在准备…")
        self.detail.set("转换速度取决于视频尺寸与所选显卡/处理器；原片保持不变。")
        self.set_busy(True)
        def run():
            try:
                inspected = inspect_input(Path(source))
                kind = inspected["kind"]
                if kind == "hdr10":
                    opts.copy_hdr_video = True
                    opts.resolution = "original"
                    if audio_effect == "original":
                        raise ConversionError("这个视频已经是标准 HDR10，无需重复转换。请选择“虚拟环绕（双声道）”，即可只处理声音并保留 HDR 画面。")
                elif kind == "dv_strip":
                    opts.copy_hdr_video = False
                    opts.resolution = "original"
                    opts.dv_mode = "strip"
                elif kind == "dv_reencode":
                    opts.copy_hdr_video = False
                    opts.dv_mode = "reencode"
                else:
                    opts.copy_hdr_video = False
                    opts.dv_mode = "auto"
                result = convert(opts, self.queue.put, self.cancel, self.keep_partial)
                self.queue.put(dict(stage="finished", result=result))
            except Cancelled as e:
                self.queue.put(dict(stage="cancelled", message=str(e)))
            except Exception as e:
                self.queue.put(dict(stage="error", message=str(e)))
        threading.Thread(target=run, daemon=True).start()

    def set_busy(self, busy):
        for c in self.controls:
            if isinstance(c, ttk.Combobox):
                c.configure(state="disabled" if busy else "readonly")
            else:
                c.configure(state="disabled" if busy else "normal")
        self.start_button.configure(state="disabled" if busy else "normal")
        self.preview_button.configure(state="disabled" if busy else "normal")
        self.stop_button.configure(state="normal" if busy else "disabled")
        self.save_partial_button.configure(state="normal" if busy else "disabled")
        if not busy:
            if self.input_kind in ("hdr10", "dv_strip"):
                self.resolution_combo.configure(state="disabled")
                self.quality_combo.configure(state="disabled")
                self.encoder_combo.configure(state="disabled")
            if self.input_kind != "sdr":
                self.style_combo.configure(state="disabled")

    def stop(self):
        self.cancel.set()
        self.save_partial_button.configure(state="disabled")
        self.status.set("正在取消并清理未完成的视频…")

    def stop_and_keep(self):
        if not self.running:
            return
        self.keep_partial.set()
        self.save_partial_button.configure(state="disabled")
        self.status.set("正在正常封包并保存已转换的 MP4 片段（请稍候）…")

    def poll(self):
        try:
            while True:
                p = self.queue.get_nowait()
                stage = p["stage"]
                if stage == "input":
                    if self.input.get() == p["path"]:
                        self.info.set(p["message"])
                        self.input_kind = p.get("kind", "sdr")
                        self.input_dv = p.get("dv")
                        if self.input_kind in ("hdr10", "dv_strip"):
                            self.resolution.set("保持原尺寸")
                        if self.output.get() == p.get("initial_output"):
                            name = Path(p["path"])
                            if self.input_kind == "hdr10":
                                self.output.set(self._next_output_path(name, "_环绕.mp4"))
                            elif self.input_kind in ("dv_strip", "dv_reencode"):
                                self.output.set(self._next_output_path(name, "_HDR10.mp4"))
                        button_text = {
                            "dv_strip": "无损导出 HDR10",
                            "dv_reencode": "导出完整 HDR10",
                            "hdr10": "导出环绕 HDR",
                        }.get(self.input_kind, "导出完整 HDR")
                        self.start_button.configure(text=button_text)
                        self.set_busy(self.running)
                        self.update_encoding_note()
                elif stage == "encoders":
                    if "error" in p:
                        self.encoder_status.set("未能自动检测编码器（转换时将再次检查）：" + p["error"])
                    else:
                        self.detected_encoders = p["encoders"]
                        self.dv_tools = p.get("dv_tools")
                        self.gpu_filter_caps = p.get("gpu_filter")
                        hw_labels = []
                        for key in HARDWARE_PRIORITY:
                            info = self.detected_encoders.get(key, {})
                            if info.get("available"):
                                suffix = "（含无损）" if info.get("lossless") else "（无损需用 CPU）"
                                hw_labels.append(info["short_label"] + suffix)
                        if hw_labels:
                            enc_part = "显卡加速：" + "、".join(hw_labels)
                        else:
                            enc_part = "显卡加速：未检测到可用硬编（使用 CPU x265）"
                        gpu_part = ""
                        if self.gpu_filter_caps:
                            if self.gpu_filter_caps.get("libplacebo_shader") and self.gpu_filter_caps.get("vulkan_hwdec"):
                                gpu_part = "；色彩管线：Vulkan 全 GPU 加速"
                            elif self.gpu_filter_caps.get("libplacebo_shader"):
                                gpu_part = "；色彩管线：libplacebo GPU 加速"
                            else:
                                gpu_part = "；色彩管线：CPU 浮点滤镜"
                        dv_part = ""
                        if self.dv_tools:
                            if self.dv_tools.get("strip_available") and self.dv_tools.get("libplacebo"):
                                dv_part = "；杜比视界：支持 8.1/7 无损直剥 + 5/8.4 色彩还原"
                            elif self.dv_tools.get("strip_available"):
                                dv_part = "；杜比视界：支持 8.1/7 无损直剥（缺 libplacebo／Vulkan）"
                            else:
                                dv_part = "；杜比视界：当前 FFmpeg 缺少 dovi_rpu／libplacebo"
                        self.encoder_status.set(enc_part + gpu_part + dv_part + "。")
                        self.update_encoding_note()
                elif stage == "stopping":
                    self.save_partial_button.configure(state="disabled")
                    self.status.set(p["message"])
                elif stage in ("encoding", "verifying", "done"):
                    self.progress.set(p.get("percent", 0))
                    self.status.set(p["message"])
                    if p.get("plan") and p["plan"].get("encoder_note"):
                        self.detail.set(p["plan"]["encoder_note"])
                    if "seconds" in p:
                        timing = (f"已处理 {p['seconds']:.1f} / {p['total']:.1f} 秒" if p.get("total") else
                                  f"已处理 {p['seconds']:.1f} 秒（原片时长未提供）")
                        self.detail.set(timing + (f"    {p['speed']}" if p.get("speed") else ""))
                elif stage in ("finished", "cancelled", "error"):
                    self.running = False
                    self.set_busy(False)
                    if stage == "finished":
                        self.last_output = p["result"]["output"]
                        encoder_label = p["result"].get("plan", {}).get("encoder_label")
                        if p["result"].get("partial"):
                            prefix = f"已停止并保留片段（{encoder_label}）" if encoder_label else "已停止并保留片段"
                            self.status.set(f"{prefix}：可正常播放，HDR10 检查均通过。")
                        else:
                            done_prefix = f"已完成（{encoder_label}）" if encoder_label else "已完成"
                            self.status.set(f"{done_prefix}：HDR 视频和文件检查均通过。")
                        self.detail.set(self.last_output)
                    else:
                        self.status.set("已取消。" if stage == "cancelled" else "转换未完成。")
                        self.detail.set(p["message"][:500])
                        if stage == "error":
                            messagebox.showerror("转换未完成", p["message"])
        except queue.Empty:
            pass
        self.root.after(120, self.poll)

    def open_folder(self):
        path = Path(getattr(self, "last_output", self.output.get()))
        if path.parent.is_dir():
            os.startfile(str(path.parent))

    def close(self):
        if self.running:
            if not messagebox.askyesno("正在转换", "取消当前转换并关闭？原片不会受影响。"):
                return
            self.cancel.set()
            self.status.set("正在停止转换…")
            self.root.after(200, self.close_after_cancel)
        else:
            self.root.destroy()

    def close_after_cancel(self):
        if self.running:
            self.root.after(200, self.close_after_cancel)
        else:
            self.root.destroy()


def main():
    if os.name == "nt":
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except OSError:
            pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()

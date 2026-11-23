"""Small local desktop front end. All video processing stays on the computer."""
import concurrent.futures
import json
import math
import os
from pathlib import Path
import queue
import sys
import threading
import time
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
    estimate_eta_seconds,
    format_duration_clock,
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
    "影院均衡高光（推荐电影·750 nit）": "balanced",
    "柔和暗室保护（450 nit）": "natural",
    "高对比风格（原版·动漫）": "vivid",
}


def format_progress_detail(seconds: float, total: float, speed: str = "",
                           elapsed: float | None = None,
                           eta_seconds: float | None = None) -> str:
    """Format progress timing string including processed seconds, speed, elapsed, and ETA."""
    base = (f"已处理 {seconds:.1f} / {total:.1f} 秒" if total and total > 0 else
            f"已处理 {seconds:.1f} 秒（原片时长未提供）")
    parts = [base]
    speed_text = str(speed or "").strip()
    if speed_text:
        parts.append(speed_text)
    if elapsed is not None and math.isfinite(elapsed) and elapsed >= 0:
        parts.append(f"已用 {format_duration_clock(elapsed)}")
    if total and total > 0:
        if eta_seconds is not None and math.isfinite(eta_seconds) and eta_seconds >= 0:
            parts.append(f"预计剩余 {format_duration_clock(eta_seconds)}")
        else:
            parts.append("预计剩余 计算中…")
    return "  ·  ".join(parts)


class App:
    def __init__(self, root):
        self.root = root
        root.title(f"SDR / 杜比视界转 HDR10 · 离线转换器 {VERSION}")
        screen_w = root.winfo_screenwidth()
        screen_h = root.winfo_screenheight()
        try:
            tk_scaling = float(root.tk.call("tk", "scaling"))
        except tk.TclError:
            tk_scaling = 96.0 / 72.0
        dpi_scale = max(1.0, tk_scaling / (96.0 / 72.0))
        init_w = min(int(round(940 * dpi_scale)), max(760, screen_w - 80))
        min_w = min(max(820, int(round(780 * dpi_scale))), max(760, screen_w - 60))
        min_h = min(int(round(460 * dpi_scale)), max(440, screen_h - 90))
        root.minsize(min_w, min_h)
        root.configure(bg="#f4f5f7")
        self.dpi_scale = dpi_scale
        self.queue = queue.Queue()
        self.cancel = threading.Event()
        self.keep_partial = threading.Event()
        self.running = False
        self.job_started = None
        self.last_progress_info = None
        self._last_clock_tick = -1
        self._scrollbar_visible = False
        self._last_wrap = 0
        self._wrap_labels = []
        self.input = tk.StringVar()
        self.output = tk.StringVar()
        self.resolution = tk.StringVar(value="保持原尺寸")
        self.hdr_style = tk.StringVar(value="影院均衡高光（推荐电影·750 nit）")
        self.quality = tk.StringVar(value="标准画质")
        self.encoder = tk.StringVar(value="自动（优先显卡加速）")
        self.audio_effect = tk.StringVar(value="原声（保留）")
        self.multipass = tk.BooleanVar(value=False)
        self.detected_encoders = None
        self.dv_tools = None
        self.gpu_filter_caps = None
        self.input_kind = "sdr"
        self.input_dv = None
        self.status = tk.StringVar(value="选择 SDR、杜比视界或 HDR 视频开始处理。")
        self.info = tk.StringVar(value="支持 MP4、MOV、MKV 等本地视频；自动识别 SDR 与杜比视界 Profile 5 / 7 / 8.1 / 8.4。")
        self.encoder_status = tk.StringVar(value="正在检测显卡硬件加速与杜比视界组件…")
        self.progress = tk.DoubleVar()
        self.percent_text = tk.StringVar(value="0.0%")
        self.detail = tk.StringVar(value="")
        self.controls = []

        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TFrame", background="#f4f5f7")
        style.configure("TLabel", background="#f4f5f7", font=("Microsoft YaHei UI", 10))
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 19, "bold"), foreground="#172132")
        style.configure("Sub.TLabel", foreground="#566273")
        style.configure("Percent.TLabel", font=("Microsoft YaHei UI", 10, "bold"), foreground="#172132")
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=(12, 6))
        style.configure("Main.TButton", font=("Microsoft YaHei UI", 11, "bold"), padding=(18, 9))
        style.configure("TEntry", padding=5)
        style.configure("TCombobox", padding=4)
        style.configure("TCheckbutton", background="#f4f5f7", font=("Microsoft YaHei UI", 10))

        viewport = tk.Canvas(root, bg="#f4f5f7", highlightthickness=0)
        scrollbar = ttk.Scrollbar(root, orient="vertical", command=viewport.yview)
        viewport.pack(side="left", fill="both", expand=True)
        viewport.configure(yscrollcommand=scrollbar.set)
        box = ttk.Frame(viewport, padding=(22, 14, 22, 14))
        window_id = viewport.create_window((0, 0), window=box, anchor="nw")
        self.viewport = viewport
        self.scrollbar = scrollbar
        self.box = box
        self._window_id = window_id

        def wheel(event):
            if self._scrollbar_visible:
                viewport.yview_scroll(-int(event.delta / 120), "units")
        root.bind_all("<MouseWheel>", wheel)

        ttk.Label(box, text="把画质和音效，一起写进视频", style="Title.TLabel").pack(anchor="w")
        ttk.Label(box, text="SDR 转 HDR10 · 杜比视界 (DV) 转 HDR10 · 已有 HDR10 无损加音效。", style="Sub.TLabel").pack(anchor="w", pady=(3, 12))

        self.path_row(box, "原始视频", self.input, self.pick_input, "选择视频")
        info_lbl = ttk.Label(box, textvariable=self.info, style="Sub.TLabel")
        info_lbl.pack(anchor="w", pady=(2, 8))
        self.path_row(box, "导出文件", self.output, self.pick_output, "保存到…")

        row = ttk.Frame(box)
        row.pack(fill="x", pady=(8, 6))
        row.columnconfigure(0, weight=10, uniform="opt")
        row.columnconfigure(1, weight=12, uniform="opt")
        row.columnconfigure(2, weight=11, uniform="opt")

        ttk.Label(row, text="输出尺寸").grid(row=0, column=0, sticky="w", padx=(0, 14))
        c1 = ttk.Combobox(row, textvariable=self.resolution, values=["保持原尺寸", "2K · 短边 1440", "4K · 短边 2160"], state="readonly", width=16)
        c1.grid(row=1, column=0, sticky="ew", padx=(0, 14), pady=(4, 0))

        ttk.Label(row, text="HDR 风格").grid(row=0, column=1, sticky="w", padx=(0, 14))
        c_style = ttk.Combobox(row, textvariable=self.hdr_style, values=list(STYLE_LABELS.keys()), state="readonly", width=18)
        c_style.grid(row=1, column=1, sticky="ew", padx=(0, 14), pady=(4, 0))

        ttk.Label(row, text="音效").grid(row=0, column=2, sticky="w")
        c4 = ttk.Combobox(row, textvariable=self.audio_effect, values=["原声（保留）", "虚拟环绕（双声道）"], state="readonly", width=16)
        c4.grid(row=1, column=2, sticky="ew", pady=(4, 0))

        ttk.Label(row, text="转换方式").grid(row=2, column=0, sticky="w", padx=(0, 14), pady=(9, 0))
        c2 = ttk.Combobox(row, textvariable=self.quality, values=["快速试转", "标准画质", "精细画质", "画面无损编码"], state="readonly", width=16)
        c2.grid(row=3, column=0, sticky="ew", padx=(0, 14), pady=(4, 0))

        ttk.Label(row, text="编码器").grid(row=2, column=1, sticky="w", padx=(0, 14), pady=(9, 0))
        c3 = ttk.Combobox(row, textvariable=self.encoder, values=list(ENCODER_LABELS.keys()), state="readonly", width=16)
        c3.grid(row=3, column=1, sticky="ew", padx=(0, 14), pady=(4, 0))

        ttk.Label(row, text="高级选项").grid(row=2, column=2, sticky="w", pady=(9, 0))
        c_mp = ttk.Checkbutton(row, text="NVENC 双遍分析 (Multipass，更慢)", variable=self.multipass, command=self.update_encoding_note)
        c_mp.grid(row=3, column=2, sticky="w", pady=(4, 0))

        self.option_grid = row
        self.resolution_combo, self.style_combo, self.quality_combo, self.encoder_combo, self.audio_combo = c1, c_style, c2, c3, c4
        self.multipass_check = c_mp
        self.controls += [c1, c_style, c2, c3, c4, c_mp]

        self.encoding_note = tk.StringVar()
        enc_status_lbl = ttk.Label(box, textvariable=self.encoder_status, style="Sub.TLabel")
        enc_status_lbl.pack(anchor="w", pady=(4, 2))
        enc_note_lbl = ttk.Label(box, textvariable=self.encoding_note, style="Sub.TLabel")
        enc_note_lbl.pack(anchor="w", pady=(0, 2))
        c_style.bind("<<ComboboxSelected>>", lambda e: self.update_encoding_note())
        c2.bind("<<ComboboxSelected>>", lambda e: self.update_encoding_note())
        c3.bind("<<ComboboxSelected>>", lambda e: self.update_encoding_note())
        audio_hint_lbl = ttk.Label(box, text="虚拟环绕输出双声道空间音效；原声模式尽可能保留原音轨，HDR 画面不重复增强。", style="Sub.TLabel")
        audio_hint_lbl.pack(anchor="w", pady=(0, 10))

        actions = ttk.Frame(box)
        actions.pack(fill="x")
        self.actions_frame = actions
        self.start_button = ttk.Button(actions, text="导出完整 HDR", style="Main.TButton", command=self.start)
        self.start_button.pack(side="left")
        self.preview_button = ttk.Button(actions, text="先试转 10 秒", command=lambda: self.start(preview=True))
        self.preview_button.pack(side="left", padx=10)
        self.stop_button = ttk.Button(actions, text="取消", command=self.stop, state="disabled")
        self.stop_button.pack(side="right")
        self.save_partial_button = ttk.Button(actions, text="停止并保留", command=self.stop_and_keep, state="disabled")
        self.save_partial_button.pack(side="right", padx=(0, 8))

        progress_row = ttk.Frame(box)
        progress_row.pack(fill="x", pady=(12, 6))
        ttk.Progressbar(progress_row, variable=self.progress, maximum=100).pack(side="left", fill="x", expand=True)
        ttk.Label(progress_row, textvariable=self.percent_text, style="Percent.TLabel", width=7, anchor="e").pack(side="right", padx=(10, 0))

        status_lbl = ttk.Label(box, textvariable=self.status)
        status_lbl.pack(anchor="w")
        detail_lbl = ttk.Label(box, textvariable=self.detail, style="Sub.TLabel")
        detail_lbl.pack(anchor="w", pady=(3, 6))

        bottom_row = ttk.Frame(box)
        bottom_row.pack(fill="x", pady=(2, 0))
        self.open_button = ttk.Button(bottom_row, text="打开导出文件夹", command=self.open_folder)
        self.open_button.pack(side="left")
        footer_lbl = ttk.Label(bottom_row, text="B 站转码后仍需检查 HDR 档位；不同屏幕和平台的显示可能存在差异。本工具离线处理，不会自动上传或发布。", style="Sub.TLabel")
        footer_lbl.pack(side="left", padx=(14, 0))
        self.footer_label = footer_lbl

        self._wrap_labels = [info_lbl, enc_status_lbl, enc_note_lbl, audio_hint_lbl, status_lbl, detail_lbl]

        initial_wrap = max(560, init_w - 52)
        self._last_wrap = initial_wrap
        for lbl in self._wrap_labels:
            lbl.configure(wraplength=initial_wrap)
        self.footer_label.configure(wraplength=max(400, init_w - 200))

        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.poll)
        self.update_encoding_note()
        self.detect_encoders_async()

        viewport.itemconfigure(window_id, width=init_w)
        root.update_idletasks()
        req_h = box.winfo_reqheight()
        headroom = int(round(68 * dpi_scale))
        init_h = min(max(int(round(520 * dpi_scale)), req_h + headroom), max(480, screen_h - 90))
        root.geometry(f"{init_w}x{init_h}")

        box.bind("<Configure>", self.sync_layout)
        viewport.bind("<Configure>", self.sync_layout)
        self.sync_layout()

        if len(sys.argv) > 1 and Path(sys.argv[1]).is_file():
            root.after(200, lambda: self.set_input(sys.argv[1]))

    def sync_layout(self, _event=None):
        vw = self.viewport.winfo_width()
        vh = self.viewport.winfo_height()
        if vw > 1:
            self.viewport.itemconfigure(self._window_id, width=vw)
            wrap = max(560, vw - 52)
            if abs(wrap - self._last_wrap) >= 4:
                self._last_wrap = wrap
                for lbl in self._wrap_labels:
                    lbl.configure(wraplength=wrap)
                self.footer_label.configure(wraplength=max(400, vw - 220))
        content_h = self.box.winfo_reqheight()
        self.viewport.configure(scrollregion=(0, 0, max(1, vw), content_h))
        need_scroll = vh > 1 and content_h > vh + 2
        if need_scroll != self._scrollbar_visible:
            if need_scroll:
                self.scrollbar.pack(side="right", fill="y", before=self.viewport)
                self._scrollbar_visible = True
            else:
                self.scrollbar.pack_forget()
                self.viewport.yview_moveto(0.0)
                self._scrollbar_visible = False

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

    def update_multipass_state(self):
        enc_key = ENCODER_LABELS[self.encoder.get()]
        allowed = (
            not self.running
            and self.input_kind not in ("hdr10", "dv_strip")
            and self.quality.get() != "画面无损编码"
            and enc_key in ("auto", "nvenc")
        )
        self.multipass_check.configure(state="normal" if allowed else "disabled")

    def update_encoding_note(self):
        notes = []
        if self.input_kind == "dv_strip":
            if self.dv_tools is not None and not self.dv_tools.get("dovi_rpu"):
                notes.append("提示：" + (self.dv_tools.get("strip_reason") or "缺少 dovi_rpu 滤镜，无法无损直剥。"))
            else:
                notes.append("杜比视界无损直剥：画面直接复制不重编码，输出尺寸／HDR 风格／画质／编码器已自动锁定；仍可选音效。")
            self.encoding_note.set("\n".join(notes))
            self.update_multipass_state()
            return
        if self.input_kind == "hdr10":
            notes.append("已有标准 HDR10：将直接保留原视频轨道，仅按音效设置处理音频。")
            self.encoding_note.set("\n".join(notes))
            self.update_multipass_state()
            return
        if self.input_kind == "dv_reencode" and self.dv_tools is not None and not self.dv_tools.get("libplacebo"):
            notes.append("提示：" + (self.dv_tools.get("libplacebo_reason") or "当前环境不支持 libplacebo／Vulkan。"))
        if self.input_kind == "sdr":
            style_key = STYLE_LABELS[self.hdr_style.get()]
            if style_key == "balanced":
                notes.append("影院均衡高光：深黑位 + 明亮中间调 + 等色度高光保护，稳住额头与肤色细节的同时释放 750 nits 极亮冲击力。")
            elif style_key == "natural":
                notes.append("柔和暗室保护：更克制的 190 nits 漫反射基准与 450 nits 峰值，适合全黑暗室观影与保守高光控制。")
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
        elif self.multipass.get() and enc_key in ("auto", "nvenc"):
            notes.append("NVENC 双遍分析已启用：码率分配更精细，耗时约增加 30%～80%。")
        self.encoding_note.set("\n".join(notes))
        self.update_multipass_state()

    def path_row(self, parent, title, var, command, label):
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(0, 6))
        ttk.Label(row, text=title, width=8).pack(side="left")
        entry = ttk.Entry(row, textvariable=var)
        entry.pack(side="left", fill="x", expand=True, padx=(8, 0))
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
                       audio_effect=audio_effect, encoder=encoder, hdr_style=hdr_style,
                       multipass=bool(self.multipass.get()))
        self.running = True
        self.job_started = time.monotonic()
        self.last_progress_info = None
        self._last_clock_tick = -1
        self.cancel.clear()
        self.keep_partial.clear()
        self.progress.set(0)
        self.percent_text.set("0.0%")
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
        self.update_multipass_state()

    def stop(self):
        self.cancel.set()
        self.save_partial_button.configure(state="disabled")
        self.status.set("正在取消并清理未完成的视频…")

    def stop_and_keep(self):
        if not self.running:
            return
        self.keep_partial.set()
        self.save_partial_button.configure(state="disabled")
        self.status.set("正在正常封包并保存已转换的 MP4 片段（请勿强行关闭）…")

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
                    self.last_progress_info = dict(stage="stopping")
                elif stage in ("encoding", "verifying", "done"):
                    pct = float(p.get("percent", 0) or 0)
                    pct = max(0.0, min(100.0, pct))
                    self.progress.set(pct)
                    self.percent_text.set(
                        "已保留" if (stage == "done" and p.get("partial")) else f"{pct:.1f}%"
                    )
                    self.status.set(p["message"])
                    live_elapsed = p.get("elapsed")
                    if live_elapsed is None and self.job_started is not None:
                        live_elapsed = max(0.0, time.monotonic() - self.job_started)
                    if stage == "verifying":
                        self.last_progress_info = dict(stage="verifying")
                        if live_elapsed is not None:
                            self.detail.set(f"已用 {format_duration_clock(live_elapsed)}  ·  正在校验导出文件…")
                    elif stage == "done":
                        self.last_progress_info = dict(stage="done")
                    else:
                        if p.get("plan") and p["plan"].get("encoder_note") and "seconds" not in p:
                            self.detail.set(p["plan"]["encoder_note"])
                        if "seconds" in p:
                            sec = float(p["seconds"])
                            tot = float(p.get("total") or 0)
                            spd = p.get("speed") or ""
                            eta = p.get("eta_seconds")
                            if eta is None and live_elapsed is not None:
                                eta = estimate_eta_seconds(sec, tot, live_elapsed, spd)
                            self.last_progress_info = dict(
                                stage="encoding",
                                seconds=sec,
                                total=tot,
                                speed=spd,
                            )
                            self.detail.set(format_progress_detail(sec, tot, spd, live_elapsed, eta))
                elif stage in ("finished", "cancelled", "error"):
                    total_elapsed = None
                    if stage == "finished":
                        total_elapsed = p["result"].get("elapsed_seconds")
                    if total_elapsed is None and self.job_started is not None:
                        total_elapsed = max(0.0, time.monotonic() - self.job_started)
                    self.running = False
                    self.job_started = None
                    self.last_progress_info = None
                    self._last_clock_tick = -1
                    self.set_busy(False)
                    if stage == "finished":
                        self.last_output = p["result"]["output"]
                        encoder_label = p["result"].get("plan", {}).get("encoder_label")
                        is_partial = bool(p["result"].get("partial"))
                        self.progress.set(100.0)
                        self.percent_text.set("已保留" if is_partial else "100.0%")
                        if is_partial:
                            prefix = f"已停止并保留片段（{encoder_label}）" if encoder_label else "已停止并保留片段"
                            self.status.set(f"{prefix}：可正常播放，HDR10 检查均通过。")
                        else:
                            done_prefix = f"已完成（{encoder_label}）" if encoder_label else "已完成"
                            self.status.set(f"{done_prefix}：HDR 视频和文件检查均通过。")
                        elapsed_prefix = (
                            f"总耗时 {format_duration_clock(total_elapsed)}  ·  "
                            if total_elapsed is not None else ""
                        )
                        self.detail.set(elapsed_prefix + self.last_output)
                    else:
                        self.status.set("已取消。" if stage == "cancelled" else "转换未完成。")
                        self.detail.set(p["message"][:500])
                        if stage == "error":
                            messagebox.showerror("转换未完成", p["message"])
        except queue.Empty:
            pass
        if self.running and self.job_started is not None:
            live_elapsed = max(0.0, time.monotonic() - self.job_started)
            whole_sec = int(live_elapsed)
            if whole_sec != self._last_clock_tick:
                self._last_clock_tick = whole_sec
                info = self.last_progress_info
                if info and info.get("stage") == "encoding":
                    eta = estimate_eta_seconds(
                        info["seconds"], info["total"], live_elapsed, info.get("speed")
                    )
                    self.detail.set(
                        format_progress_detail(
                            info["seconds"], info["total"], info.get("speed", ""),
                            live_elapsed, eta,
                        )
                    )
                elif info and info.get("stage") == "verifying":
                    self.detail.set(f"已用 {format_duration_clock(live_elapsed)}  ·  正在校验导出文件…")
                elif info and info.get("stage") == "stopping":
                    self.detail.set(f"已用 {format_duration_clock(live_elapsed)}  ·  正在封包保存已转换片段…")
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

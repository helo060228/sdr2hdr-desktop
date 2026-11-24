"""Small local desktop front end. All video processing stays on the computer."""
import concurrent.futures
import dataclasses
import math
import os
from pathlib import Path
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import uuid

from hdr_core import (
    Cancelled,
    ConversionError,
    HARDWARE_PRIORITY,
    Options,
    VERSION,
    check_container_compatibility,
    convert,
    default_preview_window,
    detect_dv_tools,
    detect_encoders,
    detect_gpu_filter,
    estimate_eta_seconds,
    format_duration_clock,
    inspect_input,
    parse_timecode_seconds,
    trigger_power_action,
)


ENCODER_LABELS = {
    "自动（优先显卡加速）": "auto",
    "NVIDIA 显卡 (NVENC)": "nvenc",
    "Intel 显卡 (QSV)": "qsv",
    "AMD 显卡 (AMF)": "amf",
    "CPU 软件编码 (x265)": "cpu",
}

STYLE_LABELS = {
    "影院均衡高光（推荐电影·1000 nit）": "balanced",
    "柔和暗室保护（450 nit）": "natural",
    "高对比风格（原版·动漫）": "vivid",
}

POWER_LABELS = {
    "无动作": "none",
    "完成后自动关机": "shutdown",
    "完成后进入待机（睡眠）": "sleep",
}

QUALITY_LABELS = {
    "快速试转": "fast",
    "标准画质": "standard",
    "精细画质": "fine",
    "画面无损编码": "lossless",
}

RESOLUTION_LABELS = {
    "保持原尺寸": "original",
    "2K · 短边 1440": "1440",
    "4K · 短边 2160": "2160",
}

AUDIO_EFFECT_LABELS = {
    "原声（保留）": "original",
    "虚拟环绕（双声道）": "surround",
}

STYLE_SHORT = {
    "balanced": "影院1000nit",
    "natural": "柔和450nit",
    "vivid": "高对比",
}

QUALITY_SHORT = {
    "fast": "快速",
    "standard": "标准",
    "fine": "精细",
    "lossless": "无损",
}

SURFACE_BG = "#ffffff"
PANEL_BG = "#f8fafc"
BORDER_COLOR = "#d6dee9"
DIVIDER_BG = "#e5ebf3"
TEXT_PRIMARY = "#172132"
TEXT_MUTED = "#566273"
ACCENT_BLUE = "#2563eb"
ACCENT_BLUE_HOVER = "#1d4ed8"
ACCENT_BLUE_ACTIVE = "#1e40af"
PAUSE_AMBER = "#d97706"
PAUSE_AMBER_HOVER = "#b45309"


@dataclasses.dataclass
class QueueTask:
    """Single queued transcoding job snapshot."""
    task_id: str
    source: str
    output: str
    resolution: str = "original"
    hdr_style: str = "balanced"
    quality: str = "standard"
    encoder: str = "auto"
    audio_effect: str = "original"
    multipass: bool = False
    audio_streams: list[int] | None = None
    subtitle_streams: list[int] | None = None
    summary: str = ""
    status: str = "pending"  # pending / running / paused / done / partial / failed / cancelled
    status_text: str = "等待中"
    error_message: str = ""
    result_output: str = ""


def format_queue_task_summary(output: str, hdr_style: str, quality: str, resolution: str,
                              audio_effect: str, audio_streams: list[int] | None,
                              subtitle_streams: list[int] | None) -> str:
    """Build a compact human-readable parameter summary for a queued task row."""
    ext = Path(output).suffix.lstrip(".").upper() or "MP4"
    res_tag = {"original": "原尺寸", "1440": "2K", "2160": "4K"}.get(resolution, resolution)
    parts = [ext, res_tag, STYLE_SHORT.get(hdr_style, hdr_style), QUALITY_SHORT.get(quality, quality)]
    if audio_effect == "surround":
        parts.append("环绕音效")
    elif audio_streams is not None:
        parts.append(f"音轨{len(audio_streams)}" if audio_streams else "无音轨")
    if subtitle_streams is not None:
        parts.append(f"字幕{len(subtitle_streams)}" if subtitle_streams else "无字幕")
    return " · ".join(parts)


def next_unique_output_path(source: Path, suffix: str, reserved: set[str] | None = None) -> str:
    """Generate a non-overwriting output path avoiding both existing files and queued targets."""
    reserved_norm = {os.path.normcase(os.path.abspath(p)) for p in (reserved or set())}
    ext = Path(suffix).suffix or ".mp4"
    stem_suffix = suffix[:-len(ext)] if suffix.lower().endswith(ext.lower()) else suffix
    dest = source.with_name(source.stem + stem_suffix + ext)
    count = 2
    while dest.exists() or os.path.normcase(os.path.abspath(str(dest))) in reserved_norm:
        dest = source.with_name(f"{source.stem}{stem_suffix}_{count}{ext}")
        count += 1
    return str(dest)


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


def format_track_summary(audio_tracks: list[dict], selected_audio: list[int] | None,
                         subtitle_tracks: list[dict], selected_subs: list[int] | None,
                         audio_effect: str = "original") -> str:
    """Build a concise 1-line summary of selected audio & embedded subtitle tracks."""
    if not audio_tracks:
        audio_part = "无音轨"
    else:
        sel_a = audio_tracks if selected_audio is None else [t for t in audio_tracks if t["stream_index"] in set(selected_audio)]
        if audio_effect == "surround":
            if sel_a:
                audio_part = f"音轨 {sel_a[0]['channel_label']} {sel_a[0]['codec']} → 双声道环绕"
            else:
                audio_part = f"音轨 0/{len(audio_tracks)}（环绕需至少勾选 1 条）"
        elif len(sel_a) == len(audio_tracks):
            audio_part = f"音轨 全部保留（{len(audio_tracks)} 条）"
        elif len(sel_a) == 0:
            audio_part = f"音轨 0/{len(audio_tracks)}（不保留音频）"
        else:
            audio_part = f"音轨 已选 {len(sel_a)}/{len(audio_tracks)} 条"

    if not subtitle_tracks:
        sub_part = "无内嵌字幕"
    else:
        sel_s = subtitle_tracks if selected_subs is None else [t for t in subtitle_tracks if t["stream_index"] in set(selected_subs)]
        if len(sel_s) == len(subtitle_tracks):
            sub_part = f"内嵌字幕 全部保留（{len(subtitle_tracks)} 条）"
        elif len(sel_s) == 0:
            sub_part = f"内嵌字幕 0/{len(subtitle_tracks)}（不保留）"
        else:
            sub_part = f"内嵌字幕 已选 {len(sel_s)}/{len(subtitle_tracks)} 条"

    return f"{audio_part}  ·  {sub_part}"


def compute_proportional_scale(vw: int, vh: int, dpi_scale: float,
                               base_w: float = 1000.0, base_h: float = 720.0) -> dict:
    """Compute edge-to-edge proportional UI scaling metrics with zero outer blank margins."""
    dpi = max(1.0, float(dpi_scale))
    lw = max(1.0, float(vw) / dpi)
    lh = max(1.0, float(vh) / dpi)
    raw = min(lw / base_w, lh / base_h)
    clamped = max(0.90, min(1.80, raw))
    scale = round(clamped * 20.0) / 20.0

    body_font = max(9, int(round(10.0 * scale)))
    sub_font = max(8, int(round(9.5 * scale)))
    title_font = max(15, int(round(18.0 * scale)))
    main_btn_font = max(10, int(round(11.0 * scale)))
    pad_x = max(14, int(round(22.0 * scale * dpi)))
    pad_y = max(10, int(round(14.0 * scale * dpi)))
    btn_pad_x = max(8, int(round(11.0 * scale)))
    btn_pad_y = max(4, int(round(6.0 * scale)))
    main_btn_pad_x = max(12, int(round(16.0 * scale)))
    main_btn_pad_y = max(6, int(round(8.0 * scale)))
    entry_pad = max(4, int(round(5.0 * scale)))
    progress_thickness = max(10, int(round(13.0 * scale)))
    tree_row_h = max(22, int(round(25.0 * scale * dpi)))

    return dict(
        scale=scale,
        body_font=body_font,
        sub_font=sub_font,
        title_font=title_font,
        main_btn_font=main_btn_font,
        pad_x=pad_x,
        pad_y=pad_y,
        btn_pad_x=btn_pad_x,
        btn_pad_y=btn_pad_y,
        main_btn_pad_x=main_btn_pad_x,
        main_btn_pad_y=main_btn_pad_y,
        entry_pad=entry_pad,
        progress_thickness=progress_thickness,
        tree_row_h=tree_row_h,
    )


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
        init_w = min(int(round(1020 * dpi_scale)), max(820, screen_w - 80))
        min_w = min(max(840, int(round(820 * dpi_scale))), max(760, screen_w - 60))
        min_h = min(int(round(560 * dpi_scale)), max(480, screen_h - 90))
        root.minsize(min_w, min_h)
        root.configure(bg=SURFACE_BG)
        self.dpi_scale = dpi_scale

        self.queue = queue.Queue()
        self.cancel = threading.Event()
        self.keep_partial = threading.Event()
        self.pause_event = threading.Event()
        self.running = False
        self.queue_running = False
        self.paused = False
        self.job_started = None
        self.pause_started_at = None
        self.paused_total = 0.0
        self.last_progress_info = None
        self._last_clock_tick = -1
        self._active_task_id: str | None = None
        self._active_queue_pos = (0, 0)
        self._last_preview_duration = 30.0
        self._current_is_preview = False
        self.last_output = ""

        self.task_queue: list[QueueTask] = []

        self._scrollbar_visible = False
        self._applied_scale = None
        self._syncing = False
        self._fullscreen = False
        self._last_wrap = 0
        self._last_box_size = None
        self._resize_after_id = None
        self._wrap_labels = []
        self._font_inputs = []
        self._dividers = []

        self.input = tk.StringVar()
        self.output = tk.StringVar()
        self.resolution = tk.StringVar(value="保持原尺寸")
        self.hdr_style = tk.StringVar(value="影院均衡高光（推荐电影·1000 nit）")
        self.quality = tk.StringVar(value="标准画质")
        self.encoder = tk.StringVar(value="自动（优先显卡加速）")
        self.audio_effect = tk.StringVar(value="原声（保留）")
        self.power_action = tk.StringVar(value="无动作")
        self.multipass = tk.BooleanVar(value=False)

        self.detected_encoders = None
        self.dv_tools = None
        self.gpu_filter_caps = None
        self.input_kind = "sdr"
        self.input_dv = None
        self.input_info = None
        self.input_duration = 0.0
        self.tracks_source_path: str | None = None
        self.audio_tracks_meta = []
        self.subtitle_tracks_meta = []
        self.selected_audio_indices = None
        self.selected_subtitle_indices = None

        self.track_summary = tk.StringVar(value="选择视频后可勾选保留音轨与内嵌字幕")
        self.queue_summary = tk.StringVar(value="暂无待转任务，可直接转换或加入列表")
        self.status = tk.StringVar(value="选择 SDR、杜比视界或 HDR 视频开始处理。")
        self.info = tk.StringVar(value="支持 MP4、MOV、MKV 等本地视频；自动识别 SDR 与杜比视界 Profile 5 / 7 / 8.1 / 8.4。")
        self.encoder_status = tk.StringVar(value="正在检测显卡硬件加速与杜比视界组件…")
        self.progress = tk.DoubleVar()
        self.percent_text = tk.StringVar(value="0.0%")
        self.detail = tk.StringVar(value="")
        self.controls = []
        self.queue_edit_buttons = []

        self.input.trace_add("write", self._on_input_var_changed)

        style = ttk.Style(root)
        style.theme_use("clam")
        self.style = style
        style.configure("TFrame", background=SURFACE_BG)
        style.configure("Card.TFrame", background=SURFACE_BG)
        style.configure("Panel.TFrame", background=PANEL_BG)
        style.configure("TLabel", background=SURFACE_BG, foreground=TEXT_PRIMARY, font=("Microsoft YaHei UI", 10))
        style.configure("Title.TLabel", background=SURFACE_BG, foreground=TEXT_PRIMARY, font=("Microsoft YaHei UI", 18, "bold"))
        style.configure("Sub.TLabel", background=SURFACE_BG, foreground=TEXT_MUTED, font=("Microsoft YaHei UI", 9))
        style.configure("Percent.TLabel", background=SURFACE_BG, foreground=TEXT_PRIMARY, font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Section.TLabel", background=SURFACE_BG, foreground=TEXT_PRIMARY, font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=(11, 6))
        style.configure("Small.TButton", font=("Microsoft YaHei UI", 9), padding=(8, 4))
        style.configure(
            "Main.TButton",
            font=("Microsoft YaHei UI", 11, "bold"),
            padding=(16, 8),
            foreground="#ffffff",
            background=ACCENT_BLUE,
            bordercolor=ACCENT_BLUE,
            lightcolor=ACCENT_BLUE,
            darkcolor=ACCENT_BLUE,
            focuscolor="#93c5fd",
        )
        style.map(
            "Main.TButton",
            background=[("disabled", "#cbd5e1"), ("pressed", ACCENT_BLUE_ACTIVE), ("active", ACCENT_BLUE_HOVER)],
            foreground=[("disabled", "#64748b"), ("active", "#ffffff")],
            bordercolor=[("disabled", "#cbd5e1"), ("pressed", ACCENT_BLUE_ACTIVE), ("active", ACCENT_BLUE_HOVER)],
            lightcolor=[("disabled", "#cbd5e1"), ("pressed", ACCENT_BLUE_ACTIVE), ("active", ACCENT_BLUE_HOVER)],
            darkcolor=[("disabled", "#cbd5e1"), ("pressed", ACCENT_BLUE_ACTIVE), ("active", ACCENT_BLUE_HOVER)],
        )
        style.configure(
            "Paused.TButton",
            font=("Microsoft YaHei UI", 10, "bold"),
            padding=(11, 6),
            foreground="#ffffff",
            background=PAUSE_AMBER,
            bordercolor=PAUSE_AMBER,
            lightcolor=PAUSE_AMBER,
            darkcolor=PAUSE_AMBER,
        )
        style.map(
            "Paused.TButton",
            background=[("disabled", "#cbd5e1"), ("active", PAUSE_AMBER_HOVER)],
            foreground=[("disabled", "#64748b"), ("active", "#ffffff")],
            bordercolor=[("disabled", "#cbd5e1"), ("active", PAUSE_AMBER_HOVER)],
            lightcolor=[("disabled", "#cbd5e1"), ("active", PAUSE_AMBER_HOVER)],
            darkcolor=[("disabled", "#cbd5e1"), ("active", PAUSE_AMBER_HOVER)],
        )
        style.configure("TEntry", padding=5, fieldbackground=SURFACE_BG)
        style.configure("TCombobox", padding=4, fieldbackground=SURFACE_BG)
        style.map("TCombobox", fieldbackground=[("readonly", SURFACE_BG), ("disabled", "#f1f5f9")])
        style.configure("TCheckbutton", background=SURFACE_BG, foreground=TEXT_PRIMARY, font=("Microsoft YaHei UI", 10))
        style.map("TCheckbutton", background=[("active", SURFACE_BG)])
        style.configure(
            "Accent.Horizontal.TProgressbar",
            troughcolor="#e5eaf1",
            background=ACCENT_BLUE,
            bordercolor=BORDER_COLOR,
            lightcolor=ACCENT_BLUE,
            darkcolor=ACCENT_BLUE,
            thickness=13,
        )
        style.configure(
            "Queue.Treeview",
            background=SURFACE_BG,
            fieldbackground=SURFACE_BG,
            foreground=TEXT_PRIMARY,
            bordercolor=BORDER_COLOR,
            rowheight=int(round(25 * dpi_scale)),
            font=("Microsoft YaHei UI", 9),
        )
        style.configure(
            "Queue.Treeview.Heading",
            background=PANEL_BG,
            foreground=TEXT_PRIMARY,
            font=("Microsoft YaHei UI", 9, "bold"),
            relief="flat",
        )
        style.map(
            "Queue.Treeview",
            background=[("selected", "#dbeafe")],
            foreground=[("selected", TEXT_PRIMARY)],
        )

        # Edge-to-edge full-bleed viewport (zero outer gray margin in windowed/maximized/fullscreen).
        viewport = tk.Canvas(root, bg=SURFACE_BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(root, orient="vertical", command=viewport.yview)
        viewport.pack(side="left", fill="both", expand=True)
        viewport.configure(yscrollcommand=scrollbar.set)

        box = ttk.Frame(viewport, style="Card.TFrame", padding=(22, 14, 22, 14))
        window_id = viewport.create_window((0, 0), window=box, anchor="nw")
        self.viewport = viewport
        self.scrollbar = scrollbar
        self.box = box
        self._window_id = window_id

        def wheel(event):
            widget = event.widget
            if widget == self.queue_tree:
                return
            if self._scrollbar_visible:
                viewport.yview_scroll(-int(event.delta / 120), "units")
        root.bind_all("<MouseWheel>", wheel)
        root.bind("<F11>", self.toggle_fullscreen)
        root.bind("<Escape>", lambda _e: self.exit_fullscreen())

        # Header
        header_row = ttk.Frame(box)
        header_row.pack(fill="x")
        ttk.Label(header_row, text="把画质和音效，一起写进视频", style="Title.TLabel").pack(side="left", anchor="w")
        ttk.Label(header_row, text=f"v{VERSION} · 按 F11 全屏", style="Sub.TLabel").pack(side="right", anchor="s", pady=(0, 3))
        ttk.Label(box, text="SDR 转 HDR10 · 杜比视界 (DV) 转 HDR10 · 已有 HDR10 无损加音效。", style="Sub.TLabel").pack(anchor="w", pady=(2, 10))

        # File path rows
        self.path_row(box, "原始视频", self.input, self.pick_input, "选择视频")
        info_lbl = ttk.Label(box, textvariable=self.info, style="Sub.TLabel")
        info_lbl.pack(anchor="w", pady=(1, 6))
        self.path_row(box, "导出文件", self.output, self.pick_output, "保存到…")

        div_top = tk.Frame(box, bg=DIVIDER_BG, height=1)
        div_top.pack(fill="x", pady=(4, 8))
        self._dividers.append(("top", div_top))

        # 3x2 Options grid (6 symmetric controls including post-job power action)
        row = ttk.Frame(box)
        row.pack(fill="x", pady=(0, 6))
        row.columnconfigure(0, weight=10, uniform="opt")
        row.columnconfigure(1, weight=12, uniform="opt")
        row.columnconfigure(2, weight=11, uniform="opt")

        ttk.Label(row, text="输出尺寸").grid(row=0, column=0, sticky="w", padx=(0, 14))
        c1 = ttk.Combobox(row, textvariable=self.resolution, values=list(RESOLUTION_LABELS.keys()), state="readonly", width=16)
        c1.grid(row=1, column=0, sticky="ew", padx=(0, 14), pady=(3, 0))

        ttk.Label(row, text="HDR 风格").grid(row=0, column=1, sticky="w", padx=(0, 14))
        c_style = ttk.Combobox(row, textvariable=self.hdr_style, values=list(STYLE_LABELS.keys()), state="readonly", width=18)
        c_style.grid(row=1, column=1, sticky="ew", padx=(0, 14), pady=(3, 0))

        ttk.Label(row, text="音效").grid(row=0, column=2, sticky="w")
        c4 = ttk.Combobox(row, textvariable=self.audio_effect, values=list(AUDIO_EFFECT_LABELS.keys()), state="readonly", width=16)
        c4.grid(row=1, column=2, sticky="ew", pady=(3, 0))

        ttk.Label(row, text="转换方式").grid(row=2, column=0, sticky="w", padx=(0, 14), pady=(8, 0))
        c2 = ttk.Combobox(row, textvariable=self.quality, values=list(QUALITY_LABELS.keys()), state="readonly", width=16)
        c2.grid(row=3, column=0, sticky="ew", padx=(0, 14), pady=(3, 0))

        ttk.Label(row, text="编码器").grid(row=2, column=1, sticky="w", padx=(0, 14), pady=(8, 0))
        c3 = ttk.Combobox(row, textvariable=self.encoder, values=list(ENCODER_LABELS.keys()), state="readonly", width=16)
        c3.grid(row=3, column=1, sticky="ew", padx=(0, 14), pady=(3, 0))

        ttk.Label(row, text="完成后动作").grid(row=2, column=2, sticky="w", pady=(8, 0))
        c_power = ttk.Combobox(row, textvariable=self.power_action, values=list(POWER_LABELS.keys()), state="readonly", width=16)
        c_power.grid(row=3, column=2, sticky="ew", pady=(3, 0))

        self.option_grid = row
        self.resolution_combo, self.style_combo, self.quality_combo, self.encoder_combo, self.audio_combo = c1, c_style, c2, c3, c4
        self.power_combo = c_power
        self.controls += [c1, c_style, c2, c3, c4]
        self._font_inputs += [c1, c_style, c2, c3, c4, c_power]

        # Track selector + NVENC multipass row
        track_row = ttk.Frame(box)
        track_row.pack(fill="x", pady=(6, 4))
        ttk.Label(track_row, text="轨道保留", width=8).pack(side="left")
        track_summary_lbl = ttk.Label(track_row, textvariable=self.track_summary, style="Sub.TLabel")
        track_summary_lbl.pack(side="left", fill="x", expand=True, padx=(8, 10))
        c_mp = ttk.Checkbutton(track_row, text="NVENC 双遍分析 (更慢)", variable=self.multipass, command=self.update_encoding_note)
        c_mp.pack(side="right", padx=(10, 0))
        self.tracks_button = ttk.Button(track_row, text="勾选音轨 / 字幕…", command=self.open_track_selector, state="disabled")
        self.tracks_button.pack(side="right")
        self.multipass_check = c_mp
        self.controls.append(c_mp)
        self.track_summary_label = track_summary_lbl

        self.encoding_note = tk.StringVar()
        enc_status_lbl = ttk.Label(box, textvariable=self.encoder_status, style="Sub.TLabel")
        enc_status_lbl.pack(anchor="w", pady=(2, 1))
        enc_note_lbl = ttk.Label(box, textvariable=self.encoding_note, style="Sub.TLabel")
        enc_note_lbl.pack(anchor="w", pady=(0, 2))
        c_style.bind("<<ComboboxSelected>>", lambda e: self.update_encoding_note())
        c2.bind("<<ComboboxSelected>>", lambda e: self.update_encoding_note())
        c3.bind("<<ComboboxSelected>>", lambda e: self.update_encoding_note())
        c4.bind("<<ComboboxSelected>>", lambda e: self.update_track_summary())

        div_mid = tk.Frame(box, bg=DIVIDER_BG, height=1)
        div_mid.pack(fill="x", pady=(4, 8))
        self._dividers.append(("mid", div_mid))

        # Action bar: direct convert + custom preview + queue controls on left; pause + stop&keep + cancel on right
        actions = ttk.Frame(box)
        actions.pack(fill="x")
        self.actions_frame = actions
        self.start_button = ttk.Button(actions, text="导出完整 HDR", style="Main.TButton", command=self.start)
        self.start_button.pack(side="left")
        self.preview_button = ttk.Button(actions, text="自定义试转…", command=self.open_preview_dialog)
        self.preview_button.pack(side="left", padx=(8, 0))
        self.add_queue_button = ttk.Button(actions, text="加入任务列表", command=self.add_to_queue)
        self.add_queue_button.pack(side="left", padx=(8, 0))
        self.start_queue_button = ttk.Button(actions, text="开始队列", command=self.start_queue, state="disabled")
        self.start_queue_button.pack(side="left", padx=(8, 0))

        self.stop_button = ttk.Button(actions, text="取消", command=self.stop, state="disabled")
        self.stop_button.pack(side="right")
        self.save_partial_button = ttk.Button(actions, text="停止并保留", command=self.stop_and_keep, state="disabled")
        self.save_partial_button.pack(side="right", padx=(0, 8))
        self.pause_button = ttk.Button(actions, text="暂停", command=self.toggle_pause, state="disabled")
        self.pause_button.pack(side="right", padx=(0, 8))

        # Progress bar + status
        progress_row = ttk.Frame(box)
        progress_row.pack(fill="x", pady=(10, 4))
        ttk.Progressbar(
            progress_row,
            variable=self.progress,
            maximum=100,
            style="Accent.Horizontal.TProgressbar",
        ).pack(side="left", fill="x", expand=True)
        ttk.Label(progress_row, textvariable=self.percent_text, style="Percent.TLabel", width=8, anchor="e").pack(side="right", padx=(10, 0))

        status_lbl = ttk.Label(box, textvariable=self.status)
        status_lbl.pack(anchor="w")
        detail_lbl = ttk.Label(box, textvariable=self.detail, style="Sub.TLabel")
        detail_lbl.pack(anchor="w", pady=(2, 6))

        # Expandable Batch Task Queue Panel (fills remaining height cleanly in windowed AND fullscreen!)
        queue_panel = ttk.Frame(box)
        queue_panel.pack(fill="both", expand=True, pady=(2, 6))
        self.queue_panel = queue_panel

        q_hdr = ttk.Frame(queue_panel)
        q_hdr.pack(fill="x", pady=(0, 4))
        ttk.Label(q_hdr, text="任务列表", style="Section.TLabel").pack(side="left")
        ttk.Label(q_hdr, textvariable=self.queue_summary, style="Sub.TLabel").pack(side="left", padx=(10, 0))

        btn_clear_all = ttk.Button(q_hdr, text="清空列表", style="Small.TButton", command=self.clear_queue)
        btn_clear_all.pack(side="right")
        btn_clear_done = ttk.Button(q_hdr, text="清理已完成", style="Small.TButton", command=self.clear_completed_tasks)
        btn_clear_done.pack(side="right", padx=(0, 5))
        btn_remove = ttk.Button(q_hdr, text="移除", style="Small.TButton", command=self.remove_selected_task)
        btn_remove.pack(side="right", padx=(0, 5))
        btn_down = ttk.Button(q_hdr, text="下移", style="Small.TButton", command=lambda: self.move_selected_task(1))
        btn_down.pack(side="right", padx=(0, 5))
        btn_up = ttk.Button(q_hdr, text="上移", style="Small.TButton", command=lambda: self.move_selected_task(-1))
        btn_up.pack(side="right", padx=(0, 5))
        btn_batch = ttk.Button(q_hdr, text="批量添加视频…", style="Small.TButton", command=self.batch_add_to_queue)
        btn_batch.pack(side="right", padx=(0, 8))
        self.queue_edit_buttons = [btn_batch, btn_up, btn_down, btn_remove, btn_clear_done, btn_clear_all]

        tree_border = tk.Frame(queue_panel, bg=BORDER_COLOR, padx=1, pady=1)
        tree_border.pack(fill="both", expand=True)
        tree_wrap = ttk.Frame(tree_border)
        tree_wrap.pack(fill="both", expand=True)
        cols = ("idx", "file", "params", "state")
        tree = ttk.Treeview(tree_wrap, columns=cols, show="headings", height=3, style="Queue.Treeview", selectmode="browse")
        tree.heading("idx", text="#", anchor="center")
        tree.heading("file", text="视频文件", anchor="w")
        tree.heading("params", text="输出与参数", anchor="w")
        tree.heading("state", text="状态", anchor="w")
        tree.column("idx", width=44, minwidth=36, stretch=False, anchor="center")
        tree.column("file", width=360, minwidth=180, stretch=True, anchor="w")
        tree.column("params", width=290, minwidth=180, stretch=True, anchor="w")
        tree.column("state", width=140, minwidth=110, stretch=False, anchor="w")
        tree_sb = ttk.Scrollbar(tree_wrap, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=tree_sb.set)
        tree.pack(side="left", fill="both", expand=True)
        tree_sb.pack(side="right", fill="y")
        self.queue_tree = tree

        # Footer
        bottom_row = ttk.Frame(box)
        bottom_row.pack(fill="x", pady=(2, 0))
        self.open_button = ttk.Button(bottom_row, text="打开导出文件夹", command=self.open_folder)
        self.open_button.pack(side="left")
        footer_lbl = ttk.Label(
            bottom_row,
            text="同封装默认导出（MKV→MKV / MP4→MP4）；本工具全程离线处理，不会自动上传或发布。",
            style="Sub.TLabel",
        )
        footer_lbl.pack(side="left", padx=(12, 0))
        self.footer_label = footer_lbl

        self._wrap_labels = [info_lbl, enc_status_lbl, enc_note_lbl, status_lbl, detail_lbl]

        self._apply_proportional_metrics(compute_proportional_scale(init_w, int(round(720 * dpi_scale)), dpi_scale))
        self._apply_wrap_for_width(init_w, int(round(22 * dpi_scale)))

        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.poll)
        self.update_encoding_note()
        self.refresh_queue_ui()
        self.detect_encoders_async()

        viewport.itemconfigure(window_id, width=init_w)
        root.update_idletasks()
        req_h = box.winfo_reqheight()
        init_h = min(max(int(round(680 * dpi_scale)), req_h + int(round(12 * dpi_scale))), max(540, screen_h - 80))
        root.geometry(f"{init_w}x{init_h}")

        box.bind("<Configure>", self._schedule_sync_layout)
        viewport.bind("<Configure>", self._schedule_sync_layout)
        self.sync_layout()

        if len(sys.argv) > 1 and Path(sys.argv[1]).is_file():
            root.after(200, lambda: self.set_input(sys.argv[1]))

    def _on_input_var_changed(self, *_args):
        cur = self.input.get().strip().strip('"')
        if self.tracks_source_path is not None and cur != self.tracks_source_path:
            # User manually edited/pasted a different path: invalidate stale track selections immediately.
            self.tracks_source_path = None
            self.input_info = None
            self.input_duration = 0.0
            self.audio_tracks_meta = []
            self.subtitle_tracks_meta = []
            self.selected_audio_indices = None
            self.selected_subtitle_indices = None
            self.track_summary.set("路径已手动修改，将默认保留全部兼容音轨与内嵌字幕")
            if hasattr(self, "tracks_button"):
                self.tracks_button.configure(state="disabled")

    def _apply_wrap_for_width(self, vw: int, pad_x: int) -> bool:
        wrap = max(460, vw - 2 * pad_x - 16)
        footer_wrap = max(320, vw - 2 * pad_x - int(round(190 * (self._applied_scale or 1.0) * self.dpi_scale)))
        if abs(wrap - self._last_wrap) < 6:
            return False
        self._last_wrap = wrap
        for lbl in self._wrap_labels:
            lbl.configure(wraplength=wrap)
        self.footer_label.configure(wraplength=footer_wrap)
        return True

    def _apply_proportional_metrics(self, metrics: dict) -> bool:
        scale = metrics["scale"]
        if self._applied_scale is not None and abs(scale - self._applied_scale) < 1e-6:
            return False
        self._applied_scale = scale
        body_font = ("Microsoft YaHei UI", metrics["body_font"])
        sub_font = ("Microsoft YaHei UI", metrics["sub_font"])
        bold_font = ("Microsoft YaHei UI", metrics["body_font"], "bold")

        self.style.configure("TLabel", font=body_font)
        self.style.configure("Title.TLabel", font=("Microsoft YaHei UI", metrics["title_font"], "bold"))
        self.style.configure("Sub.TLabel", font=sub_font)
        self.style.configure("Percent.TLabel", font=bold_font)
        self.style.configure("Section.TLabel", font=bold_font)
        self.style.configure("TCheckbutton", font=body_font)
        self.style.configure("TButton", font=body_font, padding=(metrics["btn_pad_x"], metrics["btn_pad_y"]))
        self.style.configure(
            "Small.TButton",
            font=sub_font,
            padding=(max(6, int(round(8 * scale))), max(3, int(round(4 * scale)))),
        )
        self.style.configure(
            "Main.TButton",
            font=("Microsoft YaHei UI", metrics["main_btn_font"], "bold"),
            padding=(metrics["main_btn_pad_x"], metrics["main_btn_pad_y"]),
        )
        self.style.configure("Paused.TButton", font=bold_font, padding=(metrics["btn_pad_x"], metrics["btn_pad_y"]))
        self.style.configure("TEntry", padding=metrics["entry_pad"])
        self.style.configure("TCombobox", padding=max(3, metrics["entry_pad"] - 1))
        self.style.configure("Accent.Horizontal.TProgressbar", thickness=metrics["progress_thickness"])
        self.style.configure("Queue.Treeview", font=sub_font, rowheight=metrics["tree_row_h"])
        self.style.configure("Queue.Treeview.Heading", font=("Microsoft YaHei UI", metrics["sub_font"], "bold"))
        self.root.option_add("*TCombobox*Listbox.font", body_font)
        for widget in self._font_inputs:
            widget.configure(font=body_font)

        self.box.configure(padding=(metrics["pad_x"], metrics["pad_y"], metrics["pad_x"], metrics["pad_y"]))
        div_pad = max(4, int(round(6 * scale)))
        for _role, div in self._dividers:
            div.pack_configure(pady=(div_pad, div_pad + 2))
        self._last_wrap = 0
        return True

    def _schedule_sync_layout(self, _event=None):
        if self._syncing:
            return
        if self._resize_after_id is not None:
            try:
                self.root.after_cancel(self._resize_after_id)
            except tk.TclError:
                pass
        self._resize_after_id = self.root.after(20, self.sync_layout)

    def toggle_fullscreen(self, _event=None):
        self._fullscreen = not self._fullscreen
        self.root.attributes("-fullscreen", self._fullscreen)
        self.root.after(30, self.sync_layout)

    def exit_fullscreen(self, _event=None):
        if self._fullscreen:
            self._fullscreen = False
            self.root.attributes("-fullscreen", False)
            self.root.after(30, self.sync_layout)

    def sync_layout(self, _event=None):
        self._resize_after_id = None
        if self._syncing:
            return
        self._syncing = True
        try:
            vw = self.viewport.winfo_width()
            vh = self.viewport.winfo_height()
            if vw <= 1 or vh <= 1:
                return
            metrics = compute_proportional_scale(vw, vh, self.dpi_scale)
            changed = self._apply_proportional_metrics(metrics)
            if self._apply_wrap_for_width(vw, metrics["pad_x"]):
                changed = True
            self.viewport.itemconfigure(self._window_id, width=vw)
            if changed:
                self.box.update_idletasks()
            req_h = self.box.winfo_reqheight()
            # Auto-fit guard: if scaled content overflows viewport height when scale > 1.0, gently step down scale so fullscreen never overflows.
            if req_h > vh + 2 and metrics["scale"] > 1.0:
                ratio = max(0.95, metrics["scale"] * (vh / max(1, req_h)) * 0.97)
                adjusted_base_h = (vh / self.dpi_scale) / max(1.0, ratio)
                metrics = compute_proportional_scale(vw, vh, self.dpi_scale, base_h=max(720.0, adjusted_base_h))
                self._apply_proportional_metrics(metrics)
                self._apply_wrap_for_width(vw, metrics["pad_x"])
                self.box.update_idletasks()
                req_h = self.box.winfo_reqheight()

            target_h = max(vh, req_h)
            box_size = (vw, target_h)
            if box_size != self._last_box_size:
                self.viewport.coords(self._window_id, 0, 0)
                self.viewport.itemconfigure(self._window_id, width=vw, height=target_h)
                self._last_box_size = box_size

            # Keep column proportions balanced across window widths.
            idx_w = max(40, int(round(44 * metrics["scale"] * self.dpi_scale)))
            state_w = max(120, int(round(145 * metrics["scale"] * self.dpi_scale)))
            rem_w = max(320, vw - 2 * metrics["pad_x"] - idx_w - state_w - 28)
            self.queue_tree.column("idx", width=idx_w)
            self.queue_tree.column("file", width=int(rem_w * 0.52))
            self.queue_tree.column("params", width=int(rem_w * 0.48))
            self.queue_tree.column("state", width=state_w)

            self.viewport.configure(scrollregion=(0, 0, max(1, vw), target_h))
            need_scroll = vh > 1 and req_h > vh + 2
            if need_scroll != self._scrollbar_visible:
                if need_scroll:
                    self.scrollbar.pack(side="right", fill="y", before=self.viewport)
                    self._scrollbar_visible = True
                else:
                    self.scrollbar.pack_forget()
                    self.viewport.yview_moveto(0.0)
                    self._scrollbar_visible = False
        finally:
            self._syncing = False

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
                notes.append("影院均衡高光：Gamma 2.2 通透暗部（纯黑 0 nit）+ 无凹陷云层雪地高光层次 + 等色度肤色保护，峰值直达 1000 nit。")
            elif style_key == "natural":
                notes.append("柔和暗室保护：BT.1886 (Gamma 2.4) 全黑暗室基准与 450 nits 峰值，适合全黑环境观影与保守高光控制。")
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
            notes.append("⚠ 画面无损编码：输出体积常为原片 5～15 倍（1080p 电影可达数十至上百 GB），导出后默认逐帧校验（校验时可点“停止并保留”跳过）；日常收藏建议用“标准画质”或“精细画质”。")
        elif self.multipass.get() and enc_key in ("auto", "nvenc"):
            notes.append("NVENC 双遍分析已启用：码率分配更精细，耗时约增加 30%～80%。")
        self.encoding_note.set("\n".join(notes))
        self.update_multipass_state()

    def path_row(self, parent, title, var, command, label):
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(0, 5))
        ttk.Label(row, text=title, width=8).pack(side="left")
        entry = ttk.Entry(row, textvariable=var)
        entry.pack(side="left", fill="x", expand=True, padx=(8, 0))
        button = ttk.Button(row, text=label, command=command)
        button.pack(side="right", padx=(9, 0))
        self.controls += [entry, button]
        self._font_inputs.append(entry)

    def pick_input(self):
        p = filedialog.askopenfilename(title="选择 SDR／杜比视界／HDR 视频", filetypes=[("视频", "*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.ts"), ("所有文件", "*.*")])
        if p:
            self.set_input(p)

    def _reserved_queue_outputs(self) -> set[str]:
        return {t.output for t in self.task_queue if t.status in ("pending", "running", "paused")}

    def _next_output_path(self, source: Path, suffix: str) -> str:
        return next_unique_output_path(source, suffix, self._reserved_queue_outputs())

    def update_track_summary(self):
        if self.input_info is None:
            self.tracks_button.configure(state="disabled")
            return
        audio_effect = AUDIO_EFFECT_LABELS[self.audio_effect.get()]
        self.track_summary.set(
            format_track_summary(
                self.audio_tracks_meta,
                self.selected_audio_indices,
                self.subtitle_tracks_meta,
                self.selected_subtitle_indices,
                audio_effect=audio_effect,
            )
        )
        has_tracks = bool(self.audio_tracks_meta or self.subtitle_tracks_meta)
        self.tracks_button.configure(state="normal" if (not self.running and has_tracks) else "disabled")

    def open_track_selector(self):
        if self.running or not (self.audio_tracks_meta or self.subtitle_tracks_meta):
            return
        dlg = tk.Toplevel(self.root)
        dlg.title("勾选保留的音轨与内嵌字幕")
        dlg.configure(bg=SURFACE_BG)
        dlg.transient(self.root)
        dlg.grab_set()
        w = int(round(620 * self.dpi_scale))
        h = int(round(460 * self.dpi_scale))
        x = self.root.winfo_rootx() + max(20, (self.root.winfo_width() - w) // 2)
        y = self.root.winfo_rooty() + max(20, (self.root.winfo_height() - h) // 2)
        dlg.geometry(f"{w}x{h}+{x}+{y}")
        dlg.minsize(int(round(500 * self.dpi_scale)), int(round(360 * self.dpi_scale)))

        card = ttk.Frame(dlg, style="Card.TFrame", padding=(18, 14, 18, 14))
        card.pack(fill="both", expand=True)

        ttk.Label(card, text="勾选需要保留的音轨与内嵌字幕（取消勾选将丢弃）", style="TLabel").pack(anchor="w")
        audio_effect = AUDIO_EFFECT_LABELS[self.audio_effect.get()]
        if audio_effect == "surround":
            ttk.Label(
                card,
                text="提示：当前为“虚拟环绕（双声道）”模式，将仅对勾选的第一条音轨进行双声道环绕处理。",
                style="Sub.TLabel",
            ).pack(anchor="w", pady=(3, 0))

        scroll_wrap = ttk.Frame(card)
        scroll_wrap.pack(fill="both", expand=True, pady=(10, 10))
        canvas = tk.Canvas(scroll_wrap, bg=SURFACE_BG, highlightthickness=0)
        sb = ttk.Scrollbar(scroll_wrap, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas, style="Card.TFrame")
        win_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb_visible = [False]

        def sync_dlg(_e=None):
            cw = canvas.winfo_width()
            ch = canvas.winfo_height()
            if cw > 1:
                canvas.itemconfigure(win_id, width=cw)
            req_h = inner.winfo_reqheight()
            canvas.configure(scrollregion=(0, 0, max(1, cw), req_h))
            need = ch > 1 and req_h > ch + 2
            if need != sb_visible[0]:
                if need:
                    sb.pack(side="right", fill="y", before=canvas)
                else:
                    sb.pack_forget()
                    canvas.yview_moveto(0.0)
                sb_visible[0] = need
        inner.bind("<Configure>", sync_dlg)
        canvas.bind("<Configure>", sync_dlg)

        def on_wheel(event):
            if sb_visible[0]:
                canvas.yview_scroll(-int(event.delta / 120), "units")
        dlg.bind("<MouseWheel>", on_wheel)

        audio_vars = []
        sub_vars = []
        sel_a_set = set(self.selected_audio_indices) if self.selected_audio_indices is not None else {t["stream_index"] for t in self.audio_tracks_meta}
        sel_s_set = set(self.selected_subtitle_indices) if self.selected_subtitle_indices is not None else {t["stream_index"] for t in self.subtitle_tracks_meta}

        def build_section(title_text, tracks, sel_set, store_list, is_sub=False):
            hdr = ttk.Frame(inner)
            hdr.pack(fill="x", pady=(6, 4))
            ttk.Label(hdr, text=title_text, style="Percent.TLabel").pack(side="left")
            if tracks:
                ttk.Button(hdr, text="全不选", style="Small.TButton", command=lambda: [v.set(False) for _, v in store_list]).pack(side="right")
                ttk.Button(hdr, text="全选", style="Small.TButton", command=lambda: [v.set(True) for _, v in store_list]).pack(side="right", padx=(0, 6))
                for t in tracks:
                    var = tk.BooleanVar(value=(t["stream_index"] in sel_set))
                    if is_sub:
                        tag = "  [MP4/MKV]" if t.get("mp4_supported") else "  [仅 MKV 支持]"
                    else:
                        tag = "  [MP4/MKV 直拷]" if t.get("mp4_copyable") else "  [建议 MKV 直拷]"
                    cb = ttk.Checkbutton(inner, text=t["label"] + tag, variable=var)
                    cb.pack(anchor="w", padx=(8, 4), pady=2)
                    store_list.append((t, var))
            else:
                ttk.Label(inner, text="（原片未包含此类轨道）", style="Sub.TLabel").pack(anchor="w", padx=(8, 4), pady=(0, 4))

        build_section(f"音轨（共 {len(self.audio_tracks_meta)} 条）", self.audio_tracks_meta, sel_a_set, audio_vars, is_sub=False)
        tk.Frame(inner, bg=DIVIDER_BG, height=1).pack(fill="x", pady=(10, 6))
        build_section(f"内嵌字幕（共 {len(self.subtitle_tracks_meta)} 条）", self.subtitle_tracks_meta, sel_s_set, sub_vars, is_sub=True)

        btns = ttk.Frame(card)
        btns.pack(fill="x", pady=(4, 0))

        def confirm():
            self.selected_audio_indices = [t["stream_index"] for t, v in audio_vars if v.get()]
            self.selected_subtitle_indices = [t["stream_index"] for t, v in sub_vars if v.get()]
            self.update_track_summary()
            dlg.destroy()

        ttk.Button(btns, text="取消", command=dlg.destroy).pack(side="right")
        ttk.Button(btns, text="确定", style="Main.TButton", command=confirm).pack(side="right", padx=(0, 8))
        dlg.bind("<Escape>", lambda _e: dlg.destroy())
        dlg.wait_window()

    def open_preview_dialog(self):
        if self.running:
            return
        source = self.input.get().strip().strip('"')
        output = self.output.get().strip().strip('"')
        if not source or not output:
            messagebox.showinfo("选择视频", "请先选择输入视频和导出位置。")
            return

        total_dur = self.input_duration if (self.tracks_source_path == source and self.input_duration > 0) else 0.0
        def_start, def_dur = default_preview_window(total_dur, self._last_preview_duration)

        dlg = tk.Toplevel(self.root)
        dlg.title("自定义试转片段")
        dlg.configure(bg=SURFACE_BG)
        dlg.transient(self.root)
        dlg.grab_set()
        w = int(round(600 * self.dpi_scale))
        h = int(round(360 * self.dpi_scale))
        x = self.root.winfo_rootx() + max(20, (self.root.winfo_width() - w) // 2)
        y = self.root.winfo_rooty() + max(20, (self.root.winfo_height() - h) // 2)
        dlg.geometry(f"{w}x{h}+{x}+{y}")
        dlg.minsize(int(round(480 * self.dpi_scale)), int(round(320 * self.dpi_scale)))

        card = ttk.Frame(dlg, style="Card.TFrame", padding=(20, 16, 20, 16))
        card.pack(fill="both", expand=True)

        ttk.Label(card, text="选择要试转的视频片段（默认从视频正中截取）", style="Section.TLabel").pack(anchor="w")
        dur_text = (
            f"当前视频：{Path(source).name}  ·  总时长 {format_duration_clock(total_dur)}（{total_dur:.1f} 秒）"
            if total_dur > 0 else f"当前视频：{Path(source).name}  ·  总时长未提供"
        )
        ttk.Label(card, text=dur_text, style="Sub.TLabel", wraplength=w - 50).pack(anchor="w", pady=(4, 12))

        start_var = tk.StringVar(value=format_duration_clock(def_start))
        dur_var = tk.StringVar(value=f"{def_dur:g}")
        range_summary = tk.StringVar()
        slider_var = tk.DoubleVar(value=def_start)
        updating = [False]

        def refresh_summary(*_a):
            try:
                st = parse_timecode_seconds(start_var.get())
                du = float(dur_var.get().strip())
                if not math.isfinite(du) or du <= 0:
                    raise ValueError("时长需大于 0")
                if total_dur > 0 and st >= total_dur:
                    range_summary.set(f"提示：起点 {format_duration_clock(st)} 超出视频总时长 {format_duration_clock(total_dur)}。")
                    return
                end_t = min(total_dur, st + du) if total_dur > 0 else (st + du)
                actual_du = max(0.0, end_t - st) if total_dur > 0 else du
                range_summary.set(
                    f"将试转片段：{format_duration_clock(st)} ～ {format_duration_clock(end_t)}（共 {actual_du:.1f} 秒）"
                )
                if not updating[0] and total_dur > 0:
                    updating[0] = True
                    slider_var.set(min(total_dur, st))
                    updating[0] = False
            except ValueError as err:
                range_summary.set(f"请输入有效格式（如 01:30 或 90）：{err}")

        if total_dur > 1.0:
            ttk.Label(card, text="拖动时间轴定位试转起点：").pack(anchor="w")
            max_start = max(0.0, total_dur - 1.0)
            def on_slide(val):
                if updating[0]:
                    return
                updating[0] = True
                start_var.set(format_duration_clock(float(val)))
                updating[0] = False
                refresh_summary()
            slider = ttk.Scale(card, from_=0.0, to=max_start, variable=slider_var, orient="horizontal", command=on_slide)
            slider.pack(fill="x", pady=(4, 8))

            preset_row = ttk.Frame(card)
            preset_row.pack(fill="x", pady=(0, 10))
            ttk.Label(preset_row, text="快速定位：", style="Sub.TLabel").pack(side="left")
            def jump_frac(frac):
                try:
                    cur_du = max(1.0, float(dur_var.get().strip()))
                except ValueError:
                    cur_du = 30.0
                if frac <= 0:
                    st = 0.0
                else:
                    st = max(0.0, min(max_start, total_dur * frac - cur_du * 0.5))
                updating[0] = True
                slider_var.set(st)
                start_var.set(format_duration_clock(st))
                updating[0] = False
                refresh_summary()
            for lbl, frac in (("片头 0%", 0.0), ("前段 25%", 0.25), ("正中 50%", 0.50), ("后段 75%", 0.75)):
                ttk.Button(preset_row, text=lbl, style="Small.TButton", command=lambda f=frac: jump_frac(f)).pack(side="left", padx=(6, 0))

        inp_grid = ttk.Frame(card)
        inp_grid.pack(fill="x", pady=(2, 8))
        inp_grid.columnconfigure(1, weight=1)
        inp_grid.columnconfigure(3, weight=1)
        ttk.Label(inp_grid, text="开始时间").grid(row=0, column=0, sticky="w", padx=(0, 8))
        st_entry = ttk.Entry(inp_grid, textvariable=start_var, width=14)
        st_entry.grid(row=0, column=1, sticky="ew", padx=(0, 16))
        ttk.Label(inp_grid, text="试转时长（秒）").grid(row=0, column=2, sticky="w", padx=(0, 8))
        du_entry = ttk.Entry(inp_grid, textvariable=dur_var, width=10)
        du_entry.grid(row=0, column=3, sticky="ew")

        dur_pills = ttk.Frame(card)
        dur_pills.pack(fill="x", pady=(0, 8))
        ttk.Label(dur_pills, text="常用时长：", style="Sub.TLabel").pack(side="left")
        for secs in (10, 15, 30, 60):
            ttk.Button(
                dur_pills,
                text=f"{secs} 秒",
                style="Small.TButton",
                command=lambda s=secs: dur_var.set(str(s)),
            ).pack(side="left", padx=(6, 0))

        start_var.trace_add("write", refresh_summary)
        dur_var.trace_add("write", refresh_summary)
        refresh_summary()

        ttk.Label(card, textvariable=range_summary, style="Percent.TLabel", wraplength=w - 50).pack(anchor="w", pady=(6, 12))

        btns = ttk.Frame(card)
        btns.pack(fill="x", side="bottom")
        chosen = [None]

        def confirm():
            try:
                st = parse_timecode_seconds(start_var.get())
                du = float(dur_var.get().strip())
                if not math.isfinite(du) or du <= 0:
                    raise ValueError("试转时长必须大于 0 秒。")
                if total_dur > 0 and st >= total_dur:
                    raise ValueError("开始时间不能超过视频总时长。")
            except ValueError as err:
                messagebox.showinfo("参数无效", str(err), parent=dlg)
                return
            self._last_preview_duration = du
            chosen[0] = (round(st, 2), round(du, 2))
            dlg.destroy()

        ttk.Button(btns, text="取消", command=dlg.destroy).pack(side="right")
        ttk.Button(btns, text="开始试转", style="Main.TButton", command=confirm).pack(side="right", padx=(0, 8))
        dlg.bind("<Return>", lambda _e: confirm())
        dlg.bind("<Escape>", lambda _e: dlg.destroy())
        dlg.wait_window()

        if chosen[0] is not None:
            st, du = chosen[0]
            self.start(preview=True, preview_start=st, preview_duration=du)

    def set_input(self, path):
        self.tracks_source_path = None
        self.input.set(path)
        source = Path(path)
        default_ext = ".mkv" if source.suffix.lower() == ".mkv" else ".mp4"
        initial_output = self._next_output_path(source, f"_HDR{default_ext}")
        self.output.set(initial_output)
        self.input_info = None
        self.input_duration = 0.0
        self.audio_tracks_meta = []
        self.subtitle_tracks_meta = []
        self.selected_audio_indices = None
        self.selected_subtitle_indices = None
        self.track_summary.set("正在读取音轨与内嵌字幕…")
        self.tracks_button.configure(state="disabled")
        self.info.set("正在读取视频信息…")

        def read():
            try:
                inspected = inspect_input(source)
                kind = inspected["kind"]
                p = inspected["properties"]
                dv = inspected["dv"]
                audio = inspected.get("audio") or {}
                audio_tracks = inspected.get("audio_tracks") or []
                subtitle_tracks = inspected.get("subtitle_tracks") or []
                default_container = inspected.get("default_container") or ("mkv" if source.suffix.lower() == ".mkv" else "mp4")
                duration_label = f"{p['duration']:.1f} 秒" if p["duration"] else "时长未提供"
                if audio.get("count"):
                    ch_info = audio.get("primary_label") or "音频"
                    audio_tag = (
                        f"  ·  {audio['count']} 条音轨（{ch_info} 等）"
                        if audio["count"] > 1 else f"  ·  {ch_info}"
                    )
                else:
                    audio_tag = "  ·  无音轨"
                sub_tag = f"  ·  {len(subtitle_tracks)} 条字幕" if subtitle_tracks else ""
                base = f"{p['width']} × {p['height']}  ·  {p['fps']:g} 帧/秒  ·  {duration_label}{audio_tag}{sub_tag}  ·  "
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
                self.queue.put(dict(
                    stage="input",
                    path=path,
                    message=msg,
                    kind=kind,
                    dv=dv,
                    duration=float(p.get("duration") or 0.0),
                    info=inspected.get("info"),
                    audio_tracks=audio_tracks,
                    subtitle_tracks=subtitle_tracks,
                    default_container=default_container,
                    initial_output=initial_output,
                ))
            except Exception as e:
                self.queue.put(dict(
                    stage="input", path=path, message=str(e), kind="sdr", dv=None,
                    duration=0.0, info=None, audio_tracks=[], subtitle_tracks=[], default_container="mp4",
                ))
        threading.Thread(target=read, daemon=True).start()

    def pick_output(self):
        current = Path(self.output.get() or "output_HDR.mp4")
        default_ext = current.suffix.lower() if current.suffix.lower() in (".mp4", ".mkv") else ".mp4"
        filetypes = (
            [("MKV 视频 (*.mkv，完整保留 TrueHD/DTS-HD 与蓝光字幕)", "*.mkv"), ("MP4 视频 (*.mp4)", "*.mp4")]
            if default_ext == ".mkv" else
            [("MP4 视频 (*.mp4)", "*.mp4"), ("MKV 视频 (*.mkv，完整保留 TrueHD/DTS-HD 与蓝光字幕)", "*.mkv")]
        )
        p = filedialog.asksaveasfilename(title="保存 HDR 副本", defaultextension=default_ext, initialfile=current.name,
                                       filetypes=filetypes, confirmoverwrite=False)
        if p:
            self.output.set(p)

    def _collect_current_config(self) -> dict | None:
        source = self.input.get().strip().strip('"')
        output = self.output.get().strip().strip('"')
        if not source or not output:
            messagebox.showinfo("选择视频", "请先选择输入视频和导出位置。")
            return None
        resolution = RESOLUTION_LABELS[self.resolution.get()]
        hdr_style = STYLE_LABELS[self.hdr_style.get()]
        quality = QUALITY_LABELS[self.quality.get()]
        encoder = ENCODER_LABELS[self.encoder.get()]
        audio_effect = AUDIO_EFFECT_LABELS[self.audio_effect.get()]

        # Track-selection guard: only pass explicit stream lists if they match the current inspected source.
        tracks_valid = self.tracks_source_path == source and self.input_info is not None
        sel_audio = list(self.selected_audio_indices) if (tracks_valid and self.selected_audio_indices is not None) else None
        sel_subs = list(self.selected_subtitle_indices) if (tracks_valid and self.selected_subtitle_indices is not None) else None
        if audio_effect == "surround" and sel_audio is not None and len(sel_audio) == 0 and self.audio_tracks_meta:
            messagebox.showinfo("未勾选音轨", "虚拟环绕模式需要至少勾选 1 条音轨，请点击“勾选音轨 / 字幕…”重新勾选。")
            return None

        if Path(output).suffix.lower() == ".mp4" and tracks_valid:
            compat = check_container_compatibility(
                self.input_info,
                "mp4",
                audio_effect=audio_effect,
                audio_streams=sel_audio,
                subtitle_streams=sel_subs,
            )
            if compat["needs_warning"]:
                choice = messagebox.askyesnocancel(
                    "MP4 封装兼容提示",
                    compat["warning_message"]
                    + "\n\n• 点击「是」：自动改存为 .mkv（无损直拷原音轨与字幕，推荐）"
                    + "\n• 点击「否」：仍按 .mp4 导出"
                    + "\n• 点击「取消」：返回修改设置",
                )
                if choice is None:
                    return None
                if choice is True:
                    mkv_dest = Path(output).with_suffix(".mkv")
                    reserved = {os.path.normcase(os.path.abspath(p)) for p in self._reserved_queue_outputs()}
                    if mkv_dest.exists() or os.path.normcase(os.path.abspath(str(mkv_dest))) in reserved:
                        suffix = "_HDR10.mkv" if self.input_kind in ("dv_strip", "dv_reencode") else "_HDR.mkv"
                        mkv_dest = Path(self._next_output_path(Path(source), suffix))
                    output = str(mkv_dest)
                    self.output.set(output)

        return dict(
            source=source,
            output=output,
            resolution=resolution,
            hdr_style=hdr_style,
            quality=quality,
            encoder=encoder,
            audio_effect=audio_effect,
            multipass=bool(self.multipass.get()),
            audio_streams=sel_audio,
            subtitle_streams=sel_subs,
        )

    def add_to_queue(self):
        if self.running:
            return
        cfg = self._collect_current_config()
        if cfg is None:
            return
        source_path = Path(cfg["source"])
        if not source_path.is_file():
            messagebox.showinfo("找不到视频", "原始视频文件不存在，请重新选择。")
            return
        output_path = Path(cfg["output"])
        reserved = {os.path.normcase(os.path.abspath(p)) for p in self._reserved_queue_outputs()}
        if output_path.exists() or os.path.normcase(os.path.abspath(str(output_path))) in reserved:
            ext = output_path.suffix if output_path.suffix.lower() in (".mp4", ".mkv") else (".mkv" if source_path.suffix.lower() == ".mkv" else ".mp4")
            cfg["output"] = self._next_output_path(source_path, f"_HDR{ext}")
            self.output.set(cfg["output"])

        task = QueueTask(
            task_id=uuid.uuid4().hex[:10],
            source=cfg["source"],
            output=cfg["output"],
            resolution=cfg["resolution"],
            hdr_style=cfg["hdr_style"],
            quality=cfg["quality"],
            encoder=cfg["encoder"],
            audio_effect=cfg["audio_effect"],
            multipass=cfg["multipass"],
            audio_streams=cfg["audio_streams"],
            subtitle_streams=cfg["subtitle_streams"],
            summary=format_queue_task_summary(
                cfg["output"], cfg["hdr_style"], cfg["quality"], cfg["resolution"],
                cfg["audio_effect"], cfg["audio_streams"], cfg["subtitle_streams"],
            ),
        )
        self.task_queue.append(task)
        self.refresh_queue_ui()
        # Advance output box suggestion so adding again won't collide.
        ext = Path(cfg["output"]).suffix or ".mp4"
        self.output.set(self._next_output_path(source_path, f"_HDR{ext}"))
        self.status.set(f"已加入任务列表：{source_path.name}（共 {len(self._pending_tasks())} 项等待执行）。")

    def batch_add_to_queue(self):
        if self.running:
            return
        files = filedialog.askopenfilenames(
            title="批量选择要加入任务列表的视频（应用当前画质／风格／编码器设置）",
            filetypes=[("视频", "*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.ts"), ("所有文件", "*.*")],
        )
        if not files:
            return
        resolution = RESOLUTION_LABELS[self.resolution.get()]
        hdr_style = STYLE_LABELS[self.hdr_style.get()]
        quality = QUALITY_LABELS[self.quality.get()]
        encoder = ENCODER_LABELS[self.encoder.get()]
        audio_effect = AUDIO_EFFECT_LABELS[self.audio_effect.get()]
        multipass = bool(self.multipass.get())

        added = 0
        for raw_path in files:
            src = Path(raw_path)
            if not src.is_file():
                continue
            ext = ".mkv" if src.suffix.lower() == ".mkv" else ".mp4"
            out = self._next_output_path(src, f"_HDR{ext}")
            task = QueueTask(
                task_id=uuid.uuid4().hex[:10],
                source=str(src),
                output=out,
                resolution=resolution,
                hdr_style=hdr_style,
                quality=quality,
                encoder=encoder,
                audio_effect=audio_effect,
                multipass=multipass,
                audio_streams=None,
                subtitle_streams=None,
                summary=format_queue_task_summary(out, hdr_style, quality, resolution, audio_effect, None, None),
            )
            self.task_queue.append(task)
            added += 1
        self.refresh_queue_ui()
        if added:
            self.status.set(f"已批量加入 {added} 个视频到任务列表（同封装默认导出，共 {len(self._pending_tasks())} 项等待执行）。")

    def _pending_tasks(self) -> list[QueueTask]:
        return [t for t in self.task_queue if t.status == "pending"]

    def refresh_queue_ui(self):
        sel = self.queue_tree.selection()
        selected_id = sel[0] if sel else None
        for item in self.queue_tree.get_children():
            self.queue_tree.delete(item)
        for idx, task in enumerate(self.task_queue, 1):
            src_name = Path(task.source).name
            out_name = Path(task.output).name
            file_col = f"{src_name}  →  {out_name}"
            self.queue_tree.insert("", "end", iid=task.task_id, values=(idx, file_col, task.summary, task.status_text))
        if selected_id and self.queue_tree.exists(selected_id):
            self.queue_tree.selection_set(selected_id)

        pending_cnt = sum(1 for t in self.task_queue if t.status == "pending")
        done_cnt = sum(1 for t in self.task_queue if t.status in ("done", "partial"))
        fail_cnt = sum(1 for t in self.task_queue if t.status == "failed")
        total_cnt = len(self.task_queue)
        if total_cnt == 0:
            self.queue_summary.set("暂无任务，可直接转换或点击“加入任务列表”排队执行")
        else:
            parts = [f"共 {total_cnt} 项", f"等待 {pending_cnt}"]
            if done_cnt:
                parts.append(f"完成 {done_cnt}")
            if fail_cnt:
                parts.append(f"失败 {fail_cnt}")
            self.queue_summary.set(" · ".join(parts))

        self.start_queue_button.configure(
            text=f"开始队列 ({pending_cnt})" if pending_cnt else "开始队列",
            state="normal" if (not self.running and pending_cnt > 0) else "disabled",
        )

    def _update_queue_row_status(self, task_id: str, status: str, status_text: str):
        for task in self.task_queue:
            if task.task_id == task_id:
                task.status = status
                task.status_text = status_text
                if self.queue_tree.exists(task_id):
                    vals = list(self.queue_tree.item(task_id, "values"))
                    if len(vals) == 4:
                        vals[3] = status_text
                        self.queue_tree.item(task_id, values=vals)
                break

    def move_selected_task(self, delta: int):
        if self.running:
            return
        sel = self.queue_tree.selection()
        if not sel:
            return
        tid = sel[0]
        idx = next((i for i, t in enumerate(self.task_queue) if t.task_id == tid), None)
        if idx is None:
            return
        target = idx + delta
        if 0 <= target < len(self.task_queue):
            self.task_queue[idx], self.task_queue[target] = self.task_queue[target], self.task_queue[idx]
            self.refresh_queue_ui()
            self.queue_tree.selection_set(tid)

    def remove_selected_task(self):
        if self.running:
            return
        sel = self.queue_tree.selection()
        if not sel:
            return
        tid = sel[0]
        self.task_queue = [t for t in self.task_queue if t.task_id != tid]
        self.refresh_queue_ui()

    def clear_completed_tasks(self):
        if self.running:
            return
        self.task_queue = [t for t in self.task_queue if t.status not in ("done", "partial", "cancelled", "failed")]
        self.refresh_queue_ui()

    def clear_queue(self):
        if self.running:
            return
        self.task_queue.clear()
        self.refresh_queue_ui()

    def _prepare_opts_for_source(self, opts: Options) -> Options:
        inspected = inspect_input(Path(opts.source))
        kind = inspected["kind"]
        if kind == "hdr10":
            opts.copy_hdr_video = True
            opts.resolution = "original"
            if opts.audio_effect == "original":
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
        return opts

    def _begin_run_state(self, is_queue: bool, is_preview: bool):
        self.running = True
        self.queue_running = is_queue
        self._current_is_preview = is_preview
        self.paused = False
        self.job_started = time.monotonic()
        self.pause_started_at = None
        self.paused_total = 0.0
        self.last_progress_info = None
        self._last_clock_tick = -1
        self.cancel.clear()
        self.keep_partial.clear()
        self.pause_event.clear()
        self.progress.set(0)
        self.percent_text.set("0.0%")
        self.status.set("正在准备…")
        self.detail.set("转换速度取决于视频尺寸与所选显卡/处理器；原片保持不变。")
        self.set_busy(True)

    def start(self, preview=False, preview_start: float = 0.0, preview_duration: float | None = None):
        if self.running:
            return
        cfg = self._collect_current_config()
        if cfg is None:
            return
        output = cfg["output"]
        start_sec = 0.0
        dur_sec = None
        if preview:
            start_sec = max(0.0, float(preview_start))
            dur_sec = float(preview_duration) if preview_duration is not None else 30.0
            p = Path(output)
            ext = p.suffix if p.suffix.lower() in (".mp4", ".mkv") else ".mp4"
            st_tag = format_duration_clock(start_sec).replace(":", "m") + "s"
            du_tag = f"{int(round(dur_sec))}s"
            tag = f"_试转_{st_tag}_{du_tag}"
            candidate = p.with_name(f"{p.stem}{tag}{ext}")
            n = 2
            while candidate.exists():
                candidate = p.with_name(f"{p.stem}{tag}_{n}{ext}")
                n += 1
            output = str(candidate)

        opts = Options(
            cfg["source"], output,
            resolution=cfg["resolution"],
            quality=cfg["quality"],
            start=start_sec,
            duration=dur_sec,
            audio_effect=cfg["audio_effect"],
            encoder=cfg["encoder"],
            hdr_style=cfg["hdr_style"],
            multipass=cfg["multipass"],
            audio_streams=cfg["audio_streams"],
            subtitle_streams=cfg["subtitle_streams"],
        )
        self._active_task_id = None
        self._active_queue_pos = (0, 0)
        self._begin_run_state(is_queue=False, is_preview=preview)

        def run():
            try:
                prepared = self._prepare_opts_for_source(opts)
                result = convert(prepared, self.queue.put, self.cancel, self.keep_partial, self.pause_event)
                self.queue.put(dict(stage="finished", result=result))
            except Cancelled as e:
                self.queue.put(dict(stage="cancelled", message=str(e)))
            except Exception as e:
                self.queue.put(dict(stage="error", message=str(e)))
        threading.Thread(target=run, daemon=True).start()

    def start_queue(self):
        if self.running:
            return
        pending = self._pending_tasks()
        if not pending:
            messagebox.showinfo("任务列表为空", "当前没有等待执行的任务，请先点击“加入任务列表”或“批量添加视频…”。")
            return
        self._begin_run_state(is_queue=True, is_preview=False)
        snapshot = list(pending)

        def run_batch():
            succeeded = 0
            failed = 0
            partial_cnt = 0
            stopped_by_user = False
            last_output = ""
            total_n = len(snapshot)
            for idx, task in enumerate(snapshot, 1):
                if self.cancel.is_set() or self.keep_partial.is_set():
                    stopped_by_user = True
                    break
                self.queue.put(dict(stage="queue_task_start", task_id=task.task_id, index=idx, total_count=total_n, name=Path(task.source).name))
                opts = Options(
                    task.source, task.output,
                    resolution=task.resolution,
                    quality=task.quality,
                    audio_effect=task.audio_effect,
                    encoder=task.encoder,
                    hdr_style=task.hdr_style,
                    multipass=task.multipass,
                    audio_streams=list(task.audio_streams) if task.audio_streams is not None else None,
                    subtitle_streams=list(task.subtitle_streams) if task.subtitle_streams is not None else None,
                )
                try:
                    prepared = self._prepare_opts_for_source(opts)
                    result = convert(
                        prepared,
                        lambda ev, tid=task.task_id, i=idx, n=total_n: self.queue.put({**ev, "task_id": tid, "queue_index": i, "queue_total": n}),
                        self.cancel,
                        self.keep_partial,
                        self.pause_event,
                    )
                    last_output = result["output"]
                    if result.get("partial"):
                        partial_cnt += 1
                        stopped_by_user = True
                        self.queue.put(dict(stage="queue_task_done", task_id=task.task_id, result=result))
                        break
                    succeeded += 1
                    self.queue.put(dict(stage="queue_task_done", task_id=task.task_id, result=result))
                except Cancelled as e:
                    stopped_by_user = True
                    self.queue.put(dict(stage="queue_task_cancelled", task_id=task.task_id, message=str(e)))
                    break
                except Exception as e:
                    failed += 1
                    self.queue.put(dict(stage="queue_task_error", task_id=task.task_id, message=str(e)))
            self.queue.put(dict(
                stage="queue_finished",
                succeeded=succeeded,
                failed=failed,
                partial=partial_cnt,
                stopped_by_user=stopped_by_user,
                last_output=last_output,
            ))

        threading.Thread(target=run_batch, daemon=True).start()

    def set_busy(self, busy):
        for c in self.controls:
            if isinstance(c, ttk.Combobox):
                c.configure(state="disabled" if busy else "readonly")
            else:
                c.configure(state="disabled" if busy else "normal")
        self.start_button.configure(state="disabled" if busy else "normal")
        self.preview_button.configure(state="disabled" if busy else "normal")
        self.add_queue_button.configure(state="disabled" if busy else "normal")
        for b in self.queue_edit_buttons:
            b.configure(state="disabled" if busy else "normal")
        self.stop_button.configure(state="normal" if busy else "disabled")
        self.save_partial_button.configure(state="normal" if busy else "disabled")
        self.pause_button.configure(
            text="继续" if (busy and self.paused) else "暂停",
            style="Paused.TButton" if (busy and self.paused) else "TButton",
            state="normal" if busy else "disabled",
        )
        if not busy:
            if self.input_kind in ("hdr10", "dv_strip"):
                self.resolution_combo.configure(state="disabled")
                self.quality_combo.configure(state="disabled")
                self.encoder_combo.configure(state="disabled")
            if self.input_kind != "sdr":
                self.style_combo.configure(state="disabled")
        self.tracks_button.configure(
            state="normal" if (not busy and bool(self.audio_tracks_meta or self.subtitle_tracks_meta)) else "disabled"
        )
        self.update_multipass_state()
        self.refresh_queue_ui()

    def toggle_pause(self):
        if not self.running or self.cancel.is_set() or self.keep_partial.is_set():
            return
        if not self.paused:
            self.paused = True
            self.pause_started_at = time.monotonic()
            self.pause_event.set()
            self.pause_button.configure(text="继续", style="Paused.TButton")
            self.status.set("已暂停转换，点击“继续”可恢复编码。")
            if self._active_task_id:
                pct = self.progress.get()
                self._update_queue_row_status(self._active_task_id, "paused", f"已暂停 {pct:.1f}%")
        else:
            if self.pause_started_at is not None:
                self.paused_total += max(0.0, time.monotonic() - self.pause_started_at)
                self.pause_started_at = None
            self.paused = False
            self.pause_event.clear()
            self.pause_button.configure(text="暂停", style="TButton")
            self.status.set("正在恢复转换编码…")
            if self._active_task_id:
                pct = self.progress.get()
                self._update_queue_row_status(self._active_task_id, "running", f"转换中 {pct:.1f}%")

    def _active_elapsed(self) -> float | None:
        if self.job_started is None:
            return None
        now = time.monotonic()
        extra = max(0.0, now - self.pause_started_at) if self.pause_started_at is not None else 0.0
        return max(0.0, now - self.job_started - self.paused_total - extra)

    def stop(self):
        self.paused = False
        self.pause_event.clear()
        self.cancel.set()
        self.pause_button.configure(state="disabled", text="暂停", style="TButton")
        self.save_partial_button.configure(state="disabled")
        self.status.set("正在取消并清理未完成的视频…")

    def stop_and_keep(self):
        if not self.running:
            return
        self.paused = False
        self.pause_event.clear()
        self.keep_partial.set()
        self.pause_button.configure(state="disabled", text="暂停", style="TButton")
        self.save_partial_button.configure(state="disabled")
        self.status.set("正在正常封包并保存已转换的视频片段（请勿强行关闭）…")

    def _maybe_schedule_power_action(self):
        action = POWER_LABELS.get(self.power_action.get(), "none")
        if action == "none":
            return
        action_label = "自动关机" if action == "shutdown" else "进入待机（睡眠）"
        dlg = tk.Toplevel(self.root)
        dlg.title(f"任务已完成 — {action_label}倒计时")
        dlg.configure(bg=SURFACE_BG)
        dlg.transient(self.root)
        dlg.grab_set()
        try:
            dlg.attributes("-topmost", True)
        except tk.TclError:
            pass
        w = int(round(460 * self.dpi_scale))
        h = int(round(200 * self.dpi_scale))
        x = self.root.winfo_rootx() + max(20, (self.root.winfo_width() - w) // 2)
        y = self.root.winfo_rooty() + max(20, (self.root.winfo_height() - h) // 2)
        dlg.geometry(f"{w}x{h}+{x}+{y}")

        card = ttk.Frame(dlg, style="Card.TFrame", padding=(20, 18, 20, 16))
        card.pack(fill="both", expand=True)
        remaining = [60]
        cancelled = [False]
        after_id = [None]
        msg_var = tk.StringVar()

        ttk.Label(card, text="所有 HDR 转换任务均已顺利完成", style="Section.TLabel").pack(anchor="w")
        ttk.Label(card, textvariable=msg_var, wraplength=w - 44).pack(anchor="w", pady=(8, 16))

        def do_cancel():
            cancelled[0] = True
            if after_id[0] is not None:
                try:
                    dlg.after_cancel(after_id[0])
                except tk.TclError:
                    pass
            dlg.destroy()

        def do_now():
            if cancelled[0]:
                return
            cancelled[0] = True
            if after_id[0] is not None:
                try:
                    dlg.after_cancel(after_id[0])
                except tk.TclError:
                    pass
            dlg.destroy()
            try:
                trigger_power_action(action)
            except Exception as err:
                messagebox.showerror("电源操作失败", str(err))

        def tick():
            if cancelled[0]:
                return
            if remaining[0] <= 0:
                do_now()
                return
            msg_var.set(f"系统将在 {remaining[0]} 秒后{action_label}。如需继续使用电脑，请点击下方取消。")
            remaining[0] -= 1
            after_id[0] = dlg.after(1000, tick)

        btns = ttk.Frame(card)
        btns.pack(fill="x", side="bottom")
        cancel_btn = ttk.Button(btns, text="取消（保持开机）", style="Main.TButton", command=do_cancel)
        cancel_btn.pack(side="right")
        ttk.Button(btns, text=f"立即{action_label}", command=do_now).pack(side="right", padx=(0, 8))
        dlg.protocol("WM_DELETE_WINDOW", do_cancel)
        dlg.bind("<Escape>", lambda _e: do_cancel())
        cancel_btn.focus_set()
        tick()

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
                        self.input_info = p.get("info")
                        self.input_duration = float(p.get("duration") or 0.0)
                        if self.input_info is not None:
                            self.tracks_source_path = p["path"]
                            self.audio_tracks_meta = list(p.get("audio_tracks") or [])
                            self.subtitle_tracks_meta = list(p.get("subtitle_tracks") or [])
                            self.selected_audio_indices = [t["stream_index"] for t in self.audio_tracks_meta]
                            self.selected_subtitle_indices = [t["stream_index"] for t in self.subtitle_tracks_meta]
                        else:
                            # Probe failed: keep track selections as None so convert() never strips audio accidentally.
                            self.tracks_source_path = None
                            self.audio_tracks_meta = []
                            self.subtitle_tracks_meta = []
                            self.selected_audio_indices = None
                            self.selected_subtitle_indices = None
                            self.track_summary.set("读取轨道失败，转换时将默认保留原片音轨")
                        if self.input_kind in ("hdr10", "dv_strip"):
                            self.resolution.set("保持原尺寸")
                        if self.output.get() == p.get("initial_output"):
                            name = Path(p["path"])
                            ext = ".mkv" if p.get("default_container") == "mkv" else ".mp4"
                            if self.input_kind == "hdr10":
                                self.output.set(self._next_output_path(name, f"_环绕{ext}"))
                            elif self.input_kind in ("dv_strip", "dv_reencode"):
                                self.output.set(self._next_output_path(name, f"_HDR10{ext}"))
                        button_text = {
                            "dv_strip": "无损导出 HDR10",
                            "dv_reencode": "导出完整 HDR10",
                            "hdr10": "导出环绕 HDR",
                        }.get(self.input_kind, "导出完整 HDR")
                        self.start_button.configure(text=button_text)
                        self.set_busy(self.running)
                        self.update_track_summary()
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
                elif stage == "queue_task_start":
                    self._active_task_id = p["task_id"]
                    self._active_queue_pos = (p["index"], p["total_count"])
                    self.job_started = time.monotonic()
                    self.pause_started_at = time.monotonic() if self.paused else None
                    self.paused_total = 0.0
                    self.last_progress_info = None
                    self._last_clock_tick = -1
                    self.progress.set(0)
                    self.percent_text.set("0.0%")
                    self._update_queue_row_status(p["task_id"], "running", "转换中 0.0%")
                    if self.queue_tree.exists(p["task_id"]):
                        self.queue_tree.see(p["task_id"])
                    self.status.set(f"[队列 {p['index']}/{p['total_count']}] 正在准备：{p['name']}…")
                elif stage == "queue_task_done":
                    tid = p["task_id"]
                    res = p["result"]
                    self.last_output = res["output"]
                    if res.get("partial"):
                        self._update_queue_row_status(tid, "partial", "已保留片段")
                    elif res.get("skipped_lossless_verify"):
                        self._update_queue_row_status(tid, "done", "已完成(跳过校验)")
                    else:
                        self._update_queue_row_status(tid, "done", "已完成")
                    self.refresh_queue_ui()
                elif stage == "queue_task_cancelled":
                    self._update_queue_row_status(p["task_id"], "cancelled", "已取消")
                    self.refresh_queue_ui()
                elif stage == "queue_task_error":
                    self._update_queue_row_status(p["task_id"], "failed", "失败")
                    for t in self.task_queue:
                        if t.task_id == p["task_id"]:
                            t.error_message = p["message"]
                    self.detail.set(f"队列任务失败（已继续下一项）：{p['message'][:300]}")
                    self.refresh_queue_ui()
                elif stage == "paused":
                    self.status.set(p["message"])
                    if self._active_task_id:
                        self._update_queue_row_status(self._active_task_id, "paused", f"已暂停 {float(p.get('percent', 0)):.1f}%")
                elif stage == "resumed":
                    prefix = (
                        f"[队列 {self._active_queue_pos[0]}/{self._active_queue_pos[1]}] "
                        if self.queue_running and self._active_queue_pos[1] > 0 else ""
                    )
                    self.status.set(prefix + p["message"])
                elif stage == "stopping":
                    self.save_partial_button.configure(state="disabled")
                    self.pause_button.configure(state="disabled", text="暂停", style="TButton")
                    self.status.set(p["message"])
                    self.last_progress_info = dict(stage="stopping")
                elif stage in ("encoding", "verifying", "done"):
                    pct = max(0.0, min(100.0, float(p.get("percent", 0) or 0)))
                    self.progress.set(pct)
                    self.percent_text.set(
                        "已保留" if (stage == "done" and p.get("partial")) else f"{pct:.1f}%"
                    )
                    q_prefix = (
                        f"[队列 {self._active_queue_pos[0]}/{self._active_queue_pos[1]}] "
                        if self.queue_running and self._active_queue_pos[1] > 0 else ""
                    )
                    if not self.paused:
                        self.status.set(q_prefix + p["message"])
                    if self._active_task_id and stage in ("encoding", "verifying"):
                        lbl = f"已暂停 {pct:.1f}%" if self.paused else ("校验中 99%" if stage == "verifying" else f"转换中 {pct:.1f}%")
                        self._update_queue_row_status(self._active_task_id, "paused" if self.paused else "running", lbl)
                    live_elapsed = p.get("elapsed")
                    if live_elapsed is None:
                        live_elapsed = self._active_elapsed()
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
                elif stage == "queue_finished":
                    self.running = False
                    self.queue_running = False
                    self.paused = False
                    self._active_task_id = None
                    self._active_queue_pos = (0, 0)
                    self.job_started = None
                    self.pause_started_at = None
                    self.paused_total = 0.0
                    self.last_progress_info = None
                    self._last_clock_tick = -1
                    self.set_busy(False)
                    succ = p.get("succeeded", 0)
                    fail = p.get("failed", 0)
                    part = p.get("partial", 0)
                    stopped = bool(p.get("stopped_by_user"))
                    if p.get("last_output"):
                        self.last_output = p["last_output"]
                    if stopped:
                        self.percent_text.set("已停止")
                        self.status.set(f"队列已停止：已完成 {succ} 项，保留片段 {part} 项，失败 {fail} 项。")
                    else:
                        self.progress.set(100.0)
                        self.percent_text.set("100.0%")
                        self.status.set(f"任务列表执行完成：成功 {succ} 项" + (f"，失败 {fail} 项。" if fail else "。"))
                        if succ > 0:
                            self._maybe_schedule_power_action()
                    if self.last_output:
                        self.detail.set("最近导出：" + self.last_output)
                elif stage in ("finished", "cancelled", "error"):
                    total_elapsed = None
                    if stage == "finished":
                        total_elapsed = p["result"].get("elapsed_seconds")
                    if total_elapsed is None:
                        total_elapsed = self._active_elapsed()
                    was_preview = self._current_is_preview
                    self.running = False
                    self.queue_running = False
                    self.paused = False
                    self._current_is_preview = False
                    self.job_started = None
                    self.pause_started_at = None
                    self.paused_total = 0.0
                    self.last_progress_info = None
                    self._last_clock_tick = -1
                    self.set_busy(False)
                    if stage == "finished":
                        self.last_output = p["result"]["output"]
                        encoder_label = p["result"].get("plan", {}).get("encoder_label")
                        is_partial = bool(p["result"].get("partial"))
                        skipped_verify = bool(p["result"].get("skipped_lossless_verify"))
                        self.progress.set(100.0)
                        self.percent_text.set("已保留" if is_partial else "100.0%")
                        if is_partial:
                            prefix = f"已停止并保留片段（{encoder_label}）" if encoder_label else "已停止并保留片段"
                            self.status.set(f"{prefix}：可正常播放，HDR10 检查均通过。")
                        elif skipped_verify:
                            prefix = f"已完成（{encoder_label}）" if encoder_label else "已完成"
                            self.status.set(f"{prefix}：已跳过逐帧无损校验并保留完整文件，HDR10 检查均通过。")
                        else:
                            done_prefix = f"已完成（{encoder_label}）" if encoder_label else "已完成"
                            self.status.set(f"{done_prefix}：HDR 视频和文件检查均通过。")
                        elapsed_prefix = (
                            f"总耗时 {format_duration_clock(total_elapsed)}  ·  "
                            if total_elapsed is not None else ""
                        )
                        self.detail.set(elapsed_prefix + self.last_output)
                        if not was_preview and not is_partial:
                            self._maybe_schedule_power_action()
                    else:
                        self.status.set("已取消。" if stage == "cancelled" else "转换未完成。")
                        self.detail.set(p["message"][:500])
                        if stage == "error":
                            messagebox.showerror("转换未完成", p["message"])
        except queue.Empty:
            pass
        if self.running and not self.paused and self.job_started is not None:
            live_elapsed = self._active_elapsed()
            if live_elapsed is not None:
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
            self.paused = False
            self.pause_event.clear()
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

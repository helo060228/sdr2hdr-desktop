"""Small local desktop front end. All video processing stays on the computer."""
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
    detect_encoders,
    hdr_copy_properties,
    is_hdr_video,
    probe,
    source_properties,
)


ENCODER_LABELS = {
    "自动（优先显卡加速）": "auto",
    "NVIDIA 显卡 (NVENC)": "nvenc",
    "Intel 显卡 (QSV)": "qsv",
    "AMD 显卡 (AMF)": "amf",
    "CPU 软件编码 (x265)": "cpu",
}


class App:
    def __init__(self, root):
        self.root = root
        root.title(f"SDR 转 HDR · 离线转换器 {VERSION}")
        root.geometry(f"{min(840, root.winfo_screenwidth()-80)}x{min(810, root.winfo_screenheight()-90)}")
        root.minsize(710, 580)
        root.configure(bg="#f4f5f7")
        self.queue = queue.Queue()
        self.cancel = threading.Event()
        self.running = False
        self.input = tk.StringVar()
        self.output = tk.StringVar()
        self.resolution = tk.StringVar(value="保持原尺寸")
        self.quality = tk.StringVar(value="标准画质")
        self.encoder = tk.StringVar(value="自动（优先显卡加速）")
        self.audio_effect = tk.StringVar(value="原声（保留）")
        self.detected_encoders = None
        self.input_is_hdr = False
        self.status = tk.StringVar(value="选择视频，制作 HDR 或添加环绕音效。")
        self.info = tk.StringVar(value="支持 MP4、MOV、MKV 等本地视频；保留原文件。")
        self.encoder_status = tk.StringVar(value="正在检测可用的显卡硬件加速…")
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
        ttk.Label(box, text="SDR 转 HDR10；已有 HDR 可直接加音效，保留原画面。", style="Sub.TLabel").pack(anchor="w", pady=(9, 23))
        self.path_row(box, "原始视频", self.input, self.pick_input, "选择视频")
        ttk.Label(box, textvariable=self.info, style="Sub.TLabel", wraplength=700).pack(anchor="w", pady=(2, 17))
        self.path_row(box, "导出文件", self.output, self.pick_output, "保存到…")
        row = ttk.Frame(box)
        row.pack(fill="x", pady=(14, 10))
        ttk.Label(row, text="输出尺寸").grid(row=0, column=0, sticky="w")
        c1 = ttk.Combobox(row, textvariable=self.resolution, values=["保持原尺寸", "2K · 短边 1440", "4K · 短边 2160"], state="readonly", width=24)
        c1.grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Label(row, text="转换方式").grid(row=0, column=1, sticky="w", padx=(32, 0))
        c2 = ttk.Combobox(row, textvariable=self.quality, values=["快速试转", "标准画质", "精细画质", "画面无损编码"], state="readonly", width=24)
        c2.grid(row=1, column=1, sticky="w", padx=(32, 0), pady=(6, 0))
        ttk.Label(row, text="编码器").grid(row=2, column=0, sticky="w", pady=(12, 0))
        c3 = ttk.Combobox(row, textvariable=self.encoder, values=list(ENCODER_LABELS.keys()), state="readonly", width=24)
        c3.grid(row=3, column=0, sticky="w", pady=(6, 0))
        ttk.Label(row, text="音效").grid(row=2, column=1, sticky="w", padx=(32, 0), pady=(12, 0))
        c4 = ttk.Combobox(row, textvariable=self.audio_effect, values=["原声（保留）", "虚拟环绕（双声道）"], state="readonly", width=24)
        c4.grid(row=3, column=1, sticky="w", padx=(32, 0), pady=(6, 0))
        self.resolution_combo, self.quality_combo, self.encoder_combo, self.audio_combo = c1, c2, c3, c4
        self.controls += [c1, c2, c3, c4]
        self.encoding_note = tk.StringVar()
        ttk.Label(box, textvariable=self.encoder_status, style="Sub.TLabel", wraplength=710).pack(anchor="w", pady=(0, 4))
        ttk.Label(box, textvariable=self.encoding_note, style="Sub.TLabel", wraplength=710).pack(anchor="w", pady=(0, 6))
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
        ttk.Progressbar(box, variable=self.progress, maximum=100).pack(fill="x", pady=(20, 12))
        ttk.Label(box, textvariable=self.status, wraplength=710).pack(anchor="w")
        ttk.Label(box, textvariable=self.detail, style="Sub.TLabel", wraplength=710).pack(anchor="w", pady=(5, 10))
        self.open_button = ttk.Button(box, text="打开导出文件夹", command=self.open_folder)
        self.open_button.pack(anchor="w")
        ttk.Label(box, text="B 站转码后仍需检查 HDR 档位；不同屏幕和平台的显示可能存在差异。\n本工具离线处理，不会自动上传或发布。", style="Sub.TLabel", wraplength=710).pack(anchor="w", pady=(17, 0))
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.poll)
        self.detect_encoders_async()
        if len(sys.argv) > 1 and Path(sys.argv[1]).is_file():
            root.after(200, lambda: self.set_input(sys.argv[1]))

    def detect_encoders_async(self):
        def probe_worker():
            try:
                self.queue.put(dict(stage="encoders", encoders=detect_encoders()))
            except Exception as e:
                self.queue.put(dict(stage="encoders", error=str(e)))
        threading.Thread(target=probe_worker, daemon=True).start()

    def update_encoding_note(self):
        notes = []
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
        p = filedialog.askopenfilename(title="选择 SDR 视频", filetypes=[("视频", "*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.ts"), ("所有文件", "*.*")])
        if p:
            self.set_input(p)

    def set_input(self, path):
        self.input.set(path)
        source = Path(path)
        dest = source.with_name(source.stem + "_HDR.mp4")
        count = 2
        while dest.exists():
            dest = source.with_name(source.stem + f"_HDR_{count}.mp4")
            count += 1
        self.output.set(str(dest))
        initial_output = str(dest)
        self.info.set("正在读取视频信息…")
        def read():
            try:
                info = probe(source)
                hdr = is_hdr_video(info)
                p = hdr_copy_properties(info) if hdr else source_properties(info)
                duration_label = f"{p['duration']:.1f} 秒" if p["duration"] else "时长未提供"
                msg = f"{p['width']} × {p['height']}  ·  {p['fps']:g} 帧/秒  ·  {duration_label}  ·  " + ("HDR · 画面直拷" if hdr else "SDR")
                if p["guessed"]:
                    msg += "\n" + "；".join(p["guessed"])
                self.queue.put(dict(stage="input", path=path, message=msg, hdr=hdr, initial_output=initial_output))
            except Exception as e:
                self.queue.put(dict(stage="input", path=path, message=str(e)))
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
        quality = {"快速试转": "fast", "标准画质": "standard", "精细画质": "fine", "画面无损编码": "lossless"}[self.quality.get()]
        encoder = ENCODER_LABELS[self.encoder.get()]
        audio_effect = {"原声（保留）": "original", "虚拟环绕（双声道）": "surround"}[self.audio_effect.get()]
        opts = Options(source, output, resolution=resolution, quality=quality, duration=10 if preview else None, audio_effect=audio_effect, encoder=encoder)
        self.running = True
        self.cancel.clear()
        self.progress.set(0)
        self.status.set("正在准备…")
        self.detail.set("转换速度取决于视频尺寸与所选显卡/处理器；原片保持不变。")
        self.set_busy(True)
        def run():
            try:
                opts.copy_hdr_video = is_hdr_video(probe(Path(source)))
                if opts.copy_hdr_video and audio_effect == "original":
                    raise ConversionError("这个视频已经是 HDR，无需重复转换。请选择“虚拟环绕（双声道）”，即可只处理声音并保留 HDR 画面。")
                result = convert(opts, self.queue.put, self.cancel)
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
        if not busy and self.input_is_hdr:
            self.resolution_combo.configure(state="disabled")
            self.quality_combo.configure(state="disabled")
            self.encoder_combo.configure(state="disabled")

    def stop(self):
        self.cancel.set()
        self.status.set("正在取消并清理未完成的视频…")

    def poll(self):
        try:
            while True:
                p = self.queue.get_nowait()
                stage = p["stage"]
                if stage == "input":
                    if self.input.get() == p["path"]:
                        self.info.set(p["message"])
                        self.input_is_hdr = p.get("hdr", False)
                        if self.input_is_hdr:
                            self.resolution.set("保持原尺寸")
                            if self.output.get() == p.get("initial_output"):
                                name = Path(p["path"])
                                dest = name.with_name(name.stem + "_环绕.mp4")
                                n = 2
                                while dest.exists():
                                    dest = name.with_name(name.stem + f"_环绕_{n}.mp4")
                                    n += 1
                                self.output.set(str(dest))
                        self.set_busy(self.running)
                elif stage == "encoders":
                    if "error" in p:
                        self.encoder_status.set("未能自动检测编码器（转换时将再次检查）：" + p["error"])
                    else:
                        self.detected_encoders = p["encoders"]
                        hw_labels = []
                        for key in HARDWARE_PRIORITY:
                            info = self.detected_encoders.get(key, {})
                            if info.get("available"):
                                suffix = "（含无损）" if info.get("lossless") else "（无损需用 CPU）"
                                hw_labels.append(info["short_label"] + suffix)
                        if hw_labels:
                            self.encoder_status.set("已检测到显卡加速：" + "、".join(hw_labels) + "；其余情况自动回退 CPU (x265)。")
                        else:
                            self.encoder_status.set("未检测到支持 10-bit HDR10 的显卡硬编，将使用 CPU 软件编码 (x265)。")
                        self.update_encoding_note()
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

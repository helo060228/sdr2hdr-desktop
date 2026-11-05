"""Offline SDR-to-HDR transcoder with fixed logarithmic tone expansion.

Processing intentionally operates in encoded nonlinear RGB. The result is a
stylized HDR presentation, not a colorimetric recovery of an HDR master.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from fractions import Fraction
from typing import Callable

VERSION = "1.2.1"
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
M1, M2 = 2610 / 16384, 2523 / 32
C1, C2, C3 = 3424 / 4096, 2413 / 128, 2392 / 128
ALGORITHM_ID = "Fixed logarithmic SDR-to-PQ tone expansion with chroma gain 0.56"
# Stereo spatial expansion uses channel difference, short cross delay,
# modest gain and peak limiting. Output remains a two-channel AAC stream.
SURROUND_FILTER = (
    "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
    "extrastereo=m=1.35:c=0,"
    "stereowiden=delay=12:feedback=0.12:crossfeed=0.08:drymix=0.95,"
    "volume=1.12,alimiter=limit=0.89:level=0:attack=5:release=60:latency=1"
)


class ConversionError(Exception):
    pass


class Cancelled(ConversionError):
    pass


def _finite_number(value) -> float | None:
    """Media/progress fields may contain N/A, empty values, or non-finite text."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _known_duration(*values) -> float:
    for value in values:
        number = _finite_number(value)
        if number is not None and number > 0:
            return number
    return 0.0


def _positive_ratio(value, default=1.0) -> float:
    try:
        number = float(Fraction(str(value).replace(":", "/")))
    except (TypeError, ValueError, ZeroDivisionError, OverflowError):
        return default
    return number if math.isfinite(number) and number > 0 else default


def progress_seconds(fields: dict, previous=0.0) -> float:
    """Unknown timestamps are normal before FFmpeg writes its first packet."""
    micros = _finite_number(fields.get("out_time_us"))
    seconds = micros / 1_000_000 if micros is not None else None
    if seconds is None:
        clock = str(fields.get("out_time") or "").strip()
        parts = clock.lstrip("+-").split(":")
        if len(parts) == 3:
            hours, minutes, remainder = (_finite_number(x) for x in parts)
            if (hours is not None and hours >= 0 and minutes is not None and 0 <= minutes < 60
                    and remainder is not None and 0 <= remainder < 60):
                seconds = (hours * 3600 + minutes * 60 + remainder) * (-1 if clock.startswith("-") else 1)
    return max(0.0, previous, seconds if seconds is not None else previous)


def pq_encode(nits: float) -> float:
    x = max(0.0, nits / 10000.0) ** M1
    return ((C1 + C2 * x) / (1 + C3 * x)) ** M2


def curve(x: float, peak: float = 1000.0) -> float:
    return min(pq_encode(peak), max(0.0, 0.65 * math.log1p(5 * max(x, 0.0)) / math.log(6)))


def chroma_matrix(matrix: str = "bt709") -> list[list[float]]:
    # Convert encoded RGB to the input YUV matrix, scale UV by 0.56,
    # then convert back using the explicit coefficients below.
    if matrix in ("bt470bg", "smpte170m"):
        kr, kb, rv, bu, gu, gv = .299, .114, 1.402, 1.772, .344, .714
    else:
        kr, kb, rv, bu, gu, gv = .2126, .0722, 1.5748, 1.8556, .1873, .4681
    kg = 1 - kr - kb
    y = [kr, kg, kb]
    u = [-kr / (2 * (1 - kb)), -kg / (2 * (1 - kb)), .5]
    v = [.5, -kg / (2 * (1 - kr)), -kb / (2 * (1 - kr))]
    return [[y[i] + .56 * rv * v[i] for i in range(3)],
            [y[i] - .56 * gu * u[i] - .56 * gv * v[i] for i in range(3)],
            [y[i] + .56 * bu * u[i] for i in range(3)]]


def reference_rgb(rgb: tuple[float, float, float], matrix="bt709", peak=1000.0):
    return tuple(curve(sum(a * b for a, b in zip(row, rgb)), peak) for row in chroma_matrix(matrix))


def app_dir() -> Path:
    return Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent


def find_tool(name: str) -> str:
    filename = name + (".exe" if os.name == "nt" else "")
    candidates = [app_dir() / "bin" / filename, app_dir() / filename]
    found = shutil.which(name)
    if found:
        candidates.append(Path(found))
    # Handles a WinGet install even when Explorer's inherited PATH is stale.
    if os.name == "nt" and os.getenv("LOCALAPPDATA"):
        package_root = Path(os.environ["LOCALAPPDATA"]) / "Microsoft/WinGet/Packages"
        candidates.extend(package_root.glob(f"Gyan.FFmpeg*/ffmpeg*/bin/{filename}"))
    for p in candidates:
        if p.is_file():
            return str(p.resolve())
    raise ConversionError(f"找不到 {filename}。请安装包含 zscale／libx265 的完整 FFmpeg，或将 ffmpeg.exe 和 ffprobe.exe 放进工具旁的 bin 文件夹。")


def capture(args: list[str], timeout=60) -> subprocess.CompletedProcess:
    return subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          encoding="utf-8", errors="replace", timeout=timeout,
                          creationflags=CREATE_NO_WINDOW)


def probe(path: Path) -> dict:
    result = capture([find_tool("ffprobe"), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)])
    if result.returncode:
        raise ConversionError("无法读取视频：" + result.stderr[-1600:])
    return json.loads(result.stdout)


def video_stream(info: dict) -> dict:
    videos = [s for s in info.get("streams", []) if s.get("codec_type") == "video"
              and not s.get("disposition", {}).get("attached_pic")]
    if not videos:
        raise ConversionError("文件中没有可转换的视频轨道。")
    return videos[0]


def is_hdr_video(info: dict) -> bool:
    v = video_stream(info)
    return v.get("color_transfer") in ("smpte2084", "arib-std-b67") or any(
        "DOVI" in s.get("side_data_type", "").upper() for s in v.get("side_data_list", []))


def hdr_copy_properties(info: dict) -> dict:
    v = video_stream(info)
    if not (v.get("codec_name") == "hevc" and v.get("pix_fmt") == "yuv420p10le"
            and v.get("color_transfer") == "smpte2084" and v.get("color_primaries") == "bt2020"
            and v.get("color_space") == "bt2020nc"):
        raise ConversionError("画面直拷目前支持 HEVC 10-bit、PQ／BT.2020 的 HDR 文件，例如本工具先前导出的文件。")
    fps = _positive_ratio(v.get("avg_frame_rate"), _positive_ratio(v.get("r_frame_rate"), 25.0))
    return dict(width=v["width"], height=v["height"], sar=1.0, fps=fps or 25,
                duration=_known_duration(v.get("duration"), info.get("format", {}).get("duration")),
                matrix=v["color_space"], transfer=v["color_transfer"], primaries=v["color_primaries"],
                range="limited", guessed=[], stream_index=v["index"], pixel_format=v["pix_fmt"])


def source_properties(info: dict) -> dict:
    v = video_stream(info)
    trc = v.get("color_transfer", "unknown")
    if trc in ("smpte2084", "arib-std-b67") or any("DOVI" in s.get("side_data_type", "").upper() for s in v.get("side_data_list", [])):
        raise ConversionError("这个文件已经是 HDR／杜比视界。工具只转换 SDR，避免重复增强。")
    try:
        width, height = int(v["width"]), int(v["height"])
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ConversionError("视频没有提供有效的画面尺寸，无法开始转换。") from None
    if width <= 0 or height <= 0:
        raise ConversionError("视频的画面尺寸无效，无法开始转换。")
    if v.get("field_order") not in (None, "unknown", "progressive"):
        raise ConversionError("暂不处理隔行扫描视频。请先去隔行，避免改变运动效果。")
    matrix = v.get("color_space", "unknown")
    pix_fmt = v.get("pix_fmt", "")
    rgb = pix_fmt.startswith(("rgb", "bgr", "gbr", "rgba", "bgra", "argb", "abgr", "x2rgb", "x2bgr"))
    guessed = []
    if rgb:
        matrix = "gbr"
    elif matrix not in ("bt709", "bt470bg", "smpte170m"):
        if matrix not in ("unknown", "unspecified", None):
            raise ConversionError(f"暂不支持这个 SDR 色彩矩阵：{matrix}。当前版本支持 BT.709／BT.601。")
        matrix = "bt709" if height > 576 or width > 1024 else ("bt470bg" if height > 480 else "smpte170m")
        guessed.append("原片未标记颜色矩阵，按尺寸推定 " + matrix)
    primaries = v.get("color_primaries", "unknown")
    if primaries not in ("bt709", "bt470bg", "smpte170m", "unknown", "unspecified", None):
        raise ConversionError(f"暂不支持 {primaries} 色域的 SDR。当前转换规则以 BT.709／BT.601 为输入。")
    if primaries in ("unknown", "unspecified", None):
        primaries = "bt709" if matrix in ("gbr", "bt709") else matrix
        guessed.append("原片未标记原色，使用 " + primaries)
    supported_trc = ("bt709", "smpte170m", "bt470bg", "gamma22", "gamma28", "iec61966-2-1", "bt2020-10", "bt2020-12")
    if trc not in supported_trc:
        if trc not in ("unknown", "unspecified", None):
            raise ConversionError(f"暂不支持这个传递函数：{trc}。请使用已调色完成的 SDR 视频。")
        trc = "iec61966-2-1" if rgb else "bt709"
        guessed.append("原片未标记传递函数，使用 " + trc)
    trc = {"gamma22": "bt470m", "gamma28": "bt470bg"}.get(trc, trc)
    range_ = "full" if rgb or v.get("color_range") == "pc" or pix_fmt.startswith("yuvj") else "limited"
    if not rgb and v.get("color_range") not in ("pc", "tv"):
        guessed.append("原片未标记量化范围，按常规视频使用有限范围")
    rotation = next((_finite_number(s.get("rotation")) or 0 for s in v.get("side_data_list", []) if "rotation" in s), 0)
    sar = _positive_ratio(v.get("sample_aspect_ratio"), 1.0)
    if abs(rotation) % 180 == 90:
        width, height, sar = height, width, 1 / sar
    fps = _positive_ratio(v.get("avg_frame_rate"), _positive_ratio(v.get("r_frame_rate"), 25.0))
    duration = _known_duration(v.get("duration"), info.get("format", {}).get("duration"))
    return dict(width=width, height=height, sar=sar, fps=fps or 25, duration=duration,
                matrix=matrix, primaries=primaries, transfer=trc, range=range_,
                guessed=guessed, stream_index=v["index"], pixel_format=pix_fmt)


def dimensions(p: dict, resolution="original") -> tuple[int, int]:
    w, h = p["width"] * p["sar"], p["height"]
    if resolution != "original":
        target = int(resolution)
        ratio = target / min(w, h)
        w, h = w * ratio, h * ratio
    return max(2, round(w / 2) * 2), max(2, round(h / 2) * 2)


def write_curve(path: Path, peak=1000.0):
    # Divide the mixed input by four, then index a 65,536-sample LUT representing
    # [0, 4]. This preserves super-whites and avoids a coarse 3D approximation.
    with path.open("w", encoding="ascii", newline="\n") as f:
        f.write('TITLE "Logarithmic SDR-to-PQ curve; nominal 1000-nit ceiling"\nLUT_1D_SIZE 65536\nDOMAIN_MIN 0 0 0\nDOMAIN_MAX 1 1 1\n')
        for i in range(65536):
            value = curve(i * 4 / 65535, peak)
            f.write(f"{value:.10f} {value:.10f} {value:.10f}\n")


def filter_chain(p: dict, width: int, height: int, lut_name="tone.cube") -> str:
    # LUT filename is controlled by the application; FFmpeg runs in a private
    # temporary directory. No user filename is interpolated into filter syntax.
    if lut_name != "tone.cube":
        raise ValueError("Unexpected LUT filename")
    flags = (f"pin={p['primaries']}:tin={p['transfer']}:min={p['matrix']}:rin={p['range']}:"
             f"p={p['primaries']}:t={p['transfer']}:m=gbr:r=full:filter=bilinear")
    rows = chroma_matrix("bt709" if p["matrix"] == "gbr" else p["matrix"])
    names = ("rr", "rg", "rb", "gr", "gg", "gb", "br", "bg", "bb")
    mixer = ":".join(f"{name}={value / 4:.12f}" for name, value in zip(names, sum(rows, [])))
    return ",".join([
        "zscale=" + flags, "format=gbrpf32le", "colorchannelmixer=" + mixer,
        "lut1d=file=tone.cube:interp=linear",
        "setparams=range=full:color_primaries=bt2020:color_trc=smpte2084:colorspace=gbr",
        f"zscale=pin=bt2020:tin=smpte2084:min=gbr:rin=full:p=bt2020:t=smpte2084:m=bt2020nc:r=limited:w={width}:h={height}:filter=bilinear:d=error_diffusion",
        "format=yuv420p10le", "setsar=1",
    ])


@dataclasses.dataclass
class Options:
    source: str
    output: str
    resolution: str = "original"
    quality: str = "standard"
    start: float = 0.0
    duration: float | None = None
    audio_effect: str = "original"
    copy_hdr_video: bool = False


def build_command(opts: Options, info: dict, destination: Path) -> tuple[list[str], dict]:
    p = hdr_copy_properties(info) if opts.copy_hdr_video else source_properties(info)
    if opts.copy_hdr_video and opts.resolution != "original":
        raise ConversionError("给已有 HDR 加音效时，请保持原尺寸，画面将直接复制。")
    if opts.audio_effect not in ("original", "surround"):
        raise ConversionError("未知音效设置。")
    w, h = (p["width"], p["height"]) if opts.copy_hdr_video else dimensions(p, opts.resolution)
    if opts.quality not in ("fast", "standard", "fine", "lossless"):
        raise ConversionError("未知的画面编码档位。")
    preset, crf = {"fast": ("ultrafast", 17), "standard": ("fast", 17),
                   "fine": ("medium", 16), "lossless": ("fast", None)}[opts.quality]
    lossless_video = opts.quality == "lossless" and not opts.copy_hdr_video
    threads = min(8, os.cpu_count() or 4)
    # Static metadata describes our synthetic encoding target, not a measured
    # physical mastering display. 1000/1000 are conservative content upper bounds,
    # enforced by the LUT's per-channel 1000-nit ceiling, not measured MaxFALL.
    params = ("colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:range=limited:"
              "hdr10=1:hdr10-opt=0:repeat-headers=1:"
              "master-display=G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(10000000,1):"
              f"max-cll=1000,1000:pools={threads}:frame-threads=3:log-level=warning")
    if lossless_video:
        params += ":lossless=1"
    args = [find_tool("ffmpeg"), "-hide_banner", "-nostdin", "-loglevel", "warning", "-n",
            "-filter_threads", str(threads), "-threads", str(threads)]
    if opts.start:
        args += ["-ss", str(opts.start)]
    args += ["-i", opts.source]
    if opts.duration is not None:
        args += ["-t", str(opts.duration)]
    args += ["-map", f"0:{p['stream_index']}", "-map", "0:a:0?", "-map_metadata", "-1", "-map_chapters", "-1"]
    if opts.copy_hdr_video:
        args += ["-c:v", "copy", "-tag:v", "hvc1"]
    else:
        args += ["-vf", filter_chain(p, w, h), "-c:v", "libx265", "-preset", preset]
        if crf is not None:
            args += ["-crf", str(crf)]
        args += [
             "-profile:v", "main10", "-pix_fmt", "yuv420p10le", "-x265-params", params,
             "-g", str(max(1, round(p["fps"] * 2))), "-fps_mode", "passthrough", "-enc_time_base", "filter",
             "-color_primaries", "bt2020", "-color_trc", "smpte2084", "-colorspace", "bt2020nc", "-color_range", "tv",
             "-tag:v", "hvc1"]
    audio = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    sample_rate = _finite_number(audio.get("sample_rate")) if audio else None
    channels = _finite_number(audio.get("channels")) if audio else None
    audio_duration = _known_duration(audio.get("duration"), p["duration"]) if audio else 0
    if opts.audio_effect == "surround":
        if not audio:
            raise ConversionError("原片没有音轨，无法添加环绕音效。")
        args += ["-af", SURROUND_FILTER, "-c:a", "aac", "-b:a", "320k", "-ar", "48000", "-ac", "2"]
    elif (audio and audio.get("codec_name") == "aac" and channels is not None and 0 < channels <= 2
          and sample_rate is not None and 0 < sample_rate <= 48000):
        args += ["-c:a", "copy"]
    else:
        args += ["-c:a", "aac", "-b:a", "320k", "-ar", "48000", "-ac", "2"]
    args += ["-movflags", "+faststart+write_colr", "-progress", "pipe:1", "-nostats", str(destination)]
    expected_duration = min(opts.duration or float("inf"), max(0, p["duration"] - opts.start))
    return args, dict(source=p, width=w, height=h, preset=preset, crf=crf,
                      lossless_video=lossless_video,
                      audio=args[args.index("-c:a") + 1],
                      audio_effect=opts.audio_effect,
                      audio_filter=SURROUND_FILTER if opts.audio_effect == "surround" else None,
                      video_operation="copy_hdr" if opts.copy_hdr_video else "sdr_to_hdr",
                      expect_audio=bool(audio and (not audio_duration or audio_duration > opts.start)),
                      expected_video_duration=expected_duration if math.isfinite(expected_duration) else None)


def validate_export(path: Path, expected: dict) -> dict:
    data = probe(path)
    v = video_stream(data)
    checks = {"HEVC": v.get("codec_name") == "hevc", "10-bit": v.get("pix_fmt") == "yuv420p10le",
              "PQ": v.get("color_transfer") == "smpte2084", "BT2020": v.get("color_primaries") == "bt2020",
              "matrix": v.get("color_space") == "bt2020nc", "limited_range": v.get("color_range") == "tv",
              "dimensions": (v.get("width"), v.get("height")) == (expected["width"], expected["height"]),
              "hvc1_tag": v.get("codec_tag_string") == "hvc1"}
    if expected.get("expected_video_duration"):
        checks["duration"] = abs(_known_duration(v.get("duration"), data.get("format", {}).get("duration")) - expected["expected_video_duration"]) < max(.15, 3 / expected["source"]["fps"])
    if expected.get("expect_audio"):
        checks["audio_present"] = any(s.get("codec_type") == "audio" for s in data.get("streams", []))
    if expected.get("audio_effect") == "surround":
        a = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), {})
        checks["stereo_aac_48k"] = a.get("codec_name") == "aac" and a.get("channels") == 2 and a.get("sample_rate") == "48000"
    frame = capture([find_tool("ffprobe"), "-v", "error", "-select_streams", "v:0", "-read_intervals", "%+#1",
                     "-show_frames", "-of", "json", str(path)])
    frame_data = json.loads(frame.stdout) if not frame.returncode else {}
    side = [s for f in frame_data.get("frames", []) for s in f.get("side_data_list", [])]
    checks["mastering_metadata"] = any("Mastering display" in s.get("side_data_type", "") for s in side)
    checks["content_light_metadata"] = any("Content light" in s.get("side_data_type", "") for s in side)
    if not all(checks.values()):
        raise ConversionError("输出验证未通过：" + "、".join(k for k, ok in checks.items() if not ok))
    return dict(checks=checks, stream=v, first_frame_side_data=side, format=data.get("format", {}))


def _decoded_frame_hashes(command: list[str], cache: Path, cancel: threading.Event,
                         label: str) -> list[tuple[int, str]]:
    """Hash raw 10-bit frames without storing a large uncompressed video."""
    if cancel.is_set():
        raise Cancelled("无损校验已取消，原片保留。")
    process = None
    records = []
    log_path = cache / f"verify-{label}.log"
    try:
        with log_path.open("wb") as log:
            process = subprocess.Popen(command, cwd=cache, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=log, text=True,
                                       encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW)
            def stop_when_cancelled():
                while process.poll() is None:
                    if cancel.wait(.2):
                        try:
                            process.terminate()
                        except OSError:
                            pass
                        return
            watcher = threading.Thread(target=stop_when_cancelled, daemon=True)
            watcher.start()
            for line in process.stdout:
                if line.startswith("#") or not line.strip():
                    continue
                fields = [s.strip() for s in line.split(",")]
                if len(fields) != 6 or len(fields[-1]) != 64:
                    raise ConversionError("无损校验收到无法识别的逐帧摘要。")
                records.append((int(fields[-2]), fields[-1]))
            code = process.wait()
            watcher.join(timeout=1)
        if cancel.is_set():
            raise Cancelled("无损校验已取消，原片保留。")
        if code:
            raise ConversionError("无法完成无损校验：\n" + log_path.read_text(encoding="utf-8", errors="replace")[-4000:])
        if not records:
            raise ConversionError("无损校验没有读到视频画面。")
        return records
    finally:
        if process and process.poll() is None:
            process.terminate()
            process.wait(timeout=15)
        if process and process.stdout:
            process.stdout.close()


def verify_lossless(opts: Options, plan: dict, encoded: Path, cache: Path,
                    cancel: threading.Event, callback: Callable[[dict], None]) -> dict:
    """Compare EVERY decoded frame with the intended encoder input, not SDR."""
    threads = str(min(8, os.cpu_count() or 4))
    reference = [find_tool("ffmpeg"), "-hide_banner", "-nostdin", "-v", "error",
                 "-threads", threads, "-filter_threads", threads]
    if opts.start:
        reference += ["-ss", str(opts.start)]
    reference += ["-i", opts.source]
    if opts.duration is not None:
        reference += ["-t", str(opts.duration)]
    reference += ["-map", f"0:{plan['source']['stream_index']}", "-an", "-sn", "-dn",
                  "-vf", filter_chain(plan["source"], plan["width"], plan["height"])]
    suffix = ["-c:v", "rawvideo", "-threads:v", "1", "-pix_fmt", "yuv420p10le",
              "-fps_mode", "passthrough", "-f", "framehash", "-hash", "sha256", "pipe:1"]
    reference += suffix
    decoded = [find_tool("ffmpeg"), "-hide_banner", "-nostdin", "-v", "error", "-threads", threads,
               "-i", str(encoded), "-map", "0:v:0", "-an", "-sn", "-dn"] + suffix
    callback(dict(stage="verifying", percent=99.1, message="正在逐帧校验无损：计算 HDR 编码前的参考画面…"))
    before = _decoded_frame_hashes(reference, cache, cancel, "reference")
    callback(dict(stage="verifying", percent=99.5, message="正在逐帧校验无损：核对解码后的每一帧…"))
    after = _decoded_frame_hashes(decoded, cache, cancel, "decoded")
    if len(before) != len(after):
        raise ConversionError(f"无损校验未通过：参考画面 {len(before)} 帧，导出文件 {len(after)} 帧。")
    for index, (original, reconstructed) in enumerate(zip(before, after), 1):
        if original != reconstructed:
            raise ConversionError(f"无损校验未通过：第 {index} 帧与编码前不一致，未发布输出文件。")
    digest = hashlib.sha256("\n".join(f"{size}:{value}" for size, value in before).encode("ascii")).hexdigest()
    return dict(all_frames_equal=True, frames_compared=len(before), pixel_format="yuv420p10le",
                method="SHA-256 per raw decoded frame; byte count and digest both compared",
                combined_frame_digest=digest,
                scope="Exact match to the 10-bit 4:2:0 HDR filter output before encoding. This does not assert a reversible SDR-to-HDR transform or lossless audio.")


def publish_without_overwrite(temp: Path, final: Path):
    try:
        os.link(temp, final)
    except FileExistsError:
        raise ConversionError("输出路径已被其他文件占用，未覆盖任何文件。")
    except OSError:
        created = False
        try:
            with final.open("xb") as target, temp.open("rb") as source:
                created = True
                shutil.copyfileobj(source, target, 4 * 1024 * 1024)
        except FileExistsError:
            raise ConversionError("输出路径已存在，未覆盖任何文件。")
        except BaseException:
            if created:
                final.unlink(missing_ok=True)
            raise
    temp.unlink()


def convert(opts: Options, callback: Callable[[dict], None] | None = None,
            cancel: threading.Event | None = None) -> dict:
    callback = callback or (lambda _: None)
    cancel = cancel or threading.Event()
    source = Path(opts.source).expanduser().resolve()
    output = Path(opts.output).expanduser().resolve()
    if not source.is_file():
        raise ConversionError("找不到输入视频。")
    if output == source or output.exists():
        raise ConversionError("输出文件已存在或与原片相同，请换一个文件名。原片不会被覆盖。")
    if output.suffix.lower() != ".mp4":
        raise ConversionError("输出文件请使用 .mp4 后缀。")
    if opts.start < 0 or opts.duration is not None and opts.duration <= 0:
        raise ConversionError("试转起点和时长设置无效。")
    opts = dataclasses.replace(opts, source=str(source), output=str(output))
    info = probe(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    process = None
    temporary = output.with_name(f".{output.stem}.{uuid.uuid4().hex[:10]}.part.mp4")
    started = time.monotonic()
    tail = ""
    with tempfile.TemporaryDirectory(prefix="sdr2hdr-") as cache:
        cache_path = Path(cache)
        log_path = cache_path / "ffmpeg.log"
        if not opts.copy_hdr_video:
            write_curve(cache_path / "tone.cube")
        command, plan = build_command(opts, info, temporary)
        total = min(opts.duration or float("inf"), max(0, plan["source"]["duration"] - opts.start))
        if not math.isfinite(total):
            total = 0
        encoding_message = ("正在添加音效，HDR 画面直接保留…" if opts.copy_hdr_video else
                            "正在转换 HDR 并处理音效…" if opts.audio_effect == "surround" else
                            "正在转换画面并编码 HDR…")
        callback(dict(stage="encoding", percent=0, plan=plan, message=encoding_message))
        try:
            with log_path.open("wb") as log:
                process = subprocess.Popen(command, cwd=cache, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                           stderr=log, text=True, encoding="utf-8", errors="replace",
                                           creationflags=CREATE_NO_WINDOW)
                def stop_when_cancelled():
                    while process.poll() is None:
                        if cancel.wait(.2):
                            if process.poll() is None:
                                try:
                                    process.terminate()
                                except OSError:
                                    pass
                            return
                watcher = threading.Thread(target=stop_when_cancelled, daemon=True)
                watcher.start()
                progress = {}
                last_percent = 0
                last_seconds = 0.0
                for line in process.stdout:
                    key, sep, value = line.strip().partition("=")
                    if sep:
                        progress[key] = value
                    if key == "progress":
                        seconds = progress_seconds(progress, last_seconds)
                        last_seconds = seconds
                        last_percent = max(last_percent, min(98, seconds / total * 98) if total else 0)
                        speed = progress.get("speed", "")
                        if speed.strip().lower() in ("n/a", "nan", "inf", "-inf"):
                            speed = ""
                        frame = progress.get("frame", "")
                        if _finite_number(frame) is None:
                            frame = ""
                        callback(dict(stage="encoding", percent=last_percent,
                                      seconds=seconds, total=total, speed=speed,
                                      frame=frame, message=encoding_message))
                returncode = process.wait()
                watcher.join(timeout=1)
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-20000:]
            if cancel.is_set():
                raise Cancelled("转换已取消，原片保留。")
            if returncode:
                raise ConversionError("转换失败：\n" + tail[-5000:])
            callback(dict(stage="verifying", percent=99, message="正在检查 HDR、色深和编码标记…"))
            validation = validate_export(temporary, plan)
            if plan["lossless_video"]:
                validation["lossless_pixels"] = verify_lossless(opts, plan, temporary, cache_path, cancel, callback)
            if cancel.is_set():
                raise Cancelled("转换已取消，原片保留。")
            publish_without_overwrite(temporary, output)
            validation["format"]["filename"] = str(output)
            result = dict(tool_version=VERSION, algorithm="preserved HDR video stream" if opts.copy_hdr_video else ALGORITHM_ID, source=str(source), output=str(output),
                          options=dataclasses.asdict(opts), plan=plan, validation=validation,
                          elapsed_seconds=round(time.monotonic() - started, 2),
                          method="HDR video stream copy" if opts.copy_hdr_video else "float RGB matrix (UV x0.56 equivalent) + 65536-point log curve + 10-bit HEVC",
                          video_encoding="HEVC true lossless" if plan["lossless_video"] else "preserved HDR stream" if opts.copy_hdr_video else "HEVC CRF lossy",
                          audio_effect_description=("Stereo spatial enhancement through channel widening, short cross delay and peak limiting. AAC audio is re-encoded." if opts.audio_effect == "surround" else "Original audio retained where AAC stream copy is possible."),
                          nominal_mastering_target="preserved from input HDR stream" if opts.copy_hdr_video else "BT.2020 / D65 / 1000 nits; synthetic encoding target, not a measured display",
                          declared_light_bounds="preserved from input HDR stream" if opts.copy_hdr_video else "1000-nit conservative MaxCLL/MaxFALL upper bounds, enforced by RGB PQ ceiling; not measured MaxFALL",
                          fidelity_limit="Fixed tone and color adjustment changes the SDR presentation intentionally. Resampling and 10-bit quantization are part of the conversion. Extreme over-range samples are capped at 1000 nits. Online platform transcoding is outside this tool.")
            report = output.with_suffix(".hdr.json")
            try:
                with report.open("x", encoding="utf-8") as f:
                    json.dump(result, f, ensure_ascii=False, indent=2)
            except OSError:
                pass  # A valid finished video must not be reported as failed over an optional log.
            callback(dict(stage="done", percent=100, message="HDR 视频已导出并通过文件检查。", result=result))
            return result
        finally:
            if process and process.poll() is None:
                process.terminate()
                process.wait(timeout=15)
            if process and process.stdout:
                process.stdout.close()
            if temporary.exists():
                temporary.unlink()


def cli():
    import argparse
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="离线 SDR 转 HDR 与双声道空间增强")
    parser.add_argument("source")
    parser.add_argument("output")
    parser.add_argument("--resolution", choices=["original", "1440", "2160"], default="original")
    parser.add_argument("--quality", choices=["fast", "standard", "fine", "lossless"], default="standard")
    parser.add_argument("--start", type=float, default=0)
    parser.add_argument("--duration", type=float)
    parser.add_argument("--audio-effect", choices=["original", "surround"], default="original")
    parser.add_argument("--copy-hdr-video", action="store_true", help="仅给已有 HEVC HDR 加音效，不重复处理画面")
    args = parser.parse_args()
    try:
        convert(Options(**vars(args)), lambda p: print(json.dumps(p, ensure_ascii=False), flush=True))
    except ConversionError as e:
        print(str(e), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    cli()

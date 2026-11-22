"""Offline SDR-to-HDR transcoder with fixed logarithmic tone expansion.

Processing intentionally operates in encoded nonlinear RGB. The result is a
stylized HDR presentation, not a colorimetric recovery of an HDR master.
"""
from __future__ import annotations

import concurrent.futures
import dataclasses
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from fractions import Fraction
from typing import Callable

VERSION = "1.4.0"
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
M1, M2 = 2610 / 16384, 2523 / 32
C1, C2, C3 = 3424 / 4096, 2413 / 128, 2392 / 128
ALGORITHM_ID = "Fixed logarithmic SDR-to-PQ tone expansion with chroma gain 0.56"
DV_STRIP_ALGORITHM_ID = "Lossless Dolby Vision RPU/EL strip preserving native 10-bit HDR10 base layer"
DV_REENCODE_ALGORITHM_ID = "Dolby Vision to HDR10 conversion via libplacebo (apply_dolbyvision=true)"
MASTERING_DISPLAY = "G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(10000000,1)"
CONTENT_LIGHT = "1000,1000"
HW_MIN_WIDTH = 160
HW_MIN_HEIGHT = 128
HW_MAX_DIM = 8192
HARDWARE_PRIORITY = ("nvenc", "qsv", "amf")
ENCODER_SPECS = {
    "cpu": {
        "codec": "libx265",
        "label": "CPU 软件编码 (x265)",
        "short_label": "CPU (x265)",
        "hardware": False,
        "supports_lossless": True,
    },
    "nvenc": {
        "codec": "hevc_nvenc",
        "label": "NVIDIA 显卡加速 (NVENC)",
        "short_label": "NVIDIA NVENC",
        "hardware": True,
        "supports_lossless": True,
    },
    "qsv": {
        "codec": "hevc_qsv",
        "label": "Intel 显卡加速 (QSV)",
        "short_label": "Intel QSV",
        "hardware": True,
        "supports_lossless": False,
    },
    "amf": {
        "codec": "hevc_amf",
        "label": "AMD 显卡加速 (AMF)",
        "short_label": "AMD AMF",
        "hardware": True,
        "supports_lossless": False,
    },
}
_ENCODER_CACHE: dict[str, dict[str, dict]] = {}
_ENCODER_CACHE_LOCK = threading.Lock()
_DV_TOOLS_CACHE: dict[str, dict] = {}
_DV_TOOLS_CACHE_LOCK = threading.Lock()
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


def pq_decode(code: float) -> float:
    e = max(0.0, min(1.0, code)) ** (1.0 / M2)
    num = max(0.0, e - C1)
    den = C2 - C3 * e
    if den <= 0:
        return 10000.0
    return 10000.0 * ((num / den) ** (1.0 / M1))


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


def _has_dovi_side_data(side_data_list: list[dict] | None) -> bool:
    for item in side_data_list or ():
        kind = str(item.get("side_data_type") or "").upper()
        if "DOVI" in kind or "DOLBY VISION" in kind:
            return True
    return False


def _probe_first_frame_side_data(path: Path, stream_index: int) -> list[dict]:
    try:
        frame = capture([find_tool("ffprobe"), "-v", "error", "-select_streams", str(stream_index),
                         "-read_intervals", "%+#1", "-show_frames", "-of", "json", str(path)], timeout=20)
    except (subprocess.TimeoutExpired, OSError, ConversionError):
        return []
    if frame.returncode or not frame.stdout.strip():
        return []
    try:
        frame_data = json.loads(frame.stdout)
    except json.JSONDecodeError:
        return []
    return [s for f in frame_data.get("frames", []) for s in f.get("side_data_list", [])]


def _int_or_none(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def detect_dolby_vision(info: dict, path: Path | None = None) -> dict | None:
    """Inspect stream and (when needed) first-frame metadata for Dolby Vision."""
    v = video_stream(info)
    stream_side = v.get("side_data_list") or []
    dovi_rec = next((s for s in stream_side if "DOVI" in str(s.get("side_data_type") or "").upper()), None)
    dual_track_p7 = False
    if dovi_rec is None:
        for s in info.get("streams", []):
            if s.get("codec_type") != "video" or s.get("index") == v.get("index"):
                continue
            if s.get("disposition", {}).get("attached_pic"):
                continue
            other_rec = next((x for x in (s.get("side_data_list") or [])
                              if "DOVI" in str(x.get("side_data_type") or "").upper()), None)
            if other_rec is not None:
                dovi_rec = other_rec
                dual_track_p7 = True
                break

    codec_tag = str(v.get("codec_tag_string") or "").lower()
    trc = v.get("color_transfer")
    pix_fmt = str(v.get("pix_fmt") or "")
    need_frame_probe = (
        path is not None and path.is_file() and (
            dovi_rec is not None
            or codec_tag in ("dvhe", "dvh1")
            or (
                v.get("codec_name") in ("hevc", "av1")
                and pix_fmt in ("yuv420p10le", "p010le", "")
                and trc in ("smpte2084", "arib-std-b67", "unknown", "unspecified", None)
            )
        )
    )
    frame_side = _probe_first_frame_side_data(path, v["index"]) if need_frame_probe else []
    dv_meta = next((s for s in frame_side if "DOLBY VISION METADATA" in str(s.get("side_data_type") or "").upper()), None)
    has_frame_dv = dv_meta is not None or _has_dovi_side_data(frame_side)

    if dovi_rec is None and not has_frame_dv and codec_tag not in ("dvhe", "dvh1"):
        return None

    dv_profile = _int_or_none(dovi_rec.get("dv_profile")) if dovi_rec else None
    bl_compat = _int_or_none(dovi_rec.get("dv_bl_signal_compatibility_id")) if dovi_rec else None
    el_present = bool(dovi_rec and dovi_rec.get("el_present_flag")) or dual_track_p7

    if dv_profile is None and dv_meta is not None:
        vdr_profile = _int_or_none(dv_meta.get("vdr_rpu_profile"))
        disable_residual = _int_or_none(dv_meta.get("disable_residual_flag"))
        sig_cs = _int_or_none(dv_meta.get("signal_color_space"))
        bl_full = _int_or_none(dv_meta.get("bl_video_full_range_flag"))
        sig_eotf = _int_or_none(dv_meta.get("signal_eotf"))
        if sig_cs == 3 or (bl_full == 1 and trc not in ("smpte2084", "arib-std-b67")):
            dv_profile, bl_compat = 5, 0
        elif vdr_profile == 1 or disable_residual == 0:
            dv_profile, bl_compat, el_present = 7, 6, True
        elif trc == "arib-std-b67" or sig_eotf == 18:
            dv_profile, bl_compat = 8, 4
        elif trc == "smpte2084":
            dv_profile, bl_compat = 8, 1
    if dv_profile == 7:
        el_present = True

    has_hdr10_bl = (
        v.get("codec_name") == "hevc"
        and pix_fmt == "yuv420p10le"
        and trc == "smpte2084"
        and v.get("color_primaries") == "bt2020"
        and v.get("color_space") == "bt2020nc"
    )
    strip_supported = has_hdr10_bl and ((dv_profile == 8 and bl_compat == 1) or dv_profile == 7)

    if dv_profile == 5:
        profile_code, profile_label = "5", "Profile 5 (IPTPQc2)"
    elif dv_profile == 7:
        profile_code, profile_label = "7", "Profile 7 (双层蓝光 HDR10 基础层)"
    elif dv_profile == 8 and bl_compat == 1:
        profile_code, profile_label = "8.1", "Profile 8.1 (HDR10 兼容层)"
    elif dv_profile == 8 and bl_compat == 4:
        profile_code, profile_label = "8.4", "Profile 8.4 (HLG 杜比视界)"
    elif dv_profile == 8 and bl_compat == 2:
        profile_code, profile_label = "8.2", "Profile 8.2 (SDR 兼容层)"
    elif dv_profile == 8 and bl_compat is not None:
        profile_code, profile_label = f"8.{bl_compat}", f"Profile 8.{bl_compat}"
    elif dv_profile is not None:
        profile_code, profile_label = str(dv_profile), f"Profile {dv_profile}"
    else:
        profile_code, profile_label = "unknown", "未知 Profile"

    all_side = list(stream_side) + list(frame_side)
    source_has_mastering = any("Mastering display" in str(s.get("side_data_type") or "") for s in all_side)
    source_has_content_light = any("Content light" in str(s.get("side_data_type") or "") for s in all_side)
    source_max_pq = _int_or_none(dv_meta.get("source_max_pq")) if dv_meta else None

    return dict(
        dv_profile=dv_profile,
        bl_compat=bl_compat,
        profile_code=profile_code,
        profile_label=profile_label,
        el_present=el_present,
        dual_track=dual_track_p7,
        has_hdr10_bl=has_hdr10_bl,
        strip_supported=strip_supported,
        recommended_mode="strip" if strip_supported else "reencode",
        source_max_pq=source_max_pq,
        source_has_mastering=source_has_mastering,
        source_has_content_light=source_has_content_light,
    )


def _dv_static_metadata(dv_info: dict | None) -> tuple[str, str, int]:
    peak = 1000
    max_pq = dv_info.get("source_max_pq") if dv_info else None
    if isinstance(max_pq, int) and 0 < max_pq <= 4095:
        nits = pq_decode(max_pq / 4095.0)
        if nits <= 1100:
            peak = 1000
        elif nits >= 3500:
            peak = 4000
        else:
            peak = max(1000, min(4000, int(round(nits / 100.0) * 100)))
    mastering = f"G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L({peak * 10000},1)"
    content_light = f"{peak},1000"
    return mastering, content_light, peak


def is_hdr_video(info: dict, path: Path | None = None) -> bool:
    v = video_stream(info)
    if v.get("color_transfer") in ("smpte2084", "arib-std-b67") or _has_dovi_side_data(v.get("side_data_list")):
        return True
    if path is not None:
        return detect_dolby_vision(info, path) is not None
    return False


def dv_properties(info: dict, dv: dict, strip: bool) -> dict:
    v = video_stream(info)
    try:
        width, height = int(v["width"]), int(v["height"])
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ConversionError("视频没有提供有效的画面尺寸，无法开始转换。") from None
    if width <= 0 or height <= 0:
        raise ConversionError("视频的画面尺寸无效，无法开始转换。")
    if not strip and v.get("field_order") not in (None, "unknown", "progressive"):
        raise ConversionError("暂不处理隔行扫描视频。请先去隔行，避免改变运动效果。")
    rotation = next((_finite_number(s.get("rotation")) or 0 for s in v.get("side_data_list", []) if "rotation" in s), 0)
    sar = 1.0 if strip else _positive_ratio(v.get("sample_aspect_ratio"), 1.0)
    if not strip and abs(rotation) % 180 == 90:
        width, height, sar = height, width, 1 / sar
    fps = _positive_ratio(v.get("avg_frame_rate"), _positive_ratio(v.get("r_frame_rate"), 25.0))
    duration = _known_duration(v.get("duration"), info.get("format", {}).get("duration"))
    color_range = "full" if v.get("color_range") == "pc" else ("limited" if v.get("color_range") == "tv" else "unknown")
    return dict(
        width=width, height=height, sar=sar, fps=fps or 25, duration=duration,
        matrix=v.get("color_space") or "unknown",
        transfer=v.get("color_transfer") or "unknown",
        primaries=v.get("color_primaries") or "unknown",
        range=color_range, guessed=[], stream_index=v["index"],
        pixel_format=v.get("pix_fmt") or "", dv=dv,
    )


def inspect_input(path: Path, info: dict | None = None) -> dict:
    """Classify an input file as sdr, dv_strip, dv_reencode, or hdr10."""
    info = info if info is not None else probe(path)
    dv = detect_dolby_vision(info, path)
    if dv is not None:
        strip = dv["strip_supported"]
        return dict(
            kind="dv_strip" if strip else "dv_reencode",
            dv=dv,
            properties=dv_properties(info, dv, strip=strip),
            info=info,
        )
    if is_hdr_video(info):
        return dict(
            kind="hdr10",
            dv=None,
            properties=hdr_copy_properties(info),
            info=info,
        )
    return dict(
        kind="sdr",
        dv=None,
        properties=source_properties(info),
        info=info,
    )


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


def dv_filter_chain(width: int, height: int, pix_fmt: str = "yuv420p10le") -> str:
    """Convert Dolby Vision (e.g. Profile 5 IPTPQc2 / Profile 8.4 HLG) to PQ BT.2020."""
    return (
        f"libplacebo=w={width}:h={height}:format={pix_fmt}:"
        "colorspace=bt2020nc:color_primaries=bt2020:color_trc=smpte2084:range=tv:"
        "apply_dolbyvision=true:peak_detect=false:tonemapping=clip:gamut_mode=clip:"
        "dithering=ordered_fixed,setsar=1"
    )


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
    encoder: str = "auto"
    dv_mode: str = "auto"


def clear_encoder_cache() -> None:
    """Clear cached runtime encoder capability probe results."""
    with _ENCODER_CACHE_LOCK:
        _ENCODER_CACHE.clear()


def clear_dv_tools_cache() -> None:
    """Clear cached runtime Dolby Vision filter/BSF probe results."""
    with _DV_TOOLS_CACHE_LOCK:
        _DV_TOOLS_CACHE.clear()


def _hardware_encoder_args(encoder_key: str, quality: str) -> tuple[list[str], dict]:
    if encoder_key == "nvenc":
        configs = {
            "fast": (
                ["-c:v", "hevc_nvenc", "-profile:v", "main10", "-pix_fmt", "p010le",
                 "-preset", "p2", "-tune", "hq", "-rc", "vbr", "-cq", "22", "-b:v", "0"],
                dict(codec="hevc_nvenc", preset="p2", crf=None, quality_control="VBR CQ 22"),
            ),
            "standard": (
                ["-c:v", "hevc_nvenc", "-profile:v", "main10", "-pix_fmt", "p010le",
                 "-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", "20", "-b:v", "0"],
                dict(codec="hevc_nvenc", preset="p5", crf=None, quality_control="VBR CQ 20"),
            ),
            "fine": (
                ["-c:v", "hevc_nvenc", "-profile:v", "main10", "-pix_fmt", "p010le",
                 "-preset", "p7", "-tune", "hq", "-multipass", "qres", "-rc", "vbr", "-cq", "18",
                 "-b:v", "0", "-spatial-aq", "1"],
                dict(codec="hevc_nvenc", preset="p7", crf=None, quality_control="VBR CQ 18 (multipass)"),
            ),
            "lossless": (
                ["-c:v", "hevc_nvenc", "-profile:v", "main10", "-pix_fmt", "p010le",
                 "-preset", "p4", "-tune", "lossless"],
                dict(codec="hevc_nvenc", preset="p4", crf=None, quality_control="NVENC Lossless"),
            ),
        }
        return configs[quality]
    if encoder_key == "qsv":
        if quality == "lossless":
            raise ConversionError("Intel 显卡加速 (QSV) 不支持 10-bit HEVC 无损编码，画面无损档请改用“自动”或“CPU 软件编码”。")
        configs = {
            "fast": (
                ["-c:v", "hevc_qsv", "-profile:v", "main10", "-pix_fmt", "p010le",
                 "-preset", "faster", "-global_quality", "22"],
                dict(codec="hevc_qsv", preset="faster", crf=None, quality_control="ICQ 22"),
            ),
            "standard": (
                ["-c:v", "hevc_qsv", "-profile:v", "main10", "-pix_fmt", "p010le",
                 "-preset", "medium", "-global_quality", "20"],
                dict(codec="hevc_qsv", preset="medium", crf=None, quality_control="ICQ 20"),
            ),
            "fine": (
                ["-c:v", "hevc_qsv", "-profile:v", "main10", "-pix_fmt", "p010le",
                 "-preset", "slow", "-global_quality", "18"],
                dict(codec="hevc_qsv", preset="slow", crf=None, quality_control="ICQ 18"),
            ),
        }
        return configs[quality]
    if encoder_key == "amf":
        if quality == "lossless":
            raise ConversionError("AMD 显卡加速 (AMF) 不支持 10-bit HEVC 无损编码，画面无损档请改用“自动”或“CPU 软件编码”。")
        configs = {
            "fast": (
                ["-c:v", "hevc_amf", "-profile:v", "main10", "-bitdepth", "10", "-pix_fmt", "p010le",
                 "-usage", "transcoding", "-quality", "speed", "-rc", "cqp", "-qp_i", "22", "-qp_p", "22",
                 "-header_insertion_mode", "gop"],
                dict(codec="hevc_amf", preset="speed", crf=None, quality_control="CQP 22"),
            ),
            "standard": (
                ["-c:v", "hevc_amf", "-profile:v", "main10", "-bitdepth", "10", "-pix_fmt", "p010le",
                 "-usage", "transcoding", "-quality", "balanced", "-rc", "cqp", "-qp_i", "20", "-qp_p", "20",
                 "-header_insertion_mode", "gop"],
                dict(codec="hevc_amf", preset="balanced", crf=None, quality_control="CQP 20"),
            ),
            "fine": (
                ["-c:v", "hevc_amf", "-profile:v", "main10", "-bitdepth", "10", "-pix_fmt", "p010le",
                 "-usage", "transcoding", "-quality", "quality", "-rc", "cqp", "-qp_i", "18", "-qp_p", "18",
                 "-header_insertion_mode", "gop"],
                dict(codec="hevc_amf", preset="quality", crf=None, quality_control="CQP 18"),
            ),
        }
        return configs[quality]
    raise ConversionError("未知的硬件编码器。")


def _hdr_probe_checks(path: Path) -> tuple[dict[str, bool], dict, dict, list]:
    data = probe(path)
    v = video_stream(data)
    checks = {
        "HEVC": v.get("codec_name") == "hevc",
        "10-bit": v.get("pix_fmt") == "yuv420p10le",
        "PQ": v.get("color_transfer") == "smpte2084",
        "BT2020": v.get("color_primaries") == "bt2020",
        "matrix": v.get("color_space") == "bt2020nc",
        "limited_range": v.get("color_range") == "tv",
        "hvc1_tag": v.get("codec_tag_string") == "hvc1",
    }
    frame = capture([find_tool("ffprobe"), "-v", "error", "-select_streams", "v:0", "-read_intervals", "%+#1",
                     "-show_frames", "-of", "json", str(path)])
    frame_data = json.loads(frame.stdout) if not frame.returncode else {}
    side = [s for f in frame_data.get("frames", []) for s in f.get("side_data_list", [])]
    checks["mastering_metadata"] = any("Mastering display" in s.get("side_data_type", "") for s in side)
    checks["content_light_metadata"] = any("Content light" in s.get("side_data_type", "") for s in side)
    checks["no_dovi"] = (
        not _has_dovi_side_data(v.get("side_data_list"))
        and not _has_dovi_side_data(side)
        and (v.get("codec_tag_string") or "").lower() not in ("dvhe", "dvh1")
    )
    return checks, data, v, side


def _compiled_bsfs(ffmpeg_path: str) -> set[str]:
    try:
        result = capture([ffmpeg_path, "-hide_banner", "-bsfs"], timeout=15)
    except (subprocess.TimeoutExpired, OSError):
        return set()
    if result.returncode:
        return set()
    bsfs = set()
    for line in result.stdout.splitlines():
        token = line.strip()
        if token and not token.endswith(":"):
            bsfs.add(token)
    return bsfs


def _compiled_filters(ffmpeg_path: str) -> set[str]:
    try:
        result = capture([ffmpeg_path, "-hide_banner", "-filters"], timeout=15)
    except (subprocess.TimeoutExpired, OSError):
        return set()
    if result.returncode:
        return set()
    filters = set()
    for line in result.stdout.splitlines():
        match = re.match(r"^\s*[.\w]{2,3}\s+(\S+)\s+", line)
        if match:
            filters.add(match.group(1))
    return filters


def detect_dv_tools(force: bool = False) -> dict:
    """Check FFmpeg Dolby Vision BSF and libplacebo/Vulkan availability."""
    ffmpeg_path = find_tool("ffmpeg")
    with _DV_TOOLS_CACHE_LOCK:
        if not force and ffmpeg_path in _DV_TOOLS_CACHE:
            return dict(_DV_TOOLS_CACHE[ffmpeg_path])
    bsfs = _compiled_bsfs(ffmpeg_path)
    filters = _compiled_filters(ffmpeg_path)
    has_dovi_rpu = "dovi_rpu" in bsfs
    has_dovi_split = "dovi_split" in bsfs
    has_filter_units = "filter_units" in bsfs
    has_libplacebo_compiled = "libplacebo" in filters
    libplacebo_ok = False
    libplacebo_reason = ""
    if not has_libplacebo_compiled:
        libplacebo_reason = "当前 FFmpeg 未编译 libplacebo 滤镜，无法转换杜比视界 Profile 5／8.4，请安装完整 FFmpeg 构建。"
    else:
        try:
            probe_cmd = [
                ffmpeg_path, "-hide_banner", "-nostdin", "-y", "-v", "error", "-f", "lavfi",
                "-i", "color=c=gray:s=64x64:r=25:d=0.04",
                "-vf", dv_filter_chain(64, 64, pix_fmt="yuv420p10le"),
                "-f", "null", "-",
            ]
            r = capture(probe_cmd, timeout=15)
            if r.returncode == 0:
                libplacebo_ok = True
            else:
                libplacebo_reason = "libplacebo／Vulkan 初始化失败（请检查显卡驱动是否支持 Vulkan）：" + (r.stderr.strip()[-200:] or "未知错误")
        except (subprocess.TimeoutExpired, OSError):
            libplacebo_reason = "libplacebo／Vulkan 自检超时或无法启动。"
    strip_reason = "" if has_dovi_rpu else "当前 FFmpeg 缺少 dovi_rpu 比特流滤镜，无法无损剥离杜比视界，请升级完整 FFmpeg。"
    result = dict(
        dovi_rpu=has_dovi_rpu,
        dovi_split=has_dovi_split,
        filter_units=has_filter_units,
        libplacebo=libplacebo_ok,
        strip_available=has_dovi_rpu,
        strip_reason=strip_reason,
        libplacebo_reason=libplacebo_reason,
    )
    with _DV_TOOLS_CACHE_LOCK:
        _DV_TOOLS_CACHE[ffmpeg_path] = dict(result)
    return dict(result)


def _compiled_video_encoders(ffmpeg_path: str) -> set[str]:
    try:
        result = capture([ffmpeg_path, "-hide_banner", "-encoders"], timeout=15)
    except (subprocess.TimeoutExpired, OSError):
        return set()
    if result.returncode:
        return set()
    encoders = set()
    for line in result.stdout.splitlines():
        match = re.match(r"^\s*V[.\w]{5}\s+(\S+)", line)
        if match:
            encoders.add(match.group(1))
    return encoders


def _probe_hardware_encoder(ffmpeg_path: str, key: str, compiled: set[str]) -> dict:
    spec = ENCODER_SPECS[key]
    entry = {**spec, "key": key, "available": False, "lossless": False, "reason": ""}
    if spec["codec"] not in compiled:
        entry["reason"] = "当前 FFmpeg 未编译该硬件编码器"
        return entry
    enc_args, _ = _hardware_encoder_args(key, "standard")
    with tempfile.TemporaryDirectory(prefix=f"sdr2hdr-probe-{key}-") as tmp:
        probe_mp4 = Path(tmp) / "probe.mp4"
        base_prefix = [
            ffmpeg_path, "-hide_banner", "-nostdin", "-y", "-v", "error", "-f", "lavfi",
            "-mastering_display:v", MASTERING_DISPLAY,
            "-content_light:v", CONTENT_LIGHT,
            "-i", "color=c=gray:s=256x144:r=25:d=0.08",
            "-vf", "format=yuv420p10le,setparams=range=limited:color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc,format=p010le",
        ]
        hdr_suffix = [
            "-color_primaries", "bt2020", "-color_trc", "smpte2084",
            "-colorspace", "bt2020nc", "-color_range", "tv",
            "-tag:v", "hvc1", "-movflags", "+faststart+write_colr",
        ]
        try:
            result = capture([*base_prefix, *enc_args, *hdr_suffix, str(probe_mp4)], timeout=15)
        except (subprocess.TimeoutExpired, OSError):
            entry["reason"] = "硬件编码自检超时或无法启动"
            return entry
        if result.returncode:
            stderr_lower = result.stderr.lower()
            if ("mastering_display" in stderr_lower or "content_light" in stderr_lower) and any(
                token in stderr_lower for token in ("unrecognized", "option not found", "invalid", "not supported")
            ):
                entry["reason"] = "当前 FFmpeg 版本不支持硬件编码写入 HDR10 静态元数据，请升级到较新的完整 FFmpeg"
            else:
                entry["reason"] = "未检测到可用的该显卡硬件编码器或驱动不支持 10-bit HEVC"
            return entry
        try:
            checks, _, _, _ = _hdr_probe_checks(probe_mp4)
        except (ConversionError, json.JSONDecodeError, OSError, subprocess.TimeoutExpired):
            entry["reason"] = "无法验证硬件编码输出文件"
            return entry
        missing = [name for name, ok in checks.items() if not ok]
        if missing:
            entry["reason"] = "硬件编码自检未通过完整 HDR10 元数据验证：" + "、".join(missing)
            return entry
        entry["available"] = True
        if spec["supports_lossless"]:
            ll_args, _ = _hardware_encoder_args(key, "lossless")
            probe_ll = Path(tmp) / "probe_lossless.mp4"
            try:
                ll_result = capture([*base_prefix, *ll_args, *hdr_suffix, str(probe_ll)], timeout=15)
                if ll_result.returncode == 0:
                    ll_checks, _, _, _ = _hdr_probe_checks(probe_ll)
                    entry["lossless"] = all(ll_checks.values())
            except (ConversionError, json.JSONDecodeError, OSError, subprocess.TimeoutExpired):
                entry["lossless"] = False
    return entry


def detect_encoders(force: bool = False) -> dict[str, dict]:
    """Probe CPU and GPU 10-bit HEVC HDR10 encoding capabilities at runtime."""
    ffmpeg_path = find_tool("ffmpeg")
    with _ENCODER_CACHE_LOCK:
        if not force and ffmpeg_path in _ENCODER_CACHE:
            return {k: dict(v) for k, v in _ENCODER_CACHE[ffmpeg_path].items()}
    compiled = _compiled_video_encoders(ffmpeg_path)
    cpu_ok = "libx265" in compiled
    results: dict[str, dict] = {
        "cpu": {
            **ENCODER_SPECS["cpu"],
            "key": "cpu",
            "available": cpu_ok,
            "lossless": cpu_ok,
            "reason": "" if cpu_ok else "当前 FFmpeg 未包含 libx265 编码器",
        }
    }
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(HARDWARE_PRIORITY)) as pool:
        futures = {key: pool.submit(_probe_hardware_encoder, ffmpeg_path, key, compiled)
                   for key in HARDWARE_PRIORITY}
        for key in HARDWARE_PRIORITY:
            results[key] = futures[key].result()
    with _ENCODER_CACHE_LOCK:
        _ENCODER_CACHE[ffmpeg_path] = {k: dict(v) for k, v in results.items()}
    return {k: dict(v) for k, v in results.items()}


def resolve_encoder(requested: str, quality: str, width: int, height: int,
                    detected: dict | None = None) -> tuple[str, str]:
    """Resolve the active encoder key and fallback/selection note for a job."""
    if requested not in ("auto", "cpu", *HARDWARE_PRIORITY):
        raise ConversionError("未知的编码器设置。")
    detected = detected if detected is not None else detect_encoders()
    cpu_info = detected.get("cpu", {})
    hw_dims_ok = HW_MIN_WIDTH <= width <= HW_MAX_DIM and HW_MIN_HEIGHT <= height <= HW_MAX_DIM
    if requested == "cpu":
        if not cpu_info.get("available"):
            raise ConversionError(f"CPU 软件编码不可用：{cpu_info.get('reason') or '缺少 libx265'}")
        return "cpu", ""
    if requested == "auto":
        if not hw_dims_ok:
            if not cpu_info.get("available"):
                raise ConversionError(f"输出尺寸 {width}×{height} 不在显卡硬编支持范围内，且 CPU 编码器不可用。")
            return "cpu", f"输出尺寸 {width}×{height} 不在显卡硬编范围内，已自动使用 CPU (x265)"
        for key in HARDWARE_PRIORITY:
            info = detected.get(key, {})
            if info.get("available") and (quality != "lossless" or info.get("lossless")):
                return key, f"已自动启用 {info['short_label']} 硬件加速"
        if not cpu_info.get("available"):
            raise ConversionError("未检测到可用的显卡硬编或 CPU (x265) 编码器。")
        if quality == "lossless" and any(detected.get(k, {}).get("available") for k in HARDWARE_PRIORITY):
            return "cpu", "当前可用显卡不支持 10-bit HEVC 无损编码，已自动使用 CPU (x265)"
        return "cpu", "未检测到支持 10-bit HDR10 的显卡硬编，已自动使用 CPU (x265)"
    info = detected.get(requested, {})
    label = ENCODER_SPECS[requested]["label"]
    if not info.get("available"):
        raise ConversionError(f"{label} 当前不可用：{info.get('reason') or '未检测到兼容硬件'}。请改用“自动”或“CPU 软件编码”。")
    if not hw_dims_ok:
        raise ConversionError(
            f"输出尺寸 {width}×{height} 不在显卡硬编支持范围（{HW_MIN_WIDTH}×{HW_MIN_HEIGHT} 至 {HW_MAX_DIM}×{HW_MAX_DIM}）内，请改用“自动”或“CPU 软件编码”。"
        )
    if quality == "lossless" and not info.get("lossless"):
        raise ConversionError(f"{label} 不支持 10-bit HEVC 无损编码，画面无损档请改用“自动”或“CPU 软件编码”。")
    return requested, ""


def build_command(opts: Options, info: dict, destination: Path,
                  detected_encoders: dict | None = None,
                  dv_info: dict | None = None,
                  dv_tools: dict | None = None) -> tuple[list[str], dict]:
    if opts.audio_effect not in ("original", "surround"):
        raise ConversionError("未知音效设置。")
    if opts.quality not in ("fast", "standard", "fine", "lossless"):
        raise ConversionError("未知的画面编码档位。")
    if opts.dv_mode not in ("auto", "strip", "reencode"):
        raise ConversionError("未知的杜比视界处理模式。")

    dv = None
    if opts.copy_hdr_video:
        video_operation = "copy_hdr"
        p = hdr_copy_properties(info)
        if opts.resolution != "original":
            raise ConversionError("给已有 HDR 加音效时，请保持原尺寸，画面将直接复制。")
    else:
        dv = dv_info if dv_info is not None else detect_dolby_vision(info)
        if dv is not None:
            if opts.dv_mode == "strip":
                if not dv["strip_supported"]:
                    raise ConversionError(
                        f"杜比视界 {dv['profile_label']} 不含兼容的 10-bit HDR10 基础层，无法无损直剥，请使用自动模式或重编码模式。"
                    )
                if opts.resolution != "original":
                    raise ConversionError("杜比视界无损直剥不支持调整分辨率，请保持原尺寸或改用重编码模式。")
                video_operation = "dv_strip"
            elif opts.dv_mode == "reencode":
                video_operation = "dv_reencode"
            else:
                video_operation = "dv_strip" if (dv["strip_supported"] and opts.resolution == "original") else "dv_reencode"
            p = dv_properties(info, dv, strip=(video_operation == "dv_strip"))
        else:
            video_operation = "sdr_to_hdr"
            p = source_properties(info)

    stream_copy = video_operation in ("copy_hdr", "dv_strip")
    w, h = (p["width"], p["height"]) if stream_copy else dimensions(p, opts.resolution)
    lossless_video = opts.quality == "lossless" and not stream_copy
    dv_bsf = None
    mastering_str, content_light_str, peak_nits = MASTERING_DISPLAY, CONTENT_LIGHT, 1000
    tools = None

    if video_operation == "copy_hdr":
        encoder_key, encoder_note = "copy", ""
        is_hw = False
        video_codec = "copy"
        encoder_label = "HDR 画面直拷"
        preset, crf = None, None
        quality_control = "stream copy"
    elif video_operation == "dv_strip":
        tools = dv_tools if dv_tools is not None else detect_dv_tools()
        if not tools.get("dovi_rpu"):
            raise ConversionError(tools.get("strip_reason") or "当前 FFmpeg 缺少 dovi_rpu 比特流滤镜，无法无损剥离杜比视界。")
        if dv.get("el_present") or dv.get("dv_profile") == 7:
            if tools.get("dovi_split"):
                dv_bsf = "dovi_split=mode=bl,dovi_rpu=strip=1"
            elif tools.get("filter_units"):
                dv_bsf = "filter_units=remove_types=63,dovi_rpu=strip=1"
            else:
                raise ConversionError("当前 FFmpeg 缺少 dovi_split／filter_units 比特流滤镜，无法剥离 Profile 7 增强层。")
        else:
            dv_bsf = "dovi_rpu=strip=1"
        encoder_key, encoder_note = "copy", f"杜比视界 {dv['profile_label']}：无损剥离杜比视界层，保留原生 HDR10 基础层"
        is_hw = False
        video_codec = "copy"
        encoder_label = "无损直剥 (Stream Copy)"
        preset, crf = None, None
        quality_control = "stream copy (dovi strip)"
    else:
        if video_operation == "dv_reencode":
            tools = dv_tools if dv_tools is not None else detect_dv_tools()
            if not tools.get("libplacebo"):
                raise ConversionError(tools.get("libplacebo_reason") or "当前 FFmpeg 或显卡驱动不支持 libplacebo／Vulkan，无法转换杜比视界。")
            mastering_str, content_light_str, peak_nits = _dv_static_metadata(dv)
        encoder_key, encoder_note = resolve_encoder(opts.encoder, opts.quality, w, h, detected=detected_encoders)
        is_hw = encoder_key in HARDWARE_PRIORITY
        encoder_label = ENCODER_SPECS[encoder_key]["label"]

    filter_threads = min(16, os.cpu_count() or 4) if is_hw else min(8, os.cpu_count() or 4)
    codec_threads = min(8, os.cpu_count() or 4)
    args = [find_tool("ffmpeg"), "-hide_banner", "-nostdin", "-loglevel", "warning", "-n",
            "-filter_threads", str(filter_threads), "-threads", str(codec_threads)]
    if opts.start:
        args += ["-ss", str(opts.start)]
    if is_hw:
        args += [
            f"-mastering_display:{p['stream_index']}", mastering_str,
            f"-content_light:{p['stream_index']}", content_light_str,
        ]
    args += ["-i", opts.source]
    if opts.duration is not None:
        args += ["-t", str(opts.duration)]
    args += ["-map", f"0:{p['stream_index']}", "-map", "0:a:0?", "-map_metadata", "-1", "-map_chapters", "-1"]

    if video_operation == "copy_hdr":
        args += ["-c:v", "copy", "-tag:v", "hvc1"]
    elif video_operation == "dv_strip":
        args += ["-c:v", "copy", "-bsf:v", dv_bsf, "-tag:v", "hvc1"]
    elif is_hw:
        enc_args, enc_meta = _hardware_encoder_args(encoder_key, opts.quality)
        video_codec = enc_meta["codec"]
        preset = enc_meta["preset"]
        crf = enc_meta["crf"]
        quality_control = enc_meta["quality_control"]
        if video_operation == "dv_reencode":
            # Direct p010le output in libplacebo is >3x faster at 4K for lossy encodes;
            # lossless mode uses exact yuv420p10le first for bit-exact SHA-256 verification.
            vf = (dv_filter_chain(w, h, pix_fmt="yuv420p10le") + ",format=p010le"
                  if lossless_video else dv_filter_chain(w, h, pix_fmt="p010le"))
        else:
            # Append p010le after setsar=1 rather than replacing yuv420p10le so zscale's
            # bit-exact 10-bit dithered plane layout is preserved before semi-planar packing.
            vf = filter_chain(p, w, h) + ",format=p010le"
        args += [
            "-vf", vf,
            *enc_args,
            "-g", str(max(1, round(p["fps"] * 2))), "-fps_mode", "passthrough", "-enc_time_base", "filter",
            "-color_primaries", "bt2020", "-color_trc", "smpte2084", "-colorspace", "bt2020nc", "-color_range", "tv",
            "-tag:v", "hvc1",
        ]
        if video_operation == "dv_reencode" and tools and tools.get("dovi_rpu"):
            args += ["-bsf:v", "dovi_rpu=strip=1"]
    else:
        preset, crf = {"fast": ("ultrafast", 17), "standard": ("fast", 17),
                       "fine": ("medium", 16), "lossless": ("fast", None)}[opts.quality]
        video_codec = "libx265"
        quality_control = "x265 lossless=1" if lossless_video else f"CRF {crf}"
        vf = dv_filter_chain(w, h, pix_fmt="yuv420p10le") if video_operation == "dv_reencode" else filter_chain(p, w, h)
        params = ("colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:range=limited:"
                  "hdr10=1:hdr10-opt=0:repeat-headers=1:"
                  f"master-display={mastering_str}:"
                  f"max-cll={content_light_str}:pools={codec_threads}:frame-threads=3:log-level=warning")
        if lossless_video:
            params += ":lossless=1"
        args += ["-vf", vf, "-c:v", "libx265", "-preset", preset]
        if crf is not None:
            args += ["-crf", str(crf)]
        args += [
            "-profile:v", "main10", "-pix_fmt", "yuv420p10le", "-x265-params", params,
            "-g", str(max(1, round(p["fps"] * 2))), "-fps_mode", "passthrough", "-enc_time_base", "filter",
            "-color_primaries", "bt2020", "-color_trc", "smpte2084", "-colorspace", "bt2020nc", "-color_range", "tv",
            "-tag:v", "hvc1",
        ]
        if video_operation == "dv_reencode" and tools and tools.get("dovi_rpu"):
            args += ["-bsf:v", "dovi_rpu=strip=1"]

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
                      encoder_requested="copy" if stream_copy else opts.encoder,
                      encoder=encoder_key,
                      video_codec=video_codec,
                      encoder_label=encoder_label,
                      hardware_accelerated=is_hw,
                      encoder_note=encoder_note,
                      quality_control=quality_control,
                      audio=args[args.index("-c:a") + 1],
                      audio_effect=opts.audio_effect,
                      audio_filter=SURROUND_FILTER if opts.audio_effect == "surround" else None,
                      video_operation=video_operation,
                      dolby_vision=dv,
                      dv_bsf=dv_bsf,
                      mastering_display=mastering_str,
                      content_light=content_light_str,
                      nominal_peak_nits=peak_nits,
                      clipped_stream_copy=bool(stream_copy and (opts.start or opts.duration is not None)),
                      require_limited_range=(p.get("range") == "limited") if video_operation == "dv_strip" else True,
                      require_mastering_metadata=bool(dv.get("source_has_mastering", False)) if video_operation == "dv_strip" else True,
                      require_content_light_metadata=bool(dv.get("source_has_content_light", False)) if video_operation == "dv_strip" else True,
                      expect_audio=bool(audio and (not audio_duration or audio_duration > opts.start)),
                      expected_video_duration=expected_duration if math.isfinite(expected_duration) else None)


def validate_export(path: Path, expected: dict) -> dict:
    checks, data, v, side = _hdr_probe_checks(path)
    checks["dimensions"] = (v.get("width"), v.get("height")) == (expected["width"], expected["height"])
    optional_metadata = {}
    if expected.get("video_operation") == "dv_strip":
        if not expected.get("require_limited_range", True):
            optional_metadata["limited_range"] = checks.pop("limited_range")
        if not expected.get("require_mastering_metadata", True):
            optional_metadata["mastering_metadata"] = checks.pop("mastering_metadata")
        if not expected.get("require_content_light_metadata", True):
            optional_metadata["content_light_metadata"] = checks.pop("content_light_metadata")
    if expected.get("expected_video_duration"):
        actual_duration = _known_duration(v.get("duration"), data.get("format", {}).get("duration"))
        if expected.get("clipped_stream_copy"):
            checks["duration"] = actual_duration > 0
        else:
            checks["duration"] = abs(actual_duration - expected["expected_video_duration"]) < max(.15, 3 / expected["source"]["fps"])
    if expected.get("expect_audio"):
        checks["audio_present"] = any(s.get("codec_type") == "audio" for s in data.get("streams", []))
    if expected.get("audio_effect") == "surround":
        a = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), {})
        checks["stereo_aac_48k"] = a.get("codec_name") == "aac" and a.get("channels") == 2 and a.get("sample_rate") == "48000"
    if not all(checks.values()):
        raise ConversionError("输出验证未通过：" + "、".join(k for k, ok in checks.items() if not ok))
    result = dict(checks=checks, stream=v, first_frame_side_data=side, format=data.get("format", {}))
    if optional_metadata:
        result["optional_metadata"] = optional_metadata
    return result


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
    ref_filter = (
        dv_filter_chain(plan["width"], plan["height"], pix_fmt="yuv420p10le")
        if plan.get("video_operation") == "dv_reencode"
        else filter_chain(plan["source"], plan["width"], plan["height"])
    )
    reference += ["-map", f"0:{plan['source']['stream_index']}", "-an", "-sn", "-dn", "-vf", ref_filter]
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
        dv_info = None if opts.copy_hdr_video else detect_dolby_vision(info, source)
        command, plan = build_command(opts, info, temporary, dv_info=dv_info)
        if plan["video_operation"] == "sdr_to_hdr":
            write_curve(cache_path / "tone.cube")
        total = min(opts.duration or float("inf"), max(0, plan["source"]["duration"] - opts.start))
        if not math.isfinite(total):
            total = 0
        op = plan["video_operation"]
        if op == "copy_hdr":
            encoding_message = "正在添加音效，HDR 画面直接保留…"
        elif op == "dv_strip":
            encoding_message = (
                "正在无损剥离杜比视界并处理环绕音效…" if opts.audio_effect == "surround"
                else "正在无损剥离杜比视界并导出 HDR10…"
            )
        elif op == "dv_reencode":
            encoding_message = (
                f"正在使用 libplacebo + {plan['encoder_label']} 转换杜比视界并处理音效…" if opts.audio_effect == "surround"
                else f"正在使用 libplacebo + {plan['encoder_label']} 将杜比视界转换为 HDR10…"
            )
        else:
            encoding_message = (
                f"正在使用 {plan['encoder_label']} 转换画面并处理音效…" if opts.audio_effect == "surround"
                else f"正在使用 {plan['encoder_label']} 转换画面并编码 HDR…"
            )
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
            if op == "copy_hdr":
                algorithm_desc = "preserved HDR video stream"
                method_desc = "HDR video stream copy"
                video_enc_desc = "preserved HDR stream"
                mastering_desc = "preserved from input HDR stream"
                light_desc = "preserved from input HDR stream"
                fidelity_desc = "Original HDR10 video packets are copied without re-encoding. Online platform transcoding is outside this tool."
            elif op == "dv_strip":
                algorithm_desc = DV_STRIP_ALGORITHM_ID
                method_desc = f"Dolby Vision lossless stream-copy strip ({plan['dv_bsf']})"
                video_enc_desc = "preserved HDR10 base layer (stream copy)"
                mastering_desc = "preserved from input HDR10 base layer if present"
                light_desc = "preserved from input HDR10 base layer if present"
                fidelity_desc = "Dolby Vision RPU (and Profile 7 enhancement layer when present) is removed without re-encoding the 10-bit HDR10 base layer. Profile 7 FEL luminance deltas are not baked in."
            elif op == "dv_reencode":
                peak_nits = plan.get("nominal_peak_nits", 1000)
                algorithm_desc = DV_REENCODE_ALGORITHM_ID
                method_desc = f"libplacebo Dolby Vision reshaping/color conversion + 10-bit HEVC ({plan['video_codec']})"
                video_enc_desc = f"{plan['video_codec']} true lossless" if plan["lossless_video"] else f"{plan['video_codec']} ({plan['quality_control']})"
                mastering_desc = f"BT.2020 / D65 / {peak_nits} nits synthetic HDR10 target derived from source metadata"
                light_desc = f"{peak_nits}/1000 nits declared HDR10 static bounds; not per-frame measured MaxFALL"
                fidelity_desc = "Dolby Vision metadata is applied and converted into static 10-bit PQ/BT.2020 HDR10 via libplacebo. Online platform transcoding is outside this tool."
            else:
                algorithm_desc = ALGORITHM_ID
                method_desc = f"float RGB matrix (UV x0.56 equivalent) + 65536-point log curve + 10-bit HEVC ({plan['video_codec']})"
                video_enc_desc = f"{plan['video_codec']} true lossless" if plan["lossless_video"] else f"{plan['video_codec']} ({plan['quality_control']})"
                mastering_desc = "BT.2020 / D65 / 1000 nits; synthetic encoding target, not a measured display"
                light_desc = "1000-nit conservative MaxCLL/MaxFALL upper bounds, enforced by RGB PQ ceiling; not measured MaxFALL"
                fidelity_desc = "Fixed tone and color adjustment changes the SDR presentation intentionally. Resampling and 10-bit quantization are part of the conversion. Extreme over-range samples are capped at 1000 nits. Online platform transcoding is outside this tool."
            result = dict(tool_version=VERSION, algorithm=algorithm_desc, source=str(source), output=str(output),
                          options=dataclasses.asdict(opts), plan=plan, validation=validation,
                          elapsed_seconds=round(time.monotonic() - started, 2),
                          encoder=plan["encoder"],
                          video_codec=plan["video_codec"],
                          hardware_accelerated=plan["hardware_accelerated"],
                          dolby_vision=plan.get("dolby_vision"),
                          method=method_desc,
                          video_encoding=video_enc_desc,
                          audio_effect_description=("Stereo spatial enhancement through channel widening, short cross delay and peak limiting. AAC audio is re-encoded." if opts.audio_effect == "surround" else "Original audio retained where AAC stream copy is possible."),
                          nominal_mastering_target=mastering_desc,
                          declared_light_bounds=light_desc,
                          fidelity_limit=fidelity_desc)
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
    parser = argparse.ArgumentParser(description="离线 SDR／杜比视界转 HDR10 与双声道空间增强")
    parser.add_argument("source", nargs="?")
    parser.add_argument("output", nargs="?")
    parser.add_argument("--resolution", choices=["original", "1440", "2160"], default="original")
    parser.add_argument("--quality", choices=["fast", "standard", "fine", "lossless"], default="standard")
    parser.add_argument("--encoder", choices=["auto", "cpu", "nvenc", "qsv", "amf"], default="auto")
    parser.add_argument("--dv-mode", choices=["auto", "strip", "reencode"], default="auto",
                        help="杜比视界模式：auto 自动选择（8.1/7 无损直剥，5/8.4 重编码），或手动指定 strip/reencode")
    parser.add_argument("--list-encoders", action="store_true", help="检测并列出当前环境可用的编码器")
    parser.add_argument("--list-dv-tools", action="store_true", help="检测并列出杜比视界转换所需 FFmpeg 组件状态")
    parser.add_argument("--start", type=float, default=0)
    parser.add_argument("--duration", type=float)
    parser.add_argument("--audio-effect", choices=["original", "surround"], default="original")
    parser.add_argument("--copy-hdr-video", action="store_true", help="仅给已有 HEVC HDR 加音效，不重复处理画面")
    args = parser.parse_args()
    try:
        if args.list_encoders:
            print(json.dumps(detect_encoders(), ensure_ascii=False, indent=2), flush=True)
            return
        if args.list_dv_tools:
            print(json.dumps(detect_dv_tools(), ensure_ascii=False, indent=2), flush=True)
            return
        if not args.source or not args.output:
            parser.error("请提供输入视频和输出路径（或使用 --list-encoders / --list-dv-tools 查看环境状态）。")
        option_fields = {f.name for f in dataclasses.fields(Options)}
        opts = Options(**{k: v for k, v in vars(args).items() if k in option_fields})
        convert(opts, lambda p: print(json.dumps(p, ensure_ascii=False), flush=True))
    except ConversionError as e:
        print(str(e), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    cli()

"""Offline SDR / Dolby Vision to HDR10 transcoder.

SDR-to-HDR offers three styles:
- balanced (default): colorimetric BT.1886 linear BT.709->BT.2020 luminance-preserving
  expansion with deep shadows, bright midtones, and highlight headroom up to 750 nits.
- natural: softer colorimetric BT.1886 luminance-preserving curve capped at 450 nits
  for dark-room viewing and conservative highlight protection.
- vivid (legacy): fixed logarithmic expansion in nonlinear RGB for stylized high-contrast
  presentations such as anime and dark scenes.
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

VERSION = "1.5.4"
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
M1, M2 = 2610 / 16384, 2523 / 32
C1, C2, C3 = 3424 / 4096, 2413 / 128, 2392 / 128
ALGORITHM_ID = "Fixed logarithmic SDR-to-PQ tone expansion with chroma gain 0.56"
BALANCED_ALGORITHM_ID = "Colorimetric BT.1886 linear BT.709-to-BT.2020 luminance-preserving balanced midtone & highlight-protected SDR-to-PQ expansion (750 nits peak)"
NATURAL_ALGORITHM_ID = "Colorimetric BT.1886 linear BT.709-to-BT.2020 luminance-preserving highlight-protected SDR-to-PQ expansion (450 nits peak)"
DV_STRIP_ALGORITHM_ID = "Lossless Dolby Vision RPU/EL strip preserving native 10-bit HDR10 base layer"
DV_REENCODE_ALGORITHM_ID = "Dolby Vision to HDR10 conversion via libplacebo (apply_dolbyvision=true)"
MASTERING_DISPLAY = "G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(10000000,1)"
CONTENT_LIGHT = "1000,1000"
BALANCED_CONTENT_LIGHT = "750,260"
BALANCED_BASE_NITS = 255.0
BALANCED_MID_LIFT = 0.22
BALANCED_SHADOW_RAMP = 0.08
BALANCED_KNEE = 0.68
BALANCED_BOOST_NITS = 495.0
BALANCED_POWER = 2.3
BALANCED_PEAK_NITS = 750.0
NATURAL_CONTENT_LIGHT = "450,200"
NATURAL_BASE_NITS = 190.0
NATURAL_KNEE = 0.62
NATURAL_BOOST_NITS = 260.0
NATURAL_PEAK_NITS = 450.0
NATURAL_LUT_SIZE = 49
STYLE_LUT_NAMES = {"balanced": "balanced.cube", "natural": "natural.cube", "vivid": "tone.cube"}
STYLE_SHADER_NAMES = {"balanced": "balanced.hook", "natural": "natural.hook", "vivid": "vivid.hook"}
M_BT709_TO_BT2020 = (
    (0.6274040, 0.3292820, 0.0433136),
    (0.0690970, 0.9195400, 0.0113612),
    (0.0163916, 0.0880132, 0.8955950),
)
Y_BT2020 = (0.2627, 0.6780, 0.0593)
_NATURAL_LUT_CACHE: dict[tuple[str, int], str] = {}
_NATURAL_LUT_LOCK = threading.Lock()
_GPU_FILTER_CACHE: dict[str, dict] = {}
_GPU_FILTER_CACHE_LOCK = threading.Lock()
PRIMARY_TO_BT2020 = {
    "bt709": M_BT709_TO_BT2020,
    "smpte170m": (
        (0.5952542, 0.3493139, 0.0554319),
        (0.0812437, 0.8915033, 0.0272530),
        (0.0155123, 0.0819116, 0.9025760),
    ),
    "bt470bg": (
        (0.6550368, 0.3021610, 0.0428023),
        (0.0721406, 0.9166311, 0.0112283),
        (0.0171134, 0.0978535, 0.8850332),
    ),
}
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


def format_duration_clock(seconds: float | None) -> str:
    """Format a non-negative duration in seconds as MM:SS or H:MM:SS."""
    number = _finite_number(seconds)
    if number is None or number < 0:
        return "--:--"
    total_secs = max(0, int(round(number)))
    hours, rem = divmod(total_secs, 3600)
    mins, secs = divmod(rem, 60)
    if hours > 0:
        return f"{hours}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


def parse_speed_factor(speed: str | float | int | None) -> float | None:
    """Parse an FFmpeg speed indicator (e.g. '2.67x') into a positive float."""
    if isinstance(speed, bool) or speed is None:
        return None
    raw = str(speed).strip() if not isinstance(speed, (int, float)) else speed
    if isinstance(raw, str):
        if raw.lower().endswith("x"):
            raw = raw[:-1].strip()
        if not raw:
            return None
    number = _finite_number(raw)
    if number is None or number <= 0:
        return None
    return number


def estimate_eta_seconds(seconds: float, total: float, elapsed: float,
                         speed: str | float | None = None) -> float | None:
    """Estimate remaining transcoding seconds by blending average rate and speed."""
    sec_num = _finite_number(seconds)
    tot_num = _finite_number(total)
    el_num = _finite_number(elapsed)
    if tot_num is None or tot_num <= 0 or sec_num is None or sec_num < 0:
        return None
    remaining = max(0.0, tot_num - sec_num)
    if remaining == 0.0 and sec_num >= tot_num:
        return 0.0
    avg_rate = (sec_num / el_num) if (sec_num > 0.05 and el_num is not None and el_num > 0.25) else None
    inst_rate = parse_speed_factor(speed)
    if avg_rate is not None and inst_rate is not None:
        rate = 0.6 * avg_rate + 0.4 * inst_rate
    elif avg_rate is not None:
        rate = avg_rate
    else:
        rate = inst_rate
    if rate is None or not math.isfinite(rate) or rate <= 1e-6:
        return None
    eta = remaining / rate
    if not math.isfinite(eta):
        return None
    return max(0.0, eta)


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


def capture(args: list[str], timeout=60, cwd: Path | str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          encoding="utf-8", errors="replace", timeout=timeout,
                          cwd=cwd, creationflags=CREATE_NO_WINDOW)


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
        and v.get("color_space") in ("bt2020nc", "unknown", "unspecified", None)
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
        pixel_format=v.get("pix_fmt") or "", codec_name=v.get("codec_name", ""), dv=dv,
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
                guessed=guessed, stream_index=v["index"], pixel_format=pix_fmt,
                codec_name=v.get("codec_name", ""), rotation=rotation)


def dimensions(p: dict, resolution="original") -> tuple[int, int]:
    w, h = p["width"] * p["sar"], p["height"]
    if resolution != "original":
        target = int(resolution)
        ratio = target / min(w, h)
        w, h = w * ratio, h * ratio
    return max(2, round(w / 2) * 2), max(2, round(h / 2) * 2)


def balanced_target_nits(y: float) -> float:
    """Luminance-preserving balanced SDR->HDR curve (deep blacks, bright midtones, 750 nits peak)."""
    y = max(0.0, y)
    if y >= 1.0:
        return BALANCED_PEAK_NITS
    s = min(1.0, y / BALANCED_SHADOW_RAMP)
    smooth_s = s * s * (3.0 - 2.0 * s)
    l_diff = BALANCED_BASE_NITS * y * (1.0 + BALANCED_MID_LIFT * 4.0 * y * (1.0 - y) * smooth_s)
    if y <= BALANCED_KNEE:
        return l_diff
    t = (y - BALANCED_KNEE) / (1.0 - BALANCED_KNEE)
    return l_diff + BALANCED_BOOST_NITS * (t ** BALANCED_POWER)


def natural_target_nits(y: float) -> float:
    """Luminance-preserving highlight-protected SDR->HDR target brightness (nits)."""
    y = max(0.0, y)
    if y <= NATURAL_KNEE:
        return NATURAL_BASE_NITS * y
    if y <= 1.0:
        t = (y - NATURAL_KNEE) / (1.0 - NATURAL_KNEE)
        return NATURAL_BASE_NITS * y + NATURAL_BOOST_NITS * (t * t)
    return NATURAL_PEAK_NITS


def _colorimetric_reference_rgb(rgb: tuple[float, float, float], target_fn: Callable[[float], float],
                                peak_cap: float) -> tuple[float, float, float]:
    """Analytical BT.1886 linear BT.709->BT.2020 luminance-preserving PQ transform."""
    r0, g0, b0 = (max(0.0, min(1.0, v)) ** 2.4 for v in rgb)
    r20 = M_BT709_TO_BT2020[0][0] * r0 + M_BT709_TO_BT2020[0][1] * g0 + M_BT709_TO_BT2020[0][2] * b0
    g20 = M_BT709_TO_BT2020[1][0] * r0 + M_BT709_TO_BT2020[1][1] * g0 + M_BT709_TO_BT2020[1][2] * b0
    b20 = M_BT709_TO_BT2020[2][0] * r0 + M_BT709_TO_BT2020[2][1] * g0 + M_BT709_TO_BT2020[2][2] * b0
    y = Y_BT2020[0] * r20 + Y_BT2020[1] * g20 + Y_BT2020[2] * b20
    if y <= 1e-9:
        return (0.0, 0.0, 0.0)
    gain = target_fn(y) / y
    rn, gn, bn = r20 * gain, g20 * gain, b20 * gain
    peak = max(rn, gn, bn)
    if peak > peak_cap:
        scale = peak_cap / peak
        rn, gn, bn = rn * scale, gn * scale, bn * scale
    return (pq_encode(rn), pq_encode(gn), pq_encode(bn))


def balanced_reference_rgb(rgb: tuple[float, float, float]) -> tuple[float, float, float]:
    """Analytical BT.1886 linear BT.709->BT.2020 balanced 750-nit PQ transform."""
    return _colorimetric_reference_rgb(rgb, balanced_target_nits, BALANCED_PEAK_NITS)


def natural_reference_rgb(rgb: tuple[float, float, float]) -> tuple[float, float, float]:
    """Analytical BT.1886 linear BT.709->BT.2020 luminance-preserving PQ transform."""
    return _colorimetric_reference_rgb(rgb, natural_target_nits, NATURAL_PEAK_NITS)


def _write_colorimetric_lut(path: Path, style: str, size: int = NATURAL_LUT_SIZE):
    """Write a cached 49^3 tetrahedral 3D LUT for balanced or natural HDR10."""
    cache_key = (style, size)
    with _NATURAL_LUT_LOCK:
        cached = _NATURAL_LUT_CACHE.get(cache_key)
    if cached is None:
        inv = 1.0 / (size - 1)
        pow_tbl = [(i * inv) ** 2.4 for i in range(size)]
        m00, m01, m02 = M_BT709_TO_BT2020[0]
        m10, m11, m12 = M_BT709_TO_BT2020[1]
        m20, m21, m22 = M_BT709_TO_BT2020[2]
        yr, yg, yb = Y_BT2020
        is_balanced = style == "balanced"
        if not is_balanced and style != "natural":
            raise ConversionError("未知的 HDR 风格设置。")
        cap = BALANCED_PEAK_NITS if is_balanced else NATURAL_PEAK_NITS
        title = (
            'TITLE "Colorimetric Balanced Highlight-Protected SDR-to-PQ (750 nits)"'
            if is_balanced else 'TITLE "Colorimetric Highlight-Protected SDR-to-PQ (450 nits)"'
        )
        knee = NATURAL_KNEE
        inv_span = 1.0 / (1.0 - knee)
        base = NATURAL_BASE_NITS
        boost = NATURAL_BOOST_NITS
        lines = [f"{title}\nLUT_3D_SIZE {size}\nDOMAIN_MIN 0 0 0\nDOMAIN_MAX 1 1 1\n"]
        append = lines.append
        for bi in range(size):
            b0 = pow_tbl[bi]
            b_r, b_g, b_b = m02 * b0, m12 * b0, m22 * b0
            for gi in range(size):
                g0 = pow_tbl[gi]
                gb_r, gb_g, gb_b = m01 * g0 + b_r, m11 * g0 + b_g, m21 * g0 + b_b
                for ri in range(size):
                    r0 = pow_tbl[ri]
                    r20 = m00 * r0 + gb_r
                    g20 = m10 * r0 + gb_g
                    b20 = m20 * r0 + gb_b
                    y = yr * r20 + yg * g20 + yb * b20
                    if y <= 1e-9:
                        append("0.0000000 0.0000000 0.0000000\n")
                        continue
                    if is_balanced:
                        tn = balanced_target_nits(y)
                    elif y <= knee:
                        tn = base * y
                    else:
                        t = (y - knee) * inv_span
                        tn = base * y + boost * t * t
                    gain = tn / y
                    rn, gn, bn = r20 * gain, g20 * gain, b20 * gain
                    peak = rn if rn >= gn and rn >= bn else (gn if gn >= bn else bn)
                    if peak > cap:
                        s = cap / peak
                        rn *= s
                        gn *= s
                        bn *= s
                    xr = (rn * 0.0001) ** M1
                    pr = ((C1 + C2 * xr) / (1.0 + C3 * xr)) ** M2
                    xg = (gn * 0.0001) ** M1
                    pg = ((C1 + C2 * xg) / (1.0 + C3 * xg)) ** M2
                    xb = (bn * 0.0001) ** M1
                    pb = ((C1 + C2 * xb) / (1.0 + C3 * xb)) ** M2
                    append(f"{pr:.7f} {pg:.7f} {pb:.7f}\n")
        cached = "".join(lines)
        with _NATURAL_LUT_LOCK:
            _NATURAL_LUT_CACHE[cache_key] = cached
    path.write_text(cached, encoding="ascii", newline="\n")


def write_balanced_lut(path: Path, size: int = NATURAL_LUT_SIZE):
    """Write a cached 49^3 tetrahedral 3D LUT for balanced 750-nit cinema HDR10."""
    _write_colorimetric_lut(path, "balanced", size)


def write_natural_lut(path: Path, size: int = NATURAL_LUT_SIZE):
    """Write a cached 49^3 tetrahedral 3D LUT for natural highlight-protected HDR10."""
    _write_colorimetric_lut(path, "natural", size)


def write_curve(path: Path, peak=1000.0):
    # Divide the mixed input by four, then index a 65,536-sample LUT representing
    # [0, 4]. This preserves super-whites and avoids a coarse 3D approximation.
    with path.open("w", encoding="ascii", newline="\n") as f:
        f.write('TITLE "Logarithmic SDR-to-PQ curve; nominal 1000-nit ceiling"\nLUT_1D_SIZE 65536\nDOMAIN_MIN 0 0 0\nDOMAIN_MAX 1 1 1\n')
        for i in range(65536):
            value = curve(i * 4 / 65535, peak)
            f.write(f"{value:.10f} {value:.10f} {value:.10f}\n")


def filter_chain(p: dict, width: int, height: int, lut_name="tone.cube", style="vivid") -> str:
    # LUT filename is controlled by the application; FFmpeg runs in a private
    # temporary directory. No user filename is interpolated into filter syntax.
    if style in ("balanced", "natural"):
        if lut_name != STYLE_LUT_NAMES[style]:
            raise ValueError("Unexpected LUT filename")
        flags = (f"pin={p['primaries']}:tin={p['transfer']}:min={p['matrix']}:rin={p['range']}:"
                 "p=bt709:t=bt709:m=gbr:r=full:filter=spline36")
        return ",".join([
            "zscale=" + flags, "format=gbrpf32le",
            f"lut3d=file={lut_name}:interp=tetrahedral",
            "setparams=range=full:color_primaries=bt2020:color_trc=smpte2084:colorspace=gbr",
            f"zscale=pin=bt2020:tin=smpte2084:min=gbr:rin=full:p=bt2020:t=smpte2084:m=bt2020nc:r=limited:w={width}:h={height}:filter=spline36:d=error_diffusion",
            "format=yuv420p10le", "setsar=1",
        ])
    if style != "vivid":
        raise ConversionError("未知的 HDR 风格设置。")
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


def write_balanced_shader(path: Path, primaries: str = "bt709"):
    """Write a self-contained MAIN-stage GLSL shader for balanced 750-nit cinema HDR10."""
    m = PRIMARY_TO_BT2020.get(primaries, M_BT709_TO_BT2020)
    yr, yg, yb = Y_BT2020
    span = 1.0 - BALANCED_KNEE
    lift4 = BALANCED_MID_LIFT * 4.0
    shader = (
        "//!HOOK MAIN\n"
        "//!BIND HOOKED\n"
        "vec4 hook() {\n"
        "    vec4 c = HOOKED_texOff(0);\n"
        "    vec3 lin = pow(clamp(c.rgb, 0.0, 1.0), vec3(2.4));\n"
        "    vec3 b20 = vec3(\n"
        f"        dot(lin, vec3({m[0][0]:.7f}, {m[0][1]:.7f}, {m[0][2]:.7f})),\n"
        f"        dot(lin, vec3({m[1][0]:.7f}, {m[1][1]:.7f}, {m[1][2]:.7f})),\n"
        f"        dot(lin, vec3({m[2][0]:.7f}, {m[2][1]:.7f}, {m[2][2]:.7f}))\n"
        "    );\n"
        f"    float y = dot(b20, vec3({yr:.4f}, {yg:.4f}, {yb:.4f}));\n"
        "    if (y <= 1e-9) return vec4(0.0, 0.0, 0.0, c.a);\n"
        f"    float s = clamp(y / {BALANCED_SHADOW_RAMP:.6f}, 0.0, 1.0);\n"
        "    float smooth_s = s * s * (3.0 - 2.0 * s);\n"
        f"    float yc = min(y, 1.0);\n"
        f"    float l_diff = {BALANCED_BASE_NITS:.6f} * yc * (1.0 + {lift4:.6f} * yc * (1.0 - yc) * smooth_s);\n"
        f"    float tn = (y >= 1.0) ? {BALANCED_PEAK_NITS:.6f} : ((y <= {BALANCED_KNEE:.6f}) ? l_diff : "
        f"(l_diff + {BALANCED_BOOST_NITS:.6f} * pow((y - {BALANCED_KNEE:.6f}) / {span:.6f}, {BALANCED_POWER:.6f})));\n"
        "    vec3 nits = b20 * (tn / y);\n"
        "    float pk = max(nits.r, max(nits.g, nits.b));\n"
        f"    if (pk > {BALANCED_PEAK_NITS:.6f}) nits *= ({BALANCED_PEAK_NITS:.6f} / pk);\n"
        f"    vec3 x = pow(max(nits, vec3(0.0)) * 0.0001, vec3({M1:.13f}));\n"
        f"    vec3 pq = pow(({C1:.10f} + {C2:.10f} * x) / (1.0 + {C3:.10f} * x), vec3({M2:.10f}));\n"
        "    return vec4(pq, c.a);\n"
        "}\n"
    )
    path.write_text(shader, encoding="ascii", newline="\n")


def write_natural_shader(path: Path, primaries: str = "bt709"):
    """Write a self-contained MAIN-stage GLSL shader for highlight-protected HDR10.

    Running with libplacebo color_primaries=auto:color_trc=auto:disable_linear=true
    keeps libplacebo from altering luminance when input-side HDR10 static metadata is
    attached for hardware encoders, while executing exact BT.1886->BT.2020->PQ on GPU.
    """
    m = PRIMARY_TO_BT2020.get(primaries, M_BT709_TO_BT2020)
    yr, yg, yb = Y_BT2020
    span = 1.0 - NATURAL_KNEE
    shader = (
        "//!HOOK MAIN\n"
        "//!BIND HOOKED\n"
        "vec4 hook() {\n"
        "    vec4 c = HOOKED_texOff(0);\n"
        "    vec3 lin = pow(clamp(c.rgb, 0.0, 1.0), vec3(2.4));\n"
        "    vec3 b20 = vec3(\n"
        f"        dot(lin, vec3({m[0][0]:.7f}, {m[0][1]:.7f}, {m[0][2]:.7f})),\n"
        f"        dot(lin, vec3({m[1][0]:.7f}, {m[1][1]:.7f}, {m[1][2]:.7f})),\n"
        f"        dot(lin, vec3({m[2][0]:.7f}, {m[2][1]:.7f}, {m[2][2]:.7f}))\n"
        "    );\n"
        f"    float y = dot(b20, vec3({yr:.4f}, {yg:.4f}, {yb:.4f}));\n"
        "    if (y <= 1e-9) return vec4(0.0, 0.0, 0.0, c.a);\n"
        f"    float tn = (y <= {NATURAL_KNEE:.6f}) ? ({NATURAL_BASE_NITS:.6f} * y) : "
        f"({NATURAL_BASE_NITS:.6f} * y + {NATURAL_BOOST_NITS:.6f} * pow(min((y - {NATURAL_KNEE:.6f}) / {span:.6f}, 1.0), 2.0));\n"
        "    vec3 nits = b20 * (tn / y);\n"
        "    float pk = max(nits.r, max(nits.g, nits.b));\n"
        f"    if (pk > {NATURAL_PEAK_NITS:.6f}) nits *= ({NATURAL_PEAK_NITS:.6f} / pk);\n"
        f"    vec3 x = pow(max(nits, vec3(0.0)) * 0.0001, vec3({M1:.13f}));\n"
        f"    vec3 pq = pow(({C1:.10f} + {C2:.10f} * x) / (1.0 + {C3:.10f} * x), vec3({M2:.10f}));\n"
        "    return vec4(pq, c.a);\n"
        "}\n"
    )
    path.write_text(shader, encoding="ascii", newline="\n")


def write_vivid_shader(path: Path, matrix: str = "bt709", peak: float = 1000.0):
    """Write an mpv/libplacebo MAIN-stage GLSL shader for legacy high-contrast vivid HDR10."""
    rows = chroma_matrix("bt709" if matrix == "gbr" else matrix)
    ceiling = pq_encode(peak)
    shader = (
        "//!HOOK MAIN\n"
        "//!BIND HOOKED\n"
        "vec4 hook() {\n"
        "    vec4 c = HOOKED_texOff(0);\n"
        "    vec3 m = vec3(\n"
        f"        dot(c.rgb, vec3({rows[0][0]:.10f}, {rows[0][1]:.10f}, {rows[0][2]:.10f})),\n"
        f"        dot(c.rgb, vec3({rows[1][0]:.10f}, {rows[1][1]:.10f}, {rows[1][2]:.10f})),\n"
        f"        dot(c.rgb, vec3({rows[2][0]:.10f}, {rows[2][1]:.10f}, {rows[2][2]:.10f}))\n"
        "    );\n"
        f"    vec3 q = clamp(0.65 * log(1.0 + 5.0 * max(m, vec3(0.0))) / log(6.0), 0.0, {ceiling:.10f});\n"
        "    return vec4(q, c.a);\n"
        "}\n"
    )
    path.write_text(shader, encoding="ascii", newline="\n")


def gpu_sdr_filter_chain(p: dict, width: int, height: int, shader_name: str, style: str,
                         use_hwdec: bool = False, pix_fmt: str = "p010le") -> str:
    """Build a Vulkan/libplacebo GPU SDR->HDR10 filter chain matching CPU formulas."""
    expected = STYLE_SHADER_NAMES.get(style)
    if expected is None:
        raise ConversionError("未知的 HDR 风格设置。")
    if shader_name != expected:
        raise ValueError("Unexpected shader filename")
    prefix = ([] if use_hwdec else [
        f"setparams=range={p['range']}:color_primaries={p['primaries']}:color_trc={p['transfer']}:colorspace={p['matrix']}"
    ])
    return ",".join([
        *prefix,
        (f"libplacebo=w={width}:h={height}:format={pix_fmt}:"
         "colorspace=bt2020nc:color_primaries=auto:color_trc=auto:range=tv:"
         "disable_linear=true:tonemapping=clip:gamut_mode=clip:peak_detect=false:"
         f"dithering=ordered_fixed:custom_shader_path={shader_name}"),
        "setparams=range=limited:color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc",
        "setsar=1",
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
    hdr_style: str = "balanced"
    gpu_filter: str = "auto"
    multipass: bool = False


def clear_encoder_cache() -> None:
    """Clear cached runtime encoder capability probe results."""
    with _ENCODER_CACHE_LOCK:
        _ENCODER_CACHE.clear()


def clear_dv_tools_cache() -> None:
    """Clear cached runtime Dolby Vision filter/BSF probe results."""
    with _DV_TOOLS_CACHE_LOCK:
        _DV_TOOLS_CACHE.clear()


def clear_gpu_filter_cache() -> None:
    """Clear cached runtime GPU libplacebo/Vulkan SDR filter probe results."""
    with _GPU_FILTER_CACHE_LOCK:
        _GPU_FILTER_CACHE.clear()


def _hardware_encoder_args(encoder_key: str, quality: str, multipass: bool = False,
                           qsv_extbrc: bool = False) -> tuple[list[str], dict]:
    if encoder_key == "nvenc":
        mp_args = ["-multipass", "qres"] if multipass else []
        mp_suffix = " (multipass)" if multipass else ""
        if quality == "fine":
            fine_preset = "p7" if multipass else "p6"
            fine_qc = "VBR CQ 18 (multipass)" if multipass else "VBR CQ 18 (1-pass)"
            return (
                ["-c:v", "hevc_nvenc", "-profile:v", "main10", "-pix_fmt", "p010le",
                 "-preset", fine_preset, "-tune", "hq", *mp_args, "-rc", "vbr", "-cq", "18",
                 "-b:v", "0", "-spatial-aq", "1"],
                dict(codec="hevc_nvenc", preset=fine_preset, crf=None, quality_control=fine_qc),
            )
        configs = {
            "fast": (
                ["-c:v", "hevc_nvenc", "-profile:v", "main10", "-pix_fmt", "p010le",
                 "-preset", "p2", "-tune", "hq", *mp_args, "-rc", "vbr", "-cq", "22", "-b:v", "0"],
                dict(codec="hevc_nvenc", preset="p2", crf=None, quality_control="VBR CQ 22" + mp_suffix),
            ),
            "standard": (
                ["-c:v", "hevc_nvenc", "-profile:v", "main10", "-pix_fmt", "p010le",
                 "-preset", "p5", "-tune", "hq", *mp_args, "-rc", "vbr", "-cq", "20", "-b:v", "0"],
                dict(codec="hevc_nvenc", preset="p5", crf=None, quality_control="VBR CQ 20" + mp_suffix),
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
        extbrc_args = ["-extbrc", "1", "-mbbrc", "1", "-adaptive_i", "1", "-adaptive_b", "1"] if qsv_extbrc else []
        extbrc_suffix = " + ExtBRC" if qsv_extbrc else ""
        configs = {
            "fast": (
                ["-c:v", "hevc_qsv", "-profile:v", "main10", "-pix_fmt", "p010le",
                 "-preset", "faster", "-global_quality", "19", *extbrc_args],
                dict(codec="hevc_qsv", preset="faster", crf=None, quality_control="ICQ 19" + extbrc_suffix),
            ),
            "standard": (
                ["-c:v", "hevc_qsv", "-profile:v", "main10", "-pix_fmt", "p010le",
                 "-preset", "medium", "-global_quality", "16", *extbrc_args],
                dict(codec="hevc_qsv", preset="medium", crf=None, quality_control="ICQ 16" + extbrc_suffix),
            ),
            "fine": (
                ["-c:v", "hevc_qsv", "-profile:v", "main10", "-pix_fmt", "p010le",
                 "-preset", "slow", "-global_quality", "14", *extbrc_args],
                dict(codec="hevc_qsv", preset="slow", crf=None, quality_control="ICQ 14" + extbrc_suffix),
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


def detect_gpu_filter(force: bool = False) -> dict:
    """Probe Vulkan/libplacebo custom GLSL shader and Vulkan hardware decode capabilities."""
    ffmpeg_path = find_tool("ffmpeg")
    with _GPU_FILTER_CACHE_LOCK:
        if not force and ffmpeg_path in _GPU_FILTER_CACHE:
            return dict(_GPU_FILTER_CACHE[ffmpeg_path])
    filters = _compiled_filters(ffmpeg_path)
    shader_ok = False
    hwdec_ok = False
    reason = ""
    if "libplacebo" not in filters:
        reason = "当前 FFmpeg 未编译 libplacebo 滤镜，将使用 CPU 浮点滤镜"
    else:
        with tempfile.TemporaryDirectory(prefix="sdr2hdr-gpu-probe-") as tmp:
            tmp_path = Path(tmp)
            write_natural_shader(tmp_path / "natural.hook", "bt709")
            p_probe = dict(primaries="bt709", transfer="bt709", matrix="bt709", range="limited")
            try:
                r = capture([
                    ffmpeg_path, "-hide_banner", "-nostdin", "-y", "-v", "error", "-f", "lavfi",
                    "-i", "color=c=gray:s=64x64:r=25:d=0.04",
                    "-vf", gpu_sdr_filter_chain(p_probe, 64, 64, "natural.hook", "natural", use_hwdec=False, pix_fmt="p010le"),
                    "-f", "null", "-",
                ], timeout=15, cwd=tmp_path)
                if r.returncode == 0:
                    shader_ok = True
                else:
                    reason = "libplacebo GPU 着色器初始化失败，将使用 CPU 浮点滤镜"
            except (subprocess.TimeoutExpired, OSError):
                reason = "libplacebo GPU 着色器自检超时，将使用 CPU 浮点滤镜"

            if shader_ok:
                compiled_enc = _compiled_video_encoders(ffmpeg_path)
                probe_codec = "libx264" if "libx264" in compiled_enc else ("libx265" if "libx265" in compiled_enc else None)
                if probe_codec:
                    probe_in = tmp_path / "vk_probe.mp4"
                    try:
                        mk = capture([
                            ffmpeg_path, "-hide_banner", "-nostdin", "-y", "-v", "error", "-f", "lavfi",
                            "-i", "color=c=gray:s=128x128:r=25:d=0.08",
                            "-c:v", probe_codec, "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                            "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv",
                            str(probe_in),
                        ], timeout=15, cwd=tmp_path)
                        if mk.returncode == 0:
                            r_vk = capture([
                                ffmpeg_path, "-hide_banner", "-nostdin", "-y", "-v", "error",
                                "-init_hw_device", "vulkan=vk:0", "-hwaccel", "vulkan", "-hwaccel_output_format", "vulkan",
                                "-i", str(probe_in),
                                "-vf", gpu_sdr_filter_chain(p_probe, 128, 128, "natural.hook", "natural", use_hwdec=True, pix_fmt="p010le"),
                                "-f", "null", "-",
                            ], timeout=15, cwd=tmp_path)
                            hwdec_ok = r_vk.returncode == 0
                    except (subprocess.TimeoutExpired, OSError):
                        hwdec_ok = False
    result = dict(
        libplacebo_shader=shader_ok,
        vulkan_hwdec=hwdec_ok,
        reason=reason,
    )
    with _GPU_FILTER_CACHE_LOCK:
        _GPU_FILTER_CACHE[ffmpeg_path] = dict(result)
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
    entry = {**spec, "key": key, "available": False, "lossless": False, "extbrc": False, "reason": ""}
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
        if key == "qsv":
            extbrc_args, _ = _hardware_encoder_args("qsv", "standard", qsv_extbrc=True)
            probe_extbrc = Path(tmp) / "probe_extbrc.mp4"
            try:
                ext_result = capture([*base_prefix, *extbrc_args, *hdr_suffix, str(probe_extbrc)], timeout=15)
                if ext_result.returncode == 0:
                    ext_checks, _, _, _ = _hdr_probe_checks(probe_extbrc)
                    entry["extbrc"] = all(ext_checks.values())
            except (ConversionError, json.JSONDecodeError, OSError, subprocess.TimeoutExpired):
                entry["extbrc"] = False
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
                  dv_tools: dict | None = None,
                  gpu_filter_caps: dict | None = None) -> tuple[list[str], dict]:
    if opts.audio_effect not in ("original", "surround"):
        raise ConversionError("未知音效设置。")
    if opts.quality not in ("fast", "standard", "fine", "lossless"):
        raise ConversionError("未知的画面编码档位。")
    if opts.dv_mode not in ("auto", "strip", "reencode"):
        raise ConversionError("未知的杜比视界处理模式。")
    if opts.hdr_style not in STYLE_LUT_NAMES:
        raise ConversionError("未知的 HDR 风格设置。")
    if opts.gpu_filter not in ("auto", "off"):
        raise ConversionError("未知的 GPU 滤镜加速设置。")

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
    qsv_extbrc = False

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
        elif opts.hdr_style == "balanced":
            mastering_str, content_light_str, peak_nits = MASTERING_DISPLAY, BALANCED_CONTENT_LIGHT, int(BALANCED_PEAK_NITS)
        elif opts.hdr_style == "natural":
            mastering_str, content_light_str, peak_nits = MASTERING_DISPLAY, NATURAL_CONTENT_LIGHT, int(NATURAL_PEAK_NITS)
        detected_map = detected_encoders if detected_encoders is not None else detect_encoders()
        encoder_key, encoder_note = resolve_encoder(opts.encoder, opts.quality, w, h, detected=detected_map)
        is_hw = encoder_key in HARDWARE_PRIORITY
        qsv_extbrc = bool(detected_map.get("qsv", {}).get("extbrc"))
        encoder_label = ENCODER_SPECS[encoder_key]["label"]

    use_gpu_filter = False
    use_vulkan_hwdec = False
    if video_operation == "sdr_to_hdr" and is_hw and not lossless_video and opts.gpu_filter == "auto":
        eligible_input = (
            p["matrix"] != "gbr"
            and p["transfer"] in ("bt709", "smpte170m", "bt470bg", "bt2020-10", "bt2020-12")
        )
        if eligible_input:
            gpu_caps = gpu_filter_caps if gpu_filter_caps is not None else detect_gpu_filter()
            if gpu_caps.get("libplacebo_shader"):
                use_gpu_filter = True
                codec_name = p.get("codec_name", "")
                pix_fmt = p.get("pixel_format", "")
                hwdec_codec_ok = (
                    (codec_name == "h264" and pix_fmt in ("yuv420p", "nv12"))
                    or (codec_name in ("hevc", "av1") and pix_fmt in ("yuv420p", "yuv420p10le", "nv12", "p010le"))
                )
                hwdec_color_ok = (
                    p["range"] == "limited"
                    and p["transfer"] in ("bt709", "smpte170m", "bt2020-10", "bt2020-12")
                )
                use_vulkan_hwdec = bool(gpu_caps.get("vulkan_hwdec") and hwdec_codec_ok and hwdec_color_ok)
                pipeline_note = "Vulkan 硬解 + libplacebo GPU 色彩管线" if use_vulkan_hwdec else "libplacebo GPU 色彩管线"
                encoder_note = f"{encoder_note}（{pipeline_note}）" if encoder_note else f"已启用 {pipeline_note}"
    elif video_operation == "dv_reencode" and not lossless_video and opts.gpu_filter == "auto":
        gpu_caps = gpu_filter_caps if gpu_filter_caps is not None else detect_gpu_filter()
        codec_name = p.get("codec_name", "")
        pix_fmt = p.get("pixel_format", "")
        hwdec_codec_ok = codec_name in ("hevc", "av1") and pix_fmt in ("yuv420p10le", "p010le", "yuv420p", "nv12")
        if gpu_caps.get("vulkan_hwdec") and hwdec_codec_ok:
            use_vulkan_hwdec = True
            dv_hw_note = "Vulkan 硬解 + libplacebo 杜比视界还原"
            encoder_note = f"{encoder_note}（{dv_hw_note}）" if encoder_note else f"已启用 {dv_hw_note}"

    filter_threads = min(16, os.cpu_count() or 4) if is_hw else min(8, os.cpu_count() or 4)
    codec_threads = min(8, os.cpu_count() or 4)
    args = [find_tool("ffmpeg"), "-hide_banner", "-loglevel", "warning", "-n",
            "-filter_threads", str(filter_threads), "-threads", str(codec_threads)]
    if use_vulkan_hwdec:
        args += ["-init_hw_device", "vulkan=vk:0", "-hwaccel", "vulkan", "-hwaccel_output_format", "vulkan"]
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
        enc_args, enc_meta = _hardware_encoder_args(
            encoder_key,
            opts.quality,
            multipass=bool(opts.multipass),
            qsv_extbrc=qsv_extbrc,
        )
        video_codec = enc_meta["codec"]
        preset = enc_meta["preset"]
        crf = enc_meta["crf"]
        quality_control = enc_meta["quality_control"]
        if video_operation == "dv_reencode":
            # Direct p010le output in libplacebo is >3x faster at 4K for lossy encodes;
            # lossless mode uses exact yuv420p10le first for bit-exact SHA-256 verification.
            vf = (dv_filter_chain(w, h, pix_fmt="yuv420p10le") + ",format=p010le"
                  if lossless_video else dv_filter_chain(w, h, pix_fmt="p010le"))
        elif use_gpu_filter:
            shader_name = STYLE_SHADER_NAMES[opts.hdr_style]
            vf = gpu_sdr_filter_chain(p, w, h, shader_name=shader_name, style=opts.hdr_style,
                                      use_hwdec=use_vulkan_hwdec, pix_fmt="p010le")
        else:
            # Append p010le after setsar=1 rather than replacing yuv420p10le so zscale's
            # bit-exact 10-bit dithered plane layout is preserved before semi-planar packing.
            lut_name = STYLE_LUT_NAMES[opts.hdr_style]
            vf = filter_chain(p, w, h, lut_name=lut_name, style=opts.hdr_style) + ",format=p010le"
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
        if video_operation == "dv_reencode":
            vf = dv_filter_chain(w, h, pix_fmt="yuv420p10le")
        else:
            lut_name = STYLE_LUT_NAMES[opts.hdr_style]
            vf = filter_chain(p, w, h, lut_name=lut_name, style=opts.hdr_style)
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
                      gpu_filter=use_gpu_filter,
                      vulkan_hwdec=use_vulkan_hwdec,
                      multipass=bool(opts.multipass and encoder_key == "nvenc" and not lossless_video and not stream_copy),
                      qsv_extbrc=bool(encoder_key == "qsv" and qsv_extbrc),
                      encoder_note=encoder_note,
                      quality_control=quality_control,
                      audio=args[args.index("-c:a") + 1],
                      audio_effect=opts.audio_effect,
                      audio_filter=SURROUND_FILTER if opts.audio_effect == "surround" else None,
                      video_operation=video_operation,
                      hdr_style=opts.hdr_style if video_operation == "sdr_to_hdr" else None,
                      dolby_vision=dv,
                      dv_bsf=dv_bsf,
                      mastering_display=mastering_str,
                      content_light=content_light_str,
                      nominal_peak_nits=peak_nits,
                      clipped_stream_copy=bool(stream_copy and (opts.start or opts.duration is not None)),
                      require_limited_range=(p.get("range") == "limited") if video_operation == "dv_strip" else True,
                      require_matrix_metadata=(p.get("matrix") == "bt2020nc") if video_operation == "dv_strip" else True,
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
        if not expected.get("require_matrix_metadata", True):
            optional_metadata["matrix"] = checks.pop("matrix")
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
    elif expected.get("require_nonzero_duration"):
        actual_duration = _known_duration(v.get("duration"), data.get("format", {}).get("duration"))
        checks["duration"] = actual_duration > 0
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
    if plan.get("video_operation") == "dv_reencode":
        ref_filter = dv_filter_chain(plan["width"], plan["height"], pix_fmt="yuv420p10le")
    else:
        style = plan.get("hdr_style") or "vivid"
        lut_name = STYLE_LUT_NAMES.get(style, "tone.cube")
        ref_filter = filter_chain(plan["source"], plan["width"], plan["height"], lut_name=lut_name, style=style)
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


def _next_partial_path(output: Path) -> Path:
    base = output.stem if output.stem.endswith("_已转片段") else output.stem + "_已转片段"
    candidate = output.with_name(base + output.suffix)
    count = 2
    while candidate.exists():
        candidate = output.with_name(f"{base}_{count}{output.suffix}")
        count += 1
    return candidate


def convert(opts: Options, callback: Callable[[dict], None] | None = None,
            cancel: threading.Event | None = None,
            keep_partial: threading.Event | None = None) -> dict:
    callback = callback or (lambda _: None)
    cancel = cancel or threading.Event()
    keep_partial = keep_partial or threading.Event()
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
            style_key = plan.get("hdr_style")
            if plan.get("gpu_filter"):
                if style_key == "balanced":
                    write_balanced_shader(cache_path / STYLE_SHADER_NAMES["balanced"], plan["source"]["primaries"])
                elif style_key == "natural":
                    write_natural_shader(cache_path / STYLE_SHADER_NAMES["natural"], plan["source"]["primaries"])
                else:
                    write_vivid_shader(cache_path / STYLE_SHADER_NAMES["vivid"], plan["source"]["matrix"])
            elif style_key == "balanced":
                write_balanced_lut(cache_path / STYLE_LUT_NAMES["balanced"])
            elif style_key == "natural":
                write_natural_lut(cache_path / STYLE_LUT_NAMES["natural"])
            else:
                write_curve(cache_path / STYLE_LUT_NAMES["vivid"])
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
            accel_prefix = "GPU 色彩管线 + " if plan.get("gpu_filter") else ""
            encoding_message = (
                f"正在使用 {accel_prefix}{plan['encoder_label']} 转换画面并处理音效…" if opts.audio_effect == "surround"
                else f"正在使用 {accel_prefix}{plan['encoder_label']} 转换画面并编码 HDR…"
            )
        callback(dict(stage="encoding", percent=0, plan=plan, message=encoding_message))
        try:
            keep_partial_triggered = False
            with log_path.open("wb") as log:
                process = subprocess.Popen(command, cwd=cache, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                           stderr=log, text=True, encoding="utf-8", errors="replace",
                                           creationflags=CREATE_NO_WINDOW)
                progress = {}
                last_percent = 0
                last_seconds = 0.0
                def stop_when_cancelled():
                    nonlocal keep_partial_triggered
                    while process.poll() is None:
                        if cancel.is_set():
                            if process.poll() is None:
                                try:
                                    process.terminate()
                                except OSError:
                                    pass
                            return
                        if keep_partial.is_set() and not keep_partial_triggered:
                            keep_partial_triggered = True
                            callback(dict(stage="stopping", percent=last_percent,
                                          elapsed=round(max(0.0, time.monotonic() - started), 2),
                                          message="正在正常封包并保存已转换的 MP4 片段（请勿强行关闭）…"))
                            try:
                                if process.stdin:
                                    process.stdin.write("q\n")
                                    process.stdin.flush()
                                    process.stdin.close()
                            except (OSError, ValueError):
                                pass
                        time.sleep(.1)
                watcher = threading.Thread(target=stop_when_cancelled, daemon=True)
                watcher.start()
                for line in process.stdout:
                    key, sep, value = line.strip().partition("=")
                    if sep:
                        progress[key] = value
                    if key == "progress":
                        seconds = progress_seconds(progress, last_seconds)
                        last_seconds = seconds
                        last_percent = max(last_percent, min(99.0, seconds / total * 100.0) if total else 0.0)
                        speed = progress.get("speed", "")
                        if speed.strip().lower() in ("n/a", "nan", "inf", "-inf"):
                            speed = ""
                        frame = progress.get("frame", "")
                        if _finite_number(frame) is None:
                            frame = ""
                        elapsed = max(0.0, time.monotonic() - started)
                        eta_seconds = estimate_eta_seconds(seconds, total, elapsed, speed)
                        if not keep_partial_triggered:
                            callback(dict(stage="encoding", percent=last_percent,
                                          seconds=seconds, total=total, speed=speed,
                                          elapsed=round(elapsed, 2),
                                          eta_seconds=round(eta_seconds, 2) if eta_seconds is not None else None,
                                          frame=frame, message=encoding_message))
                returncode = process.wait()
                watcher.join(timeout=1)
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-20000:]
            if cancel.is_set():
                raise Cancelled("转换已取消，原片保留。")
            is_partial = False
            final_output = output
            if keep_partial_triggered:
                if not temporary.is_file() or temporary.stat().st_size == 0:
                    raise Cancelled("已停止转换，当前尚无可保存的完整视频片段，原片保留。")
                callback(dict(stage="verifying", percent=99,
                              elapsed=round(max(0.0, time.monotonic() - started), 2),
                              message="正在检查已转片段的 HDR、色深和编码标记…"))
                partial_plan = {**plan, "expected_video_duration": None, "require_nonzero_duration": True}
                try:
                    validation = validate_export(temporary, partial_plan)
                except ConversionError:
                    raise Cancelled("已停止转换，当前尚无可保存的完整视频片段，原片保留。") from None
                is_partial = True
                final_output = _next_partial_path(output)
            else:
                if returncode:
                    raise ConversionError("转换失败：\n" + tail[-5000:])
                callback(dict(stage="verifying", percent=99,
                              elapsed=round(max(0.0, time.monotonic() - started), 2),
                              message="正在检查 HDR、色深和编码标记…"))
                validation = validate_export(temporary, plan)
                if plan["lossless_video"]:
                    validation["lossless_pixels"] = verify_lossless(opts, plan, temporary, cache_path, cancel, callback)
            if cancel.is_set():
                raise Cancelled("转换已取消，原片保留。")
            publish_without_overwrite(temporary, final_output)
            validation["format"]["filename"] = str(final_output)
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
                dec_prefix = "Vulkan hwdec + " if plan.get("vulkan_hwdec") else ""
                algorithm_desc = DV_REENCODE_ALGORITHM_ID
                method_desc = f"{dec_prefix}libplacebo Dolby Vision reshaping/color conversion + 10-bit HEVC ({plan['video_codec']})"
                video_enc_desc = f"{plan['video_codec']} true lossless" if plan["lossless_video"] else f"{plan['video_codec']} ({plan['quality_control']})"
                mastering_desc = f"BT.2020 / D65 / {peak_nits} nits synthetic HDR10 target derived from source metadata"
                light_desc = f"{peak_nits}/1000 nits declared HDR10 static bounds; not per-frame measured MaxFALL"
                fidelity_desc = "Dolby Vision metadata is applied and converted into static 10-bit PQ/BT.2020 HDR10 via libplacebo. Online platform transcoding is outside this tool."
            elif plan.get("hdr_style") == "balanced":
                algorithm_desc = BALANCED_ALGORITHM_ID
                if plan.get("gpu_filter"):
                    dec_prefix = "Vulkan hwdec + " if plan.get("vulkan_hwdec") else ""
                    method_desc = f"{dec_prefix}libplacebo GPU GLSL MAIN shader (BT.1886 BT.709->BT.2020 balanced 750 nits -> PQ) + 10-bit HEVC ({plan['video_codec']})"
                else:
                    method_desc = f"BT.1886 linear BT.709->BT.2020 balanced luminance-preserving 49^3 tetrahedral 3D LUT + spline36 + 10-bit HEVC ({plan['video_codec']})"
                video_enc_desc = f"{plan['video_codec']} true lossless" if plan["lossless_video"] else f"{plan['video_codec']} ({plan['quality_control']})"
                mastering_desc = "BT.2020 / D65 / 1000 nits mastering container; curve peak capped at 750 nits"
                light_desc = "750/260 nits declared MaxCLL/MaxFALL matching balanced curve ceiling (750 nits) and diffuse reference (~255 nits)"
                fidelity_desc = "Colorimetric BT.709-to-BT.2020 mapping keeps deep shadows anchored while lifting midtones to natural cinema brightness and expanding specular highlights smoothly to 750 nits with wide skin headroom. Lost SDR detail cannot be synthesized. Online platform transcoding is outside this tool."
            elif plan.get("hdr_style") == "natural":
                algorithm_desc = NATURAL_ALGORITHM_ID
                if plan.get("gpu_filter"):
                    dec_prefix = "Vulkan hwdec + " if plan.get("vulkan_hwdec") else ""
                    method_desc = f"{dec_prefix}libplacebo GPU GLSL MAIN shader (BT.1886 BT.709->BT.2020 luminance-preserving 450 nits -> PQ) + 10-bit HEVC ({plan['video_codec']})"
                else:
                    method_desc = f"BT.1886 linear BT.709->BT.2020 luminance-preserving 49^3 tetrahedral 3D LUT + spline36 + 10-bit HEVC ({plan['video_codec']})"
                video_enc_desc = f"{plan['video_codec']} true lossless" if plan["lossless_video"] else f"{plan['video_codec']} ({plan['quality_control']})"
                mastering_desc = "BT.2020 / D65 / 1000 nits mastering container; curve peak capped at 450 nits"
                light_desc = "450/200 nits declared MaxCLL/MaxFALL matching highlight-protected curve ceiling (450 nits) and diffuse reference (~190 nits)"
                fidelity_desc = "Colorimetric BT.709-to-BT.2020 mapping preserves chromaticity and diffuse skin tones while smoothly lifting specular highlights above knee to 450 nits. Lost SDR detail cannot be synthesized. Online platform transcoding is outside this tool."
            else:
                algorithm_desc = ALGORITHM_ID
                if plan.get("gpu_filter"):
                    dec_prefix = "Vulkan hwdec + " if plan.get("vulkan_hwdec") else ""
                    method_desc = f"{dec_prefix}libplacebo GPU GLSL MAIN shader (UV x0.56 + log curve) + 10-bit HEVC ({plan['video_codec']})"
                else:
                    method_desc = f"float RGB matrix (UV x0.56 equivalent) + 65536-point log curve + 10-bit HEVC ({plan['video_codec']})"
                video_enc_desc = f"{plan['video_codec']} true lossless" if plan["lossless_video"] else f"{plan['video_codec']} ({plan['quality_control']})"
                mastering_desc = "BT.2020 / D65 / 1000 nits; synthetic encoding target, not a measured display"
                light_desc = "1000-nit conservative MaxCLL/MaxFALL upper bounds, enforced by RGB PQ ceiling; not measured MaxFALL"
                fidelity_desc = "Fixed tone and color adjustment changes the SDR presentation intentionally. Resampling and 10-bit quantization are part of the conversion. Extreme over-range samples are capped at 1000 nits. Online platform transcoding is outside this tool."
            result = dict(tool_version=VERSION, algorithm=algorithm_desc, source=str(source), output=str(final_output),
                          options=dataclasses.asdict(opts), plan=plan, validation=validation,
                          elapsed_seconds=round(time.monotonic() - started, 2),
                          partial=is_partial,
                          stopped_early=is_partial,
                          encoder=plan["encoder"],
                          video_codec=plan["video_codec"],
                          hardware_accelerated=plan["hardware_accelerated"],
                          gpu_filter=plan.get("gpu_filter", False),
                          vulkan_hwdec=plan.get("vulkan_hwdec", False),
                          multipass=plan.get("multipass", False),
                          hdr_style=plan.get("hdr_style"),
                          dolby_vision=plan.get("dolby_vision"),
                          method=method_desc,
                          video_encoding=video_enc_desc,
                          audio_effect_description=("Stereo spatial enhancement through channel widening, short cross delay and peak limiting. AAC audio is re-encoded." if opts.audio_effect == "surround" else "Original audio retained where AAC stream copy is possible."),
                          nominal_mastering_target=mastering_desc,
                          declared_light_bounds=light_desc,
                          fidelity_limit=fidelity_desc)
            report = final_output.with_suffix(".hdr.json")
            try:
                with report.open("x", encoding="utf-8") as f:
                    json.dump(result, f, ensure_ascii=False, indent=2)
            except OSError:
                pass  # A valid finished video must not be reported as failed over an optional log.
            done_msg = ("已停止并保留已转换的 HDR 视频片段（已通过文件检查）。"
                        if is_partial else "HDR 视频已导出并通过文件检查。")
            callback(dict(stage="done", percent=100, partial=is_partial,
                          elapsed=result["elapsed_seconds"],
                          message=done_msg, result=result))
            return result
        finally:
            if process and process.poll() is None:
                process.terminate()
                process.wait(timeout=15)
            if process and process.stdin:
                try:
                    process.stdin.close()
                except OSError:
                    pass
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
    parser.add_argument("--style", dest="hdr_style", choices=["balanced", "natural", "vivid"], default="balanced",
                        help="SDR 转 HDR 风格：balanced 影院均衡高光（推荐电影·750 nit），natural 柔和暗室保护（450 nit），vivid 高对比风格（原版·动漫）")
    parser.add_argument("--gpu-filter", choices=["auto", "off"], default="auto",
                        help="GPU 色彩滤镜加速：auto 自动启用 Vulkan/libplacebo GPU 着色器（不支持时回退 CPU），off 强制 CPU 滤镜")
    parser.add_argument("--multipass", action="store_true",
                        help="为 NVIDIA NVENC 有损档启用双遍分析（画质略优但耗时增加）")
    parser.add_argument("--encoder", choices=["auto", "cpu", "nvenc", "qsv", "amf"], default="auto")
    parser.add_argument("--dv-mode", choices=["auto", "strip", "reencode"], default="auto",
                        help="杜比视界模式：auto 自动选择（8.1/7 无损直剥，5/8.4 重编码），或手动指定 strip/reencode")
    parser.add_argument("--list-encoders", action="store_true", help="检测并列出当前环境可用的编码器")
    parser.add_argument("--list-dv-tools", action="store_true", help="检测并列出杜比视界转换所需 FFmpeg 组件状态")
    parser.add_argument("--list-gpu-filter", action="store_true", help="检测并列出 Vulkan/libplacebo GPU 色彩管线状态")
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
        if args.list_gpu_filter:
            print(json.dumps(detect_gpu_filter(), ensure_ascii=False, indent=2), flush=True)
            return
        if not args.source or not args.output:
            parser.error("请提供输入视频和输出路径（或使用 --list-encoders / --list-dv-tools / --list-gpu-filter 查看环境状态）。")
        option_fields = {f.name for f in dataclasses.fields(Options)}
        opts = Options(**{k: v for k, v in vars(args).items() if k in option_fields})
        convert(opts, lambda p: print(json.dumps(p, ensure_ascii=False), flush=True))
    except ConversionError as e:
        print(str(e), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    cli()

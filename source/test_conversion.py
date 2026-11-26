"""Integration checks: real FFmpeg pixels compared with analytical formulas.

Run: python -m unittest discover -s source -p test_conversion.py -v
Requires FFmpeg with zscale/lut1d/lut3d/libx265. No third-party Python packages needed.
"""
import json
import math
from pathlib import Path
import random
import struct
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

import hdr_core as core


def analytical_reference(rgb):
    r, g, b = rgb
    y = .2126 * r + .7152 * g + .0722 * b
    u = (b - y) / 1.8556 * .56
    v = (r - y) / 1.5748 * .56
    before_log = (y + 1.5748 * v, y - .1873 * u - .4681 * v, y + 1.8556 * u)
    ceiling = .751827096247041
    return tuple(min(ceiling, max(0, .65 * math.log(1 + 5 * max(x, 0)) / math.log(6))) for x in before_log)


class ConversionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hdr-check-")
        self.dir = Path(self.temp.name) / "中文 空格 [测试]"
        self.dir.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_float_filter_matches_reference_formula(self):
        rng = random.Random(417)
        samples = [(0., 0., 0.), (1., 1., 1.), (1., 0., 0.), (0., 1., 0.), (0., 0., 1.),
                   (.5, .5, .5), (1.2, 1.2, 1.2), (-.05, -.05, -.05)]
        samples += [tuple(rng.uniform(0, 1) for _ in range(3)) for _ in range(256 - len(samples))]
        raw = b"".join(struct.pack("<f", rgb[channel]) for channel in (1, 2, 0) for rgb in samples)
        p = dict(primaries="bt709", transfer="bt709", matrix="bt709", range="full")
        filters = core.filter_chain(p, 16, 16).split(",")[2:4]
        core.write_curve(self.dir / "tone.cube")
        command = [core.find_tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-f", "rawvideo",
                   "-pix_fmt", "gbrpf32le", "-s", "16x16", "-r", "1", "-i", "pipe:0",
                   "-vf", ",".join(filters), "-frames:v", "1", "-pix_fmt", "gbrpf32le", "-f", "rawvideo", "pipe:1"]
        r = subprocess.run(command, input=raw, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           cwd=self.dir, creationflags=core.CREATE_NO_WINDOW)
        self.assertEqual(r.returncode, 0, r.stderr)
        floats = struct.unpack("<768f", r.stdout)
        actual = [(floats[512+i], floats[i], floats[256+i]) for i in range(256)]
        error = max(abs(a-b) for rgb, out in zip(samples, actual) for a, b in zip(analytical_reference(rgb), out))
        self.assertLess(error, 2e-6)
        self.assertAlmostEqual(actual[1][0], .65, places=6)
        print(f"\nFFmpeg float filter vs analytical reference: max PQ error={error:.9g}")

    def make_source(self, width=128, height=72, name="输入 视频.mkv"):
        source = self.dir / name
        command = [core.find_tool("ffmpeg"), "-v", "error", "-f", "lavfi", "-i", f"testsrc2=s={width}x{height}:r=25:d=1.2",
                   "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1.2", "-c:v", "ffv1",
                   "-pix_fmt", "yuv444p10le", "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
                   "-color_range", "tv", "-c:a", "pcm_s16le", str(source)]
        result = core.capture(command)
        self.assertEqual(result.returncode, 0, result.stderr)
        return source

    def test_real_file_hdr_metadata_audio_and_repeated_hdr_guard(self):
        source = self.make_source()
        output = self.dir / "输出 HDR.mp4"
        result = core.convert(core.Options(str(source), str(output), quality="fast", audio_effect="surround"))
        self.assertTrue(all(result["validation"]["checks"].values()))
        self.assertEqual(result["validation"]["stream"]["avg_frame_rate"], "25/1")
        report = json.loads(output.with_suffix(".hdr.json").read_text(encoding="utf-8"))
        self.assertEqual(report["validation"]["format"]["filename"], str(output))
        original_bytes = output.read_bytes()
        with self.assertRaises(core.ConversionError):
            core.convert(core.Options(str(source), str(output)))
        self.assertEqual(output.read_bytes(), original_bytes)
        with self.assertRaisesRegex(core.ConversionError, "已经是 HDR"):
            core.convert(core.Options(str(output), str(self.dir / "重复增强.mp4")))

    def test_cancel_keeps_source_and_removes_partial_video(self):
        source = self.make_source()
        original = source.read_bytes()
        output = self.dir / "取消.mp4"
        cancel = threading.Event()
        def progress(p):
            if p["stage"] == "encoding":
                cancel.set()
        with self.assertRaises(core.Cancelled):
            core.convert(core.Options(str(source), str(output)), progress, cancel)
        self.assertFalse(output.exists())
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(list(self.dir.glob("*.part.mp4")), [])

    def test_source_cannot_be_output(self):
        source = self.dir / "original.mp4"
        source.write_bytes(b"protected source")
        with self.assertRaises(core.ConversionError):
            core.convert(core.Options(str(source), str(source)))
        self.assertEqual(source.read_bytes(), b"protected source")

    def test_virtual_surround_impulse_keeps_timing_and_sample_count(self):
        count, impulse_at = 48000, 9600
        data = [0.0] * (2 * count)
        data[2 * impulse_at] = .1
        raw = struct.pack(f"<{len(data)}f", *data)
        command = [core.find_tool("ffmpeg"), "-v", "error", "-f", "f32le", "-ar", "48000", "-ac", "2",
                   "-i", "pipe:0", "-af", core.SURROUND_FILTER, "-c:a", "pcm_f32le", "-f", "f32le", "pipe:1"]
        r = subprocess.run(command, input=raw, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=core.CREATE_NO_WINDOW)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(r.stdout), len(raw), "Audio processing must preserve sample count")
        values = struct.unpack(f"<{len(data)}f", r.stdout)
        left, right = values[::2], values[1::2]
        self.assertEqual(max(range(count), key=lambda i: abs(left[i])), impulse_at)
        self.assertGreater(abs(right[impulse_at]), .001)
        self.assertGreater(abs(right[impulse_at + 576]), .001)
        self.assertTrue(all(math.isfinite(x) and abs(x) <= .891 for x in values))

    def test_audio_only_hdr_pass_preserves_video_packets(self):
        source = self.make_source()
        first = self.dir / "原声 HDR.mp4"
        second = self.dir / "环绕 HDR.mp4"
        core.convert(core.Options(str(source), str(first), quality="fast"))
        result = core.convert(core.Options(str(first), str(second), audio_effect="surround", copy_hdr_video=True))
        self.assertEqual(result["plan"]["video_operation"], "copy_hdr")
        self.assertTrue(result["validation"]["checks"]["stereo_aac_48k"])
        def packet_hash(path, kind):
            r = core.capture([core.find_tool("ffmpeg"), "-v", "error", "-i", str(path), "-map", f"0:{kind}:0",
                              "-c", "copy", "-f", "hash", "-hash", "sha256", "-"])
            self.assertEqual(r.returncode, 0, r.stderr)
            return r.stdout.strip()
        self.assertEqual(packet_hash(first, "v"), packet_hash(second, "v"))
        self.assertNotEqual(packet_hash(first, "a"), packet_hash(second, "a"))

    def unavailable_progress(self):
        # Keep a real FFmpeg encoder running, but prepend the unavailable status
        # values it may emit before its first output packet. This reliably tests
        # the reported failure independently of machine/encoder startup speed.
        real_popen = subprocess.Popen
        class StatusStream:
            def __init__(self, actual):
                self.actual = actual
            def __iter__(self):
                yield from ("frame=N/A\n", "out_time_us=N/A\n", "out_time=N/A\n",
                            "speed=N/A\n", "progress=continue\n",
                            "out_time_us=N/A\n", "out_time=00:00:00.040000\n", "progress=continue\n")
                yield from self.actual
            def close(self):
                self.actual.close()
        def start(command, *args, **kwargs):
            process = real_popen(command, *args, **kwargs)
            if "-progress" in command:
                process.stdout = StatusStream(process.stdout)
            return process
        return patch.object(core.subprocess, "Popen", side_effect=start)

    def test_unavailable_progress_does_not_abort_real_conversion(self):
        source = self.make_source()
        output = self.dir / "缺失进度 HDR.mp4"
        events = []
        with self.unavailable_progress():
            result = core.convert(core.Options(str(source), str(output), quality="fast"), events.append)
        self.assertTrue(all(result["validation"]["checks"].values()))
        updates = [e for e in events if e.get("stage") == "encoding" and "seconds" in e]
        self.assertEqual(updates[0]["seconds"], 0)
        self.assertAlmostEqual(updates[1]["seconds"], .04)
        self.assertEqual(updates[0]["speed"], "")
        self.assertEqual(updates[0]["frame"], "")
        self.assertEqual([e["seconds"] for e in updates], sorted(e["seconds"] for e in updates))

    def test_real_encoder_failure_is_reported_after_unavailable_progress(self):
        source = self.make_source()
        output = self.dir / "编码失败.mp4"
        real_build = core.build_command
        def broken_encoder(*args, **kwargs):
            command, plan = real_build(*args, **kwargs)
            command[command.index("libx265")] = "intentionally_missing_encoder"
            return command, plan
        with self.unavailable_progress(), patch.object(core, "build_command", side_effect=broken_encoder):
            with self.assertRaisesRegex(core.ConversionError, "intentionally_missing_encoder"):
                core.convert(core.Options(str(source), str(output), quality="fast", encoder="cpu"))
        self.assertFalse(output.exists())
        self.assertTrue(source.exists())

    def test_unavailable_media_metadata_uses_safe_fallbacks(self):
        source = self.make_source()
        aac_source = self.dir / "AAC 原片.mkv"
        r = core.capture([core.find_tool("ffmpeg"), "-v", "error", "-i", str(source),
                          "-c:v", "copy", "-c:a", "aac", "-ar", "48000", "-ac", "2", str(aac_source)])
        self.assertEqual(r.returncode, 0, r.stderr)
        real_probe = core.probe
        def incomplete_metadata(path):
            data = real_probe(path)
            if Path(path).resolve() == aac_source.resolve():
                v = core.video_stream(data)
                v.update(duration="N/A", avg_frame_rate="N/A", sample_aspect_ratio="N/A",
                         side_data_list=[{"rotation": "N/A"}])
                v.get("tags", {}).pop("DURATION", None)
                data["format"]["duration"] = "N/A"
                audio = next(s for s in data["streams"] if s.get("codec_type") == "audio")
                audio.update(sample_rate="N/A", duration="N/A")
            return data
        output = self.dir / "未知时长 HDR.mp4"
        events = []
        with patch.object(core, "probe", side_effect=incomplete_metadata):
            result = core.convert(core.Options(str(aac_source), str(output), quality="fast"), events.append)
        self.assertTrue(all(result["validation"]["checks"].values()))
        self.assertEqual(result["plan"]["audio"], "aac")
        self.assertEqual(result["plan"]["source"]["duration"], 0)
        self.assertEqual(result["plan"]["source"]["fps"], 25)
        self.assertTrue(any(e.get("total") == 0 for e in events if "total" in e))

    def test_middle_mkv_preview_uses_video_duration_and_clips_copied_tracks(self):
        subtitle = self.dir / "crossing.srt"
        subtitle.write_text("1\n00:00:01,000 --> 00:00:05,000\nfirst\n\n"
                            "2\n00:00:05,300 --> 00:00:08,200\nsecond\n\n"
                            "3\n00:00:09,800 --> 00:00:13,000\nlast\n", encoding="utf-8")
        source = self.dir / "long-gop.mkv"
        result = core.capture([core.find_tool("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                               "testsrc2=size=384x160:rate=24:duration=12", "-f", "lavfi", "-i",
                               "aevalsrc=0.25*sin(2*PI*(300*t+20*t*t)):s=48000:d=12", "-i", str(subtitle),
                               "-map", "0:v", "-map", "1:a", "-map", "2:s", "-c:v", "libx265", "-preset", "ultrafast",
                               "-x265-params", "keyint=96:min-keyint=96:scenecut=0:pools=2:log-level=error",
                               "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
                               "-ac", "6", "-c:a", "eac3", "-c:s", "srt", str(source)])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertAlmostEqual(core.source_properties(core.probe(source))["duration"], 12, delta=.05)

        def packets(file, stream):
            result = core.capture([core.find_tool("ffprobe"), "-v", "error", "-select_streams", stream,
                                   "-show_packets", "-show_data_hash", "sha256", "-show_entries",
                                   "packet=pts_time,duration_time,data_hash", "-of", "json", str(file)])
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)["packets"]

        source_hashes = [packet["data_hash"] for packet in packets(source, "a:0")]
        for start, seconds in ((3, 3), (11, 1)):
            with self.subTest(start=start):
                output = self.dir / f"preview-{start}.mkv"
                report = core.convert(core.Options(str(source), str(output), encoder="cpu", quality="fast",
                                                   start=start, duration=3))
                valid = report["validation"]
                self.assertAlmostEqual(valid["video_duration_seconds"], seconds, delta=.05)
                self.assertLess(float(valid["stream"]["start_time"]), .05)
                self.assertAlmostEqual(float(valid["format"]["duration"]), seconds, delta=.06)
                self.assertEqual(len(packets(output, "v:0")), seconds * 24)
                audio = packets(output, "a:0")
                self.assertEqual(core.probe(output)["streams"][1]["channels"], 6)
                kept = [packet["data_hash"] for packet in audio]
                position = source_hashes.index(kept[0])
                self.assertEqual(kept, source_hashes[position:position+len(kept)])
                self.assertAlmostEqual(position * .032, start, delta=.04)
                cues = packets(output, "s:0")
                self.assertEqual(len(cues), 2 if start == 3 else 1)
                self.assertLess(float(cues[0]["pts_time"]), .05)
                self.assertAlmostEqual(float(cues[-1]["pts_time"]) + float(cues[-1]["duration_time"]),
                                       seconds, delta=.05)

        # A three-second subtitle must not mask a genuinely short video track.
        subtitle.write_text("1\n00:00:00,000 --> 00:00:03,000\npadding\n", encoding="utf-8")
        short = self.dir / "truncated.mkv"
        result = core.capture([core.find_tool("ffmpeg"), "-v", "error", "-i", str(output),
                               "-i", str(subtitle), "-map", "0:v:0", "-map", "0:a:0", "-map", "1:s",
                               "-c", "copy", str(short)])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertAlmostEqual(float(core.probe(short)["format"]["duration"]), 3, delta=.06)
        with self.assertRaisesRegex(core.ConversionError, "视频轨时长.*预期 3.000 秒"):
            core.validate_export(short, {**report["plan"], "expected_video_duration": 3})

    def test_video_duration_tags_and_timeline_origin(self):
        stream = {"start_time": "2.25", "tags": {"DURATION": "00:00:05.250000000"}}
        info = {"format": {"start_time": "1.25", "duration": "20"}}
        self.assertEqual(core._video_span(stream), 3)
        self.assertEqual(core._video_timeline_end(info, stream), 4)
        stream["tags"] = {"DURATION-eng": "00:00:03.000000000", "_STATISTICS_TAGS-eng": "BPS DURATION"}
        self.assertEqual(core._video_span(stream), 3)
        stream["tags"] = {"DURATION": "N/A"}
        self.assertEqual(core._video_timeline_end(info, stream), 20)

    def test_lossless_roundtrip_matches_every_preencode_frame(self):
        source = self.make_source()
        output = self.dir / "画面无损 HDR.mp4"
        result = core.convert(core.Options(str(source), str(output), quality="lossless"))
        self.assertTrue(result["plan"]["lossless_video"])
        self.assertIsNone(result["plan"]["crf"])
        verification = result["validation"]["lossless_pixels"]
        self.assertTrue(verification["all_frames_equal"])
        self.assertEqual(verification["frames_compared"], 30)
        self.assertEqual(verification["pixel_format"], "yuv420p10le")

    def test_lossless_verification_rejects_actually_lossy_video(self):
        source = self.make_source()
        output = self.dir / "不能冒充无损.mp4"
        real_build = core.build_command
        def lossy_encoding(*args, **kwargs):
            command, plan = real_build(*args, **kwargs)
            at = command.index("-x265-params") + 1
            command[at] = command[at].replace(":lossless=1", "")
            command[-1:-1] = ["-crf", "28"]
            return command, plan
        with patch.object(core, "build_command", side_effect=lossy_encoding):
            with self.assertRaisesRegex(core.ConversionError, "无损校验未通过"):
                core.convert(core.Options(str(source), str(output), quality="lossless", encoder="cpu"))
        self.assertFalse(output.exists())
        self.assertEqual(list(self.dir.glob("*.part.mp4")), [])

    def test_cancel_during_lossless_verification_keeps_source(self):
        source = self.make_source()
        original = source.read_bytes()
        output = self.dir / "取消无损校验.mp4"
        cancel = threading.Event()
        def progress(event):
            if event.get("stage") == "verifying" and event.get("percent") == 99.1:
                cancel.set()
        with self.assertRaises(core.Cancelled):
            core.convert(core.Options(str(source), str(output), quality="lossless"), progress, cancel)
        self.assertEqual(source.read_bytes(), original)
        self.assertFalse(output.exists())
        self.assertEqual(list(self.dir.glob("*.part.mp4")), [])

    def test_encoder_selection_and_hardware_command_construction(self):
        fake_info = {
            "streams": [
                {
                    "index": 1,
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1920,
                    "height": 1080,
                    "pix_fmt": "yuv420p",
                    "color_space": "bt709",
                    "color_primaries": "bt709",
                    "color_transfer": "bt709",
                    "color_range": "tv",
                    "avg_frame_rate": "30/1",
                    "duration": "10.0",
                },
                {
                    "index": 0,
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "channels": 2,
                    "sample_rate": "48000",
                    "duration": "10.0",
                },
            ],
            "format": {"duration": "10.0"},
        }

        def fake_detected(nvenc=False, nvenc_lossless=False, qsv=False, qsv_extbrc=False, amf=False, cpu=True):
            return {
                "cpu": {**core.ENCODER_SPECS["cpu"], "key": "cpu", "available": cpu, "lossless": cpu, "extbrc": False, "reason": ""},
                "nvenc": {**core.ENCODER_SPECS["nvenc"], "key": "nvenc", "available": nvenc, "lossless": nvenc_lossless, "extbrc": False, "reason": "" if nvenc else "no nvenc"},
                "qsv": {**core.ENCODER_SPECS["qsv"], "key": "qsv", "available": qsv, "lossless": False, "extbrc": qsv_extbrc, "reason": "" if qsv else "no qsv"},
                "amf": {**core.ENCODER_SPECS["amf"], "key": "amf", "available": amf, "lossless": False, "extbrc": False, "reason": "" if amf else "no amf"},
            }

        dest = self.dir / "out.mp4"
        all_hw = fake_detected(nvenc=True, nvenc_lossless=True, qsv=True, amf=True)
        qsv_amf = fake_detected(nvenc=False, qsv=True, amf=True)
        amf_only = fake_detected(amf=True)

        # Priority order: auto prefers nvenc > qsv > amf > cpu.
        cmd_nv, plan_nv = core.build_command(core.Options("in.mp4", str(dest), encoder="auto"), fake_info, dest, detected_encoders=all_hw)
        self.assertEqual(plan_nv["encoder"], "nvenc")
        self.assertEqual(plan_nv["video_codec"], "hevc_nvenc")
        self.assertTrue(plan_nv["hardware_accelerated"])
        cmd_qsv_auto, plan_qsv_auto = core.build_command(core.Options("in.mp4", str(dest), encoder="auto"), fake_info, dest, detected_encoders=qsv_amf)
        self.assertEqual(plan_qsv_auto["encoder"], "qsv")
        self.assertEqual(plan_qsv_auto["video_codec"], "hevc_qsv")
        _, plan_amf_auto = core.build_command(core.Options("in.mp4", str(dest), encoder="auto"), fake_info, dest, detected_encoders=amf_only)
        self.assertEqual(plan_amf_auto["encoder"], "amf")
        self.assertEqual(plan_amf_auto["video_codec"], "hevc_amf")

        # Verify hardware encoding and GPU-first color processing independently.
        no_gpu_filter = dict(libplacebo_shader=False, vulkan_hwdec=False, reason="off")
        yes_gpu_filter = dict(libplacebo_shader=True, vulkan_hwdec=True, reason="")
        hevc_info = {**fake_info, "streams": [{**fake_info["streams"][0], "codec_name": "hevc"}, fake_info["streams"][1]]}
        for enc_key, expected_flags in [
            ("nvenc", ["-c:v", "hevc_nvenc", "-rc", "vbr", "-cq"]),
            ("qsv", ["-c:v", "hevc_qsv", "-global_quality"]),
            ("amf", ["-c:v", "hevc_amf", "-bitdepth", "10", "-rc", "cqp"]),
        ]:
            # H.264 takes the GPU color path with a bounded software decoder.
            cmd, plan = core.build_command(core.Options("in.mp4", str(dest), encoder=enc_key, quality="standard"),
                                           fake_info, dest, detected_encoders=all_hw, gpu_filter_caps=yes_gpu_filter)
            self.assertEqual(plan["encoder"], enc_key)
            self.assertTrue(plan["gpu_filter"])
            self.assertFalse(plan["fast_hw_filter"])
            self.assertFalse(plan["vulkan_hwdec"])
            input_pos = cmd.index("-i")
            md_pos = cmd.index("-mastering_display:1")
            cl_pos = cmd.index("-content_light:1")
            self.assertLess(md_pos, input_pos)
            self.assertLess(cl_pos, input_pos)
            vf = cmd[cmd.index("-vf") + 1]
            self.assertIn("custom_shader_path=balanced.hook", vf)
            self.assertLessEqual(int(cmd[cmd.index("-threads") + 1]), 4)
            self.assertNotIn("-hwaccel", cmd)
            for flag in expected_flags:
                self.assertIn(flag, cmd)

            # Opt-in gpu_filter="on" selects libplacebo GPU shader; Vulkan hwdec enabled for HEVC/AV1 only (not H.264).
            cmd_g_h264, plan_g_h264 = core.build_command(
                core.Options("in.mp4", str(dest), encoder=enc_key, quality="standard", gpu_filter="on"),
                fake_info, dest, detected_encoders=all_hw, gpu_filter_caps=yes_gpu_filter,
            )
            self.assertTrue(plan_g_h264["gpu_filter"])
            self.assertFalse(plan_g_h264["vulkan_hwdec"])
            self.assertNotIn("-hwaccel", cmd_g_h264)
            self.assertIn("custom_shader_path=balanced.hook", cmd_g_h264[cmd_g_h264.index("-vf") + 1])

            cmd_g, plan_g = core.build_command(
                core.Options("in.mp4", str(dest), encoder=enc_key, quality="standard"),
                hevc_info, dest, detected_encoders=all_hw, gpu_filter_caps=yes_gpu_filter,
            )
            self.assertTrue(plan_g["gpu_filter"])
            self.assertTrue(plan_g["vulkan_hwdec"])
            self.assertIn("-hwaccel", cmd_g)
            self.assertIn("vulkan", cmd_g)
            self.assertLess(cmd_g.index("-mastering_display:1"), cmd_g.index("-i"))
            self.assertIn("custom_shader_path=balanced.hook", cmd_g[cmd_g.index("-vf") + 1])

            fallback, plan_fallback = core.build_command(
                core.Options("in.mp4", str(dest), encoder=enc_key),
                fake_info, dest, detected_encoders=all_hw, gpu_filter_caps=no_gpu_filter,
            )
            self.assertFalse(plan_fallback["gpu_filter"])
            self.assertTrue(plan_fallback["fast_hw_filter"])
            self.assertEqual(plan_fallback["encoder"], enc_key)
            self.assertLessEqual(int(fallback[fallback.index("-filter_threads") + 1]), 4)
            self.assertIn("d=ordered", fallback[fallback.index("-vf") + 1])

        for style, shader in (("balanced", "balanced.hook"), ("natural", "natural.hook"), ("vivid", "vivid.hook")):
            cmd_st, plan_st = core.build_command(core.Options("in.mp4", str(dest), encoder="nvenc", hdr_style=style, gpu_filter="on"),
                                                 hevc_info, dest, detected_encoders=all_hw, gpu_filter_caps=yes_gpu_filter)
            self.assertEqual(plan_st["hdr_style"], style)
            self.assertIn(f"custom_shader_path={shader}", cmd_st[cmd_st.index("-vf") + 1])

        # NVENC uses calibrated CQ (25/23/21) and 4-second GOP; fine defaults to single-pass (p6), multipass enables p7 + qres.
        for q_tier, expected_cq in (("fast", "25"), ("standard", "23"), ("fine", "21")):
            cmd_nv_q, _ = core.build_command(
                core.Options("in.mp4", str(dest), encoder="nvenc", quality=q_tier),
                fake_info, dest, detected_encoders=all_hw, gpu_filter_caps=no_gpu_filter,
            )
            self.assertEqual(cmd_nv_q[cmd_nv_q.index("-cq") + 1], expected_cq)
            self.assertEqual(cmd_nv_q[cmd_nv_q.index("-g") + 1], "120")

        cmd_nv_fine, plan_nv_fine = core.build_command(
            core.Options("in.mp4", str(dest), encoder="nvenc", quality="fine"),
            fake_info, dest, detected_encoders=all_hw, gpu_filter_caps=no_gpu_filter,
        )
        self.assertFalse(plan_nv_fine["multipass"])
        self.assertEqual(plan_nv_fine["preset"], "p6")
        self.assertNotIn("-multipass", cmd_nv_fine)

        cmd_nv_mp, plan_nv_mp = core.build_command(
            core.Options("in.mp4", str(dest), encoder="nvenc", quality="fine", multipass=True),
            fake_info, dest, detected_encoders=all_hw, gpu_filter_caps=no_gpu_filter,
        )
        self.assertTrue(plan_nv_mp["multipass"])
        self.assertEqual(plan_nv_mp["preset"], "p7")
        self.assertEqual(cmd_nv_mp[cmd_nv_mp.index("-multipass") + 1], "qres")

        # QSV uses calibrated ICQ (23/21/19) and enables ExtBRC/MBBRC only when probed supported.
        for q_tier, expected_icq in (("fast", "23"), ("standard", "21"), ("fine", "19")):
            cmd_qsv_no_ext, plan_qsv_no_ext = core.build_command(
                core.Options("in.mp4", str(dest), encoder="qsv", quality=q_tier),
                fake_info, dest, detected_encoders=all_hw, gpu_filter_caps=no_gpu_filter,
            )
            self.assertEqual(cmd_qsv_no_ext[cmd_qsv_no_ext.index("-global_quality") + 1], expected_icq)
            self.assertFalse(plan_qsv_no_ext["qsv_extbrc"])
            self.assertNotIn("-extbrc", cmd_qsv_no_ext)

            all_hw_ext = fake_detected(nvenc=True, nvenc_lossless=True, qsv=True, qsv_extbrc=True, amf=True)
            cmd_qsv_ext, plan_qsv_ext = core.build_command(
                core.Options("in.mp4", str(dest), encoder="qsv", quality=q_tier),
                fake_info, dest, detected_encoders=all_hw_ext, gpu_filter_caps=no_gpu_filter,
            )
            self.assertEqual(cmd_qsv_ext[cmd_qsv_ext.index("-global_quality") + 1], expected_icq)
            self.assertTrue(plan_qsv_ext["qsv_extbrc"])
            for flag in ("-extbrc", "-mbbrc", "-adaptive_i", "-adaptive_b"):
                self.assertEqual(cmd_qsv_ext[cmd_qsv_ext.index(flag) + 1], "1")

        # Unsupported codec for Vulkan Video (e.g. ffv1) keeps GPU shader when gpu_filter="on" but skips -hwaccel vulkan.
        ffv1_info = {**fake_info, "streams": [{**fake_info["streams"][0], "codec_name": "ffv1", "pix_fmt": "yuv444p10le"}, fake_info["streams"][1]]}
        cmd_ffv1, plan_ffv1 = core.build_command(core.Options("in.mkv", str(dest), encoder="nvenc", quality="standard", gpu_filter="on"),
                                                 ffv1_info, dest, detected_encoders=all_hw, gpu_filter_caps=yes_gpu_filter)
        self.assertTrue(plan_ffv1["gpu_filter"])
        self.assertFalse(plan_ffv1["vulkan_hwdec"])
        self.assertNotIn("-hwaccel", cmd_ffv1)
        self.assertIn("setparams=", cmd_ffv1[cmd_ffv1.index("-vf") + 1])

        # Lossless and gpu_filter='off' always use the deterministic CPU filter chain (error_diffusion + yuv420p10le).
        cmd_ll_hw, plan_ll_hw = core.build_command(core.Options("in.mp4", str(dest), encoder="nvenc", quality="lossless"),
                                                   fake_info, dest, detected_encoders=all_hw, gpu_filter_caps=yes_gpu_filter)
        self.assertFalse(plan_ll_hw["gpu_filter"])
        self.assertFalse(plan_ll_hw["fast_hw_filter"])
        self.assertTrue(cmd_ll_hw[cmd_ll_hw.index("-vf") + 1].endswith("format=yuv420p10le,setsar=1,format=p010le"))
        cmd_off, plan_off = core.build_command(core.Options("in.mp4", str(dest), encoder="nvenc", gpu_filter="off"),
                                               fake_info, dest, detected_encoders=all_hw, gpu_filter_caps=yes_gpu_filter)
        self.assertFalse(plan_off["gpu_filter"])
        self.assertFalse(plan_off["fast_hw_filter"])
        self.assertTrue(cmd_off[cmd_off.index("-vf") + 1].endswith("format=yuv420p10le,setsar=1,format=p010le"))

        cmd_cpu, plan_cpu = core.build_command(core.Options("in.mp4", str(dest), encoder="cpu"), fake_info, dest, detected_encoders=all_hw)
        self.assertEqual(plan_cpu["encoder"], "cpu")
        self.assertEqual(plan_cpu["video_codec"], "libx265")
        self.assertFalse(plan_cpu["hardware_accelerated"])
        self.assertTrue(cmd_cpu[cmd_cpu.index("-vf") + 1].endswith("format=yuv420p10le,setsar=1"))

        # auto falls back to CPU when QSV/AMF only and quality is lossless.
        _, plan_ll_fallback = core.build_command(core.Options("in.mp4", str(dest), encoder="auto", quality="lossless"), fake_info, dest, detected_encoders=qsv_amf)
        self.assertEqual(plan_ll_fallback["encoder"], "cpu")
        self.assertTrue(plan_ll_fallback["lossless_video"])

        # auto falls back to CPU when dimensions are below hardware minimum.
        small_info = {**fake_info, "streams": [{**fake_info["streams"][0], "width": 128, "height": 72}, fake_info["streams"][1]]}
        _, plan_small_fallback = core.build_command(core.Options("in.mp4", str(dest), encoder="auto"), small_info, dest, detected_encoders=all_hw)
        self.assertEqual(plan_small_fallback["encoder"], "cpu")
        self.assertIn("尺寸", plan_small_fallback["encoder_note"])

        # Explicit requests validate availability, dimensions, and lossless support.
        with self.assertRaisesRegex(core.ConversionError, "不可用"):
            core.build_command(core.Options("in.mp4", str(dest), encoder="nvenc"), fake_info, dest, detected_encoders=qsv_amf)
        with self.assertRaisesRegex(core.ConversionError, "支持范围"):
            core.build_command(core.Options("in.mp4", str(dest), encoder="nvenc"), small_info, dest, detected_encoders=all_hw)
        with self.assertRaisesRegex(core.ConversionError, "无损"):
            core.build_command(core.Options("in.mp4", str(dest), encoder="qsv", quality="lossless"), fake_info, dest, detected_encoders=all_hw)
        with self.assertRaisesRegex(core.ConversionError, "无损"):
            core.build_command(core.Options("in.mp4", str(dest), encoder="amf", quality="lossless"), fake_info, dest, detected_encoders=all_hw)

    def test_real_hardware_encoder_conversion_and_lossless(self):
        core.clear_encoder_cache()
        detected = core.detect_encoders(force=True)
        self.assertEqual(set(detected.keys()), {"cpu", "nvenc", "qsv", "amf"})
        for key, item in detected.items():
            self.assertIn("available", item)
            self.assertIn("lossless", item)
            self.assertIn("reason", item)
            self.assertEqual(item["key"], key)
        self.assertTrue(detected["cpu"]["available"])

        available_hw = [key for key in core.HARDWARE_PRIORITY if detected[key]["available"]]
        if not available_hw:
            self.skipTest("No supported 10-bit HEVC HDR10 hardware encoder available on this host")
        hw_key = available_hw[0]
        source = self.make_source(width=256, height=144, name="硬件 测试.mkv")

        auto_out = self.dir / "硬件自动标准.mp4"
        res_auto = core.convert(core.Options(str(source), str(auto_out), quality="standard", encoder="auto"))
        self.assertTrue(res_auto["plan"]["hardware_accelerated"])
        self.assertEqual(res_auto["plan"]["encoder"], hw_key)
        self.assertTrue(all(res_auto["validation"]["checks"].values()))
        report = json.loads(auto_out.with_suffix(".hdr.json").read_text(encoding="utf-8"))
        self.assertTrue(report["hardware_accelerated"])
        self.assertEqual(report["video_codec"], detected[hw_key]["codec"])

        fine_out = self.dir / "硬件精细环绕.mp4"
        res_fine = core.convert(core.Options(str(source), str(fine_out), quality="fine", audio_effect="surround", encoder=hw_key))
        self.assertEqual(res_fine["plan"]["encoder"], hw_key)
        self.assertFalse(res_fine["plan"]["multipass"])
        self.assertTrue(all(res_fine["validation"]["checks"].values()))

        if hw_key == "nvenc":
            mp_out = self.dir / "硬件精细双遍.mp4"
            res_mp = core.convert(core.Options(str(source), str(mp_out), quality="fine", encoder="nvenc", multipass=True))
            self.assertTrue(res_mp["plan"]["multipass"])
            self.assertEqual(res_mp["plan"]["preset"], "p7")
            self.assertTrue(all(res_mp["validation"]["checks"].values()))

        # Copy HDR video stream generated by hardware encoder and verify video packets stay identical.
        copy_out = self.dir / "硬件直拷环绕.mp4"
        res_copy = core.convert(core.Options(str(auto_out), str(copy_out), audio_effect="surround", copy_hdr_video=True))
        self.assertEqual(res_copy["plan"]["video_operation"], "copy_hdr")
        self.assertTrue(all(res_copy["validation"]["checks"].values()))

        def video_packet_hash(path):
            r = core.capture([core.find_tool("ffmpeg"), "-v", "error", "-i", str(path), "-map", "0:v:0",
                              "-c", "copy", "-f", "hash", "-hash", "sha256", "-"])
            self.assertEqual(r.returncode, 0, r.stderr)
            return r.stdout.strip()

        self.assertEqual(video_packet_hash(auto_out), video_packet_hash(copy_out))

        if detected[hw_key]["lossless"]:
            ll_out = self.dir / "硬件无损.mp4"
            res_ll = core.convert(core.Options(str(source), str(ll_out), quality="lossless", encoder=hw_key))
            self.assertTrue(res_ll["plan"]["lossless_video"])
            self.assertEqual(res_ll["plan"]["encoder"], hw_key)
            self.assertTrue(res_ll["validation"]["lossless_pixels"]["all_frames_equal"])
            self.assertEqual(res_ll["validation"]["lossless_pixels"]["frames_compared"], 30)

    def _build_rpu_nal(self, profile="8.1"):
        bits = []
        def put(val, n):
            for i in range(n - 1, -1, -1):
                bits.append((val >> i) & 1)
        def ue(val):
            v = val + 1
            n = v.bit_length() - 1
            put(0, n)
            put(v, n + 1)
        def se(val):
            ue(val * 2 - 1 if val > 0 else -val * 2)

        put(2, 6); put(18, 11)
        put(0, 4); put(0, 4)
        put(1, 1); put(0, 1); put(0, 2); ue(14); put(2, 2)
        put(1 if profile == "5" else 0, 1)
        ue(2); ue(2); ue(4)
        put(0, 1); put(0, 3); put(0, 1); put(1, 1)
        put(1, 1); put(0, 1); ue(0)
        ue(0); ue(0)
        for _ in range(3):
            ue(0); put(0, 10); put(1023, 10)
        ue(0); ue(0)
        for _ in range(3):
            ue(0); ue(0); put(0, 1)
            se(0); put(0, 14)
            se(1); put(0, 14)
        ue(0); ue(0); ue(0)
        if profile == "5":
            ycc = [8192, 0, 8192, 8192, -2048, -2048, 8192, 8192, 0]
            off = [0, 1 << 27, 1 << 27]
            lms = [16384, 0, 0, 0, 16384, 0, 0, 0, 16384]
        else:
            ycc = [8192, 0, 12080, 8192, -1348, -4681, 8192, 15413, 0]
            off = [0, 1 << 27, 1 << 27]
            lms = [5888, 9626, 870, 2708, 12030, 1646, 369, 1437, 14578]
        for c in ycc:
            put(c & 0xFFFF, 16)
        for o in off:
            put(o & 0xFFFFFFFF, 32)
        for c in lms:
            put(c & 0xFFFF, 16)
        put(18 if profile == "8.4" else 65535, 16)
        put(0, 16); put(0, 16); put(0, 32)
        put(10, 5)
        put(3 if profile == "5" else 0, 2)
        put(0, 2)
        put(2 if profile == "5" else 1, 2)
        put(1, 12); put(3079, 12); put(0, 10)
        ue(0)
        while len(bits) % 8:
            bits.append(0)
        payload = bytes(sum(bits[i + j] << (7 - j) for j in range(8)) for i in range(0, len(bits), 8))
        crc = 0xFFFFFFFF
        for byte in payload:
            crc ^= byte << 24
            for _ in range(8):
                crc = (((crc << 1) ^ 0x04C11DB7) if (crc & 0x80000000) else (crc << 1)) & 0xFFFFFFFF
        raw = b"\x19" + payload + crc.to_bytes(4, "big") + b"\x80"
        escaped = bytearray()
        zeros = 0
        for b in raw:
            if zeros == 2 and b <= 3:
                escaped.append(3)
                zeros = 0
            escaped.append(b)
            zeros = zeros + 1 if b == 0 else 0
        return b"\x00\x00\x00\x01\x7c\x01" + bytes(escaped)

    def make_dolby_vision_source(self, profile="8.1", with_el=False, omit_cll=False, name=None):
        ffmpeg = core.find_tool("ffmpeg")
        raw = self.dir / f"base_dv_{profile}_{with_el}_{omit_cll}.hevc"
        if profile == "5":
            x265p = "aud=1:bframes=0:range=full:repeat-headers=1"
        elif profile == "8.4":
            x265p = "aud=1:bframes=0:colorprim=bt2020:transfer=arib-std-b67:colormatrix=bt2020nc:range=limited:repeat-headers=1"
        else:
            cll_part = "" if omit_cll else ":max-cll=1000,1000"
            x265p = (
                "aud=1:bframes=0:colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:range=limited:"
                f"hdr10=1:repeat-headers=1:master-display={core.MASTERING_DISPLAY}{cll_part}"
            )
        r = core.capture([
            ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=256x144:r=25:d=0.4",
            "-c:v", "libx265", "-pix_fmt", "yuv420p10le", "-preset", "ultrafast",
            "-x265-params", x265p, "-f", "hevc", str(raw),
        ])
        self.assertEqual(r.returncode, 0, r.stderr)
        suffix = (b"\x00\x00\x00\x01\x7e\x01\x00\x00\x03\x01\xaa\xbb" if with_el else b"") + self._build_rpu_nal(profile)
        marker = b"\x00\x00\x00\x01\x46\x01"
        parts = raw.read_bytes().split(marker)
        self.assertGreater(len(parts), 1)
        injected = bytearray(parts[0])
        for part in parts[1:]:
            injected.extend(marker)
            injected.extend(part)
            injected.extend(suffix)
        dv_hevc = self.dir / f"dv_{profile}_{with_el}_{omit_cll}.hevc"
        dv_hevc.write_bytes(bytes(injected))
        mp4 = self.dir / (name or f"dv_{profile}.mp4")
        r_mux = core.capture([
            ffmpeg, "-y", "-v", "error", "-r", "25", "-i", str(dv_hevc),
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=0.4",
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy", "-bsf:v", "dovi_rpu=compression=none",
            "-c:a", "aac", "-ar", "48000", "-ac", "2",
            "-strict", "unofficial", str(mp4),
        ])
        self.assertEqual(r_mux.returncode, 0, r_mux.stderr)
        return mp4

    def test_dolby_vision_profile_detection_and_command_routing(self):
        fake_p7_info = {
            "streams": [
                {
                    "index": 0, "codec_type": "video", "codec_name": "hevc",
                    "width": 3840, "height": 2160, "pix_fmt": "yuv420p10le",
                    "color_space": "bt2020nc", "color_primaries": "bt2020",
                    "color_transfer": "smpte2084", "color_range": "tv",
                    "avg_frame_rate": "24000/1001", "duration": "120.0",
                    "side_data_list": [{
                        "side_data_type": "DOVI configuration record",
                        "dv_profile": 7, "dv_bl_signal_compatibility_id": 6,
                        "el_present_flag": 1, "rpu_present_flag": 1, "bl_present_flag": 1,
                    }],
                },
                {"index": 1, "codec_type": "audio", "codec_name": "aac", "channels": 2, "sample_rate": "48000", "duration": "120.0"},
            ],
            "format": {"duration": "120.0"},
        }
        dv7 = core.detect_dolby_vision(fake_p7_info)
        self.assertIsNotNone(dv7)
        self.assertEqual(dv7["profile_code"], "7")
        self.assertTrue(dv7["strip_supported"])
        self.assertTrue(dv7["el_present"])

        dest = self.dir / "p7_out.mp4"
        tools_all = dict(dovi_rpu=True, dovi_split=True, filter_units=True, libplacebo=True, strip_available=True, strip_reason="", libplacebo_reason="")
        cmd7, plan7 = core.build_command(core.Options("p7.mkv", str(dest)), fake_p7_info, dest, dv_info=dv7, dv_tools=tools_all)
        self.assertEqual(plan7["video_operation"], "dv_strip")
        self.assertEqual(cmd7[cmd7.index("-bsf:v") + 1], "dovi_split=mode=bl,dovi_rpu=strip=1")
        self.assertEqual(cmd7[cmd7.index("-c:v") + 1], "copy")

        # Fallback to filter_units when dovi_split is unavailable.
        tools_no_split = {**tools_all, "dovi_split": False}
        cmd7_fb, _ = core.build_command(core.Options("p7.mkv", str(dest)), fake_p7_info, dest, dv_info=dv7, dv_tools=tools_no_split)
        self.assertEqual(cmd7_fb[cmd7_fb.index("-bsf:v") + 1], "filter_units=remove_types=63,dovi_rpu=strip=1")

        # Missing dovi_rpu raises clear ConversionError.
        with self.assertRaisesRegex(core.ConversionError, "dovi_rpu"):
            core.build_command(core.Options("p7.mkv", str(dest)), fake_p7_info, dest, dv_info=dv7, dv_tools={**tools_all, "dovi_rpu": False})

        # 4000-nit source_max_pq metadata derivation for DV Profile 5 re-encode.
        mastering_4k, cll_4k, peak_4k = core._dv_static_metadata({"source_max_pq": 3696})
        self.assertEqual(peak_4k, 4000)
        self.assertIn("L(40000000,1)", mastering_4k)
        self.assertEqual(cll_4k, "4000,1000")

        # Dual-track Profile 7 detection (DOVI record on secondary enhancement stream).
        dual_p7_info = {
            "streams": [
                {**fake_p7_info["streams"][0], "side_data_list": []},
                {"index": 1, "codec_type": "video", "codec_name": "hevc", "width": 1920, "height": 1080,
                 "side_data_list": fake_p7_info["streams"][0]["side_data_list"]},
                {**fake_p7_info["streams"][1], "index": 2},
            ],
            "format": {"duration": "120.0"},
        }
        dv7_dual = core.detect_dolby_vision(dual_p7_info)
        self.assertIsNotNone(dv7_dual)
        self.assertTrue(dv7_dual["dual_track"])
        self.assertTrue(dv7_dual["strip_supported"])

        # Tolerant matrix check: MKV Remuxes omitting color_space still strip losslessly.
        no_matrix_p81 = {
            "streams": [
                {
                    **fake_p7_info["streams"][0],
                    "color_space": "unknown",
                    "side_data_list": [{
                        "side_data_type": "DOVI configuration record",
                        "dv_profile": 8, "dv_bl_signal_compatibility_id": 1,
                        "el_present_flag": 0, "rpu_present_flag": 1, "bl_present_flag": 1,
                    }],
                },
                fake_p7_info["streams"][1],
            ],
            "format": {"duration": "120.0"},
        }
        dv81_nomat = core.detect_dolby_vision(no_matrix_p81)
        self.assertTrue(dv81_nomat["strip_supported"])
        _, plan81_nomat = core.build_command(core.Options("p81.mkv", str(dest)), no_matrix_p81, dest, dv_info=dv81_nomat, dv_tools=tools_all)
        self.assertEqual(plan81_nomat["video_operation"], "dv_strip")
        self.assertFalse(plan81_nomat["require_matrix_metadata"])

        # DV Profile 5 re-encode enables Vulkan hwdec when supported, but omits it for lossless / gpu_filter=off.
        p5_info = {
            "streams": [
                {
                    "index": 0, "codec_type": "video", "codec_name": "hevc",
                    "width": 3840, "height": 2160, "pix_fmt": "yuv420p10le",
                    "color_range": "pc", "avg_frame_rate": "24000/1001", "duration": "120.0",
                    "side_data_list": [{
                        "side_data_type": "DOVI configuration record",
                        "dv_profile": 5, "dv_bl_signal_compatibility_id": 0,
                        "el_present_flag": 0, "rpu_present_flag": 1, "bl_present_flag": 1,
                    }],
                },
                fake_p7_info["streams"][1],
            ],
            "format": {"duration": "120.0"},
        }
        dv5_meta = core.detect_dolby_vision(p5_info)
        yes_vk = dict(libplacebo_shader=True, vulkan_hwdec=True, reason="")
        cmd5_vk, plan5_vk = core.build_command(core.Options("p5.mp4", str(dest), encoder="cpu"), p5_info, dest,
                                               dv_info=dv5_meta, dv_tools=tools_all, gpu_filter_caps=yes_vk)
        self.assertTrue(plan5_vk["vulkan_hwdec"])
        self.assertIn("-hwaccel", cmd5_vk)
        self.assertIn("vulkan", cmd5_vk)

        cmd5_ll, plan5_ll = core.build_command(core.Options("p5.mp4", str(dest), encoder="cpu", quality="lossless"), p5_info, dest,
                                               dv_info=dv5_meta, dv_tools=tools_all, gpu_filter_caps=yes_vk)
        self.assertFalse(plan5_ll["vulkan_hwdec"])
        self.assertNotIn("-hwaccel", cmd5_ll)

        cmd5_off, plan5_off = core.build_command(core.Options("p5.mp4", str(dest), encoder="cpu", gpu_filter="off"), p5_info, dest,
                                                 dv_info=dv5_meta, dv_tools=tools_all, gpu_filter_caps=yes_vk)
        self.assertFalse(plan5_off["vulkan_hwdec"])
        self.assertNotIn("-hwaccel", cmd5_off)

    def test_real_dolby_vision_conversions(self):
        core.clear_dv_tools_cache()
        tools = core.detect_dv_tools(force=True)
        self.assertTrue(tools["dovi_rpu"])

        # 1. DV Profile 8.1 -> lossless stream-copy strip + surround audio option
        dv81 = self.make_dolby_vision_source("8.1", name="dv81.mp4")
        insp81 = core.inspect_input(dv81)
        self.assertEqual(insp81["kind"], "dv_strip")
        self.assertEqual(insp81["dv"]["profile_code"], "8.1")

        out81 = self.dir / "dv81_HDR10.mp4"
        res81 = core.convert(core.Options(str(dv81), str(out81), audio_effect="surround"))
        self.assertEqual(res81["plan"]["video_operation"], "dv_strip")
        self.assertEqual(res81["video_codec"], "copy")
        self.assertTrue(res81["validation"]["checks"]["no_dovi"])
        self.assertTrue(all(res81["validation"]["checks"].values()))

        # 2. DV Profile 8.1 without MaxCLL/MaxFALL (common Blu-ray base layer) still passes
        dv81_nocll = self.make_dolby_vision_source("8.1", omit_cll=True, name="dv81_nocll.mp4")
        out81_nocll = self.dir / "dv81_nocll_HDR10.mp4"
        res81_nocll = core.convert(core.Options(str(dv81_nocll), str(out81_nocll)))
        self.assertEqual(res81_nocll["plan"]["video_operation"], "dv_strip")
        self.assertTrue(res81_nocll["validation"]["checks"]["no_dovi"])

        # 3. DV with Enhancement Layer (NAL 63) + RPU (NAL 62) -> verify both NAL 62 & 63 stripped
        dv7_sim = self.make_dolby_vision_source("8.1", with_el=True, name="dv7_el.mp4")
        out7 = self.dir / "dv7_HDR10.mp4"
        info7 = core.probe(dv7_sim)
        dv7_meta = {**core.detect_dolby_vision(info7, dv7_sim), "dv_profile": 7, "el_present": True, "profile_code": "7"}
        with patch.object(core, "detect_dolby_vision", return_value=dv7_meta):
            res7 = core.convert(core.Options(str(dv7_sim), str(out7)))
        self.assertEqual(res7["plan"]["video_operation"], "dv_strip")
        self.assertIn("dovi_split=mode=bl", res7["plan"]["dv_bsf"])
        raw7 = self.dir / "dv7_out.hevc"
        core.capture([core.find_tool("ffmpeg"), "-y", "-v", "error", "-i", str(out7), "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb", str(raw7)])
        raw7_bytes = raw7.read_bytes()
        self.assertNotIn(b"\x00\x00\x01\x7c\x01", raw7_bytes)
        self.assertNotIn(b"\x00\x00\x01\x7e\x01", raw7_bytes)

        if not tools["libplacebo"]:
            self.skipTest("libplacebo/Vulkan not available on this host")

        gpu_caps = core.detect_gpu_filter()

        # 4. DV Profile 5 (IPTPQc2) -> Vulkan hwdec + libplacebo + encoder (and reject dv_mode='strip')
        dv5 = self.make_dolby_vision_source("5", name="dv5.mp4")
        insp5 = core.inspect_input(dv5)
        self.assertEqual(insp5["kind"], "dv_reencode")
        self.assertEqual(insp5["dv"]["profile_code"], "5")

        with self.assertRaisesRegex(core.ConversionError, "无法无损直剥"):
            core.convert(core.Options(str(dv5), str(self.dir / "dv5_bad_strip.mp4"), dv_mode="strip"))

        out5 = self.dir / "dv5_HDR10.mp4"
        res5 = core.convert(core.Options(str(dv5), str(out5), quality="standard", encoder="auto"))
        self.assertEqual(res5["plan"]["video_operation"], "dv_reencode")
        self.assertEqual(res5["plan"]["vulkan_hwdec"], gpu_caps["vulkan_hwdec"])
        self.assertTrue(res5["validation"]["checks"]["no_dovi"])
        self.assertTrue(all(res5["validation"]["checks"].values()))

        # DV Profile 5 lossless mode -> bit-exact frame verification against libplacebo reference
        out5_ll = self.dir / "dv5_HDR10_lossless.mp4"
        res5_ll = core.convert(core.Options(str(dv5), str(out5_ll), quality="lossless", encoder="auto"))
        self.assertTrue(res5_ll["plan"]["lossless_video"])
        self.assertFalse(res5_ll["plan"]["vulkan_hwdec"])
        self.assertTrue(res5_ll["validation"]["lossless_pixels"]["all_frames_equal"])
        self.assertEqual(res5_ll["validation"]["lossless_pixels"]["frames_compared"], 10)

        # 5. DV Profile 8.4 (HLG Dolby Vision) -> Vulkan hwdec + libplacebo + PQ HDR10
        dv84 = self.make_dolby_vision_source("8.4", name="dv84.mp4")
        insp84 = core.inspect_input(dv84)
        self.assertEqual(insp84["kind"], "dv_reencode")
        self.assertEqual(insp84["dv"]["profile_code"], "8.4")

        out84 = self.dir / "dv84_HDR10.mp4"
        res84 = core.convert(core.Options(str(dv84), str(out84), quality="fast", encoder="auto"))
        self.assertEqual(res84["plan"]["video_operation"], "dv_reencode")
        self.assertEqual(res84["plan"]["vulkan_hwdec"], gpu_caps["vulkan_hwdec"])
        self.assertTrue(res84["validation"]["checks"]["PQ"])
        self.assertTrue(res84["validation"]["checks"]["no_dovi"])
        self.assertTrue(all(res84["validation"]["checks"].values()))

    def test_natural_style_lut_accuracy_and_skin_protection(self):
        def expected_bt2020_linear(rgb):
            lin = [v ** 2.4 for v in rgb]
            return tuple(sum(row[j] * lin[j] for j in range(3)) for row in core.M_BT709_TO_BT2020)

        def decoded_nits(rgb):
            return tuple(core.pq_decode(v) for v in core.natural_reference_rgb(rgb))

        # Verify zero skin chromaticity shift and highlight protection thresholds.
        for skin in ((0.78, 0.62, 0.50), (0.92, 0.78, 0.65), (0.96, 0.88, 0.78)):
            lin20 = expected_bt2020_linear(skin)
            nits = decoded_nits(skin)
            self.assertAlmostEqual(nits[1] / nits[0], lin20[1] / lin20[0], delta=1e-5)
            self.assertAlmostEqual(nits[2] / nits[0], lin20[2] / lin20[0], delta=1e-5)

        self.assertLess(max(decoded_nits((0.15, 0.12, 0.10))), 2.5)
        self.assertLess(max(decoded_nits((0.92, 0.78, 0.65))), 150.0)
        self.assertLess(max(decoded_nits((0.96, 0.88, 0.78))), 205.0)
        self.assertLess(max(decoded_nits((0.95, 0.95, 0.94))), 305.0)
        for ch in decoded_nits((1.0, 1.0, 1.0)):
            self.assertAlmostEqual(ch, core.NATURAL_PEAK_NITS, delta=0.5)

        # Verify real FFmpeg 49^3 tetrahedral lut3d matches analytical natural_reference_rgb.
        rng = random.Random(2026)
        samples = [
            (0.0, 0.0, 0.0), (1.0, 1.0, 1.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0),
            (0.15, 0.12, 0.10), (0.5, 0.5, 0.5), (0.78, 0.62, 0.50), (0.92, 0.78, 0.65),
            (0.96, 0.88, 0.78), (0.95, 0.95, 0.94), (-0.05, -0.05, -0.05), (1.15, 1.15, 1.15),
        ]
        samples += [tuple(rng.uniform(0.0, 1.0) for _ in range(3)) for _ in range(256 - len(samples))]
        raw = b"".join(struct.pack("<f", rgb[channel]) for channel in (1, 2, 0) for rgb in samples)
        p = dict(primaries="bt709", transfer="bt709", matrix="bt709", range="full")
        chain = core.filter_chain(p, 16, 16, lut_name="natural.cube", style="natural")
        lut_filter = [f for f in chain.split(",") if f.startswith("lut3d=")][0]
        core.write_natural_lut(self.dir / "natural.cube")
        r = subprocess.run(
            [core.find_tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-f", "rawvideo",
             "-pix_fmt", "gbrpf32le", "-s", "16x16", "-r", "1", "-i", "pipe:0",
             "-vf", lut_filter, "-frames:v", "1", "-pix_fmt", "gbrpf32le", "-f", "rawvideo", "pipe:1"],
            input=raw, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=self.dir, creationflags=core.CREATE_NO_WINDOW,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        floats = struct.unpack("<768f", r.stdout)
        actual = [(floats[512 + i], floats[i], floats[256 + i]) for i in range(256)]
        max_err = max(abs(a - b) for rgb, out in zip(samples, actual)
                      for a, b in zip(core.natural_reference_rgb(rgb), out))
        max_nits_err = max(abs(core.pq_decode(a) - core.pq_decode(b)) for rgb, out in zip(samples, actual)
                           for a, b in zip(core.natural_reference_rgb(rgb), out))
        visible_err = max(abs(a - b) for rgb, out in zip(samples, actual)
                          for a, b in zip(core.natural_reference_rgb(rgb), out) if a >= 0.05)
        self.assertLess(max_err, 4e-3)
        self.assertLess(visible_err, 1e-3)
        self.assertLess(max_nits_err, 1.0)
        print(f"\nFFmpeg natural 49^3 lut3d vs analytical reference: visible PQ err={visible_err:.6f}, full PQ err={max_err:.6f}, max nits err={max_nits_err:.3f}")

        # Verify real conversion in natural mode (standard + lossless) and vivid mode (legacy compatibility).
        source = self.make_source(width=256, height=144, name="自然风格.mkv")
        out_nat = self.dir / "自然风格_标准.mp4"
        res_nat = core.convert(core.Options(str(source), str(out_nat), quality="standard", encoder="auto", hdr_style="natural"))
        self.assertEqual(res_nat["plan"]["hdr_style"], "natural")
        self.assertEqual(res_nat["plan"]["content_light"], "450,200")
        self.assertTrue(all(res_nat["validation"]["checks"].values()))
        cll_nat = next(s for s in res_nat["validation"]["first_frame_side_data"]
                       if s.get("side_data_type") == "Content light level metadata")
        self.assertEqual(cll_nat["max_content"], 450)
        self.assertEqual(cll_nat["max_average"], 200)
        report_nat = json.loads(out_nat.with_suffix(".hdr.json").read_text(encoding="utf-8"))
        self.assertEqual(report_nat["hdr_style"], "natural")

        out_nat_ll = self.dir / "自然风格_无损.mp4"
        res_nat_ll = core.convert(core.Options(str(source), str(out_nat_ll), quality="lossless", encoder="auto", hdr_style="natural"))
        self.assertEqual(res_nat_ll["plan"]["hdr_style"], "natural")
        self.assertTrue(res_nat_ll["validation"]["lossless_pixels"]["all_frames_equal"])
        self.assertEqual(res_nat_ll["validation"]["lossless_pixels"]["frames_compared"], 30)

        out_vivid = self.dir / "高对比原版.mp4"
        res_vivid = core.convert(core.Options(str(source), str(out_vivid), quality="fast", encoder="cpu", hdr_style="vivid"))
        self.assertEqual(res_vivid["plan"]["hdr_style"], "vivid")
        self.assertEqual(res_vivid["plan"]["content_light"], "1000,1000")
        self.assertTrue(all(res_vivid["validation"]["checks"].values()))
        cll_vivid = next(s for s in res_vivid["validation"]["first_frame_side_data"]
                         if s.get("side_data_type") == "Content light level metadata")
        self.assertEqual(cll_vivid["max_content"], 1000)
        self.assertEqual(cll_vivid["max_average"], 1000)

        with self.assertRaisesRegex(core.ConversionError, "HDR 风格"):
            core.convert(core.Options(str(source), str(self.dir / "无效风格.mp4"), hdr_style="unknown"))

    def test_gpu_libplacebo_sdr_pipeline_accuracy_and_conversion(self):
        core.clear_gpu_filter_cache()
        gpu_caps = core.detect_gpu_filter(force=True)
        detected = core.detect_encoders()
        available_hw = [key for key in core.HARDWARE_PRIORITY if detected[key]["available"]]
        if not available_hw or not gpu_caps["libplacebo_shader"]:
            self.skipTest("Hardware encoder or Vulkan/libplacebo GPU shader not available on this host")

        ffmpeg = core.find_tool("ffmpeg")
        skin_src = self.dir / "skin_h264.mp4"
        r = core.capture([
            ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=0xebc7a6:s=256x144:r=25:d=1.0",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1.0",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv",
            "-c:a", "aac", "-ar", "48000", "-ac", "2", str(skin_src),
        ])
        self.assertEqual(r.returncode, 0, r.stderr)

        def center_yuv10(mp4_path: Path):
            raw = subprocess.run(
                [ffmpeg, "-hide_banner", "-v", "error", "-i", str(mp4_path), "-frames:v", "1",
                 "-pix_fmt", "yuv420p10le", "-f", "rawvideo", "pipe:1"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=core.CREATE_NO_WINDOW,
            )
            self.assertEqual(raw.returncode, 0, raw.stderr)
            y = struct.unpack(f"<{256 * 144}H", raw.stdout[:256 * 144 * 2])[256 * 72 + 128]
            u = struct.unpack(f"<{128 * 72}H", raw.stdout[256 * 144 * 2:256 * 144 * 2 + 128 * 72 * 2])[128 * 36 + 64]
            v = struct.unpack(f"<{128 * 72}H", raw.stdout[256 * 144 * 2 + 128 * 72 * 2:])[128 * 36 + 64]
            return (y, u, v)

        for style, expected_cll in (("balanced", (1000, 260)), ("natural", (450, 200)),
                                    ("darkroom", (450, 450)), ("vivid", (1000, 1000))):
            fast_out = self.dir / f"fast_{style}.mp4"
            gpu_out = self.dir / f"gpu_{style}.mp4"
            cpu_out = self.dir / f"cpu_{style}.mp4"
            res_fast = core.convert(core.Options(str(skin_src), str(fast_out), quality="standard", encoder="auto",
                                                 hdr_style=style, gpu_filter="simd"))
            res_gpu = core.convert(core.Options(str(skin_src), str(gpu_out), quality="standard", encoder="auto",
                                                hdr_style=style))
            res_cpu = core.convert(core.Options(str(skin_src), str(cpu_out), quality="standard", encoder="auto",
                                                hdr_style=style, gpu_filter="off"))
            self.assertTrue(res_fast["plan"]["fast_hw_filter"])
            self.assertFalse(res_fast["plan"]["gpu_filter"])
            self.assertTrue(res_gpu["plan"]["gpu_filter"])
            self.assertFalse(res_gpu["plan"]["vulkan_hwdec"])
            self.assertFalse(res_cpu["plan"]["gpu_filter"])
            self.assertTrue(all(res_fast["validation"]["checks"].values()))
            self.assertTrue(all(res_gpu["validation"]["checks"].values()))
            cll = next(s for s in res_fast["validation"]["first_frame_side_data"]
                       if s.get("side_data_type") == "Content light level metadata")
            self.assertEqual((cll["max_content"], cll["max_average"]), expected_cll)
            f_yuv, g_yuv, c_yuv = center_yuv10(fast_out), center_yuv10(gpu_out), center_yuv10(cpu_out)
            for f_ch, c_ch in zip(f_yuv, c_yuv):
                self.assertLessEqual(abs(f_ch - c_ch), 4, f"{style} FastSIMD {f_yuv} vs CPU {c_yuv}")
            for g_ch, c_ch in zip(g_yuv, c_yuv):
                self.assertLessEqual(abs(g_ch - c_ch), 4, f"{style} GPU {g_yuv} vs CPU {c_yuv}")

        # Verify a clipped export on the explicitly requested Vulkan path.
        clip_out = self.dir / "gpu_clipped.mp4"
        res_clip = core.convert(core.Options(str(skin_src), str(clip_out), quality="fast", encoder="auto",
                                             start=0.2, duration=0.4, hdr_style="natural", gpu_filter="on"))
        self.assertTrue(res_clip["plan"]["gpu_filter"])
        self.assertTrue(all(res_clip["validation"]["checks"].values()))

    def test_darkroom_lift_and_shoulder(self):
        points = [core.darkroom_target_nits(i / 4096) for i in range(4097)]
        self.assertEqual(points[0], 0)
        self.assertEqual(points[-1], 450)
        self.assertTrue(all(right > left for left, right in zip(points, points[1:])))
        for code in (0.05, 0.1, 0.2, 0.5):
            linear = code ** 2.4
            self.assertGreater(core.darkroom_target_nits(linear), 1.15 * core.natural_target_nits(linear))
        # Compare equal SDR code intervals approaching white, retaining positive slope.
        def nits(code):
            return core.darkroom_target_nits(code ** 2.4)
        self.assertLess(nits(1.0) - nits(0.99), nits(0.97) - nits(0.96))
        # Keep the concentration of the lift in shadows, with less gain on lit skin and clouds.
        relative_gains = [nits(v) / core.natural_target_nits(v ** 2.4) for v in (0.1, 0.5, 0.85, 0.95)]
        self.assertEqual(relative_gains, sorted(relative_gains, reverse=True))
        self.assertLess(relative_gains[2], 1.20)
        source = self.make_source(width=256, height=144)
        result = core.convert(core.Options(str(source), str(self.dir / "lift.mp4"),
                                           encoder="cpu", hdr_style="darkroom", quality="lossless"))
        self.assertEqual(result["hdr_style"], "darkroom")
        self.assertEqual(result["validation"]["lossless_pixels"]["all_frames_equal"], True)
        self.assertEqual(result["plan"]["content_light"], "450,450")

    def test_balanced_style_lut_accuracy_midtone_and_headroom(self):
        # Verify strict monotonicity over [0, 1].
        ys = [i / 1000.0 for i in range(1001)]
        ls = [core.balanced_target_nits(y) for y in ys]
        min_slope = min((ls[i + 1] - ls[i]) * 1000.0 for i in range(1000))
        self.assertGreater(min_slope, 200.0)

        def expected_bt2020_linear(rgb):
            lin = [v ** core.BALANCED_GAMMA for v in rgb]
            return tuple(sum(row[j] * lin[j] for j in range(3)) for row in core.M_BT709_TO_BT2020)

        def decoded_nits(rgb):
            return tuple(core.pq_decode(v) for v in core.balanced_reference_rgb(rgb))

        # Zero skin chromaticity shift across all brightness tiers.
        for skin in ((0.78, 0.62, 0.50), (0.92, 0.78, 0.65), (0.96, 0.88, 0.78)):
            lin20 = expected_bt2020_linear(skin)
            nits = decoded_nits(skin)
            self.assertAlmostEqual(nits[1] / nits[0], lin20[1] / lin20[0], delta=1e-5)
            self.assertAlmostEqual(nits[2] / nits[0], lin20[2] / lin20[0], delta=1e-5)

        # Preserve the complete v1.6.1 shadow/midtone range, not just the black point.
        for i in range(361):
            y = i / 1000.0
            self.assertAlmostEqual(core.balanced_target_nits(y), 235.0 * y, places=8)
        # Joins must not introduce visible kinks even when the maximum remains unchanged.
        h = 1e-6
        for y, nits in core.BALANCED_KNOTS[:-1]:
            self.assertAlmostEqual(core.balanced_target_nits(y), nits, places=7)
            left = (nits - core.balanced_target_nits(y - h)) / h
            right = (core.balanced_target_nits(y + h) - nits) / h
            self.assertAlmostEqual(left, right, delta=0.001 * left)
        self.assertEqual(decoded_nits((0.0, 0.0, 0.0)), (0.0, 0.0, 0.0))
        self.assertGreaterEqual(max(decoded_nits((0.05, 0.05, 0.05))), 0.30)
        shelf_nits = max(decoded_nits((0.10, 0.10, 0.10)))
        self.assertAlmostEqual(shelf_nits, 1.48, delta=0.03)
        self.assertAlmostEqual(max(decoded_nits((0.5, 0.5, 0.5))), 51.14, delta=0.1)
        shadow_nits = max(decoded_nits((0.12, 0.10, 0.08)))
        self.assertGreater(shadow_nits, shelf_nits)
        self.assertLess(shadow_nits, 4.0)
        self.assertGreater(max(decoded_nits((0.50, 0.52, 0.56))), 60.0)
        forehead_peak = max(decoded_nits((0.92, 0.78, 0.65)))
        self.assertGreater(forehead_peak, 160.0)
        self.assertLess(forehead_peak, 220.0)
        # Preserve distinguishable highlight increments with a softer white shoulder than v1.6.1.
        def pq_at(signal):
            return core.pq_encode(core.balanced_target_nits(signal ** core.BALANCED_GAMMA))
        top_step = pq_at(1.0) - pq_at(0.99)
        upper_step = pq_at(0.95) - pq_at(0.94)
        self.assertGreater(top_step, 2.0 / 876)
        self.assertLess(top_step, 0.85 * upper_step)
        # Keep several distinct 10-bit PQ levels while narrowing the lit-face range.
        face_step = pq_at(0.86) - pq_at(0.80)
        self.assertGreater(face_step, 12 / 876)
        self.assertLess(face_step, 21 / 876)
        self.assertGreater(max(decoded_nits((0.99, 0.99, 0.98))), 850.0)
        for ch in decoded_nits((1.0, 1.0, 1.0)):
            self.assertAlmostEqual(ch, core.BALANCED_PEAK_NITS, delta=0.5)

        # Verify real FFmpeg tetrahedral balanced.cube accuracy across 256 samples.
        rng = random.Random(2027)
        samples = [
            (0.0, 0.0, 0.0), (1.0, 1.0, 1.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0),
            (0.12, 0.10, 0.08), (0.50, 0.52, 0.56), (0.78, 0.62, 0.50), (0.92, 0.78, 0.65),
            (0.96, 0.88, 0.78), (0.99, 0.99, 0.98), (-0.05, -0.05, -0.05), (1.15, 1.15, 1.15),
        ]
        samples += [tuple(rng.uniform(0.0, 1.0) for _ in range(3)) for _ in range(256 - len(samples))]
        raw = b"".join(struct.pack("<f", rgb[channel]) for channel in (1, 2, 0) for rgb in samples)
        p = dict(primaries="bt709", transfer="bt709", matrix="bt709", range="full")
        chain = core.filter_chain(p, 16, 16, lut_name="balanced.cube", style="balanced")
        lut_filter = [f for f in chain.split(",") if f.startswith("lut3d=")][0]
        core.write_balanced_lut(self.dir / "balanced.cube")
        r = subprocess.run(
            [core.find_tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-f", "rawvideo",
             "-pix_fmt", "gbrpf32le", "-s", "16x16", "-r", "1", "-i", "pipe:0",
             "-vf", lut_filter, "-frames:v", "1", "-pix_fmt", "gbrpf32le", "-f", "rawvideo", "pipe:1"],
            input=raw, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=self.dir, creationflags=core.CREATE_NO_WINDOW,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        floats = struct.unpack("<768f", r.stdout)
        actual = [(floats[512 + i], floats[i], floats[256 + i]) for i in range(256)]
        max_err = max(abs(a - b) for rgb, out in zip(samples, actual)
                      for a, b in zip(core.balanced_reference_rgb(rgb), out))
        max_nits_err = max(abs(core.pq_decode(a) - core.pq_decode(b)) for rgb, out in zip(samples, actual)
                           for a, b in zip(core.balanced_reference_rgb(rgb), out))
        visible_err = max(abs(a - b) for rgb, out in zip(samples, actual)
                          for a, b in zip(core.balanced_reference_rgb(rgb), out) if a >= 0.08)
        self.assertLess(max_err, 4e-3)
        self.assertLess(visible_err, 1e-3)
        self.assertLess(max_nits_err, 2.0)
        print(f"\nFFmpeg balanced {core.BALANCED_LUT_SIZE}^3 lut3d vs analytical reference: visible PQ err={visible_err:.6f}, full PQ err={max_err:.6f}, max nits err={max_nits_err:.3f}")

        # Verify real standard and lossless conversion in balanced mode.
        source = self.make_source(width=256, height=144, name="均衡风格.mkv")
        out_bal = self.dir / "均衡风格_标准.mp4"
        res_bal = core.convert(core.Options(str(source), str(out_bal), quality="standard", encoder="auto", hdr_style="balanced"))
        self.assertEqual(res_bal["plan"]["hdr_style"], "balanced")
        self.assertEqual(res_bal["plan"]["content_light"], "1000,260")
        self.assertTrue(all(res_bal["validation"]["checks"].values()))
        cll_bal = next(s for s in res_bal["validation"]["first_frame_side_data"]
                       if s.get("side_data_type") == "Content light level metadata")
        self.assertEqual((cll_bal["max_content"], cll_bal["max_average"]), (1000, 260))

        out_bal_ll = self.dir / "均衡风格_无损.mp4"
        res_bal_ll = core.convert(core.Options(str(source), str(out_bal_ll), quality="lossless", encoder="auto", hdr_style="balanced"))
        self.assertEqual(res_bal_ll["plan"]["hdr_style"], "balanced")
        self.assertTrue(res_bal_ll["validation"]["lossless_pixels"]["all_frames_equal"])
        self.assertEqual(res_bal_ll["validation"]["lossless_pixels"]["frames_compared"], 30)

    def test_graceful_stop_keeps_playable_hdr_partial_mp4(self):
        ffmpeg = core.find_tool("ffmpeg")
        source = self.dir / "长片 源.mp4"
        r = core.capture([
            ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=25:d=30",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=30",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv",
            "-c:a", "aac", "-ar", "48000", "-ac", "2", str(source),
        ])
        self.assertEqual(r.returncode, 0, r.stderr)
        original_bytes = source.read_bytes()
        output = self.dir / "长片_HDR.mp4"
        keep_partial = threading.Event()
        events = []

        def on_progress(ev):
            events.append(ev)
            if ev.get("stage") == "encoding" and ev.get("seconds", 0) >= 0.4:
                keep_partial.set()

        result = core.convert(
            core.Options(str(source), str(output), quality="standard", encoder="cpu"),
            callback=on_progress,
            keep_partial=keep_partial,
        )
        self.assertTrue(result["partial"])
        self.assertTrue(result["stopped_early"])
        partial_path = Path(result["output"])
        self.assertTrue(partial_path.is_file())
        self.assertTrue(partial_path.name.endswith("_已转片段.mp4"))
        self.assertFalse(output.exists())
        self.assertEqual(list(self.dir.glob("*.part.mp4")), [])
        self.assertEqual(source.read_bytes(), original_bytes)
        self.assertTrue(all(result["validation"]["checks"].values()))
        actual_dur = core._known_duration(
            result["validation"]["stream"].get("duration"),
            result["validation"]["format"].get("duration"),
        )
        self.assertGreater(actual_dur, 0.2)
        self.assertLess(actual_dur, 25.0)
        report = json.loads(partial_path.with_suffix(".hdr.json").read_text(encoding="utf-8"))
        self.assertTrue(report["partial"])
        self.assertEqual(report["output"], str(partial_path))

    def test_progress_clock_eta_and_gui_detail_formatting(self):
        import app as gui_app

        self.assertEqual(core.format_duration_clock(None), "--:--")
        self.assertEqual(core.format_duration_clock(-1), "--:--")
        self.assertEqual(core.format_duration_clock(float("nan")), "--:--")
        self.assertEqual(core.format_duration_clock(0), "00:00")
        self.assertEqual(core.format_duration_clock(59.4), "00:59")
        self.assertEqual(core.format_duration_clock(60), "01:00")
        self.assertEqual(core.format_duration_clock(3661), "1:01:01")
        self.assertEqual(core.format_duration_clock(7120.3), "1:58:40")

        self.assertAlmostEqual(core.parse_speed_factor("2.67x"), 2.67, places=5)
        self.assertAlmostEqual(core.parse_speed_factor(" 0.95X "), 0.95, places=5)
        self.assertAlmostEqual(core.parse_speed_factor(4.2), 4.2, places=5)
        for bad in ("N/A", "", None, 0, -1.5, True, "nanx"):
            self.assertIsNone(core.parse_speed_factor(bad))

        # Both average elapsed rate (20/10=2.0) and instantaneous speed (2.5x) blend: 0.6*2.0 + 0.4*2.5 = 2.2
        eta_blend = core.estimate_eta_seconds(20.0, 130.0, 10.0, "2.5x")
        self.assertAlmostEqual(eta_blend, 110.0 / 2.2, places=4)
        # Early tick falls back to instantaneous speed when elapsed <= 0.25s.
        eta_inst = core.estimate_eta_seconds(0.5, 100.5, 0.1, "4.0x")
        self.assertAlmostEqual(eta_inst, 25.0, places=4)
        # Missing speed still computes from elapsed average rate.
        eta_avg = core.estimate_eta_seconds(50.0, 150.0, 20.0, "N/A")
        self.assertAlmostEqual(eta_avg, 40.0, places=4)
        self.assertEqual(core.estimate_eta_seconds(100.0, 100.0, 25.0, "4.0x"), 0.0)
        self.assertIsNone(core.estimate_eta_seconds(10.0, 0.0, 5.0, "2.0x"))

        detail_known = gui_app.format_progress_detail(5861.8, 7120.3, "2.67x", 2195, 471)
        self.assertIn("已处理 5861.8 / 7120.3 秒", detail_known)
        self.assertIn("2.67x", detail_known)
        self.assertIn("已用 36:35", detail_known)
        self.assertIn("预计剩余 07:51", detail_known)

        detail_wait = gui_app.format_progress_detail(1.0, 120.0, "", 0.2, None)
        self.assertIn("预计剩余 计算中…", detail_wait)

        detail_unknown_total = gui_app.format_progress_detail(12.3, 0.0, "1.5x", 8.2, None)
        self.assertIn("原片时长未提供", detail_unknown_total)
        self.assertIn("已用 00:08", detail_unknown_total)
        self.assertNotIn("预计剩余", detail_unknown_total)

        # Verify real convert() progress events include elapsed, eta_seconds, and percentage in [0, 100].
        source = self.make_source(width=256, height=144, name="计时事件.mkv")
        out = self.dir / "计时事件_HDR.mp4"
        events = []
        core.convert(core.Options(str(source), str(out), quality="fast", encoder="cpu"), callback=events.append)
        enc_updates = [e for e in events if e.get("stage") == "encoding" and "seconds" in e]
        self.assertTrue(enc_updates)
        for ev in enc_updates:
            self.assertIn("elapsed", ev)
            self.assertGreaterEqual(ev["elapsed"], 0.0)
            self.assertIn("eta_seconds", ev)
            if ev["eta_seconds"] is not None:
                self.assertGreaterEqual(ev["eta_seconds"], 0.0)
            self.assertGreaterEqual(ev["percent"], 0.0)
            self.assertLessEqual(ev["percent"], 100.0)
        done_events = [e for e in events if e.get("stage") == "done"]
        self.assertEqual(len(done_events), 1)
        self.assertIn("elapsed", done_events[0])

    def test_queue_toolbar_fits_window_and_long_summary(self):
        import app as gui_app
        import tkinter as tk

        root = tk.Tk()
        self.addCleanup(root.destroy)
        root.attributes("-alpha", 0)
        root.tk.call("tk", "scaling", 1.25 * 96 / 72)
        with patch.object(gui_app.App, "detect_encoders_async"):
            app = gui_app.App(root)
        for size in ("1275x900", "1050x720", "1920x1080", "1275x900"):
            root.geometry(size)
            for summary in ("暂无任务，可直接转换或点击“加入任务列表”排队执行",
                            "共 100 项 · 等待 20 · 完成 50 · 失败 30"):
                app.queue_summary.set(summary)
                root.update_idletasks()
                app.sync_layout()
                root.update_idletasks()
                header = app.queue_toolbar.master
                for button in app.queue_edit_buttons:
                    self.assertTrue(button.winfo_ismapped(), (size, button.cget("text")))
                    self.assertGreaterEqual(button.winfo_width(), button.winfo_reqwidth())
                    self.assertLessEqual(button.winfo_rootx() + button.winfo_width(),
                                         header.winfo_rootx() + header.winfo_width())

    def test_proportional_fullscreen_scaling_metrics(self):
        import app as gui_app

        # 1. Normal windowed on 125% DPI (1250x880 -> logical 1000x704): scale ~1.0
        win = gui_app.compute_proportional_scale(1250, 880, 1.25)
        self.assertGreaterEqual(win["scale"], 0.95)
        self.assertLessEqual(win["scale"], 1.05)
        self.assertEqual(win["body_font"], 10)

        # 2. Maximized 2560x1370 at 125% DPI scales up proportionally without shrinking
        max_qhd = gui_app.compute_proportional_scale(2560, 1370, 1.25)
        self.assertGreater(max_qhd["scale"], 1.45)
        self.assertGreater(max_qhd["body_font"], win["body_font"])
        self.assertGreater(max_qhd["title_font"], win["title_font"])
        self.assertGreater(max_qhd["tree_row_h"], win["tree_row_h"])

        # 3. Fullscreen 2560x1440 at 125% DPI scales >= maximized
        fs_qhd = gui_app.compute_proportional_scale(2560, 1440, 1.25)
        self.assertGreaterEqual(fs_qhd["scale"], max_qhd["scale"])

        # 4. Maximized 1920x1010 at 100% DPI scales up cleanly
        max_fhd = gui_app.compute_proportional_scale(1920, 1010, 1.0)
        self.assertGreater(max_fhd["scale"], 1.35)
        self.assertGreater(max_fhd["body_font"], 12)

        # 5. Small compact window stays readable (never drops below 0.90 floor)
        small = gui_app.compute_proportional_scale(820, 560, 1.0)
        self.assertGreaterEqual(small["scale"], 0.90)
        self.assertGreaterEqual(small["body_font"], 9)

    def test_multichannel_original_audio_passthrough_mp4_and_mkv(self):
        ffmpeg = core.find_tool("ffmpeg")

        def packet_hash(path: Path, track_idx: int) -> str:
            r = core.capture([ffmpeg, "-v", "error", "-i", str(path), "-map", f"0:a:{track_idx}",
                              "-c", "copy", "-f", "hash", "-hash", "sha256", "-"])
            self.assertEqual(r.returncode, 0, r.stderr)
            return r.stdout.strip()

        # 1. Dual-track 5.1 EAC3 + 5.1 AC3 MKV -> .mp4 (audio_effect="original"): both copied losslessly.
        src_multi = self.dir / "双轨51.mkv"
        r1 = core.capture([
            ffmpeg, "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc2=s=256x144:r=25:d=0.6",
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=5.1", "-t", "0.6",
            "-map", "0:v:0", "-map", "1:a:0", "-map", "1:a:0",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv",
            "-c:a:0", "eac3", "-c:a:1", "ac3", str(src_multi),
        ])
        self.assertEqual(r1.returncode, 0, r1.stderr)
        inspected_multi = core.inspect_input(src_multi)
        self.assertEqual(inspected_multi["audio"]["count"], 2)
        self.assertEqual(inspected_multi["audio"]["primary_channels"], 6)
        self.assertFalse(inspected_multi["audio"]["needs_mkv_for_lossless_audio"])

        out_multi_mp4 = self.dir / "双轨51_HDR.mp4"
        res_multi = core.convert(core.Options(str(src_multi), str(out_multi_mp4), quality="fast", encoder="cpu"))
        self.assertEqual(res_multi["plan"]["audio"], "copy")
        self.assertEqual(res_multi["plan"]["container"], "mp4")
        self.assertTrue(all(res_multi["validation"]["checks"].values()))
        out_audio_multi = [s for s in core.probe(out_multi_mp4)["streams"] if s["codec_type"] == "audio"]
        self.assertEqual(len(out_audio_multi), 2)
        self.assertEqual([(a["codec_name"], a["channels"]) for a in out_audio_multi], [("eac3", 6), ("ac3", 6)])
        self.assertEqual(packet_hash(src_multi, 0), packet_hash(out_multi_mp4, 0))
        self.assertEqual(packet_hash(src_multi, 1), packet_hash(out_multi_mp4, 1))

        # 2. 5.1 PCM MKV -> .mp4 (audio_effect="original"): channel-preserving 5.1 AAC fallback (NOT downmixed to 2ch).
        src_pcm = self.dir / "PCM51.mkv"
        r2 = core.capture([
            ffmpeg, "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc2=s=256x144:r=25:d=0.6",
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=5.1", "-t", "0.6",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv",
            "-c:a", "pcm_s16le", str(src_pcm),
        ])
        self.assertEqual(r2.returncode, 0, r2.stderr)
        inspected_pcm = core.inspect_input(src_pcm)
        self.assertTrue(inspected_pcm["audio"]["needs_mkv_for_lossless_audio"])

        out_pcm_mp4 = self.dir / "PCM51_HDR.mp4"
        res_pcm_mp4 = core.convert(core.Options(str(src_pcm), str(out_pcm_mp4), quality="fast", encoder="cpu"))
        self.assertEqual(res_pcm_mp4["plan"]["audio"], "aac")
        self.assertTrue(all(res_pcm_mp4["validation"]["checks"].values()))
        out_audio_pcm_mp4 = [s for s in core.probe(out_pcm_mp4)["streams"] if s["codec_type"] == "audio"]
        self.assertEqual(len(out_audio_pcm_mp4), 1)
        self.assertEqual(out_audio_pcm_mp4[0]["codec_name"], "aac")
        self.assertEqual(out_audio_pcm_mp4[0]["channels"], 6)

        # 3. 5.1 PCM MKV -> .mkv (audio_effect="original"): true lossless PCM 5.1 stream copy + HDR10 MKV checks pass.
        out_pcm_mkv = self.dir / "PCM51_HDR.mkv"
        res_pcm_mkv = core.convert(core.Options(str(src_pcm), str(out_pcm_mkv), quality="fast", encoder="cpu"))
        self.assertEqual(res_pcm_mkv["plan"]["audio"], "copy")
        self.assertEqual(res_pcm_mkv["plan"]["container"], "mkv")
        self.assertNotIn("hvc1_tag", res_pcm_mkv["validation"]["checks"])
        self.assertTrue(all(res_pcm_mkv["validation"]["checks"].values()))
        out_audio_pcm_mkv = [s for s in core.probe(out_pcm_mkv)["streams"] if s["codec_type"] == "audio"]
        self.assertEqual(len(out_audio_pcm_mkv), 1)
        self.assertEqual((out_audio_pcm_mkv[0]["codec_name"], out_audio_pcm_mkv[0]["channels"]), ("pcm_s16le", 6))
        self.assertEqual(packet_hash(src_pcm, 0), packet_hash(out_pcm_mkv, 0))

        # 4. Unsupported container suffix rejects cleanly.
        with self.assertRaisesRegex(core.ConversionError, r"\.mp4 或 \.mkv"):
            core.convert(core.Options(str(src_multi), str(self.dir / "无效容器.avi")))

    def test_selective_audio_subtitle_tracks_and_container_compatibility(self):
        import app as gui_app

        ffmpeg = core.find_tool("ffmpeg")
        srt_zh = self.dir / "zh.srt"
        srt_zh.write_text("1\n00:00:00,000 --> 00:00:00,500\n你好世界\n", encoding="utf-8")
        srt_en = self.dir / "en.srt"
        srt_en.write_text("1\n00:00:00,000 --> 00:00:00,500\nHello World\n", encoding="utf-8")

        src = self.dir / "多轨字幕.mkv"
        r = core.capture([
            ffmpeg, "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc2=s=256x144:r=25:d=0.6",
            "-f", "lavfi", "-t", "0.6", "-i", "anullsrc=r=48000:cl=5.1",
            "-f", "lavfi", "-t", "0.6", "-i", "anullsrc=r=48000:cl=stereo",
            "-i", str(srt_zh),
            "-i", str(srt_en),
            "-map", "0:v:0", "-map", "1:a:0", "-map", "2:a:0", "-map", "3:s:0", "-map", "4:s:0",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv",
            "-c:a:0", "eac3", "-metadata:s:a:0", "language=eng", "-metadata:s:a:0", "title=English 5.1",
            "-c:a:1", "ac3", "-metadata:s:a:1", "language=chi", "-metadata:s:a:1", "title=国语 2.0",
            "-c:s:0", "subrip", "-metadata:s:s:0", "language=chi", "-metadata:s:s:0", "title=简体中文",
            "-c:s:1", "ass", "-metadata:s:s:1", "language=eng", "-metadata:s:s:1", "title=English ASS",
            str(src),
        ])
        self.assertEqual(r.returncode, 0, r.stderr)

        inspected = core.inspect_input(src)
        self.assertEqual(inspected["default_container"], "mkv")
        self.assertEqual(len(inspected["audio_tracks"]), 2)
        self.assertEqual(len(inspected["subtitle_tracks"]), 2)
        a_second = inspected["audio_tracks"][1]["stream_index"]
        s_first = inspected["subtitle_tracks"][0]["stream_index"]

        def stream_hash(path: Path, spec: str) -> str:
            h = core.capture([ffmpeg, "-v", "error", "-i", str(path), "-map", spec,
                              "-c", "copy", "-f", "hash", "-hash", "sha256", "-"])
            self.assertEqual(h.returncode, 0, h.stderr)
            return h.stdout.strip()

        # 1. Select ONLY second audio track (chi 2.0 AC3) + ONLY first subtitle (chi subrip) -> .mkv
        out_sel_mkv = self.dir / "精选轨道.mkv"
        res_sel = core.convert(core.Options(
            str(src), str(out_sel_mkv), quality="fast", encoder="cpu",
            audio_streams=[a_second], subtitle_streams=[s_first],
        ))
        self.assertEqual(res_sel["plan"]["audio"], "copy")
        self.assertEqual(res_sel["plan"]["subtitles"], "copy")
        self.assertTrue(all(res_sel["validation"]["checks"].values()))
        probed_mkv = core.probe(out_sel_mkv)
        out_a = [s for s in probed_mkv["streams"] if s["codec_type"] == "audio"]
        out_s = [s for s in probed_mkv["streams"] if s["codec_type"] == "subtitle"]
        self.assertEqual(len(out_a), 1)
        self.assertEqual((out_a[0]["codec_name"], out_a[0]["channels"]), ("ac3", 2))
        self.assertEqual((out_a[0].get("tags") or {}).get("language"), "chi")
        self.assertEqual((out_a[0].get("tags") or {}).get("title"), "国语 2.0")
        self.assertEqual(len(out_s), 1)
        self.assertEqual(out_s[0]["codec_name"], "subrip")
        self.assertEqual((out_s[0].get("tags") or {}).get("language"), "chi")
        self.assertEqual((out_s[0].get("tags") or {}).get("title"), "简体中文")
        self.assertEqual(stream_hash(src, "0:a:1"), stream_hash(out_sel_mkv, "0:a:0"))
        self.assertEqual(stream_hash(src, "0:s:0"), stream_hash(out_sel_mkv, "0:s:0"))

        # 2. Drop all audio tracks ([]) while keeping Chinese subtitle into .mp4 (converts subrip -> mov_text)
        out_mp4_sub = self.dir / "静音带字幕.mp4"
        res_mp4 = core.convert(core.Options(
            str(src), str(out_mp4_sub), quality="fast", encoder="cpu",
            audio_streams=[], subtitle_streams=[s_first],
        ))
        self.assertEqual(res_mp4["plan"]["audio"], "none")
        self.assertEqual(res_mp4["plan"]["subtitles"], "mov_text")
        self.assertTrue(all(res_mp4["validation"]["checks"].values()))
        probed_mp4 = core.probe(out_mp4_sub)
        self.assertEqual([s for s in probed_mp4["streams"] if s["codec_type"] == "audio"], [])
        mp4_subs = [s for s in probed_mp4["streams"] if s["codec_type"] == "subtitle"]
        self.assertEqual(len(mp4_subs), 1)
        self.assertEqual(mp4_subs[0]["codec_name"], "mov_text")
        self.assertEqual((mp4_subs[0].get("tags") or {}).get("language"), "chi")

        # 3. Verify compatibility checker on synthetic TrueHD + PGS Blu-ray info
        bluray_info = {
            "streams": [
                {"index": 0, "codec_type": "video", "codec_name": "hevc"},
                {"index": 1, "codec_type": "audio", "codec_name": "truehd", "channels": 8, "sample_rate": "48000", "tags": {"language": "eng"}},
                {"index": 2, "codec_type": "audio", "codec_name": "eac3", "channels": 6, "sample_rate": "48000", "tags": {"language": "chi"}},
                {"index": 3, "codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle", "tags": {"language": "chi"}},
                {"index": 4, "codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "eng"}},
            ]
        }
        c_mp4_all = core.check_container_compatibility(bluray_info, "mp4")
        self.assertTrue(c_mp4_all["needs_warning"])
        self.assertEqual([t["stream_index"] for t in c_mp4_all["incompatible_audio"]], [1])
        self.assertEqual([t["stream_index"] for t in c_mp4_all["incompatible_subtitles"]], [3])

        c_mp4_compat = core.check_container_compatibility(bluray_info, "mp4", audio_streams=[2], subtitle_streams=[4])
        self.assertFalse(c_mp4_compat["needs_warning"])

        c_mkv_all = core.check_container_compatibility(bluray_info, "mkv")
        self.assertFalse(c_mkv_all["needs_warning"])

        # 4. Verify GUI summary formatting helper
        summary = gui_app.format_track_summary(
            inspected["audio_tracks"], [a_second], inspected["subtitle_tracks"], [s_first], "original"
        )
        self.assertIn("已选 1/2 条", summary)
        self.assertIn("内嵌字幕 已选 1/2 条", summary)

    def test_pause_resume_and_cancel_while_paused(self):
        import time

        ffmpeg = core.find_tool("ffmpeg")
        source = self.dir / "暂停测试源.mp4"
        r = core.capture([
            ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=480x270:r=25:d=6",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=6",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv",
            "-c:a", "aac", "-ar", "48000", "-ac", "2", str(source),
        ])
        self.assertEqual(r.returncode, 0, r.stderr)

        # 1. Pause mid-encode for ~0.8s, then resume and verify clean HDR10 completion + active elapsed excludes pause.
        out_resume = self.dir / "暂停后继续_HDR.mp4"
        pause_ev = threading.Event()
        events = []
        triggered = [False]

        def on_prog(ev):
            events.append(ev)
            if ev.get("stage") == "encoding" and ev.get("seconds", 0) >= 0.3 and not triggered[0]:
                triggered[0] = True
                pause_ev.set()
                threading.Timer(0.8, pause_ev.clear).start()

        wall_start = time.monotonic()
        res = core.convert(
            core.Options(str(source), str(out_resume), quality="standard", encoder="cpu"),
            callback=on_prog,
            pause=pause_ev,
        )
        wall_elapsed = time.monotonic() - wall_start
        self.assertTrue(all(res["validation"]["checks"].values()))
        self.assertTrue(any(e.get("stage") == "paused" for e in events))
        self.assertTrue(any(e.get("stage") == "resumed" for e in events))
        self.assertLess(res["elapsed_seconds"], wall_elapsed - 0.35)

        # 2. Cancel WHILE paused must automatically resume & terminate cleanly without hanging or leaving partials.
        out_cancel = self.dir / "暂停时取消_HDR.mp4"
        pause_ev2 = threading.Event()
        cancel_ev2 = threading.Event()
        paused_once = [False]

        def on_prog2(ev):
            if ev.get("stage") == "encoding" and not paused_once[0]:
                paused_once[0] = True
                pause_ev2.set()
            elif ev.get("stage") == "paused":
                cancel_ev2.set()

        with self.assertRaises(core.Cancelled):
            core.convert(
                core.Options(str(source), str(out_cancel), quality="standard", encoder="cpu"),
                callback=on_prog2,
                cancel=cancel_ev2,
                pause=pause_ev2,
            )
        self.assertFalse(out_cancel.exists())
        self.assertEqual(list(self.dir.glob("*.part.mp4")), [])

    def test_keep_partial_skips_lossless_verification_and_publishes_full_file(self):
        source = self.make_source(width=256, height=144, name="无损跳过校验源.mkv")
        output = self.dir / "无损跳过校验_HDR.mp4"
        keep_partial = threading.Event()

        def on_prog(ev):
            if ev.get("stage") == "verifying" and ev.get("percent") == 99.1:
                keep_partial.set()

        result = core.convert(
            core.Options(str(source), str(output), quality="lossless", encoder="auto"),
            callback=on_prog,
            keep_partial=keep_partial,
        )
        self.assertTrue(output.is_file())
        self.assertEqual(Path(result["output"]), output)
        self.assertFalse(result["partial"])
        self.assertTrue(result["skipped_lossless_verify"])
        self.assertTrue(result["validation"]["lossless_pixels"]["skipped_by_user"])
        self.assertTrue(all(result["validation"]["checks"].values()))

    def test_custom_preview_timecode_queue_helpers_and_power_actions(self):
        import app as gui_app

        # Timecode parser
        self.assertAlmostEqual(core.parse_timecode_seconds("90"), 90.0)
        self.assertAlmostEqual(core.parse_timecode_seconds("01:30"), 90.0)
        self.assertAlmostEqual(core.parse_timecode_seconds("1:02:03.5"), 3723.5)
        for bad in ("", "-5", "1:70", "1:2:3:4", "abc"):
            with self.assertRaises(ValueError):
                core.parse_timecode_seconds(bad)

        # Default preview window centers on middle of video
        st, du = core.default_preview_window(600.0, 30.0)
        self.assertEqual((st, du), (285.0, 30.0))
        self.assertEqual(core.default_preview_window(12.0, 30.0), (0.0, 12.0))
        self.assertEqual(core.default_preview_window(0.0, 30.0), (0.0, 30.0))

        # Queue path collision avoidance across both disk and reserved queue entries
        sample = self.dir / "movie.mkv"
        sample.write_bytes(b"x")
        first = gui_app.next_unique_output_path(sample, "_HDR.mkv")
        self.assertTrue(first.endswith("movie_HDR.mkv"))
        second = gui_app.next_unique_output_path(sample, "_HDR.mkv", reserved={first})
        self.assertTrue(second.endswith("movie_HDR_2.mkv"))
        summary = gui_app.format_queue_task_summary(first, "balanced", "standard", "original", "original", [1], [2])
        self.assertIn("MKV", summary)
        self.assertIn("影院1000nit", summary)
        self.assertIn("音轨1", summary)
        self.assertIn("字幕1", summary)

        # Power action dispatch (mocked, never executes real OS shutdown/sleep in tests)
        core.trigger_power_action("none")
        with self.assertRaises(core.ConversionError):
            core.trigger_power_action("reboot_invalid")
        with patch.object(core.subprocess, "Popen") as mock_popen:
            core.trigger_power_action("shutdown")
            mock_popen.assert_called_once()
            self.assertIn("shutdown", mock_popen.call_args[0][0][0])


if __name__ == "__main__":
    unittest.main(verbosity=2)

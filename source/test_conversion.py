"""Integration checks: real FFmpeg pixels compared with an analytical formula.

Run: python -m unittest discover -s source -p test_conversion.py -v
Requires FFmpeg with zscale/lut1d/libx265. No third-party Python packages needed.
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

        def fake_detected(nvenc=False, nvenc_lossless=False, qsv=False, amf=False, cpu=True):
            return {
                "cpu": {**core.ENCODER_SPECS["cpu"], "key": "cpu", "available": cpu, "lossless": cpu, "reason": ""},
                "nvenc": {**core.ENCODER_SPECS["nvenc"], "key": "nvenc", "available": nvenc, "lossless": nvenc_lossless, "reason": "" if nvenc else "no nvenc"},
                "qsv": {**core.ENCODER_SPECS["qsv"], "key": "qsv", "available": qsv, "lossless": False, "reason": "" if qsv else "no qsv"},
                "amf": {**core.ENCODER_SPECS["amf"], "key": "amf", "available": amf, "lossless": False, "reason": "" if amf else "no amf"},
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

        # Verify metadata placement before -i, filter chain ending, and codec-specific flags.
        for enc_key, expected_flags in [
            ("nvenc", ["-c:v", "hevc_nvenc", "-rc", "vbr", "-cq"]),
            ("qsv", ["-c:v", "hevc_qsv", "-global_quality"]),
            ("amf", ["-c:v", "hevc_amf", "-bitdepth", "10", "-rc", "cqp"]),
        ]:
            cmd, plan = core.build_command(core.Options("in.mp4", str(dest), encoder=enc_key, quality="standard"), fake_info, dest, detected_encoders=all_hw)
            self.assertEqual(plan["encoder"], enc_key)
            input_pos = cmd.index("-i")
            md_pos = cmd.index("-mastering_display:1")
            cl_pos = cmd.index("-content_light:1")
            self.assertLess(md_pos, input_pos)
            self.assertLess(cl_pos, input_pos)
            vf = cmd[cmd.index("-vf") + 1]
            self.assertTrue(vf.endswith("format=yuv420p10le,setsar=1,format=p010le"))
            for flag in expected_flags:
                self.assertIn(flag, cmd)

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
        self.assertTrue(all(res_fine["validation"]["checks"].values()))

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


if __name__ == "__main__":
    unittest.main(verbosity=2)

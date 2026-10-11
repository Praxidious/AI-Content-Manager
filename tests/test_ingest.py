from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from unittest.mock import patch

import process_stream
from src.config import AppConfig, load_config
from src.ingest import (Recording, Registry, discover, inspect_recordings, read_guard,
                        registry_lock, resolve_path, select_video, video_fingerprint)


class IngestionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.registry = Registry(self.root / "output" / "vod_registry.json")
        self.sleep = patch("src.ingest.time.sleep")
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    def video(self, name="vod.mkv", age=100, content=b"video"):
        path = self.source / name
        path.write_bytes(content)
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
        return path

    def inspect(self):
        return inspect_recordings(discover(self.source), self.registry, 1, "full:analysis")

    def select(self, requested=None, force=False):
        return select_video(requested, self.source, self.root, self.registry, 1, "full:analysis", force)

    def test_local_relative_and_absolute_paths(self):
        self.assertEqual(resolve_path("source", self.root), self.source)
        self.assertEqual(resolve_path(self.source, self.root), self.source)

    def test_unc_and_mapped_drive_are_not_prefixed(self):
        with patch("src.ingest.Path", PureWindowsPath):
            for value in (r"\\STREAM-PC\Stream VODs", r"Z:\Recordings"):
                self.assertEqual(resolve_path(value, PureWindowsPath(r"C:\bot")), PureWindowsPath(value))

    def test_config_unc_json_and_defaults(self):
        path = self.root / "config.json"
        path.write_text(json.dumps({"paths": {"vod_source": r"\\STREAM-PC\Stream VODs"}}))
        cfg = load_config(path)
        self.assertEqual(cfg.paths.vod_source, r"\\STREAM-PC\Stream VODs")
        self.assertEqual(cfg.paths.output_directory, "output")
        path.write_text("{}")
        self.assertEqual(load_config(path).paths.vod_source, "input")

    def test_invalid_intervals(self):
        path = self.root / "config.json"
        for value in (0, -1, "30", None, True, float("inf")):
            path.write_text(json.dumps({"paths": {"stability_seconds": value}}))
            with self.assertRaisesRegex(ValueError, "stability_seconds"):
                load_config(path)

    def test_newest_first_and_supported_extensions(self):
        paths = [self.video(f"vod{ext}", age=age) for ext, age in
                 ((".mkv", 100), (".MP4", 90), (".mov", 80), (".webm", 70))]
        self.video("notes.txt")
        self.video("vod.mp4.part")
        (self.source / "folder.mp4").mkdir()
        self.assertEqual([x.path for x in discover(self.source)], list(reversed(paths)))

    def test_finished_file_is_ready(self):
        path = self.video()
        result = self.inspect()[0]
        self.assertEqual(result.status, "READY")
        self.assertEqual(result.fingerprint, video_fingerprint(path))

    def test_growing_file_skipped_for_older_finished_file(self):
        old = self.video("old.mkv", 200)
        newest = self.video("new.mkv", 100)
        with patch("src.ingest.time.sleep", side_effect=lambda _: newest.write_bytes(b"growing")):
            self.assertEqual(self.select().path, old)

    def test_mtime_only_change_is_unstable(self):
        path = self.video()
        with patch("src.ingest.time.sleep", side_effect=lambda _: os.utime(path, None)):
            self.assertEqual(self.inspect()[0].status, "RECORDING/UNSTABLE")

    def test_disappearing_file_is_unstable(self):
        path = self.video()
        with patch("src.ingest.time.sleep", side_effect=lambda _: path.unlink()):
            self.assertEqual(self.inspect()[0].status, "RECORDING/UNSTABLE")

    def test_empty_recent_and_temporary_files_are_unstable(self):
        self.video("empty.mkv", content=b"")
        self.video("recent.mp4", age=0)
        self.video("vod.remux.mp4")
        self.assertTrue(all(x.status == "RECORDING/UNSTABLE" for x in self.inspect()))

    def test_permission_failure_is_reported(self):
        self.video()
        with patch("src.ingest.read_guard", side_effect=PermissionError("read denied")):
            item = self.inspect()[0]
        self.assertEqual(item.status, "RECORDING/UNSTABLE")
        self.assertIn("denied", item.reason)

    def test_inaccessible_directory(self):
        with patch.object(Path, "iterdir", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(RuntimeError, "Cannot access VOD source"):
                discover(self.source)

    def test_completed_excluded_even_after_rename(self):
        path = self.video()
        item = self.select()
        with self.registry.job(item, self.root / "output", "full:analysis"):
            self.assertEqual(Registry(self.registry.path).status(item.fingerprint, "full:analysis"), "processing")
        path.rename(self.source / "renamed.mkv")
        self.assertEqual(self.inspect()[0].status, "PROCESSED")
        with self.assertRaisesRegex(RuntimeError, "No eligible"):
            self.select()
        self.assertEqual(self.select(force=True).status, "PROCESSED")

    def test_same_filename_replaced_is_new_recording(self):
        self.video()
        item = self.select()
        with self.registry.job(item, self.root / "output", "full:analysis"):
            pass
        self.video(content=b"replacement source")
        self.assertEqual(self.select().status, "READY")

    def test_failure_and_keyboard_interrupt_retryable(self):
        self.video()
        item = self.select()
        for error in (RuntimeError("failed"), KeyboardInterrupt()):
            with self.assertRaises(type(error)):
                with self.registry.job(item, self.root / "output", "full:analysis"):
                    raise error
            self.registry = Registry(self.registry.path)
            self.assertEqual(self.select().status, "FAILED")

    def test_crashed_processing_entry_retryable(self):
        self.video()
        item = self.select()
        self.registry.entries[f"{item.fingerprint}:full:analysis"] = {"status": "processing"}
        self.registry.save()
        self.registry = Registry(self.registry.path)
        self.assertEqual(self.select().status, "FAILED")

    def test_short_and_transcript_jobs_do_not_complete_full_analysis(self):
        self.video()
        item = self.select()
        for scope in ("test_10m:analysis", "full:transcript"):
            with self.registry.job(item, self.root / "output", scope):
                pass
        self.assertEqual(self.select().status, "READY")

    def test_explicit_path_overrides_discovery(self):
        old = self.video("old.mkv", 200)
        self.video("new.mkv", 100)
        with patch("src.ingest.discover", side_effect=AssertionError("must not scan")):
            self.assertEqual(self.select(old).path, old)

    def test_force_does_not_bypass_stability(self):
        self.video(content=b"")
        with self.assertRaisesRegex(RuntimeError, "No eligible"):
            self.select(force=True)

    def test_corrupt_registry_not_silently_reset(self):
        self.registry.path.parent.mkdir()
        self.registry.path.write_text("[]")
        with self.assertRaisesRegex(RuntimeError, "Invalid VOD registry"):
            Registry(self.registry.path)

    def test_registry_lock_prevents_concurrent_runs_and_releases(self):
        with registry_lock(self.root / "output"):
            with self.assertRaisesRegex(RuntimeError, "Another ingestion"):
                with registry_lock(self.root / "output"):
                    pass
        with registry_lock(self.root / "output"):
            pass

    @unittest.skipUnless(os.name == "nt", "Windows file sharing semantics")
    def test_windows_writer_blocks_guard_and_guard_blocks_writer(self):
        path = self.video()
        with path.open("ab"):
            with self.assertRaises(OSError):
                with read_guard(path):
                    pass
        with read_guard(path):
            self.assertEqual(path.read_bytes(), b"video")
            with self.assertRaises(OSError):
                with path.open("ab"):
                    pass

    def args(self, **overrides):
        defaults = dict(video=None, source=None, list_vods=False, force_video=False, resume_output=None,
                        word_timestamps=False, test_minutes=None, retranscribe=False, skip_analysis=False,
                        reanalyze=False, rediscover=False, reverify=False, rejudge=False)
        return SimpleNamespace(**(defaults | overrides))

    def pipeline(self, args, fail=False):
        cfg = AppConfig()
        cfg.paths.vod_source = str(self.source)
        cfg.paths.output_directory = str(self.root / "output")
        cfg.paths.stability_seconds = 1
        cfg.audio.analysis_enabled = False
        with ExitStack() as mocks:
            mocks.enter_context(patch.object(process_stream, "configure_logging"))
            mocks.enter_context(patch.object(process_stream, "require_ffmpeg"))
            mocks.enter_context(patch.object(process_stream, "choose_hardware", return_value=SimpleNamespace(
                gpu_name="test", device="cpu", compute_type="int8", warning=None)))
            extract = mocks.enter_context(patch.object(process_stream, "extract_audio", return_value=600))
            mocks.enter_context(patch.object(process_stream, "transcribe_audio", return_value={"segments": [], "metadata": {}}))
            mocks.enter_context(patch.object(process_stream, "analyze_transcript", side_effect=RuntimeError("analysis failed") if fail else None,
                                            return_value=SimpleNamespace(candidates=[], rejected=[], chunk_count=0, discovered_count=0, verified_count=0)))
            with ExitStack() as stack:
                process_stream.run_pipeline(args, cfg, stack)
            return extract

    def test_pipeline_completion_failure_and_retry(self):
        path = self.video()
        fingerprint = video_fingerprint(path)
        with self.assertRaisesRegex(RuntimeError, "analysis failed"):
            self.pipeline(self.args(), fail=True)
        self.assertEqual(Registry(self.registry.path).status(fingerprint, "full:analysis"), "failed")
        extract = self.pipeline(self.args())
        self.assertTrue(extract.call_args.kwargs["overwrite"])
        self.assertEqual(Registry(self.registry.path).status(fingerprint, "full:analysis"), "completed")

    def test_list_does_not_start_processing_or_create_registry(self):
        self.video()
        self.assertFalse(self.pipeline(self.args(list_vods=True)).called)
        self.assertFalse(self.registry.path.exists())

    def test_resume_without_source(self):
        saved = self.root / "saved"
        saved.mkdir()
        (saved / "transcript.json").write_text('{"segments": [], "metadata": {"duration": 600}}')
        with patch("src.ingest.discover", side_effect=AssertionError("must not scan")):
            self.assertFalse(self.pipeline(self.args(resume_output=saved)).called)

    def test_successful_resume_updates_failed_registry_without_vod(self):
        path = self.video()
        fingerprint = video_fingerprint(path)
        with self.assertRaises(RuntimeError):
            self.pipeline(self.args(), fail=True)
        entry = Registry(self.registry.path).entries[f"{fingerprint}:full:analysis"]
        saved = Path(entry["output_directory"])
        (saved / "transcript.json").write_text('{"segments": [], "metadata": {"duration": 600}}')
        path.unlink()
        self.pipeline(self.args(resume_output=saved))
        self.assertEqual(Registry(self.registry.path).status(fingerprint, "full:analysis"), "completed")


if __name__ == "__main__":
    unittest.main()

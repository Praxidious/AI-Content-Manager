import io
import unittest
from pathlib import Path
from unittest.mock import patch

import start_contentbot as launcher
from src.ingest import Recording


class StartMenuTests(unittest.TestCase):
    def run_menu(self, answers):
        with patch("builtins.input", side_effect=answers), patch("sys.stdout", new_callable=io.StringIO):
            return launcher.main()

    def test_website_does_not_start_processing(self):
        with patch.object(launcher, "run_batch", return_value=0) as batch, patch.object(launcher, "process") as process:
            self.assertEqual(self.run_menu(["1", "", "0"]), 0)
            batch.assert_called_once_with("OPEN_TRAINER.bat")
            process.assert_not_called()

    def test_batch_and_invalid_input(self):
        with patch.object(launcher, "process", return_value=0) as process:
            self.run_menu(["bad", "7", "2", "", "0"])
            process.assert_called_once_with(["--all-vods"])

    def test_single_selection_and_feedback_rescan(self):
        video = Path("C:/VODs/Friday & friends 100%.mkv")
        with patch.object(launcher, "pick_vod", return_value=video), patch.object(launcher, "process", return_value=0) as process:
            self.run_menu(["3", "2", "", "0"])
            process.assert_called_once_with([str(video), "--force-video", "--rediscover"])

    def test_completed_vod_opens_only_its_saved_clips(self):
        with patch.object(launcher, "load_config"), \
             patch.object(launcher, "resolve_path", return_value=Path("output")), \
             patch.object(launcher, "video_fingerprint", return_value="chosen"), \
             patch.object(launcher, "Registry") as registry, \
             patch.object(launcher, "open_review") as review, \
             patch("sys.stdout", new_callable=io.StringIO):
            registry.return_value.status.return_value = "completed"
            self.assertTrue(launcher.open_completed_vod(Path("chosen.mkv")))
            review.assert_called_once_with(launcher.ROOT/"config.json", Path("output"), source_key="chosen")
            registry.return_value.status.return_value = None
            self.assertFalse(launcher.open_completed_vod(Path("chosen.mkv")))
            self.assertEqual(review.call_count, 1)

    def test_picker_names_and_back(self):
        recordings = [Recording(Path("new.mkv")), Recording(Path("old.mp4"))]
        with patch.object(launcher, "load_config") , patch.object(launcher, "resolve_path", return_value=Path("input")), patch.object(launcher, "discover", return_value=recordings), patch("builtins.input", side_effect=["2", "0"]), patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(launcher.pick_vod(), Path("old.mp4"))
            self.assertIsNone(launcher.pick_vod())
            self.assertIn("1. new.mkv", output.getvalue())
            self.assertIn("2. old.mp4", output.getvalue())

    def test_setup_failure_does_not_start_pipeline(self):
        with patch.object(launcher, "run_batch", return_value=1), patch.object(launcher.subprocess, "run") as run:
            self.assertEqual(launcher.process(["--all-vods"]), 1)
            run.assert_not_called()

    def test_video_path_passed_without_shell(self):
        video = "C:/VODs/Friday & friends 100%.mkv"
        with patch.object(launcher, "run_batch", return_value=0), patch.object(launcher.subprocess, "run") as run:
            launcher.process([video])
            self.assertEqual(run.call_args.args[0][-1], video)
            self.assertNotIn("shell", run.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()

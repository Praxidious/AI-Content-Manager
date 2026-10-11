import tempfile
import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.review_launcher import open_review
import process_stream
import review_clips
from src.config import AppConfig
from src.ingest import Recording


class ReviewLauncherTests(unittest.TestCase):
    def test_rerun_feedback_reuses_latest_full_transcript(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name,minutes in [('full',None),('test',10)]:
                folder=root/name;folder.mkdir()
                (folder/'transcript.json').write_text('{}')
                (folder/'source.json').write_text(json.dumps({'test_minutes':minutes}))
            self.assertEqual(process_stream.latest_saved_vod(root),root/'full')
            cfg=AppConfig();cfg.paths.output_directory=str(root)
            def run(args,config,stack):
                self.assertEqual(args.resume_output,root/'full')
                self.assertTrue(args.rediscover)
                self.assertFalse(args.retranscribe)
                args.review_output=root
                return 0
            with patch('sys.argv',['process_stream.py','--rerun-feedback']), \
                 patch.object(process_stream,'load_config',return_value=cfg), \
                 patch.object(process_stream,'configure_logging'), \
                 patch.object(process_stream,'run_pipeline',side_effect=run), \
                 patch.object(process_stream,'open_review'):
                self.assertEqual(process_stream.main(),0)

    def test_batch_skips_processed_unstable_and_continues_after_failure(self):
        recordings=[Recording(Path(name),status=status) for name,status in
                    [('done','PROCESSED'),('live','RECORDING/UNSTABLE'),('failed','FAILED'),('ready','READY')]]
        with patch('sys.argv',['process_stream.py','--all-vods']), \
             patch.object(process_stream,'configure_logging'), \
             patch.object(process_stream,'discover',return_value=[]), \
             patch.object(process_stream,'inspect_recordings',return_value=recordings), \
             patch.object(process_stream,'Registry'), \
             patch.object(process_stream,'run_pipeline',side_effect=[RuntimeError('fixture failure'),0]) as run, \
             patch.object(process_stream,'open_review') as launch:
            self.assertEqual(process_stream.main(),1)
            self.assertEqual([c.args[0].video for c in run.call_args_list],[Path('failed'),Path('ready')])
            launch.assert_called_once()

    def test_review_cli_open_browser_mode(self):
        config = AppConfig()
        with patch.object(review_clips, 'load_config', return_value=config), \
             patch.object(review_clips, 'open_review') as launch, \
             patch.object(review_clips, 'FeedbackStore') as store:
            review_clips.main(['--open-browser'])
            launch.assert_called_once()
            store.assert_not_called()

    def test_review_cli_rejects_open_and_export_together(self):
        with self.assertRaisesRegex(ValueError, 'cannot be combined'):
            review_clips.main(['--open-browser', '--export-feedback', 'feedback.jsonl'])

    def test_existing_server_reused(self):
        with patch('src.review_launcher.server_matches',return_value=True), \
             patch('src.review_launcher.subprocess.Popen') as spawn, \
             patch('src.review_launcher.webbrowser.open',return_value=True) as browser:
            open_review(Path('config.json'),Path('output'))
            spawn.assert_not_called()
            browser.assert_called_once_with('http://localhost:5050')

    def test_selected_source_url_is_encoded_and_server_reused(self):
        from urllib.parse import urlparse, parse_qs
        with patch('src.review_launcher.server_matches',return_value=True), \
             patch('src.review_launcher.subprocess.Popen') as spawn, \
             patch('src.review_launcher.webbrowser.open',return_value=True) as browser:
            source = 'C:/VODs/Friday & friends #1.mkv'
            open_review(Path('config.json'), Path('output'), source_key=source)
            spawn.assert_not_called()
            self.assertEqual(parse_qs(urlparse(browser.call_args.args[0]).query), {'source':[source]})

    def test_pipeline_passes_selected_source_to_trainer(self):
        def run(args, cfg, stack):
            args.review_output = Path('output')
            args.review_source = 'selected-fingerprint'
            return 0
        with patch('sys.argv',['process_stream.py','chosen.mkv']), \
             patch.object(process_stream,'configure_logging'), \
             patch.object(process_stream,'run_pipeline',side_effect=run), \
             patch.object(process_stream,'open_review') as launch:
            self.assertEqual(process_stream.main(), 0)
            launch.assert_called_once_with(process_stream.PROJECT_ROOT/'config.json', Path('output'), source_key='selected-fingerprint')

    def test_server_started_before_browser(self):
        with patch('src.review_launcher.server_matches',side_effect=[False,False,True]), \
             patch('src.review_launcher.subprocess.Popen',return_value=Mock(poll=Mock(return_value=None))) as spawn, \
             patch('src.review_launcher.Path.open'), \
             patch('src.review_launcher.time.sleep'), \
             patch('src.review_launcher.webbrowser.open',return_value=True) as browser:
            open_review(Path('config.json'),Path('output'))
            self.assertIn('review_clips.py',spawn.call_args.args[0][1])
            browser.assert_called_once()

    def test_failed_server_does_not_open_browser(self):
        with patch('src.review_launcher.server_matches',return_value=False), \
             patch('src.review_launcher.subprocess.Popen',return_value=Mock(poll=Mock(return_value=1))), \
             patch('src.review_launcher.Path.open'), \
             patch('src.review_launcher.webbrowser.open') as browser:
            with self.assertRaisesRegex(RuntimeError,'could not start'):
                open_review(Path('config.json'),Path('output'))
            browser.assert_not_called()

    def test_pipeline_opens_only_after_successful_analysis(self):
        with tempfile.TemporaryDirectory() as directory:
            config=Path(directory)/'config.json';config.write_text('{}')
            for flags,opens in (([],True),(['--list-vods'],False),(['--skip-analysis'],False)):
                def run(args,cfg,stack):
                    args.review_output=Path(directory)
                    return 0
                with patch('sys.argv',['process_stream.py','--config',str(config),*flags]), \
                     patch.object(process_stream,'configure_logging'), \
                     patch.object(process_stream,'run_pipeline',side_effect=run), \
                     patch.object(process_stream,'open_review') as launch:
                    self.assertEqual(process_stream.main(),0)
                    self.assertEqual(launch.called,opens)
            with patch('sys.argv',['process_stream.py','--config',str(config)]), \
                 patch.object(process_stream,'configure_logging'), \
                 patch.object(process_stream,'run_pipeline',side_effect=RuntimeError('failed')), \
                 patch.object(process_stream,'open_review') as launch:
                with self.assertRaises(RuntimeError):process_stream.main()
                launch.assert_not_called()

import json
import tempfile
import unittest
import wave
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import numpy as np

from src.audio_events import (Yamnet, analyze_events, choose_track, detect_events, event_candidates,
                              attach_events, prepare_audio, RATE, HOP)
from src.config import AudioEventConfig, AnalysisConfig, AppConfig, validate_audio_events
from src.analyze import analyze_transcript, deduplicate_candidates, _bounds
from src.feedback import FeedbackStore
from tests.test_pipeline import FakeProvider, segment


MIC = {"source": "microphone", "label": "Mic Track 2"}
MIX = {"source": "mixed", "label": "Mixed stream"}


def frame(index, labels=None, rms=.01, background=0):
    return dict(start=round(index*HOP, 3), end=round(index*HOP+.96, 3), rms=rms, peak=rms*2,
                reaction_scores=labels or {}, vocal_activity_confidence=max((labels or {}).values(), default=.8),
                background_confidence=background, top_classes=[])


def write_wav(path, samples):
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(RATE)
        wav.writeframes(np.asarray(samples, dtype="<i2").tobytes())


class StubClassifier:
    labels = ["Speech", "Screaming", "Laughter", "Explosion"]
    fingerprint = "fixture-model-v1"
    device = "CPU fixture"

    def __init__(self, fail_on=None):
        self.calls = 0
        self.fail_on = fail_on

    def predict(self, samples):
        self.calls += 1
        if self.calls == self.fail_on:
            raise RuntimeError("interrupted chunk")
        rows = max(1, int(len(samples)/7680))
        values = np.zeros((rows, 4), dtype=np.float32)
        values[:, 0] = .9
        for i in range(rows):
            block = samples[i*7680:i*7680+15360]
            if len(block) and np.max(np.abs(block)) > .5:
                values[i, 1] = .95
        return values


class AudioEventTests(unittest.TestCase):
    def setUp(self):
        self.config = AudioEventConfig()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def scream(self, at=20):
        return detect_events([frame(i, {"Screaming": .94} if i in (at, at+1) else {}) for i in range(50)], self.config, MIC)

    def test_a_normal_conversation_is_not_a_reaction(self):
        frames = [frame(i, {"Laughter": .05, "Screaming": .01}, rms=.1 if i%2 else .01) for i in range(100)]
        self.assertEqual(detect_events(frames, self.config, MIX), [])

    def test_b_scream_without_words_survives_full_candidate_pipeline(self):
        events = self.scream()
        seeds = event_candidates(events, [], self.config, 100, 24)
        self.assertEqual(len(seeds), 1)
        self.assertTrue(seeds[0]["audio_only"])
        self.assertEqual(seeds[0]["exact_transcript_evidence"], "")
        self.assertAlmostEqual(seeds[0]["recommended_start"], events[0]["start"]-7)
        self.assertAlmostEqual(seeds[0]["recommended_end"], events[0]["end"]+12)
        class Provider(FakeProvider):
            def analyze(self, prompt, text, schema):
                data = json.loads(super().analyze(prompt, text, schema))
                if "tier" in data:
                    data.update(recommended_start=3, recommended_end=24)
                return json.dumps(data)
        store = FeedbackStore(self.root / "feedback.sqlite3")
        outcome = analyze_transcript([], AnalysisConfig(), Path(__file__).resolve().parents[1]/"prompts",
            self.root/"cache", provider=Provider(), audio_events=events, audio_events_available=True,
            audio_event_config=self.config, stream_duration=100, feedback_store=store, source={"fingerprint":"fixture"})
        self.assertEqual(outcome.chunk_count, 0)
        self.assertEqual(len(outcome.candidates), 1)
        c = outcome.candidates[0]
        self.assertTrue(c["contains_scream"])
        self.assertEqual(c["final_rank_score"], 8.5)  # Existing generic+preference+guarded audience formula.
        self.assertEqual(c["transcript_excerpt"], [])
        self.assertEqual(store.get(c["clip_id"])["metadata"]["audio_events"], events)
        store.rate(c["clip_id"], "good")
        self.assertEqual(store.get(c["clip_id"])["features"]["audio_event_types"], ["scream"])

    def test_c_dialogue_followed_by_laughter_merges_and_keeps_event_in_window(self):
        events = detect_events([frame(i, {"Laughter": .9} if i in (24,25) else {}) for i in range(35)], self.config, MIC)
        transcript = dict(core_start=5, core_end=11, exact_transcript_evidence="that was funny", content_tags=["REACTION"])
        attach_events(transcript, events)
        seeds = event_candidates(events, [segment(5,11,"that was funny")], self.config, 30, 24)
        candidates = deduplicate_candidates([transcript] + seeds)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(set(candidates[0]["candidate_sources"]), {"transcript", "laughter"})
        self.assertFalse(candidates[0]["audio_only"])
        self.assertEqual(candidates[0]["core_start"], 5)  # Preserve existing clip identity.
        start, end = _bounds(candidates[0], {"recommended_start":5,"recommended_end":11}, [segment(5,11)], AnalysisConfig())
        self.assertGreaterEqual(end, events[0]["end"])

    def test_audio_evidence_preserves_long_transcript_setup(self):
        event = self.scream()[0]
        candidate = dict(core_start=event["start"], core_end=event["end"], audio_events=[event],
                         audio_window_start=event["start"]-3, audio_window_end=event["end"]+3)
        start, end = _bounds(candidate, {"recommended_start":0,"recommended_end":25},
                             [segment(0,25,"long setup")], AnalysisConfig())
        self.assertEqual((start,end), (0,25))

    def test_d_isolated_mic_avoids_loud_game_track(self):
        tracks = [dict(index=1,audio_track=0,label="Game"), dict(index=2,audio_track=1,label="Morgan Mic")]
        selected = choose_track(tracks, self.config)
        self.assertEqual(selected["index"], 2)
        audio = {1: np.full(RATE*3, 25000), 2: np.zeros(RATE*3)}
        path = self.root/"mic.wav"; write_wav(path, audio[selected["index"]])
        result = analyze_events(path, self.root, self.config, selected, StubClassifier())
        self.assertEqual(result["events"], [])
        self.assertEqual(result["source"]["source"], "microphone")

    def test_e_sustained_laughter_groups_into_one_event(self):
        result = detect_events([frame(i,{"Laughter": .85}) for i in range(12)], self.config, MIC)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["detection_frames"], 12)
        self.assertAlmostEqual(result[0]["duration"], 6.24)
        self.assertAlmostEqual(result[0]["active_seconds"], 6.24)

    def test_f_scream_then_laughter_is_one_reaction_sequence(self):
        result = detect_events([frame(0,{"Screaming": .92}), frame(1,{"Screaming": .89}), frame(3,{"Laughter": .81})], self.config, MIC)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["event_types"], ["laughter","scream"])
        self.assertEqual(result[0]["confidence"], .92)
        self.assertEqual(len(event_candidates(result,[],self.config,100,24)), 1)

    def test_g_quiet_to_loud_supports_marginal_vocal_evidence_not_explosions(self):
        frames = [frame(i,{"Screaming": .4} if i==20 else {},rms=.1 if i==20 else .001) for i in range(40)]
        events = detect_events(frames, self.config, MIC)
        self.assertEqual(len(events), 1)
        self.assertGreater(events[0]["relative_loudness_db"], 30)
        self.assertTrue(events[0]["loudness_spike"])
        self.assertEqual(detect_events([frame(i,{},rms=.9 if i==20 else .001) for i in range(40)],self.config,MIX), [])
        self.assertEqual(detect_events([frame(i,{"Screaming": .7},background=.95) for i in range(4)],self.config,MIX), [])

    def test_track_selection_ambiguity_and_explicit_config(self):
        tracks = [dict(index=1,audio_track=0,label="Track1"), dict(index=2,audio_track=1,label="Track2")]
        self.assertEqual(choose_track(tracks,self.config)["source"],"mixed")
        self.assertEqual(choose_track(tracks,replace(self.config,microphone_track=1))["index"],2)
        with self.assertRaises(ValueError): choose_track(tracks,replace(self.config,microphone_track=2))
        self.assertEqual(choose_track([dict(index=1,label="Mic + Game")],self.config)["source"],"mixed")
        self.assertEqual(choose_track([dict(index=1,label="Mic 1"),dict(index=2,label="Mic 2")],self.config)["source"],"mixed")

    def test_cached_inference_resume_and_threshold_changes(self):
        wav=self.root/"audio.wav"; write_wav(wav,np.full(RATE*7,24000))
        cfg=replace(self.config,chunk_seconds=1.92)
        first=StubClassifier(fail_on=2)
        with self.assertRaises(RuntimeError): analyze_events(wav,self.root,cfg,MIC,first)
        self.assertEqual(len(list((self.root/"audio_event_cache").rglob('*.npz'))),1)
        retry=StubClassifier(); result=analyze_events(wav,self.root,cfg,MIC,retry)
        self.assertEqual(retry.calls,3)
        self.assertEqual(len(result["events"]),1)
        cached=StubClassifier(); changed=analyze_events(wav,self.root,replace(cfg,confidence_threshold=.99,loudness_threshold_relaxation=0),MIC,cached)
        self.assertEqual(cached.calls,0)
        self.assertEqual(changed["events"],[])
        # Content changes invalidate the cache, even when waveform length is unchanged.
        write_wav(wav,np.zeros(RATE*7)); model=StubClassifier(); analyze_events(wav,self.root,cfg,MIC,model)
        self.assertEqual(model.calls,4)

    def test_disabled_classes_minimum_duration_boundaries_and_limits(self):
        frames=[frame(i,{"Screaming":.9}) for i in range(8)]
        self.assertEqual(detect_events(frames,replace(self.config,enabled_classes=["laughter"]),MIC),[])
        self.assertEqual(detect_events([frame(0,{"Gasp":.9})],replace(self.config,minimum_event_seconds=2),MIC),[])
        events=detect_events(frames,replace(self.config,maximum_event_seconds=2),MIC)
        self.assertTrue(all(e["duration"]<=2 for e in events))
        seeds=event_candidates(events,[],self.config,4,2)
        self.assertEqual(len(seeds),2)
        self.assertTrue(all(0<=s["recommended_start"]<s["recommended_end"]<=4 for s in seeds))
        for config in (replace(self.config,chunk_seconds=0),replace(self.config,microphone_track=-1),replace(self.config,confidence_threshold=float('nan'))):
            with self.assertRaises(ValueError):validate_audio_events(config)

    def test_offline_mixed_reuses_waveform_and_explicit_mic_fails_closed(self):
        wav=self.root/"audio.wav";write_wav(wav,np.zeros(RATE))
        audio, source=prepare_audio(self.root,{},self.config)
        self.assertEqual(audio,wav)
        self.assertEqual(source["source"],"mixed")
        with self.assertRaises(ValueError):prepare_audio(self.root,{},replace(self.config,microphone_track=1))

    def test_audio_only_resume_needs_no_transcript_and_does_not_open_feedback(self):
        import process_stream
        write_wav(self.root/'audio.wav', np.zeros(RATE))
        with patch('sys.argv', ['process_stream.py','--resume-output',str(self.root),'--audio-events-only']):
            args=process_stream.parse_args()
        def classify(audio, directory, config, selection):
            return analyze_events(audio,directory,config,selection,StubClassifier())
        with ExitStack() as stack:
            stack.enter_context(patch('process_stream.configure_logging'))
            for method in ('choose_hardware','transcribe_audio','FeedbackStore','analyze_transcript'):
                stack.enter_context(patch('process_stream.'+method, side_effect=AssertionError('audio-only ran '+method)))
            stack.enter_context(patch('src.audio_events.analyze_events',side_effect=classify))
            self.assertEqual(process_stream.run_pipeline(args,AppConfig(),stack),0)
        self.assertFalse((self.root/'transcript.json').exists())
        self.assertEqual(json.loads((self.root/'audio_events_status.json').read_text())['status'],'complete')

    def test_successful_classifier_suppresses_legacy_volume_only_seeds(self):
        class EmptyProvider(FakeProvider):
            def analyze(self,prompt,text,schema):
                if 'candidates' in schema.get('properties',{}):return '{"candidates":[]}'
                return super().analyze(prompt,text,schema)
        result=analyze_transcript([segment(100,110,'hello')],AnalysisConfig(),Path(__file__).resolve().parents[1]/'prompts',
            self.root/'cache',provider=EmptyProvider(),audio_signals=[dict(start=100,end=110,possible_shouting=True,relative_energy=10)],
            audio_events=[],audio_events_available=True)
        self.assertEqual(result.candidates,[])


if __name__ == '__main__':
    unittest.main()

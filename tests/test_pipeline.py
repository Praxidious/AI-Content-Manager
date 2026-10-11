from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.analyze import (
    _audio_seed_candidates,
    _audio_text,
    analyze_transcript,
    attach_transcript_excerpts,
    chunk_transcript,
    deduplicate_candidates,
    explain_provider_error,
)
from src.audio import analyze_audio_energy, notable_audio_regions
from src.config import AnalysisConfig, AudioConfig
from src.report import write_report
from process_stream import unique_output_name, video_fingerprint
from src.utils import format_timestamp


def segment(start: float, end: float, text: str = "hello") -> dict:
    return {"start": start, "end": end, "text": text}


class FakeProvider:
    def __init__(self):
        self.calls = 0

    def analyze(self, _prompt: str, transcript: str, schema: dict) -> str:
        self.calls += 1
        if "candidates" in schema.get("properties", {}):
            first_line = next(line for line in transcript.splitlines() if line.startswith("["))
            start = float(first_line.split("|", 1)[0].lstrip("["))
            return json.dumps({"candidates": [{
                "core_start": start, "core_end": start + 10, "exact_transcript_evidence": "hello",
                "literal_description": "Someone says hello.", "content_tags": ["PERSONALITY"],
                "expected_setup": "A greeting.", "expected_payoff": "A response.",
                "why_detected": "A concise personality beat.", "needs_video_review": False
            }]})
        if "status" in schema.get("properties", {}):
            return json.dumps({"status": "SUPPORTED", "unsupported_claims": [],
                               "corrected_description": "Someone says hello.", "verification_confidence": 9})
        return json.dumps({
            "title": "Test moment", "tier": "A", "tier_reason": "Distinct and worth editing.",
            "entertainment_score": 8.5, "clipability_score": 8.0,
            "growth_potential_score": 6.5,
            "setup": "A greeting.", "escalation": "A response builds.", "payoff": "The response lands.",
            "why_entertaining": "It is immediate.", "what_to_look_for": "Check the reaction.",
            "suggested_editing_angle": "Open on the greeting.", "recommended_start": 100,
            "recommended_end": 110, "needs_video_review": False, "ai_confidence": 8,
            "major_rejection": False, "rejection_reasons": []
        })


class PipelineTests(unittest.TestCase):
    def test_chunking_covers_every_segment_through_long_silence_and_small_limits(self):
        segments=[segment(i,i+5,str(i)) for i in (0,5,10,15,4000,4005,4010)]
        for limit in (30,10000):
            chunks=chunk_transcript(segments,AnalysisConfig(chunk_minutes=2,chunk_overlap_minutes=1,max_chunk_characters=limit))
            covered={s['text'] for c in chunks for s in c.segments}
            self.assertEqual(covered,{s['text'] for s in segments})
            self.assertLessEqual(len(chunks),len(segments))
    def test_audio_prompt_is_compact_and_bounded(self):
        signals = [{"start": i * 2, "end": i * 2 + 1, "relative_energy": 7 + i / 100,
                    "speech_density": 8, "possible_laughter": i % 2 == 0,
                    "possible_shouting": False, "sudden_energy_change": True,
                    "rapid_speech_activity": True, "unused_large_field": "x" * 500}
                   for i in range(100)]
        payload = json.loads(_audio_text(signals, 0, 300, limit=12))
        self.assertEqual(len(payload), 12)
        self.assertTrue(all("unused_large_field" not in item for item in payload))
        self.assertLess(len(json.dumps(payload)), 3000)

    def test_audio_seed_candidates_are_capped_and_time_ordered(self):
        signals = [{"start": i * 70, "end": i * 70 + 5, "relative_energy": 9,
                    "possible_laughter": True, "sudden_energy_change": True}
                   for i in range(100)]
        segments = [segment(i * 70, i * 70 + 8, f"reaction {i}") for i in range(100)]
        seeds = _audio_seed_candidates(segments, signals, maximum=24)
        self.assertEqual(len(seeds), 24)
        self.assertEqual([item["core_start"] for item in seeds], sorted(item["core_start"] for item in seeds))

    def test_candidate_gets_verbatim_transcript_excerpt(self):
        candidates = [{"recommended_start": 10.0, "recommended_end": 20.0}]
        segments = [
            {"start": 8.0, "end": 11.0, "start_display": "00:00:08", "end_display": "00:00:11", "text": "Lead in"},
            {"start": 12.0, "end": 15.0, "start_display": "00:00:12", "end_display": "00:00:15", "text": "Exact dialogue"},
            {"start": 21.0, "end": 22.0, "start_display": "00:00:21", "end_display": "00:00:22", "text": "Outside"},
        ]
        attach_transcript_excerpts(candidates, segments)
        self.assertEqual([line["text"] for line in candidates[0]["transcript_excerpt"]], ["Lead in", "Exact dialogue"])

    def test_insufficient_quota_is_not_retried(self):
        class QuotaError(Exception):
            status_code = 429
            body = {"code": "insufficient_quota"}

        message, retryable = explain_provider_error(QuotaError("quota"))
        self.assertFalse(retryable)
        self.assertIn("billing", message)

    def test_distinct_replacement_video_gets_distinct_output(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stream.mp4"
            path.write_bytes(b"first video")
            first = video_fingerprint(path)
            path.write_bytes(b"replacement video")
            second = video_fingerprint(path)
            self.assertNotEqual(first, second)
            self.assertNotEqual(unique_output_name(path, first, None), unique_output_name(path, second, None))

    def test_timestamp_formatting(self):
        self.assertEqual(format_timestamp(2232.14), "00:37:12")
        self.assertEqual(format_timestamp(2232.14, milliseconds=True), "00:37:12.140")

    def test_chunks_overlap_and_preserve_absolute_times(self):
        segments = [segment(i, i + 9, str(i)) for i in range(0, 301, 10)]
        cfg = AnalysisConfig(chunk_minutes=2, chunk_overlap_minutes=0.5, max_chunk_characters=99999)
        chunks = chunk_transcript(segments, cfg)
        self.assertGreaterEqual(len(chunks), 3)
        self.assertEqual(chunks[0].start, 0)
        self.assertLess(chunks[1].start, chunks[0].end)
        self.assertGreater(chunks[-1].start, 0)

    def test_duplicate_overlap_keeps_higher_score(self):
        candidates = [
            {"core_start": 100, "core_end": 140, "entertainment_score": 8.1},
            {"core_start": 105, "core_end": 138, "entertainment_score": 9.2},
            {"core_start": 300, "core_end": 320, "entertainment_score": 7.5},
        ]
        result = deduplicate_candidates(candidates)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["entertainment_score"], 9.2)

    def test_analysis_cache_avoids_second_provider_call(self):
        cfg = AnalysisConfig(chunk_minutes=20, chunk_overlap_minutes=2, minimum_score=7)
        provider = FakeProvider()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompts = root / "prompts"
            prompts.mkdir()
            for name in ("candidate_discovery.txt", "evidence_verifier.txt", "content_quality_judge.txt"):
                (prompts / name).write_text(name, encoding="utf-8")
            segments = [segment(100, 110, "hello")]
            first = analyze_transcript(segments, cfg, prompts, root / "cache", provider=provider)
            second = analyze_transcript(segments, cfg, prompts, root / "cache", provider=provider)
            self.assertEqual(first.chunk_count, 1)
            self.assertEqual(first.candidates, second.candidates)
            self.assertEqual(provider.calls, 3)

    def test_malformed_discovery_retries_with_shorter_response_request(self):
        class FlakyDiscoveryProvider(FakeProvider):
            def __init__(self):
                super().__init__()
                self.requests: list[str] = []

            def analyze(self, prompt: str, transcript: str, schema: dict) -> str:
                self.requests.append(transcript)
                if len(self.requests) == 1:
                    self.calls += 1
                    return '{"candidates": ['
                return super().analyze(prompt, transcript, schema)

        cfg = AnalysisConfig(chunk_minutes=20, chunk_overlap_minutes=2, max_retries=2)
        provider = FlakyDiscoveryProvider()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompts = root / "prompts"
            prompts.mkdir()
            for name in ("candidate_discovery.txt", "evidence_verifier.txt", "content_quality_judge.txt"):
                (prompts / name).write_text(name, encoding="utf-8")
            with patch("src.analyze.time.sleep", return_value=None):
                outcome = analyze_transcript(
                    [segment(100, 110, "hello")], cfg, prompts, root / "cache", provider=provider
                )
            self.assertEqual(len(outcome.candidates), 1)
            self.assertIn("at most 5", provider.requests[1])
            self.assertEqual(len(list((root / "cache" / "discovery").glob("*_invalid.txt"))), 1)

    def test_report_tiers(self):
        candidate = {
            "core_start": 10.0, "core_end": 35.0, "recommended_start": 8.5,
            "recommended_end": 38.0, "estimated_duration": 29.5,
            "entertainment_score": 8.8, "clipability_score": 8.4, "growth_potential_score": 6.8,
            "title": "Panic", "description": "Surprise", "classification": "S",
            "tier": "S", "tier_reason": "Exceptional panic with a clear payoff.",
            "content_tags": ["CHAOS", "SCREAM_SHOCK"], "needs_video_review": False, "ai_confidence": 8.0,
            "setup": "Setup", "escalation": "Escalation", "payoff": "Payoff",
            "why_entertaining": "Clear reaction", "why_detected": "Rapid reactions",
            "what_to_look_for": "The visual cause", "suggested_editing_angle": "Open before the panic",
            "exact_transcript_evidence": "Evidence", "component_scores": {
                "hook_strength": 9, "personality": 9, "payoff_score": 9,
                "understandability": 8, "shareability": 8, "editing_potential": 8,
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "CLIP_REPORT.md"
            write_report(path, "vod.mp4", 600, {"model": "large-v3", "device": "cuda", "compute_type": "float16"}, 20, [candidate], 1)
            report = path.read_text(encoding="utf-8")
            self.assertIn("S — EXCEPTIONAL", report)
            self.assertIn("Exceptional panic with a clear payoff.", report)
            self.assertIn("00:00:08.500", report)
            self.assertIn("Panic", report)

    def test_relative_audio_spike_creates_notable_review_signal(self):
        import math
        import wave
        from array import array

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audio.wav"
            rate = 2000
            samples = array("h")
            for index in range(rate * 8):
                amplitude = 1000 if index < rate * 4 else (26000 if index < rate * 6 else 1000)
                samples.append(int(amplitude * math.sin(2 * math.pi * 120 * index / rate)))
            with wave.open(str(path), "wb") as target:
                target.setnchannels(1); target.setsampwidth(2); target.setframerate(rate)
                target.writeframes(samples.tobytes())
            windows = analyze_audio_energy(path, [segment(4, 6, "OH NO NO")], AudioConfig(
                analysis_window_seconds=1, analysis_hop_seconds=.5, baseline_seconds=8
            ))
            regions = notable_audio_regions(windows, 6.5)
            self.assertTrue(regions)
            self.assertTrue(any(region["sudden_energy_change"] for region in regions))


if __name__ == "__main__":
    unittest.main()

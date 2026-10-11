import io
import json
import math
import re
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from src.config import AnalysisConfig
from src.feedback import FeedbackStore
from src.performance import PerformanceStore, derive, validate_metrics
from src.performance_vision import extract, duration_seconds
from src.review import create_app


def metric(key, value, unit="count"):
    return dict(key=key, value=value, unit=unit)


class PerformanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = FeedbackStore(self.root / "feedback.sqlite3")
        self.performance = PerformanceStore(self.store)
        self.clip = self.add_clip(1)
        self.app = create_app(self.store, self.root / "previews")
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        page = self.client.get("/analytics").get_data(as_text=True)
        self.token = re.search(r'name="review-token" content="([^"]+)"', page).group(1)
        self.headers = {"X-Review-Token": self.token}

    def add_clip(self, number):
        return self.store.save_candidate(dict(core_start=number*30, core_end=number*30+20,
            title=f"Clip {number}", content_tags=["LAUGHTER"], entertainment_score=8,
            exact_transcript_evidence="funny reaction from the group", category="reaction",
            audio_signals=[dict(relative_energy=8, possible_laughter=True)]),
            {"fingerprint": "test-vod"}, self.root)

    def context(self, **changes):
        data = dict(clip_id=self.clip, platform="instagram_reels", account="wes",
                    posted_at="2025-01-01T12:00:00Z", captured_at="2025-01-02T12:00:00Z")
        data.update(changes)
        return data

    def save(self, data=None, rows=None, identifier="first"):
        return self.performance.confirm(self.context(**(data or {})), rows or [metric("views", 100)], identifier, {"metrics": []})

    def count(self, table):
        with closing(self.store.connect()) as db:
            return db.execute("SELECT count(*) FROM " + table).fetchone()[0]

    def test_migration_preserves_ratings_and_events_and_is_idempotent(self):
        self.store.rate(self.clip, "good", ["funny"], "keep me")
        with closing(self.store.connect()) as db, db:
            before = {t: [tuple(x) for x in db.execute("SELECT * FROM " + t)] for t in ("clips", "feedback", "feedback_events")}
            for table in ("performance_images", "performance_snapshots", "performance_posts", "performance_platforms"):
                db.execute("DROP TABLE " + table)
            db.execute("PRAGMA user_version=1")
        FeedbackStore(self.store.path)
        FeedbackStore(self.store.path)
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
            for table, rows in before.items():
                self.assertEqual([tuple(x) for x in db.execute("SELECT * FROM " + table)], rows)
        self.assertEqual(len(self.performance.platforms()), 3)

    def test_post_features_and_raw_derived_remain_separate(self):
        self.store.rate(self.clip, "good", ["funny"])
        self.save(rows=[metric("views", 100), metric("likes", 20), metric("engagement_rate", 15, "percent"),
                        metric("average_watch_time", 25, "seconds"), metric("traffic_source", "For You", "text")])
        snap = self.performance.snapshots()[0]
        self.assertEqual(next(x["value"] for x in snap["raw_metrics"] if x["key"] == "engagement_rate"), 15)
        self.assertEqual(snap["derived_metrics"]["likes_per_view_percent"]["value"], 20)
        self.assertNotIn("engagement_per_view_percent", snap["derived_metrics"])
        self.assertEqual(snap["derived_metrics"]["watch_duration_ratio_percent"]["value"], 125)
        self.store.rate(self.clip, "bad")
        self.assertEqual(self.performance.posts()[0]["features"]["human_rating"], "good")
        self.assertEqual(self.store.get(self.clip)["rating"], "bad")

    def test_multiple_snapshots_idempotency_and_same_time_rejection(self):
        identifier = self.save()
        self.assertEqual(identifier, self.save())
        post = self.performance.posts()[0]
        self.save(dict(post_id=post["post_id"], captured_at="2025-01-04T12:00:00Z"), identifier="later")
        self.assertEqual(self.count("performance_posts"), 1)
        self.assertEqual([x["age_hours"] for x in self.performance.snapshots()], [24, 72])
        with self.assertRaises(ValueError):
            self.save(identifier="duplicate-time")
        self.assertEqual(self.count("performance_snapshots"), 2)

    def test_invalid_inputs_and_unknown_clip_leave_no_records(self):
        bad = [dict(captured_at="2024-01-01T00:00:00Z"), dict(posted_at="2025-01-01T12:00:00"),
               dict(platform="unknown"), dict(account=""), dict(captured_at="2099-01-01T00:00:00Z")]
        for data in bad:
            with self.subTest(data=data), self.assertRaises(ValueError): self.save(data)
        with self.assertRaises(KeyError): self.save(dict(clip_id="missing"))
        for rows in ([metric("views", math.nan)], [metric("views", -1)], [metric("views", True)],
                     [metric("views", 12, "percent")], [metric("skip_rate", 110, "percent")],
                     [metric("views", 100), metric("views", 200)]):
            with self.subTest(rows=rows), self.assertRaises(ValueError): self.save(rows=rows)
        self.assertEqual(self.count("performance_posts"), 0)

    def test_missing_and_zero_are_distinct(self):
        self.assertEqual(derive([metric("views", 0), metric("likes", 0)], 20), {})
        result = derive([metric("views", 100), metric("likes", 0)], 20)
        self.assertEqual(result["likes_per_view_percent"]["value"], 0)
        self.assertNotIn("engagement_per_view_percent", result)
        self.assertEqual(validate_metrics([metric("other_stat", "unknown", "text")])[0]["value"], "unknown")

    def seed(self, n=8, platform="instagram_reels", account="wes", hour=24, multiplier=1, offset=0):
        for i in range(n):
            posted = datetime(2025, 1, 1, tzinfo=timezone.utc) + timedelta(days=i+offset)
            self.save(dict(clip_id=self.add_clip(i+offset+10), platform=platform, account=account,
                      posted_at=posted.isoformat(), captured_at=(posted+timedelta(hours=hour)).isoformat()),
                      [metric("views", (100+i*10)*multiplier)], f"{platform}-{account}-{hour}-{i}-{offset}")

    def test_platform_account_and_capture_age_are_isolated(self):
        self.seed()
        self.seed(platform="tiktok", multiplier=100)
        self.seed(account="other", multiplier=1000)
        self.seed(hour=168, multiplier=5000)
        data = self.performance.dashboard("instagram_reels", "wes", 24)
        last = data["observations"][-1]
        self.assertEqual(len(data["observations"]), 8)
        self.assertEqual(last["value"], 170)
        self.assertEqual(last["baseline"], 130)
        self.assertEqual(last["baseline_n"], 7)
        self.assertEqual(data["independent_clips"], 3)

    def test_growth_normalization_and_no_self_baseline(self):
        self.seed(n=6)
        original = self.performance.observations("instagram_reels", "wes")[-1]
        self.assertEqual(original["baseline"], 120)
        self.seed(n=6, offset=120, multiplier=10)
        grown = self.performance.observations("instagram_reels", "wes")[-1]
        self.assertEqual(grown["baseline"], 1200)
        self.assertAlmostEqual(original["normalized"], grown["normalized"], places=2)
        self.assertEqual(grown["baseline_n"], 5)

    def test_empty_account_cannot_mix_accounts_and_skip_rate_direction(self):
        with self.assertRaises(ValueError): self.performance.dashboard("instagram_reels", "")
        for i in range(6):
            self.save(dict(clip_id=self.add_clip(i+20), posted_at=f"2025-01-{i+1:02}T12:00:00Z",
                           captured_at=f"2025-01-{i+2:02}T12:00:00Z"),
                      [metric("skip_rate", 50 if i < 5 else 10, "percent")], f"skip{i}")
        observation = self.performance.observations("instagram_reels", "wes", metric="skip_rate")[-1]
        self.assertGreater(observation["normalized"], 0)
        self.assertEqual(observation["percentile"], 100)

    def test_reposts_and_extra_snapshots_do_not_inflate_learning_samples(self):
        self.seed(n=8)
        post = self.performance.posts()[0]
        captured = datetime.fromisoformat(post["posted_at"]) + timedelta(hours=25)
        self.save(dict(post_id=post["post_id"], captured_at=captured.isoformat()), identifier="extra")
        self.assertEqual(len(self.performance.observations("instagram_reels", "wes")), 8)
        for i in range(10):
            self.save(dict(clip_id=post["clip_id"], posted_at=f"2025-02-{i+1:02}T12:00:00Z",
                           captured_at=f"2025-02-{i+2:02}T12:00:00Z"), identifier=f"repost{i}")
        pred = self.performance.predict({"core_start": 0, "core_end": 20, "content_tags": ["LAUGHTER"]}, "instagram_reels", "wes")
        self.assertLessEqual(pred["history_n"], 3)
        self.assertFalse(pred["eligible"])
        self.assertEqual(pred["suggested_adjustment"], 0)

    def test_sufficient_history_is_bounded_and_excludes_target(self):
        self.seed(n=45)
        candidate = self.store.get(self.clip)["metadata"]
        result = self.performance.predict(candidate, "instagram_reels", "wes")
        self.assertGreaterEqual(result["history_n"], 30)
        self.assertGreaterEqual(result["similar_n"], 10)
        self.assertTrue(result["eligible"])
        self.assertGreater(result["suggested_adjustment"], 0)
        self.assertLessEqual(result["suggested_adjustment"], .5)
        self.assertEqual(self.performance.predict(candidate, "youtube_shorts", "wes")["suggested_adjustment"], 0)
        candidate["clip_id"] = self.performance.posts()[0]["clip_id"]
        self.assertNotIn(candidate["clip_id"], self.performance.predict(candidate, "instagram_reels", "wes")["example_clip_ids"])

    def draft(self, manual=True):
        return self.client.post("/api/analytics/extract", data={"context": json.dumps(self.context()), "manual": str(manual).lower(),
            "screenshots": (io.BytesIO(b"\x89PNG\r\n\x1a\nfixture"), "image.png")}, headers=self.headers)

    def test_extraction_and_preview_do_not_write_confirmation_is_required(self):
        self.app.config["ANALYTICS_EXTRACTOR"] = lambda *_: {"metrics": [metric("views", 123)], "warnings": ["Verify date"], "provider": "fake", "model": "fake"}
        result = self.draft(False)
        self.assertEqual(result.status_code, 200)
        data = result.get_json()
        self.assertEqual(data["metrics"][0]["value"], 123)
        self.assertEqual(self.count("performance_posts"), 0)
        self.assertEqual(self.count("performance_images"), 0)
        path = f"/api/analytics/drafts/{data['draft_id']}/confirm"
        payload = dict(metrics=data["metrics"], context=self.context())
        self.assertEqual(self.client.post(path, json=payload, headers=self.headers).status_code, 400)
        payload["confirmed"] = True
        self.assertEqual(self.client.post(path, json=payload).status_code, 403)
        first = self.client.post(path, json=payload, headers=self.headers)
        self.assertEqual(first.status_code, 200, first.get_data(as_text=True))
        self.assertEqual(self.client.post(path, json=payload, headers=self.headers).get_json(), first.get_json())
        self.assertEqual(self.count("performance_posts"), 1)
        self.assertEqual(self.count("performance_snapshots"), 1)
        self.assertEqual(self.count("performance_images"), 1)

    def test_discard_failure_and_invalid_image_leave_database_empty(self):
        draft = self.draft().get_json()["draft_id"]
        self.client.post(f"/api/analytics/drafts/{draft}/discard", json={}, headers=self.headers)
        result = self.client.post(f"/api/analytics/drafts/{draft}/confirm", json={"confirmed": True}, headers=self.headers)
        self.assertEqual(result.status_code, 410)
        self.app.config["ANALYTICS_EXTRACTOR"] = lambda *_: (_ for _ in ()).throw(RuntimeError("sensitive provider response"))
        result = self.draft(False)
        self.assertEqual(result.status_code, 422)
        self.assertNotIn("sensitive", result.get_data(as_text=True))
        bad = self.client.post("/api/analytics/extract", data={"context": json.dumps(self.context()),
            "screenshots": (io.BytesIO(b"<html>"), "fake.png")}, headers=self.headers)
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(self.count("performance_posts"), 0)

    def test_api_history_and_dashboard(self):
        self.save()
        post = self.performance.posts()[0]
        result = self.client.get("/api/analytics/posts/" + post["post_id"]).get_json()
        self.assertEqual(result["current_clip"]["clip_id"], self.clip)
        self.assertEqual(len(result["snapshots"]), 1)
        self.assertEqual(self.client.get("/api/analytics/dashboard?horizon=nan").status_code, 400)
        self.assertEqual(self.client.get("/api/analytics/dashboard?platform=instagram_reels&account=wes").status_code, 200)

    def test_openai_vision_request_uses_all_images_and_configured_model(self):
        from types import SimpleNamespace
        with patch("src.performance_vision.OpenAIProvider") as provider:
            provider.return_value.client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"metrics": [], "warnings": []}'))])
            extract(AnalysisConfig(analytics_vision_model="configured-vision"), [{"content": b"test", "mime": "image/png"}] * 2, "tiktok")
            kwargs = provider.return_value.client.chat.completions.create.call_args.kwargs
            self.assertEqual(kwargs["model"], "configured-vision")
            self.assertEqual(len(kwargs["messages"][1]["content"]), 3)
            self.assertTrue(kwargs["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_duration_conversion_does_not_use_model_arithmetic(self):
        self.assertEqual(duration_seconds("25h 10m 5s"), 90605)
        self.assertEqual(duration_seconds("1:02:03"), 3723)
        self.assertEqual(duration_seconds("12.5 seconds"), 12.5)
        self.assertIsNone(duration_seconds("unknown"))

    def test_pipeline_keeps_preference_separate_and_ranking_opt_in(self):
        from src.analyze import analyze_transcript
        from tests.test_pipeline import FakeProvider, segment
        prompts = Path(__file__).resolve().parents[1] / "prompts"
        def run(enabled):
            return analyze_transcript([segment(100, 110)], AnalysisConfig(audience_feedback_enabled=enabled),
                prompts, self.root / "pipeline-cache", provider=FakeProvider(), feedback_store=self.store,
                source={"fingerprint": "pipeline-test"}).candidates[0]
        # Even a mature audience signal cannot influence ranking until opted in.
        with patch.object(PerformanceStore, "predict", return_value={"suggested_adjustment": .5, "eligible": True}):
            disabled, enabled = run(False), run(True)
        self.assertEqual(disabled["audience_rank_adjustment"], 0)
        self.assertEqual(disabled["final_rank_score"], 8.5)
        self.assertEqual(enabled["final_rank_score"], 9)
        self.assertEqual(enabled["personal_preference_score"], disabled["personal_preference_score"])
        self.assertEqual(enabled["generic_quality_score"], disabled["generic_quality_score"])
        self.assertEqual(enabled["tier"], disabled["tier"])
        # Tiny real history stays neutral even when ranking influence is enabled.
        self.assertEqual(run(True)["audience_rank_adjustment"], 0)


if __name__ == "__main__":
    unittest.main()

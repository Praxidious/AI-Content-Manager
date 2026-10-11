"""Confirmed audience feedback in the existing feedback database.

Learning is transparent retrieval over age-matched, trailing-normalized outcomes.
No model is retrained and the default ranking contribution is zero.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import statistics
import uuid
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone

from .feedback import compact, now

METRICS = {
    **{k: "count" for k in ("views", "unique_viewers", "likes", "comments", "shares", "saves",
                            "reposts", "follows_gained", "subscribers_gained", "engaged_views")},
    "average_watch_time": "seconds", "total_watch_time": "seconds",
    **{k: "percent" for k in ("completion_rate", "skip_rate", "engagement_rate", "average_percentage_viewed")},
}
HORIZONS = (1, 6, 24, 72, 168)
MIN_BASELINE = 5
MIN_HISTORY = 30
MIN_NEIGHBORS = 10
LOWER_IS_BETTER = {"skip_rate"}


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("A date/time with timezone is required")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Use an ISO date/time with timezone") from exc
    if stamp.tzinfo is None:
        raise ValueError("Date/time must include a timezone")
    if stamp > datetime.now(timezone.utc):
        raise ValueError("Posting and capture dates cannot be in the future")
    return stamp.astimezone(timezone.utc).isoformat()


def finite(value):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


def validate_metrics(rows):
    if not isinstance(rows, list) or not 1 <= len(rows) <= 150:
        raise ValueError("Confirm between 1 and 150 metrics")
    result, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Each metric must be an object")
        key, value, unit = row.get("key"), row.get("value"), row.get("unit")
        if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", key):
            raise ValueError("Metric keys must use lowercase letters, numbers and underscores")
        if key in seen:
            raise ValueError(f"Resolve duplicate metric '{key}': keep one value or rename different statistics")
        seen.add(key)
        if unit not in ("count", "seconds", "percent", "number", "text"):
            raise ValueError(f"Unknown unit for {key}")
        if key in METRICS and unit != METRICS[key]:
            raise ValueError(f"{key} must use {METRICS[key]}")
        if unit == "text":
            if not isinstance(value, str) or len(value) > 2000:
                raise ValueError("Text statistics must be at most 2000 characters")
        elif not finite(value) or value < 0 or value > 1e15:
            raise ValueError(f"{key} must be a finite nonnegative number")
        elif key in ("completion_rate", "skip_rate") and value > 100:
            raise ValueError(f"{key} must be between 0 and 100 percent")
        confidence = row.get("confidence")
        if confidence is not None and (not finite(confidence) or not 0 <= confidence <= 1):
            raise ValueError("Extraction confidence must be between 0 and 1")
        sources = row.get("sources", [])
        if not isinstance(sources, list) or len(sources) > 6 or any(type(x) is not int or not 1 <= x <= 6 for x in sources):
            raise ValueError("Invalid screenshot references")
        result.append(dict(key=key, value=value, unit=unit,
                           label=str(row.get("label", key))[:200],
                           displayed_text=str(row.get("displayed_text", ""))[:500],
                           confidence=confidence, sources=sources))
    return result


def derive(rows, duration):
    values = {x["key"]: x["value"] for x in rows if finite(x["value"])}
    result = {}
    views = values.get("views", 0)
    if views > 0:
        for key in ("likes", "comments", "shares", "saves", "reposts", "follows_gained", "subscribers_gained"):
            if key in values:
                result[key + "_per_view_percent"] = {"value": 100 * values[key] / views,
                    "unit": "percent", "formula": f"100 * {key} / views"}
        if all(k in values for k in ("likes", "comments", "shares", "saves")):
            result["engagement_per_view_percent"] = {"value": 100 * sum(values[k] for k in ("likes", "comments", "shares", "saves")) / views,
                "unit": "percent", "formula": "100 * (likes + comments + shares + saves) / views"}
    if duration > 0 and "average_watch_time" in values:
        result["watch_duration_ratio_percent"] = {"value": 100 * values["average_watch_time"] / duration,
            "unit": "percent", "formula": "100 * average_watch_time / posted duration; not completion rate"}
    return result


def characteristics(metadata, attributes=None):
    """Use measured features only; missing speakers/emotion/timing stay unknown."""
    audio = metadata.get("audio_signals") or []
    tags = metadata.get("content_tags") or []
    result = {"duration": compact(metadata)["duration"], "tags": tags}
    aliases = {"category": ("clip_category", "category"), "game": ("game", "game_name"),
               "emotion": ("detected_emotion", "emotion"), "gameplay_event": ("gameplay_event",),
               "hook_timing": ("hook_timing", "hook_time_seconds"),
               "payoff_timing": ("payoff_timing", "payoff_time_seconds"),
               "reaction_intensity": ("reaction_intensity",), "speaker_count": ("speaker_count",),
               "editing_style": ("editing_style",)}
    for name, keys in aliases.items():
        for key in keys:
            if metadata.get(key) is not None:
                result[name] = metadata[key]
                break
    speakers = {str(x["speaker"]) for x in metadata.get("transcript_excerpt", []) if x.get("speaker") is not None}
    if speakers:
        result.setdefault("speaker_count", len(speakers))
    if audio:
        result["laughter"] = any(x.get("possible_laughter") for x in audio)
        energy = [x["relative_energy"] for x in audio if finite(x.get("relative_energy"))]
        if energy:
            result["audio_energy"] = max(energy)
    for key in ("laughter", "audio_energy"):
        if key in metadata:
            result[key] = metadata[key]
    result["ai_rating"] = metadata.get("entertainment_score")
    result.update(attributes or {})
    return {k: v for k, v in result.items() if v is not None}


class PerformanceStore:
    def __init__(self, feedback):
        self.feedback = feedback

    def platforms(self):
        with closing(self.feedback.connect()) as db:
            return [dict(x) for x in db.execute("SELECT * FROM performance_platforms ORDER BY label")]

    def context(self, data):
        if not isinstance(data, dict):
            raise ValueError("Expected a post object")
        captured = timestamp(data.get("captured_at"))
        post_id = data.get("post_id")
        if post_id:
            with closing(self.feedback.connect()) as db:
                row = db.execute("SELECT * FROM performance_posts WHERE post_id=?", (str(post_id),)).fetchone()
            if row is None:
                raise ValueError("Post not found")
            context = dict(row)
            context.pop("features_json")
        else:
            clip = self.feedback.get(str(data.get("clip_id", "")))
            platform = data.get("platform")
            if platform not in {x["platform"] for x in self.platforms()}:
                raise ValueError("Unknown platform")
            account = data.get("account", "")
            if not isinstance(account, str) or not account.strip() or len(account) > 100:
                raise ValueError("Enter an account name (at most 100 characters)")
            attributes = data.get("attributes", {})
            if not isinstance(attributes, dict) or len(attributes) > 30:
                raise ValueError("Invalid post characteristics")
            for key, value in attributes.items():
                if key not in {"duration", "hook_timing", "payoff_timing", "category", "laughter", "reaction_intensity", "speaker_count", "game", "editing_style", "emotion", "gameplay_event"}:
                    raise ValueError(f"Unknown post characteristic: {key}")
                if key in {"duration", "hook_timing", "payoff_timing", "reaction_intensity", "speaker_count"}:
                    if not finite(value) or value < 0 or (key == "duration" and value == 0):
                        raise ValueError(f"Invalid {key}")
                    if key == "reaction_intensity" and value > 10 or key == "speaker_count" and value != int(value):
                        raise ValueError(f"Invalid {key}")
                elif key == "laughter":
                    if type(value) is not bool:
                        raise ValueError("Laughter must be true or false")
                elif not isinstance(value, str) or len(value) > 200:
                    raise ValueError(f"Invalid {key}")
            context = dict(post_id=None, clip_id=clip["clip_id"], platform=platform,
                           account=account.strip().casefold(), posted_at=timestamp(data.get("posted_at")),
                           post_reference=str(data.get("post_reference", ""))[:500], attributes=attributes)
        age = (datetime.fromisoformat(captured) - datetime.fromisoformat(context["posted_at"])).total_seconds() / 3600
        if age < 0:
            raise ValueError("Capture time must be on or after posting time")
        return {**context, "captured_at": captured, "age_hours": age}

    def confirm(self, context, rows, import_id, extraction, images=()):
        context = self.context(context)
        rows = validate_metrics(rows)
        clip = self.feedback.get(context["clip_id"])
        features = dict(metadata=clip["metadata"], source=clip["source"], human_rating=clip["rating"],
                        rating_features=clip["features"], reasons=clip["reasons"],
                        characteristics=characteristics(clip["metadata"], context.get("attributes")))
        features["characteristics"]["human_rating"] = clip["rating"]
        post_id = context["post_id"] or uuid.uuid4().hex
        snapshot_id = uuid.uuid4().hex
        with closing(self.feedback.connect()) as db, db:
            existing = db.execute("SELECT snapshot_id FROM performance_snapshots WHERE import_id=?", (import_id,)).fetchone()
            if existing:
                return existing[0]
            if not context["post_id"]:
                duplicate = db.execute("SELECT post_id FROM performance_posts WHERE clip_id=? AND platform=? AND account=? AND posted_at=?",
                    (context["clip_id"], context["platform"], context["account"], context["posted_at"])).fetchone()
                if duplicate:
                    post_id = duplicate[0]
                else:
                    db.execute("INSERT INTO performance_posts VALUES (?,?,?,?,?,?,?,?)",
                        (post_id, context["clip_id"], context["platform"], context["account"], context["posted_at"],
                         context["post_reference"], json.dumps(features), now()))
            features = json.loads(db.execute("SELECT features_json FROM performance_posts WHERE post_id=?", (post_id,)).fetchone()[0])
            derived = derive(rows, features["characteristics"]["duration"])
            try:
                db.execute("INSERT INTO performance_snapshots VALUES (?,?,?,?,?,?,?,?,?)",
                    (snapshot_id, post_id, context["captured_at"], context["age_hours"], json.dumps(rows),
                     json.dumps(derived), json.dumps(extraction), import_id, now()))
            except sqlite3.IntegrityError as exc:
                raise ValueError("This post already has a snapshot at that capture time") from exc
            for index, image in enumerate(images, 1):
                db.execute("INSERT INTO performance_images VALUES (?,?,?,?,?,?)", (snapshot_id, index,
                    image["filename"], image["mime"], image["sha256"], image["content"]))
        return snapshot_id

    def posts(self, platform=None, account=None):
        with closing(self.feedback.connect()) as db:
            rows = db.execute("SELECT * FROM performance_posts ORDER BY posted_at DESC").fetchall()
        result = []
        for row in rows:
            row = dict(row)
            if platform and row["platform"] != platform or account and row["account"] != account:
                continue
            row["features"] = json.loads(row.pop("features_json"))
            result.append(row)
        return result

    def snapshots(self, post_id=None):
        with closing(self.feedback.connect()) as db:
            rows = db.execute("SELECT * FROM performance_snapshots" + (" WHERE post_id=?" if post_id else "") + " ORDER BY captured_at",
                              (post_id,) if post_id else ()).fetchall()
            evidence = defaultdict(list)
            for image in db.execute("SELECT snapshot_id,image_index,filename,sha256 FROM performance_images"):
                evidence[image["snapshot_id"]].append(dict(image))
        result = []
        for row in rows:
            row = dict(row)
            for key in ("raw_metrics", "derived_metrics", "extraction"):
                row[key] = json.loads(row.pop(key + "_json"))
            row.pop("import_id")
            row["images"] = evidence[row["snapshot_id"]]
            result.append(row)
        return result

    def observations(self, platform, account, horizon=24, metric="views"):
        if platform not in {x["platform"] for x in self.platforms()}:
            raise ValueError("Choose a registered platform")
        if not isinstance(account, str) or not account.strip():
            raise ValueError("Choose one account; cross-account baselines are not supported")
        if not finite(horizon) or not 0 < horizon <= 87600:
            raise ValueError("Comparison age must be between 0 and 87600 hours")
        posts = self.posts(platform, account)
        by_post = defaultdict(list)
        for snap in self.snapshots():
            # Tight maturity match. Never compare 1-hour views with 7-day views.
            if abs(snap["age_hours"] - horizon) <= max(.25, horizon * .25):
                value = next((x["value"] for x in snap["raw_metrics"] if x["key"] == metric and finite(x["value"])), None)
                if value is None:
                    value = snap["derived_metrics"].get(metric, {}).get("value")
                if finite(value):
                    by_post[snap["post_id"]].append((abs(snap["age_hours"] - horizon), snap, value))
        observations = []
        for post in sorted(posts, key=lambda p: p["posted_at"]):
            choices = by_post[post["post_id"]]
            if not choices:
                continue
            _, snap, value = min(choices, key=lambda x: (x[0], x[1]["captured_at"]))
            # Only previous posts whose matching snapshot was already captured by this capture.
            prior = [x for x in observations if x["posted_at"] < post["posted_at"]
                     and x["captured_at"] <= snap["captured_at"]
                     and 0 <= (datetime.fromisoformat(post["posted_at"]) - datetime.fromisoformat(x["posted_at"])).days <= 90]
            # Reposting a clip does not manufacture independent training examples.
            unique = {}
            for x in reversed(prior):
                if x["clip_id"] != post["clip_id"]:
                    unique.setdefault(x["clip_id"], x)
            prior = list(unique.values())[:30]
            baseline = statistics.median(x["value"] for x in prior) if prior else None
            normalized = math.log2((value + 1) / (baseline + 1)) if len(prior) >= MIN_BASELINE else None
            percentile = 100 * sum((x["value"] < value) + .5 * (x["value"] == value) for x in prior) / len(prior) if len(prior) >= MIN_BASELINE else None
            if metric in LOWER_IS_BETTER and normalized is not None:
                normalized, percentile = -normalized, 100 - percentile
            observations.append(dict(post_id=post["post_id"], clip_id=post["clip_id"], posted_at=post["posted_at"],
                captured_at=snap["captured_at"], age_hours=snap["age_hours"], value=value, baseline=baseline,
                baseline_n=len(prior), normalized=normalized, percentile=percentile,
                features=post["features"], title=post["features"]["metadata"].get("title", post["clip_id"])))
        return observations

    def dashboard(self, platform, account, horizon=24, metric="views"):
        observations = self.observations(platform, account, horizon, metric)
        unique = {}
        for x in observations:
            if x["normalized"] is not None:
                unique[x["clip_id"]] = x
        usable = list(unique.values())
        groups = defaultdict(list)
        coverage = defaultdict(int)
        for x in usable:
            for key, value in x["features"]["characteristics"].items():
                if value is None:
                    continue
                coverage[key] += 1
                if isinstance(value, list):
                    labels = [str(v) for v in value]
                elif isinstance(value, bool):
                    labels = ["yes" if value else "no"]
                elif finite(value):
                    size = 10 if key == "duration" else 2 if key in ("hook_timing", "payoff_timing") else 1
                    low = math.floor(value / size) * size
                    labels = [f"{low}–{low + size}"]
                else:
                    labels = [str(value)]
                for label in set(labels):
                    groups[(key, label)].append(x["normalized"])
        associations = []
        for (key, label), values in groups.items():
            n = len(values)
            mean = statistics.mean(values)
            margin = 1.96 * statistics.stdev(values) / math.sqrt(n) if n > 1 else None
            associations.append(dict(feature=key, group=label, n=n, mean_log2_lift=mean,
                interval=[mean - margin, mean + margin] if margin is not None else None,
                confidence="exploratory" if n >= 10 and len(usable) >= MIN_HISTORY else "insufficient",
                direction="stronger" if mean > 0 else "weaker" if mean < 0 else "typical"))
        return dict(platform=platform, account=account, horizon=horizon, metric=metric,
                    post_count=len(observations), independent_clips=len(usable), coverage=dict(coverage),
                    min_history=MIN_HISTORY, associations=sorted(associations, key=lambda x: -x["mean_log2_lift"]),
                    observations=observations, method="Trailing 30 independent clips within 90 days; same account/platform; ±25% capture age (minimum 15 minutes). Minimum 5 baseline clips. Lift = log₂((value + 1)/(median + 1)), reversed for skip rate. Higher is assumed better for other statistics. Associations are observational, not causal. Intervals are approximate 95% intervals for the group mean.")

    def predict(self, candidate, platform, account, horizon=24):
        target = characteristics(candidate)
        target_words = set(re.findall(r"[a-z]{3,}", compact(candidate)["transcript"].lower()))
        unique = {}
        for x in self.observations(platform, account, horizon):
            if x["normalized"] is not None and x["clip_id"] != candidate.get("clip_id"):
                unique[x["clip_id"]] = x
        neighbors = []
        for x in unique.values():
            features = x["features"]["characteristics"]
            matches = sum(target.get(k) is not None and target.get(k) == features.get(k)
                          for k in ("category", "game", "emotion", "laughter", "editing_style", "gameplay_event", "speaker_count"))
            tags = set(target.get("tags", [])) & set(features.get("tags", []))
            words = set(re.findall(r"[a-z]{3,}", compact(x["features"]["metadata"])["transcript"].lower()))
            overlap = len(words & target_words) / max(1, len(words | target_words))
            if not matches and not tags and overlap < .1:
                continue
            similarity = matches + len(tags) + overlap + 1 / (1 + abs(target["duration"] - features["duration"]) / 10)
            neighbors.append((similarity, x))
        neighbors.sort(key=lambda x: -x[0])
        neighbors = neighbors[:20]
        n, total = len(neighbors), len(unique)
        ready = total >= MIN_HISTORY and n >= MIN_NEIGHBORS
        values = [x[1]["normalized"] for x in neighbors]
        mean = statistics.mean(values) if values else None
        margin = 1.96 * statistics.stdev(values) / math.sqrt(n) if n > 1 else None
        # Broad or directionally uncertain evidence cannot alter ranking.
        stable = ready and margin is not None and abs(mean) > margin
        return dict(platform=platform, account=account, horizon=horizon, history_n=total, similar_n=n,
                    confidence="moderate" if stable else "low" if ready else "insufficient",
                    eligible=stable, predicted_log2_lift=mean,
                    interval=[mean-margin, mean+margin] if margin is not None else None,
                    suggested_adjustment=round(max(-.5, min(.5, mean * .25)), 3) if stable else 0,
                    example_clip_ids=[x[1]["clip_id"] for x in neighbors],
                    reason="Requires 30 normalized independent clips and 10 similar clips, with an interval excluding zero. No automatic retraining.")

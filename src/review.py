"""Loopback-only review UI and on-demand, bounded MP4 previews."""
from __future__ import annotations

import hashlib
import json
import math
import secrets
import subprocess
import threading
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_file

from .audio import require_ffmpeg
from .feedback import FeedbackStore, POSITIVE_REASONS, NEGATIVE_REASONS, RATINGS
from .ingest import read_guard, video_fingerprint


def preview_path(item, directory):
    key = json.dumps([item["clip_id"], item["start"], item["end"], item["source_key"]])
    return Path(directory) / (hashlib.sha256(key.encode()).hexdigest() + ".mp4")


def make_preview(item, directory):
    target = preview_path(item, directory)
    if target.is_file():
        return target
    source = Path(item["source"].get("source_path", ""))
    start, end = item["start"], item["end"]
    if start < 0 or end <= start or end - start > 180:
        raise ValueError("This range cannot be previewed: it must be between 0 and 180 seconds long.")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp.mp4")
    try:
        with read_guard(source):
            expected = item["source"].get("fingerprint")
            if expected and video_fingerprint(source) != expected:
                raise ValueError("Source VOD has changed. Reprocess it before generating a preview.")
            ffmpeg, _ = require_ffmpeg()
            result = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-ss", str(start), "-i", str(source), "-t", str(end - start),
                "-map", "0:v:0", "-map", "0:a:0?", "-vf", "scale=1280:720:force_original_aspect_ratio=decrease:force_divisible_by=2",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "25", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(temporary)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
            if result.returncode:
                raise RuntimeError("FFmpeg could not create this preview: " + result.stderr[-600:])
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def create_app(store: FeedbackStore, preview_directory: Path, analysis_config=None):
    app = Flask(__name__, template_folder="review_templates", static_folder="review_static")
    app.config.update(MAX_CONTENT_LENGTH=31 * 1024 * 1024, TRUSTED_HOSTS=["localhost", "127.0.0.1"])
    token = secrets.token_urlsafe(32)
    preview_lock = threading.Lock()

    @app.before_request
    def protect_local_writes():
        if request.path != "/api/analytics/extract":
            request.max_content_length = 256 * 1024 if request.path.startswith("/api/analytics/") else 16_384
        if request.method == "POST" and not secrets.compare_digest(request.headers.get("X-Review-Token", ""), token):
            abort(403)

    @app.after_request
    def headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'self'; media-src 'self'; frame-ancestors 'none'"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    def index():
        return render_template("review.html", token=token, ratings=RATINGS,
                               positive=POSITIVE_REASONS, negative=NEGATIVE_REASONS)

    @app.get("/api/clips")
    def clips():
        def score(clip, key):
            value = clip["metadata"].get(key)
            if value is None and key == "final_rank_score":
                value = clip["metadata"].get("entertainment_score")
            try:
                value = float(value)
                return value if math.isfinite(value) else float("-inf")
            except (ValueError, TypeError):
                return float("-inf")

        source_key = request.args.get("source")
        eligible = [clip for clip in store.clips(only_unrated=True)
                    if (source_key is None or clip["source_key"] == source_key)
                    and clip["end"] - clip["start"] >= 10
                    and clip["metadata"].get("tier") in {"S", "A"}
                    and not clip["metadata"].get("stage")
                    and not clip["metadata"].get("superseded")
                    and math.isfinite(score(clip, "final_rank_score"))]
        eligible.sort(key=lambda clip: (-score(clip, "final_rank_score"),
                      -score(clip, "clipability_score"), clip["source_key"], clip["start"], clip["clip_id"]))
        # Apply the limit after excluding feedback, so each visit gets the best remaining clips.
        return jsonify(eligible[:20])

    @app.get("/api/health")
    def health():
        return jsonify(app="contentbot-review", database=str(store.path.resolve()))

    @app.get("/api/summary")
    def summary():
        return jsonify(store.summary())

    def lookup(identifier):
        try:
            return store.get(identifier)
        except KeyError:
            abort(404)

    @app.post("/api/clips/<identifier>/feedback")
    def feedback(identifier):
        lookup(identifier)
        data = request.get_json()
        if not isinstance(data, dict):
            return jsonify(error="Expected a feedback object"), 400
        try:
            return jsonify(store.rate(identifier, data.get("rating"), data.get("reasons", []), data.get("note", "")))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400

    @app.post("/api/clips/<identifier>/preview")
    def preview(identifier):
        item = lookup(identifier)
        if not preview_lock.acquire(blocking=False):
            return jsonify(error="Another preview is being prepared. Try again shortly."), 409
        try:
            make_preview(item, preview_directory)
            return jsonify(url=f"/api/clips/{identifier}/video")
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            return jsonify(error=f"Preview unavailable. Check source access or FFmpeg. {exc}"), 422
        finally:
            preview_lock.release()

    @app.get("/api/clips/<identifier>/video")
    def video(identifier):
        path = preview_path(lookup(identifier), preview_directory)
        if not path.is_file():
            abort(404)
        return send_file(path, mimetype="video/mp4", conditional=True)

    from .performance_routes import register_performance
    register_performance(app, store, token, analysis_config)
    return app

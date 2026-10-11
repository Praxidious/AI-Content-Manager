"""Analytics pages/API share the review server's write token and local-only host policy."""
import io
import json
import secrets
import threading
import time
from collections import OrderedDict
from contextlib import closing

from flask import jsonify, render_template, request, send_file

from .config import AnalysisConfig
from .performance import HORIZONS, METRICS, PerformanceStore, derive, validate_metrics
from .performance_vision import extract, read_images


def register_performance(app, store, token, config=None):
    performance = PerformanceStore(store)
    config = config or AnalysisConfig()
    drafts = OrderedDict()
    lock = threading.Lock()
    extraction_lock = threading.Lock()
    app.extensions["performance"] = performance

    def prune():
        for key in list(drafts):
            if time.monotonic() - drafts[key]["created"] > 3600:
                del drafts[key]

    @app.get("/analytics")
    def analytics_page():
        return render_template("analytics.html", token=token, provider=config.provider,
                               vision_model=config.analytics_vision_model or config.model)

    @app.get("/api/analytics/catalog")
    def analytics_catalog():
        clips = [{"clip_id": c["clip_id"], "title": c["metadata"].get("title", "Untitled"),
                  "rating": c["rating"], "source": c["source"].get("filename", c["source_key"]),
                  "duration": c["end"] - c["start"]} for c in store.clips()]
        return jsonify(platforms=performance.platforms(), metrics=METRICS, horizons=HORIZONS, clips=clips,
                       posts=performance.posts())

    @app.post("/api/analytics/extract")
    def analytics_extract():
        # No database writes: bounded, expiring process memory until confirmation.
        if not extraction_lock.acquire(blocking=False):
            return jsonify(error="Another extraction is in progress. Try again shortly."), 409
        try:
            context = performance.context(json.loads(request.form.get("context", "{}")))
            manual = request.form.get("manual") == "true"
            images = read_images(request.files.getlist("screenshots")) if request.files else []
            if not images and not manual:
                raise ValueError("Choose screenshots or use manual entry")
            result = {"metrics": [], "warnings": [], "provider": "manual", "model": None}
            if not manual:
                try:
                    extractor = app.config.get("ANALYTICS_EXTRACTOR", extract)
                    result = extractor(config, images, context["platform"])
                except Exception:
                    app.logger.warning("Analytics extraction failed; no analytics saved")
                    return jsonify(error="Vision extraction failed. Check the configured vision model and credentials, retry, or use manual entry. No analytics were saved."), 422
            identifier = secrets.token_urlsafe(24)
            with lock:
                prune()
                if len(drafts) >= 4:
                    return jsonify(error="Four imports are awaiting confirmation. Confirm or discard one first."), 409
                drafts[identifier] = dict(context=context, extraction=result, images=images, created=time.monotonic())
            return jsonify(draft_id=identifier, context=context, **result,
                           images=[{"index": i, "filename": x["filename"]} for i, x in enumerate(images, 1)])
        except (ValueError, KeyError, TypeError) as exc:
            return jsonify(error=str(exc)), 400
        finally:
            extraction_lock.release()

    @app.get("/api/analytics/drafts/<identifier>/images/<int:index>")
    def draft_image(identifier, index):
        with lock:
            prune()
            draft = drafts.get(identifier)
            if not draft or not 1 <= index <= len(draft["images"]):
                return jsonify(error="Screenshot expired or missing"), 404
            image = draft["images"][index - 1]
        return send_file(io.BytesIO(image["content"]), mimetype=image["mime"])

    @app.post("/api/analytics/drafts/<identifier>/discard")
    def discard_draft(identifier):
        with lock:
            drafts.pop(identifier, None)
        return jsonify(discarded=True)

    @app.post("/api/analytics/drafts/<identifier>/confirm")
    def confirm_draft(identifier):
        data = request.get_json()
        if not isinstance(data, dict) or data.get("confirmed") is not True:
            return jsonify(error="Explicit confirmation is required"), 400
        with lock:
            prune()
            # Idempotent retry even after browser timeout or a server restart.
            with closing(store.connect()) as db:
                saved = db.execute("SELECT snapshot_id FROM performance_snapshots WHERE import_id=?", (identifier,)).fetchone()
            if saved:
                return jsonify(snapshot_id=saved[0])
            draft = drafts.get(identifier)
            if not draft:
                return jsonify(error="Import expired. Extract again; nothing was saved."), 410
            try:
                snapshot = performance.confirm(data.get("context", draft["context"]), data.get("metrics"),
                    identifier, draft["extraction"], draft["images"])
            except (ValueError, KeyError, TypeError) as exc:
                return jsonify(error=str(exc)), 400
            del drafts[identifier]
        return jsonify(snapshot_id=snapshot)

    @app.post("/api/analytics/derive")
    def preview_derived():
        try:
            data = request.get_json()
            context = performance.context(data["context"])
            if context.get("post_id"):
                duration = next(p for p in performance.posts() if p["post_id"] == context["post_id"])["features"]["characteristics"]["duration"]
            else:
                clip = store.get(context["clip_id"])
                duration = context.get("attributes", {}).get("duration", clip["end"] - clip["start"])
            return jsonify(derive(validate_metrics(data["metrics"]), duration))
        except (ValueError, KeyError, TypeError) as exc:
            return jsonify(error=str(exc)), 400

    @app.get("/api/analytics/posts/<identifier>")
    def post_history(identifier):
        post = next((p for p in performance.posts() if p["post_id"] == identifier), None)
        if not post:
            return jsonify(error="Post not found"), 404
        return jsonify(post=post, snapshots=performance.snapshots(identifier), current_clip=store.get(post["clip_id"]))

    @app.get("/api/analytics/snapshots/<identifier>/images/<int:index>")
    def saved_image(identifier, index):
        with closing(store.connect()) as db:
            image = db.execute("SELECT content,mime FROM performance_images WHERE snapshot_id=? AND image_index=?",
                               (identifier, index)).fetchone()
        if not image:
            return jsonify(error="Screenshot not found"), 404
        return send_file(io.BytesIO(image["content"]), mimetype=image["mime"])

    @app.get("/api/analytics/dashboard")
    def analytics_dashboard():
        try:
            return jsonify(performance.dashboard(request.args.get("platform", "instagram_reels"),
                request.args.get("account", "default").strip().casefold(), float(request.args.get("horizon", 24)),
                request.args.get("metric", "views")))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400

    @app.get("/api/analytics/prediction/<identifier>")
    def audience_prediction(identifier):
        try:
            clip = store.get(identifier)
            prediction = performance.predict(clip["metadata"], request.args.get("platform", "instagram_reels"),
                                             request.args.get("account", "default").strip().casefold())
            return jsonify(wes_preference_score=clip["metadata"].get("personal_preference_score"),
                           human_rating=clip["rating"], audience=prediction)
        except KeyError:
            return jsonify(error="Clip not found"), 404

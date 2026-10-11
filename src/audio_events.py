"""Local sound classification, independent of ASR. No clip scoring happens here."""
from __future__ import annotations

import csv
import hashlib
import json
import logging
import math
import re
import subprocess
import wave
import zipfile
from dataclasses import asdict
from pathlib import Path

from .audio import FFmpegError, require_ffmpeg
from .config import AudioEventConfig, validate_audio_events
from .utils import atomic_write_json

LOG = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]
RATE, HOP, PATCH = 16000, .48, .96
VERSION = 1
LABELS = {
    "Laughter": "laughter", "Belly laugh": "laughter", "Chuckle, chortle": "laughter", "Snicker": "giggle", "Giggle": "giggle",
    "Screaming": "scream", "Shout": "shout", "Yell": "shout", "Bellow": "shout", "Whoop": "whoop", "Gasp": "gasp",
    "Crying, sobbing": "crying", "Whimper": "crying", "Wail, moan": "crying", "Groan": "groan", "Grunt": "grunt",
    "Cheering": "group_reaction", "Hubbub, speech noise, speech babble": "group_reaction", "Sigh": "sigh", "Snort": "snort",
}
BACKGROUND = {"Explosion", "Gunshot, gunfire", "Artillery fire", "Machine gun", "Music", "Sound effect", "Video game music"}


def file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def probe_audio_tracks(video):
    _, ffprobe = require_ffmpeg()
    result = subprocess.run([ffprobe, "-v", "error", "-select_streams", "a", "-show_entries",
        "stream=index,codec_name,channels:stream_tags=title,handler_name", "-of", "json", str(video)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
    if result.returncode:
        raise FFmpegError(f"Cannot inspect audio tracks: {result.stderr[-500:]}")
    tracks = json.loads(result.stdout).get("streams", [])
    return [dict(t, audio_track=i, label=t.get("tags", {}).get("title") or t.get("tags", {}).get("handler_name") or f"Audio track {i+1}")
            for i, t in enumerate(tracks)]


def choose_track(tracks, config):
    if config.microphone_track is not None:
        if config.microphone_track >= len(tracks):
            raise ValueError("Configured microphone_track does not exist; audio track numbers are zero-based")
        return dict(tracks[config.microphone_track], source="microphone", selection="explicit microphone track; isolation is user-declared")
    microphones = [t for t in tracks if re.search(r"\b(mic|microphone)\b", t["label"], re.I)
                   and not re.search(r"game|desktop|mix|music|discord", t["label"], re.I)]
    if len(microphones) == 1:
        return dict(microphones[0], source="microphone", selection="single clearly labeled mic track; isolation is assumed from label")
    return dict(source="mixed", index=None, audio_track=None, label="Original transcription audio",
                selection="No unambiguous isolated mic; reuse unchanged FFmpeg automatic audio selection")


def prepare_audio(output_dir, source, config, video=None):
    """Reuse speech WAV for mixed audio; decode a selected isolated track only once.

    Speech extraction and its FFmpeg default selection are deliberately untouched.
    Offline resumes can use an already extracted mic, or conservatively labeled legacy WAV.
    """
    from .ingest import read_guard, video_fingerprint
    directory = Path(output_dir)
    selection_path = directory / "audio_event_source.json"
    cached = json.loads(selection_path.read_text()) if selection_path.is_file() else None
    selection = None
    tracks = cached.get("available_tracks", []) if cached else []
    if video is not None:
        tracks = probe_audio_tracks(video)
        selection = choose_track(tracks, config)
    elif cached and cached.get("microphone_track_config") == config.microphone_track:
        selection = cached["selection"]
    elif config.microphone_track is not None:
        raise ValueError("An explicit microphone track needs the source VOD or its previously extracted reaction_audio.wav. Run the VOD once with --force-video.")
    else:
        selection = choose_track([], config)
    identity = {"selection": selection, "microphone_track_config": config.microphone_track,
                "fingerprint": source.get("fingerprint"), "test_minutes": source.get("test_minutes"), "available_tracks": tracks}
    if selection["source"] == "microphone":
        audio = directory / "reaction_audio.wav"
        if not audio.is_file() or cached != identity:
            if video is None:
                raise ValueError("The selected microphone WAV is unavailable; run the source VOD once with --force-video")
            ffmpeg, _ = require_ffmpeg()
            temporary = audio.with_suffix(".tmp.wav")
            command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(video), "-map", f"0:{selection['index']}"]
            if source.get("test_minutes"):
                command += ["-t", str(source["test_minutes"] * 60)]
            command += ["-vn", "-af", "aresample=async=1:first_pts=0", "-ac", "1", "-ar", str(RATE), "-c:a", "pcm_s16le", str(temporary)]
            try:
                with read_guard(video):
                    if source.get("fingerprint") and video_fingerprint(video) != source["fingerprint"]:
                        raise ValueError("Source changed; microphone audio cannot be aligned with the saved transcript")
                    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
                    if result.returncode:
                        raise FFmpegError(result.stderr[-500:])
                temporary.replace(audio)
            finally:
                temporary.unlink(missing_ok=True)
    else:
        audio = directory / "audio.wav"
    if not audio.is_file():
        raise FileNotFoundError("No extracted audio available for sound-event analysis")
    # YAMNet accepts only 16 kHz mono PCM. Reuse existing default-format WAV without conversion.
    with wave.open(str(audio), "rb") as wav:
        format_ok = wav.getframerate() == RATE and wav.getnchannels() == 1 and wav.getsampwidth() == 2
    if not format_ok:
        converted = directory / "audio_events_16k.wav"
        conversion_identity = {"path": str(audio.resolve()), "size": audio.stat().st_size, "mtime": audio.stat().st_mtime_ns}
        conversion_meta = directory / "audio_events_16k_source.json"
        if not converted.is_file() or not conversion_meta.is_file() or json.loads(conversion_meta.read_text()) != conversion_identity:
            ffmpeg, _ = require_ffmpeg()
            temporary = converted.with_suffix(".tmp.wav")
            try:
                result = subprocess.run([ffmpeg, "-v", "error", "-nostdin", "-y", "-i", str(audio), "-ac", "1", "-ar", str(RATE), "-c:a", "pcm_s16le", str(temporary)], capture_output=True, text=True)
                if result.returncode:
                    raise FFmpegError(result.stderr[-500:])
                temporary.replace(converted)
                atomic_write_json(conversion_meta, conversion_identity)
            finally:
                temporary.unlink(missing_ok=True)
        audio = converted
    atomic_write_json(selection_path, identity)
    LOG.info("Audio events source: %s (%s)", selection["label"], selection["selection"])
    return audio, selection


class Yamnet:
    def __init__(self, config):
        import onnxruntime as ort
        model = Path(config.model_path)
        labels = Path(config.labels_path)
        model = model if model.is_absolute() else ROOT / model
        labels = labels if labels.is_absolute() else ROOT / labels
        if not model.is_file() or not labels.is_file():
            raise RuntimeError("Audio model missing. Run python tools/setup_audio_events.py")
        self.fingerprint = file_digest(model) + ":" + file_digest(labels)
        with labels.open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))
        if len(rows) != 521 or [int(r["index"]) for r in rows] != list(range(521)):
            raise ValueError("YAMNet requires the ordered 521-class AudioSet label map")
        self.labels = [r["display_name"] for r in rows]
        self.mids = [r["mid"] for r in rows]
        providers = ["CPUExecutionProvider"]
        if config.device != "cpu" and "CUDAExecutionProvider" in ort.get_available_providers():
            from .transcribe import configure_project_cuda_dlls
            configure_project_cuda_dlls()
            providers.insert(0, "CUDAExecutionProvider")
        options = ort.SessionOptions()
        options.intra_op_num_threads = config.cpu_threads
        try:
            self.session = ort.InferenceSession(str(model), sess_options=options, providers=providers)
        except Exception:
            if providers == ["CPUExecutionProvider"]:
                raise
            LOG.warning("YAMNet CUDA initialization failed; using CPU")
            self.session = ort.InferenceSession(str(model), sess_options=options, providers=["CPUExecutionProvider"])
        self.device = self.session.get_providers()[0]
        if config.device != "cpu" and self.device == "CPUExecutionProvider":
            LOG.info("YAMNet using CPU fallback; installed ONNX Runtime has no active CUDA provider")
        outputs = [x for x in self.session.get_outputs() if len(x.shape) == 2 and x.shape[-1] == 521]
        if len(outputs) != 1:
            raise ValueError("Configured ONNX model does not expose YAMNet 521-class frame scores")
        self.output = outputs[0].name

    def predict(self, waveform):
        try:
            return self.session.run([self.output], {self.session.get_inputs()[0].name: waveform})[0]
        except Exception:
            if self.device == "CPUExecutionProvider":
                raise
            LOG.warning("YAMNet GPU inference failed; retrying this chunk on CPU")
            self.session.set_providers(["CPUExecutionProvider"])
            self.device = "CPUExecutionProvider"
            return self.session.run([self.output], {self.session.get_inputs()[0].name: waveform})[0]


def acoustic_context(frames, config):
    """Baseline depends only on the selected waveform, never on recognized words."""
    import numpy as np
    rms = np.asarray([x["rms"] for x in frames])
    normal = float(np.median(rms[rms > 1 / 32768])) if np.any(rms > 1 / 32768) else 1 / 32768
    radius = max(1, round(config.baseline_seconds / HOP / 2))
    for i, frame in enumerate(frames):
        baseline = max(1 / 32768, float(np.median(rms[max(0, i-radius):min(len(rms), i+radius+1)])))
        frame["relative_loudness_db"] = round(20 * math.log10(max(frame["rms"], 1 / 32768) / baseline), 2)
        frame["session_relative_loudness_db"] = round(20 * math.log10(max(frame["rms"], 1 / 32768) / normal), 2)
        previous = max(1 / 32768, float(np.median(rms[max(0, i-4):i]))) if i else baseline
        frame["quiet_to_loud_db"] = round(20 * math.log10(max(frame["rms"], 1 / 32768) / previous), 2)
        frame["loudness_spike"] = max(frame["relative_loudness_db"], frame["quiet_to_loud_db"]) >= config.relative_loudness_db
    return frames


def merge_events(events, config):
    merged = []
    for event in sorted(events, key=lambda x: x["start"]):
        if (merged and event["start"] <= merged[-1]["end"] + config.merge_gap_seconds
                and event["end"] - merged[-1]["start"] <= config.maximum_event_seconds):
            current = merged[-1]
            current["end"] = max(current["end"], event["end"])
            current["event_types"] = sorted(set(current["event_types"] + event["event_types"]))
            for label, value in event["class_scores"].items():
                current["class_scores"][label] = max(current["class_scores"].get(label, 0), value)
            for key in ("confidence", "relative_loudness_db", "session_relative_loudness_db", "quiet_to_loud_db", "rms", "peak", "vocal_activity_confidence"):
                current[key] = max(current[key], event[key])
            current["loudness_spike"] |= event["loudness_spike"]
            current["detection_frames"] += 1
            current["active_seconds"] += max(0, event["end"] - max(current["active_end"], event["start"]))
            current["active_end"] = max(current["active_end"], event["end"])
        else:
            merged.append({**event, "class_scores": dict(event["class_scores"]), "detection_frames": 1,
                           "active_seconds": event["end"]-event["start"], "active_end": event["end"]})
    result = []
    for event in merged:
        if event["active_seconds"] < config.minimum_event_seconds:
            continue
        event.pop("active_end")
        event["duration"] = round(event["end"] - event["start"], 3)
        event["active_seconds"] = round(event["active_seconds"], 3)
        event["event_id"] = hashlib.sha256(json.dumps([event["start"], event["end"], event["event_types"], event["source"]]).encode()).hexdigest()[:16]
        result.append(event)
    return result


def detect_events(frames, config, selection):
    events = []
    mixed = selection["source"] != "microphone"
    threshold = config.mixed_confidence_threshold if mixed else config.confidence_threshold
    for frame in acoustic_context(frames, config):
        threshold_now = max(.25, threshold - config.loudness_threshold_relaxation) if frame["loudness_spike"] else threshold
        accepted = {label: score for label, score in frame["reaction_scores"].items()
                    if LABELS[label] in config.enabled_classes and score >= threshold_now
                    and (not mixed or score >= frame["background_confidence"] + config.background_margin)}
        if not accepted:
            continue  # Loudness alone never creates a reaction label.
        events.append({key: frame[key] for key in ("start", "end", "rms", "peak", "relative_loudness_db", "session_relative_loudness_db", "quiet_to_loud_db", "loudness_spike", "vocal_activity_confidence")}
                      | dict(event_types=sorted({LABELS[x] for x in accepted}), confidence=max(accepted.values()),
                             class_scores=accepted, source=selection["source"], source_label=selection["label"],
                             attribution="unverified; classifier cannot identify Morgan or count speakers"))
    return merge_events(events, config)


def analyze_events(audio_path, output_dir, config, selection, classifier=None):
    import numpy as np
    validate_audio_events(config)
    if not config.enabled:
        return {"status": "disabled", "events": []}
    classifier = classifier or Yamnet(config)
    digest = file_digest(audio_path)
    chunk_frames = max(2, round(config.chunk_seconds / HOP))
    identity = {"version": VERSION, "audio_sha256": digest, "model": classifier.fingerprint, "chunk_frames": chunk_frames}
    cache_key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]
    cache = Path(output_dir) / "audio_event_cache" / cache_key
    cache.mkdir(parents=True, exist_ok=True)
    frames = []
    reaction_indices = [(i, label) for i, label in enumerate(classifier.labels) if label in LABELS]
    background_indices = [i for i, x in enumerate(classifier.labels) if x in BACKGROUND]
    vocal_indices = [i for i, x in enumerate(classifier.labels) if x in LABELS or x in {"Speech", "Conversation", "Whispering"}]
    with wave.open(str(audio_path), "rb") as wav:
        if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (RATE, 1, 2):
            raise ValueError("Audio events require 16 kHz mono PCM16 WAV")
        total = wav.getnframes()
        duration = total / RATE
        for number, offset in enumerate(range(0, total, chunk_frames * 7680)):
            wav.setpos(offset)
            samples = np.frombuffer(wav.readframes(chunk_frames * 7680 + 16000), dtype="<i2").astype(np.float32) / 32768
            owned = min(chunk_frames, math.ceil((total-offset) / 7680))
            path = cache / f"chunk_{number:05d}.npz"
            scores = None
            if path.is_file():
                try:
                    with np.load(path, allow_pickle=False) as saved:
                        scores = saved["scores"]
                    if scores.shape != (owned, len(classifier.labels)) or not np.isfinite(scores).all() or (scores < 0).any() or (scores > 1).any():
                        scores = None
                except (OSError, ValueError, KeyError, EOFError, zipfile.BadZipFile):
                    scores = None
            if scores is None:
                needed = (owned-1)*7680 + 16000
                waveform = np.pad(samples, (0, max(0, needed-len(samples))))
                scores = np.asarray(classifier.predict(waveform), dtype=np.float32)[:owned]
                if scores.shape != (owned, len(classifier.labels)) or not np.isfinite(scores).all() or (scores < 0).any() or (scores > 1).any():
                    raise ValueError("Invalid classifier score shape or confidence values")
                temporary = path.with_suffix(".tmp")
                with temporary.open("wb") as stream:
                    np.savez_compressed(stream, scores=scores)
                temporary.replace(path)
                LOG.info("Audio events: analyzed %.1f / %.1f minutes", min(duration, (offset+owned*7680)/RATE)/60, duration/60)
            for i, row in enumerate(scores):
                start = offset / RATE + i * HOP
                block = samples[i*7680:i*7680+15360]
                if not len(block):
                    continue
                frames.append(dict(start=round(start, 3), end=round(min(duration, start+PATCH), 3),
                    rms=float(np.sqrt(np.mean(block*block))), peak=float(np.max(np.abs(block))),
                    vocal_activity_confidence=float(max(row[vocal_indices], default=0)),
                    background_confidence=float(max(row[background_indices], default=0)),
                    reaction_scores={label: round(float(row[i]), 5) for i, label in reaction_indices},
                    top_classes=[dict(label=classifier.labels[j], confidence=round(float(row[j]), 5)) for j in np.argsort(row)[-5:][::-1]]))
    events = detect_events(frames, config, selection)
    for event in events:
        event.update(model=config.model, model_fingerprint=classifier.fingerprint,
                     audio_stream_index=selection.get("index"), audio_track=selection.get("audio_track"))
    result = dict(schema_version=VERSION, status="complete", duration=duration, model=config.model,
                  model_fingerprint=classifier.fingerprint, device=classifier.device, source=selection,
                  cache_key=cache_key, class_names=classifier.labels, settings=asdict(config), events=events, windows=frames)
    atomic_write_json(Path(output_dir) / "audio_events.json", result)
    LOG.info("Audio events: %d grouped reactions (%s)", len(events), classifier.device)
    return result


def metadata(events):
    types = sorted({t for event in events for t in event["event_types"]})
    return dict(audio_events=events, audio_event_types=types, audio_event_count=len(events),
                max_audio_confidence=max((e["confidence"] for e in events), default=0),
                max_relative_loudness_db=max((e["relative_loudness_db"] for e in events), default=0),
                contains_scream="scream" in types, contains_laughter=bool({"laughter", "giggle"} & set(types)),
                contains_gasp="gasp" in types)


def attach_events(candidate, events):
    matched = [e for e in events if e["end"] >= candidate["core_start"] and e["start"] <= candidate["core_end"]]
    candidate.update(metadata(matched))
    sources = list(candidate.get("candidate_sources", []))
    if candidate.get("exact_transcript_evidence"):
        sources.append("transcript")
    sources.extend(candidate["audio_event_types"])
    if any(e["loudness_spike"] for e in matched):
        sources.append("loudness_spike")
    candidate["candidate_sources"] = list(dict.fromkeys(sources))
    candidate["audio_only"] = bool(matched) and "transcript" not in candidate["candidate_sources"]


def event_candidates(events, segments, config, duration, maximum):
    # Candidate cap is the existing discovery budget, not a new final clip score.
    selected = sorted(events, key=lambda e: (-e["confidence"], e["start"]))[:maximum]
    result = []
    for event in selected:
        start, end = event["start"], event["end"]
        lo, hi = max(0, start-config.pre_roll_seconds), min(duration, end+config.post_roll_seconds)
        nearby = [s for s in segments if s["end"] >= lo and s["start"] <= hi]
        evidence = " ".join(s.get("text", "").strip() for s in nearby if s.get("text", "").strip())[:300]
        candidate = dict(core_start=start, core_end=end, exact_transcript_evidence=evidence,
            description="Classifier suggests " + ", ".join(event["event_types"]) + "; cause and speaker need review.",
            content_tags=[x.upper() for x in event["event_types"]], expected_setup="", expected_payoff="",
            why_detected=("Transcript + " if evidence else "Audio only — ") + ", ".join(event["event_types"]),
            needs_video_review=True, audio_priority=True, source_chunk=-2,
            audio_window_start=round(lo, 3), audio_window_end=round(hi, 3),
            recommended_start=round(lo, 3), recommended_end=round(hi, 3))
        attach_events(candidate, [event])
        result.append(candidate)
    return result

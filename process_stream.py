from __future__ import annotations

import argparse
import copy
import math
from dataclasses import asdict
from contextlib import ExitStack
import json
import logging
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from src.analyze import analyze_transcript
from src.audio import FFmpegError, analyze_audio_energy, extract_audio, notable_audio_regions, require_ffmpeg
from src.config import load_config
from src.feedback import FeedbackStore
from src.review_launcher import open_review
from src.ingest import Recording, Registry, discover, inspect_recordings, read_guard, registry_lock, resolve_path, select_video, snapshot, video_fingerprint
from src.report import write_rejected_report, write_report
from src.transcribe import choose_hardware, transcribe_audio
from src.utils import atomic_write_json, format_timestamp

PROJECT_ROOT = Path(__file__).resolve().parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Turn a local Twitch VOD into a timestamped transcript and ranked clip report."
    )
    parser.add_argument("video", nargs="?", type=Path, help="Explicit VOD path; overrides configured source discovery")
    parser.add_argument(
        "--resume-output",
        type=Path,
        help="Rerun analysis from an existing output folder; the original VOD is not required",
    )
    parser.add_argument("--source", type=Path, help="Override the configured VOD source directory")
    parser.add_argument("--list-vods", action="store_true", help="List recording readiness without processing")
    parser.add_argument("--force-video", action="store_true", help="Allow a completed VOD; preserves existing cache/rerun flags")
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "config.json")
    parser.add_argument("--reanalyze", action="store_true", help="Ignore cached LLM chunk results and analyze again")
    parser.add_argument("--rediscover", action="store_true", help="Rerun discovery and all downstream passes")
    parser.add_argument("--reverify", action="store_true", help="Reuse discovery; rerun verification and judging")
    parser.add_argument("--rejudge", action="store_true", help="Reuse discovery/verification; rerun only content-quality judging")
    parser.add_argument("--retranscribe", action="store_true", help="Re-extract audio and run Whisper again")
    parser.add_argument("--test-minutes", type=float, help="Process only the first N minutes")
    parser.add_argument("--word-timestamps", action="store_true", help="Enable optional word-level Whisper timestamps")
    parser.add_argument("--skip-analysis", action="store_true", help="Stop after creating the transcript")
    parser.add_argument("--rerun-feedback", action="store_true", help="Rescan a saved VOD with current votes; defaults to latest full transcript")
    parser.add_argument("--all-vods", action="store_true", help="Process each finished, unprocessed VOD currently in the source")
    parser.add_argument("--audio-events-only", action="store_true", help="Analyze/cache audio events only; do not change clip reports, ratings or rankings")
    return parser.parse_args()


def latest_saved_vod(output_root):
    candidates = []
    for transcript in output_root.glob("*/transcript.json"):
        source_path = transcript.parent / "source.json"
        if not source_path.is_file():
            continue
        source = json.loads(source_path.read_text(encoding="utf-8"))
        candidates.append((not bool(source.get("test_minutes")), transcript.stat().st_mtime_ns, transcript.parent))
    if not candidates:
        raise ValueError("No saved transcript found. Run RUN_CONTENTBOT.bat first.")
    return max(candidates)[2]


def unique_output_name(video: Path, fingerprint: str, test_minutes: float | None) -> str:
    safe = "".join(character if character.isalnum() or character in "-_" else "_" for character in video.stem).strip("_")
    name = f"{safe}_{fingerprint}"
    return f"{name}_test_{test_minutes:g}m" if test_minutes else name


def configure_logging(log_dir: Path, output_dir: Path | None = None) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_dir / f"process_{stamp}.log", encoding="utf-8"),
    ]
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(output_dir / "processing.log", encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True,
    )
    # Keep normal output focused on pipeline progress rather than each model-download HTTP request.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def load_transcript(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value.get("segments"), list):
            raise ValueError("missing segments array")
        return value
    except (json.JSONDecodeError, AttributeError, ValueError) as exc:
        raise RuntimeError(f"Existing transcript is invalid ({path}): {exc}. Run with --retranscribe.") from exc


def main() -> int:
    args = parse_args()
    if args.test_minutes is not None and (not math.isfinite(args.test_minutes) or args.test_minutes <= 0):
        raise ValueError("--test-minutes must be greater than zero")
    config_path = args.config if args.config.is_absolute() else (PROJECT_ROOT / args.config)
    config = load_config(config_path.resolve())
    if args.audio_events_only and (args.skip_analysis or args.rerun_feedback or args.all_vods):
        raise ValueError("--audio-events-only cannot be combined with --skip-analysis, --rerun-feedback or --all-vods")
    if args.rerun_feedback:
        if args.video or args.all_vods or args.list_vods or args.skip_analysis or args.test_minutes or args.retranscribe:
            raise ValueError("--rerun-feedback requires saved-output analysis; do not combine it with video, batch, listing or transcription options")
        args.resume_output = args.resume_output or latest_saved_vod(resolve_path(config.paths.output_directory, PROJECT_ROOT))
        args.rediscover = True
    if args.all_vods and (args.video or args.resume_output or args.list_vods or args.force_video):
        raise ValueError("--all-vods cannot be combined with video, resume, listing or force options")
    if args.resume_output and (args.source or args.list_vods or args.force_video):
        raise ValueError("--resume-output cannot be combined with --source, --list-vods or --force-video")
    if args.list_vods and args.video:
        raise ValueError("--list-vods lists a source directory; omit the explicit video")
    configure_logging(PROJECT_ROOT / "logs")
    if args.all_vods:
        output_root = resolve_path(config.paths.output_directory, PROJECT_ROOT)
        scope = (f"test_{args.test_minutes:g}m" if args.test_minutes else "full") + (":transcript" if args.skip_analysis else ":analysis")
        recordings = inspect_recordings(discover(resolve_path(args.source or config.paths.vod_source, PROJECT_ROOT)),
            Registry(output_root / "vod_registry.json"), config.paths.stability_seconds, scope)
        completed = 0
        failed = 0
        for recording in recordings:
            if recording.status not in {"READY", "FAILED"}:
                continue
            job_args = copy.copy(args)
            job_args.video = recording.path
            try:
                with ExitStack() as stack:
                    run_pipeline(job_args, config, stack)
                completed += 1
            except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
                failed += 1
                logging.getLogger("pipeline").error("VOD failed; continuing with the next recording: %s: %s", recording.path.name, exc)
        logging.getLogger("pipeline").info("Batch complete: %d processed, %d failed", completed, failed)
        if completed and not args.skip_analysis:
            try:
                open_review(config_path, output_root)
            except (OSError, RuntimeError) as exc:
                logging.getLogger("pipeline").warning("Review could not open: %s", exc)
        return 1 if failed else 0
    with ExitStack() as stack:
        result = run_pipeline(args, config, stack)
    # Open only after reports and processing status are committed successfully.
    if result == 0 and not args.list_vods and not args.skip_analysis and not args.audio_events_only:
        try:
            open_review(config_path, args.review_output, source_key=getattr(args, "review_source", None))
        except (OSError, RuntimeError) as exc:
            logging.getLogger("pipeline").warning("Processing completed, but review could not open: %s", exc)
    return result


def run_pipeline(args, config, stack: ExitStack) -> int:
    if args.word_timestamps:
        config.whisper.word_timestamps = True
    if args.resume_output:
        if args.video or args.test_minutes or args.retranscribe or args.word_timestamps:
            raise ValueError(
                "--resume-output cannot be combined with a video, --test-minutes, "
                "--retranscribe, or --word-timestamps"
            )
        output_dir = args.resume_output if args.resume_output.is_absolute() else PROJECT_ROOT / args.resume_output
        output_dir = output_dir.resolve()
        if not output_dir.is_dir():
            raise FileNotFoundError(f"Saved output folder not found: {output_dir}")
        configure_logging(PROJECT_ROOT / "logs", output_dir)
        log = logging.getLogger("pipeline")
        transcript_path = output_dir / "transcript.json"
        if not transcript_path.is_file() and not getattr(args, "audio_events_only", False):
            raise FileNotFoundError(f"No transcript.json found in saved output folder: {output_dir}")
        transcript = load_transcript(transcript_path) if transcript_path.is_file() else {"segments": [], "metadata": {}}
        source_path = output_dir / "source.json"
        source = json.loads(source_path.read_text(encoding="utf-8")) if source_path.is_file() else {}
        registry_path = output_dir.parent / "vod_registry.json"
        if registry_path.is_file() and source.get("fingerprint") and not args.skip_analysis and not getattr(args, "audio_events_only", False):
            stack.enter_context(registry_lock(output_dir.parent))
            registry = Registry(registry_path)
            scope = (f"test_{source['test_minutes']:g}m" if source.get("test_minutes") else "full") + ":analysis"
            # Only update tracked outputs; legacy transcript-only resumes remain supported.
            if any(entry.get("output_directory") == str(output_dir) for entry in registry.entries.values()):
                item = Recording(Path(source["source_path"]), source["size_bytes"],
                                 source.get("modification_time_ns", 0), fingerprint=source["fingerprint"])
                stack.enter_context(registry.job(item, output_dir, scope))
        stream_filename = str(source.get("filename") or output_dir.name)
        duration = float(transcript.get("metadata", {}).get("duration", 0))
        if not duration and transcript["segments"]:
            duration = max(float(segment.get("end", 0)) for segment in transcript["segments"])
        log.info("Resuming saved output: %s", output_dir)
        log.info("Original video is not required for analysis-only reruns")
    else:
        output_root = resolve_path(config.paths.output_directory, PROJECT_ROOT).resolve()
        source_dir = resolve_path(args.source or config.paths.vod_source, PROJECT_ROOT)
        # Never place generated artifacts inside the configured read-only source.
        source_resolved = (resolve_path(args.video, PROJECT_ROOT).parent if args.video else source_dir).resolve()
        if output_root == source_resolved or source_resolved in output_root.parents:
            raise ValueError("paths.output_directory must be outside the VOD source directory")
        stack.enter_context(registry_lock(output_root))
        registry = Registry(output_root / "vod_registry.json")
        scope = (f"test_{args.test_minutes:g}m" if args.test_minutes else "full")
        scope += ":audio-events" if getattr(args, "audio_events_only", False) else ":transcript" if args.skip_analysis else ":analysis"
        selected = select_video(args.video, source_dir, PROJECT_ROOT, registry,
                                config.paths.stability_seconds, scope, args.force_video, args.list_vods)
        if args.list_vods:
            return 0
        video = selected.path
        # Reacquire and retain the Windows read guard through processing, closing the
        # observation/selection race and preventing OBS from reopening this file to write.
        stack.enter_context(read_guard(video))
        if snapshot(video) != (selected.size, selected.mtime_ns):
            raise RuntimeError("Selected VOD changed after discovery; retry when recording finishes.")
        stream_filename = video.name
        fingerprint = selected.fingerprint
        output_dir = output_root / unique_output_name(video, fingerprint, args.test_minutes)
        stack.enter_context(registry.job(selected, output_dir, scope))
        configure_logging(PROJECT_ROOT / "logs", output_dir)
        log = logging.getLogger("pipeline")
        log.info("Selected input video: %s", video)
        log.info("Source fingerprint: %s", fingerprint)
        atomic_write_json(
            output_dir / "source.json",
            {
                "filename": video.name,
                "source_path": str(video),
                "size_bytes": selected.size,
                "modification_time_ns": selected.mtime_ns,
                "fingerprint": fingerprint,
                "test_minutes": args.test_minutes,
            },
        )
        require_ffmpeg()

        transcript_path = output_dir / "transcript.json"
        transcript_text_path = output_dir / "transcript.txt"
        audio_path = output_dir / "audio.wav"
        if args.word_timestamps and transcript_path.exists() and not args.retranscribe:
            existing = load_transcript(transcript_path)
            if not existing.get("metadata", {}).get("word_timestamps", False):
                args.retranscribe = True
        if not getattr(args, "audio_events_only", False):
            hardware = choose_hardware(config.whisper)
            print(f"GPU detected: {hardware.gpu_name or 'No compatible CUDA GPU detected'}")
            print(f"Transcription device: {hardware.device.upper()}")
            print(f"Model: {config.whisper.model}")
            print(f"Compute type: {hardware.compute_type}")
            if hardware.warning:
                log.warning(hardware.warning)

        if getattr(args, "audio_events_only", False):
            duration = extract_audio(video, audio_path, config.audio, test_minutes=args.test_minutes, overwrite=False)
            transcript = {"segments": [], "metadata": {"duration": duration}}
        elif transcript_path.exists() and not args.retranscribe:
            log.info("Reusing transcript.json (use --retranscribe to replace it)")
            transcript = load_transcript(transcript_path)
            duration = float(transcript.get("metadata", {}).get("duration", 0))
        else:
            duration = extract_audio(
                video, audio_path, config.audio, test_minutes=args.test_minutes,
                overwrite=args.retranscribe or selected.status == "FAILED",
            )
            transcript = transcribe_audio(
                audio_path, transcript_path, transcript_text_path, config.whisper, hardware
            )
    segments = transcript["segments"]
    log.info("Transcript: %d segments covering %s", len(segments), format_timestamp(duration))
    if args.skip_analysis:
        log.info("Analysis skipped. Transcript is ready in %s", output_dir)
        return 0

    audio_path = output_dir / "audio.wav"
    event_results = {"status": "disabled", "events": []}
    event_audio = None
    if config.audio_events.enabled:
        from src.audio_events import prepare_audio, analyze_events
        source_data = json.loads((output_dir / "source.json").read_text(encoding="utf-8")) if (output_dir / "source.json").is_file() else {}
        try:
            source_video = Path(source_data["source_path"]) if source_data.get("source_path") else None
            if source_video is not None and not source_video.is_file():
                source_video = None
            event_audio, event_source = prepare_audio(output_dir, source_data, config.audio_events, video=source_video)
            event_results = analyze_events(event_audio, output_dir, config.audio_events, event_source)
            if not duration:
                duration = event_results["duration"]
        except Exception as exc:
            log.warning("Audio event detection unavailable; existing transcript/energy pipeline retained: %s", exc)
            event_results = {"status": "unavailable", "error": str(exc), "events": []}
            atomic_write_json(output_dir / "audio_events_status.json", event_results)
    atomic_write_json(output_dir / "audio_events_status.json", {k:v for k,v in event_results.items() if k in {"status", "error", "model", "device", "source", "cache_key"}})
    if getattr(args, "audio_events_only", False):
        log.info("Audio-event-only run: %s; %d events; existing clip reports and ratings unchanged", event_results["status"], len(event_results["events"]))
        return 0 if event_results["status"] in {"complete", "disabled"} else 1
    audio_signals: list[dict[str, Any]] = []
    if event_audio is not None:
        audio_path = event_audio
    if config.audio.analysis_enabled and audio_path.is_file():
        audio_features_path = output_dir / ("reaction_audio_features.json" if audio_path.name != "audio.wav" else "audio_features.json")
        energy_identity = {"path": str(audio_path.resolve()), "size": audio_path.stat().st_size,
                           "mtime_ns": audio_path.stat().st_mtime_ns, "config": asdict(config.audio)}
        import hashlib
        energy_identity["transcript"] = hashlib.sha256(json.dumps(segments, sort_keys=True).encode()).hexdigest()
        audio_features = {}
        if audio_features_path.is_file() and not args.retranscribe:
            audio_features = json.loads(audio_features_path.read_text(encoding="utf-8"))
        if audio_features.get("identity") == energy_identity:
            audio_signals = list(audio_features.get("notable_regions", []))
            log.info("Reusing %d notable audio regions", len(audio_signals))
        else:
            log.info("Analyzing relative audio energy and reaction signals")
            windows = analyze_audio_energy(audio_path, segments, config.audio)
            audio_signals = notable_audio_regions(windows, config.audio.notable_signal_threshold)
            atomic_write_json(audio_features_path, {"identity": energy_identity, "windows": windows, "notable_regions": audio_signals})
            log.info("Audio analysis found %d notable regions", len(audio_signals))
    elif config.audio.analysis_enabled:
        log.warning("audio.wav is unavailable; continuing with transcript-only discovery")

    outcome = analyze_transcript(
        segments,
        config.analysis,
        PROJECT_ROOT / "prompts",
        output_dir / "analysis_cache",
        reanalyze=args.reanalyze,
        rediscover=args.rediscover,
        reverify=args.reverify,
        rejudge=args.rejudge,
        audio_signals=audio_signals,
        audio_events=event_results["events"],
        audio_event_config=config.audio_events,
        stream_duration=duration,
        audio_events_available=event_results["status"] == "complete",
        feedback_store=FeedbackStore(output_dir.parent / "feedback.sqlite3"),
        source=json.loads((output_dir / "source.json").read_text(encoding="utf-8")) if (output_dir / "source.json").is_file()
               else {"filename": stream_filename, "source_path": str(output_dir)},
    )
    atomic_write_json(output_dir / "candidates.json", {"candidates": outcome.candidates})
    atomic_write_json(output_dir / "rejected_candidates.json", {"rejected": outcome.rejected})
    report_path = output_dir / "CLIP_REPORT.md"
    write_report(
        report_path, stream_filename, duration, transcript.get("metadata", {}), len(segments), outcome.candidates,
        outcome.chunk_count, outcome.discovered_count, outcome.verified_count,
    )
    write_rejected_report(output_dir / "REJECTED_CANDIDATES.md", stream_filename, outcome.rejected)
    log.info(
        "Complete: %d discovered, %d verified, %d selected, %d rejected",
        outcome.discovered_count, outcome.verified_count, len(outcome.candidates), len(outcome.rejected),
    )
    log.info("Report: %s", report_path)
    args.review_output = output_dir.parent
    if args.video or args.resume_output:
        review_source = json.loads((output_dir / "source.json").read_text(encoding="utf-8")) if (output_dir / "source.json").is_file() else {}
        args.review_source = str(review_source.get("fingerprint") or review_source.get("source_path") or output_dir)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, FFmpegError, sqlite3.Error) as exc:
        logging.getLogger("pipeline").error("%s", exc)
        raise SystemExit(1)
    except KeyboardInterrupt:
        logging.getLogger("pipeline").error("Processing interrupted. The VOD can be retried.")
        raise SystemExit(130)

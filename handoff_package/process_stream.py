from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from src.analyze import analyze_transcript
from src.audio import FFmpegError, analyze_audio_energy, extract_audio, notable_audio_regions, require_ffmpeg
from src.config import SUPPORTED_VIDEO_EXTENSIONS, load_config
from src.report import write_rejected_report, write_report
from src.transcribe import choose_hardware, transcribe_audio
from src.utils import atomic_write_json, format_timestamp

PROJECT_ROOT = Path(__file__).resolve().parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Turn a local Twitch VOD into a timestamped transcript and ranked clip report."
    )
    parser.add_argument("video", nargs="?", type=Path, help="VOD path; auto-detected when input/ has one supported file")
    parser.add_argument(
        "--resume-output",
        type=Path,
        help="Rerun analysis from an existing output folder; the original VOD is not required",
    )
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "config.json")
    parser.add_argument("--reanalyze", action="store_true", help="Ignore cached LLM chunk results and analyze again")
    parser.add_argument("--rediscover", action="store_true", help="Rerun discovery and all downstream passes")
    parser.add_argument("--reverify", action="store_true", help="Reuse discovery; rerun verification and judging")
    parser.add_argument("--rejudge", action="store_true", help="Reuse discovery/verification; rerun only content-quality judging")
    parser.add_argument("--retranscribe", action="store_true", help="Re-extract audio and run Whisper again")
    parser.add_argument("--test-minutes", type=float, help="Process only the first N minutes")
    parser.add_argument("--word-timestamps", action="store_true", help="Enable optional word-level Whisper timestamps")
    parser.add_argument("--skip-analysis", action="store_true", help="Stop after creating the transcript")
    return parser.parse_args()


def resolve_video(requested: Path | None) -> Path:
    if requested:
        path = requested if requested.is_absolute() else (PROJECT_ROOT / requested)
        path = path.resolve()
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(f"Video file not found: {path}")
        if path.suffix.lower() not in SUPPORTED_VIDEO_EXTENSIONS:
            raise ValueError(f"Unsupported video type {path.suffix}; use: {', '.join(sorted(SUPPORTED_VIDEO_EXTENSIONS))}")
        return path
    input_dir = PROJECT_ROOT / "input"
    matches = sorted(path for path in input_dir.iterdir() if path.is_file() and path.suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS)
    if not matches:
        raise FileNotFoundError("No supported video found in input/. Add one or pass its path on the command line.")
    if len(matches) > 1:
        names = "\n  ".join(path.name for path in matches)
        raise ValueError(f"More than one video is in input/. Specify one explicitly:\n  {names}")
    return matches[0].resolve()


def video_fingerprint(video: Path, block_size: int = 4 * 1024 * 1024) -> str:
    """Identify a VOD quickly without hashing every byte of a multi-hour file."""
    size = video.stat().st_size
    digest = hashlib.sha256()
    digest.update(str(size).encode("ascii"))
    with video.open("rb") as stream:
        digest.update(stream.read(block_size))
        if size > block_size:
            stream.seek(max(0, size - block_size))
            digest.update(stream.read(block_size))
    return digest.hexdigest()[:12]


def unique_output_name(video: Path, fingerprint: str, test_minutes: float | None) -> str:
    safe = "".join(character if character.isalnum() or character in "-_" else "_" for character in video.stem).strip("_")
    name = f"{safe}_{fingerprint}"
    return f"{name}_test_{test_minutes:g}m" if test_minutes else name


def configure_logging(log_dir: Path, output_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_dir / f"process_{stamp}.log", encoding="utf-8"),
        logging.FileHandler(output_dir / "processing.log", encoding="utf-8"),
    ]
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
    if args.test_minutes is not None and args.test_minutes <= 0:
        raise ValueError("--test-minutes must be greater than zero")
    config_path = args.config if args.config.is_absolute() else (PROJECT_ROOT / args.config)
    config = load_config(config_path.resolve())
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
        if not transcript_path.is_file():
            raise FileNotFoundError(f"No transcript.json found in saved output folder: {output_dir}")
        transcript = load_transcript(transcript_path)
        source_path = output_dir / "source.json"
        source = json.loads(source_path.read_text(encoding="utf-8")) if source_path.is_file() else {}
        stream_filename = str(source.get("filename") or output_dir.name)
        duration = float(transcript.get("metadata", {}).get("duration", 0))
        if not duration and transcript["segments"]:
            duration = max(float(segment.get("end", 0)) for segment in transcript["segments"])
        log.info("Resuming from saved transcript: %s", transcript_path)
        log.info("Original video is not required for analysis-only reruns")
    else:
        video = resolve_video(args.video)
        stream_filename = video.name
        fingerprint = video_fingerprint(video)
        output_dir = PROJECT_ROOT / "output" / unique_output_name(video, fingerprint, args.test_minutes)
        configure_logging(PROJECT_ROOT / "logs", output_dir)
        log = logging.getLogger("pipeline")
        log.info("Automatically selected input video: %s", video.name)
        log.info("Source fingerprint: %s", fingerprint)
        atomic_write_json(
            output_dir / "source.json",
            {
                "filename": video.name,
                "source_path": str(video),
                "size_bytes": video.stat().st_size,
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
        hardware = choose_hardware(config.whisper)
        print(f"GPU detected: {hardware.gpu_name or 'No compatible CUDA GPU detected'}")
        print(f"Transcription device: {hardware.device.upper()}")
        print(f"Model: {config.whisper.model}")
        print(f"Compute type: {hardware.compute_type}")
        if hardware.warning:
            log.warning(hardware.warning)

        if transcript_path.exists() and not args.retranscribe:
            log.info("Reusing transcript.json (use --retranscribe to replace it)")
            transcript = load_transcript(transcript_path)
            duration = float(transcript.get("metadata", {}).get("duration", 0))
        else:
            duration = extract_audio(
                video, audio_path, config.audio, test_minutes=args.test_minutes, overwrite=args.retranscribe
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
    audio_signals: list[dict[str, Any]] = []
    if config.audio.analysis_enabled and audio_path.is_file():
        audio_features_path = output_dir / "audio_features.json"
        if audio_features_path.is_file() and not args.retranscribe:
            audio_features = json.loads(audio_features_path.read_text(encoding="utf-8"))
            audio_signals = list(audio_features.get("notable_regions", []))
            log.info("Reusing %d notable audio regions", len(audio_signals))
        else:
            log.info("Analyzing relative audio energy and reaction signals")
            windows = analyze_audio_energy(audio_path, segments, config.audio)
            audio_signals = notable_audio_regions(windows, config.audio.notable_signal_threshold)
            atomic_write_json(audio_features_path, {"windows": windows, "notable_regions": audio_signals})
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
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, ValueError, RuntimeError, FFmpegError) as exc:
        logging.getLogger("pipeline").error("%s", exc)
        raise SystemExit(1)

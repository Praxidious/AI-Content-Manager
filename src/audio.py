from __future__ import annotations

import json
import logging
import math
import shutil
import statistics
import subprocess
import wave
from array import array
from pathlib import Path
from typing import Any

from .config import AudioConfig

LOG = logging.getLogger(__name__)
PROJECT_FFMPEG_BIN = Path(__file__).resolve().parents[1] / "tools" / "ffmpeg" / "bin"


class FFmpegError(RuntimeError):
    pass


def require_ffmpeg() -> tuple[str, str]:
    local_ffmpeg = PROJECT_FFMPEG_BIN / "ffmpeg.exe"
    local_ffprobe = PROJECT_FFMPEG_BIN / "ffprobe.exe"
    ffmpeg = shutil.which("ffmpeg") or (str(local_ffmpeg) if local_ffmpeg.is_file() else None)
    ffprobe = shutil.which("ffprobe") or (str(local_ffprobe) if local_ffprobe.is_file() else None)
    if not ffmpeg or not ffprobe:
        missing = " and ".join(name for name, value in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)) if not value)
        raise FFmpegError(
            f"Missing {missing}. Run setup_ffmpeg.bat once, or install FFmpeg on PATH."
        )
    return ffmpeg, ffprobe


def media_duration(path: Path, ffprobe: str) -> float:
    command = [
        ffprobe, "-v", "error", "-show_entries", "format=duration",
        "-of", "json", str(path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode:
        raise FFmpegError(f"ffprobe could not inspect {path.name}: {result.stderr.strip()}")
    try:
        return float(json.loads(result.stdout)["format"]["duration"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise FFmpegError(f"Could not determine the duration of {path.name}") from exc


def extract_audio(
    video_path: Path,
    audio_path: Path,
    config: AudioConfig,
    test_minutes: float | None = None,
    overwrite: bool = False,
) -> float:
    ffmpeg, ffprobe = require_ffmpeg()
    duration = media_duration(video_path, ffprobe)
    if audio_path.exists() and not overwrite:
        LOG.info("Reusing existing audio: %s", audio_path)
        return min(duration, test_minutes * 60) if test_minutes else duration
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(video_path)]
    if test_minutes:
        command.extend(["-t", str(test_minutes * 60)])
    command.extend([
        "-vn", "-ac", "1", "-ar", str(config.sample_rate),
        "-c:a", config.codec, str(audio_path),
    ])
    LOG.info("Extracting mono %s Hz speech audio with FFmpeg", config.sample_rate)
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode:
        audio_path.unlink(missing_ok=True)
        raise FFmpegError(f"FFmpeg audio extraction failed:\n{result.stderr.strip()}")
    return min(duration, test_minutes * 60) if test_minutes else duration


def _transcript_features(segments: list[dict[str, Any]], start: float, end: float) -> dict[str, Any]:
    overlapping = [s for s in segments if float(s["end"]) >= start and float(s["start"]) <= end]
    text = " ".join(str(s.get("text", "")) for s in overlapping).lower()
    duration = max(0.1, end - start)
    word_count = len(text.split())
    laughter_terms = ("haha", "hahaha", "hehe", "laugh", "lmao", "lol")
    shock_terms = ("oh my god", "what the fuck", "no no", "holy shit", "what are you doing")
    return {
        "speech_density": round(min(10.0, word_count / duration * 3.0), 1),
        "transcript_segment_density": round(min(10.0, len(overlapping) / duration * 12.0), 1),
        "possible_laughter_text": any(term in text for term in laughter_terms),
        "possible_shock_text": any(term in text for term in shock_terms),
    }


def analyze_audio_energy(
    audio_path: Path,
    segments: list[dict[str, Any]],
    config: AudioConfig,
) -> list[dict[str, Any]]:
    """Measure relative audio activity in rolling windows without extra dependencies.

    Laughter and shouting flags are deliberately described as possibilities. They combine
    acoustic shape with transcript hints and are review signals, not factual labels.
    """
    if not config.analysis_enabled or not audio_path.is_file():
        return []
    source = wave.open(str(audio_path), "rb")
    if source.getsampwidth() != 2:
        source.close()
        raise FFmpegError("Audio-energy analysis requires 16-bit PCM WAV audio")
    sample_rate = source.getframerate()
    channels = source.getnchannels()
    frame_count = source.getnframes()
    window_size = max(1, int(sample_rate * config.analysis_window_seconds))
    hop_size = max(1, int(sample_rate * config.analysis_hop_seconds))
    # 2 kHz is ample for loudness/envelope features and keeps multi-hour VODs practical.
    analysis_stride = max(1, sample_rate // 2000)
    rms_values: list[float] = []
    acoustic: list[dict[str, float]] = []
    for offset in range(0, max(1, frame_count - window_size + 1), hop_size):
        source.setpos(offset)
        raw = source.readframes(window_size)
        block = array("h")
        block.frombytes(raw)
        block = block[::channels * analysis_stride]
        if not block:
            continue
        square_sum = sum(value * value for value in block)
        rms = math.sqrt(square_sum / len(block))
        peak = max(abs(value) for value in block)
        # Energy variation across short slices helps identify pulsed, laugh-like activity.
        slice_size = max(1, (sample_rate // analysis_stride) // 5)
        slices = []
        for index in range(0, len(block), slice_size):
            part = block[index:index + slice_size]
            if part:
                slices.append(math.sqrt(sum(v * v for v in part) / len(part)))
        modulation = statistics.pstdev(slices) / max(1.0, statistics.fmean(slices)) if len(slices) > 1 else 0.0
        rms_values.append(rms)
        acoustic.append({"rms": rms, "peak": float(peak), "modulation": modulation})
    source.close()
    if not acoustic:
        return []
    baseline_radius = max(2, int(config.baseline_seconds / config.analysis_hop_seconds / 2))
    output: list[dict[str, Any]] = []
    for index, values in enumerate(acoustic):
        nearby = rms_values[max(0, index - baseline_radius):min(len(rms_values), index + baseline_radius + 1)]
        baseline = statistics.median(nearby) or 1.0
        db_change = 20.0 * math.log10(max(values["rms"], 1.0) / baseline)
        relative_energy = max(0.0, min(10.0, 5.0 + db_change / 2.0))
        start = index * config.analysis_hop_seconds
        end = start + config.analysis_window_seconds
        transcript = _transcript_features(segments, start, end)
        sudden = db_change >= 4.5
        possible_laughter = bool(
            transcript["possible_laughter_text"]
            or (values["modulation"] >= 0.55 and relative_energy >= 6.5 and transcript["speech_density"] >= 2.0)
        )
        possible_shouting = bool(
            transcript["possible_shock_text"]
            or (relative_energy >= 8.0 and values["peak"] >= 28000 and transcript["speech_density"] >= 2.0)
        )
        output.append({
            "start": round(start, 3), "end": round(end, 3),
            "rms": round(values["rms"], 1), "relative_loudness_db": round(db_change, 2),
            "relative_energy": round(relative_energy, 1),
            "speech_density": transcript["speech_density"],
            "transcript_segment_density": transcript["transcript_segment_density"],
            "rapid_speech_activity": transcript["transcript_segment_density"] >= 6.5,
            "possible_laughter": possible_laughter, "possible_shouting": possible_shouting,
            "sudden_energy_change": sudden,
        })
    return output


def notable_audio_regions(windows: list[dict[str, Any]], threshold: float = 6.5) -> list[dict[str, Any]]:
    """Return compact contiguous regions worth presenting to discovery and editors."""
    notable = [w for w in windows if float(w.get("relative_energy", 0)) >= threshold
               or w.get("possible_laughter") or w.get("possible_shouting") or w.get("sudden_energy_change")]
    regions: list[dict[str, Any]] = []
    for window in notable:
        if regions and float(window["start"]) <= float(regions[-1]["end"]) + 2.0:
            region = regions[-1]
            region["end"] = max(float(region["end"]), float(window["end"]))
            for key in ("relative_energy", "speech_density", "transcript_segment_density"):
                region[key] = max(float(region[key]), float(window.get(key, 0)))
            for key in ("rapid_speech_activity", "possible_laughter", "possible_shouting", "sudden_energy_change"):
                region[key] = bool(region[key] or window.get(key))
        else:
            regions.append({key: window[key] for key in (
                "start", "end", "relative_energy", "speech_density", "transcript_segment_density",
                "rapid_speech_activity", "possible_laughter", "possible_shouting", "sudden_energy_change"
            )})
    return regions

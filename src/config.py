from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
except ImportError:  # Allows config validation before optional dependencies are installed.
    def load_dotenv(*_args: Any, **_kwargs: Any) -> bool:
        return False


SUPPORTED_VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".webm"}


@dataclass(slots=True)
class WhisperConfig:
    model: str = "large-v3"
    device: str = "auto"
    compute_type: str = "float16"
    cpu_compute_type: str = "int8"
    language: str | None = None
    beam_size: int = 5
    vad_filter: bool = True
    word_timestamps: bool = False


@dataclass(slots=True)
class AudioConfig:
    sample_rate: int = 16_000
    codec: str = "pcm_s16le"
    analysis_enabled: bool = True
    analysis_window_seconds: float = 2.0
    analysis_hop_seconds: float = 1.0
    baseline_seconds: float = 60.0
    notable_signal_threshold: float = 6.5


@dataclass(slots=True)
class AudioEventConfig:
    enabled: bool = True
    model: str = "yamnet_onnx"
    model_path: str = "tools/models/yamnet/yamnet.onnx"
    labels_path: str = "tools/models/yamnet/yamnet_class_map.csv"
    device: str = "auto"
    cpu_threads: int = 4
    confidence_threshold: float = .45
    mixed_confidence_threshold: float = .65
    relative_loudness_db: float = 6.0
    loudness_threshold_relaxation: float = .10
    background_margin: float = .10
    baseline_seconds: float = 60.0
    merge_gap_seconds: float = 1.5
    minimum_event_seconds: float = .35
    maximum_event_seconds: float = 30.0
    pre_roll_seconds: float = 7.0
    post_roll_seconds: float = 12.0
    chunk_seconds: float = 60.0
    microphone_track: int | None = None  # zero-based AUDIO track ordinal, not global stream index
    enabled_classes: list[str] = field(default_factory=lambda: [
        "laughter", "giggle", "scream", "shout", "gasp", "crying", "whoop", "groan", "grunt", "group_reaction"])


@dataclass(slots=True)
class AnalysisConfig:
    analytics_vision_model: str = ""
    audience_feedback_enabled: bool = False
    audience_platform: str = "instagram_reels"
    audience_account: str = "default"
    personal_preferences_enabled: bool = True
    provider: str = "openai"
    model: str = "gpt-4.1-mini"
    api_key_env: str = "OPENAI_API_KEY"
    base_url_env: str = "OPENAI_BASE_URL"
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_context_tokens: int = 10_240
    ollama_max_output_tokens: int = 2_048
    chunk_minutes: float = 20
    chunk_overlap_minutes: float = 2
    max_chunk_characters: int = 24_000
    max_audio_regions_per_request: int = 12
    maximum_audio_seed_candidates: int = 24
    minimum_score: float = 7.0
    minimum_clip_duration_seconds: float = 8.0
    maximum_clip_duration_seconds: float = 45.0
    context_expansion_seconds: float = 30.0
    must_review_entertainment: float = 8.3
    must_review_clipability: float = 7.5
    good_content_entertainment: float = 6.5
    human_review_min_score: float = 5.5
    include_weak_suggestions: bool = False
    include_stream_moments: bool = False
    maximum_clips: int = 12
    temperature: float = 0.2
    request_timeout_seconds: int = 180
    max_retries: int = 3


@dataclass(slots=True)
class PathsConfig:
    vod_source: str = "input"
    output_directory: str = "output"
    stability_seconds: float = 30.0


@dataclass(slots=True)
class AppConfig:
    paths: PathsConfig = field(default_factory=PathsConfig)
    whisper: WhisperConfig = field(default_factory=WhisperConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    analysis: AnalysisConfig = field(default_factory=AnalysisConfig)
    audio_events: AudioEventConfig = field(default_factory=AudioEventConfig)


def _known_fields(cls: type, values: dict[str, Any]) -> dict[str, Any]:
    names = cls.__dataclass_fields__.keys()
    return {key: value for key, value in values.items() if key in names}


def load_config(path: Path) -> AppConfig:
    load_dotenv(path.parent / ".env")
    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from exc
    cfg = AppConfig(
        paths=PathsConfig(**_known_fields(PathsConfig, raw.get("paths", {}))),
        whisper=WhisperConfig(**_known_fields(WhisperConfig, raw.get("whisper", {}))),
        audio=AudioConfig(**_known_fields(AudioConfig, raw.get("audio", {}))),
        analysis=AnalysisConfig(**_known_fields(AnalysisConfig, raw.get("analysis", {}))),
        audio_events=AudioEventConfig(**_known_fields(AudioEventConfig, raw.get("audio_events", {}))),
    )
    for name in ("vod_source", "output_directory"):
        value = getattr(cfg.paths, name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"paths.{name} must be a nonempty path")
    interval = cfg.paths.stability_seconds
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not math.isfinite(interval) or interval <= 0:
        raise ValueError("paths.stability_seconds must be a finite number greater than zero")
    if cfg.analysis.chunk_minutes <= 0:
        raise ValueError("analysis.chunk_minutes must be greater than zero")
    if type(cfg.analysis.audience_feedback_enabled) is not bool:
        raise ValueError("analysis.audience_feedback_enabled must be true or false")
    for name in ("audience_platform", "audience_account"):
        if not isinstance(getattr(cfg.analysis, name), str) or not getattr(cfg.analysis, name).strip():
            raise ValueError(f"analysis.{name} must be nonempty text")
    if not isinstance(cfg.analysis.analytics_vision_model, str):
        raise ValueError("analysis.analytics_vision_model must be text")
    if not 0 <= cfg.analysis.chunk_overlap_minutes < cfg.analysis.chunk_minutes:
        raise ValueError("chunk_overlap_minutes must be >= 0 and less than chunk_minutes")
    if not 0 <= cfg.analysis.minimum_score <= 10:
        raise ValueError("analysis.minimum_score must be from 0 to 10")
    if cfg.analysis.maximum_clip_duration_seconds < cfg.analysis.minimum_clip_duration_seconds:
        raise ValueError("maximum_clip_duration_seconds must be >= minimum_clip_duration_seconds")
    if cfg.analysis.max_audio_regions_per_request <= 0:
        raise ValueError("analysis.max_audio_regions_per_request must be greater than zero")
    if cfg.analysis.maximum_audio_seed_candidates < 0:
        raise ValueError("analysis.maximum_audio_seed_candidates must be zero or greater")
    if cfg.audio.analysis_window_seconds <= 0 or cfg.audio.analysis_hop_seconds <= 0:
        raise ValueError("audio analysis window and hop must be greater than zero")
    validate_audio_events(cfg.audio_events)
    return cfg


def validate_audio_events(cfg):
    if type(cfg.enabled) is not bool or cfg.model != "yamnet_onnx" or cfg.device not in ("auto", "cpu", "cuda"):
        raise ValueError("audio_events requires enabled=true/false, model=yamnet_onnx and device=auto/cpu/cuda")
    for name in ("confidence_threshold", "mixed_confidence_threshold", "loudness_threshold_relaxation", "background_margin"):
        value = getattr(cfg, name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError(f"audio_events.{name} must be between 0 and 1")
    for name in ("relative_loudness_db", "baseline_seconds", "merge_gap_seconds", "minimum_event_seconds",
                 "maximum_event_seconds", "pre_roll_seconds", "post_roll_seconds", "chunk_seconds"):
        value = getattr(cfg, name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError(f"audio_events.{name} must be finite and nonnegative")
    if not .96 <= cfg.chunk_seconds <= 300 or cfg.baseline_seconds < 1 or not 0 < cfg.minimum_event_seconds <= cfg.maximum_event_seconds:
        raise ValueError("Invalid audio event chunk/baseline/duration limits")
    if type(cfg.cpu_threads) is not int or not 1 <= cfg.cpu_threads <= 64:
        raise ValueError("audio_events.cpu_threads must be 1–64")
    if cfg.microphone_track is not None and (type(cfg.microphone_track) is not int or cfg.microphone_track < 0):
        raise ValueError("audio_events.microphone_track must be a zero-based audio track number or null")
    allowed = {"laughter", "giggle", "scream", "shout", "gasp", "crying", "whoop", "groan", "grunt", "group_reaction", "sigh", "snort"}
    if not isinstance(cfg.enabled_classes, list) or any(x not in allowed for x in cfg.enabled_classes):
        raise ValueError("Unknown audio event class")
    if not all(isinstance(getattr(cfg, key), str) and getattr(cfg, key).strip() for key in ("model_path", "labels_path")):
        raise ValueError("Audio event model and label paths must be nonempty strings")


def provider_api_key(cfg: AnalysisConfig) -> str | None:
    return os.getenv(cfg.api_key_env) if cfg.api_key_env else None


def provider_base_url(cfg: AnalysisConfig) -> str | None:
    return os.getenv(cfg.base_url_env) if cfg.base_url_env else None

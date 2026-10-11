from __future__ import annotations

import json
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
class AnalysisConfig:
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
class AppConfig:
    whisper: WhisperConfig = field(default_factory=WhisperConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    analysis: AnalysisConfig = field(default_factory=AnalysisConfig)


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
        whisper=WhisperConfig(**_known_fields(WhisperConfig, raw.get("whisper", {}))),
        audio=AudioConfig(**_known_fields(AudioConfig, raw.get("audio", {}))),
        analysis=AnalysisConfig(**_known_fields(AnalysisConfig, raw.get("analysis", {}))),
    )
    if cfg.analysis.chunk_minutes <= 0:
        raise ValueError("analysis.chunk_minutes must be greater than zero")
    if not 0 <= cfg.analysis.chunk_overlap_minutes < cfg.analysis.chunk_minutes:
        raise ValueError("chunk_overlap_minutes must be >= 0 and less than chunk_minutes")
    if not 0 <= cfg.analysis.minimum_score <= 10:
        raise ValueError("analysis.minimum_score must be from 0 to 10")
    if cfg.analysis.maximum_clip_duration_seconds < cfg.analysis.minimum_clip_duration_seconds:
        raise ValueError("maximum_clip_duration_seconds must be >= minimum_clip_duration_seconds")
    if cfg.audio.analysis_window_seconds <= 0 or cfg.audio.analysis_hop_seconds <= 0:
        raise ValueError("audio analysis window and hop must be greater than zero")
    return cfg


def provider_api_key(cfg: AnalysisConfig) -> str | None:
    return os.getenv(cfg.api_key_env) if cfg.api_key_env else None


def provider_base_url(cfg: AnalysisConfig) -> str | None:
    return os.getenv(cfg.base_url_env) if cfg.base_url_env else None

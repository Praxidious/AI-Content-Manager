from __future__ import annotations

import logging
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .config import WhisperConfig
from .utils import atomic_write_json, format_timestamp

LOG = logging.getLogger(__name__)
_CUDA_DLL_HANDLES: list[Any] = []


def configure_project_cuda_dlls() -> list[Path]:
    """Expose NVIDIA runtime wheels to Windows DLL resolution."""
    if sys.platform != "win32":
        return []
    site_packages = Path(sys.prefix) / "Lib" / "site-packages"
    candidates = [
        site_packages / "nvidia" / "cuda_runtime" / "bin",
        site_packages / "nvidia" / "cublas" / "bin",
        site_packages / "nvidia" / "cudnn" / "bin",
    ]
    available = [path for path in candidates if path.is_dir()]
    if not available:
        return []
    current_path = os.environ.get("PATH", "")
    prefixes = [str(path) for path in available if str(path).lower() not in current_path.lower()]
    if prefixes:
        os.environ["PATH"] = os.pathsep.join(prefixes + [current_path])
    if hasattr(os, "add_dll_directory"):
        known = {getattr(handle, "path", None) for handle in _CUDA_DLL_HANDLES}
        for path in available:
            if str(path) not in known:
                _CUDA_DLL_HANDLES.append(os.add_dll_directory(str(path)))
    return available


@dataclass(slots=True)
class HardwareChoice:
    requested_device: str
    device: str
    compute_type: str
    gpu_name: str | None
    warning: str | None = None


def _gpu_name() -> str | None:
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().splitlines()[0]
    except (FileNotFoundError, subprocess.SubprocessError):
        pass
    return None


def choose_hardware(config: WhisperConfig) -> HardwareChoice:
    configure_project_cuda_dlls()
    requested = config.device.lower()
    if requested not in {"auto", "cuda", "cpu"}:
        raise ValueError("whisper.device must be auto, cuda, or cpu")
    cuda_count = 0
    cuda_error: str | None = None
    detected_gpu = _gpu_name() if requested != "cpu" else None
    if requested != "cpu":
        try:
            import ctranslate2
            cuda_count = ctranslate2.get_cuda_device_count()
        except (ImportError, RuntimeError, OSError) as exc:
            cuda_error = str(exc)
    if cuda_count > 0:
        return HardwareChoice(requested, "cuda", config.compute_type, detected_gpu or "NVIDIA GPU")
    if requested == "cuda":
        reason = cuda_error or "CTranslate2 reported zero CUDA devices"
        warning = (
            f"CUDA was explicitly requested but is unavailable ({reason}). Falling back to CPU. "
            "Confirm the NVIDIA driver works with `nvidia-smi`; see the README CUDA section."
        )
    elif requested == "auto":
        reason = cuda_error or "CTranslate2 reported zero CUDA devices"
        warning = f"CUDA unavailable ({reason}); transcription will use CPU and will be much slower."
    else:
        warning = None
    return HardwareChoice(requested, "cpu", config.cpu_compute_type, detected_gpu, warning)


def transcribe_audio(
    audio_path: Path,
    json_path: Path,
    text_path: Path,
    config: WhisperConfig,
    hardware: HardwareChoice,
) -> dict[str, Any]:
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError("faster-whisper is not installed. Run: pip install -r requirements.txt") from exc

    LOG.info("Loading Whisper model %s on %s (%s)", config.model, hardware.device, hardware.compute_type)
    try:
        model = WhisperModel(config.model, device=hardware.device, compute_type=hardware.compute_type)
    except Exception as exc:
        if hardware.device == "cuda":
            raise RuntimeError(
                "Whisper could not initialize CUDA. Confirm your NVIDIA driver is current and the CUDA "
                "runtime libraries required by CTranslate2 are available. Run with whisper.device='cpu' "
                f"to force CPU. Original error: {exc}"
            ) from exc
        raise

    segments_iter, info = model.transcribe(
        str(audio_path),
        language=config.language,
        beam_size=config.beam_size,
        vad_filter=config.vad_filter,
        word_timestamps=config.word_timestamps,
    )
    segments: list[dict[str, Any]] = []
    lines: list[str] = []
    LOG.info(
        "GPU transcription is now running (audio duration %s). Progress appears every 25 segments.",
        format_timestamp(info.duration),
    )
    for index, segment in enumerate(segments_iter):
        text = segment.text.strip()
        item: dict[str, Any] = {
            "id": index,
            "start": round(float(segment.start), 3),
            "end": round(float(segment.end), 3),
            "start_display": format_timestamp(segment.start),
            "end_display": format_timestamp(segment.end),
            "text": text,
        }
        if config.word_timestamps and segment.words:
            item["words"] = [
                {
                    "start": round(float(word.start), 3),
                    "end": round(float(word.end), 3),
                    "word": word.word,
                    "probability": round(float(word.probability), 4),
                }
                for word in segment.words
            ]
        segments.append(item)
        lines.append(f"[{item['start_display']}] {text}")
        if index and index % 25 == 0:
            LOG.info("Transcribed %d segments (through %s)", index, item["end_display"])

    result = {
        "metadata": {
            "model": config.model,
            "device": hardware.device,
            "compute_type": hardware.compute_type,
            "language": info.language,
            "language_probability": round(float(info.language_probability), 4),
            "duration": round(float(info.duration), 3),
            "duration_after_vad": round(float(getattr(info, "duration_after_vad", info.duration)), 3),
            "word_timestamps": config.word_timestamps,
            "hardware": asdict(hardware),
        },
        "segments": segments,
    }
    atomic_write_json(json_path, result)
    text_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    LOG.info("Saved %d transcript segments", len(segments))
    return result

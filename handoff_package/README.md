# JustGlad2BHere ContentBot — Phase 1

A local Windows pipeline that turns a downloaded Twitch VOD into a GPU-generated timestamped transcript and a short, ranked list of moments worth reviewing. It does **not** download streams, edit clips, or upload anything.

```text
local VOD → FFmpeg audio → faster-whisper → timestamped transcript
          → relative audio analysis → entertainment discovery → evidence verification → content-quality judging
          → deduplication → CLIP_REPORT.md + REJECTED_CANDIDATES.md
```

The defaults target an RTX 4070: `large-v3`, CUDA, and `float16`. Expect the first run to download several GB of Whisper model files. A full-stream run also makes one LLM request per configured transcript chunk, so begin with `--test-minutes 10`.

## Everyday workflow

After completing the one-time setup below, normal use is intentionally simple:

1. Put **one** video in the `input` folder.
2. Double-click `process_stream.bat`.
3. Wait for the window to say processing is complete.
4. Open the newly created folder under `output` and read `CLIP_REPORT.md`.

When you are finished, delete the video from `input` and put the next one there. Supported files are `.mp4`, `.mkv`, `.mov`, and `.webm`. The batch file detects the filename automatically—there is nothing to type or select. If the folder is empty or contains multiple supported videos, it stops with a clear message instead of guessing.

Each distinct source video receives a short fingerprint in its output folder name, such as `Friday_Stream_a1b2c3d4e5f6`. This prevents an old transcript from being reused if a replacement VOD has the same filename. Running the exact same file again uses the same folder and resumes completed work.

On the first run, if FFmpeg is missing, `process_stream.bat` asks whether it may download the official Windows essentials build. Choosing **Y** downloads it from the FFmpeg-recommended Gyan Windows builds, verifies the publisher's SHA-256 checksum, and installs `ffmpeg.exe` and `ffprobe.exe` under `tools\ffmpeg\bin`. It does not modify the system PATH.

The batch file also detects a missing Python virtual environment or GPU libraries. With your permission, it creates `.venv` and installs faster-whisper, CTranslate2, CUDA 12 runtime, cuBLAS, and cuDNN 9 there. The GPU dependency download is approximately 1.5 GB and happens only during initial setup.

## Quick Windows setup

### 1. Install Python

Install 64-bit Python 3.11 or 3.12. During the python.org install, enable **Add Python to PATH**. Then open a new PowerShell window:

```powershell
python --version
```

### 2. Install FFmpeg

The easiest option is to skip this manual step: the first run of `process_stream.bat` will offer to install a verified, project-local copy automatically.

For a system-wide installation, use Windows Package Manager if available:

Using Windows Package Manager:

```powershell
winget install Gyan.FFmpeg
```

Open a new terminal after installation and verify both programs:

```powershell
ffmpeg -version
ffprobe -version
```

### 3. Create the virtual environment

From this project directory:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
```

If PowerShell blocks activation, either run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once or use `.venv\Scripts\python.exe` in place of `python` below.

### 4. Install dependencies

```powershell
pip install -r requirements-windows-gpu.txt
```

This installs faster-whisper/CTranslate2 plus project-local NVIDIA CUDA 12, cuBLAS, and cuDNN 9 runtime wheels. PyTorch and a system-wide CUDA Toolkit are not required. The pipeline adds these `.venv` DLL directories only for its own process. Keep the NVIDIA display driver current.

Verify the driver and CTranslate2 detection:

```powershell
nvidia-smi
python -c "import ctranslate2; print('CUDA devices:', ctranslate2.get_cuda_device_count())"
```

The second command should print `CUDA devices: 1` (or more). At pipeline startup you should see:

```text
GPU detected: NVIDIA GeForce RTX 4070
Transcription device: CUDA
Model: large-v3
Compute type: float16
```

If CUDA is unavailable, the program prints a prominent warning and uses CPU `int8`; it never falls back silently. CPU transcription of a long VOD will be much slower.

### 5. Configure the LLM

The simplest cloud setup uses OpenAI:

```powershell
Copy-Item .env.example .env
notepad .env
```

Set `OPENAI_API_KEY` in `.env`. Secrets are loaded from the environment and `.env` is ignored by Git. Never put a key in `config.json` or source code.

Local Ollama analysis is currently configured by default on this PC:

```json
"provider": "ollama",
"model": "qwen3:8b"
```

The pipeline starts Ollama's local API when needed. `qwen3:8b` is about 5.2 GB. This project requests a 10K context, which fits the bounded transcript chunks and discovery response while remaining practical on the 12 GB RTX 4070. A strong cloud model may still select clips more reliably. `openai-compatible` is also accepted: set `OPENAI_BASE_URL` in `.env` for a compatible endpoint.

### 6. Add a VOD and run

Put one `.mp4`, `.mkv`, `.mov`, or `.webm` file in `input\`, then double-click:

```text
process_stream.bat
```

That is the normal workflow. The command-line equivalent is optional:

```powershell
python process_stream.py "input\my_stream.mp4"
```

When exactly one supported video is in `input\`, the path is optional. For the simple batch workflow, keep only one video there.

### 7. Start with a short test

```powershell
python process_stream.py "input\my_stream.mp4" --test-minutes 10
```

The test has its own output directory, so it cannot be confused with a full-stream transcript.

## Output

For `my_stream.mp4`, results are written under a fingerprinted folder such as `output\my_stream_a1b2c3d4e5f6\`:

```text
audio.wav                 mono 16 kHz PCM speech audio (~115 MB/hour)
audio_features.json       rolling relative-energy features and notable reaction regions
source.json               source filename, size, and safety fingerprint
transcript.txt            human-readable timestamped transcript
transcript.json           millisecond-precision segments and metadata
candidates.json           machine-readable ranked candidates
rejected_candidates.json  machine-readable rejection audit trail
CLIP_REPORT.md            editor-friendly report
REJECTED_CANDIDATES.md    candidates removed at verification, scoring, or deduplication
processing.log            log for this stream
analysis_cache\discovery\  resumable high-recall discovery results
analysis_cache\verification\ transcript-grounding decisions
analysis_cache\judging\    strict content-quality decisions
```

The report separates **🔥 must review**, **🎬 good content**, and **👀 check the video**. Every candidate includes content tags, entertainment/clipability/growth-potential scores, transcript evidence, audio signals, what to inspect in the video, and a suggested editing angle. Timestamps refer to the original VOD.

The three scores are independent. Entertainment measures humor, emotion, reaction, personality, chaos, and surprise. Clipability measures whether an approximately 8–45 second edit can be cut cleanly. Growth potential estimates cold-audience appeal; moderate growth potential does not disqualify an entertaining creator moment.

## Resume and rerun behavior

Normal runs reuse existing expensive artifacts:

```powershell
# Reuse transcript and completed analysis chunks
python process_stream.py "input\my_stream.mp4"

# Reuse transcript, but request every analysis chunk again
python process_stream.py "input\my_stream.mp4" --reanalyze

# Rerun only candidate discovery and every downstream pass
python process_stream.py "input\my_stream.mp4" --rediscover

# Reuse discovery, but rerun evidence verification and judging
python process_stream.py "input\my_stream.mp4" --reverify

# Reuse discovery and verification, but rerun only content-quality judging
python process_stream.py "input\my_stream.mp4" --rejudge

# Re-extract audio and replace transcript; analysis cache fingerprints change automatically
python process_stream.py "input\my_stream.mp4" --retranscribe

# Transcribe only (useful for isolating GPU/setup issues)
python process_stream.py "input\my_stream.mp4" --skip-analysis

# Optional, larger transcript JSON with word-level timestamps
python process_stream.py "input\my_stream.mp4" --word-timestamps --retranscribe
```

You may delete the VOD from `input` and still rerun any analysis pass from its saved output folder:

```powershell
python process_stream.py --resume-output "output\my_stream_a1b2c3d4e5f6" --rejudge
python process_stream.py --resume-output "output\my_stream_a1b2c3d4e5f6" --reverify
python process_stream.py --resume-output "output\my_stream_a1b2c3d4e5f6" --rediscover
```

Completed stage responses are saved immediately. If a request fails, the next normal run reuses earlier completed work. `--reanalyze` ignores every analysis cache, while the narrower flags only invalidate their named stage and its downstream dependencies. Atomic JSON writes prevent interrupted writes from masquerading as completed transcripts or cache entries.

## Configuration reference

Edit `config.json`:

- `whisper.model`: default `large-v3`; `distil-large-v3` is a faster English-focused alternative.
- `whisper.device`: `auto`, `cuda`, or `cpu`.
- `whisper.compute_type`: default GPU type, normally `float16` on the RTX 4070.
- `whisper.cpu_compute_type`: fallback type, normally `int8`.
- `whisper.language`: `null` for detection or a language code such as `en`.
- `whisper.word_timestamps`: optional word timing; segment timing is always enabled.
- `analysis.provider`: `openai`, `openai-compatible`, or `ollama`.
- `analysis.model`: provider model name.
- `analysis.api_key_env` / `base_url_env`: names of environment variables, not secret values.
- `analysis.chunk_minutes`: target time window (default 20).
- `analysis.chunk_overlap_minutes`: boundary overlap (default 2).
- `analysis.max_chunk_characters`: safety ceiling when speech is dense.
- `analysis.minimum_clip_duration_seconds`: discards fragments too short to be useful review cuts.
- `audio.analysis_enabled`: enables rolling relative-energy and reaction-signal analysis.
- `audio.analysis_window_seconds` / `analysis_hop_seconds`: rolling audio window and step sizes.
- `audio.baseline_seconds`: surrounding interval used to establish relative loudness.
- `audio.notable_signal_threshold`: minimum relative-energy score for an ordinary region to be surfaced.
- `analysis.maximum_clip_duration_seconds`: caps recommended short-form cuts (default 45 seconds).
- `analysis.context_expansion_seconds`: transcript context supplied around each discovery (default 30 seconds).
- `analysis.must_review_entertainment` / `must_review_clipability`: thresholds for the top review tier.
- `analysis.good_content_entertainment`: minimum entertainment score for non-review-dependent content.
- `analysis.human_review_min_score`: minimum score retained when transcript evidence suggests video review may resolve uncertainty.
- `analysis.include_stream_moments`: retain ordinary stream moments in the final report when `true`.
- `analysis.maximum_clips`: maximum globally ranked candidates in the report.
- `analysis.ollama_context_tokens`: local model context allocation; currently 10,240 for practical RTX 4070 VRAM use.
- `analysis.ollama_max_output_tokens`: maximum local structured response size.

Chunks preserve absolute timestamps. Discovery combines transcript and notable relative-audio regions and intentionally favors recall. Strong laughter, shouting, chaos, and sudden energy can be kept for video review even when the transcript does not explain the visual cause. Evidence verification corrects unsupported descriptions without rejecting a real emotional reaction merely because visuals are unknown. The content-quality judge then favors precision, and overlapping moments are removed globally. Prompts live in `prompts\candidate_discovery.txt`, `prompts\evidence_verifier.txt`, and `prompts\content_quality_judge.txt`. Cache fingerprints include prompts, model, provider, schema, transcript, and audio input.

## Troubleshooting

- **FFmpeg missing:** reopen the terminal after `winget install`, then verify `ffmpeg -version`. The app needs both `ffmpeg` and `ffprobe` on PATH.
- **CUDA count is zero:** run `nvidia-smi`, update the NVIDIA driver, restart Windows, and retest the one-line CTranslate2 check above.
- **CUDA DLL/load error:** the program reports the original initialization error. Check the faster-whisper/CTranslate2 compatibility notes for the installed version; the NVIDIA driver alone may not supply every required runtime library.
- **Out of GPU memory:** close GPU-heavy apps, or try `distil-large-v3`. Keep `float16`; lowering the analysis chunk size does not reduce Whisper VRAM.
- **No API key:** create `.env`, ensure the configured `api_key_env` matches its variable name, and restart the command.
- **Bad LLM JSON:** the pipeline retries each chunk. Persistent failures are logged and completed earlier chunks remain cached.
- **No candidates:** inspect `REJECTED_CANDIDATES.md` and confirm that `audio_features.json` was generated before changing thresholds.

## Developer checks

The offline tests do not require FFmpeg, Whisper model downloads, a GPU, or an API key:

```powershell
python -m unittest discover -v
python -m py_compile process_stream.py src\audio.py src\transcribe.py src\analyze.py src\report.py src\config.py
```

The pipeline intentionally stops at human-review ranges. Twitch EventSub/VOD retrieval, chat alignment, automatic cutting, vertical reframing, dashboard review, and performance feedback remain future modules.

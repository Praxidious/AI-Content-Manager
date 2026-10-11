# JustGlad2BHere ContentBot — Phase 1

A local Windows pipeline that turns a downloaded Twitch VOD into a GPU-generated timestamped transcript and a short, ranked list of moments worth reviewing. It does **not** download streams, edit clips, or upload anything.

```text
local VOD → FFmpeg audio → faster-whisper → timestamped transcript
          → relative audio analysis → entertainment discovery → evidence verification → content-quality judging
          → deduplication → CLIP_REPORT.md + REJECTED_CANDIDATES.md
```

The defaults target an RTX 4070: `large-v3`, CUDA, and `float16`. Expect the first run to download several GB of Whisper model files. A full-stream run also makes one LLM request per configured transcript chunk, so begin with `--test-minutes 10`.

Long-stream audio evidence is bounded before it reaches Ollama: each model request includes at most `analysis.max_audio_regions_per_request` strongest overlapping regions (default 12), and the full run adds at most `analysis.maximum_audio_seed_candidates` audio-only candidates (default 24), selected for strength and time coverage. This prevents noisy multi-hour recordings from overflowing the model context or creating hundreds of downstream judge calls. Full audio features remain available in `audio_features.json` and the review database.

## Everyday workflow

After completing the one-time setup below, normal use is intentionally simple:

1. Configure `paths.vod_source` in `config.json` to point at your OBS recordings share.
2. Double-click `RUN_CONTENTBOT.bat`.
3. Pick a number: open the grading website, run all unprocessed VODs, choose one VOD by name, run the newest VOD, rescan with feedback, or list readiness. Enter **0** to exit.
4. For a single VOD, pick its filename from the numbered list, then choose normal processing or a rescan with current feedback. Enter **0** to go back. After successful analysis, the trainer opens automatically in your default browser, and the launcher returns to its menu.

`process_stream.bat` remains available to run the newest eligible VOD directly. Passing command-line arguments to `RUN_CONTENTBOT.bat` also bypasses the menu and forwards them to the original launcher. Use `OPEN_TRAINER.bat` whenever you only want to open the trainer and review saved clips without processing another VOD. The trainer launcher starts the local server in the background, reuses it if it is already running, and opens `http://localhost:5050`.

Supported files are `.mp4`, `.mkv`, `.mov`, and `.webm` (case-insensitive). Discovery scans the directory itself, not subfolders. Multiple recordings are expected. Each invocation processes one eligible recording; it is not a continuous watcher. If none is eligible, it prints the reason and exits.

Each distinct source video receives the existing short fingerprint in its output folder name, such as `Friday_Stream_a1b2c3d4e5f6`. Completed recordings are skipped automatically. Use an explicit path with `--force-video` to revisit a completed recording; existing transcription and AI caches are still reused unless you request the corresponding rerun flags.

Selecting a single VOD opens the grading website filtered to that recording. Refreshing and moving between clips preserve that filter, and an empty queue never fills with another VOD's clips. Batch processing and opening the grading website directly show the full library.

### Network share configuration

For a share displayed in Windows as `\\STREAM-PC\Stream VODs`, add this top-level section to `config.json` alongside `whisper`, `audio`, and `analysis`:

```json
"paths": {
  "vod_source": "\\\\STREAM-PC\\Stream VODs",
  "output_directory": "output",
  "stability_seconds": 30
}
```

JSON requires **each backslash to be doubled**: the two leading UNC backslashes become four in JSON. Replace `STREAM-PC` with your streaming PC's actual name. This project's configuration is already set to the supplied share. Credentials belong in Windows share access/Credential Manager, never in this JSON. Confirm the share opens under the same Windows account running the bot.

Mapped drives (`"Z:\\Recordings"`) and local directories (`"C:\\Recordings"` or `"input"`) also work. Relative paths are based on the project directory. If `paths` is omitted, discovery defaults to `input` and output to `output`. Keep `output_directory` on the ContentBot PC and outside the source folder. Audio, transcripts, caches, reports and `vod_registry.json` live there. FFmpeg reads the original VOD directly; the bot does not copy, rename, move, delete or write to source recordings.

```powershell
# Inspect readiness without starting FFmpeg, Whisper or AI analysis
.\.venv\Scripts\python.exe process_stream.py --list-vods

# Temporarily override the configured directory
.\.venv\Scripts\python.exe process_stream.py --source "\\STREAM-PC\Stream VODs" --list-vods

# Process only the first ten minutes of the newest eligible network recording
.\.venv\Scripts\python.exe process_stream.py --source "\\STREAM-PC\Stream VODs" --test-minutes 10

# An explicit video always overrides automatic discovery
python process_stream.py "C:\some\video.mkv"
python process_stream.py "\\STREAM-PC\Stream VODs\video.mkv"

# Revisit a completed VOD, optionally rerunning the AI analysis
python process_stream.py "\\STREAM-PC\Stream VODs\video.mkv" --force-video --reanalyze
```

### Readiness and processing history

`--list-vods` displays `READY`, `RECORDING/UNSTABLE`, `PROCESSED` or `FAILED`, with reasons where applicable. Status is for the requested run scope: add `--test-minutes 10` or `--skip-analysis` to inspect that scope. Failed/interrupted jobs are retryable; completed jobs require `--force-video`. Force never bypasses recording safety checks.

All candidates are observed over a shared `stability_seconds` interval (default 30, finite and greater than zero). A file must exist, be nonempty, have unchanged size and modification time, and not have been modified within that interval. Temporary names such as `*.part`, `*.tmp`, `*.remux.mp4` are excluded or marked unstable. Missing files, permission failures and disconnected shares produce readable messages. Retry when the recording/remux or network problem is resolved.

On Windows, including Windows SMB shares, an additional read handle denies write/delete sharing. An existing OBS writer prevents that handle from opening; the selected file is checked again and the handle is held throughout processing. This also prevents another writer from reopening it during the run. On other operating systems only metadata/readability checks apply. `READY` means stable and readable, not that every media byte has been validated; existing FFprobe/FFmpeg checks still reject damaged or unfinished containers when processing starts.

The local atomic registry records filename, source path, size, modification time in nanoseconds, existing content fingerprint, UTC processing date, output directory, scope and `processing`/`completed`/`failed` status. The existing fingerprint reads size plus the first/last 4 MiB, not the entire VOD. Renaming an unchanged file does not evade tracking; different content with the same filename gets a different fingerprint. This sampled fingerprint is not a full-file integrity hash. MKV and remuxed MP4 versions generally have different fingerprints and are separate recordings.

Short tests and transcript-only runs have separate registry entries and do not mark full analysis completed. A Ctrl+C records failure; a killed process leaves `processing`, which is retryable on the next invocation. A local OS lock prevents simultaneous ingestion runs sharing the same output root and releases automatically on process exit. Retrying a failed extraction rebuilds audio when no transcript exists, avoiding reuse of interrupted audio. Preserve `vod_registry.json` with your outputs; old outputs made before this feature are not automatically imported into the registry. `--resume-output` continues to work without the VOD or share.

On the first run, if FFmpeg is missing, `process_stream.bat` asks whether it may download the official Windows essentials build. Choosing **Y** downloads it from the FFmpeg-recommended Gyan Windows builds, verifies the publisher's SHA-256 checksum, and installs `ffmpeg.exe` and `ffprobe.exe` under `tools\ffmpeg\bin`. It does not modify the system PATH.

The batch file also detects a missing Python virtual environment or GPU libraries. With your permission, it creates `.venv` and installs faster-whisper, CTranslate2, CUDA 12 runtime, cuBLAS, and cuDNN 9 there. The GPU dependency download is approximately 1.5 GB and happens only during initial setup.

## Review clips and teach ContentBot your taste

### Apply votes and rescan streams

Double-click **RERUN_WITH_FEEDBACK.bat** after voting. It selects the latest saved full-VOD transcript (a short test is used only if no full transcript exists), rescans every transcript chunk using current editor feedback, reruns verification/judging, and opens the trainer when finished. Audio extraction and transcription are reused. To choose a particular saved VOD:

```powershell
.\RERUN_WITH_FEEDBACK.bat --resume-output "output\YOUR_SAVED_FOLDER"
```

Feedback is pooled across all saved streams. Discovery receives common positive/rejection reasons plus at most four relevant rated examples. Changed votes refresh the discovery cache as well as preference inference. If all votes are negative or Maybe, the bot uses the rejection evidence without inventing positive preferences. Matching/overlapping moments from the same VOD already rated Bad or Never Again are rejected before spending more verification/judging calls. After a successful rescan, candidates absent from that scan are retired from the queue; their feedback remains stored. Partial test rescans do not retire moments beyond their test limit.

Double-click **PROCESS_ALL_STREAMS.bat** to process every finished, unprocessed recording present in the configured source at launch. It checks stability, skips completed/active recordings, processes sequentially, continues to other VODs after a failure, and opens the trainer once at the end if any VOD succeeded. This is a finite batch, not a scheduled watcher. Each new stream uses the accumulated feedback. The trainer still shows only the top 20 qualifying S/A clips of at least 10 seconds across the library.

Transcript chunking advances by segment, including across silent gaps and character-limited windows. It evaluates all saved transcript segments; visual-only action without speech or notable audio can still be missed because this pipeline does not analyze video frames. Feedback improves the search criteria, but better clips are not guaranteed—review the next results to establish whether they match your taste better.

From the project directory:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe review_clips.py
```

With the virtual environment activated, `python review_clips.py` is equivalent. Open **http://localhost:5050**. Use `--port 5051` if that port is occupied, or `--config path\to\config.json` for another configuration. The app binds only to this PC, with debug mode disabled. Stop it with Ctrl+C.

For normal use, double-click `OPEN_TRAINER.bat`; this starts or reuses the background trainer and opens it in your default browser. The equivalent command is `python review_clips.py --open-browser`.

After a successful full analysis or analysis resume, ContentBot automatically starts the review server if needed and opens the website in your default browser. An existing server for the same output directory is reused. Listing VODs, transcript-only runs and failed processing runs do not open the website. Background server messages are saved to `logs/review_server.log`; a browser/server startup problem does not mark completed processing as failed.

The trainer shows at most **20 unreviewed clips across the saved library**, ordered by final rank score (or entertainment score for older clips), then clipability. Only **S/A-tier clips at least 10 seconds long** qualify. B-tier, rejected, duplicate and unscored moments are excluded. The queue may contain fewer than 20 clips; it never fills empty places with mediocre moments. All saved feedback remains available for summaries and preference examples.

Existing candidate and rejection reports under the configured output directory are imported at startup. New processing runs save discovered, judged and rejected moments automatically. Refresh the queue to see newly saved clips; restart the app to import additional legacy outputs. Every opening or refresh selects the best 20 remaining unreviewed clips. All five saved ratings exclude a clip from future queues, including after rerunning the same VOD. Voting leaves the current clip visible so you can finish adding reasons and confirm with Submit feedback; navigation removes rated clips from the current set. Refresh loads the next best qualifying set. Feedback stays saved for summaries and preference learning.

Click **Prepare preview** to generate a short H.264/AAC MP4 from the original VOD. This reads local, mapped-drive or UNC sources directly and saves only the candidate range under `output/review_previews`. Generation is on demand, limited to three minutes per clip and one preview at a time. Fingerprints and Windows read guards protect against changed recordings and active writers. If footage is unavailable, transcript and rating controls still work. Cached previews play without the source PC. The video timer starts at zero within the preview; source timestamps appear below it. Playback uses Flask's [conditional/range file responses](https://flask.palletsprojects.com/en/stable/api/#flask.send_file).

The editor shows transcript, source timestamps, AI tier and scores, audio features and detected reasons/tags.

| Key | Rating | Preference example strength |
| --- | --- | --- |
| 1 | S-TIER (`s_tier`) | Strong positive, weight 2 |
| 2 | GOOD (`good`) | Positive, weight 1 |
| 3 | MAYBE (`maybe`) | Saved label; excluded from polarized examples |
| 4 | BAD (`bad`) | Negative, weight 1 |
| 5 | NEVER AGAIN (`never_again`) | Strong negative, weight 2 |

Use **left/right arrows** or Previous/Next to navigate and **Space** to play/pause a loaded preview. Shortcuts are inactive while editing text or selecting reason tags. Votes and reason tags save automatically. Click **Submit feedback** below the optional note to save the clip's rating, reasons and note together. The button shows “Submitting…” while saving and then a timestamped “Feedback saved successfully” confirmation after the server confirms the write. A rating is required to submit; failures are shown with a retry message. Ratings and reasons remain editable. Mixed reasons are allowed, such as a great reaction that requires too much context.

The summary shows total clips reviewed, rating distribution, and common positive/rejection reason tags. Reason-only entries remain unrated and are excluded from learning examples and reviewed totals.

### Persistent identity and feedback

Back up `<output_directory>/feedback.sqlite3` with your outputs. It stores source metadata, transcript excerpts, AI scores, audio features, tags/reasons, tier, timestamps and creation/update dates, plus current feedback and append-only feedback events. Original VODs are never changed. Unit-test ratings use temporary databases.

A persistent `clip_id` derives from the VOD fingerprint and core start/end rounded to milliseconds. Reimporting or rejudging that moment updates AI metadata while retaining its ID, creation date and human feedback. Suggested edit-boundary changes do not change the ID; a different core range creates a new moment. Different VODs receive different IDs at identical timestamps. Legacy outputs without fingerprints use the saved source path as fallback identity. The original `candidate_id` continues to support existing analysis caches.

Each feedback edit saves a snapshot of candidate features so future training can use what the editor actually rated, even if AI metadata is regenerated later.

### How preferences affect ranking

No Ollama weights are retrained or fine-tuned. Before judging, the bot retrieves at most **two relevant liked and two relevant disliked examples**, using tag overlap and transcript/description similarity. S-tier and Never Again have twice the retrieval weight of Good and Bad; this strength is also stated in the preference prompt. Unrated/Maybe clips, unrelated examples and the current clip itself are excluded.

The generic judge retains its existing prompt and scoring. A separate cached assessment uses the selected examples with the configured provider (Ollama in this project):

```text
generic_quality_score = existing entertainment_score (0–10)
personal_preference_score = -2 to +2
final_rank_score = generic_quality_score + personal_preference_score
```

Final rank orders surviving clips, retaining clipability/time tie-breakers. Existing generic tier rejection, evidence checks and deduplication stay in force. Never Again is a strong preference signal, not a universal automatic ban. With no relevant feedback, or if preference assessment fails, the adjustment is zero. Set `analysis.personal_preferences_enabled` to `false` to disable preference inference while continuing to collect feedback.

Preference cache keys include the chosen examples, ratings, reasons and notes. Feedback changes refresh affected discovery and preference calls; generic judging remains reusable when its candidate evidence is unchanged. Apply feedback to an existing output with:

```powershell
.\.venv\Scripts\python.exe process_stream.py --resume-output "output\YOUR_SAVED_FOLDER"
```

Export current labeled feature snapshots for future statistical modeling:

```powershell
.\.venv\Scripts\python.exe review_clips.py --export-feedback "output\feedback-training.jsonl"
```

The versioned JSONL contains clip/source identity, labels, weights, reasons, notes, feature snapshots and rating dates. Export does not train a model. Review data stays local; preference examples go to the configured analysis provider, so switching from local Ollama to a cloud provider also changes where examples are processed.

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

Configure the VOD source directory as described above, then double-click:

```text
process_stream.bat
```

That is the normal workflow. The command-line equivalent is optional:

```powershell
python process_stream.py "input\my_stream.mp4"
```

The path is optional: the bot selects the newest stable, unprocessed recording from the configured source.

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
python process_stream.py "input\my_stream.mp4" --force-video --reanalyze

# Rerun only candidate discovery and every downstream pass
python process_stream.py "input\my_stream.mp4" --force-video --rediscover

# Reuse discovery, but rerun evidence verification and judging
python process_stream.py "input\my_stream.mp4" --force-video --reverify

# Reuse discovery and verification, but rerun only content-quality judging
python process_stream.py "input\my_stream.mp4" --force-video --rejudge

# Re-extract audio and replace transcript; analysis cache fingerprints change automatically
python process_stream.py "input\my_stream.mp4" --force-video --retranscribe

# Transcribe only (useful for isolating GPU/setup issues)
python process_stream.py "input\my_stream.mp4" --skip-analysis

# Optional, larger transcript JSON with word-level timestamps
python process_stream.py "input\my_stream.mp4" --force-video --word-timestamps --retranscribe
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

The pipeline intentionally stops at human-review ranges. Twitch EventSub/VOD retrieval, chat alignment, automatic cutting, and vertical reframing remain future modules.

## Performance Feedback / Analytics

Independent non-verbal sound detection is now available alongside transcription. See [Non-verbal audio events](AUDIO_EVENTS.md) for the local YAMNet setup, isolated microphone-track selection, configuration, testing, cache behavior and limitations. Existing ranking and performance-feedback formulas are unchanged.

Open **Performance / Analytics** from the Editor’s desk (or `/analytics` on the same local review server). Restart an already-running review server after upgrading.

1. Choose a stored clip, platform and account/channel, and enter the posting and analytics capture times. Times are entered in the browser’s local timezone and stored as UTC. Use a consistent account name; accounts and platforms never share a baseline.
2. Upload 1–6 PNG/JPEG/WebP screenshots (up to 5 MB each) from the **same post and capture time**, then choose **Extract & review**. Or use manual entry, including when the provider is unavailable.
3. Review/edit metric keys, labels, values, units and original text alongside the screenshots. Unknown statistics are supported, including text. Missing values stay missing. Duplicate metric keys must be resolved before saving. Percent values use `12.5` for 12.5%; watch times use seconds.
4. Preview calculations, check the confirmation box and save. Only this action writes post/snapshot/image data. Drafts exist in process memory for one hour, at most four at once; discarding or restarting drops them. Confirm retries are idempotent.
5. For later snapshots, choose the **existing post**. Presets support 1, 6, 24, 72 and 168 hours; any actual capture timestamp is accepted. Presets are timestamp helpers, not scheduled collection. Post history shows all snapshots and linked clip features.

### Local vision

This installation uses `analysis.analytics_vision_model: "qwen3-vl:4b-instruct"` with the existing Ollama provider. `analysis.model` remains the text model used by the clipping pipeline. To install the vision model on another PC with Ollama running:

```powershell
python tools/setup_analytics_vision.py
```

The model is downloaded from the [official Ollama library](https://ollama.com/library/qwen3-vl:4b-instruct). Image requests use the configured local Ollama endpoint; they do not use OpenAI when `analysis.provider` is `ollama`. If using an OpenAI/OpenAI-compatible provider instead, the same API-key/base-URL configuration is reused, and the selected model must support vision. The adapter follows the [OpenAI image input format](https://developers.openai.com/api/docs/guides/images-vision). Extraction errors leave no analytics in the database; manual entry remains available. Model values are suggestions that always require human confirmation.

### Storage and preservation

`src/migrations/002_performance.sql` adds platforms, posts, snapshots and screenshot evidence to the **existing** `output/feedback.sqlite3`. Migration is transactional and idempotent. It never rewrites `clips`, `feedback`, or `feedback_events`; clip UUIDs and the existing preference export format are unchanged. Back up the database before deploying on another PC. Do not use an old version of the app after migrating; old versions correctly reject the newer schema.

Each post references the existing clip ID and freezes the full metadata, source, human rating/reasons and training feature snapshot available at first import. Later rescans or rating changes cannot rewrite this historical evidence. History also exposes the current clip/rating separately. Optional posted-edit annotations cover duration, hook/payoff timing, category, game, emotion, gameplay event, reaction intensity, laughter, speakers and editing style. Unmeasured features remain unknown. Audio laughter is explicitly a detector signal, not verified laughter; existing audio energy and AI scores are reused.

Snapshots retain raw platform metrics (including labels, displayed text, units, screenshot indices and extraction confidence), original extraction output, and confirmed screenshot bytes. Derived values live in a separate JSON column with formulas. Watch-duration ratio is **not** completion rate; engagement/view rate is computed only when all its defined inputs exist. Original platform engagement rates are never replaced. Zero-view snapshots do not generate divide-by-zero rates. One post/capture timestamp is unique. To add a platform, insert its stable key and label into `performance_platforms`; metric storage, imports, UI and learning use that registry without schema changes.

### Comparisons and guarded learning

The dashboard compares the same **platform + account + metric + capture age**. It chooses one closest snapshot per post within ±25% of the requested age (at least a 15-minute tolerance). Each post uses up to 30 earlier independent clips posted within its preceding 90 days, with a minimum of 5, and excludes later-captured evidence and its own clip. This historical trailing median makes outcomes relative to account performance at the time, rather than fixed view cutoffs. The displayed score is `log2((value + 1)/(median + 1))`; skip rate is reversed because lower is better. For other/custom metrics, higher is assumed better. These comparisons do not control for all changes in reach, topic, paid promotion or platform measurement definitions.

Feature groups show normalized outcomes, independent clip counts, coverage and approximate 95% intervals. Results are exploratory correlations, not causal claims or calibrated prediction probabilities. Repeated snapshots/posts of the same clip cannot inflate independent sample counts. Features not already measured can be entered as posted-edit annotations; the module does not fabricate hook/payoff timings, speaker labels or emotion.

The pipeline saves an independent `audience_prediction` alongside its existing `personal_preference_score` (Wes preference). It retrieves similar clips within the configured platform/account and predicts normalized **24-hour views**. Other outcomes are available for dashboard exploration. Historical normalized outcomes are used directly; no model is automatically retrained.

Ranking influence defaults to **off** via `analysis.audience_feedback_enabled: false`. With explicit configuration enabled, it still requires **30 normalized independent historical clips**, **10 similar clips**, and an approximate interval excluding zero. Even then, the adjustment is capped at **±0.5 points**. No audience prediction changes quality tiers, evidence requirements, rejection decisions, existing human ratings or preference prompts. This is deliberately a bounded first feedback loop, not a calibrated or heavily weighted audience model. Set `analysis.audience_platform` and `analysis.audience_account` to select the target audience for a pipeline run.

Run `python -m unittest discover -s tests -v` for offline migration, confirmation, validation, baseline isolation, growth normalization, duplicate/repost and learning-gate tests. `tests/fixtures/performance_analytics.png` is synthetic analytics for a live local vision smoke test, not real account performance.

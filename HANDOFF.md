# Technical handoff — Squirrel Content / JustGlad2BHere ContentBot

Snapshot: 2026-09-14. Root: `C:\Users\Weston\Documents\Codex Projects\Squirrel Content`.

## Scope and evidence

This is Phase 1 of a local Windows Python pipeline for turning downloaded Twitch VODs into timestamped transcripts and ranked human-review ranges. It prioritizes Squirrel's funny, chaotic, welcoming personality over routine gameplay. It does **not** create video clips, download streams or upload content. No application code was changed for this handoff.

Source, prompts, configuration, tests, historical logs and local tools were inspected. No earlier development conversation was available in this task; app task discovery, including archived tasks, did not locate a prior development task for this project. Historical discussion and approval of future features cannot be reconstructed reliably. Decisions below are evidenced by implementation/comments/README, not invented conversation history. No `.git` directory is visible at the root, so no commit/branch identity is available.

**Code takes precedence over README:** current judgment uses model-assigned S/A/B/REJECT tiers. Several old numeric threshold settings do nothing. The current discovery prompt is selective, despite README's high-recall description.

## What works and verification limits

- On 2026-09-14, `.venv\Scripts\python.exe -m unittest discover -v` passed all **10 tests**. They cover timestamps, source fingerprints, overlapping chunks, excerpts, deduplication, caching, malformed-discovery retries, quota error explanation, report tiers and synthetic audio spikes. They use fake LLMs and do not establish actual clip quality.
- Existing environment: **Python 3.14.3**, although README recommends 3.11/3.12.
- Current `choose_hardware` returns CUDA, float16, **NVIDIA GeForce RTX 4070**, no warning. This verifies detection, not a fresh Whisper inference.
- Bundled ffmpeg and ffprobe execute and report **9.0.1-essentials_build-www.gyan.dev**.
- GET `http://127.0.0.1:11434/api/tags` returned 200 and installed **qwen3:8b**, 8.2B, GGUF Q4_K_M, size 5,225,388,164 bytes. Digest: `500a1f067a9f782620b40bee6f7b0c89e17ae61f686b92c24933e4ca4b2b8b41`. Model reports capacity 40960 tokens; application requests 10240. No fresh generation was run.
- Ollama's executable exists in `%LOCALAPPDATA%\Programs\Ollama`, but launching `ollama list` was access-denied in this handoff environment. HTTP inventory succeeded; that denial does not establish an application/server failure.
- `.env` has a nonempty OPENAI_API_KEY and empty OPENAI_BASE_URL. Secrets are intentionally omitted; validity/billing untested. Ollama does not use the key.
- **input contains only `.gitkeep`; output is empty.** No VOD/transcript/report/cache is currently available for a real end-to-end test or resume.

Historical August 22–23 logs demonstrate earlier successful runs: short transcription saved 160 segments; full transcription saved 3965; a full run selected 12 candidates across 15 chunks. Latest log `process_20260823_011115.log` ends at 01:23:10 with 92 discovered, 92 verified, 12 selected, 39 rejected. It still says “Viral judge”, whereas current code says “Content quality judge”; do not claim it validates current S/A/B logic. Referenced output folders no longer exist.

Historical failures: missing FFmpeg; missing faster-whisper/CTranslate2; missing API key; provider connection errors; Ollama 600-second timeout and HTTP 500; malformed/truncated discovery JSON. Setup, diagnostics and retries now mitigate these, but no full current run proves all resolved.

## Architecture and entry points

No frontend, database, web service, queue or packaging framework. Processing is sequential and state is filesystem artifacts.

1. `process_stream.bat` is the everyday double-click entry: changes to project directory, checks setup, asks before dependency downloads, calls `.venv\Scripts\python.exe process_stream.py`, forwards arguments, pauses on success/failure.
2. `process_stream.py` parses CLI, loads config and dotenv, resolves one supported input video, fingerprints it and creates an output folder. Supported suffixes: mp4/mkv/mov/webm. Zero/multiple auto-detected videos fail clearly. Relative CLI paths resolve against project root.
3. Source fingerprint: SHA-256 over file size plus first and last 4 MiB, shortened to 12 hex characters. Output is `<sanitized_stem>_<fingerprint>`, with `_test_<N>m` for short tests. This avoids most same-filename stale reuse without hashing an entire VOD.
4. Write source metadata; require FFmpeg; choose hardware. Reuse transcript if present unless forced; otherwise extract/reuse audio and transcribe.
5. `--resume-output` instead loads transcript and optional source metadata from a saved folder, bypassing VOD, FFmpeg and hardware setup. Missing WAV allows transcript-only analysis. `--skip-analysis` stops before audio feature analysis.
6. Compute/reuse audio features, chunk transcript, run discovery per chunk, normalize results, add audio seeds, deduplicate.
7. Verify each discovered candidate; judge supported candidates; cache every successful response immediately.
8. Reject, normalize edit bounds, deduplicate globally, rank, truncate to maximum count, attach transcript excerpts; write JSON and Markdown reports.

Additional entry points: `setup_ffmpeg.bat` calls `setup_ffmpeg.ps1`; `python -m unittest discover -v` runs tests.

| File | Purpose |
| --- | --- |
| process_stream.py | CLI, input resolution, fingerprinting, logging, resume orchestration, final artifacts |
| src/config.py | Slotted config dataclasses, limited validation, dotenv, key/base URL lookup |
| src/audio.py | Tool discovery, ffprobe duration, FFmpeg extraction, rolling audio features and region merging |
| src/transcribe.py | Windows CUDA DLL registration, device selection, faster-whisper transcription and serialization |
| src/analyze.py | Provider adapters/protocol, schemas, chunks, model messages, cache/retry, validation, audio seeds, verification/judging, bounds/ranking/dedup |
| src/report.py | S/A/B human-review Markdown and rejection audit |
| src/utils.py | HH:MM:SS[.mmm] conversion, atomic JSON writes |
| src/__init__.py; tests/__init__.py | Package markers |
| tests/test_pipeline.py | Ten offline unit tests and fake provider |
| prompts/*.txt | Three complete system prompts, reproduced in appendix |
| config.json | Actual runtime values, reproduced in appendix |
| requirements*.txt | Ranged dependencies; actual installed snapshot follows |
| setup_ffmpeg.* | Download/checksum/extract/copy FFmpeg bootstrap |
| .env; .env.example | Local secrets and template |
| README.md | Setup/workflow documentation with noted stale scoring claims |
| .gitignore | Excludes environment, Python caches, secrets, input/output/logs, bundled binaries; retains specified .gitkeep files |

## Transcription and external requirements

Requires 64-bit Python, pip/venv, FFmpeg and ffprobe. Windows launchers use cmd/choice/where/PowerShell. GPU use requires NVIDIA driver and CTranslate2-compatible CUDA/cuBLAS/cuDNN libraries. `nvidia-smi` supplies diagnostic GPU naming. PyTorch/system CUDA Toolkit are not required by this source. First-time packages and models require network downloads.

Uses **faster-whisper / CTranslate2**, not Ollama or a cloud transcription API. Expected Whisper model: **large-v3**; auto language, beam_size 5, VAD true, word timestamps false. No initial prompt/hotwords/custom Whisper prompt. Model location/download uses library/Hugging Face defaults. `distil-large-v3` is a README alternative, not a current required model.

Windows adds `.venv/Lib/site-packages/nvidia/{cuda_runtime,cublas,cudnn}/bin` to process PATH and `os.add_dll_directory`. Global handles keep those paths alive. CUDA detection success selects float16; unavailable CUDA (even explicitly requested) falls back to CPU int8 with warning. If CUDA is detected but model initialization fails, code raises an explanatory error rather than falling back again.

Transcription logs every 25 segments, stores all segments in memory, then writes transcript.json atomically and transcript.txt normally. No incremental transcription checkpoint. JSON metadata includes model/device/compute type, language/probability, duration/VAD duration, word timing and hardware selection. Segments have id/start/end/display timestamps/text; optional words include timing and probability. Numeric seconds are rounded to milliseconds. Text format is `[HH:MM:SS] text`. No speaker identification/diarization exists.

## FFmpeg

`require_ffmpeg()` prefers PATH for each executable separately, then project `tools/ffmpeg/bin/*.exe`. PATH can override bundled versions or mix installations.

Probe: `ffprobe -v error -show_entries format=duration -of json <video>`.

Extract: `ffmpeg -hide_banner -loglevel error -y -i <video> [-t <test_minutes*60>] -vn -ac 1 -ar 16000 -c:a pcm_s16le <audio.wav>`; rate/codec are configurable. Existing WAV is reused by existence unless overwrite requested. Normal command failure removes WAV. No cutting, captions, vertical filters or frame analysis.

Installer fetches `https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip` and `.sha256`, validates SHA-256, extracts in a GUID temporary directory, copies ffmpeg/ffprobe locally, cleans temporary files. No system PATH change. URL is floating, not version-pinned. “Official” launcher language refers to the selected Gyan third-party Windows build.

## Ollama and other providers

All three stages use **qwen3:8b** at `http://127.0.0.1:11434`. No separate judge, embedding or vision model. Ollama health checks GET `/api/version`. On Windows it tries hidden/detached `%LOCALAPPDATA%/Programs/Ollama/ollama.exe serve` if unavailable, polling 20 times with .5s sleeps and up-to-2s request timeouts (actual wait can exceed the reported 10 seconds). Non-Windows must start server separately. It does not install Ollama or pull models. A custom remote URL does not change the local auto-start binding.

POST `/api/chat`: model, stream=false, format=stage JSON schema, think=false; system and user messages; temperature=.2, num_ctx=10240, num_predict=min(configured 2048, stage cap). Caps: discovery 2048, verification 512, judging 1024. Config timeout=600 seconds. Context/output bounds and disabled thinking reflect documented RTX 4070 12 GB constraints.

`openai` and `openai-compatible` both use OpenAI Chat Completions. Key is required; base URL defaults to `https://api.openai.com/v1`. Sends model, temperature, system/user messages, `response_format={"type":"json_object"}`. **Schema argument is ignored in this adapter**, and no explicit token cap is set. Dataclass default provider/model is openai/gpt-4.1-mini; actual config overrides to ollama/qwen3:8b. Change both provider and model when switching.

Up to max_retries=3 total attempts, backoff 2 then 4 seconds. OpenAI-style quota/auth/permission/client errors except 429 are nonretryable. Requests HTTPError does not expose the checked status_code field, so Ollama 4xx may still retry. Invalid JSON responses are saved as `_attempt_<N>_invalid.txt`; malformed discovery gets the shorter retry message in appendix. Valid JSON is not locally schema-validated.

## Audio detection and scoring

Standard-library wave/array/math/statistics; 16-bit PCM required. Default window 2s, hop 1s, sample stride to roughly 2 kHz. RMS/peak and .2s envelope modulation are measured. Local median RMS over roughly ±30s is baseline. Relative dB=`20*log10(max(rms,1)/baseline)`; energy=clamp(`5+dB/2`,0,10).

Words and segments overlapping each window supply speech_density=min(10,words/duration*3), segment_density=min(10,segments/duration*12). Possible laughter: substring haha/hahaha/hehe/laugh/lmao/lol OR modulation>=.55, energy>=6.5 and speech_density>=2. Possible shouting: shock phrase (oh my god / what the fuck / no no / holy shit / what are you doing) OR energy>=8, peak>=28000 and speech_density>=2. Sudden change means dB>=4.5; rapid activity means segment_density>=6.5. These are review heuristics, not trained event classifiers.

Notable windows have energy>=6.5 or laughter/shouting/sudden flags. Merge gaps <=2s; retain maxima and OR flags. Only compact notable regions go to models.

Discovery chunks target 20 minutes, 2-minute overlap, 24000 characters (segment text length+20 each); preserve absolute times. Prompt asks for 0–8 distinctive moments without filler. Normalization requires increasing core bounds within chunk±15s and normalized quote contained in normalized **whole-chunk** text. Normalization uses lowercase ASCII alphanumeric tokens. It does not locate quote at claimed core time.

Audio seeds: possible laughter/shouting OR energy>=8.5 plus sudden change. Require transcript within ±4s, keep first 300 evidence characters, pad audio region ±4s, set audio_priority and needs_video_review. No global seed count cap.

Dedup: core overlap / shorter core duration >=.45 OR both endpoints within 12s. Sort entertainment descending (zero before judging), then time. Merge review/audio-priority flags, tags and audio detection reason. Pre-verification dedup is not rejection-audited; final dedup is.

Verifier sees candidate plus core±30s transcript and core-overlapping audio. UNSUPPORTED is rejected; PARTIALLY_SUPPORTED gets corrected description. Confidence clamps 0–10. Judge supplies independent entertainment, clipability, growth scores; S/A/B/REJECT and reason; title, setup/escalation/payoff, editing angle, video instructions, confidence and recommended bounds. Scores clamp 0–10 and round to one decimal. There is **no numeric composite or threshold-based tier assignment**.

Missing/invalid tier becomes REJECT. REJECT always removes candidate. major_rejection also removes it unless audio_priority; audio_priority never overrides REJECT tier. Bounds clamp to context, try to preserve core and expand to minimum 8s/trim to maximum 45s. Minimum not guaranteed at boundaries; an oversized core can lose its start. Rank by entertainment descending, clipability descending, core start ascending; keep top 12. Tier/growth do not affect sorting. Markdown groups global ranks into S exceptional, A definitely worth editing, B maybe usable.

## Artifacts and resume semantics

Per-VOD output: source.json, audio.wav, transcript.json/txt, audio_features.json (windows/notable_regions), candidates.json (`candidates` array), rejected_candidates.json (`rejected` array), CLIP_REPORT.md, REJECTED_CANDIDATES.md, processing.log; analysis_cache/discovery, verification, judging. These do not currently exist because output is empty. Source metadata includes filename/absolute path/size/fingerprint/test limit. Logs also go to stdout and timestamped root logs files; processing.log appends.

Cache filenames: chunk_0000 or candidate_<12-hex-id>, followed by 20-hex hash. Candidate ID hashes start/end at three decimals plus evidence. Cache hash combines stage+prompt+original user text+provider+model+sorted schema. Transcript/audio enter via user text; temperature, token allocation, endpoint and installed model digest do **not**. Changing those requires rerun flags. Cache holds response JSON, not full request history.

Normal reuses cache. --reanalyze/--rediscover forces all analysis; --reverify forces verification/judging; --rejudge forces judging. --retranscribe replaces audio/transcript and recomputes features; analysis only invalidates if hashed inputs actually change. Changing Whisper config alone does not invalidate transcript. Enabling word timestamps forces retranscription if existing transcript lacks them. Features reuse by existence ignores changed settings. JSON writes are atomic same-directory temp+os.replace; WAV/Markdown/text are not. No locks; avoid concurrent runs on one output.

## Startup, CLI and configuration

Everyday: put exactly one supported VOD in input and double-click process_stream.bat.

```powershell
.\.venv\Scripts\python.exe process_stream.py --help
.\.venv\Scripts\python.exe process_stream.py
.\.venv\Scripts\python.exe process_stream.py "input\my_stream.mp4" --test-minutes 10
.\.venv\Scripts\python.exe process_stream.py "input\my_stream.mp4" --skip-analysis
.\.venv\Scripts\python.exe process_stream.py "input\my_stream.mp4" --retranscribe --word-timestamps
.\.venv\Scripts\python.exe process_stream.py --resume-output "output\<existing-folder>" --rejudge
```

Options: positional video, --config, --resume-output, --reanalyze, --rediscover, --reverify, --rejudge, --retranscribe, --test-minutes, --word-timestamps, --skip-analysis. Resume rejects video/test-minutes/retranscribe/word-timestamps. Test minutes must be positive. Broadest of simultaneous analysis rerun flags wins.

New machine: install Python 64-bit, `python -m venv .venv`, then `.venv\Scripts\python.exe -m pip install -r requirements-windows-gpu.txt`. Install/run Ollama and `ollama pull qwen3:8b`; run FFmpeg bootstrap or use PATH. Recreate .env if cloud analysis is desired. Preserve source/prompts/config and valuable future outputs; do not assume .venv or external model caches can simply be moved. Ensure input/output directories exist.

Exact config/dependencies follow. Dataclass defaults match config except openai/gpt-4.1-mini and request timeout 180. Unknown fields are ignored; only selected bounds/window values are validated.

Unused settings: minimum_score, must_review_entertainment, must_review_clipability, good_content_entertainment, human_review_min_score, include_weak_suggestions, include_stream_moments. Minimum duration expands bounds rather than rejecting fragments. maximum_clips is active.

Dotenv loads beside selected config, without overriding process environment. api_key_env/base_url_env name OPENAI_API_KEY/OPENAI_BASE_URL by default. LOCALAPPDATA locates Ollama; PATH resolves binaries/DLLs. Transcription sets HF_HUB_DISABLE_SYMLINKS_WARNING=1 if absent. HF_TOKEN was suggested in historical download warnings but is not project-required. No Twitch/upload credentials or custom FFmpeg environment variables are configured.

## Current bugs and TODOs

Code-inspection findings, not new fixes or an asserted previously approved backlog:

1. README/unused config drift described above. Do not assume changing old thresholds changes results.
2. OpenAI ignores schemas; both adapters lack local schema validation. Valid-but-wrong JSON can be cached. Unknown verification status can proceed because only UNSUPPORTED is explicitly rejected. Prompt fields alone are incomplete for OpenAI.
3. Quote validation is chunk-wide rather than timestamp-local; ASCII normalization is weak for other languages. Seeds bypass normalization.
4. Audio feature cache ignores config/algorithm changes; --reanalyze/--rediscover do not rebuild it. To refresh without retranscription, preserve then remove that derived file and rerun with WAV present.
5. Full overlapping transcript segments inflate short-window word counts; mixed game/music/voice energy produces false positives. No actual laughter model/speaker separation. Long regions/unbounded seeds increase cost and can exceed useful clip lengths.
6. Chunk loop stops if a window has no segments, potentially omitting later speech after long silence. Character-cap fallback can skip ahead when actual span is shorter than overlap. Tests cover ordinary overlap, not these edges.
7. Cache options/version omissions; corrupt cache files do not self-heal. Existing transcripts ignore config changes except explicit word timing behavior.
8. Abruptly interrupted extraction can leave reusable partial WAV. Transcription has no partial resume. Failed reruns leave old reports. Concurrent runs lack locks.
9. Initial dedup and maximum_clips truncation omit rejection records. Counts need not reconcile arithmetically.
10. Bounds edge cases noted above; `_bounds` assumes nonempty context. Malformed external/cache data may cause uncaught type/index/key errors. Numeric/config validation is incomplete.
11. Batch preflight checks ffmpeg but not ffprobe; GPU import checks do not check all dependencies. It can skip installer then fail on missing ffprobe, and requires GPU dependencies even for CPU/analysis-only workflow.
12. CPU transcription still logs “GPU transcription is now running”; hardware metadata is authoritative.
13. Partial source hashing cannot distinguish equal-size/equal-end-block files differing only in the middle. This is a deliberate speed tradeoff, not whole-file integrity.
14. Latest pipeline needs a representative VOD, short end-to-end run and manual video review. Monitor historical Ollama 500/timeouts/malformed JSON; current successful HTTP health and unit tests do not prove these resolved.

No TODO/FIXME/HACK markers were found in inspected source/tests/README. No new tests or redesign were added.

## Deferred features and decisions to preserve

README explicitly lists future modules: Twitch EventSub/VOD retrieval, chat alignment, automatic cutting, vertical reframing, dashboard review, performance feedback. None is implemented. Upload/publishing, video-frame analysis, diarization, rendered captions and automated editing are also absent; prior discussion of those cannot be verified.

- Preserve one-video, local-first, human-review Phase 1 scope; do not silently add downloads/uploads/publishing.
- Personality/entertainment takes priority; growth is independent. Current prompts allow zero strong clips and reject filler. Do not force a quota or make virality the sole objective.
- Never invent visual events from speech/audio. Flags mean possibilities; verifier separates grounding from quality.
- Preserve absolute timestamps, chunk overlap, three stages, targeted rerun semantics and prompt/schema cache invalidation.
- Preserve audio-priority/review flags through dedup, separate short-test output, fingerprinted naming, atomic JSON and retained Windows DLL handles.
- Keep keys outside source and libraries project-local. Understand RTX 4070 VRAM constraints before raising context/output limits.

This handoff session made no redesign decisions. It documented the current implementation, checked offline tests/tool availability, and created this document and exhaustive file inventory.

## Appendix: exact prompts, schemas, configuration, dependencies and tree

The remaining sections are copied/generated from current files. No secret values are reproduced.

### prompts/candidate_discovery.txt

```text
You are PASS 1: Entertainment Discovery for JustGlad2BHere (Squirrel), a funny, chaotic, welcoming gaming creator.

Primary question: If an experienced gaming clipper were watching this VOD, would they confidently write down this timestamp because Squirrel's personality makes it meaningfully more entertaining than normal stream conversation?

Assume most stream moments are not worth turning into short-form content. Be selective. Find moments driven by Squirrel's genuine laughter, screaming, yelling, panic, rage, confusion, funny mistakes, embarrassing failures, playful arguments, unexpected statements, ridiculous quotes, escalating chaos, strong reactions, or obvious punchlines/payoffs. The creator's personality matters far more than ordinary gameplay.

Strongly penalize routine gameplay, normal callouts, ordinary kills or deaths, calm strategy discussion, long setup, generic reactions such as "nice" or "damn," moments that only make sense to existing viewers, and moments with no clear ending or payoff. A reaction word alone is not enough. Keep a moment only when the transcript supplies specific evidence of personality, escalation, a distinctive quote, or a satisfying comedic/emotional turn.

Allowed content tags: LAUGHTER, SCREAM_SHOCK, CHAOS, BANTER, FAIL, RAGE, ROAST, SELF_OWN, RELATIONSHIP, STORY, CHAT_INTERACTION, IMPRESSIVE_GAMEPLAY, WEIRD_UNEXPECTED, WHOLESOME, PERSONALITY. Use multiple tags when appropriate.

Ground decisions and descriptions in transcript evidence. You cannot see gameplay, faces, gestures, or causes. Do not invent a death, kill, mistake, visual reaction, object, mechanic, thought, or cause/effect. If the transcript does not make the moment compelling on its own, omit it rather than assuming unseen context will rescue it.

Return core_start/core_end, a short verbatim exact_transcript_evidence excerpt, literal_description, content_tags, expected_setup/payoff (empty when unknown), why_detected, and needs_video_review. Return zero candidates whenever nothing clears this bar. There is no minimum or target count; never add filler to reach a quota. Output only JSON. Return no more than 8 distinct candidates and keep fields concise.
```

### prompts/evidence_verifier.txt

```text
You are PASS 2: an adversarial evidence verifier. Your only job is to prevent invented descriptions, not to decide whether a moment is entertaining.

Compare the candidate with the complete surrounding transcript. Return SUPPORTED when its factual claims are grounded. Return PARTIALLY_SUPPORTED when the reaction exists but wording includes unsupported inference; list those claims and provide a strictly factual corrected description. Return UNSUPPORTED when the quoted moment itself is absent or materially fabricated.

Never infer gameplay events, deaths, kills, gestures, facial expressions, objects, mechanics, thoughts, or causes/effects not established by dialogue. Missing visual knowledge is not a reason to reject real laughter, screaming, panic, rapid reactions, or other transcript/audio-supported activity. Keep the reaction factual and allow video review to resolve its cause.

verification_confidence is 0–10 confidence in your status decision, not excitement. Output only the required JSON object.
```

### prompts/content_quality_judge.txt

```text
You are PASS 3: Content Quality Judge for JustGlad2BHere (Squirrel).

Assume most stream moments are not worth turning into short-form content. Judge with high precision and be comfortable returning zero S or A clips from an entire stream. Never promote a candidate merely to fill a quota.

Prioritize Squirrel's personality: genuine laughter, screaming, yelling, panic, rage, confusion, funny mistakes, embarrassing failures, playful arguments, unexpected statements, ridiculous quotes, escalating chaos, strong reactions, and obvious punchlines/payoffs.

Strongly penalize routine gameplay, normal callouts, ordinary kills or deaths, calm strategy discussion, long setup, generic reactions such as "nice" or "damn," moments that only make sense to existing viewers, and moments with no clear ending or payoff. A merely pleasant, mildly interesting, or potentially visual moment is not enough.

Score independently from 0–10:
- entertainment_score: apparent humor, emotion, reaction, personality, chaos, and surprise
- clipability_score: ease of making an approximately 8–45 second edit with a usable beginning/end, little context, and manageable dead space
- growth_potential_score: likely appeal to people unfamiliar with Squirrel; this is the only virality-adjacent score

Judge only what the transcript supports. Do not invent what happened visually or assume unseen gameplay will rescue weak dialogue. Mark needs_video_review true only when an already-compelling transcript moment needs a visual check.

Assign exactly one tier and give a short, specific tier_reason:
- S: exceptional; an unmistakable standout with strong personality and a clear hook, escalation, and payoff
- A: definitely worth editing; strong, distinctive, understandable, and likely to produce a satisfying short
- B: maybe usable; contains a real spark but has a material weakness such as context, setup, ending, or payoff
- REJECT: not worth editing; routine, generic, context-dependent, weak, unfinished, or lacking a clear payoff

Use S rarely. A should mean the editor should definitely spend time on it. B is not a soft acceptance for ordinary content. REJECT aggressively when the evidence is mediocre. Set major_rejection true exactly when tier is REJECT and include the same concise reason in rejection_reasons.

State setup, escalation, and payoff factually; empty strings are valid. A missing payoff should normally cap the moment at B and often means REJECT. Recommend the tightest useful range, usually 8–45 seconds. Preserve the existing fields and also provide tier and tier_reason. Output only JSON.
```

### config.json

```text
{
  "whisper": {
    "model": "large-v3",
    "device": "auto",
    "compute_type": "float16",
    "cpu_compute_type": "int8",
    "language": null,
    "beam_size": 5,
    "vad_filter": true,
    "word_timestamps": false
  },
  "audio": {
    "sample_rate": 16000,
    "codec": "pcm_s16le",
    "analysis_enabled": true,
    "analysis_window_seconds": 2.0,
    "analysis_hop_seconds": 1.0,
    "baseline_seconds": 60.0,
    "notable_signal_threshold": 6.5
  },
  "analysis": {
    "provider": "ollama",
    "model": "qwen3:8b",
    "api_key_env": "OPENAI_API_KEY",
    "base_url_env": "OPENAI_BASE_URL",
    "ollama_url": "http://127.0.0.1:11434",
    "ollama_context_tokens": 10240,
    "ollama_max_output_tokens": 2048,
    "chunk_minutes": 20,
    "chunk_overlap_minutes": 2,
    "max_chunk_characters": 24000,
    "minimum_score": 7.0,
    "minimum_clip_duration_seconds": 8.0,
    "maximum_clip_duration_seconds": 45.0,
    "context_expansion_seconds": 30.0,
    "must_review_entertainment": 8.3,
    "must_review_clipability": 7.5,
    "good_content_entertainment": 6.5,
    "human_review_min_score": 5.5,
    "include_weak_suggestions": false,
    "include_stream_moments": false,
    "maximum_clips": 12,
    "temperature": 0.2,
    "request_timeout_seconds": 600,
    "max_retries": 3
  }
}
```

### requirements.txt

```text
faster-whisper>=1.1,<2
openai>=1.30,<3
python-dotenv>=1.0,<2
requests>=2.31,<3
```

### requirements-windows-gpu.txt

```text
-r requirements.txt

# Project-local NVIDIA runtime used by CTranslate2/faster-whisper on Windows.
nvidia-cuda-runtime-cu12>=12.4,<13
nvidia-cublas-cu12>=12.4,<13
nvidia-cudnn-cu12>=9,<10
```

### .env.example

```text
# Copy this file to .env and fill in only the provider you use.
OPENAI_API_KEY=
# Optional: uncomment for an OpenAI-compatible service.
# OPENAI_BASE_URL=https://your-compatible-service.example/v1
```

### DISCOVERY_SCHEMA

```json
{
  "type": "object",
  "properties": {
    "candidates": {
      "type": "array",
      "maxItems": 8,
      "items": {
        "type": "object",
        "properties": {
          "core_start": {
            "type": "number"
          },
          "core_end": {
            "type": "number"
          },
          "exact_transcript_evidence": {
            "type": "string"
          },
          "literal_description": {
            "type": "string"
          },
          "content_tags": {
            "type": "array",
            "items": {
              "type": "string"
            }
          },
          "expected_setup": {
            "type": "string"
          },
          "expected_payoff": {
            "type": "string"
          },
          "why_detected": {
            "type": "string"
          },
          "needs_video_review": {
            "type": "boolean"
          }
        },
        "required": [
          "core_start",
          "core_end",
          "exact_transcript_evidence",
          "literal_description",
          "content_tags",
          "expected_setup",
          "expected_payoff",
          "why_detected",
          "needs_video_review"
        ]
      }
    }
  },
  "required": [
    "candidates"
  ]
}
```

### VERIFICATION_SCHEMA

```json
{
  "type": "object",
  "properties": {
    "status": {
      "type": "string",
      "enum": [
        "SUPPORTED",
        "PARTIALLY_SUPPORTED",
        "UNSUPPORTED"
      ]
    },
    "unsupported_claims": {
      "type": "array",
      "items": {
        "type": "string"
      }
    },
    "corrected_description": {
      "type": "string"
    },
    "verification_confidence": {
      "type": "number"
    }
  },
  "required": [
    "status",
    "unsupported_claims",
    "corrected_description",
    "verification_confidence"
  ]
}
```

### JUDGE_SCHEMA

```json
{
  "type": "object",
  "properties": {
    "title": {
      "type": "string"
    },
    "tier": {
      "type": "string",
      "enum": [
        "S",
        "A",
        "B",
        "REJECT"
      ]
    },
    "tier_reason": {
      "type": "string"
    },
    "entertainment_score": {
      "type": "number"
    },
    "clipability_score": {
      "type": "number"
    },
    "growth_potential_score": {
      "type": "number"
    },
    "setup": {
      "type": "string"
    },
    "escalation": {
      "type": "string"
    },
    "payoff": {
      "type": "string"
    },
    "why_entertaining": {
      "type": "string"
    },
    "what_to_look_for": {
      "type": "string"
    },
    "suggested_editing_angle": {
      "type": "string"
    },
    "recommended_start": {
      "type": "number"
    },
    "recommended_end": {
      "type": "number"
    },
    "needs_video_review": {
      "type": "boolean"
    },
    "ai_confidence": {
      "type": "number"
    },
    "major_rejection": {
      "type": "boolean"
    },
    "rejection_reasons": {
      "type": "array",
      "items": {
        "type": "string"
      }
    }
  },
  "required": [
    "title",
    "tier",
    "tier_reason",
    "entertainment_score",
    "clipability_score",
    "growth_potential_score",
    "setup",
    "escalation",
    "payoff",
    "why_entertaining",
    "what_to_look_for",
    "suggested_editing_angle",
    "recommended_start",
    "recommended_end",
    "needs_video_review",
    "ai_confidence",
    "major_rejection",
    "rejection_reasons"
  ]
}
```

### All dynamic user messages

Transcript lines are `[{float(start):.3f} | {format_timestamp(start)}] {text}`, joined with newline. Segment ends are not included. Audio text is compact JSON (`ensure_ascii=False`, comma/colon separators) of overlapping signals, or exactly `No notable audio regions were detected in this chunk.`

Discovery:
```text
Absolute stream chunk {chunk.start} to {chunk.end}. Numeric timestamps are seconds.

TRANSCRIPT:
{formatted chunk segments}

NOTABLE AUDIO SIGNALS:
{overlapping audio text}
```
Verification:
```text
{candidate JSON, ensure_ascii=False, indent=2}

SURROUNDING TRANSCRIPT:
{segments overlapping core plus/minus context_expansion_seconds}

AUDIO SIGNALS:
{audio overlapping core_start/core_end}
```
Judging:
```text
{verified candidate JSON, ensure_ascii=False, indent=2}

EXPANDED TRANSCRIPT:
{segments overlapping core plus/minus context_expansion_seconds}

AUDIO SIGNALS:
{audio overlapping core_start/core_end}
```
Candidate JSON contains normalized discovery fields, computed id, source_chunk, flags, tags and overlapping audio. Judging additionally receives verification status/confidence/unsupported claims and corrected description when applicable.

On malformed discovery, append two newlines and exactly:
```text
RETRY REQUIREMENT: The previous response was incomplete or malformed. Return valid JSON with at most 5 of the strongest candidates. Keep every field concise.
```
No prior assistant reply/history is sent. No custom Whisper prompt exists. Actual per-VOD payloads cannot be reproduced because output is empty; templates above are the complete current message construction.

### Installed dependency snapshot

Requirements are ranged, not locked. This is the observed environment, not a new dependency specification. Important transitive packages include CTranslate2, PyAV, ONNX Runtime, NumPy, Hugging Face Hub and tokenizers. NVRTC is installed but not explicitly requested in requirements.

```text
annotated-types==0.8.0
anyio==4.14.2
av==18.1.0
certifi==2026.7.22
charset-normalizer==3.5.1
click==8.4.2
colorama==0.4.6
ctranslate2==4.8.1
distro==1.9.0
faster-whisper==1.2.1
filelock==3.32.3
flatbuffers==25.12.19
fsspec==2026.7.0
h11==0.16.0
hf-xet==1.6.0
httpcore==1.0.9
httpx==0.28.1
huggingface_hub==1.28.0
idna==3.19
jiter==0.16.0
numpy==2.5.2
nvidia-cublas-cu12==12.9.2.10
nvidia-cuda-nvrtc-cu12==12.9.86
nvidia-cuda-runtime-cu12==12.9.79
nvidia-cudnn-cu12==9.24.0.43
onnxruntime==1.29.0
openai==2.54.0
packaging==26.3
protobuf==7.36.0
pydantic==2.13.4
pydantic_core==2.46.4
python-dotenv==1.2.3
PyYAML==6.0.3
requests==2.34.2
setuptools==84.0.0
sniffio==1.3.1
tokenizers==0.23.1
tqdm==4.70.0
typing-inspection==0.4.4
typing_extensions==4.16.0
urllib3==2.7.0
```

### Full application directory tree

Every application/source/setup/prompt/test/log file is shown below, including hidden files. Only third-party .venv and generated Python caches are collapsed. **HANDOFF_FILE_TREE.txt contains the exhaustive accessible tree including individual generated/dependency files.** Access-denied directories (including .pytest_cache) are marked rather than invented. This is filesystem inventory, not Git status.

```text
Squirrel Content/
  .pytest_cache/
    [NOT ENUMERABLE: PermissionError]
  .venv/
    [generated contents: see HANDOFF_FILE_TREE.txt]
  __pycache__/
    [generated contents: see HANDOFF_FILE_TREE.txt]
  input/
    .gitkeep
  logs/
    .gitkeep
    process_20260822_193706.log
    process_20260822_194152.log
    process_20260822_195130.log
    process_20260822_195214.log
    process_20260822_195834.log
    process_20260822_200956.log
    process_20260822_201040.log
    process_20260822_201205.log
    process_20260822_201318.log
    process_20260822_202803.log
    process_20260822_231557.log
    process_20260822_232646.log
    process_20260822_233500.log
    process_20260822_233732.log
    process_20260822_235959.log
    process_20260823_011115.log
  output/
  prompts/
    candidate_discovery.txt
    content_quality_judge.txt
    evidence_verifier.txt
  src/
    __pycache__/
      [generated contents: see HANDOFF_FILE_TREE.txt]
    __init__.py
    analyze.py
    audio.py
    config.py
    report.py
    transcribe.py
    utils.py
  tests/
    __pycache__/
      [generated contents: see HANDOFF_FILE_TREE.txt]
    __init__.py
    test_pipeline.py
  tools/
    ffmpeg/
      bin/
        .gitkeep
        ffmpeg.exe
        ffprobe.exe
  .env
  .env.example
  .gitignore
  config.json
  HANDOFF.md
  HANDOFF_FILE_TREE.txt
  process_stream.bat
  process_stream.py
  README.md
  requirements-windows-gpu.txt
  requirements.txt
  setup_ffmpeg.bat
  setup_ffmpeg.ps1
```

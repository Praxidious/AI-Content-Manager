# Non-verbal audio events

This adds sound evidence to the existing pipeline. It does not change the final ranking formula, quality tiers, preference scoring, performance-feedback weighting, Whisper configuration, clip-ID function, or review eligibility rules. Audio-only moments still pass through the existing verifier and content-quality judge; an event is not a guaranteed accepted clip.

## Model and dependencies

The detector uses **YAMNet / AudioSet**, a MobileNet-based classifier with 521 sound classes. It runs locally through **ONNX Runtime**, consuming 16 kHz mono waveforms and producing scores for approximately 0.96-second patches at 0.48-second intervals. This is a semantic sound classifier, not just a loudness threshold. The existing environment already contains NumPy and ONNX Runtime, so no TensorFlow, PyTorch, paid API, or transcription change is needed.

The ONNX conversion is from `audiomagic/yamnet-onnx`, pinned to revision `f25b741c2f0bdc6d7e6db24b5fddda23347dbafd`. This is a third-party format conversion of Google's YAMNet, not an official Google ONNX distribution. The setup script saves model provenance, SHA-256 digests, license and label map beside the 16 MB model. No downloaded Python model code is executed. NumPy (`>=2,<3`) and ONNX Runtime (`>=1.24,<2`) are now explicit dependencies in `requirements.txt`.

Sources: [Google YAMNet documentation](https://www.tensorflow.org/hub/tutorials/yamnet), [conversion provenance and interface](https://huggingface.co/audiomagic/yamnet-onnx/blob/f25b741c2f0bdc6d7e6db24b5fddda23347dbafd/README.md), [ONNX CUDA execution provider](https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html).

Install on another PC:

```powershell
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe tools\setup_audio_events.py
```

`device: auto` uses CUDA when the installed ONNX Runtime exposes a working CUDA provider, and falls back to CPU on initialization/inference failure. This installation currently has the CPU ONNX package; Whisper's CTranslate2 CUDA support is separate. The CPU path is practical here: a cached 139-minute waveform took about 23 seconds for its first event-analysis pass. That is a local smoke-test measurement, not a general performance guarantee. ONNX Runtime GPU can be installed separately for compatible environments; do not install CPU and GPU ONNX Runtime packages simultaneously. The code uses the project's existing CUDA DLL configuration when available.

## Audio tracks and extraction

Normal transcription continues using the original `extract_audio()` path and `audio.wav` without a changed FFmpeg mapping. For reactions, FFprobe inventories audio streams:

- An explicit `microphone_track` selects that **zero-based audio-track ordinal**. `1` means the second audio track, even if its global media stream index is `2`.
- With `null`, one unambiguous microphone title such as `Morgan Mic` is selected automatically. Multiple microphone labels, generic `Track1` names, or labels mentioning mixes/game/desktop/Discord do not establish isolation.
- Otherwise the detector reuses `audio.wav` and labels the source mixed/uncertain. The recent network VOD inspected during implementation has one stereo AAC track named `Track1`.

An isolated track is extracted once to `reaction_audio.wav`. Its map, source fingerprint, test range and track inventory are saved in `audio_event_source.json`; it is reused on subsequent runs. This is a separate required decode of the mic stream, not repeated decoding of the transcription stream. The extractor preserves alignment by padding timestamp gaps and only reads the source VOD under the existing read guard. A changed source fingerprint prevents mismatched mic extraction.

Offline resumes reuse a previously selected mic WAV, or the legacy mixed WAV if no microphone was configured. An explicit mic selection cannot silently fall back to mixed audio when its source is missing. Non-default WAV formats are converted once from the existing WAV to `audio_events_16k.wav`; no video frames are analyzed.

## Detection and candidate generation

Supported default groups include laughter, giggling, screams, shouting, gasps, crying, whoops, groans, grunts and group-reaction proxies. Original labels such as `Belly laugh`, `Chuckle, chortle` and `Screaming` and their confidence values remain in the event data. Optional sigh/snort groups are available.

For each patch the detector also records RMS, peak amplitude, relative loudness against a surrounding rolling median and the VOD median, recent quiet-to-loud change, vocal-activity confidence, background confidence and top classifier classes. Waveform features never depend on the transcript. Confidence is a classifier score, not a calibrated probability or entertainment score.

Mixed audio has a higher confidence threshold and requires the human-reaction class to exceed competing explosion/gunfire/music scores. A relative loudness spike can modestly relax the confidence threshold, but cannot create an event by itself. When classification succeeds, classifier events replace the old volume-only candidate seeds; transcript discovery and acoustic context remain. When the detector is unavailable or disabled, the existing transcript/heuristic path continues with a warning/status record.

Detections separated by at most the configured merge gap become one reaction sequence. Merging retains event types, peak class confidences, maximum relative/transition loudness, active duration and frame count. Sequences are bounded at 30 seconds by default so a whole noisy session does not become one event.

Each grouped event can seed a candidate with **zero transcript words**. Defaults supply 7 seconds of pre-roll and 12 seconds of post-roll, clipped to the recording. The existing candidate budget limits audio seeds; the existing judge still selects a useful range within its duration limits. Available nearby words are attached as evidence rather than required as a gate.

The existing candidate deduplicator combines overlapping transcript/audio candidates and preserves their event metadata and `candidate_sources`. It keeps the retained candidate's core timestamps/clip ID. The final range includes merged audio evidence where possible within the existing maximum clip duration. The verifier/judge prompts now permit probabilistic audio evidence and prohibit invented words, causes and speaker identities. No new final score is introduced.

## Stored data and review

Within each VOD output directory:

- `audio_events.json`: model/label provenance, track source, settings, timestamped grouped events and acoustic/classification windows.
- `audio_event_cache/<fingerprint>/chunk_*.npz`: full 521-class score matrices, written atomically in roughly 60-second chunks. Completed chunks survive interruptions. Content/model/label/chunk changes invalidate cache; threshold and merge changes reuse inference.
- `audio_events_status.json`: complete, disabled or unavailable status. Check this before relying on an older `audio_events.json` after an unsuccessful run.
- `audio_event_source.json`: selected track, selection rationale and available track inventory.
- `reaction_audio_features.json`: the existing energy features when a separate mic WAV is used; the default mixed path retains `audio_features.json`.

Every newly analyzed candidate has `audio_events`, `audio_event_types`, `candidate_sources`, `audio_only`, `audio_event_detection_status`, `audio_event_count`, `max_audio_confidence`, `max_relative_loudness_db`, and scream/laughter/gasp flags. Individual events retain source/track, model hash and original class scores. Unknown/unavailable analysis is distinguished from a completed analysis with no events.

These fields travel through existing `candidates.json`, rejected-candidate metadata, SQLite `clips.metadata_json`, rating feature snapshots, and feedback exports. Performance posts already freeze full clip metadata, so they retain the new fields without a migration or changes to historical analytics. Existing ratings, training records and posted-performance tables are not rewritten.

The review page's **Audio detection, features & clip identity** section and generated clip reports show candidate sources, class confidence, timestamps, source track, relative loudness and quiet-to-loud change. Restart an already-running review server to load its updated template. The review queue still applies the existing S/A and minimum-duration filters.

## Configuration

The new `audio_events` section in `config.json` contains:

| Setting | Default | Meaning |
|---|---:|---|
| `enabled` | `true` | Enable independent sound-event analysis |
| `model` | `yamnet_onnx` | Implemented adapter; other files must follow its interface |
| `model_path`, `labels_path` | `tools/models/yamnet/...` | Project-relative or absolute model/label paths |
| `device`, `cpu_threads` | `auto`, `4` | ONNX execution preference and CPU threads |
| `confidence_threshold` | `0.45` | Isolated mic detection threshold |
| `mixed_confidence_threshold` | `0.65` | More conservative mixed-audio threshold |
| `relative_loudness_db` | `6` | Relative/transition loudness sensitivity |
| `loudness_threshold_relaxation` | `0.10` | Maximum threshold relaxation for a spike; floor 0.25 |
| `background_margin` | `0.10` | Human class must exceed competing background classes on mixed audio |
| `baseline_seconds` | `60` | Local rolling baseline span |
| `merge_gap_seconds` | `1.5` | Join nearby reaction patches |
| `minimum_event_seconds` | `0.35` | Minimum detected active duration |
| `maximum_event_seconds` | `30` | Bound sustained reaction sequences |
| `pre_roll_seconds`, `post_roll_seconds` | `7`, `12` | Suggested context around event seeds |
| `chunk_seconds` | `60` | Inference/cache chunk size, aligned to model hops |
| `enabled_classes` | see configuration | Canonical human-reaction groups to retain |
| `microphone_track` | `null` | Explicit zero-based isolated microphone audio track, otherwise auto |

## Manually test an existing VOD

From the project directory, inspect events without changing transcription, clip reports, rankings or ratings:

```powershell
.venv\Scripts\python.exe process_stream.py --resume-output "output\2026-09-05_19-27-53_50e9b19aa6f2" --audio-events-only
```

Inspect that directory's `audio_events.json` and `audio_events_status.json`. This recording produced four grouped suggestions in the initial mixed-audio smoke test; listen around the timestamps to assess false positives and missed reactions. The number of suggestions is not an accuracy measurement. Repeat the command to reuse cached inference.

To regenerate candidates using the current transcript and new audio evidence:

```powershell
.venv\Scripts\python.exe process_stream.py --resume-output "output\2026-09-05_19-27-53_50e9b19aa6f2" --rediscover
```

This intentionally updates candidate reports using the existing judging/ranking system; saved human ratings and performance records remain intact. For a different VOD with an explicit microphone selection, run its source once with `--force-video` to extract that track; subsequent resumes can work offline. To inspect only the first ten minutes of a new VOD without running Whisper, use an explicit VOD path with `--test-minutes 10 --audio-events-only`.

Run automated tests with `.venv\Scripts\python.exe -m unittest discover -s tests -v`. A–G use deterministic waveform/classifier fixtures to test pipeline behavior, not a claimed real-world detection benchmark. Additional tests cover cache resume/invalidation, track ambiguity, bounds, configuration and zero-word end-to-end candidate persistence. Live checks used the actual YAMNet model on silence and saved VOD audio. A real FFmpeg two-track fixture verified that a labeled silent mic was extracted instead of the loud game track, producing zero events. Browser verification confirmed audio-only reasons, class confidences and relative loudness display in the existing review page.

## Limitations and recording advice

YAMNet cannot identify Morgan, reliably count speakers, tell real human screams from every game/voice-chat scream, infer rage/excitement reliably, or recover the visual cause. Group reaction is a sound-class proxy, not speaker diarization. Very brief or quiet gasps/giggles can be missed at its patch resolution. Conservative mixed thresholds trade recall for fewer game-effect false positives. Human listening/review is required; do not treat a confidence percentage as ground truth.

For better results, record Morgan's microphone on its own OBS audio track while retaining the normal mixed track for playback/transcription. In OBS Advanced Audio Properties, route only her microphone to that dedicated recording track and exclude game, desktop, music and voice chat from it. Enable the track in recording output and use a recording format supporting multiple audio tracks (such as MKV). Verify the saved track contains her mic alone, label it `Morgan Mic` if the recording workflow supports labels, or set its zero-based `microphone_track` number explicitly. A loud explosion on a genuinely isolated quiet mic will then not enter the reaction classifier at all.

See the [official OBS multiple-track setup guide](https://obsproject.com/kb/multiple-audio-track-recording-guide).

## Changed files

Added: `src/audio_events.py`, `tools/setup_audio_events.py`, `tests/test_audio_events.py`, this guide, and the downloaded local model assets (ignored by source control).

Updated: `process_stream.py`, `src/config.py`, `src/analyze.py`, `src/feedback.py`, `src/report.py`, `src/review_templates/review.html`, `src/review_static/review.js`, `prompts/evidence_verifier.txt`, `prompts/content_quality_judge.txt`, `config.json`, `requirements.txt`, `.gitignore`, and `README.md`. No database migration was necessary.

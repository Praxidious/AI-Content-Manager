# Minimal handoff package

This folder contains only application/test source, configuration, dependency requirements, prompts, documentation, and setup/run scripts. `.env.example` contains blank/example values only; `.env` and all secrets are excluded.

Read HANDOFF.md for implementation details. Its original-workspace inventory and historical log references describe the source machine, not files shipped here. The large HANDOFF_FILE_TREE.txt inventory is intentionally excluded; project_tree.txt is the complete inventory of this package.

Before running, recreate the empty runtime directories from this folder in PowerShell:

```powershell
New-Item -ItemType Directory -Force input, output, logs
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-windows-gpu.txt
```

Install Ollama and pull qwen3:8b separately. Run setup_ffmpeg.bat to install FFmpeg locally if it is not on PATH. Whisper large-v3 downloads on first transcription. Put one supported VOD in input, then run process_stream.bat. See README.md and HANDOFF.md for details and limitations. Models, binaries, media, dependencies and outputs are intentionally not bundled.

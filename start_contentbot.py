"""Interactive Windows launcher; processing remains in the existing pipeline."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

from src.config import load_config
from src.ingest import Registry, discover, resolve_path, video_fingerprint
from src.review_launcher import open_review

ROOT = Path(__file__).resolve().parent
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"


def choose(prompt: str, maximum: int) -> int:
    while True:
        value = input(prompt).strip()
        if value.isascii() and value.isdecimal() and 0 <= int(value) <= maximum:
            return int(value)
        print(f"Please enter a number from 0 to {maximum}.")


def pick_vod() -> Path | None:
    config = load_config(ROOT / "config.json")
    source = resolve_path(config.paths.vod_source, ROOT)
    print(f"\nVODs in {source} (newest first):")
    recordings = discover(source)
    if not recordings:
        print("No supported VODs found. Check paths.vod_source in config.json.")
        return None
    for number, recording in enumerate(recordings, 1):
        print(f"  {number}. {recording.path.name}")
    print("  0. Back to the main menu")
    print("Recording safety and processing history will be checked before running.")
    selected = choose("Choose a VOD number: ", len(recordings))
    return recordings[selected - 1].path if selected else None


def run_batch(name: str, *args: str) -> int:
    # Only fixed launcher names/options go through cmd; VOD paths use argv below.
    return subprocess.run(
        [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", name, *args],
        cwd=ROOT,
    ).returncode


def process(args: list[str]) -> int:
    result = run_batch("process_stream.bat", "--setup-only")
    if result:
        return result
    return subprocess.run([str(PYTHON), str(ROOT / "process_stream.py"), *args], cwd=ROOT).returncode


def open_completed_vod(video: Path) -> bool:
    config = load_config(ROOT / "config.json")
    output = resolve_path(config.paths.output_directory, ROOT)
    fingerprint = video_fingerprint(video)
    if Registry(output / "vod_registry.json").status(fingerprint, "full:analysis") != "completed":
        return False
    print("This VOD is already processed. Opening its saved clips...")
    open_review(ROOT / "config.json", output, source_key=fingerprint)
    return True


def main() -> int:
    while True:
        print("\n=== JustGlad2BHere ContentBot ===")
        print("How do you want to start?")
        print("  1. Open the grading website / trainer")
        print("  2. Run all finished, unprocessed VODs")
        print("  3. Choose a single VOD by name")
        print("  4. Run the newest finished, unprocessed VOD")
        print("  5. Rescan the latest saved VOD using my grading feedback")
        print("  6. List VOD readiness and processing status")
        print("  0. Exit")
        choice = choose("Pick a number: ", 6)
        if choice == 0:
            return 0
        try:
            if choice == 1:
                result = run_batch("OPEN_TRAINER.bat")
            else:
                args = {2: ["--all-vods"], 4: [], 5: ["--rerun-feedback"], 6: ["--list-vods"]}.get(choice, [])
                if choice == 3:
                    video = pick_vod()
                    if video is None:
                        continue
                    print(f"\nSelected: {video.name}")
                    print("  1. Process / open saved clips if already completed")
                    print("  2. Rescan with current feedback (reuse saved transcription)")
                    print("  0. Back to the main menu")
                    mode = choose("Choose how to run this VOD: ", 2)
                    if mode == 0:
                        continue
                    if mode == 1 and open_completed_vod(video):
                        input("\nPress Enter to return to the menu...")
                        continue
                    args = [str(video)]
                    if mode == 2:
                        args += ["--force-video", "--rediscover"]
                result = process(args)
            if result:
                print("The action did not finish successfully. Review the messages above.")
            else:
                print("Done.")
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"Could not complete the action: {exc}")
        input("\nPress Enter to return to the menu...")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (EOFError, KeyboardInterrupt):
        print("\nContentBot menu closed.")

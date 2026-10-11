"""Run the local ContentBot review app: python review_clips.py."""
import argparse
import logging
import sqlite3
from pathlib import Path

from src.config import load_config
from src.feedback import FeedbackStore, import_outputs
from src.ingest import resolve_path
from src.review_launcher import open_review

ROOT = Path(__file__).resolve().parent


def main(argv=None):
    parser = argparse.ArgumentParser(description="Review clips and teach ContentBot your preferences locally")
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--port", type=int, default=5050)
    parser.add_argument("--output-directory", type=Path, help="Review another output directory")
    parser.add_argument("--open-browser", action="store_true",
                        help="Start or reuse the trainer in the background and open it in the default browser")
    parser.add_argument("--export-feedback", type=Path, help="Export labeled feature snapshots as JSONL and exit")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        raise ValueError("Port must be between 1 and 65535")
    config = load_config(resolve_path(args.config, ROOT))
    output = resolve_path(args.output_directory or config.paths.output_directory, ROOT)
    if args.open_browser:
        if args.export_feedback:
            raise ValueError("--open-browser cannot be combined with --export-feedback")
        open_review(resolve_path(args.config, ROOT), output)
        return
    store = FeedbackStore(output / "feedback.sqlite3")
    count = import_outputs(store, output)
    print(f"Imported {count} existing candidates. Feedback database: {store.path}", flush=True)
    if args.export_feedback:
        store.export(args.export_feedback)
        print(f"Exported feedback to {args.export_feedback}")
        return
    try:
        from src.review import create_app
    except ImportError as exc:
        raise RuntimeError("Install review dependencies with: python -m pip install -r requirements.txt") from exc
    print(f"Review clips at http://localhost:{args.port} (Ctrl+C to stop)", flush=True)
    create_app(store, output / "review_previews", config.analysis).run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        logging.error("%s", exc)
        raise SystemExit(1)

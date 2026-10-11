"""Start or reuse the local review server and open it after a completed run."""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode
import webbrowser
from pathlib import Path


def server_matches(url, database):
    try:
        with urllib.request.urlopen(url + "/api/health", timeout=1) as response:
            health = json.load(response)
        return health.get("app") == "contentbot-review" and Path(health.get("database", "")).resolve() == database.resolve()
    except (OSError, ValueError, AttributeError):
        return False


def open_review(config_path: Path, output_directory: Path, source_key: str | None = None):
    url = "http://localhost:5050"
    database = output_directory / "feedback.sqlite3"
    if not server_matches(url, database):
        root = Path(__file__).resolve().parents[1]
        log_path = root / "logs" / "review_server.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        flags = (subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS) if os.name == "nt" else 0
        with log_path.open("a", encoding="utf-8") as log:
            process = subprocess.Popen([sys.executable, str(root / "review_clips.py"),
                "--config", str(config_path.resolve()), "--output-directory", str(output_directory.resolve())],
                cwd=root, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                creationflags=flags, start_new_session=os.name != "nt")
        for _ in range(30):
            if server_matches(url, database):
                break
            if process.poll() is not None:
                raise RuntimeError(f"Review server could not start. Check {log_path}; port 5050 may already be in use.")
            time.sleep(0.5)
        else:
            raise RuntimeError(f"Review server is still starting. Open {url} shortly or check {log_path}.")
    if source_key is not None:
        url += "?" + urlencode({"source": source_key})
    if not webbrowser.open(url):
        raise RuntimeError(f"Could not open the browser automatically. Open {url} to review your clips.")

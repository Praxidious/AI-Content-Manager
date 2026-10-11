"""Download the configured local screenshot model: python tools/setup_analytics_vision.py."""
import json
from pathlib import Path

import requests


def main():
    settings = json.loads((Path(__file__).resolve().parents[1] / "config.json").read_text())["analysis"]
    model = settings.get("analytics_vision_model") or "qwen3-vl:4b-instruct"
    url = settings.get("ollama_url", "http://127.0.0.1:11434").rstrip("/")
    last = None
    with requests.post(url + "/api/pull", json={"model": model, "stream": True}, stream=True, timeout=(10, 600)) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if not line:
                continue
            data = json.loads(line)
            if "error" in data:
                raise RuntimeError(data["error"])
            percent = int(data.get("completed", 0) / max(1, data.get("total", 1)) * 10) * 10
            status = (data.get("status"), percent)
            if status != last:
                print(status[0], f"{percent}%" if data.get("total") else "", flush=True)
                last = status
    response = requests.post(url + "/api/show", json={"model": model}, timeout=30)
    response.raise_for_status()
    if "vision" not in response.json().get("capabilities", []):
        raise RuntimeError("The downloaded model does not report vision support")
    print(f"Ready: {model} supports vision.", flush=True)


if __name__ == "__main__":
    main()

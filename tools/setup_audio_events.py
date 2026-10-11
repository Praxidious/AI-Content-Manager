"""Install the pinned, local YAMNet ONNX model (no Python model code is executed)."""
import hashlib
import json
from pathlib import Path
import requests

REVISION = "f25b741c2f0bdc6d7e6db24b5fddda23347dbafd"
REPOSITORY = "audiomagic/yamnet-onnx"


def main():
    directory = Path(__file__).resolve().parents[1] / "tools/models/yamnet"
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {"repository": REPOSITORY, "revision": REVISION, "files": {}}
    for name in ("yamnet.onnx", "yamnet_class_map.csv", "LICENSE", "README.md"):
        path = directory / name
        with requests.get(f"https://huggingface.co/{REPOSITORY}/resolve/{REVISION}/{name}", stream=True, timeout=(20, 180)) as response:
            response.raise_for_status()
            temporary = path.with_suffix(path.suffix + ".part")
            digest = hashlib.sha256()
            with temporary.open("wb") as stream:
                for chunk in response.iter_content(1024 * 1024):
                    stream.write(chunk)
                    digest.update(chunk)
            temporary.replace(path)
        manifest["files"][name] = digest.hexdigest()
        print(f"Installed {name}: {path.stat().st_size:,} bytes", flush=True)
    (directory / "provenance.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()

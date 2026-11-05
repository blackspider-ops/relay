"""Build the release archive from tracked files, without generated packages."""
import gzip
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile

root = Path(__file__).resolve().parent.parent
version = json.loads((root / "package.json").read_text())["version"]
paths = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode().split("\0")
destination = root / "dist" / f"relay-{version}.tar.gz"
destination.parent.mkdir(exist_ok=True)
with destination.open("wb") as output:
    with gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as archive:
            for name in sorted(p for p in paths if p and not p.startswith("Formula/")):
                source = root / name
                if source.is_symlink() or not source.is_file():
                    raise ValueError("Release files must be regular files: " + name)
                data = source.read_bytes()
                info = tarfile.TarInfo(f"relay-{version}/{name}")
                info.size = len(data)
                info.mode = source.stat().st_mode & 0o777
                info.mtime = 0
                archive.addfile(info, io.BytesIO(data))
print(json.dumps({"path": str(destination), "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(), "version": version}))

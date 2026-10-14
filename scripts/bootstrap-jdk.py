#!/usr/bin/env python3
"""Install only the locked Temurin archive into this project's ignored .tools directory."""
import hashlib
import json
import platform
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
lock = json.loads((root / "toolchains.lock.json").read_text())
key = platform.system().lower() + "-" + platform.machine().lower()
asset = lock["jdk"]["assets"].get(key)
if not asset:
    raise SystemExit(f"No locked JDK archive for {key}; use Temurin {lock['jdk']['version']}.")
tools = root / ".tools"
tools.mkdir(exist_ok=True)
destination = tools / "jdk"
if destination.exists():
    raise SystemExit(f"{destination} already exists; inspect it before reinstalling.")
with tempfile.TemporaryDirectory(dir=tools) as temp:
    temp = Path(temp)
    archive = temp / "jdk.tar.gz"
    subprocess.run(["curl", "--fail", "--location", "--retry", "3", asset["url"], "--output", str(archive)], check=True)
    if hashlib.file_digest(archive.open("rb"), "sha256").hexdigest() != asset["sha256"]:
        raise SystemExit("JDK SHA-256 mismatch")
    unpacked = temp / "unpacked"
    unpacked.mkdir()
    with tarfile.open(archive) as tar:
        tar.extractall(unpacked, filter="data")
    children = list(unpacked.iterdir())
    if len(children) != 1:
        raise SystemExit("Unexpected JDK archive layout")
    shutil.move(str(children[0]), destination)
print(f"Installed verified Temurin {lock['jdk']['version']} in {destination}")

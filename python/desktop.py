"""Optional macOS menu app."""

from pathlib import Path
import plistlib
import shutil
import subprocess
from processes import spawn


def menubar(state, launcher=None, build_only=False):
    from router import VERSION

    launcher = launcher or shutil.which("relay")
    if not launcher:
        raise ValueError("The relay command is not on PATH")
    source = Path(__file__).with_name("Menu.swift")
    app = state / "Relay Menu.app"
    contents = app / "Contents"
    binary = contents / "MacOS" / "RelayMenu"
    binary.parent.mkdir(parents=True, exist_ok=True)
    if not binary.exists() or source.stat().st_mtime > binary.stat().st_mtime:
        compiler = shutil.which("swiftc")
        if not compiler:
            raise ValueError(
                "Swift compiler is unavailable; install Apple command-line tools to build the optional menu app"
            )
        subprocess.run([compiler, str(source), "-o", str(binary)], check=True)
        with (contents / "Info.plist").open("wb") as stream:
            plistlib.dump(
                {
                    "CFBundleIdentifier": "local.relay.menu",
                    "CFBundleName": "Relay",
                    "CFBundleExecutable": "RelayMenu",
                    "CFBundleVersion": VERSION,
                    "RelayCommand": launcher,
                    "LSUIElement": True,
                },
                stream,
            )
        subprocess.run(
            ["/usr/bin/codesign", "--force", "--sign", "-", str(app)],
            check=True,
            capture_output=True,
        )
    if not build_only:
        spawn(
            ["/usr/bin/open", "-n", str(app), "--args", str(launcher)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    return app

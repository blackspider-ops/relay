"""Native menu app and per-user login startup. No account credentials in launch files."""

from pathlib import Path
import hashlib
import os
import plistlib
import shutil
import subprocess
from processes import spawn

LABEL = "local.relay.menu"


def login_item(state, app=None, enabled=True, home=None):
    home = Path(home or Path.home())
    path = home / "Library/LaunchAgents" / (LABEL + ".plist")
    domain = f"gui/{os.getuid()}"
    if not enabled:
        # Removing login startup should not close the menu currently in use.
        path.unlink(missing_ok=True)
        return path
    if path.exists():
        subprocess.run(
            ["/bin/launchctl", "bootout", domain, str(path)], capture_output=True
        )
    if app is None:
        app = Path(state) / "Relay Menu.app"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "Label": LABEL,
        "ProgramArguments": [str(Path(app) / "Contents/MacOS/RelayMenu")],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ProcessType": "Interactive",
        "EnvironmentVariables": {
            "PATH": os.environ.get(
                "PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
            ),
            "RELAY_STATE": str(Path(state).resolve()),
        },
        "ThrottleInterval": 30,
    }
    with path.open("wb") as stream:
        plistlib.dump(payload, stream)
    path.chmod(0o600)
    result = subprocess.run(
        ["/bin/launchctl", "bootstrap", domain, str(path)], capture_output=True
    )
    if result.returncode:
        raise ValueError(
            "Login startup was saved, but launchctl could not load it. Run relay menubar again after signing in to macOS."
        )
    return path


def menubar(state, launcher=None, build_only=False, startup=True):
    from router import VERSION

    launcher = launcher or shutil.which("relay")
    if not launcher:
        raise ValueError("The relay command is not on PATH")
    launcher = str(Path(launcher).absolute())
    source = Path(__file__).with_name("Menu.swift")
    app = state / "Relay Menu.app"
    contents = app / "Contents"
    binary = contents / "MacOS" / "RelayMenu"
    stamp = state / "menu-build.txt"
    (contents / "relay-build.txt").unlink(missing_ok=True)
    identity = hashlib.sha256(
        source.read_bytes() + launcher.encode() + VERSION.encode()
    ).hexdigest()
    if not binary.exists() or not stamp.exists() or stamp.read_text() != identity:
        compiler = shutil.which("swiftc")
        if not compiler:
            raise ValueError(
                "Install Apple command-line tools to build the menu app: xcode-select --install"
            )
        binary.parent.mkdir(parents=True, exist_ok=True)
        temporary = binary.with_suffix(".new")
        subprocess.run([compiler, str(source), "-o", str(temporary)], check=True)
        temporary.replace(binary)
        with (contents / "Info.plist").open("wb") as stream:
            plistlib.dump(
                {
                    "CFBundleIdentifier": LABEL,
                    "CFBundleName": "Relay",
                    "CFBundleExecutable": "RelayMenu",
                    "CFBundleVersion": VERSION,
                    "RelayCommand": launcher,
                    "RelayState": str(state),
                    "RelayNodeDirectory": str(
                        Path(shutil.which("node") or "/usr/bin/node").parent
                    ),
                    "CFBundlePackageType": "APPL",
                    "LSUIElement": True,
                    "NSHighResolutionCapable": True,
                },
                stream,
            )
        subprocess.run(
            ["/usr/bin/codesign", "--force", "--sign", "-", str(app)],
            check=True,
            capture_output=True,
        )
        stamp.write_text(identity)
    if not build_only:
        # Replace only this user's previous Relay menu, preserving all Codex tasks.
        subprocess.run(
            ["/usr/bin/pkill", "-u", str(os.getuid()), "-x", "RelayMenu"],
            capture_output=True,
        )
        if startup:
            login_item(state, app, enabled=True)
        else:
            spawn(
                ["/usr/bin/open", str(app)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    return app


def job_startup(state, action, session=None, launcher=None, home=None):
    """Opt-in recovery of explicitly selected background jobs at Mac login."""
    from service import resolve

    home = Path(home or Path.home())
    root = home / "Library/LaunchAgents"
    if action == "list":
        return [
            p.stem.removeprefix("local.relay.job.")
            for p in root.glob("local.relay.job.*.plist")
        ]
    ident = resolve(state, session)
    path = root / ("local.relay.job." + ident + ".plist")
    domain = f"gui/{os.getuid()}"
    if path.exists():
        subprocess.run(
            ["/bin/launchctl", "bootout", domain, str(path)], capture_output=True
        )
    if action == "disable":
        path.unlink(missing_ok=True)
        return {"session": ident, "startup": False}
    launcher = launcher or shutil.which("relay")
    if not launcher:
        raise ValueError("The relay command is not on PATH")
    root.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        plistlib.dump(
            {
                "Label": "local.relay.job." + ident,
                "ProgramArguments": [
                    str(Path(launcher).absolute()),
                    "restart",
                    ident,
                    "--if-stopped",
                ],
                "RunAtLoad": True,
                "EnvironmentVariables": {
                    "PATH": os.environ.get(
                        "PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
                    ),
                    "RELAY_STATE": str(Path(state).resolve()),
                },
            },
            stream,
        )
    path.chmod(0o600)
    # Do not run a stopped task immediately; load it at the next login.
    return {"session": ident, "startup": True}

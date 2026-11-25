from __future__ import annotations

import argparse
import hashlib
import os
import secrets
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "querygate.yaml"
DEFAULT_ENV = ROOT / ".env"


def write_if_missing(path: Path, content: str) -> None:
    try:
        with path.open("x", encoding="utf-8") as file:
            file.write(content)
    except FileExistsError:
        pass


def initialize() -> None:
    write_if_missing(DEFAULT_CONFIG, (ROOT / "querygate.example.yaml").read_text(encoding="utf-8"))
    template = (ROOT / ".env.example").read_text(encoding="utf-8")
    write_if_missing(DEFAULT_ENV, template.replace(
        "QUERYGATE_ANALYST_TOKEN=", f"QUERYGATE_ANALYST_TOKEN={secrets.token_urlsafe(32)}",
    ))
    print(f"Config: {DEFAULT_CONFIG}")
    print(f"Environment file: {DEFAULT_ENV}")
    print("Fill in the approved views and datasource credentials, then run Start-QueryGate.cmd.")


def checked_path(value: str | Path, label: str) -> Path:
    path = (ROOT / value).resolve()
    if not path.is_file():
        raise SystemExit(f"{label} not found: {path}")
    return path


def prepare_python(install: bool) -> Path:
    venv = ROOT / ".venv"
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file():
        print("Creating the QueryGate virtual environment...", flush=True)
        subprocess.run([sys.executable, "-m", "venv", str(venv)], cwd=ROOT, check=True)

    version = subprocess.run([python, "-c", "import sys; sys.exit(sys.version_info < (3, 11))"])
    if version.returncode:
        raise SystemExit(f"Python 3.11 or newer is required. Check the interpreter in {venv}.")

    dependency_hash = hashlib.sha256((ROOT / "pyproject.toml").read_bytes()).hexdigest()
    marker = venv / ".querygate-dependencies"
    installed_hash = marker.read_text(encoding="ascii").strip() if marker.is_file() else None
    if install or installed_hash != dependency_hash:
        pip = subprocess.run([python, "-m", "pip", "--version"], capture_output=True)
        if pip.returncode:
            subprocess.run([python, "-m", "ensurepip", "--upgrade"], cwd=ROOT, check=True)
        print("Installing QueryGate dependencies...", flush=True)
        subprocess.run(
            [python, "-m", "pip", "--disable-pip-version-check", "install", "--editable", ROOT], cwd=ROOT, check=True,
        )
        marker.write_text(dependency_hash, encoding="ascii")
    return python


def main() -> int:
    if sys.version_info < (3, 11):
        raise SystemExit("Install Python 3.11 or newer.")

    parser = argparse.ArgumentParser(description="Start the local QueryGate REST service.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--initialize", action="store_true", help="create the default YAML and .env files")
    mode.add_argument("--configure", action="store_true", help="open the local Web datasource administration page")
    parser.add_argument("--config", help="path to the QueryGate YAML configuration")
    parser.add_argument("--env-file", help="path to a dotenv file")
    parser.add_argument("--port", type=int, default=8000, help="local HTTP port, defaults to 8000")
    parser.add_argument("--datasource", help="start only this enabled datasource during the current run")
    parser.add_argument("--install", action="store_true", help="rerun dependency installation")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")

    if args.initialize:
        initialize()
        return 0

    source_root = ROOT / "src"
    if not (source_root / "querygate" / "__main__.py").is_file():
        parser.error("QueryGate source files are missing. Extract the complete server ZIP before starting it.")

    selected_config = args.config or os.environ.get("QUERYGATE_CONFIG")
    config = (ROOT / (selected_config or DEFAULT_CONFIG)).resolve()
    env_file = (ROOT / (args.env_file or DEFAULT_ENV)).resolve()
    if not args.configure:
        if selected_config:
            checked_path(config, "Config file")
        if args.env_file:
            checked_path(env_file, "Environment file")

    try:
        python = prepare_python(args.install)
    except (OSError, subprocess.CalledProcessError):
        raise SystemExit("QueryGate dependency setup failed. Check Python and your package index, then try again.") from None

    environment = os.environ.copy()
    environment["QUERYGATE_CONFIG"] = str(config)
    # The launcher owns the source path so a cached editable install can be reused after extraction.
    python_path = [str(source_root)]
    if previous_path := environment.get("PYTHONPATH"):
        python_path.append(previous_path)
    environment["PYTHONPATH"] = os.pathsep.join(python_path)
    built_workbench = ROOT / "web" / "dist"
    if not environment.get("QUERYGATE_WEB_ROOT") and (built_workbench / "index.html").is_file():
        environment["QUERYGATE_WEB_ROOT"] = str(built_workbench)

    command = [python, "-P", "-m", "querygate", "--port", str(args.port)]
    if args.configure:
        command.append("--configure")
    if args.configure or env_file.is_file():
        command.extend(["--env-file", env_file])
    if args.datasource:
        command.extend(["--datasource", args.datasource])
    try:
        return subprocess.run(command, cwd=ROOT, env=environment).returncode
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

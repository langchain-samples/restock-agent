"""Run repeatable local checks. External providers are replaced; no wallet is used."""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    if not shutil.which("sfw"):
        raise SystemExit("Install Socket Firewall (sfw) before running checks")
    if not (ROOT / "node_modules/@stripe/link-cli/dist/cli.js").is_file():
        raise SystemExit("Run sfw npm ci --ignore-scripts first")
    # Child tests cannot inherit a real wallet or model key.
    safe_env = {
        k: v
        for k, v in os.environ.items()
        if not any(word in k.upper() for word in ("API_KEY", "SECRET", "TOKEN", "PASSWORD"))
        and not k.startswith(("LINK_", "RESTOCK_", "LANGSMITH_", "LANGCHAIN_"))
    }
    safe_env.update(
        OPENAI_API_KEY="offline-test-key",
        LANGSMITH_TRACING="false",
        LANGCHAIN_TRACING="false",
        LANGCHAIN_TRACING_V2="false",
        PYTHONDONTWRITEBYTECODE="1",
    )
    python = sys.executable
    steps = [
        [python, "-m", "ruff", "check", "."],
        [python, "-m", "ruff", "format", "--check", "."],
        [python, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
    ]
    for command in steps:
        subprocess.run(command, cwd=ROOT, env=safe_env, check=True)
    # Do not build against the operator's .env or overwrite their deployed app's
    # build directory. A fresh export also tests copying this project elsewhere.
    with tempfile.TemporaryDirectory(prefix="restock-verify-") as temporary:
        app = Path(temporary) / "app"
        build = app / ".mda/build"
        steps = [
            [python, "scripts/export_app.py", "--destination", str(app), "--slack"],
            ["sfw", "uv", "sync", "--project", str(app), "--frozen"],
            ["sfw", "uv", "run", "--project", str(app), "mda", "build", str(app)],
            [
                "sfw",
                "uv",
                "run",
                "--project",
                str(build),
                "python",
                str(ROOT / "tests/run_compiled.py"),
                str(build),
            ],
            [
                "sfw",
                "uv",
                "run",
                "--project",
                str(build),
                "python",
                str(ROOT / "tests/run_slack_compiled.py"),
                str(build),
            ],
            [
                "sfw",
                "uv",
                "run",
                "--project",
                str(build),
                "python",
                str(ROOT / "tests/run_notifications_compiled.py"),
                str(build),
            ],
        ]
        for command in steps:
            subprocess.run(command, cwd=ROOT, env=safe_env, check=True)
    print("PASS: lint, offline suite, clean application build and compiled approval scenarios")


if __name__ == "__main__":
    main()

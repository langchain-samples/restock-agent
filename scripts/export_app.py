"""Copy only the application into .local/app, preserving its .env and MDA deployment state."""

import argparse
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / ".local/app"
FILES = (
    "agent.py",
    "identity.py",
    "pyproject.toml",
    "uv.lock",
    "instructions.md",
    ".env.example",
    ".gitignore",
)
DIRECTORIES = ("restock", "tools", "sandbox", "skills", "channels")


def export(destination=APP, source=ROOT, *, slack=None):
    destination, source = Path(destination), Path(source)
    destination.mkdir(parents=True, exist_ok=True)
    manifest = destination / ".restock-export.json"
    previous = json.loads(manifest.read_text()) if manifest.exists() else []
    if slack is None:
        slack = "channels/slack.py" in previous
    copied = []
    for name in FILES:
        shutil.copyfile(source / name, destination / name)
        copied.append(name)
    for name in DIRECTORIES:
        for item in (source / name).rglob("*"):
            if name == "channels" and not slack and item.name != "link_proxy.py":
                continue
            if item.is_symlink():
                raise ValueError("Application source must not contain symlinks")
            if not item.is_file() or "__pycache__" in item.parts:
                continue
            if item.suffix not in {".py", ".md", ".sh"}:
                continue
            target = destination / item.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(item, target)
            copied.append(str(item.relative_to(source)))
    for name in set(previous) - set(copied):
        path = Path(name)
        # Prune only a source file recorded by this exporter. Settings and MDA
        # deployment metadata are never part of the manifest.
        if path.is_absolute() or ".." in path.parts or path.parts[0] not in DIRECTORIES:
            raise ValueError("Invalid application export manifest")
        target = destination / path
        if target.is_symlink() or not target.resolve().is_relative_to(destination.resolve()):
            raise ValueError("Invalid application export path")
        target.unlink(missing_ok=True)
    manifest.write_text(json.dumps(sorted(copied), indent=2) + "\n")
    print(f"Exported {len(copied)} application files to {destination}")
    print("Existing local settings and deployment metadata were preserved.")
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=APP)
    parser.add_argument("--slack", action="store_const", const=True, default=None)
    args = parser.parse_args()
    export(args.destination, slack=args.slack)

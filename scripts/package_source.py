"""Create a shareable source ZIP from an allowlist, never from the whole working folder."""

import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "README.md",
    "LICENSE",
    "CONTRIBUTING.md",
    "AGENTS.md",
    ".env.example",
    ".gitignore",
    "agent.py",
    "identity.py",
    "instructions.md",
    "pyproject.toml",
    "uv.lock",
    "package.json",
    "package-lock.json",
)
DIRECTORIES = (
    "restock",
    "tools",
    "sandbox",
    "skills",
    "channels",
    "scripts",
    "tests",
    "docs",
    ".github",
)
SECRET_PATTERN = re.compile(
    r"lsv2_pt_[A-Za-z0-9]+|sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{36}|xox[bp]-[A-Za-z0-9-]+|AKIA[A-Z0-9]{16}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
)


def source_files(root=ROOT):
    paths = [root / name for name in FILES]
    for folder in DIRECTORIES:
        paths.extend(
            p
            for p in (root / folder).rglob("*")
            if p.suffix in {".py", ".md", ".sh", ".yml", ".yaml"} and "__pycache__" not in p.parts
        )
    for path in paths:
        if (
            not path.is_file()
            or path.is_symlink()
            or not path.resolve().is_relative_to(root.resolve())
        ):
            raise ValueError("Missing or linked release source: " + str(path.relative_to(root)))
        if SECRET_PATTERN.search(path.read_text()):
            raise ValueError("Review possible credential in: " + str(path.relative_to(root)))
    # The sole environment file in the release must contain empty secret slots.
    for line in (root / ".env.example").read_text().splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if (
            any(word in key for word in ("API_KEY", "PASSWORD", "SECRET", "TOKEN", "SIGNING_KEY"))
            and value.strip()
        ):
            raise ValueError("The public environment template must contain empty secret slots")
    return sorted(set(paths))


def main():
    paths = source_files()
    output = ROOT / ".local/dist/restock-agent-source.zip"
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            archive.write(path, Path("restock-agent") / path.relative_to(ROOT))
    print(f"Packaged {len(paths)} reviewed source files: {output}")
    print("Excluded private settings, Git history, generated builds, and installed dependencies.")


if __name__ == "__main__":
    main()

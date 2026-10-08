"""Configure automatic Slack updates locally. Does not deploy or contact Zinc."""

import argparse
import os
import secrets
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from restock.config import RestockError  # noqa: E402
from restock.notifications import configuration  # noqa: E402
from scripts.preflight import settings_from_file  # noqa: E402

NAMES = {"RESTOCK_PUBLIC_URL", "RESTOCK_UPDATES_SIGNING_KEY"}


def configure(project, url):
    path = project / ".env"
    if not path.is_file() or not (project / "channels/slack.py").is_file():
        raise ValueError("Run scripts/setup.py --slack first.")
    values = settings_from_file(path, {}, names=NAMES)
    values["RESTOCK_PUBLIC_URL"] = url.rstrip("/")
    if not values.get("RESTOCK_UPDATES_SIGNING_KEY"):
        values["RESTOCK_UPDATES_SIGNING_KEY"] = secrets.token_urlsafe(48)
    configuration(values)
    lines = []
    for line in path.read_text().splitlines():
        name = line.strip().removeprefix("export ").split("=", 1)[0].strip()
        if name not in NAMES:
            lines.append(line)
    lines.extend(f"{name}={values[name]}" for name in sorted(NAMES))
    fd, temporary = tempfile.mkstemp(prefix=".updates-", dir=project)
    try:
        with os.fdopen(fd, "w") as output:
            output.write("\n".join(lines) + "\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=ROOT / ".local/app")
    parser.add_argument("--url", required=True, help="This agent's HTTPS deployment origin")
    args = parser.parse_args()
    try:
        configure(args.project, args.url)
    except (ValueError, RestockError):
        print("Check the HTTPS deployment URL, private settings, and Slack app export.")
        return 2
    print("Saved notification settings privately; kept any existing signing key.")
    print("Redeploy, then check an existing live order in its original Slack thread.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

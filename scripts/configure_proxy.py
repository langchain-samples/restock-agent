"""Configure the native Link proxy callback locally; never deploys or logs in."""

import argparse
import os
import secrets
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from restock.config import RestockError  # noqa: E402
from restock.proxy_config import configuration  # noqa: E402
from scripts.preflight import settings_from_file  # noqa: E402

NAMES = {"RESTOCK_PROXY_URL", "RESTOCK_PROXY_SIGNING_KEY", "RESTOCK_LINK_TRANSPORT"}


def configure(project, url):
    path = project / ".env"
    if not path.is_file() or not (project / "channels/link_proxy.py").is_file():
        raise ValueError("Run scripts/setup.py first.")
    values = settings_from_file(path, {}, names=NAMES)
    values["RESTOCK_PROXY_URL"] = url.rstrip("/")
    values["RESTOCK_LINK_TRANSPORT"] = "proxy"
    if not values.get("RESTOCK_PROXY_SIGNING_KEY"):
        values["RESTOCK_PROXY_SIGNING_KEY"] = secrets.token_urlsafe(48)
    configuration(values)
    lines = [
        line
        for line in path.read_text().splitlines()
        if line.strip().removeprefix("export ").split("=", 1)[0].strip() not in NAMES
    ]
    lines.extend(f"{name}={values[name]}" for name in sorted(NAMES))
    fd, temporary = tempfile.mkstemp(prefix=".proxy-", dir=project)
    try:
        with os.fdopen(fd, "w") as output:
            output.write("\n".join(lines) + "\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=ROOT / ".local/app")
    parser.add_argument("--url", required=True, help="Public HTTPS origin serving this same app")
    args = parser.parse_args()
    try:
        configure(args.project, args.url)
    except (ValueError, RestockError):
        print("Check the HTTPS app URL, private settings, and current app export.")
        return 2
    print("Saved proxy settings privately; kept any existing signing key.")
    print("Restart the local app or redeploy to activate them. No wallet was accessed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

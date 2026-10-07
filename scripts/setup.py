"""Prepare the app and create a private settings file only when one does not exist."""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.export_app import export


def initialize(app: Path) -> bool:
    try:
        fd = os.open(app / ".env", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as output:
        output.write((app / ".env.example").read_text())
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slack", action="store_const", const=True, default=None)
    app = export(slack=parser.parse_args().slack)
    created = initialize(app)
    print("Created private settings." if created else "Kept your existing private settings.")
    print(f"Edit {app / '.env'} privately, then run scripts/preflight.py.")

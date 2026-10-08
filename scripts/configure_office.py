"""Create or update private office delivery details, with visible terminal input."""

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from restock.config import RestockError
from restock.storage import notification_email, validate_office
from scripts.office_connection import OfficeUpdateError, OfficeUpdater


def read_field(title, *, optional=False, default="", max_length=150):
    prompt = f"{title} [{default}]: " if default else f"{title}: "
    while True:
        value = input(prompt).strip() or default
        if not value and not optional:
            print(f"{title} is required. Please enter it before continuing.")
        elif len(value) > max_length:
            print(f"{title} must be {max_length} characters or fewer. Please try again.")
        else:
            return value


def collect_office():
    label = read_field("Office label shown in chat", default="Office", max_length=80)
    fields = {
        "first_name": "Recipient first name",
        "last_name": "Recipient last name",
        "address_line1": "Street address",
        "address_line2": "Suite or floor (optional)",
        "city": "City",
        "state": "Two-letter state, such as MA",
        "postal_code": "ZIP code",
        "phone_number": "Delivery phone number",
    }
    address = {"country": "US"}
    for key, title in fields.items():
        value = read_field(title, optional=key == "address_line2")
        if value:
            address[key] = value.upper() if key == "state" else value
    office = {"label": label, "shipping_address": address}
    print(
        "Optional: Zinc can email order updates. Availability depends on Zinc and an extra fee may apply."
    )
    print("Any fee must fit the upfront amount you choose in chat. Leave blank to disable email.")
    while True:
        email = read_field("Order-update email (optional)", optional=True, max_length=254)
        if not email:
            break
        try:
            office["notification_email"] = notification_email(email)
            break
        except RestockError:
            print("Enter one valid email address, or leave blank to disable email.")
    return validate_office(office)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=".local/app")
    parser.add_argument(
        "--update", action="store_true", help="Replace this agent's existing details"
    )
    parser.add_argument(
        "--deployment", default="restock", help="Deployment name for --update (default: restock)"
    )
    args = parser.parse_args()
    if not sys.stdin.isatty():
        parser.error("Run this interactively in your own terminal")
    print("Enter the office's US delivery details. They are sent privately to Connections.")
    print("Your answers are visible as you type. Press Enter after each answer.")
    print(
        "Suite/floor and order-update email can be blank. Press Enter for the default office label."
    )
    saving_update = False
    try:
        if args.update:
            with OfficeUpdater.from_project(Path(args.project), args.deployment) as updater:
                updater.resolve()
                print(f"Updating {updater.slug} for deployment {updater.deployment}.")
                print("Re-enter all fields. A blank suite/floor or email clears the old value.")
                print("Cancel pending orders before changing delivery details.")
                value = collect_office()
                saving_update = True
                updater.replace(value)
            print("Updated office details. Start a new order; no redeploy is needed.")
            return 0
        value = collect_office()
    except (KeyboardInterrupt, EOFError):
        if saving_update:
            print(
                "\nUpdate interrupted. It may have saved; re-run with the same details before ordering."
            )
        else:
            print("\nCanceled. No office Connection was saved.")
        return 130
    except RestockError:
        print("Office details were not accepted. Run this command again to correct them.")
        return 2
    except OfficeUpdateError as error:
        print(str(error))
        return 2
    except (ValueError, OSError):
        print("Office setup could not finish. Check the project settings and connection.")
        return 2
    result = subprocess.run(
        [
            str(Path(sys.executable).parent / "mda"),
            "connections",
            "create",
            "restock-office",
            "--project",
            args.project,
        ],
        input=json.dumps(value),
        text=True,
        capture_output=True,
        check=False,
    )
    # Provider errors can echo input. Keep the receipt private too.
    if result.returncode:
        print(
            "Connection was not saved. Check MDA sign-in, deployment access and whether this slug already exists."
        )
        return result.returncode
    print("Saved the restock-office Connection.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

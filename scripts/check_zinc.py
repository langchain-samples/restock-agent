"""Check Zinc reachability. Only a prepared cart can establish its Stripe payment support."""

import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from restock.config import RestockError
from restock.zinc import Zinc


async def main():
    try:
        result = await Zinc().availability()
    except RestockError as error:
        print(f"BLOCKED: {error}")
        print("No payment was requested. This bodyless check does not establish Stripe support.")
        return 2
    except (httpx.HTTPError, ValueError, TypeError):
        print("UNAVAILABLE: could not verify Zinc's payment route. No payment was requested.")
        return 3
    print(result["status"])
    print(result["notice"])
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

"""Standalone harness to exercise the Watercare API outside Home Assistant.

Usage:
    WATERCARE_EMAIL=you@example.com WATERCARE_PASSWORD=secret \
        .venv-test/bin/python scripts/test_api.py [endpoint]

Reads credentials from env vars so they never touch disk/history.
Default endpoint is "halfhourly". The api module only depends on aiohttp,
so no Home Assistant install is required.
"""

import asyncio
import importlib.util
import logging
import os
import sys

# Load api.py directly by path so we skip custom_components/watercare/__init__.py,
# which imports Home Assistant. api.py itself only needs aiohttp.
_API_PATH = os.path.join(
    os.path.dirname(__file__), "..", "custom_components", "watercare", "api.py"
)
_spec = importlib.util.spec_from_file_location("watercare_api", _API_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
WatercareApi = _mod.WatercareApi

logging.basicConfig(level=logging.DEBUG)


async def main():
    email = os.environ.get("WATERCARE_EMAIL")
    password = os.environ.get("WATERCARE_PASSWORD")
    if not email or not password:
        print("ERROR: set WATERCARE_EMAIL and WATERCARE_PASSWORD env vars")
        sys.exit(1)

    endpoint = sys.argv[1] if len(sys.argv) > 1 else "halfhourly"
    start_date = sys.argv[2] if len(sys.argv) > 2 else None
    end_date = sys.argv[3] if len(sys.argv) > 3 else None

    api = WatercareApi(email, password)
    print(f"\n=== Calling get_data({endpoint!r}, {start_date!r}, {end_date!r}) ===")
    result = await api.get_data(endpoint, start_date, end_date)
    print(f"\n=== RESULT (type={type(result).__name__}) ===")
    if result is None:
        print("None  (request failed — see logs above)")
    else:
        print(result[:2000])
        print(f"\n... total length: {len(result)} chars")


if __name__ == "__main__":
    asyncio.run(main())

#!/usr/bin/env python3
"""Smoke test for your Anthropic API key.

Does NOT print the key or any fragment of it. Only reports whether the key
is present in the environment and whether a trivial /v1/messages call works.
"""

import json
import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY environment variable not set")
        return 1

    print("Found API key: present (value hidden)")
    print("\nTesting API connection...")

    request_body = {
        "model": (
            os.environ.get("SCAN_ANTHROPIC_MODEL")
            or os.environ.get("SCAN_MODEL")
            or "claude-3-haiku-20240307"
        ),
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "Say hello in one word"}],
    }

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(request_body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
            print("\n[OK] API key is working!")
            print(f"Claude's response: {result['content'][0]['text']}")
            return 0
    except urllib.error.HTTPError as exc:
        print(f"\n[FAIL] HTTP Error {exc.code}: {exc.reason}")
        try:
            print(f"Details: {exc.read().decode('utf-8')}")
        except Exception:
            pass
        return 2
    except Exception as exc:
        print(f"\n[FAIL] {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())

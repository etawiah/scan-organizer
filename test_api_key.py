#!/usr/bin/env python3
"""Test if your Anthropic API key works"""

import os
import json
import urllib.request

# Get API key from environment
api_key = os.environ.get('ANTHROPIC_API_KEY')

if not api_key:
    print("ERROR: ANTHROPIC_API_KEY environment variable not set")
    exit(1)

print(f"Found API key: {api_key[:10]}...{api_key[-4:]}")
print("\nTesting API connection...")

try:
    request_body = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 100,
        "messages": [
            {"role": "user", "content": "Say hello in one word"}
        ]
    }
    
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(request_body).encode('utf-8'),
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01"
        },
        method="POST"
    )
    
    with urllib.request.urlopen(req, timeout=30) as response:
        result = json.loads(response.read().decode('utf-8'))
        print("\n✓ SUCCESS! API key is working!")
        print(f"Claude's response: {result['content'][0]['text']}")
        
except urllib.error.HTTPError as e:
    print(f"\n✗ FAILED: HTTP Error {e.code}")
    print(f"Reason: {e.reason}")
    error_body = e.read().decode('utf-8')
    print(f"Details: {error_body}")
    
except Exception as e:
    print(f"\n✗ FAILED: {e}")

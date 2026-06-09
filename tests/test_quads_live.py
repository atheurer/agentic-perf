#!/usr/bin/env python3
"""Live smoke test for the QUADS API client.

Tests against the real QUADS API — does NOT create assignments or schedule hosts.
Safe to run without side effects.
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from providers.quads import QuadsClient
from providers.secrets.local import LocalSecretsProvider


async def main():
    print("=== QUADS API Client Live Test ===\n")

    # Load credentials from secrets provider
    secrets = LocalSecretsProvider()
    raw = await secrets.get_secret("quads/config.json")
    if not raw:
        print("FAIL: No QUADS config at ~/.agentic-perf/secrets/quads/config.json")
        return 1
    config = json.loads(raw)
    print(f"Config loaded: api_host={config['api_host']}, owner={config['owner']}")

    client = await QuadsClient.from_secrets(secrets)

    try:
        # Test 1: List available hosts (no auth required)
        print("\n--- Test 1: List available hosts ---")
        hosts = await client.get_available()
        print(f"  Available hosts: {len(hosts)}")
        for h in hosts[:5]:
            print(f"    {h['hostname']}")
        if len(hosts) > 5:
            print(f"    ... and {len(hosts) - 5} more")

        # Test 2: Filter by model
        print("\n--- Test 2: Filter by model (r650) ---")
        r650s = await client.get_available(model_filter="r650")
        print(f"  R650 hosts available: {len(r650s)}")
        for h in r650s[:3]:
            print(f"    {h['hostname']}")

        # Test 3: Login (verify credentials)
        print("\n--- Test 3: Login ---")
        token = await client._login()
        print(f"  Token obtained: {token[:20]}...")

        # Test 4: Filter with NIC details
        print("\n--- Test 4: Filter by NIC vendor (Intel) ---")
        intel_hosts = await client.get_available(vendor_filter="Intel")
        print(f"  Hosts with Intel NICs: {len(intel_hosts)}")
        for h in intel_hosts[:3]:
            print(f"    {h['hostname']} model={h['model']} cores={h['cores']} mem={h['memory_gb']}GB")
            for nic in h.get("matching_nics", [])[:2]:
                print(f"      {nic['name']}: {nic['vendor']} {nic.get('speed', '?')}G")

        print("\n=== All tests passed ===")
        return 0

    except Exception as e:
        print(f"\nFAIL: {e}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        await client.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

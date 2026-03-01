"""
Genera customer_profiles.json — eseguire UNA SOLA VOLTA prima della gara.

    python generate_customer_profiles.py

Scarica le bio da https://hackablog.datapizza.tech/tag/bio/ (tutte le pagine),
classifica ogni cliente con keyword heuristic (no LLM, ~10-15s) e salva il
risultato in customer_profiles.json nella root del progetto.
"""

import asyncio
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(__file__))

from agents.customer_profiler import CustomerProfilerAgent


async def main() -> None:
    print("=== Generazione profili cliente ===\n")
    agent = CustomerProfilerAgent()
    profiles = await agent.run_all()

    if not profiles:
        print("ERRORE: nessun profilo estratto.")
        return

    archetypes = Counter(p["archetype"] for p in profiles)
    tiers = Counter(p["preferred_tier"] for p in profiles)

    print("\n=== Distribuzione Archetipo ===")
    for archetype, count in sorted(archetypes.items(), key=lambda x: -x[1]):
        print(f"  {archetype:25s}: {count:4d} ({count / len(profiles) * 100:.1f}%)")

    print("\n=== Distribuzione Tier ===")
    for tier, count in sorted(tiers.items(), key=lambda x: -x[1]):
        print(f"  {tier:10s}: {count:4d} ({count / len(profiles) * 100:.1f}%)")

    print(f"\nTotale: {len(profiles)} profili salvati in customer_profiles.json")


if __name__ == "__main__":
    asyncio.run(main())

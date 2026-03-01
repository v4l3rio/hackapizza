"""
CustomerProfilerAgent
=====================
Classificazione profili cliente dal blog bio tramite LLM (batch JSON, no tool calling).

Flusso:
  - Eseguire generate_customer_profiles.py una volta prima della gara per
    generare customer_profiles.json con tutti i profili noti.
  - Durante ogni fase 'stopped', CustomerProfilerAgent.run_once() controlla
    se ci sono nuovi slug non ancora nel JSON, li classifica e li appende.

Caricamento in-game:
    from agents.customer_profiler import load_customer_profiles
    profiles = load_customer_profiles()
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any

import aiohttp

from infrastructure.llm_factory import get_llm_client_small
from utils.logger import log, log_error


BIO_INDEX_URL = "https://hackablog.datapizza.tech/tag/bio/"
PROFILES_JSON = Path(__file__).parent.parent / "customer_profiles.json"
_HEADERS = {"User-Agent": "HackapizzaProfilerBot/1.0"}
_TIMEOUT = aiohttp.ClientTimeout(total=15)

_BATCH_SIZE = 30       # entries per LLM call
_LLM_CONCURRENCY = 5  # max parallel LLM calls

_TIER = {
    "space_sage": "PRESTIGE",
    "galactic_explorer": "BUDGET",
    "astrobaron": "STANDARD",
    "orbital_family": "STANDARD",
}
_SENS = {
    "space_sage": "low",
    "galactic_explorer": "high",
    "astrobaron": "medium",
    "orbital_family": "medium",
}
_ARCHETYPES = set(_TIER.keys())

_SYSTEM_PROMPT = """\
Sei un classificatore di clienti per un ristorante spaziale di gioco.
Classifica ogni cliente in uno di questi archetipi in base al nome e all'estratto della bio:

- space_sage: apprezza cultura, filosofia, tradizione, ingredienti esotici, pasti lenti e riflessivi, prestige
- galactic_explorer: urgenza, velocità, economia, lavoratore dei trasporti, budget ridotto, poco tempo
- astrobaron: riunioni di affari, eleganza, status, efficienza, qualità rapida, gala
- orbital_family: famiglia, equilibrio, comodità, pasti quotidiani, gruppo

Rispondi SOLO con un array JSON, senza testo aggiuntivo.
Formato: [{"name": "...", "archetype": "..."}, ...]
I valori validi per archetype sono: space_sage, galactic_explorer, astrobaron, orbital_family.
"""


# ---------------------------------------------------------------------------
# HTML parsing
# ---------------------------------------------------------------------------

def _slug_to_name(slug: str) -> str:
    return " ".join(w.capitalize() for w in slug.strip("/").split("-"))


def _extract_entries(html: str) -> list[tuple[str, str]]:
    """Estrae (name, excerpt) dall'HTML di una pagina indice /tag/bio/."""
    base = "hackablog.datapizza.tech"
    skip = {"tag", "author", "page", "assets", "rss"}
    seen: set[str] = set()
    entries: list[tuple[str, str]] = []

    slug_re = re.compile(
        r'href=["\'](?:https?://' + re.escape(base) + r')?/([\w][\w-]*)/["\']',
        re.IGNORECASE,
    )
    for m in slug_re.finditer(html):
        slug = m.group(1)
        if slug in skip or slug in seen:
            continue
        seen.add(slug)
        name = _slug_to_name(slug)

        tail = html[m.end(): m.end() + 3000]
        p_re = re.compile(
            r'<p[^>]*>\s*(' + re.escape(name) + r'[^<]{5,}?)\s*</p>',
            re.DOTALL | re.IGNORECASE,
        )
        m2 = p_re.search(tail)
        if m2:
            excerpt = re.sub(r'\s+', ' ', m2.group(1)).strip()
        else:
            m3 = re.search(r'<p[^>]*>\s*([^<]{20,}?)\s*</p>', tail, re.DOTALL)
            excerpt = re.sub(r'\s+', ' ', m3.group(1)).strip() if m3 else ""

        entries.append((name, excerpt))

    return entries


# ---------------------------------------------------------------------------
# LLM classifier
# ---------------------------------------------------------------------------

async def _classify_batch_llm(
    entries: list[tuple[str, str]],
    sem: asyncio.Semaphore,
) -> dict[str, str]:
    """
    Classifica un batch di (name, excerpt) via LLM.
    Ritorna dict {name -> archetype}.
    """
    user_lines = []
    for i, (name, excerpt) in enumerate(entries, 1):
        user_lines.append(f"{i}. Nome: {name}\n   Bio: {excerpt[:300]}")
    user_msg = "\n\n".join(user_lines)

    llm = get_llm_client_small()
    async with sem:
        try:
            response = await llm.a_invoke(
                input=user_msg,
                system_prompt=_SYSTEM_PROMPT,
            )
            raw = response.text or ""
        except Exception as exc:
            log_error("profiler", "-", "llm", f"LLM call failed: {exc}")
            return {}

    # Parse JSON from response
    try:
        # extract JSON array even if surrounded by markdown fences
        m = re.search(r'\[.*\]', raw, re.DOTALL)
        if not m:
            log_error("profiler", "-", "llm", f"No JSON array in response: {raw[:200]}")
            return {}
        items = json.loads(m.group(0))
        result: dict[str, str] = {}
        for item in items:
            name = item.get("name", "")
            archetype = item.get("archetype", "orbital_family")
            if archetype not in _ARCHETYPES:
                archetype = "orbital_family"
            result[name] = archetype
        return result
    except Exception as exc:
        log_error("profiler", "-", "parse", f"JSON parse error: {exc} | raw: {raw[:200]}")
        return {}


async def _classify_all_llm(entries: list[tuple[str, str]]) -> dict[str, str]:
    """Classifica tutti gli entries in batch concorrenti via LLM."""
    sem = asyncio.Semaphore(_LLM_CONCURRENCY)
    batches = [
        entries[i: i + _BATCH_SIZE]
        for i in range(0, len(entries), _BATCH_SIZE)
    ]
    log("profiler", "-", "llm", f"Classificazione LLM: {len(entries)} entries in {len(batches)} batch")
    results = await asyncio.gather(
        *[_classify_batch_llm(b, sem) for b in batches],
        return_exceptions=True,
    )
    merged: dict[str, str] = {}
    for r in results:
        if isinstance(r, dict):
            merged.update(r)
    return merged


# ---------------------------------------------------------------------------
# JSON persistence
# ---------------------------------------------------------------------------

def load_customer_profiles(path: Path = PROFILES_JSON) -> list[dict[str, Any]]:
    """Carica i profili dal JSON. Ritorna lista vuota se il file non esiste."""
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_customer_profiles(profiles: list[dict[str, Any]], path: Path = PROFILES_JSON) -> None:
    path.write_text(json.dumps(profiles, indent=2, ensure_ascii=False), encoding="utf-8")


def _build_profile(name: str, excerpt: str, archetype: str, ts: float) -> dict[str, Any]:
    return {
        "name": name,
        "archetype": archetype,
        "preferred_tier": _TIER[archetype],
        "price_sensitivity": _SENS[archetype],
        "excerpt": excerpt,
        "recorded_at": ts,
    }


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class CustomerProfilerAgent:
    """
    Classifica profili cliente dal blog bio tramite LLM (batch JSON).

    - run_all(): scarica tutte le pagine e classifica tutto (per generate_customer_profiles.py)
    - run_once(): controlla solo i nuovi slug non ancora nel JSON, appende (per manager stopped)
    """

    def __init__(self, json_path: Path = PROFILES_JSON) -> None:
        self._json_path = json_path

    # ------------------------------------------------------------------ fetch

    async def _fetch(self, session: aiohttp.ClientSession, url: str) -> str | None:
        try:
            async with session.get(url, timeout=_TIMEOUT) as resp:
                return await resp.text(errors="ignore") if resp.status == 200 else None
        except Exception as exc:
            log_error("profiler", "-", "fetch", str(exc))
            return None

    async def _last_page(self, session: aiohttp.ClientSession) -> int:
        """Binary search per l'ultima pagina (~7 richieste)."""
        lo, hi = 1, 256
        while lo < hi:
            mid = (lo + hi + 1) // 2
            html = await self._fetch(session, f"{BIO_INDEX_URL}page/{mid}/")
            if html and _extract_entries(html):
                lo = mid
            else:
                hi = mid - 1
        return lo

    async def _fetch_page(self, session: aiohttp.ClientSession, page: int) -> list[tuple[str, str]]:
        url = BIO_INDEX_URL if page == 1 else f"{BIO_INDEX_URL}page/{page}/"
        html = await self._fetch(session, url)
        return _extract_entries(html) if html else []

    async def _fetch_all_entries(self, session: aiohttp.ClientSession) -> list[tuple[str, str]]:
        """Fetch concorrente di tutte le pagine indice."""
        total = await self._last_page(session)
        log("profiler", "-", "index", f"Pagine totali: {total}")
        results = await asyncio.gather(
            *[self._fetch_page(session, p) for p in range(1, total + 1)],
            return_exceptions=True,
        )
        entries: list[tuple[str, str]] = []
        for r in results:
            if isinstance(r, list):
                entries.extend(r)
        return entries

    # ------------------------------------------------------------------ public API

    async def run_all(self) -> list[dict[str, Any]]:
        """
        Scarica e classifica TUTTI i profili (ignorando il JSON esistente).
        Usato da generate_customer_profiles.py.
        """
        log("profiler", "-", "agent", "run_all avviato")
        async with aiohttp.ClientSession(headers=_HEADERS) as session:
            entries = await self._fetch_all_entries(session)

        # Deduplica
        seen: set[str] = set()
        unique: list[tuple[str, str]] = []
        for name, excerpt in entries:
            if name not in seen:
                seen.add(name)
                unique.append((name, excerpt))

        log("profiler", "-", "agent", f"Entries uniche: {len(unique)}")

        # LLM classification
        classifications = await _classify_all_llm(unique)

        ts = time.time()
        profiles: list[dict[str, Any]] = []
        for name, excerpt in unique:
            archetype = classifications.get(name, "orbital_family")
            profiles.append(_build_profile(name, excerpt, archetype, ts))

        log("profiler", "-", "agent", f"Profili estratti: {len(profiles)}")
        _save_customer_profiles(profiles, self._json_path)
        return profiles

    async def run_once(self, memory: Any | None = None) -> int:
        """
        Controlla se ci sono nuovi slug non ancora nel JSON.
        Classifica solo i nuovi e li appende al JSON (e a memory se fornita).
        Ritorna il numero di nuovi profili aggiunti.
        """
        existing = load_customer_profiles(self._json_path)
        known_names: set[str] = {p["name"] for p in existing}

        async with aiohttp.ClientSession(headers=_HEADERS) as session:
            entries = await self._fetch_all_entries(session)

        new_entries = [(n, e) for n, e in entries if n not in known_names]
        if not new_entries:
            log("profiler", "-", "agent", "Nessun nuovo cliente trovato")
            return 0

        log("profiler", "-", "agent", f"{len(new_entries)} nuovi clienti da classificare")

        classifications = await _classify_all_llm(new_entries)

        ts = time.time()
        new_profiles: list[dict[str, Any]] = []
        for name, excerpt in new_entries:
            archetype = classifications.get(name, "orbital_family")
            new_profiles.append(_build_profile(name, excerpt, archetype, ts))
            log("profiler", "-", "profile", f"{name} -> {archetype}")

        all_profiles = existing + new_profiles
        _save_customer_profiles(all_profiles, self._json_path)

        if memory is not None:
            memory.customer_profiles = all_profiles

        log("profiler", "-", "agent",
            f"Aggiunti {len(new_profiles)} profili | Totale: {len(all_profiles)}")
        return len(new_profiles)

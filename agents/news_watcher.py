"""
NewsWatcherAgent
================
Scraper continuo del blog "Cronache dal Cosmo" (https://hackablog.datapizza.tech/).

Gira in background per tutta la partita come task asyncio.
Ogni POLL_INTERVAL_SECONDS:
  1. Recupera gli articoli nuovi dal blog
  2. Usa l'LLM per estrarne informazioni azionabili
  3. Salva gli insight in StrategyMemory per uso in disinformazione
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from html.parser import HTMLParser
from typing import Any

import aiohttp

from datapizza.agents import Agent
from datapizza.tools import tool

from infrastructure.llm_factory import get_llm_client
from utils.logger import log, log_error


BLOG_URL = "https://hackablog.datapizza.tech/"
NEWS_INDEX_URL = "https://hackablog.datapizza.tech/tag/news/"  # solo articoli di gioco, no bio
POLL_INTERVAL_SECONDS = 90  # controlla il blog ogni 90 secondi


# ---------------------------------------------------------------------------
# HTML → testo (nessuna dipendenza esterna)
# ---------------------------------------------------------------------------

def _html_to_text(html: str) -> str:
    """Converte HTML in testo pulito usando regex (no dipendenze esterne)."""
    # Estrai solo il contenuto del body (ignora head con metadati/script)
    body_match = re.search(r'<body[^>]*>(.*?)</body>', html, re.DOTALL | re.IGNORECASE)
    content = body_match.group(1) if body_match else html
    # Rimuovi blocchi script/style (separatamente per evitare cross-tag matching)
    content = re.sub(r'<script[^>]*>.*?</script>', ' ', content, flags=re.DOTALL | re.IGNORECASE)
    content = re.sub(r'<style[^>]*>.*?</style>', ' ', content, flags=re.DOTALL | re.IGNORECASE)
    # Rimuovi tutti i tag HTML rimanenti
    text = re.sub(r'<[^>]+>', ' ', content)
    # Decodifica entità HTML comuni
    text = (text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
            .replace('&nbsp;', ' ').replace('&#x3D;', '=').replace('&quot;', '"').replace('&#x27;', "'"))
    # Normalizza spazi
    return re.sub(r'\s+', ' ', text).strip()


_SKIP_EXTENSIONS = {".css", ".js", ".xml", ".json", ".png", ".jpg", ".gif", ".svg", ".ico", ".woff", ".woff2"}
_SKIP_PATH_PREFIXES = ("/assets/", "/public/", "/rss", "/page/", "/webmentions/", "/tag/", "/author/")


def _is_article_url(url: str, base_url: str) -> bool:
    """Ritorna True solo se l'URL sembra un vero articolo del blog."""
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url)
        path = parsed.path.rstrip("/")
        # deve essere sullo stesso dominio
        base_domain = urlparse(base_url).netloc
        if parsed.netloc != base_domain:
            return False
        # escludi root
        if not path or path == "":
            return False
        # escludi file con estensioni non-HTML
        _, ext = path.rsplit(".", 1) if "." in path.split("/")[-1] else (path, "")
        if "." + ext in _SKIP_EXTENSIONS:
            return False
        # escludi percorsi di sistema/navigazione
        for prefix in _SKIP_PATH_PREFIXES:
            if path.startswith(prefix) or path == prefix.rstrip("/"):
                return False
        return True
    except Exception:
        return False


def _extract_links_from_html(html: str, base_url: str) -> list[str]:
    """Ritorna URL assoluti di articoli trovati nell'HTML (filtra CSS/JS/RSS/ecc.)."""
    base_domain = base_url.split("//", 1)[-1].rstrip("/")
    hrefs = re.findall(r'href=["\']([^"\'#?][^"\']*)["\']', html)
    links: list[str] = []
    for href in hrefs:
        if href.startswith("http"):
            absolute = href
        elif href.startswith("/"):
            scheme = base_url.split("//")[0]
            absolute = f"{scheme}//{base_domain}{href}"
        else:
            absolute = base_url.rstrip("/") + "/" + href
        if base_domain in absolute and _is_article_url(absolute, base_url):
            links.append(absolute)
    seen: set[str] = set()
    result: list[str] = []
    for lnk in links:
        if lnk not in seen:
            seen.add(lnk)
            result.append(lnk)
    return result


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """
Sei l'agente di sorveglianza notizie per un ristorante galattico in un hackathon competitivo.
REGOLA FONDAMENTALE: rispondi SEMPRE e SOLO in italiano, inclusi tutti i campi che passi a record_insights.

Il blog "Cronache dal Cosmo" pubblica eventi che influenzano le dinamiche del gioco:
- Scarsità o surplus di ingredienti (cambiano i prezzi all'asta e al mercato)
- Crisi o opportunità commerciali (rotte interstellari, embarghi, surplus improvvisi)
- Decisioni della Federazione (nuove restrizioni, priorità culturali)
- Fenomeni cosmici (alterano mercato o clientela)

Il gioco ha fasi: speaking → closed_bid → waiting → serving → stopped.
Le notizie impattano principalmente:
  - closed_bid: se un ingrediente è scarso, offri prezzi più alti per aggiudicartelo
  - waiting/serving: aggiorna menu e strategie di mercato di conseguenza

Il tuo compito: leggere l'articolo fornito ed estrarre insight CONCRETI e AZIONABILI
chiamando record_insights. Sii specifico sugli ingredienti e sulle azioni.
Tutto il testo che produci (headline, azioni, sintesi) deve essere in italiano.
"""


# ---------------------------------------------------------------------------
# Agent class
# ---------------------------------------------------------------------------

class NewsWatcherAgent(Agent):
    """
    Agente di background: scraper del blog + analisi LLM delle notizie.

    Utilizzo standalone:
        agent = NewsWatcherAgent()
        await agent.run_once()     # singola passata
        await agent.run_loop()     # loop infinito

    Utilizzo integrato (in-game):
        task = agent.start()       # avvia asyncio task in background
        agent.stop()               # cancella il task
    """

    name = "news_watcher_agent"
    system_prompt = _SYSTEM_PROMPT

    def __init__(self) -> None:
        self._seen_urls: set[str] = set()           # URL già analizzati
        self._seen_headlines: set[str] = set()      # headline normalizzati già salvati (anti-duplicati)
        self._ins: list[dict[str, Any]] = []   # insights raccolti durante la sessione
        self._strategy_memory: Any | None = None    # StrategyMemory condivisa (opzionale)
        self._polling_task: asyncio.Task | None = None
        super().__init__(client=get_llm_client(), max_steps=3)

    @staticmethod
    def _normalize_headline(headline: str) -> str:
        """Normalizza un titolo per il confronto anti-duplicati."""
        return re.sub(r'\W+', ' ', headline.lower()).strip()

    # ------------------------------------------------------------------ tools

    @tool(
        name="record_insights",
        description=(
            "Registra le informazioni estratte da un articolo del blog. "
            "TUTTI i campi testuali devono essere SCRITTI IN ITALIANO. "
            "Parametri: "
            "'headline' (str) — titolo o sintesi breve dell'articolo IN ITALIANO; "
            "'ingredients_affected' (list[str]) — ingredienti coinvolti (lista vuota se nessuno); "
            "'direction' (str) — 'scarcity' se scarsità/rincaro, 'surplus' se abbondanza/ribasso, "
            "'neutral' se nessun impatto diretto sui prezzi; "
            "'actions' (list[str]) — azioni concrete consigliate IN ITALIANO "
            "(es: ['aumentare le offerte su farina', 'evitare piatti con pomodoro', "
            "'aggiungere tartufo nebulare al menu', 'vendere surplus di spezie sul mercato']); "
            "'priority' (str) — 'high' se impatto immediato sul turno, 'medium' se rilevante "
            "nei prossimi turni, 'low' se notizia generica; "
            "'raw_summary' (str, opzionale) — breve sintesi dell'articolo in italiano."
        ),
    )
    async def record_insights(
        self,
        headline: str,
        ingredients_affected: list,
        direction: str,
        actions: list,
        priority: str,
        raw_summary: str = "",
    ) -> str:
        """Salva l'insight in memoria e lo propaga alla StrategyMemory condivisa."""
        # Deduplicazione semantica: skip se headline molto simile a uno già visto
        norm = self._normalize_headline(str(headline))
        if norm in self._seen_headlines:
            log("news", "—", "dedup", f"Notizia duplicata ignorata: {headline!r}")
            return f"Notizia duplicata, ignorata: {headline}"
        self._seen_headlines.add(norm)

        insight: dict[str, Any] = {
            "headline": str(headline),
            "ingredients_affected": [str(i) for i in ingredients_affected],
            "direction": str(direction),
            "actions": [str(a) for a in actions],
            "priority": str(priority),
            "raw_summary": str(raw_summary),
            "recorded_at": time.time(),
        }
        self._ins.append(insight)
        # Propaga anche alla StrategyMemory condivisa (se disponibile)
        if self._strategy_memory is not None:
            self._strategy_memory.news_insights.append(insight)

        log(
            "news", "—", "insight",
            f"[{priority.upper()}] {headline}"
            + (f" | ingredienti={ingredients_affected}" if ingredients_affected else "")
            + (f" | azioni={actions}" if actions else ""),
        )
        return f"Insight registrato: {headline}"

    # ------------------------------------------------------------------ fetch helpers

    async def _fetch_html(self, url: str) -> str | None:
        """GET url → HTML grezzo, None in caso di errore."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=20),
                    headers={"User-Agent": "HackapizzaNewsBot/1.0"},
                ) as resp:
                    if resp.status != 200:
                        log_error("news", "—", "fetch", f"HTTP {resp.status} for {url}")
                        return None
                    return await resp.text(errors="ignore")
        except Exception as exc:
            log_error("news", "—", "fetch", f"Fetch failed for {url}: {exc}")
            return None

    @staticmethod
    def _norm_url(url: str) -> str:
        """Normalizza URL rimuovendo trailing slash per deduplicazione."""
        return url.rstrip("/")

    async def _get_new_article_urls(self) -> list[str]:
        """
        Scarica la pagina indice delle NEWS (non le bio, non la homepage)
        e ritorna gli URL degli articoli non ancora analizzati.
        """
        html = await self._fetch_html(NEWS_INDEX_URL)
        if not html:
            return []

        all_links = _extract_links_from_html(html, BLOG_URL)
        article_urls = [
            lnk for lnk in all_links
            if self._norm_url(lnk) not in self._seen_urls
        ]
        return article_urls

    # ------------------------------------------------------------------ analysis

    async def _analyze_url(self, url: str) -> None:
        """Scarica e analizza un singolo articolo del blog."""
        html = await self._fetch_html(url)
        if not html:
            self._seen_urls.add(self._norm_url(url))
            return

        text = _html_to_text(html)
        if len(text.strip()) < 30:
            log("news", "—", "fetch", f"Contenuto troppo breve per {url}, salto")
            self._seen_urls.add(self._norm_url(url))
            return

        snippet = text[:4000]  # tronca per non saturare il contesto LLM

        task = (
            f"Analizza il seguente contenuto del blog 'Cronache dal Cosmo' "
            f"e chiama record_insights con i dati estratti.\n\n"
            f"URL: {url}\n\n"
            f"CONTENUTO:\n{snippet}\n\n"
            f"Istruzioni IMPORTANTI:\n"
            f"- Scrivi TUTTO in italiano: headline, azioni, sintesi.\n"
            f"- Identifica ingredienti menzionati che potrebbero subire variazioni di prezzo/disponibilità.\n"
            f"- Determina se la notizia indica scarsità (scarcity), surplus (surplus) o nulla di concreto (neutral).\n"
            f"- Suggerisci azioni specifiche IN ITALIANO: es. 'aumentare le offerte su [ingrediente]', "
            f"'vendere surplus di [ingrediente] sul mercato', 'aggiungere [piatto] al menu', "
            f"'evitare [ingrediente] in questa fase', 'monitorare il prezzo di [ingrediente]'.\n"
            f"- Assegna priorità: high se l'impatto è immediato, medium se nei prossimi turni, low se generico.\n"
            f"Chiama record_insights UNA SOLA VOLTA con tutti i dati IN ITALIANO."
        )

        try:
            await self.a_run(task, tool_choice="required_first")
        except Exception as exc:
            log_error("news", "—", "analyze", f"Analisi LLM fallita per {url}: {exc}")

        self._seen_urls.add(self._norm_url(url))

    # ------------------------------------------------------------------ public API

    async def run_once(self) -> list[dict[str, Any]]:
        """
        Singola passata: scarica e analizza tutti gli articoli nuovi.
        Ritorna la lista degli insight raccolti in questa passata.
        """
        log("news", "—", "poll", "Controllo blog per nuovi articoli...")
        new_urls = await self._get_new_article_urls()

        if not new_urls:
            log("news", "—", "poll", "Nessun articolo nuovo trovato.")
            return []

        log("news", "—", "poll", f"Trovati {len(new_urls)} articoli nuovi")

        before = len(self._ins)
        for url in new_urls[:5]:  # max 5 articoli per passata
            await self._analyze_url(url)

        return self._ins[before:]

    async def run_loop(self) -> None:
        """Loop infinito: analizza subito poi ogni POLL_INTERVAL_SECONDS."""
        log(
            "news", "—", "agent",
            f"NewsWatcherAgent avviato — polling ogni {POLL_INTERVAL_SECONDS}s su {BLOG_URL}",
        )
        while True:
            try:
                await self.run_once()
            except Exception as exc:
                log_error("news", "—", "loop", f"Errore nel ciclo di polling: {exc}")
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    def start(self, memory: Any | None = None) -> asyncio.Task:
        """Avvia il loop come task asyncio in background. Idempotente.

        Args:
            memory: StrategyMemory condivisa — se fornita, ogni insight viene
                    propagato in memory.news_insights (disponibile a tutti gli agenti).
        """
        if memory is not None:
            self._strategy_memory = memory
        if self._polling_task and not self._polling_task.done():
            return self._polling_task
        self._polling_task = asyncio.create_task(self.run_loop())
        log("news", "—", "agent", "Task di polling avviato in background")
        return self._polling_task

    def stop(self) -> None:
        """Cancella il task di background."""
        if self._polling_task and not self._polling_task.done():
            self._polling_task.cancel()
            log("news", "—", "agent", "Task di polling fermato")

    @property
    def insights(self) -> list[dict[str, Any]]:
        """Tutti gli insight raccolti finora."""
        return list(self._ins)

    def format_advisory(self, max_items: int = 5) -> str:
        """
        Ritorna una stringa formattata con i top insight (per includerla
        nel prompt degli altri agenti).
        """
        if not self._ins:
            return ""
        priority_order = {"high": 0, "medium": 1, "low": 2}
        sorted_ins = sorted(
            self._ins,
            key=lambda x: (priority_order.get(x.get("priority", "low"), 2), -x.get("recorded_at", 0)),
        )[:max_items]
        lines = ["=== Notizie da 'Cronache dal Cosmo' ==="]
        for ins in sorted_ins:
            pri = ins.get("priority", "?").upper()
            headline = ins.get("headline", "")
            ingredients = ins.get("ingredients_affected", [])
            actions = ins.get("actions", [])
            line = f"[{pri}] {headline}"
            if ingredients:
                line += f"\n     Ingredienti: {', '.join(ingredients)}"
            if actions:
                line += f"\n     Azioni consigliate: {'; '.join(actions)}"
            lines.append(line)
        lines.append("=== Fine notizie ===")
        return "\n".join(lines)


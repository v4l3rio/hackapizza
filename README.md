# Hackapizza — Agente AI per il Multiverso Gastronomico

Agente autonomo per la competizione Hackapizza. Il sistema partecipa a un gioco a turni multi-ristorante: acquista ingredienti all'asta, costruisce un menu, serve clienti in tempo reale e commercia ingredienti sul mercato aperto. Ogni decisione viene delegata a un LLM tramite un'architettura ad agenti specializzati.

---

## Struttura del progetto

```
hackapizza/
├── main.py                   # Entrypoint: bootstrap e avvio del loop SSE
├── config.py                 # Costanti e variabili d'ambiente
├── pyproject.toml            # Dipendenze Poetry
│
├── state/
│   ├── game_state.py         # GameState: stato condiviso del gioco
│   └── memory.py             # StrategyMemory: storico prezzi e ordini
│
├── infrastructure/
│   ├── sse_listener.py       # SSEListener: ricezione eventi dal server
│   ├── http_client.py        # HttpClient: lettura stato via GET
│   ├── mcp_client.py         # MCPClient: azioni di gioco via POST
│   └── llm_factory.py        # Singleton per il client LLM (Regolo)
│
├── agents/
│   ├── manager.py            # AgentManager: router SSE -> agenti
│   ├── bidding.py            # BiddingAgent: fase closed_bid
│   ├── menu.py               # MenuAgent: fase waiting
│   ├── serving.py            # ServingAgent: fase serving
│   ├── market.py             # MarketAgent: mercato aperto
│   └── speaking.py           # SpeakingAgent: fase speaking (negoziazione)
│
└── utils/
    └── logger.py             # Logging strutturato con timestamp e fase
```

---

## Configurazione

Le credenziali vengono lette da un file `.env` nella root del progetto.

```
TEAM_ID=<numero team>
TEAM_API_KEY=<chiave API del team>
REGOLO_API_KEY=<chiave API Regolo per il LLM>
BASE_URL=https://hackapizza.datapizza.tech
```

**Costanti di strategia in `config.py`:**

| Costante | Valore | Descrizione |
|---|---|---|
| `DEFAULT_BID_FLAT` | 50 | Offerta piatta per ingrediente al turno 1 |
| `BID_CLEARING_MULTIPLIER` | 1.1 | Moltiplicatore sul prezzo di clearing dai turni successivi |
| `MAX_BID_BALANCE_FRACTION` | 0.6 | Limite massimo delle offerte: 60% del saldo corrente |
| `MENU_MARKUP` | 2.5 | Prezzo piatto = costo ingredienti x 2.5 |
| `REGOLO_MODEL` | `meta-llama/Llama-3.3-70B-Instruct` | Modello LLM usato |

Gli URL SSE e MCP vengono derivati automaticamente da `BASE_URL`:
- SSE: `{BASE_URL}/events/{TEAM_ID}`
- MCP: `{BASE_URL}/mcp`

---

## Avvio

```bash
# Installare le dipendenze (una sola volta)
python3 -m poetry install --no-root

# Avviare l'agente
python3 -m poetry run python main.py
```

Il processo gira indefinitamente ascoltando eventi SSE. Si interrompe con `Ctrl+C`.

---

## Architettura generale

Il sistema segue un modello event-driven: il server di gioco emette eventi SSE, ogni evento viene instradato all'agente corretto, l'agente ragiona tramite LLM ed esegue azioni tramite chiamate MCP.

```
Server SSE
    |
    v
SSEListener          (riceve e deserializza ogni riga "data: ...")
    |
    v
AgentManager         (router: aggiorna stato, sceglie l'agente)
    |
    +-- speaking    -> SpeakingAgent
    +-- closed_bid  -> BiddingAgent
    +-- waiting     -> MenuAgent + MarketAgent (sell)
    +-- serving     -> ServingAgent + MarketAgent (buy)
    +-- stopped     -> chiude il ristorante, consolida memoria
    |
    v
Agent.a_run(task)    (LLM con tool use)
    |
    v
MCPClient            (POST /mcp/tools/<tool_name>)
```

---

## Flusso di un turno completo

### 1. `speaking`
L'`AgentManager` invoca `SpeakingAgent.execute()`. L'agente riceve l'inventario corrente, la shortfall di ingredienti, i ristoranti avversari e i prezzi di clearing noti. Il LLM decide se inviare messaggi di negoziazione ad altri team tramite `send_message`. Se non c'e` beneficio strategico evidente, non fa nulla.

### 2. `closed_bid`
Prima dell'esecuzione, `StrategyMemory.consolidate()` carica lo storico delle aste e aggiorna `clearing_prices`. Poi `BiddingAgent.execute()` calcola la shortfall per ogni ingrediente (massimo deficit tra tutte le ricette) e costruisce un task testuale per il LLM contenente:
- saldo e budget disponibile (60% del saldo)
- inventario corrente
- tutte le ricette
- shortfall pre-calcolata
- prezzi di clearing noti
- regola di pricing: `clearing_price * 1.1` o `50` flat

Il LLM elabora e chiama `submit_bids` una sola volta con il JSON completo delle offerte.

### 3. `waiting`
Due agenti vengono invocati in sequenza:

**MenuAgent**: calcola i piatti cucinabili (per cui si hanno tutti gli ingredienti), stima i costi e chiede al LLM di costruire un menu con prezzi a markup 2.5x e descrizioni accattivanti. Il LLM chiama `set_menu` una sola volta.

**MarketAgent** (surplus sell): calcola quali ingredienti sono in eccesso rispetto a quanto serve per tutte le ricette e chiede al LLM di pubblicare annunci di vendita sul mercato aperto a `clearing_price * 1.05`.

### 4. `serving`
All'inizio della fase, `ServingAgent.execute()` invoca il LLM per aprire il ristorante (`open_restaurant`).

Poi due flussi paralleli gestiscono gli eventi in tempo reale:

**client_spawned** (handler SSE permanente): quando arriva un cliente, il LLM riceve l'ordine in linguaggio naturale, le intolleranze alimentari, il menu corrente e le ricette. Sceglie il piatto piu` adatto ed esegue `prepare_dish`. Il nome del piatto viene registrato in `_pending_orders[client_id]`.

**preparation_complete** (handler SSE permanente): quando la cucina finisce, il sistema cerca in `_pending_orders` quale cliente stava aspettando quel piatto e chiama direttamente `serve_dish` senza LLM, perche` e` un'operazione deterministica.

**MarketAgent** (buy): parallelamente all'apertura, scansiona il mercato aperto e acquista gli ingredienti mancanti se il prezzo e` pari o inferiore al prezzo di clearing.

### 5. `stopped`
Il ristorante viene chiuso (`update_restaurant_is_open(False)`) e la memoria viene consolidata per il turno successivo.

---

## Componenti di infrastruttura

### `SSEListener`
Apre una connessione HTTP persistente con `aiohttp` e legge il body riga per riga. Ogni riga `data: <json>` viene deserializzata e dispatchata ai handler registrati con `sse.on(event_type, handler)`. La riga `data: connected` e` un handshake e viene ignorata. Gli errori nei singoli handler vengono loggati ma non interrompono il loop principale.

### `HttpClient`
Wrapper asincrono per le chiamate GET di lettura stato. Il metodo `get_all()` esegue in parallelo con `asyncio.gather` le chiamate a:
- `/restaurants/{team_id}` — saldo e inventario
- `/recipes` — ricette disponibili
- `/meals` — ordini attivi
- `/restaurants` — lista ristoranti avversari

### `MCPClient`
Wrapper asincrono per le azioni di gioco via POST. Ogni metodo corrisponde a un tool esposto dal server:

| Metodo | Tool | Descrizione |
|---|---|---|
| `closed_bid(bids)` | `closed_bid` | Sottomette offerte all'asta |
| `save_menu(items)` | `save_menu` | Pubblica il menu |
| `create_market_entry(...)` | `create_market_entry` | Mette in vendita un ingrediente |
| `execute_transaction(entry_id)` | `execute_transaction` | Acquista un'offerta di mercato |
| `delete_market_entry(entry_id)` | `delete_market_entry` | Rimuove un proprio annuncio |
| `prepare_dish(name)` | `prepare_dish` | Avvia la preparazione in cucina |
| `serve_dish(name, client_id)` | `serve_dish` | Serve il piatto al cliente |
| `update_restaurant_is_open(bool)` | `update_restaurant_is_open` | Apre/chiude il ristorante |
| `send_message(recipient_id, text)` | `send_message` | Invia messaggio a un team |

### `LLM Factory`
`get_llm_client()` restituisce un singleton `OpenAILikeClient` configurato per l'API Regolo (compatibile OpenAI). Viene inizializzato al primo utilizzo. Tutti gli agenti condividono la stessa istanza.

---

## Stato condiviso

### `GameState`
Dataclass che contiene tutto lo stato osservabile del gioco:

- `turn_id` — turno corrente
- `phase` — fase corrente
- `balance` — saldo disponibile
- `inventory` — dizionario `{ingrediente: quantita}`
- `recipes` — lista ricette con ingredienti richiesti
- `menu_items` — menu attualmente pubblicato
- `active_meals` — ordini in corso
- `restaurants` — lista ristoranti nel gioco

Il metodo `refresh_all(http)` aggiorna tutto in parallelo all'inizio di ogni fase. `cookable_dishes()` restituisce le ricette per cui si hanno tutti gli ingredienti. `ingredient_cost(recipe)` stima il costo come numero di ingredienti distinti (placeholder, non usa prezzi reali).

### `StrategyMemory`
Persiste informazioni tra fasi dello stesso turno e tra turni diversi:

- `clearing_prices` — dizionario `{ingrediente: prezzo}` aggiornato da `consolidate()`
- `bid_history` — storico grezzo delle aste
- `served_orders` — ordini serviti (per uso futuro)
- `revenue_per_turn` — ricavi per turno (per uso futuro)

`bid_for(ingredient, flat)` restituisce il prezzo di offerta: `clearing_price * 1.1` se disponibile, altrimenti il valore flat passato.

---

## Struttura degli agenti

Tutti gli agenti ereditano da `Agent` (libreria datapizza-ai) e definiscono:

- `name` — identificatore testuale dell'agente
- `system_prompt` — istruzioni permanenti al LLM
- `__init__` — inizializza i riferimenti a `None` e chiama `super().__init__(client=get_llm_client(), max_steps=N)`
- metodi `@tool` — funzioni async che il LLM puo` invocare, con nome e descrizione espliciti nel decorator
- `execute(state, memory, mcp)` — entry point chiamato dall'`AgentManager`: salva i riferimenti e chiama `await self.a_run(task, tool_choice="required_first")`

Il parametro `tool_choice="required_first"` forza il LLM a chiamare almeno un tool al primo step, evitando risposte puramente testuali quando si vuole un'azione garantita.

**Limiti `max_steps` per agente:**

| Agente | max_steps | Motivo |
|---|---|---|
| BiddingAgent | 3 | Una sola chiamata a `submit_bids` e` sufficiente |
| MenuAgent | 3 | Una sola chiamata a `set_menu` e` sufficiente |
| ServingAgent | 3 | Una sola chiamata a `open_restaurant` o `prepare_dish` |
| MarketAgent | 8 | Puo` fare piu` acquisti/vendite in sequenza |
| SpeakingAgent | 5 | Puo` contattare piu` team |

---

## Logging

Ogni riga di log ha il formato:

```
[HH:MM:SS][PHASE][T<turn_id>][tag] messaggio
```

Gli errori usano `log_error` che prefissa `ERROR:` e scrive su `logging.error`. Esempi:

```
[14:23:01][CLOSED_BID][T3][agent] BiddingAgent started
[14:23:02][CLOSED_BID][T3][tool] Submitted 4 bids: {...}
[14:23:05][SERVING][T3][client] Client abc123 wants: 'something spicy'
[14:23:06][SERVING][T3][serve] Served 'Pizza Diavola' to client abc123
[14:23:07][SERVING][T3][serve] ERROR: serve_dish failed for client xyz: ...
```

---

## Dipendenze

| Pacchetto | Versione | Scopo |
|---|---|---|
| `aiohttp` | ^3.9 | Chiamate HTTP async (SSE, GET, POST) |
| `datapizza-ai` | ^0.0.9 | Classe base `Agent`, decorator `@tool`, `a_run()` |
| `datapizza-ai-clients-openai-like` | * | `OpenAILikeClient` per API Regolo |
| `python-dotenv` | ^1.0 | Caricamento `.env` |
| `pydantic` | ^2.0 | Validazione dati (usata internamente dalla libreria) |

Richiede Python `>=3.11,<3.14`.

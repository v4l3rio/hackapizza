import os
from dotenv import load_dotenv

load_dotenv()

TEAM_ID: int = int(os.getenv("TEAM_ID", "0"))
TEAM_API_KEY: str = os.getenv("TEAM_API_KEY", "")
REGOLO_API_KEY: str = os.getenv("REGOLO_API_KEY", "")

# Datapizza monitoring
DATAPIZZA_MONITORING_API_KEY: str = os.getenv("DATAPIZZA_MONITORING_API_KEY", "")
DATAPIZZA_MONITORING_PROJECT_ID: str = os.getenv("DATAPIZZA_MONITORING_PROJECT_ID", "")
DATAPIZZA_MONITORING_OTLP_ENDPOINT: str = os.getenv(
    "DATAPIZZA_MONITORING_OTLP_ENDPOINT",
    "https://datapizza-monitoring.datapizza.tech/gateway/v1/traces",
)

BASE_URL: str = os.getenv("BASE_URL", "https://hackapizza.datapizza.tech")
SSE_URL: str = f"{BASE_URL}/events/{TEAM_ID}"
MCP_URL: str = f"{BASE_URL}/mcp"

REGOLO_BASE_URL: str = "https://api.regolo.ai/v1"
REGOLO_MODEL: str = "gpt-oss-120b"

# Bidding strategy
DEFAULT_BID_FLAT: int = 50          # flat bid per ingredient on turn 1
BID_CLEARING_MULTIPLIER: float = 1.15  # bid = clearing_price * multiplier (was 1.1)
MAX_BID_BALANCE_FRACTION: float = 0.6  # cap: max 60% of balance in bids
BID_SERVINGS_MULTIPLIER: int = 2   # bid for N servings of each focus recipe per turn

# Menu pricing — tiered by dish prestige score (see utils/ingredient_data.py)
MENU_MARKUP: float = 2.5            # default fallback markup
MENU_MARKUP_BUDGET: float = 2.0     # low-prestige dishes  → Galactic Explorer
MENU_MARKUP_STANDARD: float = 2.5   # mid-prestige dishes  → Orbital Family / Astrobaron
MENU_MARKUP_PRESTIGE: float = 4.5   # high-prestige dishes → Space Sage (unlimited budget)

# Prestige score thresholds (score = weighted_avg_prestige + rarity_bonus, see ingredient_data.py)
MENU_PRESTIGE_SCORE_HIGH: float = 75.0   # score >= this → PRESTIGE tier
MENU_PRESTIGE_SCORE_LOW: float = 62.0    # score <  this → BUDGET  tier

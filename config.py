import os
from dotenv import load_dotenv
import yaml

from utils.history_util import DashboardClient

load_dotenv()


def load_yaml(filename: str):
    with open(f"{filename}.yml", "r") as f:
        return yaml.safe_load(f)


from pathlib import Path

ROOT = Path(__file__).parent  # always the project root, regardless of where you run from

config = load_yaml(ROOT / 'config')

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

WEB_APP_URL: str = os.getenv("WEB_APP_URL")
BASE_URL: str = os.getenv("BASE_URL", "https://hackapizza.datapizza.tech")
SSE_URL: str = f"{BASE_URL}/events/{TEAM_ID}"
MCP_URL: str = f"{BASE_URL}/mcp"

REGOLO_BASE_URL: str = config["REGOLO_BASE_URL"]
REGOLO_MODEL: str = config["REGOLO_MODEL_BIG"]

# Bidding strategy
DEFAULT_BID_FLAT: int = config['DEFAULT_BID_FLAT']  # flat bid per ingredient on turn 1
DEFAULT_BID_QUANTITY: int = config['DEFAULT_BID_QUANTITY']
DEFAULT_PRICE_SELL: int = config['DEFAULT_PRICE_SELL']
DEFAULT_PRICE_SELL_MARKET: int = config['DEFAULT_PRICE_SELL_MARKET']
BID_CLEARING_MULTIPLIER: float = config['BID_CLEARING_MULTIPLIER']  # bid = clearing_price * multiplier
MAX_BID_BALANCE_FRACTION: float = config['MAX_BID_BALANCE_FRACTION']  # cap: max 60% of balance in bids
BID_SERVINGS_MULTIPLIER: int = config['BID_SERVINGS_MULTIPLIER']  # bid for N servings of each focus recipe per turn
MIN_DISH_TO_FULFILL_OR_CLOSE_FRACTION: float = config['MIN_DISH_TO_FULFILL_OR_CLOSE_FRACTION']

MAX_RECIPES: int = config['MAX_RECIPES']

# Market strategy
MARKET_MAX_BUY_MULTIPLIER: float = config['MARKET_MAX_BUY_MULTIPLIER']
MARKET_MAX_BUY_FLAT: int = config['MARKET_MAX_BUY_FLAT']

# Menu pricing
MENU_MARKUP: float = config['MENU_MARKUP']  # dish price = ingredient cost * markup
# Menu pricing — tiered by dish prestige score (see utils/ingredient_data.py)
# MENU_MARKUP: float = 2.5            # default fallback markup
MENU_MARKUP_BUDGET: float = 2.0  # low-prestige dishes  → Galactic Explorer
MENU_MARKUP_STANDARD: float = 2.5  # mid-prestige dishes  → Orbital Family / Astrobaron
MENU_MARKUP_PRESTIGE: float = 4.5  # high-prestige dishes → Space Sage (unlimited budget)

# Prestige score thresholds (score = weighted_avg_prestige + rarity_bonus, see ingredient_data.py)
MENU_PRESTIGE_SCORE_HIGH: float = 75.0  # score >= this → PRESTIGE tier
MENU_PRESTIGE_SCORE_LOW: float = 62.0  # score <  this → BUDGET  tier

DASHBOARD = DashboardClient(
    base_url=WEB_APP_URL,
    api_key=TEAM_API_KEY,
    my_restaurant_id=str(TEAM_ID),
    dumps_dir="./dumps",
)

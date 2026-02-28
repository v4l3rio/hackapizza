import os
from dotenv import load_dotenv
import yaml

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

BASE_URL: str = config["BASE_URL"]
SSE_URL: str = f"{BASE_URL}/events/{TEAM_ID}"
MCP_URL: str = f"{BASE_URL}/mcp"

REGOLO_BASE_URL: str = config["REGOLO_BASE_URL"]
REGOLO_MODEL: str = config["REGOLO_MODEL_BIG"]

# Bidding strategy
DEFAULT_BID_FLAT: int = config['DEFAULT_BID_FLAT']          # flat bid per ingredient on turn 1
BID_CLEARING_MULTIPLIER: float = config['BID_CLEARING_MULTIPLIER']  # bid = clearing_price * multiplier
MAX_BID_BALANCE_FRACTION: float = config['MAX_BID_BALANCE_FRACTION']  # cap: max 60% of balance in bids
BID_SERVINGS_MULTIPLIER: int = config['BID_SERVINGS_MULTIPLIER']   # bid for N servings of each focus recipe per turn

# Menu pricing
MENU_MARKUP: float = config['MENU_MARKUP']            # dish price = ingredient cost * markup

import os
from dotenv import load_dotenv

load_dotenv()

TEAM_ID: int = int(os.getenv("TEAM_ID", "0"))
TEAM_API_KEY: str = os.getenv("TEAM_API_KEY", "")
REGOLO_API_KEY: str = os.getenv("REGOLO_API_KEY", "")

BASE_URL: str = os.getenv("BASE_URL", "https://hackapizza.datapizza.tech")
SSE_URL: str = f"{BASE_URL}/events/{TEAM_ID}"
MCP_URL: str = f"{BASE_URL}/mcp"

REGOLO_BASE_URL: str = "https://api.regolo.ai/v1"
REGOLO_MODEL: str = "gpt-oss-120b"

# Bidding strategy
DEFAULT_BID_FLAT: int = 50          # flat bid per ingredient on turn 1
BID_CLEARING_MULTIPLIER: float = 1.1  # bid = clearing_price * multiplier
MAX_BID_BALANCE_FRACTION: float = 0.6  # cap: max 60% of balance in bids

# Menu pricing
MENU_MARKUP: float = 2.5            # dish price = ingredient cost * markup

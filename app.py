import os
import json
import time
import base64
import threading
import random
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import requests
import pandas as pd
import streamlit as st
import urllib3

from dotenv import load_dotenv
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

try:
from streamlit_autorefresh import st_autorefresh
HAS_AUTOREFRESH = True
except ImportError:
HAS_AUTOREFRESH = False

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
load_dotenv()

================================================================
CONFIG
================================================================

HELIUS_KEY = os.getenv("HELIUS_KEY", "").strip()
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY", "").strip()
ENV_PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY", "").strip()

HELIUS_RPC_URL = (
f"https://mainnet.helius-rpc.com/?api-key={HELIUS_KEY}"
if HELIUS_KEY else ""
)

DEXSCREENER_API = "https://api.dexscreener.com"
BIRDEYE_API = "https://public-api.birdeye.so"
GECKO_API = "https://api.geckoterminal.com/api/v2"
RUGCHECK_API = "https://api.rugcheck.xyz/v1"

Jupiter's legacy v6 endpoint is retained only as a fallback/compatibility
layer. Verify the currently supported Jupiter API before enabling live mode.

JUPITER_API = "https://quote-api.jup.ag/v6"

WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

MAX_TRADE_SOL_CAP = 0.5
STATE_FILE = "cyber_sniper_state.json"

SCAN_INTERVAL_SECONDS = 20
DISCOVERY_LIMIT = 50
MAX_CANDIDATES_TO_SCORE = 20

REQUEST_TIMEOUT = 12

================================================================
HTTP SESSION
================================================================

SESSION = requests.Session()
SESSION.headers.update({
"User-Agent": "CYBER-SNIPER/7.0",
"Accept": "application/json",
})

================================================================
HELPERS
================================================================

def utc_now():
return datetime.now(timezone.utc)

def safe_float(value, default=0.0):
try:
if value is None:
return default
return float(value)
except Exception:
return default

def safe_int(value, default=0):
try:
if value is None:
return default
return int(value)
except Exception:
return default

def age_minutes(timestamp):
if not timestamp:
return None

try:
    if isinstance(timestamp, (int, float)):
        ts = float(timestamp)
        if ts > 10_000_000_000:
            ts /= 1000
        created = datetime.fromtimestamp(ts, tz=timezone.utc)
    else:
        text = str(timestamp).replace("Z", "+00:00")
        created = datetime.fromisoformat(text)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)

    return max(
        0,
        (utc_now() - created.astimezone(timezone.utc)).total_seconds() / 60
    )
except Exception:
    return None

def fmt_age(minutes):
if minutes is None:
return "unknown"

if minutes < 60:
    return f"{minutes:.0f}m"

hours = minutes / 60

if hours < 24:
    return f"{hours:.1f}h"

return f"{hours / 24:.1f}d"

def api_get(url, params=None, headers=None, timeout=REQUEST_TIMEOUT):
try:
r = SESSION.get(
url,
params=params,
headers=headers,
timeout=timeout,
)

    if r.status_code != 200:
        return None, f"HTTP {r.status_code}: {r.text[:180]}"

    try:
        return r.json(), None
    except Exception:
        return None, "invalid JSON response"

except requests.Timeout:
    return None, "request timeout"
except requests.RequestException as e:
    return None, f"network error: {e}"
except Exception as e:
    return None, f"unexpected error: {e}"
================================================================
ENGINE STATE
================================================================

@dataclass
class EngineState:
lock: threading.Lock = field(default_factory=threading.Lock)

running: bool = False
paper_mode: bool = True

wallet: Optional[Keypair] = None
wallet_address: str = ""

positions: List[Dict] = field(default_factory=list)
trades: List[Dict] = field(default_factory=list)

logs: List[str] = field(default_factory=list)

discovered: List[Dict] = field(default_factory=list)
rejected: List[Dict] = field(default_factory=list)

source_status: Dict = field(default_factory=dict)

scan_stats: Dict = field(default_factory=lambda: {
    "last_scan": "",
    "raw_candidates": 0,
    "unique_candidates": 0,
    "market_pass": 0,
    "risk_pass": 0,
    "quote_pass": 0,
    "final_candidates": 0,
})

config: Dict = field(default_factory=lambda: {
    "snipe_amount": 0.05,

    "take_profit_pct": 50,
    "trailing_stop_pct": 15,

    "min_liquidity_usd": 10000,
    "max_liquidity_usd": 10000000,

    "min_volume_5m_usd": 250,
    "min_volume_1h_usd": 1000,

    "max_age_minutes": 720,

    "max_top10_pct": 40,

    "max_positions": 5,

    "daily_loss_limit": 0.2,

    "min_score": 55,

    "require_sell_quote": True,

    "enable_birdeye": True,
    "enable_gecko": True,
    "enable_dexscreener": True,

    "birdeye_meme_mode": True,
})

def log(self, msg: str, tag: str = "sys"):
    ts = datetime.now().strftime("%H:%M:%S")

    safe = str(msg).replace("<", "&lt;").replace(">", "&gt;")

    with self.lock:
        self.logs.insert(
            0,
            f'<div class="line {tag}">[{ts}] {safe}</div>'
        )
        self.logs = self.logs[:120]

    print(f"[{ts}] {msg}")

@st.cache_resource
def get_state():
s = EngineState()
load_persisted(s)
return s

def load_persisted(s):
if not os.path.exists(STATE_FILE):
return

try:
    with open(STATE_FILE, "r") as f:
        data = json.load(f)

    s.positions = data.get("positions", [])
    s.trades = data.get("trades", [])

except Exception as e:
    print(f"state load error: {e}")

def save_persisted(s):
try:
with s.lock:
data = {
"positions": s.positions,
"trades": s.trades,
}

    tmp = STATE_FILE + ".tmp"

    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)

    os.replace(tmp, STATE_FILE)

except Exception as e:
    print(f"state save error: {e}")
================================================================
RPC
================================================================

def rpc_call(method, params=None):
if not HELIUS_RPC_URL:
return None, "HELIUS_KEY missing"

payload = {
    "jsonrpc": "2.0",
    "id": int(time.time() * 1000),
    "method": method,
    "params": params or [],
}

try:
    r = SESSION.post(
        HELIUS_RPC_URL,
        json=payload,
        timeout=15,
    )

    if r.status_code != 200:
        return None, f"HTTP {r.status_code}: {r.text[:180]}"

    data = r.json()

    if "error" in data:
        return None, str(data["error"])

    return data.get("result"), None

except Exception as e:
    return None, str(e)

def get_wallet_balance(pubkey):
if not pubkey:
return 0.0

result, _ = rpc_call(
    "getBalance",
    [pubkey]
)

if not result:
    return 0.0

return safe_float(result.get("value")) / 1_000_000_000

def get_token_balance(pubkey, mint):
result, _ = rpc_call(
"getTokenAccountsByOwner",
[
pubkey,
{"mint": mint},
{"encoding": "jsonParsed"},
],
)

try:
    accounts = result.get("value", [])

    if not accounts:
        return 0, 0

    info = (
        accounts[0]
        ["account"]
        ["data"]
        ["parsed"]
        ["info"]
        ["tokenAmount"]
    )

    return (
        safe_int(info.get("amount")),
        safe_int(info.get("decimals")),
    )

except Exception:
    return 0, 0

def send_raw_transaction_rpc(signed_tx_bytes):
result, error = rpc_call(
"sendTransaction",
[
base64.b64encode(signed_tx_bytes).decode(),
{
"skipPreflight": False,
"encoding": "base64",
"preflightCommitment": "processed",
"maxRetries": 3,
},
],
)

if error:
    print(f"sendTransaction error: {error}")
    return None

return result

def confirm_transaction_rpc(signature, timeout=30):
start = time.time()

while time.time() - start < timeout:
    result, _ = rpc_call(
        "getSignatureStatuses",
        [
            [signature],
            {"searchTransactionHistory": True},
        ],
    )

    try:
        status = result["value"][0]

        if status:
            if status.get("err") is not None:
                return False

            if status.get("confirmationStatus") in (
                "confirmed",
                "finalized",
            ):
                return True

    except Exception:
        pass

    time.sleep(2)

return False
================================================================
DISCOVERY — DEXSCREENER
================================================================

def discover_dexscreener():
results = []

# Profiles are closer to "newly surfaced tokens" than Boosts.
endpoints = [
    "/token-profiles/latest/v1",
    "/community-takeovers/latest/v1",
    "/token-boosts/latest/v1",
]

seen = set()

for endpoint in endpoints:

    data, error = api_get(
        f"{DEXSCREENER_API}{endpoint}"
    )

    if error:
        state.log(
            f"DEXSCREENER {endpoint}: {error}",
            "error"
        )
        continue

    if not isinstance(data, list):
        continue

    for item in data:

        if item.get("chainId") != "solana":
            continue

        mint = item.get("tokenAddress")

        if not mint or mint in seen:
            continue

        seen.add(mint)

        results.append({
            "mint": mint,
            "source": "DEXScreener",
            "symbol": "UNKNOWN",
            "name": "Unknown",
            "liquidity": 0,
            "volume_5m": 0,
            "volume_1h": 0,
            "volume_24h": 0,
            "market_cap": 0,
            "fdv": 0,
            "pair_created_at": None,
            "pair_address": "",
            "dex": "",
            "boosted": endpoint.startswith("/token-boosts"),
        })

# Enrich in batches. DEX Screener supports multiple token addresses
# in one request.
mints = [x["mint"] for x in results]

for start in range(0, len(mints), 30):

    batch = mints[start:start + 30]

    data, error = api_get(
        f"{DEXSCREENER_API}/tokens/v1/solana/{','.join(batch)}"
    )

    if error:
        state.log(
            f"DEXSCREENER enrichment: {error}",
            "error"
        )
        continue

    if not isinstance(data, list):
        continue

    for pair in data:

        mint = pair.get("baseToken", {}).get("address")

        if not mint:
            continue

        match = next(
            (x for x in results if x["mint"] == mint),
            None
        )

        if not match:
            continue

        liq = safe_float(
            pair.get("liquidity", {}).get("usd")
        )

        volume = pair.get("volume", {})

        match["liquidity"] = max(
            match["liquidity"],
            liq
        )

        match["volume_5m"] = max(
            match["volume_5m"],
            safe_float(volume.get("m5"))
        )

        match["volume_1h"] = max(
            match["volume_1h"],
            safe_float(volume.get("h1"))
        )

        match["volume_24h"] = max(
            match["volume_24h"],
            safe_float(volume.get("h24"))
        )

        match["symbol"] = (
            pair.get("baseToken", {}).get("symbol")
            or match["symbol"]
        )

        match["name"] = (
            pair.get("baseToken", {}).get("name")
            or match["name"]
        )

        match["market_cap"] = max(
            match["market_cap"],
            safe_float(pair.get("marketCap"))
        )

        match["fdv"] = max(
            match["fdv"],
            safe_float(pair.get("fdv"))
        )

        if not match["pair_created_at"]:
            match["pair_created_at"] = pair.get(
                "pairCreatedAt"
            )

        if not match["pair_address"]:
            match["pair_address"] = (
                pair.get("pairAddress") or ""
            )

        if not match["dex"]:
            match["dex"] = (
                pair.get("dexId") or ""
            )

state.source_status["DEXScreener"] = {
    "ok": True,
    "count": len(results),
    "time": datetime.now().strftime("%H:%M:%S"),
}

return results
================================================================
DISCOVERY — BIRDEYE
================================================================

def discover_birdeye():
if not BIRDEYE_API_KEY:
state.source_status["Birdeye"] = {
"ok": False,
"count": 0,
"time": datetime.now().strftime("%H:%M:%S"),
"error": "BIRDEYE_API_KEY not configured",
}
return []

headers = {
    "X-API-KEY": BIRDEYE_API_KEY,
    "x-chain": "solana",
}

params = {
    "limit": 20,
}

if state.config.get("birdeye_meme_mode"):
    params["meme_platform_enabled"] = "true"

data, error = api_get(
    f"{BIRDEYE_API}/defi/v2/tokens/new_listing",
    params=params,
    headers=headers,
)

if error:
    state.source_status["Birdeye"] = {
        "ok": False,
        "count": 0,
        "time": datetime.now().strftime("%H:%M:%S"),
        "error": error,
    }

    state.log(
        f"BIRDEYE: {error}",
        "error"
    )

    return []

# Birdeye responses can differ slightly between account/API versions.
raw = []

if isinstance(data, dict):
    raw = (
        data.get("data", {}).get("items")
        or data.get("data")
        or data.get("items")
        or []
    )

if not isinstance(raw, list):
    raw = []

results = []

for item in raw:

    mint = (
        item.get("address")
        or item.get("tokenAddress")
        or item.get("token_address")
    )

    if not mint:
        continue

    results.append({
        "mint": mint,
        "source": "Birdeye",
        "symbol": (
            item.get("symbol")
            or item.get("tokenSymbol")
            or "UNKNOWN"
        ),
        "name": (
            item.get("name")
            or item.get("tokenName")
            or "Unknown"
        ),
        "liquidity": safe_float(
            item.get("liquidity")
        ),
        "volume_5m": safe_float(
            item.get("volume_5m_usd")
            or item.get("v5mUSD")
        ),
        "volume_1h": safe_float(
            item.get("volume_1h_usd")
            or item.get("v1hUSD")
        ),
        "volume_24h": safe_float(
            item.get("volume_24h_usd")
            or item.get("v24hUSD")
        ),
        "market_cap": safe_float(
            item.get("market_cap")
            or item.get("mc")
        ),
        "fdv": safe_float(item.get("fdv")),
        "pair_created_at": (
            item.get("listed_at")
            or item.get("created_at")
            or item.get("listing_time")
        ),
        "pair_address": "",
        "dex": "",
        "boosted": False,
    })

state.source_status["Birdeye"] = {
    "ok": True,
    "count": len(results),
    "time": datetime.now().strftime("%H:%M:%S"),
}

return results
================================================================
DISCOVERY — GECKOTERMINAL
================================================================

def discover_gecko():
data, error = api_get(
f"{GECKO_API}/networks/solana/new_pools",
params={
"include": "base_token,quote_token,dex",
"page": 1,
},
)

if error:
    state.source_status["GeckoTerminal"] = {
        "ok": False,
        "count": 0,
        "time": datetime.now().strftime("%H:%M:%S"),
        "error": error,
    }

    state.log(
        f"GECKOTERMINAL: {error}",
        "error"
    )

    return []

pools = data.get("data", []) if isinstance(data, dict) else []
included = data.get("included", []) if isinstance(data, dict) else []

included_map = {
    item.get("id"): item
    for item in included
    if item.get("id")
}

results = []

for pool in pools:

    attrs = pool.get("attributes", {})
    relationships = pool.get("relationships", {})

    base_ref = (
        relationships
        .get("base_token", {})
        .get("data", {})
        .get("id")
    )

    base = included_map.get(base_ref, {})
    base_attrs = base.get("attributes", {})

    mint = (
        base_attrs.get("address")
        or str(base_ref or "").replace("solana_", "")
    )

    if not mint:
        continue

    volume = attrs.get("volume_usd", {})
    txns = attrs.get("transactions", {})

    results.append({
        "mint": mint,
        "source": "GeckoTerminal",
        "symbol": base_attrs.get("symbol") or "UNKNOWN",
        "name": base_attrs.get("name") or "Unknown",

        "liquidity": safe_float(
            attrs.get("reserve_in_usd")
        ),

        "volume_5m": safe_float(
            volume.get("m5")
        ),

        "volume_1h": safe_float(
            volume.get("h1")
        ),

        "volume_24h": safe_float(
            volume.get("h24")
        ),

        "market_cap": safe_float(
            attrs.get("market_cap_usd")
        ),

        "fdv": safe_float(
            attrs.get("fdv_usd")
        ),

        "pair_created_at": attrs.get(
            "pool_created_at"
        ),

        "pair_address": attrs.get("address") or "",

        "dex": (
            str(
                pool
                .get("relationships", {})
                .get("dex", {})
                .get("data", {})
                .get("id", "")
            )
        ),

        "boosted": False,
    })

state.source_status["GeckoTerminal"] = {
    "ok": True,
    "count": len(results),
    "time": datetime.now().strftime("%H:%M:%S"),
}

return results
================================================================
MULTI-SOURCE DISCOVERY
================================================================

def discover_all():
all_candidates = []

if state.config.get("enable_birdeye"):
    all_candidates.extend(
        discover_birdeye()
    )

if state.config.get("enable_gecko"):
    all_candidates.extend(
        discover_gecko()
    )

if state.config.get("enable_dexscreener"):
    all_candidates.extend(
        discover_dexscreener()
    )

state.scan_stats["raw_candidates"] = len(all_candidates)

# ------------------------------------------------------------
# Deduplicate by mint
# ------------------------------------------------------------

merged = {}

for item in all_candidates:

    mint = item.get("mint")

    if not mint:
        continue

    if mint not in merged:

        merged[mint] = dict(item)

        merged[mint]["sources"] = [
            item.get("source")
        ]

        continue

    existing = merged[mint]

    if item.get("source") not in existing["sources"]:
        existing["sources"].append(
            item.get("source")
        )

    existing["liquidity"] = max(
        safe_float(existing.get("liquidity")),
        safe_float(item.get("liquidity"))
    )

    existing["volume_5m"] = max(
        safe_float(existing.get("volume_5m")),
        safe_float(item.get("volume_5m"))
    )

    existing["volume_1h"] = max(
        safe_float(existing.get("volume_1h")),
        safe_float(item.get("volume_1h"))
    )

    existing["volume_24h"] = max(
        safe_float(existing.get("volume_24h")),
        safe_float(item.get("volume_24h"))
    )

    existing["market_cap"] = max(
        safe_float(existing.get("market_cap")),
        safe_float(item.get("market_cap"))
    )

    existing["fdv"] = max(
        safe_float(existing.get("fdv")),
        safe_float(item.get("fdv"))
    )

    if (
        not existing.get("pair_created_at")
        and item.get("pair_created_at")
    ):
        existing["pair_created_at"] = (
            item["pair_created_at"]
        )

    if (
        not existing.get("pair_address")
        and item.get("pair_address")
    ):
        existing["pair_address"] = (
            item["pair_address"]
        )

    if (
        existing.get("symbol") in (None, "", "UNKNOWN")
        and item.get("symbol")
    ):
        existing["symbol"] = item["symbol"]

    if (
        existing.get("name") in (None, "", "Unknown")
        and item.get("name")
    ):
        existing["name"] = item["name"]

candidates = list(merged.values())

state.scan_stats["unique_candidates"] = len(candidates)

# ------------------------------------------------------------
# Rank by multi-source confirmation + liquidity + activity
# ------------------------------------------------------------

def rank_score(x):

    source_bonus = len(
        x.get("sources", [])
    ) * 100

    liquidity = safe_float(
        x.get("liquidity")
    )

    volume = safe_float(
        x.get("volume_1h")
    )

    age = age_minutes(
        x.get("pair_created_at")
    )

    freshness_bonus = 0

    if age is not None:

        if age <= 30:
            freshness_bonus = 100

        elif age <= 120:
            freshness_bonus = 60

        elif age <= 360:
            freshness_bonus = 30

    return (
        source_bonus
        + min(liquidity / 100, 500)
        + min(volume / 100, 500)
        + freshness_bonus
    )

candidates.sort(
    key=rank_score,
    reverse=True
)

return candidates[:DISCOVERY_LIMIT]
================================================================
MARKET FILTER
================================================================

def market_filter(token, cfg) -> Tuple[bool, str]:

liq = safe_float(
    token.get("liquidity")
)

if liq < cfg["min_liquidity_usd"]:
    return False, (
        f"liquidity ${liq:,.0f} "
        f"< ${cfg['min_liquidity_usd']:,.0f}"
    )

if liq > cfg["max_liquidity_usd"]:
    return False, (
        f"liquidity ${liq:,.0f} "
        f"> max ${cfg['max_liquidity_usd']:,.0f}"
    )

age = age_minutes(
    token.get("pair_created_at")
)

if age is not None and age > cfg["max_age_minutes"]:
    return False, (
        f"pool age {fmt_age(age)} "
        f"> {fmt_age(cfg['max_age_minutes'])}"
    )

volume_5m = safe_float(
    token.get("volume_5m")
)

if volume_5m < cfg["min_volume_5m_usd"]:
    return False, (
        f"5m volume ${volume_5m:,.0f} too low"
    )

volume_1h = safe_float(
    token.get("volume_1h")
)

if volume_1h < cfg["min_volume_1h_usd"]:
    return False, (
        f"1h volume ${volume_1h:,.0f} too low"
    )

return True, "market-pass"
================================================================
BIRDEYE SECURITY
================================================================

def get_birdeye_security(mint):

if not BIRDEYE_API_KEY:
    return None

data, error = api_get(
    f"{BIRDEYE_API}/defi/token_security",
    params={
        "address": mint,
    },
    headers={
        "X-API-KEY": BIRDEYE_API_KEY,
        "x-chain": "solana",
    },
)

if error:
    return None

if isinstance(data, dict):
    return data.get("data") or data

return None
================================================================
BIRDEYE HOLDER PROFILE
================================================================

def get_birdeye_holder_profile(mint):

if not BIRDEYE_API_KEY:
    return None

data, error = api_get(
    f"{BIRDEYE_API}/defi/v3/token/holder-profile",
    params={
        "address": mint,
    },
    headers={
        "X-API-KEY": BIRDEYE_API_KEY,
        "x-chain": "solana",
    },
)

if error:
    return None

if isinstance(data, dict):
    return data.get("data") or data

return None
================================================================
RUGCHECK
================================================================

def get_rugcheck_report(mint):

data, error = api_get(
    f"{RUGCHECK_API}/tokens/{mint}/report"
)

if error:
    return None

return data
================================================================
RISK SCORE
================================================================

def calculate_risk_score(token, cfg):

score = 0
reasons = []
hard_fail = False

mint = token["mint"]

# ------------------------------------------------------------
# Liquidity
# ------------------------------------------------------------

liquidity = safe_float(
    token.get("liquidity")
)

if liquidity >= 50000:
    score += 20
elif liquidity >= 25000:
    score += 15
elif liquidity >= 10000:
    score += 10
else:
    hard_fail = True
    reasons.append("low liquidity")

# ------------------------------------------------------------
# Multi-source confirmation
# ------------------------------------------------------------

sources = token.get("sources", [])

if len(sources) >= 3:
    score += 15
elif len(sources) == 2:
    score += 10
elif len(sources) == 1:
    score += 5

# ------------------------------------------------------------
# Activity
# ------------------------------------------------------------

v5 = safe_float(
    token.get("volume_5m")
)

v1 = safe_float(
    token.get("volume_1h")
)

if v5 >= 5000:
    score += 15
elif v5 >= 1000:
    score += 10
elif v5 >= 250:
    score += 5

if v1 >= 25000:
    score += 10
elif v1 >= 5000:
    score += 7
elif v1 >= 1000:
    score += 3

# ------------------------------------------------------------
# Freshness
# ------------------------------------------------------------

age = age_minutes(
    token.get("pair_created_at")
)

if age is not None:

    if age <= 30:
        score += 15
    elif age <= 120:
        score += 12
    elif age <= 360:
        score += 7
    elif age <= cfg["max_age_minutes"]:
        score += 3

# ------------------------------------------------------------
# RugCheck
# ------------------------------------------------------------

report = get_rugcheck_report(mint)

if report is None:

    # We don't hard fail here because external APIs can temporarily
    # fail. Instead, we reduce the score.
    score -= 20
    reasons.append(
        "RugCheck unavailable"
    )

else:

    mint_authority = report.get(
        "mintAuthority"
    )

    freeze_authority = report.get(
        "freezeAuthority"
    )

    if mint_authority not in (None, "", "null"):
        hard_fail = True
        reasons.append(
            "mint authority active"
        )
    else:
        score += 10

    if freeze_authority not in (None, "", "null"):
        hard_fail = True
        reasons.append(
            "freeze authority active"
        )
    else:
        score += 10

    # RugCheck top holders.
    top_holders = report.get(
        "topHolders"
    ) or []

    top10 = 0

    for holder in top_holders[:10]:

        pct = safe_float(
            holder.get("pct")
        )

        # Some APIs represent pct as decimal.
        if pct <= 1:
            pct *= 100

        top10 += pct

    if top10:

        token["top10_pct"] = top10

        if top10 > cfg["max_top10_pct"]:
            hard_fail = True
            reasons.append(
                f"top10 {top10:.1f}%"
            )
        else:
            score += 15

# ------------------------------------------------------------
# Birdeye security enrichment
# ------------------------------------------------------------

security = get_birdeye_security(mint)

if security:

    # API schemas can evolve, so only use fields if present.
    suspicious = (
        security.get("is_scam")
        or security.get("is_honeypot")
        or security.get("honeypot")
    )

    if suspicious:
        hard_fail = True
        reasons.append(
            "security provider flagged token"
        )
    else:
        score += 5

token["risk_score"] = max(
    0,
    min(100, score)
)

token["risk_reasons"] = reasons

if hard_fail:
    return False, (
        " | ".join(reasons)
        or "hard risk failure"
    )

if token["risk_score"] < cfg["min_score"]:
    return False, (
        f"score {token['risk_score']:.0f}"
        f" < {cfg['min_score']}"
    )

return True, "risk-pass"
================================================================
JUPITER
================================================================

def jupiter_quote(
input_mint,
output_mint,
amount,
slippage_bps=500
):

try:

    r = SESSION.get(
        f"{JUPITER_API}/quote",
        params={
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": amount,
            "slippageBps": slippage_bps,
        },
        timeout=15,
    )

    if r.status_code != 200:
        return None

    return r.json()

except Exception as e:

    print(
        f"Jupiter quote error: {e}"
    )

    return None

def simulate_sell_check(
mint,
amount=1000
):

quote = jupiter_quote(
    mint,
    WSOL_MINT,
    amount
)

if not quote:
    return False

return safe_int(
    quote.get("outAmount")
) > 0
================================================================
BUY / SELL
================================================================

def jupiter_swap(quote, wallet):

if not wallet:
    return None

try:

    r = SESSION.post(
        f"{JUPITER_API}/swap",
        json={
            "quoteResponse": quote,
            "userPublicKey": str(
                wallet.pubkey()
            ),
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": "auto",
        },
        timeout=20,
    )

    if r.status_code != 200:
        print(
            "Jupiter swap build failed:",
            r.text[:500]
        )
        return None

    payload = r.json()

    raw = base64.b64decode(
        payload["swapTransaction"]
    )

    unsigned = (
        VersionedTransaction
        .from_bytes(raw)
    )

    signed = VersionedTransaction(
        unsigned.message,
        [wallet]
    )

    signature = (
        send_raw_transaction_rpc(
            bytes(signed)
        )
    )

    if not signature:
        return None

    if not confirm_transaction_rpc(
        signature
    ):
        print(
            "Transaction not confirmed:",
            signature
        )

        return None

    return signature

except Exception as e:

    print(
        f"swap execution error: {e}"
    )

    return None

def do_buy(
state,
mint,
symbol,
sol_amount
):

lamports = int(
    sol_amount * 1_000_000_000
)

quote = jupiter_quote(
    WSOL_MINT,
    mint,
    lamports
)

if not quote:
    return None

out_amount = safe_int(
    quote.get("outAmount")
)

if out_amount <= 0:
    return None

if state.paper_mode:

    sig = "PAPER"

else:

    if not state.wallet:
        return None

    sig = jupiter_swap(
        quote,
        state.wallet
    )

    if not sig:
        return None

return {
    "mint": mint,
    "symbol": symbol,
    "entry_sol": sol_amount,
    "out_amount": out_amount,
    "opened_at": datetime.now().isoformat(),
    "buy_sig": sig,
    "peak_pnl_pct": 0.0,
}

def get_current_pnl_pct(
state,
position
):

if state.paper_mode:

    return (
        position.get("peak_pnl_pct", 0)
        + random.uniform(-5, 8)
    )

quote = jupiter_quote(
    position["mint"],
    WSOL_MINT,
    position.get("out_amount", 1)
)

if not quote:
    return None

current_sol = (
    safe_int(
        quote.get("outAmount")
    )
    / 1_000_000_000
)

entry = safe_float(
    position.get("entry_sol")
)

if entry <= 0:
    return None

return (
    (current_sol - entry)
    / entry
    * 100
)

def do_sell(
state,
position
):

if state.paper_mode:

    pnl_pct = random.uniform(
        -30,
        60
    )

    entry = position[
        "entry_sol"
    ]

    profit = (
        entry
        * pnl_pct
        / 100
    )

    return {
        "date": datetime.now().strftime(
            "%Y-%m-%d"
        ),
        "time": datetime.now().strftime(
            "%H:%M:%S"
        ),
        "symbol": position["symbol"],
        "mint": position["mint"],
        "entry_sol": entry,
        "exit_sol": entry + profit,
        "profit": profit,
        "sell_sig": "PAPER",
    }

if not state.wallet:
    return None

balance, _ = get_token_balance(
    state.wallet_address,
    position["mint"]
)

if balance <= 0:
    return None

quote = jupiter_quote(
    position["mint"],
    WSOL_MINT,
    balance
)

if not quote:
    return None

signature = jupiter_swap(
    quote,
    state.wallet
)

if not signature:
    return None

exit_sol = (
    safe_int(
        quote.get("outAmount")
    )
    / 1_000_000_000
)

profit = (
    exit_sol
    - position["entry_sol"]
)

return {
    "date": datetime.now().strftime(
        "%Y-%m-%d"
    ),
    "time": datetime.now().strftime(
        "%H:%M:%S"
    ),
    "symbol": position["symbol"],
    "mint": position["mint"],
    "entry_sol": position["entry_sol"],
    "exit_sol": exit_sol,
    "profit": profit,
    "sell_sig": signature,
}
================================================================
DAILY PNL
================================================================

def daily_pnl(state):

today = datetime.now().strftime(
    "%Y-%m-%d"
)

with state.lock:

    return sum(
        safe_float(t.get("profit"))
        for t in state.trades
        if t.get("date") == today
    )
================================================================
SCAN PIPELINE
================================================================

def run_scan(state):

started = time.time()

state.log(
    ">>> MULTI-SOURCE SCAN STARTED",
    "sys"
)

candidates = discover_all()

state.log(
    f"DISCOVERY: {len(candidates)} unique candidates",
    "sys"
)

rejected = []
final = []

market_pass = 0
risk_pass = 0
quote_pass = 0

# Only deeply score the best candidates.
candidates = candidates[
    :MAX_CANDIDATES_TO_SCORE
]

for token in candidates:

    symbol = token.get(
        "symbol",
        "UNKNOWN"
    )

    ok, reason = market_filter(
        token,
        state.config
    )

    if not ok:

        rejected.append({
            **token,
            "stage": "MARKET",
            "reason": reason,
        })

        continue

    market_pass += 1

    state.log(
        f"MARKET PASS {symbol} "
        f"${token['liquidity']:,.0f} "
        f"{fmt_age(age_minutes(token.get('pair_created_at')))}",
        "scan"
    )

    ok, reason = calculate_risk_score(
        token,
        state.config
    )

    if not ok:

        rejected.append({
            **token,
            "stage": "RISK",
            "reason": reason,
        })

        state.log(
            f"RISK BLOCK {symbol}: {reason}",
            "warn"
        )

        continue

    risk_pass += 1

    # --------------------------------------------------------
    # Quote/sell route check
    # --------------------------------------------------------

    if state.config.get(
        "require_sell_quote",
        True
    ):

        if not simulate_sell_check(
            token["mint"]
        ):

            rejected.append({
                **token,
                "stage": "ROUTE",
                "reason": "no sell route",
            })

            state.log(
                f"ROUTE BLOCK {symbol}: no sell quote",
                "error"
            )

            continue

    quote_pass += 1

    token["status"] = "READY"
    token["decision"] = "BUY-CANDIDATE"

    final.append(token)

    state.log(
        f"🟢 READY {symbol} "
        f"score={token.get('risk_score', 0):.0f} "
        f"liq=${token['liquidity']:,.0f}",
        "buy"
    )

state.scan_stats.update({
    "last_scan": datetime.now().strftime(
        "%H:%M:%S"
    ),
    "market_pass": market_pass,
    "risk_pass": risk_pass,
    "quote_pass": quote_pass,
    "final_candidates": len(final),
})

final.sort(
    key=lambda x: (
        safe_float(
            x.get("risk_score")
        ),
        safe_float(
            x.get("volume_5m")
        ),
        safe_float(
            x.get("liquidity")
        ),
    ),
    reverse=True
)

with state.lock:
    state.discovered = final[:20]
    state.rejected = rejected[:100]

elapsed = time.time() - started

state.log(
    f"<<< SCAN COMPLETE "
    f"ready={len(final)} "
    f"elapsed={elapsed:.1f}s",
    "sys"
)

return final
================================================================
ENGINE
================================================================

def engine_loop(state):

state.log(
    "CYBER ENGINE ONLINE",
    "sys"
)

while True:

    try:

        if not state.running:
            time.sleep(2)
            continue

        # ----------------------------------------------------
        # Daily kill switch
        # ----------------------------------------------------

        if (
            daily_pnl(state)
            <= -abs(
                state.config[
                    "daily_loss_limit"
                ]
            )
        ):

            state.log(
                "🛑 DAILY LOSS LIMIT HIT",
                "error"
            )

            with state.lock:
                state.running = False

            continue

        # ----------------------------------------------------
        # Position management
        # ----------------------------------------------------

        with state.lock:
            positions = list(
                state.positions
            )

        for pos in positions:

            pnl = get_current_pnl_pct(
                state,
                pos
            )

            if pnl is None:
                continue

            with state.lock:

                pos["peak_pnl_pct"] = max(
                    pos.get(
                        "peak_pnl_pct",
                        0
                    ),
                    pnl
                )

                peak = pos[
                    "peak_pnl_pct"
                ]

            tp = state.config[
                "take_profit_pct"
            ]

            trail = state.config[
                "trailing_stop_pct"
            ]

            should_close = False
            reason = ""

            if pnl >= tp:

                should_close = True
                reason = "TAKE PROFIT"

            elif (
                peak > 0
                and pnl <= peak - trail
            ):

                should_close = True
                reason = "TRAILING STOP"

            elif (
                peak <= 0
                and pnl <= -trail
            ):

                should_close = True
                reason = "STOP LOSS"

            if should_close:

                trade = do_sell(
                    state,
                    pos
                )

                if trade:

                    with state.lock:

                        if pos in state.positions:
                            state.positions.remove(
                                pos
                            )

                        state.trades.append(
                            trade
                        )

                    tag = (
                        "sell-win"
                        if trade["profit"] >= 0
                        else "sell-loss"
                    )

                    state.log(
                        f"CLOSED {pos['symbol']} "
                        f"{reason} "
                        f"{trade['profit']:+.4f} SOL",
                        tag
                    )

                    save_persisted(
                        state
                    )

        # ----------------------------------------------------
        # New scan
        # ----------------------------------------------------

        with state.lock:

            open_count = len(
                state.positions
            )

            cfg = dict(
                state.config
            )

            can_trade = (
                state.paper_mode
                or state.wallet is not None
            )

        if (
            open_count
            < cfg["max_positions"]
            and can_trade
        ):

            final = run_scan(
                state
            )

            for token in final:

                with state.lock:

                    if len(
                        state.positions
                    ) >= cfg[
                        "max_positions"
                    ]:
                        break

                    already = any(
                        p["mint"]
                        == token["mint"]
                        for p in state.positions
                    )

                if already:
                    continue

                # Do not automatically live-buy unless explicitly
                # enabled through the UI.
                if not state.paper_mode:
                    if not state.config.get(
                        "live_auto_buy",
                        False
                    ):
                        state.log(
                            f"LIVE LOCK: "
                            f"{token['symbol']} "
                            f"ready but auto-buy disabled",
                            "warn"
                        )
                        continue

                pos = do_buy(
                    state,
                    token["mint"],
                    token["symbol"],
                    cfg["snipe_amount"]
                )

                if pos:

                    with state.lock:
                        state.positions.append(
                            pos
                        )

                    state.log(
                        f"🎯 BUY "
                        f"{token['symbol']} "
                        f"{cfg['snipe_amount']} SOL",
                        "buy"
                    )

                    save_persisted(
                        state
                    )

        time.sleep(
            SCAN_INTERVAL_SECONDS
        )

    except Exception as e:

        state.log(
            f"ENGINE ERROR: "
            f"{type(e).__name__}: {e}",
            "error"
        )

        time.sleep(5)

@st.cache_resource
def start_engine_thread(_state):

thread = threading.Thread(
    target=engine_loop,
    args=(_state,),
    daemon=True,
    name="cyber-sniper-engine",
)

thread.start()

return thread
================================================================
CYBERPUNK UI
================================================================

st.set_page_config(
page_title="CYBER SNIPER // NEON HUNTER",
page_icon="🟣",
layout="wide",
)

st.markdown("""

<style> @import url( 'https://fonts.googleapis.com/css2?family=Orbitron:wght@400;600;700;900&family=Share+Tech+Mono&display=swap' ); :root { --cyan:#00ffe6; --pink:#ff00d9; --green:#00ff66; --red:#ff174f; --purple:#8a2be2; --blue:#00aaff; --bg:#020308; } .stApp { background: radial-gradient( circle at 15% 15%, rgba(255,0,217,.12), transparent 30% ), radial-gradient( circle at 85% 75%, rgba(0,255,230,.10), transparent 35% ), linear-gradient( 135deg, #020308, #09001b, #00131a ); color:#d9ffff; } .stApp::before { content:""; position:fixed; inset:0; background-image: linear-gradient( rgba(0,255,230,.045) 1px, transparent 1px ), linear-gradient( 90deg, rgba(255,0,217,.035) 1px, transparent 1px ); background-size: 35px 35px, 35px 35px; animation:gridmove 20s linear infinite; pointer-events:none; z-index:-3; } .stApp::after { content:""; position:fixed; inset:0; background: repeating-linear-gradient( 0deg, rgba(0,0,0,.16) 0px, rgba(0,0,0,.16) 1px, transparent 2px, transparent 4px ); pointer-events:none; z-index:-2; animation:flicker 7s infinite; } @keyframes gridmove { from { background-position:0 0,0 0; } to { background-position:0 700px,700px 0; } } @keyframes flicker { 0%,100% { opacity:.5; } 48% { opacity:.4; } 49% { opacity:.7; } 50% { opacity:.35; } } @keyframes pulse { 0%,100% { box-shadow: 0 0 10px rgba(0,255,230,.2), inset 0 0 10px rgba(255,0,217,.05); } 50% { box-shadow: 0 0 30px rgba(0,255,230,.45), 0 0 60px rgba(255,0,217,.18), inset 0 0 20px rgba(255,0,217,.08); } } @keyframes glitch { 0%,100% { transform:translate(0); } 92% { transform:translate(0); } 93% { transform:translate(-3px,1px); } 94% { transform:translate(3px,-1px); } 95% { transform:translate(0); } } .cyber-header { position:relative; padding:30px; border:1px solid var(--pink); border-radius:15px; background: linear-gradient( 135deg, rgba(4,0,15,.95), rgba(22,0,40,.92), rgba(0,25,34,.92) ); box-shadow: 0 0 25px rgba(255,0,217,.3), inset 0 0 30px rgba(0,255,230,.04); overflow:hidden; } .cyber-header::before { content:""; position:absolute; top:0; left:-100%; width:50%; height:100%; background: linear-gradient( 90deg, transparent, rgba(0,255,230,.15), transparent ); animation:scanline 4s linear infinite; } @keyframes scanline { from { left:-100%; } to { left:200%; } } .cyber-title { font-family:'Orbitron',sans-serif; font-weight:900; font-size:38px; letter-spacing:6px; color:var(--cyan); text-shadow: 0 0 5px #fff, 0 0 15px var(--cyan), 0 0 30px var(--pink); animation:glitch 5s infinite; } .cyber-sub { color:var(--pink); font-family:'Share Tech Mono',monospace; letter-spacing:3px; } .cyber-card { background: linear-gradient( 145deg, rgba(3,4,13,.94), rgba(15,0,35,.9) ); border:1px solid rgba(0,255,230,.55); border-radius:10px; padding:17px; box-shadow: 0 0 14px rgba(0,255,230,.14); transition: transform .2s, border .2s, box-shadow .2s; } .cyber-card:hover { transform: translateY(-3px) scale(1.01); border-color:var(--pink); box-shadow: 0 0 30px rgba(255,0,217,.3); } .metric-value { color:var(--cyan); font-family:'Orbitron',sans-serif; font-size:22px; font-weight:700; text-shadow: 0 0 12px var(--cyan); } .terminal { background:#000; border:1px solid var(--green); border-radius:8px; padding:15px; height:280px; overflow-y:auto; font-family:'Share Tech Mono',monospace; font-size:12px; box-shadow: inset 0 0 25px rgba(0,255,102,.08), 0 0 15px rgba(0,255,102,.15); } .terminal .buy { color:var(--cyan); } .terminal .sell-win { color:var(--green); } .terminal .sell-loss { color:var(--red); } .terminal .error { color:var(--red); } .terminal .warn { color:#ffaa00; } .terminal .scan { color:#00aaff; } .terminal .sys { color:var(--pink); } section[data-testid="stSidebar"] { background: linear-gradient( 180deg, #020308, #09001a, #00131a ); border-right:1px solid var(--pink); } .stButton > button { background: linear-gradient( 135deg, #05000d, #160025 ); color:var(--cyan); border:1px solid var(--cyan); border-radius:7px; font-family:'Orbitron',sans-serif; font-weight:700; letter-spacing:2px; transition:.2s; } .stButton > button:hover { color:#000; background: linear-gradient( 135deg, var(--pink), var(--cyan) ); box-shadow: 0 0 25px rgba(255,0,217,.5); } div[data-testid="stMetric"] { background: rgba(0,0,0,.25); border: 1px solid rgba(0,255,230,.2); border-radius:8px; } h1,h2,h3 { font-family:'Orbitron',sans-serif; } </style>

""", unsafe_allow_html=True)

================================================================
STATE
================================================================

state = get_state()
start_engine_thread(state)

================================================================
HEADER
================================================================

st.markdown("""

<div class="cyber-header">

<div class="cyber-title"> 🟣 CYBER SNIPER // NEON HUNTER </div>

<div class="cyber-sub"> [ MULTI-SOURCE INTELLIGENCE ] [ SOLANA ] [ RISK ENGINE ] [ PAPER-FIRST ] </div>

</div> """, unsafe_allow_html=True)

================================================================
SIDEBAR
================================================================

with st.sidebar:

st.markdown(
    "## 🔐 WALLET NODE"
)

if (
    ENV_PRIVATE_KEY
    and state.wallet is None
):

    try:

        state.wallet = (
            Keypair.from_base58_string(
                ENV_PRIVATE_KEY
            )
        )

        state.wallet_address = str(
            state.wallet.pubkey()
        )

    except Exception as e:

        st.error(
            f"Wallet error: {e}"
        )

if state.wallet:

    st.success(
        "WALLET CONNECTED"
    )

    st.code(
        f"{state.wallet_address[:8]}..."
        f"{state.wallet_address[-8:]}"
    )

else:

    key_input = st.text_input(
        "Private key",
        type="password"
    )

    if st.button(
        "CONNECT WALLET"
    ):

        try:

            wallet = (
                Keypair.from_base58_string(
                    key_input.strip()
                )
            )

            state.wallet = wallet
            state.wallet_address = str(
                wallet.pubkey()
            )

            st.rerun()

        except Exception as e:

            st.error(
                f"Invalid key: {e}"
            )

st.divider()

# ------------------------------------------------------------
# MODE
# ------------------------------------------------------------

st.markdown(
    "## ⚠️ OPERATING MODE"
)

new_paper = st.toggle(
    "PAPER MODE",
    value=state.paper_mode
)

if new_paper != state.paper_mode:

    with state.lock:
        state.paper_mode = new_paper

if not state.paper_mode:

    st.error(
        "LIVE MODE = REAL FUNDS"
    )

    st.warning(
        "Live auto-buy is intentionally disabled "
        "until explicitly enabled."
    )

    live_confirm = st.text_input(
        'Type "ENABLE LIVE AUTO BUY"',
        type="password"
    )

    live_enabled = (
        live_confirm.strip().upper()
        == "ENABLE LIVE AUTO BUY"
    )

    with state.lock:
        state.config[
            "live_auto_buy"
        ] = live_enabled

else:

    with state.lock:
        state.config[
            "live_auto_buy"
        ] = False

st.divider()

# ------------------------------------------------------------
# DISCOVERY
# ------------------------------------------------------------

st.markdown(
    "## 🛰️ DISCOVERY NETWORK"
)

enable_birdeye = st.checkbox(
    "BIRDEYE NEW LISTINGS",
    value=state.config[
        "enable_birdeye"
    ],
    disabled=not BIRDEYE_API_KEY,
)

enable_gecko = st.checkbox(
    "GECKOTERMINAL NEW POOLS",
    value=state.config[
        "enable_gecko"
    ],
)

enable_dex = st.checkbox(
    "DEXSCREENER",
    value=state.config[
        "enable_dexscreener"
    ],
)

meme_mode = st.checkbox(
    "BIRDEYE MEME-PLATFORM MODE",
    value=state.config[
        "birdeye_meme_mode"
    ],
    disabled=not BIRDEYE_API_KEY,
)

with state.lock:

    state.config.update({
        "enable_birdeye": enable_birdeye,
        "enable_gecko": enable_gecko,
        "enable_dexscreener": enable_dex,
        "birdeye_meme_mode": meme_mode,
    })

st.divider()

# ------------------------------------------------------------
# STRATEGY
# ------------------------------------------------------------

st.markdown(
    "## 🎯 HUNT PARAMETERS"
)

preset_name = st.selectbox(
    "PROFILE",
    [
        "🛡️ SAFE HUNTER",
        "⚖️ BALANCED",
        "🔥 HIGH VELOCITY",
    ],
)

presets = {

    "🛡️ SAFE HUNTER": {
        "amount": 0.02,
        "min_liq": 25000,
        "min_v5": 1000,
        "min_v1": 5000,
        "max_age": 720,
        "score": 70,
        "top10": 30,
        "tp": 20,
        "trail": 8,
    },

    "⚖️ BALANCED": {
        "amount": 0.05,
        "min_liq": 10000,
        "min_v5": 250,
        "min_v1": 1000,
        "max_age": 720,
        "score": 55,
        "top10": 40,
        "tp": 50,
        "trail": 15,
    },

    "🔥 HIGH VELOCITY": {
        "amount": 0.10,
        "min_liq": 5000,
        "min_v5": 100,
        "min_v1": 500,
        "max_age": 1440,
        "score": 45,
        "top10": 50,
        "tp": 100,
        "trail": 25,
    },
}

preset = presets[
    preset_name
]

snipe_amount = st.slider(
    "BUY SOL",
    0.01,
    MAX_TRADE_SOL_CAP,
    preset["amount"],
    0.01,
)

min_liquidity = st.number_input(
    "MIN LIQUIDITY USD",
    1000,
    1000000,
    preset["min_liq"],
    1000,
)

min_volume_5m = st.number_input(
    "MIN 5M VOLUME USD",
    0,
    100000,
    preset["min_v5"],
    100,
)

min_volume_1h = st.number_input(
    "MIN 1H VOLUME USD",
    0,
    1000000,
    preset["min_v1"],
    500,
)

max_age_hours = st.number_input(
    "MAX POOL AGE HOURS",
    1,
    168,
    max(1, preset["max_age"] // 60),
    1,
)

min_score = st.slider(
    "MIN CYBER SCORE",
    0,
    100,
    preset["score"],
    5,
)

max_top10 = st.slider(
    "MAX TOP 10 HOLDER %",
    10,
    90,
    preset["top10"],
    5,
)

max_positions = st.slider(
    "MAX POSITIONS",
    1,
    10,
    5,
)

take_profit = st.slider(
    "TAKE PROFIT %",
    10,
    300,
    preset["tp"],
    5,
)

trailing_stop = st.slider(
    "TRAILING STOP %",
    5,
    50,
    preset["trail"],
    5,
)

daily_loss_limit = st.number_input(
    "DAILY LOSS LIMIT SOL",
    0.01,
    10.0,
    0.2,
    0.01,
)

require_sell_quote = st.checkbox(
    "REQUIRE SELL ROUTE",
    value=True,
)

with state.lock:

    state.config.update({

        "snipe_amount": snipe_amount,

        "min_liquidity_usd":
            min_liquidity,

        "min_volume_5m_usd":
            min_volume_5m,

        "min_volume_1h_usd":
            min_volume_1h,

        "max_age_minutes":
            max_age_hours * 60,

        "min_score":
            min_score,

        "max_top10_pct":
            max_top10,

        "max_positions":
            max_positions,

        "take_profit_pct":
            take_profit,

        "trailing_stop_pct":
            trailing_stop,

        "daily_loss_limit":
            daily_loss_limit,

        "require_sell_quote":
            require_sell_quote,
    })

st.divider()

# ------------------------------------------------------------
# ENGINE
# ------------------------------------------------------------

st.markdown(
    "## 🤖 NEURAL ENGINE"
)

st.caption(
    f"Scan interval: {SCAN_INTERVAL_SECONDS}s"
)

if not state.running:

    if st.button(
        "⚡ START HUNT",
        width="stretch",
    ):

        with state.lock:
            state.running = True

        state.log(
            "OPERATOR ACTIVATED HUNT",
            "sys"
        )

        st.rerun()

else:

    if st.button(
        "🛑 STOP HUNT",
        width="stretch",
    ):

        with state.lock:
            state.running = False

        state.log(
            "OPERATOR STOPPED HUNT",
            "warn"
        )

        st.rerun()

if st.button(
    "🔎 FORCE SCAN",
    width="stretch",
):

    final = run_scan(state)

    st.success(
        f"{len(final)} candidates ready"
    )

    st.rerun()
================================================================
TOP STATUS
================================================================

with state.lock:

running = state.running
paper_mode = state.paper_mode

positions_copy = list(
    state.positions
)

trades_copy = list(
    state.trades
)

discovered_copy = list(
    state.discovered
)

rejected_copy = list(
    state.rejected
)

logs_copy = list(
    state.logs
)

source_status = dict(
    state.source_status
)

stats = dict(
    state.scan_stats
)
================================================================
METRICS
================================================================

balance = get_wallet_balance(
state.wallet_address
) if state.wallet else 0

total_trades = len(
trades_copy
)

wins = sum(
1
for t in trades_copy
if safe_float(
t.get("profit")
) > 0
)

win_rate = (
wins / total_trades * 100
if total_trades
else 0
)

total_pnl = sum(
safe_float(
t.get("profit")
)
for t in trades_copy
)

mode = (
"PAPER"
if paper_mode
else "LIVE"
)

c1,c2,c3,c4,c5 = st.columns(5)

with c1:
st.markdown(
f"""
<div class="cyber-card">
<small>ENGINE</small>
<div class="metric-value">
{"🟢 ONLINE" if running else "🔴 OFFLINE"}
</div>
<small>{mode}</small>
</div>
""",
unsafe_allow_html=True
)

with c2:
st.markdown(
f"""
<div class="cyber-card">
<small>DISCOVERED</small>
<div class="metric-value">
{stats.get("unique_candidates",0)}
</div>
<small>UNIQUE MINTS</small>
</div>
""",
unsafe_allow_html=True
)

with c3:
st.markdown(
f"""
<div class="cyber-card">
<small>READY</small>
<div class="metric-value">
{stats.get("final_candidates",0)}
</div>
<small>TRADE CANDIDATES</small>
</div>
""",
unsafe_allow_html=True
)

with c4:
st.markdown(
f"""
<div class="cyber-card">
<small>POSITIONS</small>
<div class="metric-value">
{len(positions_copy)}
</div>
<small>OPEN</small>
</div>
""",
unsafe_allow_html=True
)

with c5:
st.markdown(
f"""
<div class="cyber-card">
<small>NET P/L</small>
<div class="metric-value">
{total_pnl:+.4f}
</div>
<small>SOL</small>
</div>
""",
unsafe_allow_html=True
)

================================================================
SOURCE STATUS
================================================================

st.markdown(
"### 🛰️ SOURCE NETWORK"
)

source_cols = st.columns(3)

for i, name in enumerate([
"Birdeye",
"GeckoTerminal",
"DEXScreener",
]):

info = source_status.get(
    name,
    {}
)

ok = info.get(
    "ok",
    False
)

count = info.get(
    "count",
    0
)

error = info.get(
    "error",
    ""
)

with source_cols[i]:

    color = (
        "#00ff66"
        if ok
        else "#ff174f"
    )

    st.markdown(
        f"""
        <div class="cyber-card">
        <div style="
            color:{color};
            font-family:Orbitron;
            font-weight:bold;
        ">
        {"● ONLINE" if ok else "● OFFLINE"}
        </div>

        <div style="
            color:#00ffe6;
            margin-top:8px;
        ">
        {name}
        </div>

        <div style="
            color:#888;
            font-size:11px;
            margin-top:6px;
        ">
        {count} candidates
        </div>

        <div style="
            color:#ff174f;
            font-size:10px;
            margin-top:5px;
        ">
        {error[:100]}
        </div>

        </div>
        """,
        unsafe_allow_html=True
    )
================================================================
PIPELINE
================================================================

st.markdown(
"### 🧠 HUNT PIPELINE"
)

p1,p2,p3,p4,p5 = st.columns(5)

pipeline = [
("DISCOVERY", stats.get("unique_candidates",0)),
("MARKET", stats.get("market_pass",0)),
("RISK", stats.get("risk_pass",0)),
("ROUTE", stats.get("quote_pass",0)),
("READY", stats.get("final_candidates",0)),
]

for col, (label, value) in zip(
[p1,p2,p3,p4,p5],
pipeline
):

with col:

    st.markdown(
        f"""
        <div class="cyber-card">
        <div style="
            color:#ff00d9;
            font-family:Orbitron;
            font-size:10px;
        ">
        {label}
        </div>

        <div class="metric-value">
        {value}
        </div>
        </div>
        """,
        unsafe_allow_html=True
    )
================================================================
READY TOKENS
================================================================

st.markdown(
"### 🎯 READY TARGETS"
)

if discovered_copy:

for idx, token in enumerate(
    discovered_copy[:10]
):

    symbol = token.get(
        "symbol",
        "UNKNOWN"
    )

    name = token.get(
        "name",
        "Unknown"
    )

    mint = token.get(
        "mint",
        ""
    )

    liquidity = safe_float(
        token.get("liquidity")
    )

    v5 = safe_float(
        token.get("volume_5m")
    )

    v1 = safe_float(
        token.get("volume_1h")
    )

    score = safe_float(
        token.get("risk_score")
    )

    age = fmt_age(
        age_minutes(
            token.get(
                "pair_created_at"
            )
        )
    )

    sources = ", ".join(
        token.get(
            "sources",
            []
        )
    )

    st.markdown(
        f"""
        <div class="cyber-card"
             style="margin-bottom:8px;text-align:left;">

        <div style="
            color:#00ffe6;
            font-family:Orbitron;
            font-size:16px;
            font-weight:bold;
        ">
        🪙 {symbol}
        </div>

        <div style="
            color:#888;
            font-size:11px;
            margin-top:4px;
        ">
        {name}
        </div>

        <div style="
            color:#aaa;
            font-size:10px;
            margin-top:7px;
        ">
        MINT:
        {mint[:8]}...{mint[-8:]}
        </div>

        <div style="
            color:#00ff66;
            font-size:11px;
            margin-top:8px;
        ">
        LIQ ${liquidity:,.0f}
        &nbsp; | &nbsp;
        5M ${v5:,.0f}
        &nbsp; | &nbsp;
        1H ${v1:,.0f}
        &nbsp; | &nbsp;
        AGE {age}
        &nbsp; | &nbsp;
        SCORE {score:.0f}
        </div>

        <div style="
            color:#ff00d9;
            font-size:10px;
            margin-top:5px;
        ">
        SOURCES: {sources}
        </div>

        </div>
        """,
        unsafe_allow_html=True
    )

    c1,c2 = st.columns(
        [5,1]
    )

    with c1:

        st.caption(
            "Risk: "
            + (
                " | ".join(
                    token.get(
                        "risk_reasons",
                        []
                    )
                )
                or "No warnings"
            )
        )

    with c2:

        disabled = (
            len(positions_copy)
            >= state.config[
                "max_positions"
            ]
        )

        if st.button(
            f"BUY {snipe_amount if 'snipe_amount' in locals() else state.config['snipe_amount']} SOL",
            key=f"manual_buy_{idx}",
            disabled=disabled,
        ):

            pos = do_buy(
                state,
                token["mint"],
                symbol,
                state.config[
                    "snipe_amount"
                ],
            )

            if pos:

                with state.lock:

                    state.positions.append(
                        pos
                    )

                state.log(
                    f"MANUAL BUY {symbol}",
                    "buy"
                )

                save_persisted(
                    state
                )

                st.rerun()

            else:

                st.error(
                    "Buy failed"
                )

else:

st.info(
    "No ready targets yet. "
    "Run FORCE SCAN or start the engine."
)
================================================================
REJECTION INTELLIGENCE
================================================================

st.markdown(
"### ☠️ REJECTION INTELLIGENCE"
)

if rejected_copy:

rows = []

for token in rejected_copy[:50]:

    rows.append({
        "Symbol": token.get(
            "symbol",
            "UNKNOWN"
        ),

        "Stage": token.get(
            "stage",
            "?"
        ),

        "Reason": token.get(
            "reason",
            "?"
        ),

        "Liquidity": round(
            safe_float(
                token.get("liquidity")
            )
        ),

        "Age": fmt_age(
            age_minutes(
                token.get(
                    "pair_created_at"
                )
            )
        ),

        "Sources": ", ".join(
            token.get(
                "sources",
                []
            )
        ),
    })

st.dataframe(
    pd.DataFrame(rows),
    width="stretch",
    hide_index=True,
)

else:

st.caption(
    "No rejected candidates recorded yet."
)
================================================================
OPEN POSITIONS
================================================================

st.markdown(
"### 📌 OPEN POSITIONS"
)

if positions_copy:

for idx, pos in enumerate(
    positions_copy
):

    pnl = safe_float(
        pos.get(
            "peak_pnl_pct"
        )
    )

    color = (
        "#00ff66"
        if pnl >= 0
        else "#ff174f"
    )

    c1,c2 = st.columns(
        [5,1]
    )

    with c1:

        st.markdown(
            f"""
            <div class="cyber-card">
            <div style="
                color:#00ffe6;
                font-family:Orbitron;
            ">
            🎯 {pos["symbol"]}
            </div>

            <div style="
                color:#888;
                font-size:11px;
            ">
            Entry:
            {pos["entry_sol"]:.4f} SOL
            </div>

            <div style="
                color:{color};
                font-weight:bold;
            ">
            PEAK {pnl:+.2f}%
            </div>
            </div>
            """,
            unsafe_allow_html=True
        )

    with c2:

        if st.button(
            "SELL",
            key=f"sell_{idx}",
        ):

            trade = do_sell(
                state,
                pos
            )

            if trade:

                with state.lock:

                    if pos in state.positions:
                        state.positions.remove(
                            pos
                        )

                    state.trades.append(
                        trade
                    )

                state.log(
                    f"MANUAL SELL "
                    f"{pos['symbol']} "
                    f"{trade['profit']:+.4f} SOL",
                    (
                        "sell-win"
                        if trade["profit"] >= 0
                        else "sell-loss"
                    )
                )

                save_persisted(
                    state
                )

                st.rerun()

            else:

                st.error(
                    "Sell failed"
                )

else:

st.info(
    "No open positions."
)
================================================================
TERMINAL
================================================================

st.markdown(
"### 🖥️ NEON TERMINAL"
)

terminal_html = "".join(
logs_copy
) or (
'<div class="sys">'
'// SYSTEM READY...'
'</div>'
)

st.markdown(
f'<div class="terminal">'
f'{terminal_html}'
f'</div>',
unsafe_allow_html=True
)

================================================================
TRADE HISTORY
================================================================

st.markdown(
"### 📜 TRADE HISTORY"
)

if trades_copy:

trade_rows = []

for trade in reversed(
    trades_copy[-50:]
):

    trade_rows.append({
        "Date": trade.get(
            "date",
            ""
        ),

        "Time": trade.get(
            "time",
            ""
        ),

        "Symbol": trade.get(
            "symbol",
            ""
        ),

        "Entry SOL": round(
            safe_float(
                trade.get(
                    "entry_sol"
                )
            ),
            5,
        ),

        "Exit SOL": round(
            safe_float(
                trade.get(
                    "exit_sol"
                )
            ),
            5,
        ),

        "Profit SOL": round(
            safe_float(
                trade.get(
                    "profit"
                )
            ),
            5,
        ),
    })

st.dataframe(
    pd.DataFrame(
        trade_rows
    ),
    width="stretch",
    hide_index=True,
)

else:

st.caption(
    "No trades yet."
)
================================================================
STATUS
================================================================

st.markdown(
"---"
)

if running:

st.markdown(
    """
    <div style="
    text-align:center;
    padding:20px;
    color:#00ff66;
    font-family:Orbitron;
    text-shadow:0 0 20px #00ff66;
    ">
    <h2>
    🟢 NEON HUNTER ONLINE
    </h2>
    <div style="
    color:#00ffe6;
    font-size:11px;
    ">
    MULTI-SOURCE INTELLIGENCE NETWORK ACTIVE
    </div>
    </div>
    """,
    unsafe_allow_html=True
)

else:

st.markdown(
    """
    <div style="
    text-align:center;
    padding:20px;
    color:#ff174f;
    font-family:Orbitron;
    ">
    <h2>
    🔴 HUNTER OFFLINE
    </h2>
    </div>
    """,
    unsafe_allow_html=True
)

if HAS_AUTOREFRESH:

st_autorefresh(
    interval=6000,
    key="cyber_refresh"
)

else:

st.caption(
    "Install streamlit-autorefresh "
    "for automatic dashboard refresh."
)

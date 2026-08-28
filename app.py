import os
import json
import time
import base64
import threading
import random
import traceback
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import requests
import pandas as pd
import streamlit as st

from dotenv import load_dotenv
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

try:
    from streamlit_autorefresh import st_autorefresh
    HAS_AUTOREFRESH = True
except ImportError:
    HAS_AUTOREFRESH = False

load_dotenv()

# ================================================================
# CONFIG
# ================================================================

APP_VERSION = "8.0"

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

HELIUS_KEY = os.getenv("HELIUS_KEY", "").strip()
BIRDEYE_KEY = os.getenv("BIRDEYE_API_KEY", "").strip()

HELIUS_RPC_URL = (
    f"https://mainnet.helius-rpc.com/?api-key={HELIUS_KEY}"
    if HELIUS_KEY
    else ""
)

# Jupiter endpoint can be overridden without editing the program.
JUPITER_API_BASE = os.getenv(
    "JUPITER_API_BASE",
    "https://quote-api.jup.ag/v6"
).rstrip("/")

DEXSCREENER_API = "https://api.dexscreener.com"
BIRDEYE_API = "https://public-api.birdeye.so"
RUGCHECK_API = "https://api.rugcheck.xyz/v1"

STATE_FILE = os.getenv(
    "CYBER_SNIPER_STATE_FILE",
    "cyber_sniper_state.json"
)

MAX_TRADE_SOL_CAP = 0.50

REQUEST_TIMEOUT = 15
SCAN_INTERVAL_SECONDS = 15

# ================================================================
# HTTP SESSION
# ================================================================

HTTP = requests.Session()
HTTP.headers.update({
    "User-Agent": "CyberSniper/8.0",
    "Accept": "application/json",
})

# ================================================================
# STATE
# ================================================================

@dataclass
class EngineState:
    # IMPORTANT:
    # This object is intentionally NOT passed as a normal argument
    # to a Streamlit cached function. See start_engine(_state).
    lock: threading.Lock = field(default_factory=threading.Lock)

    running: bool = False
    paper_mode: bool = True

    wallet: Optional[Keypair] = None
    wallet_address: str = ""

    positions: List[Dict] = field(default_factory=list)
    trades: List[Dict] = field(default_factory=list)

    discovered: List[Dict] = field(default_factory=list)
    logs: List[str] = field(default_factory=list)

    last_scan: str = "Never"
    last_scan_count: int = 0
    scan_errors: List[str] = field(default_factory=list)

    engine_started_at: str = ""

    config: Dict = field(default_factory=lambda: {
        "snipe_amount": 0.05,
        "take_profit_pct": 50.0,
        "trailing_stop_pct": 15.0,

        "min_liquidity_usd": 15000.0,
        "min_volume_24h": 5000.0,

        "max_top10_pct": 40.0,
        "min_lp_locked_pct": 0.0,

        "max_positions": 5,
        "daily_loss_limit": 0.20,

        "min_token_age_minutes": 0,
        "max_token_age_hours": 168,

        "min_score": 45,

        "use_dexscreener": True,
        "use_birdeye_new": True,
        "use_birdeye_ranked": True,
        "use_dex_profiles": True,
        "use_dex_boosts": True,

        "require_rugcheck": False,
        "require_sell_quote": False,
    })

    def log(self, message: str, tag: str = "sys"):
        ts = datetime.now().strftime("%H:%M:%S")

        safe = str(message).replace("<", "&lt;").replace(">", "&gt;")

        with self.lock:
            self.logs.insert(
                0,
                f'<div class="line {tag}">[{ts}] {safe}</div>'
            )
            self.logs = self.logs[:120]

        print(f"[{ts}] {message}")


# ================================================================
# STREAMLIT RESOURCE STATE
# ================================================================

@st.cache_resource
def get_state() -> EngineState:
    state = EngineState()
    load_persisted(state)
    return state


def load_persisted(state: EngineState):
    if not os.path.exists(STATE_FILE):
        return

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        with state.lock:
            state.positions = data.get("positions", [])
            state.trades = data.get("trades", [])

    except Exception as exc:
        print(f"state load error: {exc}")


def save_persisted(state: EngineState):
    try:
        with state.lock:
            data = {
                "positions": state.positions,
                "trades": state.trades,
            }

        tmp_file = STATE_FILE + ".tmp"

        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        os.replace(tmp_file, STATE_FILE)

    except Exception as exc:
        print(f"state save error: {exc}")


# ================================================================
# GENERIC HTTP HELPERS
# ================================================================

def http_get(
    url: str,
    *,
    params: Optional[Dict] = None,
    headers: Optional[Dict] = None,
    timeout: int = REQUEST_TIMEOUT,
):
    try:
        response = HTTP.get(
            url,
            params=params,
            headers=headers,
            timeout=timeout,
        )

        if response.status_code != 200:
            return None, f"HTTP {response.status_code}: {response.text[:250]}"

        try:
            return response.json(), None
        except Exception:
            return None, "Invalid JSON response"

    except requests.RequestException as exc:
        return None, str(exc)


def http_post(
    url: str,
    *,
    json_data: Optional[Dict] = None,
    headers: Optional[Dict] = None,
    timeout: int = REQUEST_TIMEOUT,
):
    try:
        response = HTTP.post(
            url,
            json=json_data,
            headers=headers,
            timeout=timeout,
        )

        if response.status_code not in (200, 201):
            return None, f"HTTP {response.status_code}: {response.text[:300]}"

        try:
            return response.json(), None
        except Exception:
            return None, "Invalid JSON response"

    except requests.RequestException as exc:
        return None, str(exc)


# ================================================================
# RPC
# ================================================================

def rpc_call(method: str, params: list):
    if not HELIUS_RPC_URL:
        return None, "HELIUS_KEY not configured"

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params,
    }

    try:
        response = HTTP.post(
            HELIUS_RPC_URL,
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )

        if response.status_code != 200:
            return None, f"RPC HTTP {response.status_code}"

        data = response.json()

        if "error" in data:
            return None, str(data["error"])

        return data.get("result"), None

    except Exception as exc:
        return None, str(exc)


def rpc_health() -> Tuple[bool, str]:
    result, error = rpc_call("getHealth", [])

    if error:
        return False, error

    return True, str(result)


def get_wallet_balance(pubkey: str) -> float:
    if not pubkey:
        return 0.0

    result, error = rpc_call("getBalance", [pubkey])

    if error or not result:
        return 0.0

    try:
        return float(result.get("value", 0)) / 1_000_000_000
    except Exception:
        return 0.0


def get_token_balance(
    pubkey: str,
    mint: str
) -> Tuple[int, int]:

    if not pubkey or not mint:
        return 0, 0

    result, error = rpc_call(
        "getTokenAccountsByOwner",
        [
            pubkey,
            {"mint": mint},
            {"encoding": "jsonParsed"},
        ],
    )

    if error or not result:
        return 0, 0

    try:
        accounts = result.get("value", [])

        if not accounts:
            return 0, 0

        amount = (
            accounts[0]["account"]["data"]
            ["parsed"]["info"]["tokenAmount"]
        )

        return int(amount["amount"]), int(amount["decimals"])

    except Exception:
        return 0, 0


def send_raw_transaction_rpc(
    signed_tx_bytes: bytes
) -> Optional[str]:

    if not HELIUS_RPC_URL:
        return None

    encoded = base64.b64encode(signed_tx_bytes).decode("utf-8")

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "sendTransaction",
        "params": [
            encoded,
            {
                "encoding": "base64",
                "skipPreflight": False,
                "preflightCommitment": "processed",
                "maxRetries": 3,
            },
        ],
    }

    try:
        response = HTTP.post(
            HELIUS_RPC_URL,
            json=payload,
            timeout=20,
        )

        if response.status_code != 200:
            return None

        data = response.json()

        if "error" in data:
            print(f"sendTransaction error: {data['error']}")
            return None

        return data.get("result")

    except Exception as exc:
        print(f"transaction error: {exc}")
        return None


def confirm_transaction_rpc(
    signature: str,
    timeout: int = 40
) -> bool:

    started = time.time()

    while time.time() - started < timeout:

        result, error = rpc_call(
            "getSignatureStatuses",
            [
                [signature],
                {"searchTransactionHistory": True},
            ],
        )

        if not error and result:
            statuses = result.get("value", [])

            if statuses and statuses[0] is not None:
                status = statuses[0]

                if status.get("err") is not None:
                    return False

                if status.get("confirmationStatus") in (
                    "confirmed",
                    "finalized",
                ):
                    return True

        time.sleep(2)

    return False


# ================================================================
# DEXSCREENER DISCOVERY
# ================================================================

def dex_pairs_for_tokens(mints: List[str]) -> List[Dict]:
    """
    DEX Screener permits multiple comma-separated token addresses
    in this endpoint, up to 30 at a time.
    """

    if not mints:
        return []

    out = []

    for start in range(0, len(mints), 30):

        batch = mints[start:start + 30]

        url = (
            f"{DEXSCREENER_API}/tokens/v1/solana/"
            + ",".join(batch)
        )

        data, error = http_get(url)

        if error or not isinstance(data, list):
            continue

        out.extend(data)

    return out


def normalize_pair(pair: Dict, source: str) -> Optional[Dict]:

    if pair.get("chainId") != "solana":
        return None

    base = pair.get("baseToken") or {}
    mint = base.get("address")

    if not mint:
        return None

    liquidity = float(
        (pair.get("liquidity") or {}).get("usd") or 0
    )

    volume_24h = float(
        (pair.get("volume") or {}).get("h24") or 0
    )

    txns_24h = pair.get("txns", {}).get("h24", {}) or {}

    buys = int(txns_24h.get("buys") or 0)
    sells = int(txns_24h.get("sells") or 0)

    pair_created = pair.get("pairCreatedAt")

    age_minutes = None

    if pair_created:
        try:
            age_minutes = max(
                0,
                (time.time() * 1000 - float(pair_created))
                / 60000
            )
        except Exception:
            pass

    return {
        "mint": mint,
        "symbol": base.get("symbol") or "UNKNOWN",
        "name": base.get("name") or "",
        "source": source,

        "liquidity": liquidity,
        "volume_24h": volume_24h,

        "buys_24h": buys,
        "sells_24h": sells,

        "pair_address": pair.get("pairAddress", ""),
        "dex": pair.get("dexId", ""),
        "url": pair.get("url", ""),

        "price_usd": float(pair.get("priceUsd") or 0),
        "fdv": float(pair.get("fdv") or 0),

        "pair_created_at": pair_created,
        "age_minutes": age_minutes,

        "boosts": int(
            (pair.get("boosts") or {}).get("active") or 0
        ),

        "market_cap": float(
            pair.get("marketCap") or 0
        ),

        "raw": pair,
    }


def get_dexscreener_tokens(
    limit: int = 50
) -> Tuple[List[Dict], List[str]]:

    candidates = []
    errors = []

    # ------------------------------------------------------------
    # 1. Latest boosts
    # ------------------------------------------------------------

    data, error = http_get(
        f"{DEXSCREENER_API}/token-boosts/latest/v1"
    )

    if error:
        errors.append(f"DS boosts: {error}")

    else:
        mints = [
            x.get("tokenAddress")
            for x in (data or [])
            if x.get("chainId") == "solana"
            and x.get("tokenAddress")
        ]

        pairs = dex_pairs_for_tokens(mints[:30])

        for pair in pairs:
            item = normalize_pair(pair, "dex_boost")
            if item:
                candidates.append(item)

    # ------------------------------------------------------------
    # 2. Latest token profiles
    # ------------------------------------------------------------

    data, error = http_get(
        f"{DEXSCREENER_API}/token-profiles/latest/v1"
    )

    if error:
        errors.append(f"DS profiles: {error}")

    else:
        mints = [
            x.get("tokenAddress")
            for x in (data or [])
            if x.get("chainId") == "solana"
            and x.get("tokenAddress")
        ]

        pairs = dex_pairs_for_tokens(mints[:30])

        for pair in pairs:
            item = normalize_pair(pair, "dex_profile")
            if item:
                candidates.append(item)

    # ------------------------------------------------------------
    # 3. Top boosts
    # ------------------------------------------------------------

    data, error = http_get(
        f"{DEXSCREENER_API}/token-boosts/top/v1"
    )

    if error:
        errors.append(f"DS top boosts: {error}")

    else:
        mints = [
            x.get("tokenAddress")
            for x in (data or [])
            if x.get("chainId") == "solana"
            and x.get("tokenAddress")
        ]

        pairs = dex_pairs_for_tokens(mints[:30])

        for pair in pairs:
            item = normalize_pair(pair, "dex_top_boost")
            if item:
                candidates.append(item)

    return candidates[:limit], errors


# ================================================================
# BIRDEYE DISCOVERY
# ================================================================

def birdeye_headers() -> Dict:
    return {
        "X-API-KEY": BIRDEYE_KEY,
        "x-chain": "solana",
    }


def get_birdeye_new_tokens(
    limit: int = 20
) -> Tuple[List[Dict], List[str]]:

    if not BIRDEYE_KEY:
        return [], ["Birdeye: BIRDEYE_API_KEY not configured"]

    data, error = http_get(
        f"{BIRDEYE_API}/defi/v2/tokens/new_listing",
        params={
            "limit": min(limit, 20),
            "meme_platform_enabled": "true",
        },
        headers=birdeye_headers(),
    )

    if error:
        return [], [f"Birdeye new listings: {error}"]

    raw_items = []

    if isinstance(data, dict):
        payload = data.get("data")

        if isinstance(payload, dict):
            raw_items = (
                payload.get("items")
                or payload.get("tokens")
                or []
            )
        elif isinstance(payload, list):
            raw_items = payload

    out = []

    for token in raw_items:

        mint = (
            token.get("address")
            or token.get("tokenAddress")
            or token.get("address_id")
        )

        if not mint:
            continue

        out.append({
            "mint": mint,
            "symbol": token.get("symbol") or "UNKNOWN",
            "name": token.get("name") or "",
            "source": "birdeye_new",
            "liquidity": float(
                token.get("liquidity") or 0
            ),
            "volume_24h": float(
                token.get("volume24hUSD")
                or token.get("volume24h")
                or 0
            ),
            "buys_24h": int(
                token.get("buy24h")
                or token.get("buys24h")
                or 0
            ),
            "sells_24h": int(
                token.get("sell24h")
                or token.get("sells24h")
                or 0
            ),
            "pair_address": "",
            "dex": "",
            "url": "",
            "price_usd": float(
                token.get("price") or 0
            ),
            "fdv": float(
                token.get("fdv") or 0
            ),
            "pair_created_at": None,
            "age_minutes": None,
            "boosts": 0,
            "market_cap": float(
                token.get("marketcap")
                or token.get("marketCap")
                or 0
            ),
            "raw": token,
        })

    return out, []


def get_birdeye_ranked_tokens(
    limit: int = 50
) -> Tuple[List[Dict], List[str]]:

    if not BIRDEYE_KEY:
        return [], ["Birdeye ranked: BIRDEYE_API_KEY not configured"]

    data, error = http_get(
        f"{BIRDEYE_API}/defi/v3/token/list",
        params={
            "sort_by": "volume_24h_usd",
            "sort_type": "desc",
            "limit": min(limit, 100),
            "min_liquidity": 1000,
        },
        headers=birdeye_headers(),
    )

    if error:
        return [], [f"Birdeye ranked: {error}"]

    raw_items = []

    if isinstance(data, dict):
        payload = data.get("data")

        if isinstance(payload, dict):
            raw_items = payload.get("items") or []
        elif isinstance(payload, list):
            raw_items = payload

    out = []

    for token in raw_items:

        mint = (
            token.get("address")
            or token.get("tokenAddress")
        )

        if not mint:
            continue

        out.append({
            "mint": mint,
            "symbol": token.get("symbol") or "UNKNOWN",
            "name": token.get("name") or "",
            "source": "birdeye_ranked",

            "liquidity": float(
                token.get("liquidity")
                or token.get("liquidity_usd")
                or 0
            ),

            "volume_24h": float(
                token.get("volume24hUSD")
                or token.get("volume_24h_usd")
                or 0
            ),

            "buys_24h": int(
                token.get("buy24h")
                or 0
            ),

            "sells_24h": int(
                token.get("sell24h")
                or 0
            ),

            "pair_address": "",
            "dex": "",
            "url": "",

            "price_usd": float(
                token.get("price")
                or 0
            ),

            "fdv": float(
                token.get("fdv")
                or 0
            ),

            "pair_created_at": None,
            "age_minutes": None,
            "boosts": 0,

            "market_cap": float(
                token.get("marketcap")
                or token.get("market_cap")
                or 0
            ),

            "raw": token,
        })

    return out, []


# ================================================================
# MERGE / ENRICHMENT
# ================================================================

def merge_candidates(
    lists: List[List[Dict]]
) -> List[Dict]:

    merged = {}

    for items in lists:

        for item in items:

            mint = item.get("mint")

            if not mint:
                continue

            if mint not in merged:
                merged[mint] = dict(item)

                merged[mint]["sources"] = [
                    item.get("source", "unknown")
                ]

            else:
                existing = merged[mint]

                existing["sources"].append(
                    item.get("source", "unknown")
                )

                existing["liquidity"] = max(
                    existing.get("liquidity", 0),
                    item.get("liquidity", 0),
                )

                existing["volume_24h"] = max(
                    existing.get("volume_24h", 0),
                    item.get("volume_24h", 0),
                )

                existing["buys_24h"] = max(
                    existing.get("buys_24h", 0),
                    item.get("buys_24h", 0),
                )

                existing["sells_24h"] = max(
                    existing.get("sells_24h", 0),
                    item.get("sells_24h", 0),
                )

                existing["boosts"] = max(
                    existing.get("boosts", 0),
                    item.get("boosts", 0),
                )

                if not existing.get("symbol"):
                    existing["symbol"] = item.get("symbol")

                if not existing.get("name"):
                    existing["name"] = item.get("name")

                if not existing.get("url"):
                    existing["url"] = item.get("url")

    return list(merged.values())


def enrich_with_dex(
    candidates: List[Dict]
) -> List[Dict]:

    mints = [x["mint"] for x in candidates[:100]]

    pairs = dex_pairs_for_tokens(mints)

    best = {}

    for pair in pairs:

        item = normalize_pair(pair, "dex_enrichment")

        if not item:
            continue

        mint = item["mint"]

        old = best.get(mint)

        if old is None:
            best[mint] = item
        else:
            if item["liquidity"] > old["liquidity"]:
                best[mint] = item

    for candidate in candidates:

        dex = best.get(candidate["mint"])

        if not dex:
            continue

        candidate["liquidity"] = max(
            candidate.get("liquidity", 0),
            dex.get("liquidity", 0),
        )

        candidate["volume_24h"] = max(
            candidate.get("volume_24h", 0),
            dex.get("volume_24h", 0),
        )

        candidate["buys_24h"] = max(
            candidate.get("buys_24h", 0),
            dex.get("buys_24h", 0),
        )

        candidate["sells_24h"] = max(
            candidate.get("sells_24h", 0),
            dex.get("sells_24h", 0),
        )

        candidate["age_minutes"] = dex.get(
            "age_minutes"
        )

        candidate["pair_address"] = dex.get(
            "pair_address"
        )

        candidate["dex"] = dex.get("dex")
        candidate["url"] = dex.get("url")

        candidate["price_usd"] = dex.get(
            "price_usd",
            candidate.get("price_usd", 0)
        )

        candidate["boosts"] = max(
            candidate.get("boosts", 0),
            dex.get("boosts", 0),
        )

    return candidates


# ================================================================
# TOKEN SCORING
# ================================================================

def score_token(
    token: Dict,
    cfg: Dict
) -> Tuple[int, List[str]]:

    score = 0
    reasons = []

    liquidity = float(
        token.get("liquidity", 0) or 0
    )

    volume = float(
        token.get("volume_24h", 0) or 0
    )

    buys = int(
        token.get("buys_24h", 0) or 0
    )

    sells = int(
        token.get("sells_24h", 0) or 0
    )

    boosts = int(
        token.get("boosts", 0) or 0
    )

    age = token.get("age_minutes")

    # ------------------------------------------------------------
    # Liquidity
    # ------------------------------------------------------------

    if liquidity >= cfg["min_liquidity_usd"]:
        score += 25
        reasons.append("liquidity OK")

    elif liquidity >= cfg["min_liquidity_usd"] * 0.5:
        score += 10
        reasons.append("medium liquidity")

    # ------------------------------------------------------------
    # Volume
    # ------------------------------------------------------------

    if volume >= cfg["min_volume_24h"]:
        score += 20
        reasons.append("volume OK")

    elif volume >= cfg["min_volume_24h"] * 0.5:
        score += 8
        reasons.append("medium volume")

    # ------------------------------------------------------------
    # Buy pressure
    # ------------------------------------------------------------

    total_tx = buys + sells

    if total_tx > 0:

        buy_ratio = buys / total_tx

        if buy_ratio >= 0.65:
            score += 15
            reasons.append("buy pressure")

        elif buy_ratio >= 0.52:
            score += 7
            reasons.append("slight buy pressure")

    # ------------------------------------------------------------
    # Multi-source confirmation
    # ------------------------------------------------------------

    sources = set(token.get("sources", []))

    if len(sources) >= 2:
        score += 15
        reasons.append("multi-source")

    # ------------------------------------------------------------
    # DEX presence
    # ------------------------------------------------------------

    if token.get("pair_address"):
        score += 10
        reasons.append("active pair")

    # ------------------------------------------------------------
    # Boost signal
    # ------------------------------------------------------------

    if boosts > 0:
        score += min(10, boosts)
        reasons.append("boost activity")

    # ------------------------------------------------------------
    # Age
    # ------------------------------------------------------------

    if age is not None:

        if age >= cfg["min_token_age_minutes"]:
            score += 5
            reasons.append("age acceptable")

        if age > cfg["max_token_age_hours"] * 60:
            score -= 15
            reasons.append("older token")

    return max(0, min(100, score)), reasons


def apply_filters(
    candidates: List[Dict],
    cfg: Dict
) -> Tuple[List[Dict], List[Dict]]:

    accepted = []
    rejected = []

    for token in candidates:

        liquidity = float(
            token.get("liquidity", 0) or 0
        )

        volume = float(
            token.get("volume_24h", 0) or 0
        )

        age = token.get("age_minutes")

        reasons = []

        if liquidity < cfg["min_liquidity_usd"]:
            reasons.append(
                f"liq ${liquidity:,.0f} < "
                f"${cfg['min_liquidity_usd']:,.0f}"
            )

        if volume < cfg["min_volume_24h"]:
            reasons.append(
                f"vol ${volume:,.0f} < "
                f"${cfg['min_volume_24h']:,.0f}"
            )

        if age is not None:

            if age < cfg["min_token_age_minutes"]:
                reasons.append(
                    f"too new ({age:.0f}m)"
                )

            if age > cfg["max_token_age_hours"] * 60:
                reasons.append(
                    f"too old ({age / 60:.1f}h)"
                )

        score, score_reasons = score_token(
            token,
            cfg
        )

        token["score"] = score
        token["score_reasons"] = score_reasons

        if score < cfg["min_score"]:
            reasons.append(
                f"score {score} < {cfg['min_score']}"
            )

        if reasons:
            token["reject_reason"] = " | ".join(reasons)
            rejected.append(token)
        else:
            token["reject_reason"] = ""
            accepted.append(token)

    accepted.sort(
        key=lambda x: (
            x.get("score", 0),
            x.get("liquidity", 0),
            x.get("volume_24h", 0),
        ),
        reverse=True,
    )

    rejected.sort(
        key=lambda x: x.get("score", 0),
        reverse=True,
    )

    return accepted, rejected


# ================================================================
# RUGCHECK
# ================================================================

def get_rugcheck_report(
    mint: str
) -> Optional[Dict]:

    data, error = http_get(
        f"{RUGCHECK_API}/tokens/{mint}/report",
        timeout=10,
    )

    if error:
        return None

    return data


def safety_check(
    mint: str,
    token: Dict,
    cfg: Dict
) -> Tuple[bool, str]:

    liquidity = float(
        token.get("liquidity", 0) or 0
    )

    if liquidity < cfg["min_liquidity_usd"]:
        return False, "liquidity below minimum"

    # RugCheck is optional because APIs can rate-limit or fail.
    if not cfg["require_rugcheck"]:
        return True, "basic filters passed"

    report = get_rugcheck_report(mint)

    if not report:
        return False, "RugCheck unavailable"

    mint_authority = report.get("mintAuthority")

    freeze_authority = report.get("freezeAuthority")

    if mint_authority not in (None, ""):
        return False, "mint authority active"

    if freeze_authority not in (None, ""):
        return False, "freeze authority active"

    top_holders = report.get("topHolders") or []

    top10_pct = sum(
        float(holder.get("pct", 0) or 0)
        for holder in top_holders[:10]
    )

    if top10_pct > cfg["max_top10_pct"]:
        return False, (
            f"top10 holders {top10_pct:.1f}%"
        )

    return True, "RugCheck passed"


# ================================================================
# JUPITER
# ================================================================

def jupiter_quote(
    input_mint: str,
    output_mint: str,
    amount: int,
    slippage_bps: int = 500,
) -> Optional[Dict]:

    try:

        response = HTTP.get(
            f"{JUPITER_API_BASE}/quote",
            params={
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": amount,
                "slippageBps": slippage_bps,
            },
            timeout=15,
        )

        if response.status_code != 200:
            print(
                f"Jupiter quote HTTP "
                f"{response.status_code}: "
                f"{response.text[:250]}"
            )
            return None

        return response.json()

    except Exception as exc:
        print(f"Jupiter quote error: {exc}")
        return None


def simulate_sell_check(
    mint: str,
    test_amount: int = 1000
) -> bool:

    quote = jupiter_quote(
        mint,
        SOL_MINT,
        test_amount,
        slippage_bps=1000,
    )

    if not quote:
        return False

    try:
        return int(
            quote.get("outAmount", 0)
        ) > 0
    except Exception:
        return False


def jupiter_swap(
    quote: Dict,
    wallet: Keypair
) -> Optional[str]:

    try:

        payload = {
            "quoteResponse": quote,
            "userPublicKey": str(wallet.pubkey()),
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": "auto",
        }

        response = HTTP.post(
            f"{JUPITER_API_BASE}/swap",
            json=payload,
            timeout=25,
        )

        if response.status_code != 200:
            print(
                "Jupiter swap build failed:",
                response.text[:500]
            )
            return None

        body = response.json()

        raw = base64.b64decode(
            body["swapTransaction"]
        )

        unsigned = VersionedTransaction.from_bytes(raw)

        signed = VersionedTransaction(
            unsigned.message,
            [wallet],
        )

        signature = send_raw_transaction_rpc(
            bytes(signed)
        )

        if not signature:
            return None

        if not confirm_transaction_rpc(signature):
            print(
                f"Transaction not confirmed: {signature}"
            )
            return None

        return signature

    except Exception as exc:
        print(f"Jupiter swap error: {exc}")
        return None


# ================================================================
# BUY / SELL
# ================================================================

def do_buy(
    state: EngineState,
    token: Dict,
    sol_amount: float
) -> Optional[Dict]:

    mint = token["mint"]
    symbol = token.get("symbol", "UNKNOWN")

    lamports = int(
        sol_amount * 1_000_000_000
    )

    quote = jupiter_quote(
        SOL_MINT,
        mint,
        lamports,
    )

    if not quote:
        return None

    out_amount = int(
        quote.get("outAmount", 0)
    )

    if out_amount <= 0:
        return None

    if state.paper_mode:

        signature = "PAPER"

    else:

        if state.wallet is None:
            return None

        signature = jupiter_swap(
            quote,
            state.wallet
        )

        if not signature:
            return None

    return {
        "mint": mint,
        "symbol": symbol,
        "entry_sol": sol_amount,
        "out_amount": out_amount,
        "opened_at": datetime.now().isoformat(),
        "buy_sig": signature,
        "peak_pnl_pct": 0.0,
        "source": token.get("sources", []),
        "score": token.get("score", 0),
    }


def do_sell(
    state: EngineState,
    position: Dict
) -> Optional[Dict]:

    if state.paper_mode:

        pnl_pct = random.uniform(-30, 60)

        profit = (
            position["entry_sol"]
            * pnl_pct
            / 100
        )

        exit_sol = (
            position["entry_sol"]
            + profit
        )

        signature = "PAPER"

    else:

        if state.wallet is None:
            return None

        balance, _ = get_token_balance(
            state.wallet_address,
            position["mint"],
        )

        if balance <= 0:
            return None

        quote = jupiter_quote(
            position["mint"],
            SOL_MINT,
            balance,
            slippage_bps=1000,
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
            int(quote.get("outAmount", 0))
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


def get_current_pnl_pct(
    state: EngineState,
    position: Dict
) -> Optional[float]:

    if state.paper_mode:
        return None

    amount = int(
        position.get("out_amount", 0)
    )

    if amount <= 0:
        return None

    quote = jupiter_quote(
        position["mint"],
        SOL_MINT,
        amount,
        slippage_bps=1000,
    )

    if not quote:
        return None

    try:
        current_sol = (
            int(quote.get("outAmount", 0))
            / 1_000_000_000
        )

        entry = float(
            position.get("entry_sol", 0)
        )

        if entry <= 0:
            return None

        return (
            (current_sol - entry)
            / entry
            * 100
        )

    except Exception:
        return None


# ================================================================
# PNL
# ================================================================

def daily_pnl(state: EngineState) -> float:

    today = datetime.now().strftime(
        "%Y-%m-%d"
    )

    with state.lock:
        return sum(
            float(t.get("profit", 0))
            for t in state.trades
            if t.get("date") == today
        )


# ================================================================
# DISCOVERY ENGINE
# ================================================================

def discover_tokens(
    state: EngineState
) -> List[Dict]:

    cfg = dict(state.config)

    all_lists = []
    errors = []

    if cfg["use_dexscreener"]:

        items, errs = get_dexscreener_tokens()

        all_lists.append(items)
        errors.extend(errs)

    if cfg["use_birdeye_new"]:

        items, errs = get_birdeye_new_tokens()

        all_lists.append(items)
        errors.extend(errs)

    if cfg["use_birdeye_ranked"]:

        items, errs = get_birdeye_ranked_tokens()

        all_lists.append(items)
        errors.extend(errs)

    candidates = merge_candidates(
        all_lists
    )

    if candidates:

        candidates = enrich_with_dex(
            candidates
        )

    accepted, rejected = apply_filters(
        candidates,
        cfg
    )

    # ------------------------------------------------------------
    # Store diagnostics
    # ------------------------------------------------------------

    with state.lock:

        state.last_scan = datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        state.last_scan_count = len(
            candidates
        )

        state.scan_errors = errors[:20]

        # Show accepted first, then rejected candidates.
        state.discovered = (
            accepted[:50]
            + rejected[:50]
        )

    state.log(
        f"SCAN: {len(candidates)} unique candidates / "
        f"{len(accepted)} passed filters",
        "sys",
    )

    for error in errors[:5]:
        state.log(
            f"DATA: {error}",
            "sell-loss",
        )

    return accepted


# ================================================================
# ENGINE
# ================================================================

def engine_loop(
    state: EngineState
):

    state.log(
        "Background engine thread started",
        "sys",
    )

    while True:

        try:

            if not state.running:
                time.sleep(2)
                continue

            # ----------------------------------------------------
            # Daily loss kill switch
            # ----------------------------------------------------

            pnl = daily_pnl(state)

            if pnl <= -abs(
                state.config["daily_loss_limit"]
            ):

                state.log(
                    "DAILY LOSS LIMIT HIT — ENGINE STOPPED",
                    "sell-loss",
                )

                with state.lock:
                    state.running = False

                continue

            # ----------------------------------------------------
            # Manage positions
            # ----------------------------------------------------

            with state.lock:
                positions = list(
                    state.positions
                )
                cfg = dict(state.config)

            for position in positions:

                pnl_pct = get_current_pnl_pct(
                    state,
                    position,
                )

                if state.paper_mode:

                    pnl_pct = (
                        position.get(
                            "paper_pnl_pct",
                            0
                        )
                        + random.uniform(
                            -8,
                            12
                        )
                    )

                    with state.lock:
                        position["paper_pnl_pct"] = (
                            pnl_pct
                        )

                if pnl_pct is None:
                    continue

                with state.lock:

                    position["peak_pnl_pct"] = max(
                        position.get(
                            "peak_pnl_pct",
                            0
                        ),
                        pnl_pct,
                    )

                    peak = position[
                        "peak_pnl_pct"
                    ]

                trailing_trigger = (
                    peak
                    - cfg["trailing_stop_pct"]
                )

                should_close = False
                reason = ""

                if (
                    pnl_pct
                    >= cfg["take_profit_pct"]
                ):

                    should_close = True
                    reason = "take profit"

                elif (
                    peak > 0
                    and pnl_pct <= trailing_trigger
                ):

                    should_close = True
                    reason = "trailing stop"

                elif (
                    peak <= 0
                    and pnl_pct
                    <= -cfg["trailing_stop_pct"]
                ):

                    should_close = True
                    reason = "stop loss"

                if should_close:

                    trade = do_sell(
                        state,
                        position,
                    )

                    if trade:

                        with state.lock:

                            if (
                                position
                                in state.positions
                            ):
                                state.positions.remove(
                                    position
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
                            f"CLOSED "
                            f"{position['symbol']} "
                            f"({reason}) "
                            f"{trade['profit']:+.4f} SOL",
                            tag,
                        )

                        save_persisted(state)

            # ----------------------------------------------------
            # Discovery
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

                accepted = discover_tokens(
                    state
                )

                for token in accepted:

                    with state.lock:

                        if (
                            len(state.positions)
                            >= cfg["max_positions"]
                        ):
                            break

                        already_holding = any(
                            p["mint"]
                            == token["mint"]
                            for p
                            in state.positions
                        )

                    if already_holding:
                        continue

                    # ------------------------------------------------
                    # Safety
                    # ------------------------------------------------

                    ok, reason = safety_check(
                        token["mint"],
                        token,
                        cfg,
                    )

                    if not ok:

                        state.log(
                            f"SKIP "
                            f"{token.get('symbol')} "
                            f"{reason}",
                            "sys",
                        )

                        continue

                    # ------------------------------------------------
                    # Optional sell simulation
                    # ------------------------------------------------

                    if cfg[
                        "require_sell_quote"
                    ]:

                        if not simulate_sell_check(
                            token["mint"]
                        ):

                            state.log(
                                f"SKIP "
                                f"{token.get('symbol')}: "
                                f"no sell route",
                                "sell-loss",
                            )

                            continue

                    # ------------------------------------------------
                    # Buy
                    # ------------------------------------------------

                    position = do_buy(
                        state,
                        token,
                        cfg["snipe_amount"],
                    )

                    if position:

                        with state.lock:
                            state.positions.append(
                                position
                            )

                        state.log(
                            f"BOUGHT "
                            f"{token.get('symbol')} "
                            f"score={token.get('score', 0)} "
                            f"for "
                            f"{cfg['snipe_amount']} SOL",
                            "buy",
                        )

                        save_persisted(
                            state
                        )

                        # Don't fill every position from
                        # one scan immediately.
                        time.sleep(1)

            time.sleep(
                SCAN_INTERVAL_SECONDS
            )

        except Exception as exc:

            state.log(
                f"ENGINE ERROR: {exc}",
                "sell-loss",
            )

            print(
                traceback.format_exc()
            )

            time.sleep(5)


# ================================================================
# IMPORTANT STREAMLIT FIX
# ================================================================

@st.cache_resource
def start_engine(_state: EngineState):
    """
    CRITICAL FIX.

    The argument is named _state so Streamlit does NOT try to hash
    EngineState. EngineState contains threading.Lock and mutable
    objects that Streamlit cannot safely hash/deep-copy.

    This is the fix for:

        TypeError
        dataclasses.asdict(...)
        copy.deepcopy(...)
    """

    thread = threading.Thread(
        target=engine_loop,
        args=(_state,),
        daemon=True,
        name="cyber-sniper-engine",
    )

    thread.start()

    return thread


# ================================================================
# UI CONFIG
# ================================================================

st.set_page_config(
    page_title="CYBER SNIPER // NEON PROTOCOL",
    page_icon="🟣",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ================================================================
# CYBERPUNK CSS
# ================================================================

st.markdown(
    """
<style>

@import url(
'https://fonts.googleapis.com/css2?family=Orbitron:wght@400;600;700;900&family=Share+Tech+Mono&display=swap'
);

:root {
    --cyan:#00ffe7;
    --pink:#ff00ea;
    --green:#00ff88;
    --red:#ff245f;
    --purple:#7a00ff;
    --bg:#03020a;
}

.stApp {
    background:
        radial-gradient(
            circle at 20% 20%,
            rgba(255,0,230,.10),
            transparent 35%
        ),
        radial-gradient(
            circle at 80% 70%,
            rgba(0,255,255,.08),
            transparent 40%
        ),
        #03020a;
    color:#dffffb;
}

/* animated grid */

.stApp::before {
    content:"";
    position:fixed;
    inset:0;
    pointer-events:none;
    z-index:-3;

    background:
        linear-gradient(
            rgba(0,255,231,.035) 1px,
            transparent 1px
        ),
        linear-gradient(
            90deg,
            rgba(0,255,231,.035) 1px,
            transparent 1px
        );

    background-size:32px 32px;

    animation:gridMove 18s linear infinite;
}

@keyframes gridMove {
    from {
        background-position:0 0,0 0;
    }
    to {
        background-position:0 640px,640px 0;
    }
}

/* scanlines */

.stApp::after {
    content:"";
    position:fixed;
    inset:0;
    pointer-events:none;
    z-index:-2;

    background:
        repeating-linear-gradient(
            0deg,
            rgba(0,0,0,.12) 0px,
            rgba(0,0,0,.12) 1px,
            transparent 2px,
            transparent 4px
        );
}

/* header */

.cyber-header {
    padding:28px;
    margin-bottom:22px;

    border:2px solid var(--pink);
    border-radius:14px;

    background:
        linear-gradient(
            135deg,
            rgba(5,1,15,.95),
            rgba(25,0,45,.95)
        );

    box-shadow:
        0 0 15px rgba(255,0,230,.4),
        inset 0 0 40px rgba(0,255,231,.05);

    text-align:center;

    animation:
        pulseBorder 3s ease-in-out infinite;
}

@keyframes pulseBorder {
    0%,100% {
        box-shadow:
            0 0 15px rgba(255,0,230,.35),
            inset 0 0 40px rgba(0,255,231,.05);
    }
    50% {
        box-shadow:
            0 0 35px rgba(255,0,230,.65),
            0 0 60px rgba(0,255,231,.12),
            inset 0 0 40px rgba(0,255,231,.08);
    }
}

.cyber-title {
    font-family:'Orbitron',sans-serif;
    color:var(--cyan);
    font-size:38px;
    font-weight:900;
    letter-spacing:5px;

    text-shadow:
        0 0 5px #fff,
        0 0 12px var(--cyan),
        0 0 30px var(--pink);

    animation:glitch 4s infinite;
}

@keyframes glitch {
    0%,94%,100% {
        transform:translate(0);
    }
    95% {
        transform:translate(-2px,1px);
    }
    96% {
        transform:translate(2px,-1px);
    }
    97% {
        transform:translate(-1px,0);
    }
}

.sub {
    color:var(--pink);
    font-family:'Share Tech Mono',monospace;
    letter-spacing:3px;
}

/* cards */

.cyber-card {
    background:
        linear-gradient(
            135deg,
            rgba(5,2,15,.96),
            rgba(18,0,40,.88)
        );

    border:1px solid var(--cyan);
    border-radius:10px;

    padding:17px;
    margin:4px;

    box-shadow:
        0 0 15px rgba(0,255,231,.15);

    transition:
        transform .2s,
        box-shadow .2s,
        border-color .2s;
}

.cyber-card:hover {
    transform:translateY(-3px);

    border-color:var(--pink);

    box-shadow:
        0 0 30px rgba(255,0,230,.35);
}

.metric-value {
    color:var(--cyan);
    font-family:'Orbitron',sans-serif;
    font-size:22px;
    font-weight:800;

    text-shadow:
        0 0 10px var(--cyan);
}

/* terminal */

.terminal {
    background:#000;

    border:1px solid var(--green);
    border-radius:8px;

    padding:14px;

    height:260px;
    overflow-y:auto;

    font-family:'Share Tech Mono',monospace;
    font-size:12px;

    box-shadow:
        inset 0 0 25px rgba(0,255,136,.12);
}

.terminal .buy {
    color:var(--cyan);
}

.terminal .sell-win {
    color:var(--green);
}

.terminal .sell-loss {
    color:var(--red);
}

.terminal .sys {
    color:var(--pink);
}

/* sidebar */

section[data-testid="stSidebar"] {
    background:
        linear-gradient(
            180deg,
            #04020b,
            #10001f
        );

    border-right:1px solid var(--pink);
}

.sidebar-header {
    color:var(--cyan);
    font-family:'Orbitron',sans-serif;
    font-size:18px;
    font-weight:900;
    text-align:center;

    padding:8px;

    letter-spacing:2px;

    text-shadow:
        0 0 10px var(--cyan);
}

/* buttons */

.stButton > button {
    background:
        linear-gradient(
            135deg,
            #05010d,
            #16002c
        );

    color:var(--cyan);

    border:1px solid var(--cyan);
    border-radius:7px;

    font-family:'Orbitron',sans-serif;
    font-weight:700;

    transition:.2s;
}

.stButton > button:hover {
    color:#000;

    background:
        linear-gradient(
            135deg,
            var(--pink),
            var(--cyan)
        );

    box-shadow:
        0 0 25px rgba(255,0,230,.55);
}

/* tables */

[data-testid="stDataFrame"] {
    border:1px solid var(--cyan);
}

/* alerts */

.stAlert {
    background:#0d0020;
    border:1px solid var(--pink);
}

/* status */

.online {
    color:var(--green);
    text-shadow:0 0 20px var(--green);
}

.offline {
    color:var(--red);
    text-shadow:0 0 20px var(--red);
}

</style>
""",
    unsafe_allow_html=True,
)


# ================================================================
# INITIALIZE
# ================================================================

state = get_state()

# This is safe now because start_engine uses _state.
start_engine(state)


# ================================================================
# AUTO LOAD WALLET FROM ENV
# ================================================================

ENV_PRIVATE_KEY = os.getenv(
    "SOLANA_PRIVATE_KEY",
    ""
).strip()

if (
    ENV_PRIVATE_KEY
    and state.wallet is None
):

    try:

        with state.lock:

            state.wallet = (
                Keypair.from_base58_string(
                    ENV_PRIVATE_KEY
                )
            )

            state.wallet_address = str(
                state.wallet.pubkey()
            )

        state.log(
            "Wallet loaded from environment",
            "sys",
        )

    except Exception as exc:

        state.log(
            f"Wallet load failed: {exc}",
            "sell-loss",
        )


# ================================================================
# HEADER
# ================================================================

st.markdown(
    """
<div class="cyber-header">

<div class="cyber-title">
🟣 CYBER SNIPER
</div>

<div class="sub">
// NEON PROTOCOL //
MULTI-SOURCE SOLANA DISCOVERY //
PAPER-FIRST EXECUTION
</div>

</div>
""",
    unsafe_allow_html=True,
)


# ================================================================
# SIDEBAR
# ================================================================

with st.sidebar:

    st.markdown(
        '<div class="sidebar-header">'
        '🔑 WALLET ACCESS'
        '</div>',
        unsafe_allow_html=True,
    )

    if state.wallet:

        st.success(
            "Wallet connected"
        )

        st.caption(
            f"{state.wallet_address[:8]}"
            "..."
            f"{state.wallet_address[-6:]}"
        )

    else:

        key_input = st.text_input(
            "Private key",
            type="password",
            help=(
                "Prefer SOLANA_PRIVATE_KEY "
                "in environment/secrets."
            ),
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

                with state.lock:

                    state.wallet = wallet
                    state.wallet_address = str(
                        wallet.pubkey()
                    )

                st.success(
                    "Wallet connected"
                )

                st.rerun()

            except Exception as exc:

                st.error(
                    f"Invalid key: {exc}"
                )

    st.markdown("---")

    # ------------------------------------------------------------
    # Trading mode
    # ------------------------------------------------------------

    st.markdown(
        '<div class="sidebar-header">'
        '⚠️ TRADING MODE'
        '</div>',
        unsafe_allow_html=True,
    )

    paper = st.toggle(
        "PAPER MODE",
        value=state.paper_mode,
    )

    if paper != state.paper_mode:

        with state.lock:
            state.paper_mode = paper

    if not state.paper_mode:

        st.warning(
            "LIVE MODE can submit real transactions."
        )

        confirmation = st.text_input(
            'Type "I ACCEPT RISK"',
            type="password",
        )

        live_unlocked = (
            confirmation.strip().upper()
            == "I ACCEPT RISK"
        )

    else:

        live_unlocked = False

    st.markdown("---")

    # ------------------------------------------------------------
    # Strategy
    # ------------------------------------------------------------

    st.markdown(
        '<div class="sidebar-header">'
        '🎯 STRATEGY'
        '</div>',
        unsafe_allow_html=True,
    )

    preset_name = st.selectbox(
        "Preset",
        [
            "Conservative",
            "Moderate",
            "Aggressive",
        ],
        index=1,
    )

    presets = {

        "Conservative": {
            "amount": 0.02,
            "tp": 20,
            "trail": 8,
            "liq": 25000,
            "vol": 10000,
            "score": 65,
        },

        "Moderate": {
            "amount": 0.05,
            "tp": 50,
            "trail": 15,
            "liq": 15000,
            "vol": 5000,
            "score": 50,
        },

        "Aggressive": {
            "amount": 0.10,
            "tp": 100,
            "trail": 25,
            "liq": 8000,
            "vol": 2500,
            "score": 40,
        },
    }

    preset = presets[preset_name]

    snipe_amount = st.slider(
        "Buy Amount SOL",
        0.01,
        MAX_TRADE_SOL_CAP,
        float(preset["amount"]),
        0.01,
    )

    take_profit = st.slider(
        "Take Profit %",
        10,
        300,
        int(preset["tp"]),
        5,
    )

    trailing_stop = st.slider(
        "Trailing Stop %",
        5,
        50,
        int(preset["trail"]),
        5,
    )

    min_liquidity = st.number_input(
        "Minimum Liquidity USD",
        1000,
        200000,
        int(preset["liq"]),
        1000,
    )

    min_volume = st.number_input(
        "Minimum 24h Volume USD",
        0,
        1000000,
        int(preset["vol"]),
        500,
    )

    min_score = st.slider(
        "Minimum Token Score",
        0,
        100,
        int(preset["score"]),
        5,
    )

    min_age = st.number_input(
        "Minimum Token Age Minutes",
        0,
        10080,
        0,
        5,
    )

    max_age_hours = st.number_input(
        "Maximum Token Age Hours",
        1,
        720,
        168,
        1,
    )

    max_positions = st.slider(
        "Maximum Open Positions",
        1,
        10,
        5,
    )

    daily_loss_limit = st.number_input(
        "Daily Loss Kill Switch SOL",
        0.01,
        10.0,
        0.20,
        0.01,
    )

    with state.lock:

        state.config.update({

            "snipe_amount":
                snipe_amount,

            "take_profit_pct":
                float(take_profit),

            "trailing_stop_pct":
                float(trailing_stop),

            "min_liquidity_usd":
                float(min_liquidity),

            "min_volume_24h":
                float(min_volume),

            "min_score":
                int(min_score),

            "min_token_age_minutes":
                int(min_age),

            "max_token_age_hours":
                int(max_age_hours),

            "max_positions":
                int(max_positions),

            "daily_loss_limit":
                float(daily_loss_limit),
        })

    st.markdown("---")

    # ------------------------------------------------------------
    # Sources
    # ------------------------------------------------------------

    st.markdown(
        '<div class="sidebar-header">'
        '📡 DATA SOURCES'
        '</div>',
        unsafe_allow_html=True,
    )

    use_dex = st.checkbox(
        "DEX Screener",
        True,
    )

    use_be_new = st.checkbox(
        "Birdeye New Listings",
        bool(BIRDEYE_KEY),
        disabled=not bool(BIRDEYE_KEY),
    )

    use_be_ranked = st.checkbox(
        "Birdeye Ranked",
        bool(BIRDEYE_KEY),
        disabled=not bool(BIRDEYE_KEY),
    )

    with state.lock:

        state.config[
            "use_dexscreener"
        ] = use_dex

        state.config[
            "use_birdeye_new"
        ] = use_be_new

        state.config[
            "use_birdeye_ranked"
        ] = use_be_ranked

    if not BIRDEYE_KEY:

        st.caption(
            "Birdeye disabled: "
            "set BIRDEYE_API_KEY."
        )

    st.markdown("---")

    # ------------------------------------------------------------
    # Safety
    # ------------------------------------------------------------

    st.markdown(
        '<div class="sidebar-header">'
        '🛡️ SAFETY'
        '</div>',
        unsafe_allow_html=True,
    )

    require_rugcheck = st.checkbox(
        "Require RugCheck",
        False,
    )

    require_sell = st.checkbox(
        "Require Jupiter sell route",
        False,
    )

    with state.lock:

        state.config[
            "require_rugcheck"
        ] = require_rugcheck

        state.config[
            "require_sell_quote"
        ] = require_sell

    st.markdown("---")

    # ------------------------------------------------------------
    # Engine controls
    # ------------------------------------------------------------

    st.markdown(
        '<div class="sidebar-header">'
        '🤖 ENGINE'
        '</div>',
        unsafe_allow_html=True,
    )

    can_start = (
        state.paper_mode
        or (
            state.wallet is not None
            and live_unlocked
        )
    )

    if not state.running:

        if st.button(
            "⚡ START ENGINE",
            disabled=not can_start,
        ):

            with state.lock:

                state.running = True

                state.engine_started_at = (
                    datetime.now().isoformat()
                )

            state.log(
                "ENGINE ACTIVATED",
                "sys",
            )

            st.rerun()

        if not can_start:

            st.caption(
                "Paper mode or live confirmation required."
            )

    else:

        if st.button(
            "🛑 STOP ENGINE"
        ):

            with state.lock:
                state.running = False

            state.log(
                "ENGINE STOPPED",
                "sys",
            )

            st.rerun()

    if st.button(
        "🔍 FORCE SCAN"
    ):

        with st.spinner(
            "Scanning sources..."
        ):

            accepted = discover_tokens(
                state
            )

        st.success(
            f"Scan complete: "
            f"{len(accepted)} passed"
        )

        st.rerun()

    if st.button(
        "🧹 CLEAR DISCOVERY"
    ):

        with state.lock:
            state.discovered = []

        st.rerun()


# ================================================================
# SNAPSHOT
# ================================================================

with state.lock:

    positions = list(
        state.positions
    )

    trades = list(
        state.trades
    )

    discovered = list(
        state.discovered
    )

    logs = list(
        state.logs
    )

    running = state.running

    paper_mode = state.paper_mode

    last_scan = state.last_scan

    scan_count = state.last_scan_count

    scan_errors = list(
        state.scan_errors
    )

    cfg = dict(
        state.config
    )


# ================================================================
# METRICS
# ================================================================

wallet_balance = 0.0

if state.wallet:

    wallet_balance = get_wallet_balance(
        state.wallet_address
    )

total_trades = len(trades)

wins = sum(
    1
    for trade in trades
    if float(
        trade.get("profit", 0)
    ) > 0
)

win_rate = (
    wins / total_trades * 100
    if total_trades
    else 0
)

total_pnl = sum(
    float(
        trade.get("profit", 0)
    )
    for trade in trades
)

today_pnl = daily_pnl(
    state
)

m1, m2, m3, m4, m5 = st.columns(5)

with m1:

    st.markdown(
        f"""
<div class="cyber-card">
<div style="color:#888">WALLET</div>
<div class="metric-value">
{wallet_balance:.4f} SOL
</div>
</div>
""",
        unsafe_allow_html=True,
    )

with m2:

    st.markdown(
        f"""
<div class="cyber-card">
<div style="color:#888">POSITIONS</div>
<div class="metric-value">
{len(positions)}/{cfg['max_positions']}
</div>
</div>
""",
        unsafe_allow_html=True,
    )

with m3:

    st.markdown(
        f"""
<div class="cyber-card">
<div style="color:#888">DISCOVERED</div>
<div class="metric-value">
{scan_count}
</div>
</div>
""",
        unsafe_allow_html=True,
    )

with m4:

    color = (
        "#00ff88"
        if total_pnl >= 0
        else "#ff245f"
    )

    st.markdown(
        f"""
<div class="cyber-card">
<div style="color:#888">NET P/L</div>
<div class="metric-value"
style="color:{color}">
{total_pnl:+.4f} SOL
</div>
</div>
""",
        unsafe_allow_html=True,
    )

with m5:

    st.markdown(
        f"""
<div class="cyber-card">
<div style="color:#888">WIN RATE</div>
<div class="metric-value">
{win_rate:.1f}%
</div>
</div>
""",
        unsafe_allow_html=True,
    )


# ================================================================
# ENGINE STATUS
# ================================================================

status_color = (
    "#00ff88"
    if running
    else "#ff245f"
)

status_text = (
    "ONLINE"
    if running
    else "OFFLINE"
)

mode = (
    "PAPER"
    if paper_mode
    else "LIVE"
)

st.markdown(
    f"""
<div style="
text-align:center;
padding:12px;
margin:12px 0;
border:1px solid {status_color};
border-radius:8px;
background:rgba(0,0,0,.35);
">

<h2 style="
color:{status_color};
font-family:Orbitron;
margin:0;
text-shadow:0 0 18px {status_color};
">
● ENGINE {status_text}
</h2>

<div style="
font-family:'Share Tech Mono';
color:#00ffe7;
margin-top:5px;
">
MODE: {mode}
&nbsp; // &nbsp;
LAST SCAN: {last_scan}
</div>

</div>
""",
    unsafe_allow_html=True,
)


# ================================================================
# SCAN DIAGNOSTICS
# ================================================================

with st.expander(
    "📡 SCAN DIAGNOSTICS",
    expanded=False,
):

    st.write(
        f"Raw unique candidates: "
        f"**{scan_count}**"
    )

    st.write(
        f"Currently displayed: "
        f"**{len(discovered)}**"
    )

    if scan_errors:

        st.warning(
            "Some sources reported errors:"
        )

        for error in scan_errors:
            st.code(error)

    else:

        st.success(
            "No source errors recorded."
        )

    st.caption(
        "If this says 0 candidates, the problem "
        "is upstream discovery rather than the "
        "buy logic."
    )


# ================================================================
# DISCOVERED TOKENS
# ================================================================

st.markdown(
    "### 🔎 DISCOVERED TOKENS"
)

if discovered:

    rows = []

    for token in discovered[:50]:

        rows.append({

            "Score":
                token.get("score", 0),

            "Symbol":
                token.get(
                    "symbol",
                    "UNKNOWN"
                ),

            "Liquidity":
                f"${token.get('liquidity', 0):,.0f}",

            "24h Volume":
                f"${token.get('volume_24h', 0):,.0f}",

            "Buys":
                token.get(
                    "buys_24h",
                    0
                ),

            "Sells":
                token.get(
                    "sells_24h",
                    0
                ),

            "Sources":
                ", ".join(
                    token.get(
                        "sources",
                        []
                    )
                ),

            "Age":
                (
                    f"{token['age_minutes']:.0f}m"
                    if token.get(
                        "age_minutes"
                    ) is not None
                    else "?"
                ),

            "Mint":
                token.get(
                    "mint",
                    ""
                ),
        })

    st.dataframe(
        pd.DataFrame(rows),
        use_container_width=True,
        hide_index=True,
    )

    st.caption(
        "Highest-score candidates appear first."
    )

else:

    st.info(
        "No candidates yet. "
        "Use FORCE SCAN or start the engine."
    )


# ================================================================
# MANUAL BUY
# ================================================================

if discovered:

    st.markdown(
        "### ⚡ MANUAL CANDIDATE ACTION"
    )

    choices = [
        x
        for x in discovered
        if not x.get(
            "reject_reason"
        )
    ]

    if choices:

        labels = [
            f"{x.get('symbol', 'UNKNOWN')} "
            f"// score {x.get('score', 0)} "
            f"// {x.get('mint', '')[:8]}..."
            for x in choices[:20]
        ]

        selected_label = st.selectbox(
            "Candidate",
            labels,
        )

        selected_index = labels.index(
            selected_label
        )

        selected = choices[
            selected_index
        ]

        if st.button(
            f"BUY {cfg['snipe_amount']} SOL",
        ):

            if (
                not paper_mode
                and state.wallet is None
            ):

                st.error(
                    "No wallet connected."
                )

            else:

                ok, reason = safety_check(
                    selected["mint"],
                    selected,
                    cfg,
                )

                if not ok:

                    st.error(reason)

                elif (
                    cfg[
                        "require_sell_quote"
                    ]
                    and not simulate_sell_check(
                        selected["mint"]
                    )
                ):

                    st.error(
                        "No Jupiter sell route."
                    )

                else:

                    position = do_buy(
                        state,
                        selected,
                        cfg["snipe_amount"],
                    )

                    if position:

                        with state.lock:
                            state.positions.append(
                                position
                            )

                        state.log(
                            f"MANUAL BUY "
                            f"{selected.get('symbol')} "
                            f"score={selected.get('score')}",
                            "buy",
                        )

                        save_persisted(
                            state
                        )

                        st.success(
                            "Position opened."
                        )

                        st.rerun()

                    else:

                        st.error(
                            "Buy failed. "
                            "Check quote/API logs."
                        )


# ================================================================
# POSITIONS
# ================================================================

st.markdown(
    "### 📌 OPEN POSITIONS"
)

if positions:

    position_rows = []

    for position in positions:

        position_rows.append({

            "Symbol":
                position.get(
                    "symbol",
                    "UNKNOWN"
                ),

            "Entry SOL":
                position.get(
                    "entry_sol",
                    0
                ),

            "Peak %":
                position.get(
                    "peak_pnl_pct",
                    0
                ),

            "Score":
                position.get(
                    "score",
                    0
                ),

            "Opened":
                position.get(
                    "opened_at",
                    ""
                ),
        })

    st.dataframe(
        pd.DataFrame(
            position_rows
        ),
        use_container_width=True,
        hide_index=True,
    )

    for index, position in enumerate(
        positions
    ):

        if st.button(
            f"SELL {position.get('symbol', 'TOKEN')}",
            key=f"sell_position_{index}",
        ):

            trade = do_sell(
                state,
                position,
            )

            if trade:

                with state.lock:

                    if (
                        position
                        in state.positions
                    ):
                        state.positions.remove(
                            position
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
                    f"MANUAL SELL "
                    f"{position['symbol']} "
                    f"{trade['profit']:+.4f} SOL",
                    tag,
                )

                save_persisted(
                    state
                )

                st.rerun()

            else:

                st.error(
                    "Sell failed."
                )

else:

    st.info(
        "No open positions."
    )


# ================================================================
# TERMINAL
# ================================================================

st.markdown(
    "### 🖥️ NEON TERMINAL"
)

terminal = "".join(
    logs
) or (
    '<div class="sys">'
    '// SYSTEM READY //'
    '</div>'
)

st.markdown(
    f'<div class="terminal">{terminal}</div>',
    unsafe_allow_html=True,
)


# ================================================================
# TRADE HISTORY
# ================================================================

st.markdown(
    "### 📜 TRADE HISTORY"
)

if trades:

    trade_rows = []

    for trade in trades[:100]:

        trade_rows.append({

            "Date":
                trade.get(
                    "date",
                    ""
                ),

            "Time":
                trade.get(
                    "time",
                    ""
                ),

            "Symbol":
                trade.get(
                    "symbol",
                    ""
                ),

            "Entry":
                trade.get(
                    "entry_sol",
                    0
                ),

            "Exit":
                trade.get(
                    "exit_sol",
                    0
                ),

            "P/L":
                trade.get(
                    "profit",
                    0
                ),
        })

    st.dataframe(
        pd.DataFrame(
            trade_rows
        ),
        use_container_width=True,
        hide_index=True,
    )

else:

    st.info(
        "No completed trades yet."
    )


# ================================================================
# PNL CALENDAR
# ================================================================

st.markdown(
    "### 📅 P/L CALENDAR"
)

calendar = []

today = datetime.now()

for i in range(30):

    day = today - timedelta(
        days=i
    )

    date_string = day.strftime(
        "%Y-%m-%d"
    )

    day_trades = [
        t
        for t in trades
        if t.get("date")
        == date_string
    ]

    pnl = sum(
        float(
            t.get("profit", 0)
        )
        for t in day_trades
    )

    day_wins = sum(
        1
        for t in day_trades
        if float(
            t.get("profit", 0)
        ) > 0
    )

    day_rate = (
        day_wins
        / len(day_trades)
        * 100
        if day_trades
        else 0
    )

    calendar.append({

        "Date":
            day.strftime("%m/%d"),

        "P/L SOL":
            f"{pnl:+.6f}",

        "Trades":
            len(day_trades),

        "Win Rate":
            f"{day_rate:.0f}%",
    })

st.dataframe(
    pd.DataFrame(calendar),
    use_container_width=True,
    hide_index=True,
)


# ================================================================
# REFRESH
# ================================================================

if HAS_AUTOREFRESH:

    st_autorefresh(
        interval=6000,
        key="cyber_sniper_refresh",
    )

else:

    st.caption(
        "Install streamlit-autorefresh for "
        "automatic dashboard refresh."
    )


# ================================================================
# FOOTER
# ================================================================

st.markdown(
    """
<div style="
text-align:center;
padding:25px;
font-family:'Share Tech Mono';
color:#00ffe7;
opacity:.75;
">

🟣 CYBER SNIPER // NEON PROTOCOL v8.0

<br>

MULTI-SOURCE DISCOVERY //
DEXSCREENER //
BIRDEYE //
JUPITER //
SOLANA RPC

<br><br>

PAPER MODE FIRST.
NO FILTER GUARANTEES PROFIT.

</div>
""",
    unsafe_allow_html=True,
)

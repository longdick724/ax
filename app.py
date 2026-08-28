import os
import json
import time
import math
import random
import threading
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import requests
import pandas as pd
import streamlit as st

from dotenv import load_dotenv
from solders.keypair import Keypair

try:
    from streamlit_autorefresh import st_autorefresh
    HAS_AUTOREFRESH = True
except ImportError:
    HAS_AUTOREFRESH = False


# ================================================================
# ENVIRONMENT
# ================================================================

load_dotenv()

HELIUS_KEY = os.getenv("HELIUS_KEY", "").strip()
BIRDEYE_KEY = os.getenv("BIRDEYE_API_KEY", "").strip()
PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY", "").strip()

HELIUS_RPC_URL = (
    f"https://mainnet.helius-rpc.com/?api-key={HELIUS_KEY}"
    if HELIUS_KEY
    else ""
)

BIRDEYE_URL = "https://public-api.birdeye.so"
DEXSCREENER_URL = "https://api.dexscreener.com"

WSOL_MINT = "So11111111111111111111111111111111111111112"

STATE_FILE = "cyber_sniper_state.json"

MAX_TRADE_SOL_CAP = 0.5

REQUEST_TIMEOUT = 12
SCAN_INTERVAL_SECONDS = 20

# Default safety thresholds.
DEFAULT_CONFIG = {
    "snipe_amount": 0.05,
    "take_profit_pct": 50,
    "trailing_stop_pct": 15,
    "min_liquidity_usd": 15000,
    "min_volume_24h": 5000,
    "min_txns_24h": 50,
    "max_top10_pct": 40,
    "max_positions": 5,
    "daily_loss_limit": 0.2,
    "min_score": 65,
    "max_token_age_hours": 168,
}


# ================================================================
# HTTP SESSION
# ================================================================

SESSION = requests.Session()

SESSION.headers.update({
    "User-Agent": "CyberSniper/7.0",
    "Accept": "application/json",
})


def safe_get(
    url: str,
    *,
    params: Optional[Dict] = None,
    headers: Optional[Dict] = None,
    timeout: int = REQUEST_TIMEOUT,
    retries: int = 2,
) -> Optional[requests.Response]:

    for attempt in range(retries + 1):
        try:
            response = SESSION.get(
                url,
                params=params,
                headers=headers,
                timeout=timeout,
            )

            if response.status_code == 200:
                return response

            if response.status_code in (429, 500, 502, 503, 504):
                time.sleep(min(2 ** attempt, 5))
                continue

            return response

        except requests.RequestException:
            if attempt < retries:
                time.sleep(min(2 ** attempt, 5))

    return None


def safe_post(
    url: str,
    *,
    json_body: Optional[Dict] = None,
    timeout: int = REQUEST_TIMEOUT,
    retries: int = 2,
) -> Optional[requests.Response]:

    for attempt in range(retries + 1):
        try:
            response = SESSION.post(
                url,
                json=json_body,
                timeout=timeout,
            )

            if response.status_code == 200:
                return response

            if response.status_code in (429, 500, 502, 503, 504):
                time.sleep(min(2 ** attempt, 5))
                continue

            return response

        except requests.RequestException:
            if attempt < retries:
                time.sleep(min(2 ** attempt, 5))

    return None


# ================================================================
# STATE
# ================================================================

@dataclass
class EngineState:

    lock: threading.RLock = field(default_factory=threading.RLock)

    running: bool = False
    paper_mode: bool = True

    wallet: Optional[Keypair] = None
    wallet_address: str = ""

    positions: List[Dict] = field(default_factory=list)
    trades: List[Dict] = field(default_factory=list)

    discovered: List[Dict] = field(default_factory=list)
    rejected: List[Dict] = field(default_factory=list)

    seen_mints: Dict[str, float] = field(default_factory=dict)

    logs: List[str] = field(default_factory=list)

    config: Dict = field(
        default_factory=lambda: dict(DEFAULT_CONFIG)
    )

    scanner_stats: Dict = field(
        default_factory=lambda: {
            "scans": 0,
            "raw_candidates": 0,
            "unique_candidates": 0,
            "accepted": 0,
            "rejected": 0,
            "last_scan": "",
        }
    )

    def log(self, message: str, tag: str = "sys"):

        timestamp = datetime.now().strftime("%H:%M:%S")

        with self.lock:
            self.logs.insert(
                0,
                f'<div class="line {tag}">[{timestamp}] {message}</div>'
            )

            self.logs = self.logs[:120]

        print(f"[{timestamp}] {message}")


@st.cache_resource
def get_state() -> EngineState:

    state = EngineState()
    load_state(state)

    return state


def load_state(state: EngineState):

    if not os.path.exists(STATE_FILE):
        return

    try:

        with open(STATE_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)

        with state.lock:
            state.positions = data.get("positions", [])
            state.trades = data.get("trades", [])
            state.seen_mints = data.get("seen_mints", {})

    except Exception as exc:
        print(f"State load error: {exc}")


def save_state(state: EngineState):

    try:

        with state.lock:

            data = {
                "positions": state.positions,
                "trades": state.trades,
                "seen_mints": state.seen_mints,
            }

        temp_file = STATE_FILE + ".tmp"

        with open(temp_file, "w", encoding="utf-8") as file:
            json.dump(data, file, indent=2)

        os.replace(temp_file, STATE_FILE)

    except Exception as exc:
        print(f"State save error: {exc}")


# ================================================================
# WALLET
# ================================================================

def load_wallet_from_env(state: EngineState):

    if not PRIVATE_KEY:
        return

    if state.wallet is not None:
        return

    try:

        wallet = Keypair.from_base58_string(PRIVATE_KEY)

        with state.lock:
            state.wallet = wallet
            state.wallet_address = str(wallet.pubkey())

        state.log(
            f"Wallet loaded: {state.wallet_address[:6]}..."
            f"{state.wallet_address[-4:]}",
            "sys",
        )

    except Exception as exc:
        state.log(f"Wallet error: {exc}", "sell-loss")


# ================================================================
# RPC
# ================================================================

def rpc_call(
    method: str,
    params: List,
) -> Optional[Dict]:

    if not HELIUS_RPC_URL:
        return None

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params,
    }

    response = safe_post(
        HELIUS_RPC_URL,
        json_body=payload,
    )

    if response is None:
        return None

    try:
        result = response.json()

        if "error" in result:
            return None

        return result.get("result")

    except Exception:
        return None


def get_wallet_balance(pubkey: str) -> float:

    if not pubkey:
        return 0.0

    result = rpc_call(
        "getBalance",
        [pubkey],
    )

    if not result:
        return 0.0

    try:
        return float(result.get("value", 0)) / 1_000_000_000
    except Exception:
        return 0.0


def get_token_balance(
    owner: str,
    mint: str,
) -> Tuple[int, int]:

    result = rpc_call(
        "getTokenAccountsByOwner",
        [
            owner,
            {"mint": mint},
            {"encoding": "jsonParsed"},
        ],
    )

    if not result:
        return 0, 0

    try:

        accounts = result.get("value", [])

        if not accounts:
            return 0, 0

        amount = (
            accounts[0]
            ["account"]
            ["data"]
            ["parsed"]
            ["info"]
            ["tokenAmount"]
        )

        return (
            int(amount["amount"]),
            int(amount["decimals"]),
        )

    except Exception:
        return 0, 0


# ================================================================
# DEXSCREENER DISCOVERY
# ================================================================

def dex_token_search(query: str) -> List[Dict]:

    response = safe_get(
        f"{DEXSCREENER_URL}/latest/dex/search",
        params={"q": query},
    )

    if not response:
        return []

    try:
        return response.json().get("pairs") or []
    except Exception:
        return []


def dex_boosted_tokens() -> List[Dict]:

    response = safe_get(
        f"{DEXSCREENER_URL}/token-boosts/latest/v1"
    )

    if not response:
        return []

    try:
        return response.json() or []
    except Exception:
        return []


def dex_token_pairs(mint: str) -> List[Dict]:

    response = safe_get(
        f"{DEXSCREENER_URL}/latest/dex/tokens/{mint}"
    )

    if not response:
        return []

    try:
        return response.json().get("pairs") or []
    except Exception:
        return []


def normalize_dex_pair(pair: Dict, source: str) -> Optional[Dict]:

    base = pair.get("baseToken") or {}

    mint = base.get("address")

    if not mint:
        return None

    liquidity = (
        pair.get("liquidity") or {}
    ).get("usd", 0) or 0

    volume = (
        pair.get("volume") or {}
    ).get("h24", 0) or 0

    txns = (
        pair.get("txns") or {}
    ).get("h24") or {}

    buys = txns.get("buys", 0) or 0
    sells = txns.get("sells", 0) or 0

    created_ms = pair.get("pairCreatedAt")

    age_hours = None

    if created_ms:

        try:
            age_hours = max(
                0,
                (
                    time.time() - float(created_ms) / 1000
                ) / 3600,
            )
        except Exception:
            pass

    return {
        "mint": mint,
        "symbol": base.get("symbol") or "UNKNOWN",
        "name": base.get("name") or "Unknown",
        "source": source,
        "dex": pair.get("dexId") or "unknown",
        "liquidity": float(liquidity),
        "volume_24h": float(volume),
        "buys_24h": int(buys),
        "sells_24h": int(sells),
        "txns_24h": int(buys + sells),
        "price_usd": float(pair.get("priceUsd") or 0),
        "pair_address": pair.get("pairAddress") or "",
        "url": pair.get("url") or "",
        "age_hours": age_hours,
    }


def discover_dexscreener() -> List[Dict]:

    candidates = []
    seen = set()

    # ------------------------------------------------------------
    # Boosted tokens
    # ------------------------------------------------------------

    for item in dex_boosted_tokens():

        if item.get("chainId") != "solana":
            continue

        mint = item.get("tokenAddress")

        if not mint or mint in seen:
            continue

        seen.add(mint)

        pairs = dex_token_pairs(mint)

        for pair in pairs:

            if pair.get("chainId") != "solana":
                continue

            normalized = normalize_dex_pair(
                pair,
                "DEXSCREENER BOOST",
            )

            if normalized:
                candidates.append(normalized)

    # ------------------------------------------------------------
    # Search discovery terms
    #
    # DexScreener search is not a guaranteed "new token" feed.
    # These searches supplement the boost feed.
    # ------------------------------------------------------------

    for query in (
        "SOL",
        "USD",
        "USDC",
    ):

        pairs = dex_token_search(query)

        for pair in pairs:

            if pair.get("chainId") != "solana":
                continue

            normalized = normalize_dex_pair(
                pair,
                "DEXSCREENER SEARCH",
            )

            if normalized:
                mint = normalized["mint"]

                if mint not in seen:
                    seen.add(mint)
                    candidates.append(normalized)

    return candidates


# ================================================================
# BIRDEYE DISCOVERY
#
# Birdeye currently exposes:
#   - new listings
#   - trending tokens
#   - token lists
#   - markets
#
# We use it only when an API key is configured.
# ================================================================

def birdeye_headers() -> Dict:

    return {
        "X-API-KEY": BIRDEYE_KEY,
        "x-chain": "solana",
        "accept": "application/json",
    }


def birdeye_new_listings() -> List[Dict]:

    if not BIRDEYE_KEY:
        return []

    response = safe_get(
        f"{BIRDEYE_URL}/defi/v2/tokens/new_listing",
        params={
            "limit": 20,
            "meme_platform_enabled": "true",
        },
        headers=birdeye_headers(),
    )

    if not response:
        return []

    try:

        data = response.json()

        return (
            data.get("data", {}).get("items")
            or data.get("data", {}).get("tokens")
            or []
        )

    except Exception:
        return []


def birdeye_trending() -> List[Dict]:

    if not BIRDEYE_KEY:
        return []

    response = safe_get(
        f"{BIRDEYE_URL}/defi/token_trending",
        params={
            "sort_by": "volumeUSD",
            "sort_type": "desc",
            "offset": 0,
            "limit": 50,
            "interval": "1h",
        },
        headers=birdeye_headers(),
    )

    if not response:
        return []

    try:

        data = response.json()

        return (
            data.get("data", {}).get("tokens")
            or []
        )

    except Exception:
        return []


def birdeye_token_overview(mint: str) -> Optional[Dict]:

    if not BIRDEYE_KEY:
        return None

    response = safe_get(
        f"{BIRDEYE_URL}/defi/token_overview",
        params={
            "address": mint,
        },
        headers=birdeye_headers(),
    )

    if not response:
        return None

    try:

        data = response.json()

        return data.get("data") or None

    except Exception:
        return None


def normalize_birdeye_token(
    token: Dict,
    source: str,
) -> Optional[Dict]:

    mint = (
        token.get("address")
        or token.get("tokenAddress")
    )

    if not mint:
        return None

    return {
        "mint": mint,
        "symbol": (
            token.get("symbol")
            or token.get("name")
            or "UNKNOWN"
        ),
        "name": token.get("name") or "Unknown",
        "source": source,
        "dex": "multiple",
        "liquidity": float(
            token.get("liquidity")
            or token.get("liquidityUsd")
            or 0
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
        "txns_24h": int(
            token.get("trade24h")
            or token.get("txns24h")
            or 0
        ),
        "price_usd": float(
            token.get("price")
            or token.get("priceUsd")
            or 0
        ),
        "pair_address": "",
        "url": "",
        "age_hours": None,
    }


def discover_birdeye() -> List[Dict]:

    if not BIRDEYE_KEY:
        return []

    candidates = []

    for token in birdeye_new_listings():

        normalized = normalize_birdeye_token(
            token,
            "BIRDEYE NEW",
        )

        if normalized:
            candidates.append(normalized)

    for token in birdeye_trending():

        normalized = normalize_birdeye_token(
            token,
            "BIRDEYE TRENDING",
        )

        if normalized:
            candidates.append(normalized)

    return candidates


# ================================================================
# CANDIDATE MERGING
# ================================================================

def merge_candidates(
    candidates: List[Dict],
) -> List[Dict]:

    merged = {}

    for candidate in candidates:

        mint = candidate.get("mint")

        if not mint:
            continue

        if mint not in merged:

            merged[mint] = dict(candidate)

            merged[mint]["sources"] = [
                candidate.get("source", "unknown")
            ]

        else:

            current = merged[mint]

            current["liquidity"] = max(
                current.get("liquidity", 0),
                candidate.get("liquidity", 0),
            )

            current["volume_24h"] = max(
                current.get("volume_24h", 0),
                candidate.get("volume_24h", 0),
            )

            current["buys_24h"] = max(
                current.get("buys_24h", 0),
                candidate.get("buys_24h", 0),
            )

            current["sells_24h"] = max(
                current.get("sells_24h", 0),
                candidate.get("sells_24h", 0),
            )

            source = candidate.get("source", "unknown")

            if source not in current["sources"]:
                current["sources"].append(source)

    return list(merged.values())


# ================================================================
# TOKEN ENRICHMENT
# ================================================================

def enrich_candidate(token: Dict) -> Dict:

    mint = token["mint"]

    pairs = dex_token_pairs(mint)

    if pairs:

        sol_pairs = [
            p for p in pairs
            if p.get("chainId") == "solana"
        ]

        if sol_pairs:

            # Choose the strongest liquidity market.
            sol_pairs.sort(
                key=lambda p: float(
                    (p.get("liquidity") or {}).get("usd", 0)
                    or 0
                ),
                reverse=True,
            )

            best = normalize_dex_pair(
                sol_pairs[0],
                "DEXSCREENER ENRICHMENT",
            )

            if best:

                token["liquidity"] = max(
                    token.get("liquidity", 0),
                    best.get("liquidity", 0),
                )

                token["volume_24h"] = max(
                    token.get("volume_24h", 0),
                    best.get("volume_24h", 0),
                )

                token["buys_24h"] = max(
                    token.get("buys_24h", 0),
                    best.get("buys_24h", 0),
                )

                token["sells_24h"] = max(
                    token.get("sells_24h", 0),
                    best.get("sells_24h", 0),
                )

                token["txns_24h"] = max(
                    token.get("txns_24h", 0),
                    best.get("txns_24h", 0),
                )

                token["price_usd"] = best.get(
                    "price_usd",
                    token.get("price_usd", 0),
                )

                token["pair_address"] = best.get(
                    "pair_address",
                    "",
                )

                token["url"] = best.get(
                    "url",
                    "",
                )

                if best.get("age_hours") is not None:
                    token["age_hours"] = best["age_hours"]

                token["dex"] = best.get(
                    "dex",
                    token.get("dex", "unknown"),
                )

    return token


# ================================================================
# SCORING
# ================================================================

def clamp(value: float, low: float, high: float) -> float:

    return max(low, min(high, value))


def liquidity_score(liquidity: float) -> float:

    if liquidity <= 0:
        return 0

    # Logarithmic scaling prevents enormous liquidity
    # from completely dominating the score.
    score = (
        math.log10(max(liquidity, 1))
        / math.log10(1_000_000)
        * 100
    )

    return clamp(score, 0, 100)


def volume_score(volume: float) -> float:

    if volume <= 0:
        return 0

    score = (
        math.log10(max(volume, 1))
        / math.log10(5_000_000)
        * 100
    )

    return clamp(score, 0, 100)


def activity_score(
    buys: int,
    sells: int,
) -> float:

    total = buys + sells

    if total <= 0:
        return 0

    # Balanced activity is preferable to an almost entirely
    # one-sided flow.
    balance = min(buys, sells) / max(buys, sells)

    count_score = clamp(
        math.log10(total + 1) / math.log10(10000) * 100,
        0,
        100,
    )

    return (
        count_score * 0.65
        + balance * 100 * 0.35
    )


def source_score(sources: List[str]) -> float:

    score = 20

    for source in sources:

        if "BIRDEYE NEW" in source:
            score += 25

        elif "BIRDEYE TRENDING" in source:
            score += 20

        elif "DEXSCREENER BOOST" in source:
            score += 15

        elif "DEXSCREENER SEARCH" in source:
            score += 5

    return clamp(score, 0, 100)


def age_score(age_hours: Optional[float]) -> float:

    if age_hours is None:
        return 35

    if age_hours < 0.25:
        return 35

    if age_hours < 1:
        return 55

    if age_hours < 6:
        return 85

    if age_hours < 24:
        return 100

    if age_hours < 72:
        return 90

    if age_hours < 168:
        return 70

    return 35


def calculate_score(token: Dict) -> Tuple[int, Dict]:

    liquidity = token.get("liquidity", 0)
    volume = token.get("volume_24h", 0)
    buys = token.get("buys_24h", 0)
    sells = token.get("sells_24h", 0)

    scores = {
        "liquidity": liquidity_score(liquidity),
        "volume": volume_score(volume),
        "activity": activity_score(buys, sells),
        "sources": source_score(
            token.get("sources", [])
        ),
        "age": age_score(
            token.get("age_hours")
        ),
    }

    final_score = (
        scores["liquidity"] * 0.28
        + scores["volume"] * 0.24
        + scores["activity"] * 0.23
        + scores["sources"] * 0.10
        + scores["age"] * 0.15
    )

    return int(round(clamp(final_score, 0, 100))), scores


# ================================================================
# SAFETY / RISK
# ================================================================

def check_mint_authorities(mint: str) -> Dict:

    # getAccountInfo is used here rather than pretending a quote
    # is a security guarantee.
    result = rpc_call(
        "getAccountInfo",
        [
            mint,
            {
                "encoding": "jsonParsed",
            },
        ],
    )

    if not result:
        return {
            "available": False,
            "mint_authority": None,
            "freeze_authority": None,
        }

    try:

        parsed = (
            result["value"]
            ["data"]
            ["parsed"]
            ["info"]
        )

        return {
            "available": True,
            "mint_authority": parsed.get(
                "mintAuthority"
            ),
            "freeze_authority": parsed.get(
                "freezeAuthority"
            ),
        }

    except Exception:

        return {
            "available": False,
            "mint_authority": None,
            "freeze_authority": None,
        }


def estimate_risk(
    token: Dict,
) -> Tuple[int, List[str]]:

    risk = 0
    reasons = []

    liquidity = token.get("liquidity", 0)
    volume = token.get("volume_24h", 0)
    buys = token.get("buys_24h", 0)
    sells = token.get("sells_24h", 0)

    if liquidity < 5000:
        risk += 35
        reasons.append("very low liquidity")

    elif liquidity < 15000:
        risk += 15
        reasons.append("low liquidity")

    if volume < 1000:
        risk += 25
        reasons.append("very low volume")

    elif volume < 5000:
        risk += 10
        reasons.append("low volume")

    if buys + sells < 20:
        risk += 20
        reasons.append("low transaction activity")

    if buys > 0 and sells == 0:
        risk += 30
        reasons.append("no observed sells")

    if sells > buys * 5 and sells > 100:
        risk += 15
        reasons.append("strong sell imbalance")

    if token.get("age_hours") is not None:

        if token["age_hours"] < 0.10:
            risk += 15
            reasons.append("extremely new token")

    return min(risk, 100), reasons


def safety_filter(
    token: Dict,
    config: Dict,
) -> Tuple[bool, str]:

    liquidity = token.get("liquidity", 0)
    volume = token.get("volume_24h", 0)
    txns = token.get("txns_24h", 0)

    if liquidity < config["min_liquidity_usd"]:
        return (
            False,
            f"liquidity ${liquidity:,.0f} "
            f"< ${config['min_liquidity_usd']:,.0f}",
        )

    if volume < config["min_volume_24h"]:
        return (
            False,
            f"volume ${volume:,.0f} "
            f"< ${config['min_volume_24h']:,.0f}",
        )

    if txns < config["min_txns_24h"]:
        return (
            False,
            f"transactions {txns} "
            f"< {config['min_txns_24h']}",
        )

    age = token.get("age_hours")

    if (
        age is not None
        and age > config["max_token_age_hours"]
    ):
        return (
            False,
            f"token age {age:.1f}h exceeds "
            f"{config['max_token_age_hours']}h",
        )

    score, _ = calculate_score(token)

    if score < config["min_score"]:
        return (
            False,
            f"score {score} < {config['min_score']}",
        )

    return True, "passed"


# ================================================================
# DISCOVERY ENGINE
# ================================================================

def discover_all(state: EngineState) -> List[Dict]:

    state.log(
        "SCANNER // collecting discovery feeds...",
        "sys",
    )

    all_candidates = []

    dex_candidates = discover_dexscreener()

    state.log(
        f"DEXSCREENER // {len(dex_candidates)} raw candidates",
        "sys",
    )

    all_candidates.extend(dex_candidates)

    if BIRDEYE_KEY:

        birdeye_candidates = discover_birdeye()

        state.log(
            f"BIRDEYE // {len(birdeye_candidates)} raw candidates",
            "sys",
        )

        all_candidates.extend(birdeye_candidates)

    else:

        state.log(
            "BIRDEYE // API key not configured",
            "sys",
        )

    merged = merge_candidates(all_candidates)

    state.log(
        f"MERGER // {len(merged)} unique token mints",
        "sys",
    )

    enriched = []

    # Prevent an expensive enrichment storm.
    # Start with the most promising raw candidates.
    merged.sort(
        key=lambda x: (
            x.get("liquidity", 0),
            x.get("volume_24h", 0),
        ),
        reverse=True,
    )

    for token in merged[:80]:

        try:
            enriched.append(
                enrich_candidate(token)
            )
        except Exception as exc:
            state.log(
                f"enrichment error {token.get('symbol')}: {exc}",
                "sell-loss",
            )

    return enriched


def process_candidates(
    state: EngineState,
    candidates: List[Dict],
):

    accepted = []
    rejected = []

    with state.lock:
        config = dict(state.config)

    for token in candidates:

        mint = token.get("mint")

        if not mint:
            continue

        score, component_scores = calculate_score(token)

        token["score"] = score
        token["score_components"] = component_scores

        risk, risk_reasons = estimate_risk(token)

        token["risk"] = risk
        token["risk_reasons"] = risk_reasons

        # Never automatically buy a token solely because it
        # appears on a discovery feed.
        ok, reason = safety_filter(
            token,
            config,
        )

        if not ok:

            token["status"] = "REJECTED"
            token["reject_reason"] = reason

            rejected.append(token)

            continue

        # RPC authority check is supplementary.
        # Missing RPC data does not silently become "safe".
        authority = check_mint_authorities(mint)

        token["authority_check"] = authority

        if authority["available"]:

            if authority.get("mint_authority"):
                token["risk"] += 15
                token["risk_reasons"].append(
                    "mint authority active"
                )

            if authority.get("freeze_authority"):
                token["risk"] += 20
                token["risk_reasons"].append(
                    "freeze authority active"
                )

        if token["risk"] >= 60:

            token["status"] = "HIGH RISK"
            token["reject_reason"] = (
                "; ".join(token["risk_reasons"])
                or "risk score too high"
            )

            rejected.append(token)

            continue

        token["status"] = "WATCH"
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

    with state.lock:

        state.discovered = accepted[:50]
        state.rejected = rejected[:50]

        state.scanner_stats["scans"] += 1
        state.scanner_stats["raw_candidates"] = len(candidates)
        state.scanner_stats["unique_candidates"] = len(candidates)
        state.scanner_stats["accepted"] = len(accepted)
        state.scanner_stats["rejected"] = len(rejected)
        state.scanner_stats["last_scan"] = (
            datetime.now().strftime("%H:%M:%S")
        )

    state.log(
        f"SCAN COMPLETE // {len(accepted)} watch candidates // "
        f"{len(rejected)} rejected",
        "buy" if accepted else "sys",
    )


# ================================================================
# PAPER POSITION PRICING
# ================================================================

def simulated_price_move(
    position: Dict,
) -> float:

    current = float(
        position.get(
            "sim_price_usd",
            position.get("entry_price_usd", 0),
        )
    )

    if current <= 0:
        return 0

    # Small random market movement.
    # This is deliberately only a visual simulation.
    drift = random.gauss(0, 0.035)

    current *= max(
        0.2,
        1 + drift,
    )

    position["sim_price_usd"] = current

    entry = float(
        position.get("entry_price_usd", current)
    )

    if entry <= 0:
        return 0

    return (
        (current - entry)
        / entry
        * 100
    )


# ================================================================
# PAPER BUY
# ================================================================

def paper_buy(
    state: EngineState,
    token: Dict,
) -> Optional[Dict]:

    amount = float(
        state.config["snipe_amount"]
    )

    price = float(
        token.get("price_usd", 0)
    )

    position = {
        "mint": token["mint"],
        "symbol": token.get("symbol", "UNKNOWN"),
        "entry_sol": amount,
        "entry_price_usd": price,
        "sim_price_usd": price,
        "out_amount": 0,
        "opened_at": datetime.now().isoformat(),
        "buy_sig": "PAPER",
        "peak_pnl_pct": 0.0,
        "score": token.get("score", 0),
    }

    return position


# ================================================================
# PAPER SELL
# ================================================================

def paper_sell(
    position: Dict,
) -> Dict:

    entry = float(
        position.get("entry_sol", 0)
    )

    pnl_pct = float(
        position.get("peak_pnl_pct", 0)
    )

    profit = (
        entry * pnl_pct / 100
    )

    exit_sol = entry + profit

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
        "exit_sol": exit_sol,
        "profit": profit,
        "sell_sig": "PAPER",
    }


# ================================================================
# POSITION MANAGEMENT
# ================================================================

def manage_positions(
    state: EngineState,
):

    with state.lock:
        positions = list(state.positions)
        config = dict(state.config)

    for position in positions:

        if state.paper_mode:

            pnl = simulated_price_move(
                position
            )

        else:
            # Live execution is intentionally not performed by
            # this discovery build. Keep the engine paper-first
            # while validating the scanner.
            continue

        with state.lock:

            position["peak_pnl_pct"] = max(
                float(
                    position.get(
                        "peak_pnl_pct",
                        0,
                    )
                ),
                pnl,
            )

            peak = position["peak_pnl_pct"]

        trailing_trigger = (
            peak
            - config["trailing_stop_pct"]
        )

        should_close = False
        reason = ""

        if pnl >= config["take_profit_pct"]:

            should_close = True
            reason = "take profit"

        elif (
            peak > 0
            and pnl <= trailing_trigger
        ):

            should_close = True
            reason = "trailing stop"

        elif (
            peak <= 0
            and pnl <= -config["trailing_stop_pct"]
        ):

            should_close = True
            reason = "stop loss"

        if should_close:

            trade = paper_sell(position)

            with state.lock:

                if position in state.positions:
                    state.positions.remove(position)

                state.trades.append(trade)

            tag = (
                "sell-win"
                if trade["profit"] >= 0
                else "sell-loss"
            )

            state.log(
                f"CLOSED {position['symbol']} "
                f"({reason}) "
                f"{trade['profit']:+.4f} SOL",
                tag,
            )

            save_state(state)


# ================================================================
# AUTO ENTRY
# ================================================================

def automatic_entries(
    state: EngineState,
):

    if not state.paper_mode:
        return

    with state.lock:

        if len(state.positions) >= state.config["max_positions"]:
            return

        candidates = list(state.discovered)
        config = dict(state.config)

        held = {
            p["mint"]
            for p in state.positions
        }

    # Only consider the highest-scoring candidates.
    for token in candidates[:10]:

        mint = token["mint"]

        if mint in held:
            continue

        if token.get("score", 0) < config["min_score"]:
            continue

        if token.get("risk", 100) >= 60:
            continue

        position = paper_buy(
            state,
            token,
        )

        if not position:
            continue

        with state.lock:

            if len(state.positions) >= config["max_positions"]:
                break

            state.positions.append(position)

        state.log(
            f"🟢 PAPER ENTRY // "
            f"{token.get('symbol', 'UNKNOWN')} "
            f"// SCORE {token.get('score', 0)} "
            f"// RISK {token.get('risk', 0)}",
            "buy",
        )

        save_state(state)


# ================================================================
# DAILY PNL
# ================================================================

def daily_pnl(
    state: EngineState,
) -> float:

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
# ENGINE
# ================================================================

def engine_loop(
    state: EngineState,
):

    state.log(
        "NEON ENGINE ONLINE // background worker started",
        "sys",
    )

    last_scan = 0

    while True:

        try:

            if not state.running:

                time.sleep(2)
                continue

            current_time = time.time()

            # --------------------------------------------
            # Daily kill switch
            # --------------------------------------------

            if (
                daily_pnl(state)
                <= -abs(
                    state.config["daily_loss_limit"]
                )
            ):

                state.log(
                    "KILL SWITCH // daily loss limit reached",
                    "sell-loss",
                )

                with state.lock:
                    state.running = False

                continue

            # --------------------------------------------
            # Position management
            # --------------------------------------------

            manage_positions(state)

            # --------------------------------------------
            # Discovery scan
            # --------------------------------------------

            if (
                current_time - last_scan
                >= SCAN_INTERVAL_SECONDS
            ):

                last_scan = current_time

                candidates = discover_all(
                    state
                )

                process_candidates(
                    state,
                    candidates,
                )

                automatic_entries(
                    state
                )

            time.sleep(2)

        except Exception as exc:

            state.log(
                f"ENGINE ERROR // {exc}",
                "sell-loss",
            )

            time.sleep(5)


@st.cache_resource
def start_engine(
    state: EngineState,
):

    thread = threading.Thread(
        target=engine_loop,
        args=(state,),
        daemon=True,
        name="CyberSniperEngine",
    )

    thread.start()

    return thread


# ================================================================
# UI
# ================================================================

st.set_page_config(
    page_title="CYBER SNIPER // NEON HUNTER",
    page_icon="🟣",
    layout="wide",
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
    --cyan: #00ffe6;
    --pink: #ff00e6;
    --green: #00ff88;
    --red: #ff225f;
    --purple: #8b00ff;
    --bg: #030008;
}

.stApp {
    background:
        radial-gradient(
            circle at 15% 15%,
            rgba(255,0,230,.10),
            transparent 35%
        ),
        radial-gradient(
            circle at 85% 80%,
            rgba(0,255,230,.08),
            transparent 35%
        ),
        #030008;
    color: #e8ffff;
}

/* Animated cyber grid */

.stApp::before {

    content: "";

    position: fixed;

    inset: 0;

    pointer-events: none;

    z-index: -2;

    background:
        linear-gradient(
            rgba(0,255,230,.035) 1px,
            transparent 1px
        ),
        linear-gradient(
            90deg,
            rgba(255,0,230,.035) 1px,
            transparent 1px
        );

    background-size: 35px 35px;

    animation:
        gridmove 18s linear infinite;
}

@keyframes gridmove {

    from {
        background-position:
            0 0,
            0 0;
    }

    to {
        background-position:
            0 700px,
            700px 0;
    }
}

/* Scanlines */

.stApp::after {

    content: "";

    position: fixed;

    inset: 0;

    pointer-events: none;

    z-index: 999;

    background:
        repeating-linear-gradient(
            0deg,
            rgba(0,0,0,.12) 0px,
            rgba(0,0,0,.12) 1px,
            transparent 2px,
            transparent 4px
        );

    opacity: .45;
}

/* Header */

.cyber-header {

    border:
        1px solid var(--cyan);

    border-radius: 14px;

    padding: 25px;

    text-align: center;

    background:
        linear-gradient(
            135deg,
            rgba(5,0,20,.96),
            rgba(25,0,50,.90)
        );

    box-shadow:
        0 0 20px rgba(0,255,230,.35),
        inset 0 0 30px rgba(255,0,230,.08);

    position: relative;

    overflow: hidden;
}

.cyber-header::before {

    content: "";

    position: absolute;

    left: -100%;

    top: 0;

    width: 100%;

    height: 2px;

    background:
        linear-gradient(
            90deg,
            transparent,
            var(--cyan),
            var(--pink),
            transparent
        );

    animation: scan 3s linear infinite;
}

@keyframes scan {

    0% {
        left: -100%;
    }

    100% {
        left: 100%;
    }
}

.cyber-title {

    font-family: 'Orbitron', sans-serif;

    color: var(--cyan);

    font-size: 38px;

    letter-spacing: 5px;

    text-shadow:
        0 0 8px var(--cyan),
        0 0 18px var(--pink);

    animation:
        glitch 4s infinite;
}

@keyframes glitch {

    0%, 94%, 100% {
        transform: translate(0);
    }

    95% {
        transform: translate(-2px, 1px);
    }

    96% {
        transform: translate(2px, -1px);
    }

    97% {
        transform: translate(-1px, 0);
    }
}

.sub {

    color: var(--pink);

    font-family: 'Share Tech Mono', monospace;

    letter-spacing: 3px;
}

/* Cards */

.cyber-card {

    background:
        linear-gradient(
            135deg,
            rgba(7,2,18,.96),
            rgba(20,0,40,.88)
        );

    border:
        1px solid rgba(0,255,230,.65);

    border-radius: 10px;

    padding: 16px;

    margin: 5px;

    box-shadow:
        0 0 14px rgba(0,255,230,.15);

    transition:
        transform .2s,
        box-shadow .2s,
        border-color .2s;
}

.cyber-card:hover {

    transform:
        translateY(-3px);

    border-color:
        var(--pink);

    box-shadow:
        0 0 25px rgba(255,0,230,.35);
}

.metric-value {

    color:
        var(--cyan);

    font-family:
        'Orbitron', sans-serif;

    font-size:
        23px;

    text-shadow:
        0 0 12px var(--cyan);
}

/* Terminal */

.terminal {

    background:
        #000;

    border:
        1px solid var(--green);

    border-radius:
        8px;

    padding:
        14px;

    height:
        280px;

    overflow-y:
        auto;

    font-family:
        'Share Tech Mono',
        monospace;

    font-size:
        12px;

    box-shadow:
        inset 0 0 25px rgba(0,255,136,.12);
}

.terminal .buy {
    color: var(--cyan);
}

.terminal .sell-win {
    color: var(--green);
}

.terminal .sell-loss {
    color: var(--red);
}

.terminal .sys {
    color: var(--pink);
}

/* Sidebar */

section[data-testid="stSidebar"] {

    background:
        linear-gradient(
            180deg,
            #030008,
            #10001f
        );

    border-right:
        1px solid var(--pink);
}

/* Buttons */

.stButton > button {

    background:
        linear-gradient(
            135deg,
            #07000f,
            #1b0033
        );

    color:
        var(--cyan);

    border:
        1px solid var(--cyan);

    font-family:
        'Orbitron',
        sans-serif;

    letter-spacing:
        1px;

    transition:
        all .2s;
}

.stButton > button:hover {

    color:
        #000;

    background:
        linear-gradient(
            135deg,
            var(--pink),
            var(--cyan)
        );

    box-shadow:
        0 0 25px
        rgba(0,255,230,.45);
}

/* Score bars */

.score-bar {

    height: 7px;

    border-radius: 5px;

    background:
        #150020;

    overflow:
        hidden;

    margin:
        5px 0 10px;
}

.score-fill {

    height: 100%;

    background:
        linear-gradient(
            90deg,
            var(--pink),
            var(--cyan)
        );

    box-shadow:
        0 0 10px var(--cyan);
}

/* Status */

.status-online {

    color:
        var(--green);

    font-family:
        'Orbitron',
        sans-serif;

    text-align:
        center;

    text-shadow:
        0 0 15px var(--green);

    animation:
        pulse 2s infinite;
}

@keyframes pulse {

    50% {
        opacity: .55;
    }
}

</style>
""",
    unsafe_allow_html=True,
)


# ================================================================
# INITIALIZE
# ================================================================

state = get_state()

load_wallet_from_env(state)

start_engine(state)


# ================================================================
# HEADER
# ================================================================

st.markdown(
    """
<div class="cyber-header">

<div class="cyber-title">
🟣 CYBER SNIPER // NEON HUNTER
</div>

<div class="sub">
[ SOLANA ] [ MULTI-SOURCE DISCOVERY ]
[ RISK MATRIX ] [ PAPER ENGINE ]
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
        "## ⚡ NEON CONTROL",
    )

    st.caption(
        "v7.0 // multi-source token intelligence"
    )

    st.markdown("---")

    # ------------------------------------------------------------
    # Wallet
    # ------------------------------------------------------------

    st.markdown("### 🔑 WALLET")

    if state.wallet:

        st.success(
            f"{state.wallet_address[:6]}..."
            f"{state.wallet_address[-4:]}"
        )

    else:

        st.info(
            "No wallet loaded. "
            "Paper mode does not require one."
        )

    st.markdown("---")

    # ------------------------------------------------------------
    # Mode
    # ------------------------------------------------------------

    st.markdown("### ⚠️ MODE")

    new_paper_mode = st.toggle(
        "PAPER MODE",
        value=state.paper_mode,
    )

    if new_paper_mode != state.paper_mode:

        with state.lock:
            state.paper_mode = new_paper_mode

        state.log(
            "Trading mode changed",
            "sys",
        )

    if not state.paper_mode:

        st.error(
            "LIVE MODE IS NOT ENABLED BY THIS BUILD."
        )

        st.caption(
            "Validate discovery and paper execution first."
        )

        with state.lock:
            state.paper_mode = True

    # ------------------------------------------------------------
    # Strategy
    # ------------------------------------------------------------

    st.markdown("---")
    st.markdown("### 🎯 STRATEGY")

    preset_name = st.selectbox(
        "Preset",
        [
            "🛡️ Conservative",
            "⚖️ Moderate",
            "🔥 Aggressive",
        ],
        index=1,
    )

    presets = {

        "🛡️ Conservative": {
            "tp": 20,
            "trail": 8,
            "amount": 0.02,
            "liq": 25000,
            "volume": 15000,
            "txns": 150,
            "score": 75,
        },

        "⚖️ Moderate": {
            "tp": 50,
            "trail": 15,
            "amount": 0.05,
            "liq": 15000,
            "volume": 5000,
            "txns": 50,
            "score": 65,
        },

        "🔥 Aggressive": {
            "tp": 100,
            "trail": 25,
            "amount": 0.10,
            "liq": 8000,
            "volume": 2000,
            "txns": 25,
            "score": 60,
        },
    }

    preset = presets[preset_name]

    snipe_amount = st.slider(
        "Paper Position (SOL)",
        0.01,
        MAX_TRADE_SOL_CAP,
        float(preset["amount"]),
        0.01,
    )

    take_profit = st.slider(
        "Take Profit %",
        10,
        300,
        preset["tp"],
        5,
    )

    trailing_stop = st.slider(
        "Trailing Stop %",
        5,
        50,
        preset["trail"],
        5,
    )

    min_liquidity = st.number_input(
        "Minimum Liquidity USD",
        1000,
        500000,
        preset["liq"],
        1000,
    )

    min_volume = st.number_input(
        "Minimum 24h Volume USD",
        0,
        10000000,
        preset["volume"],
        1000,
    )

    min_txns = st.number_input(
        "Minimum 24h Transactions",
        0,
        100000,
        preset["txns"],
        10,
    )

    min_score = st.slider(
        "Minimum Token Score",
        0,
        100,
        preset["score"],
        1,
    )

    max_positions = st.slider(
        "Maximum Positions",
        1,
        10,
        5,
    )

    daily_loss_limit = st.number_input(
        "Daily Paper Loss Limit",
        0.01,
        10.0,
        0.2,
        0.01,
    )

    with state.lock:

        state.config.update({
            "snipe_amount": snipe_amount,
            "take_profit_pct": take_profit,
            "trailing_stop_pct": trailing_stop,
            "min_liquidity_usd": min_liquidity,
            "min_volume_24h": min_volume,
            "min_txns_24h": min_txns,
            "min_score": min_score,
            "max_positions": max_positions,
            "daily_loss_limit": daily_loss_limit,
        })

    # ------------------------------------------------------------
    # Engine
    # ------------------------------------------------------------

    st.markdown("---")
    st.markdown("### 🤖 ENGINE")

    if not state.running:

        if st.button(
            "⚡ START SCANNER",
            width="stretch",
        ):

            with state.lock:
                state.running = True

            state.log(
                "ENGINE ACTIVATED // scanner armed",
                "sys",
            )

            st.rerun()

    else:

        if st.button(
            "🛑 STOP SCANNER",
            width="stretch",
        ):

            with state.lock:
                state.running = False

            state.log(
                "ENGINE STOPPED",
                "sys",
            )

            st.rerun()

    if st.button(
        "🔍 FORCE SCAN",
        width="stretch",
    ):

        with st.spinner(
            "Scanning discovery feeds..."
        ):

            candidates = discover_all(
                state
            )

            process_candidates(
                state,
                candidates,
            )

        st.success(
            f"Scan complete: "
            f"{len(state.discovered)} watch candidates"
        )

        st.rerun()


# ================================================================
# SNAPSHOT
# ================================================================

with state.lock:

    positions = list(state.positions)
    trades = list(state.trades)
    discovered = list(state.discovered)
    rejected = list(state.rejected)
    logs = list(state.logs)
    stats = dict(state.scanner_stats)
    running = state.running
    config = dict(state.config)


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
    if float(trade.get("profit", 0)) > 0
)

win_rate = (
    wins / total_trades * 100
    if total_trades
    else 0
)

total_pnl = sum(
    float(trade.get("profit", 0))
    for trade in trades
)


m1, m2, m3, m4, m5 = st.columns(5)

with m1:

    st.markdown(
        f"""
<div class="cyber-card">

<div style="color:#888">
BALANCE
</div>

<div class="metric-value">
{wallet_balance:.5f} SOL
</div>

</div>
""",
        unsafe_allow_html=True,
    )


with m2:

    st.markdown(
        f"""
<div class="cyber-card">

<div style="color:#888">
OPEN POSITIONS
</div>

<div class="metric-value">
{len(positions)}
</div>

</div>
""",
        unsafe_allow_html=True,
    )


with m3:

    st.markdown(
        f"""
<div class="cyber-card">

<div style="color:#888">
WATCHLIST
</div>

<div class="metric-value">
{len(discovered)}
</div>

</div>
""",
        unsafe_allow_html=True,
    )


with m4:

    st.markdown(
        f"""
<div class="cyber-card">

<div style="color:#888">
WIN RATE
</div>

<div class="metric-value">
{win_rate:.1f}%
</div>

</div>
""",
        unsafe_allow_html=True,
    )


with m5:

    color = (
        "#00ff88"
        if total_pnl >= 0
        else "#ff225f"
    )

    st.markdown(
        f"""
<div class="cyber-card">

<div style="color:#888">
PAPER P/L
</div>

<div class="metric-value"
     style="color:{color}">
{total_pnl:+.5f} SOL
</div>

</div>
""",
        unsafe_allow_html=True,
    )


# ================================================================
# SCANNER STATUS
# ================================================================

st.markdown("---")

status_col1, status_col2 = st.columns([3, 1])

with status_col1:

    if running:

        st.markdown(
            """
<h2 class="status-online">
🟢 NEON ENGINE ONLINE
</h2>
""",
            unsafe_allow_html=True,
        )

    else:

        st.markdown(
            """
<h2 style="
text-align:center;
color:#ff225f;
font-family:Orbitron;
">
🔴 ENGINE STANDBY
</h2>
""",
            unsafe_allow_html=True,
        )


with status_col2:

    st.metric(
        "Last Scan",
        stats.get("last_scan") or "--",
    )


# ================================================================
# DISCOVERY SOURCES
# ================================================================

st.markdown(
    "### 📡 DISCOVERY NETWORK"
)

source_cols = st.columns(4)

sources = [
    (
        "DEXSCREENER",
        "ONLINE",
        "#00ffe6",
    ),
    (
        "BIRDEYE",
        "ONLINE" if BIRDEYE_KEY else "NO API KEY",
        "#ff00e6",
    ),
    (
        "SOLANA RPC",
        "ONLINE" if HELIUS_KEY else "NO API KEY",
        "#00ff88",
    ),
    (
        "PAPER ENGINE",
        "ACTIVE",
        "#8b00ff",
    ),
]

for col, (name, status, color) in zip(
    source_cols,
    sources,
):

    with col:

        st.markdown(
            f"""
<div class="cyber-card">

<div style="
color:{color};
font-family:Orbitron;
">
{name}
</div>

<div style="
color:#aaa;
font-size:12px;
margin-top:8px;
">
● {status}
</div>

</div>
""",
            unsafe_allow_html=True,
        )


# ================================================================
# DISCOVERED TOKENS
# ================================================================

st.markdown("---")

st.markdown(
    "### 🧬 LIVE TOKEN MATRIX"
)

if discovered:

    rows = []

    for token in discovered[:25]:

        score = token.get("score", 0)
        risk = token.get("risk", 0)

        risk_label = (
            "LOW"
            if risk < 25
            else "MEDIUM"
            if risk < 50
            else "HIGH"
        )

        rows.append({
            "TOKEN": token.get(
                "symbol",
                "UNKNOWN",
            ),

            "SCORE": score,

            "RISK": risk_label,

            "LIQUIDITY": (
                f"${token.get('liquidity', 0):,.0f}"
            ),

            "24H VOLUME": (
                f"${token.get('volume_24h', 0):,.0f}"
            ),

            "TXNS": token.get(
                "txns_24h",
                0,
            ),

            "SOURCE": " + ".join(
                token.get(
                    "sources",
                    [],
                )[:2]
            ),

            "AGE": (
                f"{token['age_hours']:.1f}h"
                if token.get("age_hours")
                is not None
                else "?"
            ),
        })

    st.dataframe(
        pd.DataFrame(rows),
        width="stretch",
        hide_index=True,
    )

else:

    st.info(
        "No candidates yet. "
        "Use FORCE SCAN or start the scanner."
    )


# ================================================================
# TOKEN DETAIL CARDS
# ================================================================

if discovered:

    st.markdown(
        "### 🎯 TOP SIGNALS"
    )

    for token in discovered[:6]:

        score = token.get("score", 0)
        risk = token.get("risk", 0)

        color = (
            "#00ff88"
            if score >= 80
            else "#00ffe6"
            if score >= 65
            else "#ff225f"
        )

        components = token.get(
            "score_components",
            {},
        )

        sources = " + ".join(
            token.get(
                "sources",
                [],
            )
        )

        st.markdown(
            f"""
<div class="cyber-card"
     style="text-align:left;">

<div style="
display:flex;
justify-content:space-between;
">

<div>

<span style="
color:#00ffe6;
font-family:Orbitron;
font-size:20px;
">
🪙 {token.get('symbol', 'UNKNOWN')}
</span>

<br>

<span style="
color:#777;
font-size:11px;
">
{token.get('mint', '')[:10]}...
{token.get('mint', '')[-8:]}
</span>

</div>

<div style="
color:{color};
font-family:Orbitron;
font-size:24px;
">
{score}/100
</div>

</div>

<div style="
color:#aaa;
margin-top:10px;
">
LIQUIDITY:
${token.get('liquidity',0):,.0f}
&nbsp; | &nbsp;
VOLUME:
${token.get('volume_24h',0):,.0f}
&nbsp; | &nbsp;
TXNS:
{token.get('txns_24h',0):,}
</div>

<div style="
color:#ff00e6;
margin-top:8px;
font-size:11px;
">
SOURCE // {sources}
</div>

<div style="
color:#aaa;
margin-top:8px;
font-size:11px;
">
RISK // {risk}/100
</div>

<div class="score-bar">

<div class="score-fill"
style="width:{score}%">
</div>

</div>

<div style="
color:#777;
font-size:10px;
">
Liquidity {components.get('liquidity',0):.0f}
|
Volume {components.get('volume',0):.0f}
|
Activity {components.get('activity',0):.0f}
|
Age {components.get('age',0):.0f}
</div>

</div>
""",
            unsafe_allow_html=True,
        )


# ================================================================
# REJECTED TOKENS
# ================================================================

with st.expander(
    "☠️ REJECTED / HIGH-RISK TOKENS"
):

    if rejected:

        rejected_rows = []

        for token in rejected[:50]:

            rejected_rows.append({
                "TOKEN": token.get(
                    "symbol",
                    "UNKNOWN",
                ),

                "SCORE": token.get(
                    "score",
                    0,
                ),

                "RISK": token.get(
                    "risk",
                    0,
                ),

                "REASON": token.get(
                    "reject_reason",
                    "unknown",
                ),

                "LIQUIDITY": (
                    f"${token.get('liquidity',0):,.0f}"
                ),

                "MINT": token.get(
                    "mint",
                    "",
                ),
            })

        st.dataframe(
            pd.DataFrame(
                rejected_rows
            ),
            width="stretch",
            hide_index=True,
        )

    else:

        st.caption(
            "No rejected candidates recorded."
        )


# ================================================================
# OPEN POSITIONS
# ================================================================

st.markdown("---")

st.markdown(
    "### 📌 OPEN POSITIONS"
)

if positions:

    for position in positions:

        pnl = position.get(
            "peak_pnl_pct",
            0,
        )

        color = (
            "#00ff88"
            if pnl >= 0
            else "#ff225f"
        )

        st.markdown(
            f"""
<div class="cyber-card">

<div style="
color:#00ffe6;
font-family:Orbitron;
font-size:18px;
">
🎯 {position['symbol']}
</div>

<div style="
color:#aaa;
margin-top:8px;
">
ENTRY:
{position['entry_sol']:.4f} SOL
</div>

<div style="
color:{color};
font-size:20px;
font-family:Orbitron;
margin-top:8px;
">
{pnl:+.2f}%
</div>

<div class="score-bar">

<div class="score-fill"
style="
width:{min(abs(pnl),100)}%;
background:
linear-gradient(
90deg,
#ff00e6,
#00ffe6
);
">
</div>

</div>

</div>
""",
            unsafe_allow_html=True,
        )

else:

    st.info(
        "No open paper positions."
    )


# ================================================================
# TERMINAL
# ================================================================

st.markdown("---")

st.markdown(
    "### 🖥️ NEON TERMINAL"
)

terminal_html = "".join(logs)

if not terminal_html:

    terminal_html = (
        '<div class="sys">'
        '// awaiting scanner telemetry...'
        '</div>'
    )

st.markdown(
    f"""
<div class="terminal">
{terminal_html}
</div>
""",
    unsafe_allow_html=True,
)


# ================================================================
# SCANNER STATISTICS
# ================================================================

st.markdown("---")

st.markdown(
    "### 📊 SCANNER TELEMETRY"
)

c1, c2, c3, c4 = st.columns(4)

with c1:
    st.metric(
        "Scans",
        stats.get("scans", 0),
    )

with c2:
    st.metric(
        "Raw Candidates",
        stats.get(
            "raw_candidates",
            0,
        ),
    )

with c3:
    st.metric(
        "Accepted",
        stats.get(
            "accepted",
            0,
        ),
    )

with c4:
    st.metric(
        "Rejected",
        stats.get(
            "rejected",
            0,
        ),
    )


# ================================================================
# TRADE HISTORY
# ================================================================

st.markdown("---")

st.markdown(
    "### 📜 PAPER TRADE HISTORY"
)

if trades:

    trade_rows = []

    for trade in trades[-50:][::-1]:

        trade_rows.append({
            "DATE": trade.get(
                "date",
                "",
            ),

            "TIME": trade.get(
                "time",
                "",
            ),

            "TOKEN": trade.get(
                "symbol",
                "",
            ),

            "ENTRY": trade.get(
                "entry_sol",
                0,
            ),

            "EXIT": trade.get(
                "exit_sol",
                0,
            ),

            "P/L": trade.get(
                "profit",
                0,
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

    st.info(
        "No paper trades yet."
    )


# ================================================================
# PNL CALENDAR
# ================================================================

st.markdown("---")

st.markdown(
    "### 📅 PNL CALENDAR"
)

calendar_rows = []

today = datetime.now()

for i in range(30):

    day = today - timedelta(days=i)

    date_string = day.strftime(
        "%Y-%m-%d"
    )

    day_trades = [
        t
        for t in trades
        if t.get("date") == date_string
    ]

    pnl = sum(
        float(
            t.get(
                "profit",
                0,
            )
        )
        for t in day_trades
    )

    wins_day = sum(
        1
        for t in day_trades
        if float(
            t.get(
                "profit",
                0,
            )
        ) > 0
    )

    win_rate_day = (
        wins_day / len(day_trades) * 100
        if day_trades
        else 0
    )

    calendar_rows.append({
        "DATE": day.strftime(
            "%m/%d"
        ),

        "P/L SOL": f"{pnl:+.6f}",

        "TRADES": len(day_trades),

        "WIN RATE": (
            f"{win_rate_day:.0f}%"
        ),
    })

st.dataframe(
    pd.DataFrame(calendar_rows),
    width="stretch",
    hide_index=True,
)


# ================================================================
# FOOTER
# ================================================================

st.markdown(
    """
<div style="
text-align:center;
padding:25px;
color:#00ffe6;
font-family:'Share Tech Mono';
">

<div style="
font-family:Orbitron;
letter-spacing:3px;
">
CYBER SNIPER // NEON HUNTER v7.0
</div>

<div style="
font-size:10px;
color:#777;
margin-top:8px;
">
MULTI-SOURCE DISCOVERY // RISK ENGINE //
PAPER EXECUTION // SOLANA
</div>

</div>
""",
    unsafe_allow_html=True,
)


# ================================================================
# AUTO REFRESH
# ================================================================

if HAS_AUTOREFRESH:

    st_autorefresh(
        interval=6000,
        key="cyber_refresh",
    )

else:

    st.caption(
        "Install streamlit-autorefresh for automatic dashboard updates."
    )
```

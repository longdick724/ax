# app.py - J.A.R.V.I.S. // Stark Industries HUD - Multi-AI Solana Memecoin Engine
# Full script ready for GitHub
import os
import json
import time
import base64
import threading
import re
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
from flask import Flask, render_template_string, jsonify, request
import requests
from dotenv import load_dotenv

load_dotenv()

try:
    from solders.keypair import Keypair
    from solders.transaction import VersionedTransaction
    SOLDERS_AVAILABLE = True
except ImportError:
    SOLDERS_AVAILABLE = False
    Keypair = None
    VersionedTransaction = None

# ================================================================
# CONFIG
# ================================================================
APP_VERSION = "20.0 STARK HUD MULTI-AI"
SOL_MINT = "So11111111111111111111111111111111111111112"

def get_toronto_time():
    """Toronto: UTC-5 EST winter, UTC-4 EDT summer (2nd Sun Mar → 1st Sun Nov)."""
    now = datetime.now(timezone.utc)
    year = now.year
    mar8 = datetime(year, 3, 8)
    march_2nd_sun = mar8 + timedelta(days=(6 - mar8.weekday()) % 7)
    nov1 = datetime(year, 11, 1)
    nov_1st_sun = nov1 + timedelta(days=(6 - nov1.weekday()) % 7)
    is_dst = march_2nd_sun.replace(tzinfo=timezone.utc) <= now < nov_1st_sun.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone(timedelta(hours=-4 if is_dst else -5)))

HELIUS_KEY = os.getenv("HELIUS_KEY", "").strip()
FREELLM_API_KEY = os.getenv(
    "FREELLM_API_KEY",
    "freellmapi-9cb5353c69c403f8fe383633e4bf373d4c8838d3feef63ee",
).strip()
ENV_PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY", "").strip()
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY", "c86fe0a2e3ad4af2b5409d49a81f3cf8").strip()

HELIUS_RPC_URL = (
    f"https://mainnet.helius-rpc.com/?api-key={HELIUS_KEY}"
    if HELIUS_KEY
    else "https://api.mainnet-beta.solana.com"
)
JUPITER_API_BASE = "https://quote-api.jup.ag/v6"
DEXSCREENER_API = "https://api.dexscreener.com"
BIRDEYE_API = "https://public-api.birdeye.so"
FREELLM_BASE = os.getenv("FREELLM_BASE", "https://api.freellmapi.com/v1").rstrip("/")
FREELLM_BASES = [
    FREELLM_BASE,
    "https://api.freellmapi.com/v1",
    "http://127.0.0.1:3001/v1",
    "http://localhost:3001/v1",
]
# dedupe
_seen = set()
FREELLM_BASES = [b for b in FREELLM_BASES if not (b in _seen or _seen.add(b))]
COINGECKO_API = "https://api.coingecko.com/api/v3"
STATE_FILE = "jarvis_state.json"
MAX_TRADE_SOL_CAP = 0.50

# ================================================================
# AI MODELS (FreeLLM pools)
# ================================================================
AI_MODELS = {
    "groq/gpt-oss-120b": {"name": "GPT-OSS 120B", "role": "Deep Analysis"},
    "groq/gpt-oss-20b": {"name": "GPT-OSS 20B", "role": "Quick Scoring"},
    "groq/compound": {"name": "Compound", "role": "Strategy"},
    "groq/compound-mini": {"name": "Compound Mini", "role": "Fast Decisions"},
    "groq/gpt-oss-safeguard-20b": {"name": "Safeguard 20B", "role": "Safety"},
    "groq/allam-2-7b": {"name": "ALLaM 2 7B", "role": "Multilingual"},
    "groq/qwen3.6-27b": {"name": "Qwen 3.6 27B", "role": "Patterns"},
    "openrouter/nemotron-3-super-120b": {"name": "Nemotron 120B", "role": "Risk"},
    "openrouter/gemma-4-31b": {"name": "Gemma 31B", "role": "Sentiment"},
    "openrouter/gemma-4-26b-a4b": {"name": "Gemma 26B", "role": "Quick"},
    "openrouter/nemotron-3-nano-30b": {"name": "Nemotron Nano", "role": "Reasoning"},
    "openrouter/north-mini-code": {"name": "North Mini", "role": "Code"},
    "openrouter/poolside-laguna-s-2.1": {"name": "Laguna S", "role": "Strategy"},
    "openrouter/poolside-laguna-xs-2.1": {"name": "Laguna XS", "role": "Fast Strat"},
}

class MultiAIEngine:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.model_status: Dict[str, bool] = {}
        self.cache: Dict[str, Dict] = {}
        self.cache_ttl = 45
        self._lock = threading.Lock()
        threading.Thread(target=self._bg_test, daemon=True).start()

    def _bg_test(self):
        # Prefer router "auto" then named models
        for mid in ["auto", "auto:fast"] + list(AI_MODELS.keys())[:4]:
            ok = self.query(mid, "Reply with only: OK", 8) is not None
            self.model_status[mid] = ok
            if ok:
                break
            time.sleep(0.2)

    def query(self, model: str, prompt: str, max_tokens: int = 300) -> Optional[str]:
        if not self.api_key:
            return None
        key = f"{model}:{hash(prompt)}"
        with self._lock:
            if key in self.cache and time.time() - self.cache[key]["t"] < self.cache_ttl:
                return self.cache[key]["r"]
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.25,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        for base in FREELLM_BASES:
            try:
                r = requests.post(
                    f"{base}/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=18,
                    verify=False,
                )
                if r.status_code == 200:
                    content = (
                        r.json().get("choices", [{}])[0].get("message", {}).get("content", "")
                    )
                    if content:
                        with self._lock:
                            self.cache[key] = {"t": time.time(), "r": content}
                        self.model_status[model] = True
                        return content
            except Exception:
                continue
        self.model_status[model] = False
        return None

    def get_multi_ai_consensus(self, token_data: Dict) -> Optional[float]:
        prompt = (
            "Score this Solana memecoin 0-100 for short-term trade potential. "
            "Return ONLY an integer 0-100.\n"
            f"Symbol: {token_data.get('symbol')}\n"
            f"Liquidity: ${token_data.get('liquidity', 0):,.0f}\n"
            f"Vol24h: ${token_data.get('volume_24h', 0):,.0f}\n"
            f"Buys: {token_data.get('buys_24h', 0)} Sells: {token_data.get('sells_24h', 0)}\n"
            f"MC: ${token_data.get('market_cap', 0):,.0f}"
        )
        models = [
            "groq/gpt-oss-20b",
            "groq/compound-mini",
            "groq/qwen3.6-27b",
            "openrouter/gemma-4-26b-a4b",
            "openrouter/nemotron-3-nano-30b",
            "openrouter/poolside-laguna-xs-2.1",
        ]
        scores = []
        for m in models:
            resp = self.query(m, prompt, 20)
            if resp:
                for n in re.findall(r"\b([0-9]{1,3})\b", resp):
                    s = int(n)
                    if 0 <= s <= 100:
                        scores.append(s)
                        break
        return sum(scores) / len(scores) if scores else None

    def get_strategy_advice(self, perf: Dict) -> List[str]:
        prompt = (
            "Optimize memecoin sniper. Reply: TP=XX TRAIL=YY MINLIQ=ZZ MINSCORE=WW\n"
            f"Trades:{perf.get('total_trades',0)} WR:{perf.get('win_rate',0):.1f}% "
            f"PnL:{perf.get('net_pnl',0):+.4f} SOL"
        )
        out = []
        for m in ["groq/compound", "openrouter/nemotron-3-super-120b", "openrouter/poolside-laguna-s-2.1"]:
            r = self.query(m, prompt, 80)
            if r:
                out.append(r.strip())
        return out

    def jarvis_speak(self, prompt: str) -> str:
        sys = (
            "You are J.A.R.V.I.S., Stark Industries AI. Concise, professional, dry wit. "
            "You manage a Solana memecoin trading core. Answer status, risk, strategy."
        )
        full = f"{sys}\n\nUser: {prompt}\nJARVIS:"
        for m in ["auto", "auto:smart", "groq/gpt-oss-120b", "openrouter/nemotron-3-super-120b", "groq/compound"]:
            r = self.query(m, full, 450)
            if r:
                return r.strip()
        # Local fallback when FreeLLM unreachable
        low = prompt.lower()
        if any(w in low for w in ("status", "online", "running")):
            return (
                f"Engine is {'ONLINE' if state.running else 'STANDBY'}. "
                f"Positions: {len(state.positions)}. Trades logged: {len(state.trades)}. "
                "FreeLLM link is down — using local heuristics until the API responds."
            )
        if "win" in low or "pnl" in low or "profit" in low:
            wins = sum(1 for t in state.trades if (t.get('profit') or 0) > 0)
            total = len(state.trades)
            net = sum(t.get('profit') or 0 for t in state.trades)
            wr = (wins / total * 100) if total else 0
            return f"Win rate {wr:.1f}% ({wins}/{total}). Net P/L {net:+.4f} SOL."
        if "help" in low or "command" in low:
            return "Commands: status, win rate, engage engine, positions, strategy. FreeLLM will power deeper analysis when connected."
        return (
            "FreeLLM core is unreachable from this host (check FREELLM_BASE / your FreeLLM server). "
            "HUD, DexScreener, Birdeye, and rule-based scoring remain active. Standing by, sir."
        )

# ================================================================
# STATE
# ================================================================
@dataclass
class EngineState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    running: bool = False
    wallet: Any = None
    wallet_address: str = ""
    positions: List[Dict] = field(default_factory=list)
    trades: List[Dict] = field(default_factory=list)
    logs: List[str] = field(default_factory=list)
    config: Dict = field(
        default_factory=lambda: {
            "snipe_amount": 0.05,
            "take_profit_pct": 45.0,
            "trailing_stop_pct": 12.0,
            "min_liquidity_usd": 12000.0,
            "min_volume_24h": 5000.0,
            "max_positions": 5,
            "min_score": 58,
            "max_trade_sol": 0.50,
        }
    )

    def log(self, msg: str):
        ts = get_toronto_time().strftime("%H:%M:%S")
        entry = f"[{ts}] {msg}"
        with self.lock:
            self.logs.append(entry)
            if len(self.logs) > 200:
                self.logs = self.logs[-150:]
        print(entry)

state = EngineState()
if os.path.exists(STATE_FILE):
    try:
        with open(STATE_FILE) as f:
            d = json.load(f)
        state.positions = d.get("positions", [])
        state.trades = d.get("trades", [])
        if isinstance(d.get("config"), dict):
            state.config.update(d["config"])
        if d.get("config"):
            state.config.update(d["config"])
    except Exception:
        pass

def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({
                "positions": state.positions,
                "trades": state.trades,
                "config": state.config,
            }, f)
    except Exception:
        pass

HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36", "Accept": "application/json"})

# ================================================================
# TOKEN DISCOVERY
# ================================================================
def discover_tokens_multi_source() -> List[Dict]:
    tokens: List[Dict] = []

    try:
        r = HTTP.get(f"{DEXSCREENER_API}/token-boosts/latest/v1", timeout=8)
        if r.status_code == 200:
            boosts = r.json() if isinstance(r.json(), list) else []
            mints = [x.get("tokenAddress") for x in boosts if x.get("chainId") == "solana" and x.get("tokenAddress")][:40]
            for i in range(0, len(mints), 30):
                batch = mints[i : i + 30]
                r2 = HTTP.get(f"{DEXSCREENER_API}/tokens/v1/solana/{','.join(batch)}", timeout=10)
                if r2.status_code == 200:
                    for pair in (r2.json() if isinstance(r2.json(), list) else []):
                        if pair.get("chainId") != "solana":
                            continue
                        base = pair.get("baseToken") or {}
                        liq = pair.get("liquidity") or {}
                        vol = pair.get("volume") or {}
                        txns = (pair.get("txns") or {}).get("h24") or {}
                        tokens.append({
                            "mint": base.get("address"),
                            "symbol": base.get("symbol") or "UNKNOWN",
                            "liquidity": float(liq.get("usd") or 0),
                            "volume_24h": float(vol.get("h24") or 0),
                            "buys_24h": int(txns.get("buys") or 0),
                            "sells_24h": int(txns.get("sells") or 0),
                            "market_cap": float(pair.get("marketCap") or 0),
                            "price_change_h1": float((pair.get("priceChange") or {}).get("h1") or 0),
                            "source": "dexscreener",
                        })
    except Exception as e:
        state.log(f"DexScreener: {e}")

    # DexScreener multi-query (real pairs)
    for q in ("SOL", "pump", "bonk", "meme"):
        try:
            r = HTTP.get(
                f"{DEXSCREENER_API}/latest/dex/search",
                params={"q": q},
                timeout=10,
            )
            if r.status_code != 200:
                continue
            pairs = (r.json() or {}).get("pairs") or []
            for pair in pairs[:20]:
                if pair.get("chainId") != "solana":
                    continue
                base = pair.get("baseToken") or {}
                liq = pair.get("liquidity") or {}
                vol = pair.get("volume") or {}
                txns = (pair.get("txns") or {}).get("h24") or {}
                tokens.append({
                    "mint": base.get("address"),
                    "symbol": base.get("symbol") or "UNKNOWN",
                    "liquidity": float(liq.get("usd") or 0),
                    "volume_24h": float(vol.get("h24") or 0),
                    "buys_24h": int(txns.get("buys") or 0),
                    "sells_24h": int(txns.get("sells") or 0),
                    "market_cap": float(pair.get("marketCap") or 0),
                    "price_change_h1": float((pair.get("priceChange") or {}).get("h1") or 0),
                    "source": "dexscreener",
                })
            time.sleep(0.35)  # soft rate limit
        except Exception:
            pass

    if BIRDEYE_API_KEY:
        headers = {"X-API-KEY": BIRDEYE_API_KEY, "x-chain": "solana"}
        for ep, params in [
            ("/defi/v2/tokens/new_listing", {"limit": 20, "meme_platform_enabled": "true"}),
            ("/defi/token_trending", {"sort_by": "rank", "sort_type": "asc", "offset": 0, "limit": 20}),
        ]:
            try:
                r = HTTP.get(f"{BIRDEYE_API}{ep}", headers=headers, params=params, timeout=10)
                if r.status_code == 200:
                    data = (r.json() or {}).get("data") or {}
                    items = data.get("items") or data.get("tokens") or (data if isinstance(data, list) else [])
                    for t in items:
                        tokens.append({
                            "mint": t.get("address") or t.get("mint") or t.get("token_address"),
                            "symbol": t.get("symbol") or "UNKNOWN",
                            "liquidity": float(t.get("liquidity") or t.get("liquidityUsd") or 0),
                            "volume_24h": float(t.get("volume24hUSD") or t.get("v24hUSD") or t.get("volume_24h") or 0),
                            "buys_24h": int(t.get("buy24h") or t.get("buy_24h") or 0),
                            "sells_24h": int(t.get("sell24h") or t.get("sell_24h") or 0),
                            "market_cap": float(t.get("marketCap") or t.get("mc") or 0),
                            "source": "birdeye",
                        })
            except Exception as e:
                state.log(f"Birdeye: {e}")

    # GMGN real quotation endpoints
    gmgn_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Referer": "https://gmgn.ai/",
        "Origin": "https://gmgn.ai",
    }
    gmgn_paths = [
        ("/pairs/new_pairs", {"chain": "sol", "limit": 30}),
        ("/rank/sol/swaps/1h", {"limit": 30, "orderby": "volume"}),
        ("/rank/sol/swaps/6h", {"limit": 20}),
        ("/tokens/sol/trending", {"limit": 20}),
    ]
    for path, params in gmgn_paths:
        try:
            r = HTTP.get(
                f"https://gmgn.ai/defi/quotation/v1{path}",
                params=params,
                headers=gmgn_headers,
                timeout=10,
            )
            if r.status_code != 200:
                continue
            body = r.json() or {}
            data = body.get("data") or body
            items = (
                data.get("pairs")
                or data.get("rank")
                or data.get("list")
                or data.get("tokens")
                or (data if isinstance(data, list) else [])
            )
            if isinstance(items, dict):
                items = list(items.values())
            for p in items[:25]:
                if not isinstance(p, dict):
                    continue
                mint = (
                    p.get("address")
                    or p.get("base_address")
                    or p.get("token_address")
                    or p.get("base_token_address")
                    or (p.get("base_token") or {}).get("address")
                )
                tokens.append({
                    "mint": mint,
                    "symbol": p.get("symbol") or p.get("base_symbol") or p.get("token_symbol") or "UNKNOWN",
                    "liquidity": float(p.get("liquidity") or p.get("liquidity_usd") or p.get("liq") or 0),
                    "volume_24h": float(p.get("volume_24h") or p.get("volume") or p.get("v24h") or 0),
                    "buys_24h": int(p.get("buys_24h") or p.get("buys") or p.get("buy_count") or 0),
                    "sells_24h": int(p.get("sells_24h") or p.get("sells") or p.get("sell_count") or 0),
                    "market_cap": float(p.get("market_cap") or p.get("mc") or p.get("fdv") or 0),
                    "source": "gmgn",
                })
        except Exception:
            pass

    best: Dict[str, Dict] = {}
    for t in tokens:
        mint = t.get("mint")
        if not mint or not isinstance(mint, str) or len(mint) < 32 or mint == SOL_MINT:
            continue
        prev = best.get(mint)
        if not prev or (t.get("liquidity") or 0) > (prev.get("liquidity") or 0):
            best[mint] = t
    unique = list(best.values())
    unique.sort(key=lambda x: (x.get("liquidity") or 0) * 0.4 + (x.get("volume_24h") or 0) * 0.6, reverse=True)
    return unique[:60]

def rpc_call(method: str, params: list):
    try:
        r = HTTP.post(HELIUS_RPC_URL, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=15)
        if r.status_code == 200:
            return r.json().get("result")
    except Exception:
        pass
    return None

def get_balance(pubkey: str) -> float:
    if not pubkey:
        return 0.0
    result = rpc_call("getBalance", [pubkey])
    if result and "value" in result:
        return float(result["value"]) / 1e9
    return 0.0

def jupiter_quote(input_mint, output_mint, amount, slippage_bps=800):
    try:
        r = HTTP.get(
            f"{JUPITER_API_BASE}/quote",
            params={"inputMint": input_mint, "outputMint": output_mint, "amount": amount, "slippageBps": slippage_bps},
            timeout=12,
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None

def jupiter_swap(quote, wallet) -> Optional[str]:
    if not SOLDERS_AVAILABLE or wallet is None:
        return None
    try:
        r = HTTP.post(
            f"{JUPITER_API_BASE}/swap",
            json={
                "quoteResponse": quote,
                "userPublicKey": str(wallet.pubkey()),
                "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
            },
            timeout=20,
        )
        if r.status_code != 200:
            return None
        raw = base64.b64decode(r.json()["swapTransaction"])
        unsigned = VersionedTransaction.from_bytes(raw)
        signed = VersionedTransaction(unsigned.message, [wallet])
        encoded = base64.b64encode(bytes(signed)).decode()
        r2 = HTTP.post(
            HELIUS_RPC_URL,
            json={"jsonrpc": "2.0", "id": 1, "method": "sendTransaction", "params": [encoded, {"encoding": "base64", "skipPreflight": True}]},
            timeout=25,
        )
        if r2.status_code == 200:
            result = r2.json().get("result")
            if result:
                time.sleep(1.5)
                return result
    except Exception as e:
        state.log(f"Swap: {e}")
    return None

def get_live_prices() -> Dict:
    try:
        r = HTTP.get(
            f"{COINGECKO_API}/simple/price",
            params={
                "ids": "bitcoin,ethereum,solana,binancecoin,ripple,cardano,dogecoin",
                "vs_currencies": "usd",
                "include_24hr_change": "true",
            },
            timeout=8,
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return {}

def score_token(token: Dict, ai_engine: MultiAIEngine) -> int:
    liq = token.get("liquidity") or 0
    vol = token.get("volume_24h") or 0
    buys = token.get("buys_24h") or 0
    sells = token.get("sells_24h") or 0
    mc = token.get("market_cap") or 0
    chg = token.get("price_change_h1") or 0
    score = 0.0
    if liq < state.config["min_liquidity_usd"]:
        return 0
    if liq >= 50000:
        score += 28
    elif liq >= 25000:
        score += 22
    else:
        score += 15
    if vol >= 50000:
        score += 22
    elif vol >= 15000:
        score += 16
    elif vol >= state.config["min_volume_24h"]:
        score += 10
    else:
        score += 2
    if sells > 0 and buys > sells * 2.0:
        score += 18
    elif sells > 0 and buys > sells * 1.4:
        score += 12
    elif buys > sells:
        score += 6
    if 20000 <= mc <= 2_000_000:
        score += 12
    elif 5000 <= mc < 20000 or 2e6 < mc <= 8e6:
        score += 5
    if 5 <= chg <= 80:
        score += 8
    elif chg > 80:
        score += 2
    src = token.get("source")
    if src == "birdeye":
        score += 6
    elif src == "gmgn":
        score += 5
    elif src == "dexscreener":
        score += 4
    ai = ai_engine.get_multi_ai_consensus(token)
    if ai is not None:
        score = score * 0.35 + ai * 0.65
    return int(max(0, min(100, round(score))))

def engine_loop(ai_engine: MultiAIEngine):
    state.log(f"JARVIS {APP_VERSION} online")
    cycle = 0
    while True:
        try:
            if not state.running:
                time.sleep(2)
                continue
            cycle += 1
            for pos in list(state.positions):
                try:
                    quote = jupiter_quote(pos["mint"], SOL_MINT, pos.get("out_amount") or 1, 1000)
                    if not quote:
                        continue
                    current = int(quote.get("outAmount") or 0) / 1e9
                    entry = pos.get("entry_sol") or 0.01
                    if entry <= 0:
                        continue
                    pnl = ((current - entry) / entry) * 100
                    pos["current_pnl"] = pnl
                    peak = pos.get("peak_pnl", pnl)
                    if pnl > peak:
                        pos["peak_pnl"] = pnl
                        peak = pnl
                    sell = False
                    reason = ""
                    if pnl >= state.config["take_profit_pct"]:
                        sell, reason = True, f"TP {pnl:.1f}%"
                    elif peak > 15 and (peak - pnl) >= state.config["trailing_stop_pct"]:
                        sell, reason = True, f"TRAIL {peak:.1f}%->{pnl:.1f}%"
                    elif pnl <= -state.config["trailing_stop_pct"]:
                        sell, reason = True, f"SL {pnl:.1f}%"
                    if sell and state.wallet:
                        result = rpc_call(
                            "getTokenAccountsByOwner",
                            [state.wallet_address, {"mint": pos["mint"]}, {"encoding": "jsonParsed"}],
                        )
                        if result and result.get("value"):
                            try:
                                amount = int(result["value"][0]["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
                                if amount > 0:
                                    sq = jupiter_quote(pos["mint"], SOL_MINT, amount, 1200)
                                    if sq:
                                        sig = jupiter_swap(sq, state.wallet)
                                        if sig:
                                            exit_sol = int(sq.get("outAmount") or 0) / 1e9
                                            profit = exit_sol - entry
                                            with state.lock:
                                                if pos in state.positions:
                                                    state.positions.remove(pos)
                                                state.trades.append({
                                                    "date": get_toronto_time().strftime("%Y-%m-%d %H:%M"),
                                                    "symbol": pos.get("symbol"),
                                                    "profit": profit,
                                                    "pnl_pct": pnl,
                                                    "reason": reason,
                                                })
                                            state.log(f"SOLD {pos.get('symbol')} {profit:+.4f} SOL ({reason})")
                                            save_state()
                            except Exception as e:
                                state.log(f"Sell: {e}")
                except Exception as e:
                    state.log(f"Pos: {e}")

            if len(state.positions) < state.config["max_positions"] and state.wallet:
                tokens = discover_tokens_multi_source()
                scored = []
                for token in tokens:
                    if any(p.get("mint") == token.get("mint") for p in state.positions):
                        continue
                    s = score_token(token, ai_engine)
                    token["score"] = s
                    if s >= state.config["min_score"]:
                        scored.append(token)
                scored.sort(key=lambda x: x["score"], reverse=True)
                for token in scored[:3]:
                    if len(state.positions) >= state.config["max_positions"]:
                        break
                    amt = min(state.config["snipe_amount"], state.config.get("max_trade_sol", MAX_TRADE_SOL_CAP))
                    q = jupiter_quote(SOL_MINT, token["mint"], int(amt * 1e9), 900)
                    if not q:
                        continue
                    sig = jupiter_swap(q, state.wallet)
                    if sig:
                        with state.lock:
                            state.positions.append({
                                "mint": token["mint"],
                                "symbol": token["symbol"],
                                "entry_sol": amt,
                                "out_amount": int(q.get("outAmount") or 0),
                                "score": token["score"],
                                "source": token.get("source"),
                                "peak_pnl": 0.0,
                                "entry_time": get_toronto_time().isoformat(),
                            })
                        state.log(f"BOUGHT {token['symbol']} score={token['score']} src={token.get('source')}")
                        save_state()
                        time.sleep(1.2)

            if cycle % 20 == 0 and len(state.trades) >= 5:
                total = len(state.trades)
                wins = sum(1 for t in state.trades if t.get("profit", 0) > 0)
                wr = wins / total * 100
                net = sum(t.get("profit", 0) for t in state.trades)
                for a in ai_engine.get_strategy_advice({"total_trades": total, "win_rate": wr, "net_pnl": net}):
                    for pat, key, lo, hi in [
                        (r"TP\s*=\s*([\d.]+)", "take_profit_pct", 15, 120),
                        (r"TRAIL\s*=\s*([\d.]+)", "trailing_stop_pct", 5, 40),
                        (r"MINLIQ\s*=\s*([\d.]+)", "min_liquidity_usd", 3000, 100000),
                        (r"MINSCORE\s*=\s*([\d.]+)", "min_score", 40, 85),
                    ]:
                        m = re.search(pat, a, re.I)
                        if m:
                            v = float(m.group(1))
                            if lo <= v <= hi:
                                state.config[key] = int(v) if key == "min_score" else v
                state.log(f"Self-tune applied WR={wr:.1f}%")

            time.sleep(8)
        except Exception as e:
            state.log(f"Engine: {e}")
            time.sleep(4)

# ================================================================
# STARK INDUSTRIES HUD
# ================================================================
app = Flask(__name__)

HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>J.A.R.V.I.S. — Stark Industries</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@400;700;900&family=Share+Tech+Mono&display=swap');
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:100%;height:100%;overflow:hidden}
body{
  background:#000814;height:100vh;
  font-family:'Share Tech Mono',monospace;color:#00d4ff;
  -webkit-font-smoothing:antialiased;
}
canvas{position:fixed;top:0;left:0;z-index:1;width:100%;height:100%}

.top-bar{
  position:fixed;top:0;left:0;right:0;height:52px;z-index:30;
  background:linear-gradient(180deg,rgba(0,40,80,.95),rgba(0,20,40,.75));
  border-bottom:1px solid rgba(0,180,255,.4);
  display:flex;align-items:center;justify-content:space-between;
  padding:0 28px;font-size:15px;letter-spacing:3px
}
.top-bar .brand{font-family:'Orbitron',sans-serif;font-weight:700;font-size:18px;color:#00e5ff;text-shadow:0 0 16px #00b4ff}
.top-bar .clock{color:#7fdfff;font-size:15px}

.panel{
  position:fixed;z-index:20;
  background:rgba(0,18,42,.85);
  border:1px solid rgba(0,180,255,.55);
  box-shadow:0 0 28px rgba(0,150,255,.22),inset 0 0 40px rgba(0,100,200,.1);
  backdrop-filter:blur(8px);
  padding:20px 24px;border-radius:4px;min-width:260px
}
.panel::before{
  content:'';position:absolute;top:0;left:0;width:16px;height:16px;
  border-top:3px solid #00d4ff;border-left:3px solid #00d4ff
}
.panel::after{
  content:'';position:absolute;bottom:0;right:0;width:16px;height:16px;
  border-bottom:3px solid #00d4ff;border-right:3px solid #00d4ff
}
.pt{font-size:14px;letter-spacing:3px;color:#00b4ff;margin-bottom:8px;opacity:.9;text-transform:uppercase}
.pv{font-size:36px;font-weight:700;color:#e8f9ff;text-shadow:0 0 14px rgba(0,180,255,.55);font-family:'Orbitron',sans-serif;line-height:1.15}
.ps{font-size:14px;color:#5aa0c8;margin-top:6px}

#p-bal{top:70px;left:28px}
#p-pos{top:210px;left:28px}
#p-wr{top:70px;right:28px}
#p-pnl{top:210px;right:28px}
#p-time{top:70px;left:50%;transform:translateX(-50%);text-align:center;min-width:280px}
#p-ai{bottom:280px;left:28px}
#p-mkt{bottom:280px;right:28px}
#p-logs{
  bottom:140px;left:28px;width:340px;max-height:160px;overflow:auto;
  font-size:13px;line-height:1.45;color:#7ec0e0;min-width:340px
}
#p-logs div{opacity:.9;margin-bottom:4px}

.gauge-wrap{
  position:fixed;z-index:18;left:28px;top:360px;
  display:flex;flex-direction:column;gap:16px
}
.gauge{width:130px;height:130px;position:relative}
.gauge canvas{position:absolute;top:0;left:0}
.gauge .glabel{
  position:absolute;inset:0;display:flex;flex-direction:column;
  align-items:center;justify-content:center;font-size:12px;color:#00b4ff
}
.gauge .gval{font-size:22px;font-weight:700;color:#fff;font-family:'Orbitron',sans-serif}

.core-label{
  position:fixed;top:50%;left:50%;transform:translate(-50%,260px);
  z-index:12;text-align:center;pointer-events:none
}
.core-label .main{
  font-family:'Orbitron',sans-serif;font-size:26px;letter-spacing:12px;
  color:#00e5ff;text-shadow:0 0 24px #00b4ff
}
.core-label .sub{font-size:13px;color:#5aa0c8;letter-spacing:4px;margin-top:8px}

.controls{
  position:fixed;top:120px;left:50%;transform:translateX(-50%);z-index:35;
  display:flex;gap:12px
}
.btn{
  background:rgba(0,30,60,.9);border:1px solid #00b4ff;color:#00d4ff;
  padding:14px 32px;font-size:15px;letter-spacing:3px;cursor:pointer;
  font-family:'Share Tech Mono',monospace;transition:all .2s;
  box-shadow:0 0 16px rgba(0,150,255,.25)
}
.btn:hover{background:#00b4ff;color:#001020;box-shadow:0 0 28px #00b4ff}
.btn.active{background:rgba(0,180,255,.3);border-color:#00e5ff}

.chat-box{
  position:fixed;bottom:20px;left:50%;transform:translateX(-50%);
  width:min(94%,720px);z-index:40;display:flex;flex-direction:column;gap:8px
}
#messages{
  max-height:200px;overflow-y:auto;display:flex;flex-direction:column;gap:6px;
  scrollbar-width:thin;scrollbar-color:rgba(0,180,255,.4) transparent
}
.msg{
  background:rgba(0,25,50,.92);border-left:3px solid #00b4ff;
  padding:12px 16px;font-size:15px;color:#c8e8ff;line-height:1.45;
  animation:fadeIn .2s ease
}
.msg.you{border-left-color:#40a0ff}
.msg.jarvis{border-left-color:#00e5ff}
.msg b{color:#00d4ff}
.msg.typing{opacity:.65;font-style:italic}
.chat-input{
  width:100%;background:rgba(0,20,45,.96);border:1px solid #00b4ff;
  padding:18px 20px;color:#00d4ff;font-size:16px;outline:none;
  font-family:'Share Tech Mono',monospace;letter-spacing:.5px;
  box-shadow:0 0 22px rgba(0,150,255,.28)
}
.chat-input:focus{border-color:#00e5ff;box-shadow:0 0 32px rgba(0,180,255,.45)}
.chat-input::placeholder{color:rgba(0,180,255,.4)}

.footer{
  position:fixed;bottom:6px;left:50%;transform:translateX(-50%);
  font-family:'Orbitron',sans-serif;font-size:10px;letter-spacing:5px;
  color:rgba(0,180,255,.35);z-index:10
}

/* SETTINGS MODAL */
.settings-overlay{
  display:none;position:fixed;inset:0;z-index:100;
  background:rgba(0,8,20,.72);backdrop-filter:blur(6px);
  align-items:center;justify-content:center
}
.settings-overlay.open{display:flex}
.settings-panel{
  width:min(92%,520px);background:rgba(0,18,42,.96);
  border:1px solid rgba(0,180,255,.6);
  box-shadow:0 0 40px rgba(0,150,255,.3);
  padding:28px 32px;border-radius:4px;position:relative
}
.settings-panel h2{
  font-family:'Orbitron',sans-serif;font-size:20px;letter-spacing:4px;
  color:#00e5ff;margin-bottom:20px;text-shadow:0 0 12px #00b4ff
}
.set-row{
  display:flex;align-items:center;justify-content:space-between;
  margin-bottom:14px;gap:16px
}
.set-row label{font-size:14px;color:#7ec0e0;letter-spacing:1px;flex:1}
.set-row input{
  width:140px;background:rgba(0,30,60,.9);border:1px solid #00b4ff;
  color:#00e5ff;padding:10px 12px;font-size:15px;font-family:'Share Tech Mono',monospace;
  outline:none;text-align:right
}
.set-row input:focus{border-color:#00e5ff;box-shadow:0 0 12px rgba(0,180,255,.35)}
.set-actions{display:flex;gap:12px;margin-top:22px;justify-content:flex-end}
.set-actions .btn{padding:12px 24px;font-size:13px}
.set-hint{font-size:12px;color:#4a90b8;margin-top:12px;line-height:1.4}

@keyframes fadeIn{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}

@media(max-width:900px){
  .panel{padding:14px 16px;min-width:160px}
  .pv{font-size:24px}
  .pt{font-size:11px}
  #p-logs,.gauge-wrap,#p-mkt{display:none}
  .core-label{transform:translate(-50%,160px)}
  .core-label .main{font-size:18px}
}
@media(min-width:2560px){
  .pv{font-size:44px}
  .pt{font-size:16px}
  .ps{font-size:16px}
  .panel{padding:24px 28px;min-width:320px}
  .btn{padding:16px 36px;font-size:17px}
  .chat-input{font-size:18px;padding:20px}
  .msg{font-size:17px}
  .core-label .main{font-size:32px}
  .top-bar{height:60px;font-size:17px}
  .top-bar .brand{font-size:22px}
}
@media(min-width:3840px){
  .pv{font-size:56px}
  .pt{font-size:20px}
  .ps{font-size:18px}
  .panel{padding:28px 34px;min-width:400px}
  .btn{padding:20px 44px;font-size:20px}
  .chat-input{font-size:22px;padding:24px}
  .msg{font-size:20px}
  .core-label .main{font-size:40px;letter-spacing:16px}
  .top-bar{height:72px;font-size:20px}
  .top-bar .brand{font-size:26px}
  .gauge{width:160px;height:160px}
}

/* SETTINGS */
#settings-overlay{
  display:none;position:fixed;inset:0;z-index:200;
  background:rgba(0,8,20,.75);backdrop-filter:blur(8px);
  align-items:center;justify-content:center
}
#settings-overlay.open{display:flex}
.settings-modal{
  width:min(94%,560px);background:rgba(0,16,40,.97);
  border:1px solid rgba(0,180,255,.65);
  box-shadow:0 0 50px rgba(0,150,255,.35);
  padding:32px 36px;border-radius:4px
}
.settings-title{
  font-family:'Orbitron',sans-serif;font-size:22px;letter-spacing:5px;
  color:#00e5ff;margin-bottom:24px;text-shadow:0 0 14px #00b4ff
}
.settings-grid{
  display:grid;grid-template-columns:1fr 1fr;gap:16px 20px
}
.settings-grid label{
  display:flex;flex-direction:column;gap:6px;
  font-size:13px;color:#7ec0e0;letter-spacing:1px
}
.settings-grid input{
  background:rgba(0,30,60,.95);border:1px solid #00b4ff;
  color:#00e5ff;padding:12px 14px;font-size:16px;
  font-family:'Share Tech Mono',monospace;outline:none
}
.settings-grid input:focus{border-color:#00e5ff;box-shadow:0 0 14px rgba(0,180,255,.4)}
.settings-actions{display:flex;gap:14px;margin-top:28px;justify-content:flex-end}
.settings-actions .btn{padding:12px 28px;font-size:14px}
.settings-hint{margin-top:16px;font-size:13px;color:#4a90b8;line-height:1.4}
@media(max-width:600px){.settings-grid{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="top-bar">
  <span class="brand">STARK INDUSTRIES</span>
  <span id="top-status">SYSTEM STANDBY</span>
  <span class="clock" id="top-clock">--:--:--</span>
</div>
<div class="controls">
  <button class="btn" id="btn-toggle" onclick="toggleEngine()">ENGAGE</button>
  <button class="btn" onclick="refreshData()">SCAN</button>
  <button class="btn" onclick="openSettings()">SETTINGS</button>
</div>

<!-- TRADE SETTINGS MODAL -->
<div id="settings-overlay" onclick="if(event.target===this)closeSettings()">
  <div class="settings-modal">
    <div class="settings-title">TRADE SETTINGS</div>
    <div class="settings-grid">
      <label>Snipe Amount (SOL)
        <input type="number" id="cfg-snipe" step="0.01" min="0.01" max="1">
      </label>
      <label>Take Profit %
        <input type="number" id="cfg-tp" step="1" min="5" max="500">
      </label>
      <label>Trailing Stop %
        <input type="number" id="cfg-trail" step="1" min="3" max="80">
      </label>
      <label>Min Liquidity USD
        <input type="number" id="cfg-liq" step="100" min="1000" max="500000">
      </label>
      <label>Min Volume 24h USD
        <input type="number" id="cfg-vol" step="100" min="500" max="500000">
      </label>
      <label>Min AI/Score
        <input type="number" id="cfg-score" step="1" min="20" max="95">
      </label>
      <label>Max Positions
        <input type="number" id="cfg-maxpos" step="1" min="1" max="15">
      </label>
      <label>Max Trade Cap (SOL)
        <input type="number" id="cfg-cap" step="0.05" min="0.05" max="5">
      </label>
    </div>
    <div class="settings-actions">
      <button class="btn" onclick="saveSettings()">SAVE</button>
      <button class="btn" onclick="closeSettings()">CLOSE</button>
    </div>
    <div class="settings-hint">Changes apply to the live engine immediately. Saved in memory for this session.</div>
  </div>
</div>
<div class="panel" id="p-bal">
  <div class="pt">Balance</div>
  <div class="pv" id="balance">0.0000</div>
  <div class="ps" id="balance-usd">$0.00 · SOL</div>
</div>
<div class="panel" id="p-pos">
  <div class="pt">Positions</div>
  <div class="pv" id="positions">0 / 5</div>
  <div class="ps">ACTIVE SLOTS</div>
</div>
<div class="panel" id="p-wr">
  <div class="pt">Win Rate</div>
  <div class="pv" id="winrate">0.0%</div>
  <div class="ps" id="trades">0 / 0 trades</div>
</div>
<div class="panel" id="p-pnl">
  <div class="pt">Net P/L</div>
  <div class="pv" id="pnl">+0.0000</div>
  <div class="ps" id="pnl-usd">$+0.00</div>
</div>
<div class="panel" id="p-time">
  <div class="pt">Toronto Time</div>
  <div class="pv" id="toronto-time" style="font-size:15px">--:--:--</div>
  <div class="ps" id="tz-label">EST / EDT</div>
</div>
<div class="panel" id="p-ai">
  <div class="pt">AI Core</div>
  <div class="pv" id="ai-ready" style="font-size:14px">0</div>
  <div class="ps">MODELS ONLINE</div>
</div>
<div class="panel" id="p-mkt">
  <div class="pt">Markets</div>
  <div class="ps" id="mkt-lines" style="line-height:1.5;color:#a0d8f0;font-size:10px">loading...</div>
</div>
<div class="panel" id="p-logs"></div>
<div class="gauge-wrap">
  <div class="gauge" id="g1"><canvas width="130" height="130"></canvas><div class="glabel"><span class="gval" id="g-score">--</span>SCORE</div></div>
  <div class="gauge" id="g2"><canvas width="130" height="130"></canvas><div class="glabel"><span class="gval" id="g-cpu">--</span>LOAD</div></div>
</div>
<div class="core-label">
  <div class="main">J.A.R.V.I.S.</div>
  <div class="sub" id="core-sub">ATOM DNA · MULTI-AI</div>
</div>
<div class="chat-box">
  <div id="messages"></div>
  <input class="chat-input" id="chatInput" type="text" placeholder="Speak to J.A.R.V.I.S...."
    onkeypress="if(event.key==='Enter')sendCommand()">
</div>
<div class="footer">STARK INDUSTRIES · SOLANA CORE</div>
<canvas id="canvas"></canvas>
<script>
const canvas = document.getElementById('canvas');
const ctx = canvas.getContext('2d');
let W, H, CX, CY, t0 = Date.now();
function resize() {
  const dpr = Math.min(window.devicePixelRatio || 1, 3); // cap for perf, still sharp on 4K/8K
  const cssW = window.innerWidth;
  const cssH = window.innerHeight;
  canvas.style.width = cssW + 'px';
  canvas.style.height = cssH + 'px';
  canvas.width = Math.floor(cssW * dpr);
  canvas.height = Math.floor(cssH * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  W = cssW;
  H = cssH;
  CX = W / 2;
  CY = H / 2 - 10;
}
resize();
window.addEventListener('resize', resize);
let market = {BTC:0,ETH:0,SOL:0,BNB:0,XRP:0,ADA:0,DOGE:0};
let marketChg = {};
let hudData = { running: false, win_rate: 0, positions: 0, max_positions: 5, balance: 0 };
function drawGrid(t) {
  ctx.strokeStyle = 'rgba(0,140,220,0.04)';
  ctx.lineWidth = 0.5;
  const g = 40;
  for (let x = 0; x < W; x += g) { ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke(); }
  for (let y = 0; y < H; y += g) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke(); }
  ctx.strokeStyle = 'rgba(0,160,255,0.03)';
  for (let i = -H; i < W + H; i += 80) {
    ctx.beginPath(); ctx.moveTo(i, 0); ctx.lineTo(i + H, H); ctx.stroke();
  }
}
function drawCircuitLines(t) {
  const nodes = [
    [80, 180], [60, 280], [90, 380], [70, 480],
    [W - 80, 180], [W - 60, 280], [W - 90, 380], [W - 70, 480],
  ];
  nodes.forEach((n, i) => {
    const pulse = 0.08 + 0.08 * Math.sin(t * 2 + i);
    ctx.strokeStyle = `rgba(0,180,255,${pulse})`;
    ctx.beginPath();
    ctx.moveTo(n[0], n[1]);
    const mx = (n[0] + CX) / 2 + Math.sin(t + i) * 20;
    const my = (n[1] + CY) / 2;
    ctx.quadraticCurveTo(mx, my, CX + (n[0] < CX ? -120 : 120), CY);
    ctx.stroke();
    ctx.beginPath();
    ctx.arc(n[0], n[1], 2.5, 0, Math.PI * 2);
    ctx.fillStyle = `rgba(0,220,255,${0.4 + pulse})`;
    ctx.fill();
  });
}
const ringDefs = [
  { r: 45,  w: 2.5, a: 0.9,  speed: 0.4,  dash: null },
  { r: 62,  w: 1.5, a: 0.7,  speed: -0.25, dash: [4, 6] },
  { r: 78,  w: 1.2, a: 0.55, speed: 0.18, dash: [8, 4] },
  { r: 98,  w: 2.0, a: 0.65, speed: -0.12, dash: null },
  { r: 118, w: 1.0, a: 0.4,  speed: 0.08,  dash: [2, 8] },
  { r: 140, w: 1.5, a: 0.5,  speed: -0.06, dash: [12, 6] },
  { r: 165, w: 1.0, a: 0.35, speed: 0.04,  dash: null },
  { r: 190, w: 0.8, a: 0.25, speed: -0.03, dash: [6, 10] },
  { r: 220, w: 1.2, a: 0.3,  speed: 0.02,  dash: [20, 8] },
  { r: 250, w: 0.7, a: 0.18, speed: -0.015,dash: null },
];
function drawStarkRings(t) {
  ringDefs.forEach((ring, i) => {
    ctx.save();
    ctx.translate(CX, CY);
    ctx.rotate(t * ring.speed);
    ctx.beginPath();
    ctx.arc(0, 0, ring.r, 0, Math.PI * 2);
    ctx.strokeStyle = `rgba(0,200,255,${ring.a})`;
    ctx.lineWidth = ring.w;
    if (ring.dash) ctx.setLineDash(ring.dash);
    else ctx.setLineDash([]);
    ctx.stroke();
    ctx.setLineDash([]);
    if (i % 2 === 0) {
      for (let a = 0; a < 12; a++) {
        const ang = (a / 12) * Math.PI * 2;
        ctx.beginPath();
        ctx.moveTo(Math.cos(ang) * (ring.r - 4), Math.sin(ang) * (ring.r - 4));
        ctx.lineTo(Math.cos(ang) * (ring.r + 4), Math.sin(ang) * (ring.r + 4));
        ctx.strokeStyle = `rgba(0,180,255,${ring.a * 0.6})`;
        ctx.lineWidth = 1;
        ctx.stroke();
      }
    }
    ctx.restore();
  });
  const arcs = [
    { r: 105, start: t * 0.5, len: 1.2, color: 'rgba(0,220,255,0.8)', w: 3 },
    { r: 130, start: -t * 0.35 + 1, len: 0.9, color: 'rgba(0,160,255,0.6)', w: 2.5 },
    { r: 175, start: t * 0.2 + 2, len: 1.5, color: 'rgba(40,180,255,0.5)', w: 2 },
    { r: 205, start: -t * 0.15, len: 0.7, color: 'rgba(0,200,255,0.7)', w: 3 },
  ];
  arcs.forEach(a => {
    ctx.beginPath();
    ctx.arc(CX, CY, a.r, a.start, a.start + a.len);
    ctx.strokeStyle = a.color;
    ctx.lineWidth = a.w;
    ctx.lineCap = 'round';
    ctx.stroke();
  });
}
function drawCore(t) {
  const pulse = 1 + Math.sin(t * 2.5) * 0.12;
  let g = ctx.createRadialGradient(CX, CY, 0, CX, CY, 55 * pulse);
  g.addColorStop(0, 'rgba(200,240,255,0.95)');
  g.addColorStop(0.25, 'rgba(0,200,255,0.7)');
  g.addColorStop(0.55, 'rgba(0,120,220,0.35)');
  g.addColorStop(1, 'transparent');
  ctx.beginPath();
  ctx.arc(CX, CY, 55 * pulse, 0, Math.PI * 2);
  ctx.fillStyle = g;
  ctx.fill();
  g = ctx.createRadialGradient(CX, CY, 0, CX, CY, 22);
  g.addColorStop(0, '#ffffff');
  g.addColorStop(0.4, '#60dfff');
  g.addColorStop(1, '#0088cc');
  ctx.beginPath();
  ctx.arc(CX, CY, 18, 0, Math.PI * 2);
  ctx.fillStyle = g;
  ctx.shadowColor = '#00d4ff';
  ctx.shadowBlur = 25;
  ctx.fill();
  ctx.shadowBlur = 0;
  ctx.save();
  ctx.translate(CX, CY);
  ctx.rotate(t * 1.5);
  ctx.beginPath();
  ctx.arc(0, 0, 28, 0.2, Math.PI * 1.3);
  ctx.strokeStyle = 'rgba(255,255,255,0.7)';
  ctx.lineWidth = 2;
  ctx.stroke();
  ctx.restore();
}
function drawDNA(t, side) {
  const baseX = side > 0 ? CX + Math.min(W * 0.32, 300) : CX - Math.min(W * 0.32, 300);
  const baseY = CY + Math.sin(t * 0.3 * side) * 8;
  const len = 130, rad = 20, amp = 100, turns = 5.5;
  const rot = t * 1.1 * side;
  for (let i = 0; i < len; i++) {
    const tt = i / len;
    const angle = tt * Math.PI * 2 * turns + rot;
    const y = baseY - amp + tt * amp * 2;
    const x1 = baseX + Math.cos(angle) * rad;
    const x2 = baseX + Math.cos(angle + Math.PI) * rad;
    const d1 = (Math.sin(angle) + 1) / 2;
    ctx.beginPath();
    ctx.arc(x1, y, 2, 0, Math.PI * 2);
    ctx.fillStyle = `rgba(80,${150 + d1 * 80},255,${0.45 + d1 * 0.4})`;
    ctx.fill();
    ctx.beginPath();
    ctx.arc(x2, y, 2, 0, Math.PI * 2);
    ctx.fillStyle = `rgba(40,${120 + d1 * 60},240,${0.45 + d1 * 0.4})`;
    ctx.fill();
    if (i % 5 === 0) {
      ctx.beginPath();
      ctx.moveTo(x1, y);
      ctx.lineTo(x2, y);
      ctx.strokeStyle = `rgba(0,180,255,${0.12 + d1 * 0.2})`;
      ctx.lineWidth = 0.7;
      ctx.stroke();
    }
  }
}
const orbitals = [
  { symbol: 'BTC', angle: 0, r: 155, speed: 0.22 },
  { symbol: 'ETH', angle: 1.2, r: 185, speed: -0.18 },
  { symbol: 'SOL', angle: 2.5, r: 155, speed: 0.15 },
  { symbol: 'BNB', angle: 3.8, r: 210, speed: -0.12 },
  { symbol: 'XRP', angle: 5.0, r: 185, speed: 0.1 },
];
function drawOrbitals(t) {
  orbitals.forEach(o => {
    o.angle += o.speed * 0.016;
    const x = CX + Math.cos(o.angle) * o.r;
    const y = CY + Math.sin(o.angle) * o.r * 0.92;
    ctx.beginPath();
    ctx.moveTo(CX, CY);
    ctx.lineTo(x, y);
    ctx.strokeStyle = 'rgba(0,160,255,0.08)';
    ctx.lineWidth = 0.6;
    ctx.stroke();
    const grd = ctx.createRadialGradient(x, y, 0, x, y, 14);
    grd.addColorStop(0, 'rgba(0,220,255,0.9)');
    grd.addColorStop(0.5, 'rgba(0,160,255,0.3)');
    grd.addColorStop(1, 'transparent');
    ctx.beginPath();
    ctx.arc(x, y, 14, 0, Math.PI * 2);
    ctx.fillStyle = grd;
    ctx.fill();
    ctx.beginPath();
    ctx.arc(x, y, 5, 0, Math.PI * 2);
    ctx.fillStyle = '#00e5ff';
    ctx.fill();
    ctx.fillStyle = '#fff';
    ctx.font = 'bold 8px Share Tech Mono';
    ctx.textAlign = 'center';
    ctx.fillText(o.symbol, x, y - 12);
    const p = market[o.symbol];
    if (p) {
      ctx.fillStyle = (marketChg[o.symbol] || 0) >= 0 ? '#40ffc0' : '#ff6060';
      ctx.font = '7px Share Tech Mono';
      ctx.fillText(p >= 100 ? '$' + p.toFixed(0) : '$' + p.toFixed(2), x, y + 16);
    }
  });
}
function drawParticles(t) {
  for (let i = 0; i < 50; i++) {
    const s = i * 7919;
    const x = ((Math.sin(s + t * 0.12) * 0.5 + 0.5) * W);
    const y = ((Math.cos(s * 1.3 + t * 0.1) * 0.5 + 0.5) * H);
    ctx.beginPath();
    ctx.arc(x, y, 1, 0, Math.PI * 2);
    ctx.fillStyle = `rgba(0,180,255,${0.06 + 0.1 * Math.sin(t + i)})`;
    ctx.fill();
  }
}
function drawGauge(canvasEl, value, max, color) {
  const c = canvasEl.getContext('2d');
  const s = 130, cx = 65, cy = 65, r = 52;
  c.clearRect(0, 0, s, s);
  c.beginPath();
  c.arc(cx, cy, r, 0, Math.PI * 2);
  c.strokeStyle = 'rgba(0,100,160,0.4)';
  c.lineWidth = 4;
  c.stroke();
  const pct = Math.min(1, Math.max(0, value / max));
  c.beginPath();
  c.arc(cx, cy, r, -Math.PI / 2, -Math.PI / 2 + pct * Math.PI * 2);
  c.strokeStyle = color;
  c.lineWidth = 4;
  c.lineCap = 'round';
  c.stroke();
}

// ========== WIREFRAME ATOM (center) ==========
function drawWireSphere(cx, cy, radius, latStep, lonStep, rotY, rotX, alpha) {
  // latitude lines
  for (let i = 1; i < latStep; i++) {
    const phi = (i / latStep) * Math.PI;
    const r = radius * Math.sin(phi);
    const y = cy - radius * Math.cos(phi);
    ctx.beginPath();
    for (let j = 0; j <= 48; j++) {
      const th = (j / 48) * Math.PI * 2 + rotY;
      // simple Y-rotation perspective
      const x3 = r * Math.cos(th);
      const z3 = r * Math.sin(th);
      // tilt X
      const y3 = (y - cy);
      const y2 = y3 * Math.cos(rotX) - z3 * Math.sin(rotX);
      const z2 = y3 * Math.sin(rotX) + z3 * Math.cos(rotX);
      const scale = 1 + z2 / (radius * 4);
      const px = cx + x3 * scale;
      const py = cy + y2 * scale;
      if (j === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
    }
    ctx.strokeStyle = `rgba(0,200,255,${alpha * 0.55})`;
    ctx.lineWidth = 0.8;
    ctx.stroke();
  }
  // longitude lines
  for (let i = 0; i < lonStep; i++) {
    const th0 = (i / lonStep) * Math.PI * 2 + rotY;
    ctx.beginPath();
    for (let j = 0; j <= 32; j++) {
      const phi = (j / 32) * Math.PI;
      const r = radius * Math.sin(phi);
      const y3 = -radius * Math.cos(phi);
      const x3 = r * Math.cos(th0);
      const z3 = r * Math.sin(th0);
      const y2 = y3 * Math.cos(rotX) - z3 * Math.sin(rotX);
      const z2 = y3 * Math.sin(rotX) + z3 * Math.cos(rotX);
      const scale = 1 + z2 / (radius * 4);
      const px = cx + x3 * scale;
      const py = cy + y2 * scale;
      if (j === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
    }
    ctx.strokeStyle = `rgba(0,180,255,${alpha * 0.45})`;
    ctx.lineWidth = 0.7;
    ctx.stroke();
  }
}

function drawWireOrbit(cx, cy, rx, ry, rot, tilt, electronAngles, t) {
  // elliptical orbit path
  ctx.beginPath();
  for (let i = 0; i <= 64; i++) {
    const a = (i / 64) * Math.PI * 2 + rot;
    const x = Math.cos(a) * rx;
    const y = Math.sin(a) * ry;
    // tilt around X
    const yt = y * Math.cos(tilt);
    const zt = y * Math.sin(tilt);
    const scale = 1 + zt / 400;
    const px = cx + x * scale;
    const py = cy + yt * scale;
    if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
  }
  ctx.strokeStyle = 'rgba(180,230,255,0.75)';
  ctx.lineWidth = 1.4;
  ctx.stroke();

  // electrons on orbit
  electronAngles.forEach((ea, idx) => {
    const a = ea + rot + t * (0.4 + idx * 0.15);
    const x = Math.cos(a) * rx;
    const y = Math.sin(a) * ry;
    const yt = y * Math.cos(tilt);
    const zt = y * Math.sin(tilt);
    const scale = 1 + zt / 400;
    const px = cx + x * scale;
    const py = cy + yt * scale;
    const er = 14 + (scale - 1) * 10;
    // wire sphere electron
    drawWireSphere(px, py, er, 6, 8, t * 1.2 + idx, 0.3, 0.9);
    // glow
    ctx.beginPath();
    ctx.arc(px, py, er + 4, 0, Math.PI * 2);
    ctx.strokeStyle = 'rgba(0,220,255,0.25)';
    ctx.lineWidth = 1;
    ctx.stroke();
  });
}

function drawAtomCore(t) {
  const R = Math.min(W, H) * 0.18;
  // nucleus wireframe
  drawWireSphere(CX, CY, R * 0.55, 10, 12, t * 0.35, 0.4 + Math.sin(t * 0.2) * 0.1, 1.0);

  // three classic atom orbits at different tilts
  const orbits = [
    { rx: R * 2.2, ry: R * 0.7, rot: t * 0.5, tilt: 0.9, electrons: [0, Math.PI] },
    { rx: R * 2.2, ry: R * 0.7, rot: -t * 0.4 + 1.0, tilt: -0.7, electrons: [0.5, Math.PI + 0.5] },
    { rx: R * 2.0, ry: R * 0.55, rot: t * 0.3 + 2.0, tilt: 0.15, electrons: [1.2, Math.PI + 1.2, 0] },
  ];
  orbits.forEach(o => drawWireOrbit(CX, CY, o.rx, o.ry, o.rot, o.tilt, o.electrons, t));

  // faint outer guide rings
  for (let i = 0; i < 3; i++) {
    ctx.beginPath();
    ctx.ellipse(CX, CY, R * (2.6 + i * 0.35), R * (2.6 + i * 0.35) * 0.35, t * 0.05 * (i % 2 ? 1 : -1), 0, Math.PI * 2);
    ctx.strokeStyle = `rgba(0,160,255,${0.12 - i * 0.03})`;
    ctx.lineWidth = 0.8;
    ctx.stroke();
  }
}


function animate() {
  const t = (Date.now() - t0) / 1000;
  ctx.fillStyle = '#000814';
  ctx.fillRect(0, 0, W, H);
  drawGrid(t);
  drawParticles(t);
  drawCircuitLines(t);
  drawDNA(t, 1);
  drawDNA(t, -1);
  drawAtomCore(t);
  requestAnimationFrame(animate);
}
animate();
function escapeHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
function updateData() {
  fetch('/api/status').then(r => r.json()).then(d => {
    hudData = d;
    document.getElementById('balance').textContent = d.balance.toFixed(4);
    document.getElementById('balance-usd').textContent = '$' + d.balance_usd.toFixed(2) + ' · SOL';
    document.getElementById('positions').textContent = d.positions + ' / ' + d.max_positions;
    document.getElementById('winrate').textContent = d.win_rate.toFixed(1) + '%';
    document.getElementById('trades').textContent = d.wins + ' / ' + d.total_trades + ' trades';
    document.getElementById('pnl').textContent = (d.net_pnl >= 0 ? '+' : '') + d.net_pnl.toFixed(4);
    document.getElementById('pnl-usd').textContent = '$' + (d.net_pnl_usd >= 0 ? '+' : '') + d.net_pnl_usd.toFixed(2);
    document.getElementById('toronto-time').textContent = d.toronto_time || '--';
    document.getElementById('tz-label').textContent = d.timezone || 'EST/EDT';
    document.getElementById('top-clock').textContent = (d.toronto_time || '') + ' ' + (d.timezone || '');
    document.getElementById('ai-ready').textContent = d.freellm_ok ? (d.ai_ready + ' OK') : 'LOCAL';
    document.getElementById('ai-ready').style.color = d.freellm_ok ? '#40ffc0' : '#ffaa40';
    document.getElementById('top-status').textContent = d.running ? 'SYSTEM ONLINE' : 'SYSTEM STANDBY';
    document.getElementById('btn-toggle').textContent = d.running ? 'DISENGAGE' : 'ENGAGE';
    document.getElementById('btn-toggle').classList.toggle('active', d.running);
    document.getElementById('core-sub').textContent = d.running ? 'ENGAGED · SCANNING' : 'ATOM DNA · MULTI-AI';
    market = d.market || market;
    marketChg = d.market_chg || {};
    const ml = document.getElementById('mkt-lines');
    if (ml && market) {
      ml.innerHTML = ['SOL','BTC','ETH'].map(s => {
        const ch = marketChg[s] || 0;
        const col = ch >= 0 ? '#40ffc0' : '#ff7070';
        return '<div>' + s + ' <span style="color:' + col + '">$' + (market[s]||0).toFixed(s==='SOL'?2:0) + ' ' + (ch>=0?'+':'') + ch.toFixed(1) + '%</span></div>';
      }).join('');
    }
    if (d.logs) {
      document.getElementById('p-logs').innerHTML = d.logs.slice(-8).reverse().map(l => '<div>'+l+'</div>').join('');
    }
    const g1 = document.querySelector('#g1 canvas');
    const g2 = document.querySelector('#g2 canvas');
    if (g1) drawGauge(g1, d.win_rate || 0, 100, '#00d4ff');
    if (g2) drawGauge(g2, d.positions || 0, d.max_positions || 5, '#40b0ff');
    document.getElementById('g-score').textContent = (d.win_rate || 0).toFixed(0);
    document.getElementById('g-cpu').textContent = (d.positions || 0) + '/' + (d.max_positions || 5);
  }).catch(() => {});
}

function openSettings() {
  fetch('/api/config').then(r => r.json()).then(cfg => {
    document.getElementById('cfg-snipe').value = cfg.snipe_amount ?? 0.05;
    document.getElementById('cfg-tp').value = cfg.take_profit_pct ?? 45;
    document.getElementById('cfg-trail').value = cfg.trailing_stop_pct ?? 12;
    document.getElementById('cfg-liq').value = cfg.min_liquidity_usd ?? 12000;
    document.getElementById('cfg-vol').value = cfg.min_volume_24h ?? 5000;
    document.getElementById('cfg-score').value = cfg.min_score ?? 58;
    document.getElementById('cfg-maxpos').value = cfg.max_positions ?? 5;
    document.getElementById('cfg-cap').value = cfg.max_trade_sol ?? 0.5;
    document.getElementById('settings-overlay').classList.add('open');
  }).catch(() => {
    document.getElementById('settings-overlay').classList.add('open');
  });
}
function closeSettings() {
  document.getElementById('settings-overlay').classList.remove('open');
}
function saveSettings() {
  const body = {
    snipe_amount: parseFloat(document.getElementById('cfg-snipe').value),
    take_profit_pct: parseFloat(document.getElementById('cfg-tp').value),
    trailing_stop_pct: parseFloat(document.getElementById('cfg-trail').value),
    min_liquidity_usd: parseFloat(document.getElementById('cfg-liq').value),
    min_volume_24h: parseFloat(document.getElementById('cfg-vol').value),
    min_score: parseInt(document.getElementById('cfg-score').value, 10),
    max_positions: parseInt(document.getElementById('cfg-maxpos').value, 10),
    max_trade_sol: parseFloat(document.getElementById('cfg-cap').value),
  };
  fetch('/api/config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  })
  .then(r => r.json())
  .then(() => {
    const box = document.getElementById('messages');
    box.innerHTML += '<div class="msg jarvis"><b>JARVIS:</b> Trade settings updated and armed.</div>';
    box.scrollTop = box.scrollHeight;
    closeSettings();
    updateData();
  })
  .catch(() => alert('Failed to save settings'));
}

function toggleEngine() {
  fetch('/api/toggle', { method: 'POST' }).then(() => updateData());
}
function refreshData() { updateData(); }
function sendCommand() {
  const input = document.getElementById('chatInput');
  const msg = input.value.trim();
  if (!msg) return;
  const box = document.getElementById('messages');
  box.innerHTML += '<div class="msg you"><b>YOU:</b> ' + escapeHtml(msg) + '</div>';
  input.value = '';
  input.disabled = true;
  const tip = document.createElement('div');
  tip.className = 'msg jarvis typing';
  tip.id = 'typing';
  tip.innerHTML = '<b>JARVIS:</b> processing...';
  box.appendChild(tip);
  box.scrollTop = box.scrollHeight;
  fetch('/api/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message: msg })
  })
  .then(r => r.json())
  .then(d => {
    const t = document.getElementById('typing');
    if (t) t.remove();
    box.innerHTML += '<div class="msg jarvis"><b>JARVIS:</b> ' + escapeHtml(d.response || '...') + '</div>';
    box.scrollTop = box.scrollHeight;
  })
  .catch(() => {
    const t = document.getElementById('typing');
    if (t) t.remove();
    box.innerHTML += '<div class="msg jarvis"><b>JARVIS:</b> Link disrupted.</div>';
  })
  .finally(() => { input.disabled = false; input.focus(); });
}
document.getElementById('messages').innerHTML =
  '<div class="msg jarvis"><b>JARVIS:</b> Stark core online. Atom DNA active. How may I assist you, sir?</div>';
updateData();
setInterval(updateData, 4000);
</script>
</body>
</html>
"""

@app.route("/")
def home():
    return render_template_string(HTML_TEMPLATE)

@app.route("/api/status")
def api_status():
    balance = get_balance(state.wallet_address) if state.wallet_address else 0.0
    prices = get_live_prices()
    sol_price = float((prices.get("solana") or {}).get("usd") or 140.0)
    toronto = get_toronto_time()
    offset = toronto.utcoffset().total_seconds() if toronto.utcoffset() else -18000
    tz_label = "EDT" if offset == -14400 else "EST"

    def px(i):
        return float((prices.get(i) or {}).get("usd") or 0)

    def ch(i):
        return float((prices.get(i) or {}).get("usd_24h_change") or 0)

    market = {
        "BTC": px("bitcoin"), "ETH": px("ethereum"), "SOL": px("solana"),
        "BNB": px("binancecoin"), "XRP": px("ripple"), "ADA": px("cardano"), "DOGE": px("dogecoin"),
    }
    market_chg = {
        "BTC": ch("bitcoin"), "ETH": ch("ethereum"), "SOL": ch("solana"),
        "BNB": ch("binancecoin"), "XRP": ch("ripple"), "ADA": ch("cardano"), "DOGE": ch("dogecoin"),
    }
    total = len(state.trades)
    wins = sum(1 for t in state.trades if (t.get("profit") or 0) > 0)
    wr = (wins / total * 100) if total else 0.0
    net = sum(t.get("profit") or 0 for t in state.trades)
    ai_ready = sum(1 for v in getattr(ai_engine_global, "model_status", {}).values() if v)
    freellm_ok = ai_ready > 0
    with state.lock:
        logs = list(state.logs[-12:])
    return jsonify({
        "balance": balance,
        "balance_usd": balance * sol_price,
        "positions": len(state.positions),
        "max_positions": state.config["max_positions"],
        "win_rate": wr,
        "wins": wins,
        "total_trades": total,
        "net_pnl": net,
        "net_pnl_usd": net * sol_price,
        "running": state.running,
        "market": market,
        "market_chg": market_chg,
        "toronto_time": toronto.strftime("%H:%M:%S"),
        "timezone": tz_label,
        "ai_ready": ai_ready,
        "freellm_ok": freellm_ok,
        "logs": logs,
        "version": APP_VERSION,
    })

@app.route("/api/toggle", methods=["POST"])
def api_toggle():
    state.running = not state.running
    state.log("ENGINE " + ("ONLINE" if state.running else "STANDBY"))
    return jsonify({"running": state.running})

@app.route("/api/chat", methods=["POST"])
def api_chat():
    data = request.json or {}
    msg = (data.get("message") or "").strip()
    if not msg:
        return jsonify({"response": "Awaiting input, sir."})
    global ai_engine_global
    resp = ai_engine_global.jarvis_speak(msg) if ai_engine_global else "AI core offline."
    return jsonify({"response": resp})

@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "POST":
        data = request.json or {}
        for k in ("snipe_amount", "take_profit_pct", "trailing_stop_pct", "min_liquidity_usd",
                  "min_volume_24h", "max_positions", "min_score", "max_trade_sol"):
            if k in data:
                try:
                    state.config[k] = type(state.config.get(k, data[k]))(data[k])
                except Exception:
                    pass
        save_state()
        state.log("Settings updated via HUD")
        return jsonify({"ok": True, "config": state.config})
    return jsonify(state.config)

ai_engine_global: Optional[MultiAIEngine] = None

def main():
    global ai_engine_global
    if ENV_PRIVATE_KEY and SOLDERS_AVAILABLE:
        try:
            state.wallet = Keypair.from_base58_string(ENV_PRIVATE_KEY)
            state.wallet_address = str(state.wallet.pubkey())
            print(f"Wallet: {state.wallet_address[:8]}...")
        except Exception as e:
            print(f"Wallet error: {e}")
    elif ENV_PRIVATE_KEY and not SOLDERS_AVAILABLE:
        print("Install solders for trading: pip install solders")

    ai_engine_global = MultiAIEngine(FREELLM_API_KEY)
    threading.Thread(target=engine_loop, args=(ai_engine_global,), daemon=True).start()

    toronto = get_toronto_time()
    tz = "EDT" if (toronto.utcoffset() or timedelta(hours=-5)).total_seconds() == -14400 else "EST"
    print(f"\nJ.A.R.V.I.S. {APP_VERSION}")
    print(f"http://0.0.0.0:5000")
    print(f"Toronto: {toronto.strftime('%Y-%m-%d %H:%M:%S')} {tz}")
    print(f"FreeLLM key: {'set' if FREELLM_API_KEY else 'MISSING'}")
    print(f"FreeLLM base: {FREELLM_BASE}")
    print("Tip: if AI shows LOCAL, set FREELLM_BASE to your FreeLLM server (e.g. http://127.0.0.1:3001/v1)")
    print(f"Birdeye: {'OK' if BIRDEYE_API_KEY else 'MISSING'}\n")

    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False, use_reloader=False)

if __name__ == "__main__":
    main()

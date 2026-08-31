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
FREELLM_BASE = "https://api.freellmapi.com/v1"
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
        for mid in list(AI_MODELS.keys())[:5]:
            self.model_status[mid] = self.query(mid, "Reply: OK", 8) is not None
            time.sleep(0.25)

    def query(self, model: str, prompt: str, max_tokens: int = 300) -> Optional[str]:
        if not self.api_key:
            return None
        key = f"{model}:{hash(prompt)}"
        with self._lock:
            if key in self.cache and time.time() - self.cache[key]["t"] < self.cache_ttl:
                return self.cache[key]["r"]
        try:
            r = requests.post(
                f"{FREELLM_BASE}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": max_tokens,
                    "temperature": 0.25,
                },
                timeout=18,
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
            self.model_status[model] = False
        except Exception:
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
        for m in ["groq/gpt-oss-120b", "openrouter/nemotron-3-super-120b", "groq/compound"]:
            r = self.query(m, full, 450)
            if r:
                return r.strip()
        return "Core offline. Standing by, sir."

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
    except Exception:
        pass

def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({"positions": state.positions, "trades": state.trades}, f)
    except Exception:
        pass

HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "JARVIS/20.0 Stark", "Accept": "application/json"})

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

    try:
        r = HTTP.get(f"{DEXSCREENER_API}/latest/dex/search", params={"q": "solana"}, timeout=8)
        if r.status_code == 200:
            for pair in ((r.json() or {}).get("pairs") or [])[:25]:
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

    try:
        for path in ["/pairs/new_pairs", "/rank/sol/swaps/1h"]:
            r = HTTP.get(
                f"https://gmgn.ai/defi/quotation/v1{path}",
                params={"limit": 20, "chain": "sol"},
                timeout=8,
            )
            if r.status_code == 200:
                data = (r.json() or {}).get("data") or {}
                items = data.get("pairs") or data.get("rank") or data.get("list") or []
                if isinstance(items, dict):
                    items = list(items.values())
                for p in items[:20]:
                    if not isinstance(p, dict):
                        continue
                    tokens.append({
                        "mint": p.get("address") or p.get("base_address") or p.get("token_address"),
                        "symbol": p.get("symbol") or p.get("base_symbol") or "UNKNOWN",
                        "liquidity": float(p.get("liquidity") or p.get("liquidity_usd") or 0),
                        "volume_24h": float(p.get("volume_24h") or p.get("volume") or 0),
                        "buys_24h": int(p.get("buys_24h") or p.get("buys") or 0),
                        "sells_24h": int(p.get("sells_24h") or p.get("sells") or 0),
                        "market_cap": float(p.get("market_cap") or p.get("mc") or 0),
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
body{
  background:#000814;overflow:hidden;height:100vh;
  font-family:'Share Tech Mono',monospace;color:#00d4ff;
}
canvas{position:fixed;top:0;left:0;z-index:1;width:100%;height:100%}
.top-bar{
  position:fixed;top:0;left:0;right:0;height:36px;z-index:20;
  background:linear-gradient(180deg,rgba(0,40,80,.9),rgba(0,20,40,.6));
  border-bottom:1px solid rgba(0,180,255,.35);
  display:flex;align-items:center;justify-content:space-between;
  padding:0 16px;font-size:11px;letter-spacing:2px
}
.top-bar .brand{font-family:'Orbitron',sans-serif;font-weight:700;color:#00e5ff;text-shadow:0 0 12px #00b4ff}
.top-bar .clock{color:#7fdfff}
.panel{
  position:fixed;z-index:15;
  background:rgba(0,20,45,.72);
  border:1px solid rgba(0,180,255,.4);
  box-shadow:0 0 20px rgba(0,150,255,.15),inset 0 0 30px rgba(0,100,200,.08);
  backdrop-filter:blur(6px);
  padding:10px 12px;border-radius:2px
}
.panel::before{
  content:'';position:absolute;top:0;left:0;width:12px;height:12px;
  border-top:2px solid #00d4ff;border-left:2px solid #00d4ff
}
.panel::after{
  content:'';position:absolute;bottom:0;right:0;width:12px;height:12px;
  border-bottom:2px solid #00d4ff;border-right:2px solid #00d4ff
}
.pt{font-size:9px;letter-spacing:2px;color:#00b4ff;margin-bottom:4px;opacity:.85;text-transform:uppercase}
.pv{font-size:18px;font-weight:700;color:#e0f7ff;text-shadow:0 0 10px rgba(0,180,255,.5);font-family:'Orbitron',sans-serif}
.ps{font-size:9px;color:#4a90b8;margin-top:2px}
#p-bal{top:50px;left:14px;min-width:140px}
#p-pos{top:140px;left:14px;min-width:140px}
#p-wr{top:50px;right:14px;min-width:140px}
#p-pnl{top:140px;right:14px;min-width:140px}
#p-time{top:50px;left:50%;transform:translateX(-50%);text-align:center;min-width:160px}
#p-ai{bottom:200px;left:14px;min-width:140px}
#p-mkt{bottom:200px;right:14px;min-width:150px}
#p-logs{
  bottom:100px;left:14px;width:220px;max-height:90px;overflow:auto;
  font-size:9px;line-height:1.4;color:#6ab0d0
}
#p-logs div{opacity:.85;margin-bottom:2px}
.gauge-wrap{
  position:fixed;z-index:14;left:14px;top:240px;
  display:flex;flex-direction:column;gap:12px
}
.gauge{width:70px;height:70px;position:relative}
.gauge canvas{position:absolute;top:0;left:0}
.gauge .glabel{
  position:absolute;inset:0;display:flex;flex-direction:column;
  align-items:center;justify-content:center;font-size:8px;color:#00b4ff
}
.gauge .gval{font-size:14px;font-weight:700;color:#fff;font-family:'Orbitron',sans-serif}
.core-label{
  position:fixed;top:50%;left:50%;transform:translate(-50%,140px);
  z-index:12;text-align:center;pointer-events:none
}
.core-label .main{
  font-family:'Orbitron',sans-serif;font-size:13px;letter-spacing:6px;
  color:#00e5ff;text-shadow:0 0 20px #00b4ff
}
.core-label .sub{font-size:9px;color:#4a90b8;letter-spacing:3px;margin-top:4px}
.controls{
  position:fixed;top:100px;left:50%;transform:translateX(-50%);z-index:25;
  display:flex;gap:8px
}
.btn{
  background:rgba(0,30,60,.85);border:1px solid #00b4ff;color:#00d4ff;
  padding:8px 16px;font-size:10px;letter-spacing:2px;cursor:pointer;
  font-family:'Share Tech Mono',monospace;transition:all .2s;
  box-shadow:0 0 12px rgba(0,150,255,.2)
}
.btn:hover{background:#00b4ff;color:#001020;box-shadow:0 0 20px #00b4ff}
.btn.active{background:rgba(0,180,255,.25);border-color:#00e5ff}
.chat-box{
  position:fixed;bottom:16px;left:50%;transform:translateX(-50%);
  width:min(94%,480px);z-index:30;display:flex;flex-direction:column;gap:6px
}
#messages{
  max-height:140px;overflow-y:auto;display:flex;flex-direction:column;gap:4px;
  scrollbar-width:thin;scrollbar-color:rgba(0,180,255,.35) transparent
}
.msg{
  background:rgba(0,25,50,.9);border-left:2px solid #00b4ff;
  padding:7px 11px;font-size:11px;color:#c8e8ff;line-height:1.4;
  animation:fadeIn .2s ease
}
.msg.you{border-left-color:#40a0ff}
.msg.jarvis{border-left-color:#00e5ff}
.msg b{color:#00d4ff}
.msg.typing{opacity:.65;font-style:italic}
.chat-input{
  width:100%;background:rgba(0,20,45,.95);border:1px solid #00b4ff;
  padding:11px 14px;color:#00d4ff;font-size:12px;outline:none;
  font-family:'Share Tech Mono',monospace;letter-spacing:.5px;
  box-shadow:0 0 18px rgba(0,150,255,.25)
}
.chat-input:focus{border-color:#00e5ff;box-shadow:0 0 28px rgba(0,180,255,.4)}
.chat-input::placeholder{color:rgba(0,180,255,.4)}
.footer{
  position:fixed;bottom:4px;left:50%;transform:translateX(-50%);
  font-family:'Orbitron',sans-serif;font-size:8px;letter-spacing:4px;
  color:rgba(0,180,255,.35);z-index:10
}
@keyframes fadeIn{from{opacity:0;transform:translateY(3px)}to{opacity:1;transform:none}}
@media(max-width:768px){
  .panel{padding:7px 9px;min-width:100px}
  .pv{font-size:13px}
  #p-logs,.gauge-wrap,#p-mkt,#p-ai{display:none}
  .core-label{transform:translate(-50%,100px)}
}
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
  <div class="gauge" id="g1"><canvas width="70" height="70"></canvas><div class="glabel"><span class="gval" id="g-score">--</span>SCORE</div></div>
  <div class="gauge" id="g2"><canvas width="70" height="70"></canvas><div class="glabel"><span class="gval" id="g-cpu">--</span>LOAD</div></div>
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
  W = canvas.width = window.innerWidth;
  H = canvas.height = window.innerHeight;
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
  const s = 70, cx = 35, cy = 35, r = 28;
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
function animate() {
  const t = (Date.now() - t0) / 1000;
  ctx.fillStyle = '#000814';
  ctx.fillRect(0, 0, W, H);
  drawGrid(t);
  drawParticles(t);
  drawCircuitLines(t);
  drawStarkRings(t);
  drawOrbitals(t);
  drawDNA(t, 1);
  drawDNA(t, -1);
  drawCore(t);
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
    document.getElementById('ai-ready').textContent = d.ai_ready || 0;
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
                  "min_volume_24h", "max_positions", "min_score"):
            if k in data:
                try:
                    state.config[k] = type(state.config[k])(data[k])
                except Exception:
                    pass
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
    print(f"FreeLLM: {'OK' if FREELLM_API_KEY else 'MISSING'}")
    print(f"Birdeye: {'OK' if BIRDEYE_API_KEY else 'MISSING'}\n")

    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False, use_reloader=False)

if __name__ == "__main__":
    main()

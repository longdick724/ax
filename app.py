# app.py - J.A.R.V.I.S. // Stark Industries HUD - API Keys in Settings
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
APP_VERSION = "21.0 API KEYS SETTINGS"
SOL_MINT = "So11111111111111111111111111111111111111112"

def get_toronto_time():
    now = datetime.now(timezone.utc)
    year = now.year
    mar8 = datetime(year, 3, 8)
    march_2nd_sun = mar8 + timedelta(days=(6 - mar8.weekday()) % 7)
    nov1 = datetime(year, 11, 1)
    nov_1st_sun = nov1 + timedelta(days=(6 - nov1.weekday()) % 7)
    is_dst = march_2nd_sun.replace(tzinfo=timezone.utc) <= now < nov_1st_sun.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone(timedelta(hours=-4 if is_dst else -5)))

# Mutable secrets
SECRETS = {
    "HELIUS_KEY": os.getenv("HELIUS_KEY", "").strip(),
    "FREELLM_API_KEY": os.getenv("FREELLM_API_KEY", "").strip(),
    "FREELLM_BASE": os.getenv("FREELLM_BASE", "https://api.freellmapi.com/v1").rstrip("/"),
    "BIRDEYE_API_KEY": os.getenv("BIRDEYE_API_KEY", "").strip(),
    "SOLANA_PRIVATE_KEY": os.getenv("SOLANA_PRIVATE_KEY", "").strip(),
}

def get_helius_rpc():
    k = SECRETS.get("HELIUS_KEY") or ""
    return f"https://mainnet.helius-rpc.com/?api-key={k}" if k else "https://api.mainnet-beta.solana.com"

def get_freellm_bases():
    base = (SECRETS.get("FREELLM_BASE") or "").rstrip("/")
    bases = [base, "https://api.freellmapi.com/v1", "http://127.0.0.1:3001/v1", "http://localhost:3001/v1"]
    seen = set()
    out = []
    for b in bases:
        if b and b not in seen:
            seen.add(b)
            out.append(b)
    return out

HELIUS_KEY = SECRETS["HELIUS_KEY"]
FREELLM_API_KEY = SECRETS["FREELLM_API_KEY"]
ENV_PRIVATE_KEY = SECRETS["SOLANA_PRIVATE_KEY"]
BIRDEYE_API_KEY = SECRETS["BIRDEYE_API_KEY"]
HELIUS_RPC_URL = get_helius_rpc()
FREELLM_BASE = SECRETS["FREELLM_BASE"]
FREELLM_BASES = get_freellm_bases()
JUPITER_API_BASE = "https://quote-api.jup.ag/v6"
DEXSCREENER_API = "https://api.dexscreener.com"
BIRDEYE_API = "https://public-api.birdeye.so"
COINGECKO_API = "https://api.coingecko.com/api/v3"
STATE_FILE = "jarvis_state.json"
MAX_TRADE_SOL_CAP = 0.50

def apply_secrets(updates: dict):
    global HELIUS_KEY, FREELLM_API_KEY, ENV_PRIVATE_KEY, BIRDEYE_API_KEY
    global HELIUS_RPC_URL, FREELLM_BASE, FREELLM_BASES, ai_engine_global
    for k, v in updates.items():
        if k in SECRETS and v is not None:
            val = str(v).strip()
            if val and not val.startswith("••••") and val != "(unchanged)":
                SECRETS[k] = val
    HELIUS_KEY = SECRETS["HELIUS_KEY"]
    FREELLM_API_KEY = SECRETS["FREELLM_API_KEY"]
    ENV_PRIVATE_KEY = SECRETS["SOLANA_PRIVATE_KEY"]
    BIRDEYE_API_KEY = SECRETS["BIRDEYE_API_KEY"]
    HELIUS_RPC_URL = get_helius_rpc()
    FREELLM_BASE = SECRETS["FREELLM_BASE"]
    FREELLM_BASES = get_freellm_bases()
    if ai_engine_global is not None:
        ai_engine_global.api_key = FREELLM_API_KEY
        threading.Thread(target=ai_engine_global._bg_test, daemon=True).start()
    if ENV_PRIVATE_KEY and SOLDERS_AVAILABLE:
        try:
            state.wallet = Keypair.from_base58_string(ENV_PRIVATE_KEY)
            state.wallet_address = str(state.wallet.pubkey())
            state.log(f"Wallet linked: {state.wallet_address[:8]}...")
        except Exception as e:
            state.log(f"Wallet key error: {e}")

# ================================================================
# AI MODELS
# ================================================================
AI_MODELS = {
    "groq/gpt-oss-120b": {"name": "GPT-OSS 120B", "role": "Deep Analysis"},
    "groq/gpt-oss-20b": {"name": "GPT-OSS 20B", "role": "Quick Scoring"},
    "groq/compound": {"name": "Compound", "role": "Strategy"},
    "groq/compound-mini": {"name": "Compound Mini", "role": "Fast Decisions"},
    "groq/qwen3.6-27b": {"name": "Qwen 3.6 27B", "role": "Patterns"},
    "openrouter/nemotron-3-super-120b": {"name": "Nemotron 120B", "role": "Risk"},
    "openrouter/gemma-4-31b": {"name": "Gemma 31B", "role": "Sentiment"},
    "openrouter/gemma-4-26b-a4b": {"name": "Gemma 26B", "role": "Quick"},
    "openrouter/nemotron-3-nano-30b": {"name": "Nemotron Nano", "role": "Reasoning"},
    "openrouter/poolside-laguna-s-2.1": {"name": "Laguna S", "role": "Strategy"},
}

class MultiAIEngine:
    def __init__(self, api_key):
        self.api_key = api_key
        self.model_status = {}
        self.cache = {}
        self.cache_ttl = 45
        self._lock = threading.Lock()
        threading.Thread(target=self._bg_test, daemon=True).start()

    def _bg_test(self):
        for mid in ["auto", "auto:fast"] + list(AI_MODELS.keys())[:4]:
            ok = self.query(mid, "Reply with only: OK", 8) is not None
            self.model_status[mid] = ok
            if ok:
                break
            time.sleep(0.2)

    def query(self, model, prompt, max_tokens=300):
        if not self.api_key:
            return None
        key = f"{model}:{hash(prompt)}"
        with self._lock:
            if key in self.cache and time.time() - self.cache[key]["t"] < self.cache_ttl:
                return self.cache[key]["r"]
        payload = {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens, "temperature": 0.25}
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        for base in get_freellm_bases():
            try:
                r = requests.post(f"{base}/chat/completions", headers=headers, json=payload, timeout=18, verify=False)
                if r.status_code == 200:
                    content = r.json().get("choices", [{}])[0].get("message", {}).get("content", "")
                    if content:
                        with self._lock:
                            self.cache[key] = {"t": time.time(), "r": content}
                        self.model_status[model] = True
                        return content
            except Exception:
                continue
        self.model_status[model] = False
        return None

    def get_multi_ai_consensus(self, token_data):
        prompt = (
            "Score this Solana memecoin 0-100 for short-term trade potential. Return ONLY an integer.\n"
            f"Symbol: {token_data.get('symbol')}\nLiquidity: ${token_data.get('liquidity', 0):,.0f}\n"
            f"Vol24h: ${token_data.get('volume_24h', 0):,.0f}\n"
            f"Buys: {token_data.get('buys_24h', 0)} Sells: {token_data.get('sells_24h', 0)}"
        )
        models = ["groq/gpt-oss-20b", "groq/compound-mini", "groq/qwen3.6-27b", "openrouter/gemma-4-26b-a4b"]
        scores = []
        for m in models:
            resp = self.query(m, prompt, 20)
            if resp:
                nums = re.findall(r'\b([0-9]{1,3})\b', resp)
                for n in nums:
                    s = int(n)
                    if 0 <= s <= 100:
                        scores.append(s)
                        break
        return sum(scores) / len(scores) if scores else None

    def jarvis_speak(self, prompt):
        sys = "You are J.A.R.V.I.S., Stark Industries AI. Be concise and professional."
        for m in ["auto", "groq/gpt-oss-120b", "openrouter/nemotron-3-super-120b", "groq/compound"]:
            r = self.query(m, f"{sys}\nUser: {prompt}\nJARVIS:", 400)
            if r:
                return r.strip()
        return f"Engine status: {'ONLINE' if state.running else 'STANDBY'}. FreeLLM API unreachable."

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
    config: Dict = field(default_factory=lambda: {
        "snipe_amount": 0.05,
        "take_profit_pct": 45.0,
        "trailing_stop_pct": 12.0,
        "min_liquidity_usd": 12000.0,
        "min_volume_24h": 5000.0,
        "max_positions": 5,
        "min_score": 58,
    })

    def log(self, msg):
        ts = get_toronto_time().strftime("%H:%M:%S")
        entry = f"[{ts}] {msg}"
        with self.lock:
            self.logs.append(entry)
            self.logs = self.logs[-100:]
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
        if isinstance(d.get("secrets"), dict):
            for sk, sv in d["secrets"].items():
                if sk in SECRETS and sv:
                    SECRETS[sk] = str(sv).strip()
            globals()["HELIUS_KEY"] = SECRETS["HELIUS_KEY"]
            globals()["FREELLM_API_KEY"] = SECRETS["FREELLM_API_KEY"]
            globals()["ENV_PRIVATE_KEY"] = SECRETS["SOLANA_PRIVATE_KEY"]
            globals()["BIRDEYE_API_KEY"] = SECRETS["BIRDEYE_API_KEY"]
            globals()["HELIUS_RPC_URL"] = get_helius_rpc()
            globals()["FREELLM_BASE"] = SECRETS["FREELLM_BASE"]
            globals()["FREELLM_BASES"] = get_freellm_bases()
    except Exception:
        pass

def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({
                "positions": state.positions,
                "trades": state.trades,
                "config": state.config,
                "secrets": {
                    "HELIUS_KEY": SECRETS.get("HELIUS_KEY", ""),
                    "FREELLM_API_KEY": SECRETS.get("FREELLM_API_KEY", ""),
                    "FREELLM_BASE": SECRETS.get("FREELLM_BASE", ""),
                    "BIRDEYE_API_KEY": SECRETS.get("BIRDEYE_API_KEY", ""),
                    "SOLANA_PRIVATE_KEY": SECRETS.get("SOLANA_PRIVATE_KEY", ""),
                },
            }, f)
    except Exception:
        pass

HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json"})

# ================================================================
# TOKEN DISCOVERY
# ================================================================
def discover_tokens_multi_source():
    tokens = []
    try:
        r = HTTP.get(f"{DEXSCREENER_API}/token-boosts/latest/v1", timeout=8, verify=False)
        if r.status_code == 200:
            mints = [x.get("tokenAddress") for x in r.json() if x.get("chainId") == "solana"][:40]
            for i in range(0, len(mints), 30):
                r2 = HTTP.get(f"{DEXSCREENER_API}/tokens/v1/solana/{','.join(mints[i:i+30])}", timeout=10, verify=False)
                if r2.status_code == 200:
                    for pair in r2.json():
                        if pair.get("chainId") == "solana":
                            base = pair.get("baseToken", {})
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
                                "source": "dexscreener",
                            })
    except Exception as e:
        state.log(f"DexScreener: {e}")

    if SECRETS.get("BIRDEYE_API_KEY"):
        headers = {"X-API-KEY": SECRETS.get("BIRDEYE_API_KEY", ""), "x-chain": "solana"}
        try:
            r = HTTP.get(f"{BIRDEYE_API}/defi/v2/tokens/new_listing", headers=headers, params={"limit": 20, "meme_platform_enabled": "true"}, timeout=10, verify=False)
            if r.status_code == 200:
                data = r.json().get("data", {})
                items = data.get("items", data if isinstance(data, list) else [])
                for t in items:
                    tokens.append({
                        "mint": t.get("address"),
                        "symbol": t.get("symbol", "UNKNOWN"),
                        "liquidity": float(t.get("liquidity", 0)),
                        "volume_24h": float(t.get("volume24hUSD", 0)),
                        "buys_24h": int(t.get("buy24h", 0)),
                        "sells_24h": int(t.get("sell24h", 0)),
                        "market_cap": float(t.get("marketCap", 0)),
                        "source": "birdeye",
                    })
        except Exception as e:
            state.log(f"Birdeye: {e}")

    seen = set()
    unique = []
    for t in tokens:
        mint = t.get("mint")
        if mint and mint not in seen and mint != SOL_MINT:
            seen.add(mint)
            unique.append(t)
    unique.sort(key=lambda x: (x.get("liquidity") or 0), reverse=True)
    return unique[:50]

# ================================================================
# RPC + JUPITER
# ================================================================
def rpc_call(method, params):
    try:
        r = HTTP.post(get_helius_rpc(), json={"jsonrpc":"2.0","id":1,"method":method,"params":params}, timeout=15, verify=False)
        return r.json().get("result") if r.status_code == 200 else None
    except:
        return None

def get_balance(pubkey):
    if not pubkey:
        return 0.0
    result = rpc_call("getBalance", [pubkey])
    return float(result.get("value", 0)) / 1e9 if result else 0.0

def jupiter_quote(input_mint, output_mint, amount, slippage_bps=800):
    try:
        r = HTTP.get(f"{JUPITER_API_BASE}/quote", params={"inputMint":input_mint,"outputMint":output_mint,"amount":amount,"slippageBps":slippage_bps}, timeout=12, verify=False)
        return r.json() if r.status_code == 200 else None
    except:
        return None

def jupiter_swap(quote, wallet):
    if not SOLDERS_AVAILABLE or wallet is None:
        return None
    try:
        r = HTTP.post(f"{JUPITER_API_BASE}/swap", json={"quoteResponse":quote,"userPublicKey":str(wallet.pubkey()),"wrapAndUnwrapSol":True}, timeout=20, verify=False)
        if r.status_code != 200:
            return None
        raw = base64.b64decode(r.json()["swapTransaction"])
        unsigned = VersionedTransaction.from_bytes(raw)
        signed = VersionedTransaction(unsigned.message, [wallet])
        encoded = base64.b64encode(bytes(signed)).decode()
        r2 = HTTP.post(get_helius_rpc(), json={"jsonrpc":"2.0","id":1,"method":"sendTransaction","params":[encoded,{"encoding":"base64","skipPreflight":True}]}, timeout=25, verify=False)
        if r2.status_code == 200:
            result = r2.json().get("result")
            if result:
                time.sleep(1.5)
                return result
    except Exception as e:
        state.log(f"Swap: {e}")
    return None

def get_live_prices():
    try:
        r = HTTP.get(f"{COINGECKO_API}/simple/price", params={"ids":"bitcoin,ethereum,solana,binancecoin,ripple,cardano,dogecoin","vs_currencies":"usd","include_24hr_change":"true"}, timeout=8, verify=False)
        if r.status_code == 200:
            return r.json()
    except:
        pass
    return {}

def score_token(token, ai_engine):
    liq = token.get("liquidity") or 0
    vol = token.get("volume_24h") or 0
    buys = token.get("buys_24h") or 0
    sells = token.get("sells_24h") or 0
    score = 0.0
    if liq < state.config["min_liquidity_usd"]:
        return 0
    if liq >= 50000: score += 28
    elif liq >= 25000: score += 22
    else: score += 15
    if vol >= 50000: score += 22
    elif vol >= 15000: score += 16
    elif vol >= state.config["min_volume_24h"]: score += 10
    else: score += 2
    if sells > 0 and buys > sells * 2.0: score += 18
    elif sells > 0 and buys > sells * 1.4: score += 12
    elif buys > sells: score += 6
    ai_score = ai_engine.get_multi_ai_consensus(token)
    if ai_score:
        score = score * 0.35 + ai_score * 0.65
    return int(max(0, min(100, round(score))))

def engine_loop(ai_engine):
    state.log(f"JARVIS {APP_VERSION} online")
    while True:
        try:
            if not state.running:
                time.sleep(2)
                continue
            for pos in list(state.positions):
                quote = jupiter_quote(pos["mint"], SOL_MINT, pos.get("out_amount") or 1, 1000)
                if quote:
                    current = int(quote.get("outAmount") or 0) / 1e9
                    pnl = ((current - pos["entry_sol"]) / pos["entry_sol"]) * 100
                    if pnl >= state.config["take_profit_pct"] or pnl <= -state.config["trailing_stop_pct"]:
                        if state.wallet:
                            result = rpc_call("getTokenAccountsByOwner", [state.wallet_address, {"mint": pos["mint"]}, {"encoding":"jsonParsed"}])
                            if result and result.get("value"):
                                try:
                                    amount = int(result["value"][0]["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
                                    sq = jupiter_quote(pos["mint"], SOL_MINT, amount, 1200)
                                    if sq:
                                        sig = jupiter_swap(sq, state.wallet)
                                        if sig:
                                            exit_sol = int(sq.get("outAmount") or 0) / 1e9
                                            profit = exit_sol - pos["entry_sol"]
                                            state.positions.remove(pos)
                                            state.trades.append({"date": get_toronto_time().strftime("%Y-%m-%d %H:%M"), "symbol": pos["symbol"], "profit": profit})
                                            state.log(f"SOLD {pos['symbol']} {profit:+.4f} SOL")
                                            save_state()
                                except:
                                    pass
            if len(state.positions) < state.config["max_positions"] and state.wallet:
                tokens = discover_tokens_multi_source()
                scored = []
                for token in tokens:
                    s = score_token(token, ai_engine)
                    token["score"] = s
                    if s >= state.config["min_score"]:
                        scored.append(token)
                scored.sort(key=lambda x: x["score"], reverse=True)
                for token in scored[:3]:
                    if len(state.positions) >= state.config["max_positions"]:
                        break
                    q = jupiter_quote(SOL_MINT, token["mint"], int(state.config["snipe_amount"] * 1e9), 900)
                    if q:
                        sig = jupiter_swap(q, state.wallet)
                        if sig:
                            state.positions.append({"mint": token["mint"], "symbol": token["symbol"], "entry_sol": state.config["snipe_amount"], "out_amount": int(q.get("outAmount") or 0), "score": token["score"]})
                            state.log(f"BOUGHT {token['symbol']} score={token['score']}")
                            save_state()
                            time.sleep(1)
            time.sleep(8)
        except Exception as e:
            state.log(f"Engine: {e}")
            time.sleep(4)

# ================================================================
# FLASK APP
# ================================================================
app = Flask(__name__)

HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>J.A.R.V.I.S.</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@400;700;900&family=Share+Tech+Mono&display=swap');
*{margin:0;padding:0;box-sizing:border-box}
body{background:#000814;color:#00d4ff;font-family:'Share Tech Mono',monospace;height:100vh;overflow:hidden}
canvas{position:fixed;top:0;left:0;z-index:1}
.top-bar{position:fixed;top:0;left:0;right:0;height:52px;z-index:30;background:linear-gradient(180deg,rgba(0,40,80,.95),rgba(0,20,40,.75));border-bottom:1px solid rgba(0,180,255,.4);display:flex;align-items:center;justify-content:space-between;padding:0 28px;font-size:15px;letter-spacing:3px}
.top-bar .brand{font-family:'Orbitron',sans-serif;font-weight:700;font-size:18px;color:#00e5ff;text-shadow:0 0 16px #00b4ff}
.panel{position:fixed;z-index:20;background:rgba(0,18,42,.85);border:1px solid rgba(0,180,255,.55);box-shadow:0 0 28px rgba(0,150,255,.22);padding:20px 24px;border-radius:4px;min-width:260px}
.pt{font-size:14px;letter-spacing:3px;color:#00b4ff;margin-bottom:8px;text-transform:uppercase}
.pv{font-size:36px;font-weight:700;color:#e8f9ff;font-family:'Orbitron',sans-serif}
.ps{font-size:14px;color:#5aa0c8;margin-top:6px}
#p-bal{top:70px;left:28px}
#p-pos{top:210px;left:28px}
#p-wr{top:70px;right:28px}
#p-pnl{top:210px;right:28px}
#p-time{top:70px;left:50%;transform:translateX(-50%);text-align:center}
#p-ai{bottom:280px;left:28px}
#p-mkt{bottom:280px;right:28px}
#p-logs{bottom:140px;left:28px;width:340px;max-height:160px;overflow:auto;font-size:13px;color:#7ec0e0}
.core-label{position:fixed;top:50%;left:50%;transform:translate(-50%,260px);z-index:12;text-align:center}
.core-label .main{font-family:'Orbitron',sans-serif;font-size:26px;letter-spacing:12px;color:#00e5ff;text-shadow:0 0 24px #00b4ff}
.core-label .sub{font-size:13px;color:#5aa0c8;letter-spacing:4px;margin-top:8px}
.controls{position:fixed;top:120px;left:50%;transform:translateX(-50%);z-index:35;display:flex;gap:12px}
.btn{background:rgba(0,30,60,.9);border:1px solid #00b4ff;color:#00d4ff;padding:14px 32px;font-size:15px;letter-spacing:3px;cursor:pointer;font-family:'Share Tech Mono',monospace}
.btn:hover{background:#00b4ff;color:#001020}
.chat-box{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);width:min(94%,720px);z-index:40;display:flex;flex-direction:column;gap:8px}
#messages{max-height:200px;overflow-y:auto;display:flex;flex-direction:column;gap:6px}
.msg{background:rgba(0,25,50,.92);border-left:3px solid #00b4ff;padding:12px 16px;font-size:15px;color:#c8e8ff}
.msg b{color:#00d4ff}
.chat-input{width:100%;background:rgba(0,20,45,.96);border:1px solid #00b4ff;padding:18px 20px;color:#00d4ff;font-size:16px;outline:none;font-family:'Share Tech Mono',monospace}
.footer{position:fixed;bottom:6px;left:50%;transform:translateX(-50%);font-family:'Orbitron',sans-serif;font-size:10px;letter-spacing:5px;color:rgba(0,180,255,.35);z-index:10}
#settings-overlay{display:none;position:fixed;inset:0;z-index:200;background:rgba(0,8,20,.75);backdrop-filter:blur(8px);align-items:center;justify-content:center}
#settings-overlay.open{display:flex}
.settings-modal{width:min(94%,580px);max-height:90vh;overflow-y:auto;background:rgba(0,16,40,.97);border:1px solid rgba(0,180,255,.65);box-shadow:0 0 50px rgba(0,150,255,.35);padding:32px 36px;border-radius:4px}
.settings-title{font-family:'Orbitron',sans-serif;font-size:22px;letter-spacing:5px;color:#00e5ff;margin-bottom:24px}
.settings-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px 20px}
.settings-grid label{display:flex;flex-direction:column;gap:6px;font-size:13px;color:#7ec0e0}
.settings-grid input{background:rgba(0,30,60,.95);border:1px solid #00b4ff;color:#00e5ff;padding:12px 14px;font-size:16px;font-family:'Share Tech Mono',monospace;outline:none}
.settings-actions{display:flex;gap:14px;margin-top:28px;justify-content:flex-end}
.settings-hint{margin-top:16px;font-size:13px;color:#4a90b8;line-height:1.4}
@media(max-width:600px){.settings-grid{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="top-bar">
  <span class="brand">STARK INDUSTRIES</span>
  <span id="top-status">SYSTEM STANDBY</span>
  <span id="top-clock">--:--:--</span>
</div>
<div class="controls">
  <button class="btn" id="btn-toggle" onclick="toggleEngine()">ENGAGE</button>
  <button class="btn" onclick="refreshData()">SCAN</button>
  <button class="btn" onclick="openSettings()">SETTINGS</button>
</div>

<div id="settings-overlay" onclick="if(event.target===this)closeSettings()">
  <div class="settings-modal">
    <div class="settings-title">TRADE SETTINGS</div>
    <div class="settings-grid">
      <label>Snipe Amount (SOL)<input type="number" id="cfg-snipe" step="0.01" min="0.01" max="1"></label>
      <label>Take Profit %<input type="number" id="cfg-tp" step="1" min="5" max="500"></label>
      <label>Trailing Stop %<input type="number" id="cfg-trail" step="1" min="3" max="80"></label>
      <label>Min Liquidity USD<input type="number" id="cfg-liq" step="100" min="1000" max="500000"></label>
      <label>Min Volume 24h USD<input type="number" id="cfg-vol" step="100" min="500" max="500000"></label>
      <label>Min Score<input type="number" id="cfg-score" step="1" min="20" max="95"></label>
      <label>Max Positions<input type="number" id="cfg-maxpos" step="1" min="1" max="15"></label>
    </div>
    <div class="settings-title" style="margin-top:28px;font-size:16px">API KEYS</div>
    <div class="settings-grid" style="grid-template-columns:1fr">
      <label>FreeLLM API Key<input type="password" id="cfg-freellm-key" placeholder="freellmapi-..." autocomplete="off"></label>
      <label>FreeLLM Base URL<input type="text" id="cfg-freellm-base" placeholder="https://api.freellmapi.com/v1"></label>
      <label>Birdeye API Key<input type="password" id="cfg-birdeye" placeholder="Birdeye X-API-KEY" autocomplete="off"></label>
      <label>Helius API Key<input type="password" id="cfg-helius" placeholder="Helius RPC key" autocomplete="off"></label>
      <label>Solana Private Key<input type="password" id="cfg-wallet" placeholder="Leave blank to keep current" autocomplete="off"></label>
    </div>
    <div class="settings-actions">
      <button class="btn" onclick="saveSettings()">SAVE</button>
      <button class="btn" onclick="closeSettings()">CLOSE</button>
    </div>
    <div class="settings-hint">Keys stored on server. Leave blank to keep current value.</div>
  </div>
</div>

<div class="panel" id="p-bal"><div class="pt">Balance</div><div class="pv" id="balance">0.0000</div><div class="ps" id="balance-usd">$0.00 · SOL</div></div>
<div class="panel" id="p-pos"><div class="pt">Positions</div><div class="pv" id="positions">0 / 5</div><div class="ps">ACTIVE SLOTS</div></div>
<div class="panel" id="p-wr"><div class="pt">Win Rate</div><div class="pv" id="winrate">0.0%</div><div class="ps" id="trades">0 / 0</div></div>
<div class="panel" id="p-pnl"><div class="pt">Net P/L</div><div class="pv" id="pnl">+0.0000</div><div class="ps" id="pnl-usd">$+0.00</div></div>
<div class="panel" id="p-time"><div class="pt">Toronto</div><div class="pv" id="toronto-time" style="font-size:15px">--:--:--</div><div class="ps" id="tz-label">EST/EDT</div></div>
<div class="panel" id="p-ai"><div class="pt">AI Core</div><div class="pv" id="ai-ready" style="font-size:14px">0</div><div class="ps">MODELS</div></div>
<div class="panel" id="p-mkt"><div class="pt">Markets</div><div class="ps" id="mkt-lines">loading...</div></div>
<div class="panel" id="p-logs"></div>

<div class="core-label"><div class="main">J.A.R.V.I.S.</div><div class="sub" id="core-sub">ATOM DNA · MULTI-AI</div></div>

<div class="chat-box">
  <div id="messages"></div>
  <input class="chat-input" id="chatInput" type="text" placeholder="Speak to J.A.R.V.I.S...." onkeypress="if(event.key==='Enter')sendCommand()">
</div>
<div class="footer">STARK INDUSTRIES · SOLANA CORE</div>
<canvas id="canvas"></canvas>

<script>
const canvas=document.getElementById('canvas'),ctx=canvas.getContext('2d');
canvas.width=innerWidth;canvas.height=innerHeight;
const CX=innerWidth/2,CY=innerHeight/2;
let market={BTC:0,ETH:0,SOL:0,BNB:0,XRP:0,ADA:0,DOGE:0};
const rings=[{r:100,s:0.01},{r:160,s:-0.008},{r:220,s:0.006},{r:280,s:-0.005},{r:340,s:0.004}];
const electrons=[{ring:0,a:0,sym:'BTC'},{ring:1,a:1.5,sym:'ETH'},{ring:2,a:3,sym:'SOL'},{ring:3,a:4.5,sym:'BNB'},{ring:4,a:0,sym:'XRP'}];

function drawAtom(t){
  rings.forEach(r=>{ctx.beginPath();ctx.arc(CX,CY,r.r,0,Math.PI*2);ctx.strokeStyle='rgba(0,255,136,0.3)';ctx.stroke()});
  electrons.forEach(e=>{const r=rings[e.ring];e.a+=r.s;const x=CX+Math.cos(e.a)*r.r,y=CY+Math.sin(e.a)*r.r;
    ctx.beginPath();ctx.arc(x,y,8,0,Math.PI*2);ctx.fillStyle='#00ff88';ctx.shadowColor='#00ff88';ctx.shadowBlur=20;ctx.fill();ctx.shadowBlur=0;
    ctx.fillStyle='#fff';ctx.font='8px monospace';ctx.textAlign='center';ctx.fillText(e.sym,x,y-12);ctx.fillText('$'+(market[e.sym]||0).toFixed(0),x,y+18)});
  const pulse=1+Math.sin(t/500)*0.15;const grd=ctx.createRadialGradient(CX,CY,0,CX,CY,40*pulse);
  grd.addColorStop(0,'#fff');grd.addColorStop(0.3,'#00ff88');grd.addColorStop(0.7,'#006633');grd.addColorStop(1,'transparent');
  ctx.beginPath();ctx.arc(CX,CY,40*pulse,0,Math.PI*2);ctx.fillStyle=grd;ctx.fill();
}

function drawDNA(t){
  const dx=CX+250,dy=CY;
  for(let i=0;i<150;i++){const tt=i/150,ang=tt*Math.PI*6+t/1000,y=dy-100+tt*200,x1=dx+Math.cos(ang)*25,x2=dx+Math.cos(ang+Math.PI)*25;
    ctx.fillStyle='rgba(0,255,136,0.7)';ctx.fillRect(x1,y,2,2);ctx.fillRect(x2,y,2,2);
    if(i%3===0){ctx.strokeStyle='rgba(0,255,136,0.2)';ctx.beginPath();ctx.moveTo(x1,y);ctx.lineTo(x2,y);ctx.stroke()}}
}

function animate(){const t=Date.now();ctx.fillStyle='#000';ctx.fillRect(0,0,canvas.width,canvas.height);drawAtom(t);drawDNA(t);requestAnimationFrame(animate)}
animate();

function updateData(){
  fetch('/api/status').then(r=>r.json()).then(d=>{
    document.getElementById('balance').textContent=d.balance.toFixed(4);
    document.getElementById('balance-usd').textContent='$'+d.balance_usd.toFixed(2)+' · SOL';
    document.getElementById('positions').textContent=d.positions+' / '+d.max_positions;
    document.getElementById('winrate').textContent=d.win_rate.toFixed(1)+'%';
    document.getElementById('trades').textContent=d.wins+' / '+d.total_trades;
    document.getElementById('pnl').textContent=(d.net_pnl>=0?'+':'')+d.net_pnl.toFixed(4);
    document.getElementById('pnl-usd').textContent='$'+(d.net_pnl_usd>=0?'+':'')+d.net_pnl_usd.toFixed(2);
    document.getElementById('toronto-time').textContent=d.toronto_time||'--';
    document.getElementById('tz-label').textContent=d.timezone||'EST/EDT';
    document.getElementById('top-clock').textContent=(d.toronto_time||'')+' '+(d.timezone||'');
    document.getElementById('top-status').textContent=d.running?'SYSTEM ONLINE':'SYSTEM STANDBY';
    document.getElementById('btn-toggle').textContent=d.running?'DISENGAGE':'ENGAGE';
    document.getElementById('core-sub').textContent=d.running?'ENGAGED · SCANNING':'ATOM DNA · MULTI-AI';
    document.getElementById('ai-ready').textContent=d.ai_ready+' AI';
    market=d.market||market;
    const ml=document.getElementById('mkt-lines');
    if(ml){ml.innerHTML=['SOL','BTC','ETH'].map(s=>'<div>'+s+' $'+(market[s]||0).toFixed(2)+'</div>').join('')}
    if(d.logs){document.getElementById('p-logs').innerHTML=d.logs.slice(-8).reverse().map(l=>'<div>'+l+'</div>').join('')}
  }).catch(()=>{});
}

function openSettings(){
  fetch('/api/config').then(r=>r.json()).then(cfg=>{
    document.getElementById('cfg-snipe').value=cfg.snipe_amount||0.05;
    document.getElementById('cfg-tp').value=cfg.take_profit_pct||45;
    document.getElementById('cfg-trail').value=cfg.trailing_stop_pct||12;
    document.getElementById('cfg-liq').value=cfg.min_liquidity_usd||12000;
    document.getElementById('cfg-vol').value=cfg.min_volume_24h||5000;
    document.getElementById('cfg-score').value=cfg.min_score||58;
    document.getElementById('cfg-maxpos').value=cfg.max_positions||5;
    const sec=cfg.secrets||{};
    document.getElementById('cfg-freellm-key').placeholder=sec.FREELLM_API_KEY_SET?'•••• set — enter new':'freellmapi-...';
    document.getElementById('cfg-freellm-base').value=sec.FREELLM_BASE||'';
    document.getElementById('cfg-birdeye').placeholder=sec.BIRDEYE_API_KEY_SET?'•••• set — enter new':'Birdeye key';
    document.getElementById('cfg-helius').placeholder=sec.HELIUS_KEY_SET?'•••• set — enter new':'Helius key';
    document.getElementById('cfg-wallet').placeholder=sec.SOLANA_PRIVATE_KEY_SET?'•••• set — enter new':'base58 private key';
    document.getElementById('settings-overlay').classList.add('open');
  }).catch(()=>document.getElementById('settings-overlay').classList.add('open'));
}

function closeSettings(){document.getElementById('settings-overlay').classList.remove('open')}

function saveSettings(){
  const body={
    snipe_amount:parseFloat(document.getElementById('cfg-snipe').value),
    take_profit_pct:parseFloat(document.getElementById('cfg-tp').value),
    trailing_stop_pct:parseFloat(document.getElementById('cfg-trail').value),
    min_liquidity_usd:parseFloat(document.getElementById('cfg-liq').value),
    min_volume_24h:parseFloat(document.getElementById('cfg-vol').value),
    min_score:parseInt(document.getElementById('cfg-score').value),
    max_positions:parseInt(document.getElementById('cfg-maxpos').value),
    secrets:{}
  };
  const freeKey=document.getElementById('cfg-freellm-key').value.trim();
  const freeBase=document.getElementById('cfg-freellm-base').value.trim();
  const birdeye=document.getElementById('cfg-birdeye').value.trim();
  const helius=document.getElementById('cfg-helius').value.trim();
  const wallet=document.getElementById('cfg-wallet').value.trim();
  if(freeKey)body.secrets.FREELLM_API_KEY=freeKey;
  if(freeBase)body.secrets.FREELLM_BASE=freeBase;
  if(birdeye)body.secrets.BIRDEYE_API_KEY=birdeye;
  if(helius)body.secrets.HELIUS_KEY=helius;
  if(wallet)body.secrets.SOLANA_PRIVATE_KEY=wallet;
  fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
  .then(r=>r.json()).then(()=>{closeSettings();updateData()}).catch(()=>alert('Save failed'));
}

function toggleEngine(){fetch('/api/toggle',{method:'POST'}).then(()=>updateData())}
function refreshData(){updateData()}

function sendCommand(){
  const input=document.getElementById('chatInput'),msg=input.value.trim();
  if(!msg)return;
  document.getElementById('messages').innerHTML+='<div class="msg"><b>YOU:</b> '+msg+'</div>';
  input.value='';
  fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:msg})})
  .then(r=>r.json()).then(d=>{document.getElementById('messages').innerHTML+='<div class="msg"><b>JARVIS:</b> '+(d.response||'...')+'</div>'});
}

document.getElementById('messages').innerHTML='<div class="msg"><b>JARVIS:</b> Stark core online. How may I assist you, sir?</div>';
updateData();
setInterval(updateData,4000);
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
    tz = "EDT" if (toronto.utcoffset() or timedelta(hours=-5)).total_seconds() == -14400 else "EST"
    market = {"BTC": float((prices.get("bitcoin") or {}).get("usd") or 0), "ETH": float((prices.get("ethereum") or {}).get("usd") or 0), "SOL": float((prices.get("solana") or {}).get("usd") or 0), "BNB": float((prices.get("binancecoin") or {}).get("usd") or 0), "XRP": float((prices.get("ripple") or {}).get("usd") or 0), "ADA": float((prices.get("cardano") or {}).get("usd") or 0), "DOGE": float((prices.get("dogecoin") or {}).get("usd") or 0)}
    total = len(state.trades)
    wins = sum(1 for t in state.trades if (t.get("profit") or 0) > 0)
    wr = (wins / total * 100) if total else 0.0
    net = sum(t.get("profit") or 0 for t in state.trades)
    ai_ready = sum(1 for v in getattr(ai_engine_global, "model_status", {}).values() if v)
    return jsonify({"balance": balance, "balance_usd": balance * sol_price, "positions": len(state.positions), "max_positions": state.config["max_positions"], "win_rate": wr, "wins": wins, "total_trades": total, "net_pnl": net, "net_pnl_usd": net * sol_price, "running": state.running, "market": market, "toronto_time": toronto.strftime("%H:%M:%S"), "timezone": tz, "ai_ready": ai_ready, "logs": list(state.logs[-12:])})

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
        return jsonify({"response": "Awaiting input."})
    global ai_engine_global
    return jsonify({"response": ai_engine_global.jarvis_speak(msg) if ai_engine_global else "AI offline"})

@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "POST":
        data = request.json or {}
        for k in ("snipe_amount", "take_profit_pct", "trailing_stop_pct", "min_liquidity_usd", "min_volume_24h", "max_positions", "min_score"):
            if k in data and data[k] is not None:
                try:
                    state.config[k] = type(state.config.get(k, data[k]))(data[k])
                except:
                    pass
        if isinstance(data.get("secrets"), dict):
            apply_secrets(data["secrets"])
        save_state()
        return jsonify({"ok": True, "config": state.config})
    return jsonify({**state.config, "secrets": {"FREELLM_BASE": SECRETS.get("FREELLM_BASE", ""), "FREELLM_API_KEY_SET": bool(SECRETS.get("FREELLM_API_KEY")), "BIRDEYE_API_KEY_SET": bool(SECRETS.get("BIRDEYE_API_KEY")), "HELIUS_KEY_SET": bool(SECRETS.get("HELIUS_KEY")), "SOLANA_PRIVATE_KEY_SET": bool(SECRETS.get("SOLANA_PRIVATE_KEY"))}})

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
    ai_engine_global = MultiAIEngine(FREELLM_API_KEY)
    threading.Thread(target=engine_loop, args=(ai_engine_global,), daemon=True).start()
    print(f"\nJ.A.R.V.I.S. {APP_VERSION} running on http://0.0.0.0:5000\n")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False, use_reloader=False)

if __name__ == "__main__":
    main()

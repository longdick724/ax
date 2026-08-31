# app.py - JARVIS // CYBER SNIPER - Complete Trading + AI Command Center
import os
import json
import time
import base64
import threading
import random
import re
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
# CONFIG - ALL KEYS FROM ENVIRONMENT
# ================================================================
APP_NAME = "J.A.R.V.I.S. // CYBER SNIPER"
APP_VERSION = "11.0"

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

HELIUS_KEY = os.getenv("HELIUS_KEY", "").strip()
FREELLM_API_KEY = os.getenv("FREELLM_API_KEY", "").strip()
ENV_PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY", "").strip()
BIRDEYE_KEY = os.getenv("BIRDEYE_API_KEY", "").strip()

HELIUS_RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_KEY}" if HELIUS_KEY else ""
JUPITER_API_BASE = "https://quote-api.jup.ag/v6"
DEXSCREENER_API = "https://api.dexscreener.com"
FREELLM_BASE = "https://api.freellmapi.com/v1"

STATE_FILE = "cyber_sniper_state.json"
MAX_TRADE_SOL_CAP = 0.50
REQUEST_TIMEOUT = 90
SCAN_INTERVAL_SECONDS = 15

# ================================================================
# AI MODELS
# ================================================================
AI_MODELS = {
    "groq/gpt-oss-120b": {"name": "GPT-OSS 120B", "role": "Deep Analysis", "tokens": "6.0M/6.0M"},
    "groq/gpt-oss-20b": {"name": "GPT-OSS 20B", "role": "Quick Scoring", "tokens": "6.0M/6.0M"},
    "groq/compound": {"name": "Compound", "role": "Strategy", "tokens": "6.0M/6.0M"},
    "groq/compound-mini": {"name": "Compound Mini", "role": "Fast Decisions", "tokens": "6.0M/6.0M"},
    "groq/qwen3.6-27b": {"name": "Qwen 3.6 27B", "role": "Pattern Recognition", "tokens": "15.0M/15.0M"},
    "openrouter/nemotron-3-super-120b": {"name": "Nemotron 120B", "role": "Risk Assessment", "tokens": "6.0M/6.0M"},
    "openrouter/gemma-4-31b": {"name": "Gemma 4 31B", "role": "Sentiment", "tokens": "6.0M/6.0M"},
    "openrouter/gemma-4-26b-a4b": {"name": "Gemma 4 26B", "role": "Quick Analysis", "tokens": "6.0M/6.0M"},
}

# ================================================================
# AI ENGINE
# ================================================================
class AIEngine:
    def __init__(self, api_key):
        self.api_key = api_key
        self.cache = {}
        self.cache_ttl = 300
        self.model_status = {}
        if api_key:
            self.test_models()
    
    def test_models(self):
        for model_id in AI_MODELS.keys():
            try:
                response = self.query(model_id, "Say OK", 10)
                self.model_status[model_id] = "🟢" if response else "🔴"
            except:
                self.model_status[model_id] = "🔴"
    
    def query(self, model, prompt, max_tokens=500):
        if not self.api_key:
            return None
        cache_key = f"{model}:{hash(prompt)}"
        if cache_key in self.cache:
            cached_time, cached_response = self.cache[cache_key]
            if time.time() - cached_time < self.cache_ttl:
                return cached_response
        try:
            headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": "You are JARVIS, a Solana trading expert. Be concise and precise."},
                    {"role": "user", "content": prompt}
                ],
                "max_tokens": max_tokens,
                "temperature": 0.3
            }
            response = requests.post(f"{FREELLM_BASE}/chat/completions", headers=headers, json=payload, timeout=20, verify=False)
            if response.status_code == 200:
                data = response.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                self.cache[cache_key] = (time.time(), content)
                return content
            return None
        except:
            return None
    
    def get_consensus_score(self, token_data):
        if not self.api_key:
            return None
        prompt = f"""
        Score this Solana token 0-100 for trading potential.
        Return ONLY a number.
        Symbol: {token_data.get('symbol', 'UNKNOWN')}
        Liquidity: ${token_data.get('liquidity', 0):,.0f}
        Volume: ${token_data.get('volume_24h', 0):,.0f}
        Buys: {token_data.get('buys_24h', 0)}
        Sells: {token_data.get('sells_24h', 0)}
        """
        scores = []
        for model in ["groq/gpt-oss-20b", "groq/compound-mini", "openrouter/gemma-4-26b-a4b"]:
            if self.model_status.get(model) == "🟢":
                response = self.query(model, prompt, 50)
                if response:
                    numbers = re.findall(r'\d+', response)
                    if numbers:
                        score = int(numbers[0])
                        if 0 <= score <= 100:
                            scores.append(score)
        return sum(scores) / len(scores) if scores else None
    
    def jarvis_query(self, user_message, system_prompt=None):
        """JARVIS-style chat"""
        if not self.api_key:
            return "I'm sorry, but my AI core is offline. Please check the API key configuration."
        system = system_prompt or "You are J.A.R.V.I.S., an advanced AI assistant. Be calm, precise, and helpful."
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user_message}]
        return self.query("groq/gpt-oss-120b", user_message, 2000)

# ================================================================
# STATE
# ================================================================
@dataclass
class EngineState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    running: bool = False
    wallet: Optional[Keypair] = None
    wallet_address: str = ""
    positions: List[Dict] = field(default_factory=list)
    trades: List[Dict] = field(default_factory=list)
    logs: List[str] = field(default_factory=list)
    jarvis_messages: List[Dict] = field(default_factory=list)
    jarvis_requests: int = 0
    start_time: float = field(default_factory=time.time)
    
    config: Dict = field(default_factory=lambda: {
        "snipe_amount": 0.05,
        "take_profit_pct": 50.0,
        "trailing_stop_pct": 15.0,
        "min_liquidity_usd": 15000.0,
        "min_volume_24h": 5000.0,
        "max_positions": 5,
        "daily_loss_limit": 0.20,
        "min_score": 45,
        "use_ai": True,
    })
    
    def log(self, message, tag="sys"):
        ts = datetime.now().strftime("%H:%M:%S")
        safe = str(message).replace("<", "&lt;").replace(">", "&gt;")
        with self.lock:
            self.logs.insert(0, f'<div class="line {tag}">[{ts}] {safe}</div>')
            self.logs = self.logs[:120]
        print(f"[{ts}] {message}")

@st.cache_resource
def get_state():
    state = EngineState()
    load_persisted(state)
    return state

def load_persisted(state):
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
        state.positions = data.get("positions", [])
        state.trades = data.get("trades", [])
    except:
        pass

def save_persisted(state):
    try:
        with state.lock:
            data = {"positions": state.positions, "trades": state.trades}
        with open(STATE_FILE + ".tmp", "w") as f:
            json.dump(data, f, indent=2)
        os.replace(STATE_FILE + ".tmp", STATE_FILE)
    except:
        pass

# ================================================================
# HTTP HELPERS
# ================================================================
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": f"JARVIS-CyberSniper/{APP_VERSION}"})

def http_get(url, params=None, timeout=REQUEST_TIMEOUT):
    try:
        r = HTTP.get(url, params=params, timeout=timeout, verify=False)
        return r.json() if r.status_code == 200 else None
    except:
        return None

def http_post(url, json_data=None, timeout=REQUEST_TIMEOUT):
    try:
        r = HTTP.post(url, json=json_data, timeout=timeout, verify=False)
        return r.json() if r.status_code == 200 else None
    except:
        return None

# ================================================================
# RPC
# ================================================================
def rpc_call(method, params):
    if not HELIUS_RPC_URL:
        return None
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    try:
        r = HTTP.post(HELIUS_RPC_URL, json=payload, timeout=REQUEST_TIMEOUT, verify=False)
        if r.status_code == 200:
            data = r.json()
            if "error" not in data:
                return data.get("result")
    except:
        pass
    return None

def get_wallet_balance(pubkey):
    if not pubkey:
        return 0.0
    result = rpc_call("getBalance", [pubkey])
    return float(result.get("value", 0)) / 1_000_000_000 if result else 0.0

def send_raw_transaction(signed_tx_bytes):
    encoded = base64.b64encode(signed_tx_bytes).decode()
    payload = {"jsonrpc": "2.0", "id": 1, "method": "sendTransaction", "params": [encoded, {"encoding": "base64", "skipPreflight": False, "maxRetries": 3}]}
    try:
        r = HTTP.post(HELIUS_RPC_URL, json=payload, timeout=20, verify=False)
        if r.status_code == 200:
            data = r.json()
            return data.get("result")
    except:
        pass
    return None

def confirm_transaction(signature, timeout=40):
    start = time.time()
    while time.time() - start < timeout:
        result = rpc_call("getSignatureStatuses", [[signature], {"searchTransactionHistory": True}])
        if result:
            statuses = result.get("value", [])
            if statuses and statuses[0]:
                if statuses[0].get("err"):
                    return False
                if statuses[0].get("confirmationStatus") in ("confirmed", "finalized"):
                    return True
        time.sleep(2)
    return False

# ================================================================
# DISCOVERY
# ================================================================
def discover_tokens():
    tokens = []
    data = http_get(f"{DEXSCREENER_API}/token-boosts/latest/v1")
    if data:
        mints = [x.get("tokenAddress") for x in data if x.get("chainId") == "solana"]
        for start in range(0, min(len(mints), 30), 30):
            batch = mints[start:start+30]
            pairs = http_get(f"{DEXSCREENER_API}/tokens/v1/solana/{','.join(batch)}")
            if pairs:
                for pair in pairs:
                    if pair.get("chainId") == "solana":
                        base = pair.get("baseToken", {})
                        tokens.append({
                            "mint": base.get("address"),
                            "symbol": base.get("symbol", "UNKNOWN"),
                            "liquidity": float((pair.get("liquidity") or {}).get("usd", 0)),
                            "volume_24h": float((pair.get("volume") or {}).get("h24", 0)),
                            "buys_24h": int((pair.get("txns", {}).get("h24", {}) or {}).get("buys", 0)),
                            "sells_24h": int((pair.get("txns", {}).get("h24", {}) or {}).get("sells", 0)),
                        })
    return tokens[:30]

# ================================================================
# JUPITER
# ================================================================
def jupiter_quote(input_mint, output_mint, amount, slippage_bps=500):
    try:
        r = HTTP.get(f"{JUPITER_API_BASE}/quote", params={"inputMint": input_mint, "outputMint": output_mint, "amount": amount, "slippageBps": slippage_bps}, timeout=15, verify=False)
        return r.json() if r.status_code == 200 else None
    except:
        return None

def jupiter_swap(quote, wallet):
    try:
        r = HTTP.post(f"{JUPITER_API_BASE}/swap", json={"quoteResponse": quote, "userPublicKey": str(wallet.pubkey()), "wrapAndUnwrapSol": True}, timeout=25, verify=False)
        if r.status_code != 200:
            return None
        body = r.json()
        raw = base64.b64decode(body["swapTransaction"])
        unsigned = VersionedTransaction.from_bytes(raw)
        signed = VersionedTransaction(unsigned.message, [wallet])
        sig = send_raw_transaction(bytes(signed))
        return sig if sig and confirm_transaction(sig) else None
    except:
        return None

def do_buy(state, token, sol_amount):
    mint = token["mint"]
    lamports = int(sol_amount * 1_000_000_000)
    quote = jupiter_quote(SOL_MINT, mint, lamports)
    if not quote:
        return None
    out_amount = int(quote.get("outAmount", 0))
    if out_amount <= 0 or state.wallet is None:
        return None
    sig = jupiter_swap(quote, state.wallet)
    if not sig:
        return None
    return {"mint": mint, "symbol": token.get("symbol", "UNKNOWN"), "entry_sol": sol_amount, "out_amount": out_amount, "opened_at": datetime.now().isoformat(), "buy_sig": sig, "peak_pnl_pct": 0.0, "score": token.get("score", 0)}

def do_sell(state, position):
    if state.wallet is None:
        return None
    result = rpc_call("getTokenAccountsByOwner", [state.wallet_address, {"mint": position["mint"]}, {"encoding": "jsonParsed"}])
    if not result:
        return None
    try:
        accounts = result.get("value", [])
        if not accounts:
            return None
        token_amount = int(accounts[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
    except:
        return None
    if token_amount <= 0:
        return None
    quote = jupiter_quote(position["mint"], SOL_MINT, token_amount, 1000)
    if not quote:
        return None
    sig = jupiter_swap(quote, state.wallet)
    if not sig:
        return None
    exit_sol = int(quote.get("outAmount", 0)) / 1_000_000_000
    profit = exit_sol - position["entry_sol"]
    return {"date": datetime.now().strftime("%Y-%m-%d"), "time": datetime.now().strftime("%H:%M:%S"), "symbol": position["symbol"], "mint": position["mint"], "entry_sol": position["entry_sol"], "exit_sol": exit_sol, "profit": profit, "sell_sig": sig}

# ================================================================
# ENGINE
# ================================================================
def engine_loop(state, ai_engine):
    state.log("JARVIS TRADING ENGINE ONLINE", "sys")
    while True:
        try:
            if not state.running:
                time.sleep(2)
                continue
            today = datetime.now().strftime("%Y-%m-%d")
            today_pnl = sum(t["profit"] for t in state.trades if t.get("date") == today)
            if today_pnl <= -abs(state.config["daily_loss_limit"]):
                state.log("DAILY LOSS LIMIT - STOPPING", "sell-loss")
                state.running = False
                continue
            with state.lock:
                positions = list(state.positions)
                cfg = dict(state.config)
            for position in positions:
                quote = jupiter_quote(position["mint"], SOL_MINT, position.get("out_amount", 1), 1000)
                if quote:
                    current_sol = int(quote.get("outAmount", 0)) / 1_000_000_000
                    pnl_pct = ((current_sol - position["entry_sol"]) / position["entry_sol"]) * 100
                    position["peak_pnl_pct"] = max(position.get("peak_pnl_pct", 0), pnl_pct)
                    peak = position["peak_pnl_pct"]
                    should_close = False
                    reason = ""
                    if pnl_pct >= cfg["take_profit_pct"]:
                        should_close, reason = True, "TP"
                    elif peak > 0 and pnl_pct <= peak - cfg["trailing_stop_pct"]:
                        should_close, reason = True, "TRAIL"
                    elif pnl_pct <= -cfg["trailing_stop_pct"]:
                        should_close, reason = True, "SL"
                    if should_close:
                        trade = do_sell(state, position)
                        if trade:
                            with state.lock:
                                state.positions.remove(position)
                                state.trades.append(trade)
                            state.log(f"SOLD {position['symbol']} ({reason}) {trade['profit']:+.4f} SOL", "sell-win" if trade["profit"] >= 0 else "sell-loss")
                            save_persisted(state)
            if len(state.positions) < cfg["max_positions"] and state.wallet:
                tokens = discover_tokens()
                for token in tokens:
                    if len(state.positions) >= cfg["max_positions"]:
                        break
                    score = 0
                    if token["liquidity"] >= cfg["min_liquidity_usd"]:
                        score += 25
                    if token["volume_24h"] >= cfg["min_volume_24h"]:
                        score += 20
                    total_tx = token["buys_24h"] + token["sells_24h"]
                    if total_tx > 0 and token["buys_24h"] / total_tx >= 0.6:
                        score += 15
                    if cfg["use_ai"]:
                        ai_score = ai_engine.get_consensus_score(token)
                        if ai_score is not None:
                            token["ai_score"] = ai_score
                            score = int((score * 0.5) + (ai_score * 0.5))
                    token["score"] = score
                    if score >= cfg["min_score"]:
                        position = do_buy(state, token, cfg["snipe_amount"])
                        if position:
                            with state.lock:
                                state.positions.append(position)
                            state.log(f"BOUGHT {token['symbol']} score={score}", "buy")
                            save_persisted(state)
                            time.sleep(1)
            time.sleep(SCAN_INTERVAL_SECONDS)
        except Exception as e:
            state.log(f"ERROR: {e}", "sell-loss")
            time.sleep(5)

@st.cache_resource
def start_engine(_state, _ai):
    t = threading.Thread(target=engine_loop, args=(_state, _ai), daemon=True)
    t.start()
    return t

# ================================================================
# JARVIS CSS
# ================================================================
st.set_page_config(page_title="J.A.R.V.I.S. // CYBER SNIPER", page_icon="◉", layout="wide", initial_sidebar_state="expanded")

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@400;700;900&family=Share+Tech+Mono&display=swap');

.stApp {
    background: radial-gradient(ellipse at 50% -20%, rgba(0,220,255,.16), transparent 42%),
                radial-gradient(ellipse at 100% 50%, rgba(0,110,255,.09), transparent 35%),
                radial-gradient(ellipse at 0% 80%, rgba(0,255,180,.07), transparent 35%),
                #02060b;
    color: #dcefff;
}

.stApp::before {
    content: "";
    position: fixed;
    inset: 0;
    pointer-events: none;
    background-image: linear-gradient(rgba(0,205,255,.025) 1px, transparent 1px),
                      linear-gradient(90deg, rgba(0,205,255,.025) 1px, transparent 1px);
    background-size: 45px 45px;
    mask-image: linear-gradient(to bottom, black, transparent 95%);
    z-index: -2;
}

.stApp::after {
    content: "";
    position: fixed;
    inset: 0;
    pointer-events: none;
    background: repeating-linear-gradient(0deg, rgba(255,255,255,.015), rgba(255,255,255,.015) 1px, transparent 1px, transparent 5px);
    z-index: 999;
}

.jarvis-header {
    position: relative;
    min-height: 175px;
    padding: 30px;
    margin-bottom: 20px;
    border: 1px solid rgba(0,225,255,.55);
    border-radius: 6px;
    background: linear-gradient(135deg, rgba(4,15,25,.96), rgba(3,24,35,.88));
    box-shadow: inset 0 0 50px rgba(0,180,255,.04), 0 0 35px rgba(0,200,255,.08);
}

.jarvis-header::before, .jarvis-header::after {
    content: "";
    position: absolute;
    width: 40px;
    height: 40px;
    border-color: #00eaff;
    border-style: solid;
}

.jarvis-header::before { left: -1px; top: -1px; border-width: 2px 0 0 2px; }
.jarvis-header::after { right: -1px; bottom: -1px; border-width: 0 2px 2px 0; }

.arc-reactor {
    position: absolute;
    right: 45px;
    top: 35px;
    width: 82px;
    height: 82px;
    border: 1px solid rgba(0,225,255,.5);
    border-radius: 50%;
    box-shadow: 0 0 15px rgba(0,225,255,.3), inset 0 0 15px rgba(0,225,255,.2);
}

.arc-reactor::before {
    content: "";
    position: absolute;
    left: 50%;
    top: 50%;
    width: 42px;
    height: 42px;
    transform: translate(-50%, -50%);
    border: 3px solid #00eaff;
    border-radius: 50%;
    box-shadow: 0 0 18px #00eaff, inset 0 0 12px #00eaff;
}

.arc-reactor::after {
    content: "";
    position: absolute;
    left: 50%;
    top: 50%;
    width: 9px;
    height: 9px;
    transform: translate(-50%, -50%);
    border-radius: 50%;
    background: white;
    box-shadow: 0 0 15px 5px #00eaff;
}

.jarvis-label { font-family: Orbitron; font-size: 11px; color: rgba(0,225,255,.65); letter-spacing: 5px; margin-bottom: 8px; }
.jarvis-title { font-family: Orbitron; font-weight: 700; font-size: 42px; letter-spacing: 8px; color: #e8fbff; text-shadow: 0 0 12px rgba(0,225,255,.8); }
.jarvis-subtitle { font-family: 'Share Tech Mono'; color: #6ccfe5; letter-spacing: 2px; margin-top: 5px; }

.panel { background: linear-gradient(145deg, rgba(5,15,23,.96), rgba(2,10,17,.92)); border: 1px solid rgba(0,190,230,.28); border-radius: 5px; padding: 18px; box-shadow: inset 0 0 35px rgba(0,180,255,.025); margin-bottom: 12px; }
.panel-title { font-family: Orbitron; color: #67e8ff; font-size: 12px; letter-spacing: 2px; margin-bottom: 12px; border-bottom: 1px solid rgba(0,200,255,.12); padding-bottom: 8px; }

.metric { background: rgba(3,15,23,.9); border-left: 2px solid #00dfff; padding: 12px 14px; min-height: 75px; }
.metric-label { color: #557b87; font-size: 10px; font-family: 'Share Tech Mono'; letter-spacing: 2px; }
.metric-value { color: #dffbff; font-family: Orbitron; font-size: 20px; margin-top: 7px; }

.status-online { color: #00ffae; text-shadow: 0 0 12px rgba(0,255,174,.8); }
.status-offline { color: #ff426d; text-shadow: 0 0 12px rgba(255,66,109,.8); }

.terminal { height: 240px; overflow-y: auto; background: #010305; border: 1px solid rgba(0,255,174,.22); padding: 12px; font-family: 'Share Tech Mono'; font-size: 11px; color: #68d8b2; }
.terminal .buy { color: #00ffe7; }
.terminal .sell-win { color: #00ff88; }
.terminal .sell-loss { color: #ff245f; }
.terminal .sys { color: #ff00ea; }

.chat-user { background: rgba(0,125,170,.08); border-left: 2px solid #00cfff; padding: 14px; margin: 8px 0; border-radius: 3px; }
.chat-jarvis { background: rgba(0,255,180,.035); border-left: 2px solid #00ffae; padding: 14px; margin: 8px 0 18px; border-radius: 3px; }
.chat-label { font-family: 'Share Tech Mono'; font-size: 10px; letter-spacing: 2px; color: #54aabb; margin-bottom: 7px; }

.stButton > button {
    background: linear-gradient(135deg, #03131d, #05222d);
    color: #70eaff;
    border: 1px solid rgba(0,220,255,.5);
    border-radius: 3px;
    font-family: Orbitron;
    font-size: 10px;
    letter-spacing: 1px;
    transition: all .2s ease;
}

.stButton > button:hover {
    background: #00cfff;
    color: #001018;
    box-shadow: 0 0 20px rgba(0,220,255,.35);
}

section[data-testid="stSidebar"] {
    background: linear-gradient(180deg, #02080d, #030b11);
    border-right: 1px solid rgba(0,210,255,.22);
}

.hud-line { height: 1px; background: linear-gradient(90deg, transparent, #00dfff, transparent); opacity: .25; margin: 15px 0; }

#MainMenu { visibility: hidden; }
footer { visibility: hidden; }
header { background: transparent !important; }
</style>
""", unsafe_allow_html=True)

# ================================================================
# INIT
# ================================================================
state = get_state()
ai_engine = AIEngine(FREELLM_API_KEY)

if ENV_PRIVATE_KEY and state.wallet is None:
    try:
        state.wallet = Keypair.from_base58_string(ENV_PRIVATE_KEY)
        state.wallet_address = str(state.wallet.pubkey())
        state.log("WALLET CONNECTED", "sys")
    except:
        state.log("WALLET ERROR", "sell-loss")

start_engine(state, ai_engine)

# ================================================================
# JARVIS HEADER
# ================================================================
st.markdown("""
<div class="jarvis-header">
    <div class="arc-reactor"></div>
    <div class="jarvis-label">STARK INDUSTRIES // AUTONOMOUS TRADING SYSTEM</div>
    <div class="jarvis-title">J.A.R.V.I.S.</div>
    <div class="jarvis-subtitle">JUST A RATHER VERY INTELLIGENT SYSTEM // CYBER SNIPER COMMAND CENTER</div>
</div>
""", unsafe_allow_html=True)

# ================================================================
# TOP HUD METRICS
# ================================================================
with state.lock:
    positions = list(state.positions)
    trades = list(state.trades)
    logs = list(state.logs)

balance = get_wallet_balance(state.wallet_address) if state.wallet else 0
total_trades = len(trades)
wins = sum(1 for t in trades if t.get("profit", 0) > 0)
win_rate = (wins / total_trades * 100) if total_trades else 0
total_pnl = sum(t.get("profit", 0) for t in trades)

c1, c2, c3, c4, c5 = st.columns(5)

with c1:
    st.markdown(f'<div class="metric"><div class="metric-label">WALLET</div><div class="metric-value">{balance:.4f} SOL</div></div>', unsafe_allow_html=True)
with c2:
    st.markdown(f'<div class="metric"><div class="metric-label">POSITIONS</div><div class="metric-value">{len(positions)}</div></div>', unsafe_allow_html=True)
with c3:
    st.markdown(f'<div class="metric"><div class="metric-label">TRADES</div><div class="metric-value">{total_trades}</div></div>', unsafe_allow_html=True)
with c4:
    color = "#00ff88" if total_pnl >= 0 else "#ff245f"
    st.markdown(f'<div class="metric"><div class="metric-label">NET P/L</div><div class="metric-value" style="color:{color}">{total_pnl:+.4f} SOL</div></div>', unsafe_allow_html=True)
with c5:
    st.markdown(f'<div class="metric"><div class="metric-label">WIN RATE</div><div class="metric-value">{win_rate:.1f}%</div></div>', unsafe_allow_html=True)

st.markdown('<div class="hud-line"></div>', unsafe_allow_html=True)

# ================================================================
# SIDEBAR
# ================================================================
with st.sidebar:
    st.markdown("### 🔑 WALLET")
    if state.wallet:
        st.success(f"Connected: {state.wallet_address[:8]}...")
    else:
        st.error("No wallet in secrets")
    
    st.markdown("---")
    st.markdown("### 🤖 AI MODELS")
    if FREELLM_API_KEY:
        for model_id, info in AI_MODELS.items():
            status = ai_engine.model_status.get(model_id, "🔴")
            st.markdown(f"{status} {info['name']}")
    else:
        st.error("No AI key")
    
    st.markdown("---")
    st.markdown("### 🎯 STRATEGY")
    use_ai = st.checkbox("Enable AI", value=True)
    snipe_amount = st.slider("Buy SOL", 0.01, 0.50, 0.05, 0.01)
    take_profit = st.slider("TP %", 10, 300, 50, 5)
    trailing_stop = st.slider("Trail %", 5, 50, 15, 5)
    min_liquidity = st.number_input("Min Liq $", 1000, 200000, 15000, 1000)
    min_score = st.slider("Min Score", 0, 100, 45, 5)
    max_positions = st.slider("Max Positions", 1, 10, 5)
    
    with state.lock:
        state.config.update({
            "use_ai": use_ai,
            "snipe_amount": snipe_amount,
            "take_profit_pct": float(take_profit),
            "trailing_stop_pct": float(trailing_stop),
            "min_liquidity_usd": float(min_liquidity),
            "min_score": int(min_score),
            "max_positions": int(max_positions),
        })
    
    st.markdown("---")
    if not state.running:
        if st.button("⚡ ACTIVATE TRADING", type="primary"):
            if state.wallet:
                state.running = True
                state.log("TRADING ACTIVATED", "sys")
                st.rerun()
            else:
                st.error("No wallet!")
    else:
        if st.button("🛑 STOP"):
            state.running = False
            st.rerun()

# ================================================================
# JARVIS CHAT
# ================================================================
st.markdown('<div class="panel-title">◉ JARVIS INTELLIGENCE TERMINAL</div>', unsafe_allow_html=True)

for msg in state.jarvis_messages[-10:]:
    if msg["role"] == "user":
        st.markdown(f'<div class="chat-user"><div class="chat-label">COMMAND // USER</div>{msg["content"]}</div>', unsafe_allow_html=True)
    else:
        st.markdown(f'<div class="chat-jarvis"><div class="chat-label">J.A.R.V.I.S. // RESPONSE</div>{msg["content"]}</div>', unsafe_allow_html=True)

jarvis_prompt = st.chat_input("Speak to JARVIS...")

if jarvis_prompt:
    state.jarvis_messages.append({"role": "user", "content": jarvis_prompt})
    with st.spinner("JARVIS PROCESSING..."):
        response = ai_engine.jarvis_query(jarvis_prompt)
        if response:
            state.jarvis_messages.append({"role": "assistant", "content": response})
        else:
            state.jarvis_messages.append({"role": "assistant", "content": "I apologize, but my AI core is currently offline."})
    st.rerun()

# ================================================================
# STATUS
# ================================================================
st.markdown('<div class="hud-line"></div>', unsafe_allow_html=True)

status_color = "#00ff88" if state.running else "#ff245f"
status_text = "TRADING ACTIVE" if state.running else "ENGINE OFFLINE"
st.markdown(f'<div style="text-align:center;padding:15px;border:1px solid {status_color};border-radius:8px;"><h2 style="color:{status_color};font-family:Orbitron;margin:0;">{status_text}</h2></div>', unsafe_allow_html=True)

# ================================================================
# POSITIONS
# ================================================================
st.markdown("### 📌 POSITIONS")
if positions:
    for i, pos in enumerate(positions):
        st.markdown(f'<div class="panel">🎯 {pos["symbol"]} | {pos["entry_sol"]} SOL | Score: {pos.get("score", 0)}</div>', unsafe_allow_html=True)
        if st.button(f"SELL {pos['symbol']}", key=f"sell_{i}"):
            trade = do_sell(state, pos)
            if trade:
                with state.lock:
                    state.positions.remove(pos)
                    state.trades.append(trade)
                st.rerun()
else:
    st.info("No open positions")

# ================================================================
# TERMINAL
# ================================================================
st.markdown("### 🖥️ SYSTEM TERMINAL")
terminal = "".join(logs) or '<div class="sys">// JARVIS READY //</div>'
st.markdown(f'<div class="terminal">{terminal}</div>', unsafe_allow_html=True)

if HAS_AUTOREFRESH:
    st_autorefresh(interval=6000, key="refresh")

st.markdown("---")
st.markdown('<div style="text-align:center;color:#00ffe7;font-family:Orbitron;">⚡ J.A.R.V.I.S. // CYBER SNIPER v11.0 // REAL + AI</div>', unsafe_allow_html=True)

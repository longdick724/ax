# app.py - J.A.R.V.I.S. // CYBER SNIPER - Immersive HUD + Real Trading
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
# CONFIG
# ================================================================
APP_VERSION = "13.0"

SOL_MINT = "So11111111111111111111111111111111111111112"

HELIUS_KEY = os.getenv("HELIUS_KEY", "").strip()
FREELLM_API_KEY = os.getenv("FREELLM_API_KEY", "").strip()
ENV_PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY", "").strip()

HELIUS_RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_KEY}" if HELIUS_KEY else ""
JUPITER_API_BASE = "https://quote-api.jup.ag/v6"
DEXSCREENER_API = "https://api.dexscreener.com"
FREELLM_BASE = "https://api.freellmapi.com/v1"

STATE_FILE = "cyber_sniper_state.json"
MAX_TRADE_SOL_CAP = 0.50
REQUEST_TIMEOUT = 90
SCAN_INTERVAL_SECONDS = 15

# ================================================================
# AI ENGINE
# ================================================================
class AIEngine:
    def __init__(self, api_key):
        self.api_key = api_key
        self.cache = {}
    
    def query(self, model, prompt, max_tokens=500):
        if not self.api_key:
            return None
        try:
            headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "temperature": 0.3
            }
            response = requests.post(f"{FREELLM_BASE}/chat/completions", headers=headers, json=payload, timeout=20, verify=False)
            if response.status_code == 200:
                return response.json().get("choices", [{}])[0].get("message", {}).get("content", "")
            return None
        except:
            return None
    
    def jarvis_speak(self, prompt):
        system = "You are J.A.R.V.I.S., Tony Stark's AI assistant. Be calm, professional, concise, and helpful."
        full_prompt = f"{system}\n\nUser: {prompt}\nJARVIS:"
        return self.query("groq/gpt-oss-120b", full_prompt, 1000)
    
    def get_ai_score(self, token_data):
        prompt = f"Score 0-100 for trading: {token_data.get('symbol')} liq=${token_data.get('liquidity',0):,.0f} vol=${token_data.get('volume_24h',0):,.0f}. Return only number."
        response = self.query("groq/gpt-oss-20b", prompt, 50)
        if response:
            numbers = re.findall(r'\d+', response)
            if numbers:
                return min(100, max(0, int(numbers[0])))
        return None

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
    chat: List[Dict] = field(default_factory=list)
    
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
        with self.lock:
            self.logs.insert(0, f"[{ts}] {message}")
            self.logs = self.logs[:100]

@st.cache_resource
def get_state():
    state = EngineState()
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                data = json.load(f)
            state.positions = data.get("positions", [])
            state.trades = data.get("trades", [])
        except:
            pass
    return state

def save_state(state):
    try:
        with state.lock:
            data = {"positions": state.positions, "trades": state.trades}
        with open(STATE_FILE, "w") as f:
            json.dump(data, f)
    except:
        pass

# ================================================================
# TRADING FUNCTIONS
# ================================================================
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "JARVIS/13.0"})

def rpc_call(method, params):
    if not HELIUS_RPC_URL:
        return None
    try:
        r = HTTP.post(HELIUS_RPC_URL, json={"jsonrpc":"2.0","id":1,"method":method,"params":params}, timeout=15, verify=False)
        if r.status_code == 200:
            return r.json().get("result")
    except:
        pass
    return None

def get_balance(pubkey):
    if not pubkey:
        return 0.0
    result = rpc_call("getBalance", [pubkey])
    return float(result.get("value", 0)) / 1_000_000_000 if result else 0.0

def discover_tokens():
    tokens = []
    data = None
    try:
        r = HTTP.get(f"{DEXSCREENER_API}/token-boosts/latest/v1", timeout=15, verify=False)
        if r.status_code == 200:
            data = r.json()
    except:
        pass
    
    if data:
        mints = [x.get("tokenAddress") for x in data if x.get("chainId") == "solana"]
        for start in range(0, min(len(mints), 30), 30):
            try:
                r = HTTP.get(f"{DEXSCREENER_API}/tokens/v1/solana/{','.join(mints[start:start+30])}", timeout=15, verify=False)
                if r.status_code == 200:
                    for pair in r.json():
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
            except:
                pass
    return tokens[:30]

def jupiter_quote(input_mint, output_mint, amount, slippage_bps=500):
    try:
        r = HTTP.get(f"{JUPITER_API_BASE}/quote", params={"inputMint":input_mint,"outputMint":output_mint,"amount":amount,"slippageBps":slippage_bps}, timeout=15, verify=False)
        return r.json() if r.status_code == 200 else None
    except:
        return None

def jupiter_swap(quote, wallet):
    try:
        r = HTTP.post(f"{JUPITER_API_BASE}/swap", json={"quoteResponse":quote,"userPublicKey":str(wallet.pubkey()),"wrapAndUnwrapSol":True}, timeout=25, verify=False)
        if r.status_code != 200:
            return None
        raw = base64.b64decode(r.json()["swapTransaction"])
        unsigned = VersionedTransaction.from_bytes(raw)
        signed = VersionedTransaction(unsigned.message, [wallet])
        encoded = base64.b64encode(bytes(signed)).decode()
        r2 = HTTP.post(HELIUS_RPC_URL, json={"jsonrpc":"2.0","id":1,"method":"sendTransaction","params":[encoded,{"encoding":"base64","skipPreflight":False}]}, timeout=20, verify=False)
        if r2.status_code == 200:
            sig = r2.json().get("result")
            time.sleep(3)
            return sig
    except Exception as e:
        print(f"Swap error: {e}")
    return None

def do_buy(state, token, sol_amount):
    if state.wallet is None:
        return None
    quote = jupiter_quote(SOL_MINT, token["mint"], int(sol_amount * 1_000_000_000))
    if not quote:
        return None
    sig = jupiter_swap(quote, state.wallet)
    if not sig:
        return None
    return {
        "mint": token["mint"],
        "symbol": token.get("symbol", "UNKNOWN"),
        "entry_sol": sol_amount,
        "out_amount": int(quote.get("outAmount", 0)),
        "opened_at": datetime.now().isoformat(),
        "buy_sig": sig,
        "peak_pnl_pct": 0.0,
        "score": token.get("score", 0),
    }

def do_sell(state, position):
    if state.wallet is None:
        return None
    result = rpc_call("getTokenAccountsByOwner", [state.wallet_address, {"mint": position["mint"]}, {"encoding":"jsonParsed"}])
    if not result:
        return None
    try:
        amount = int(result["value"][0]["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
    except:
        return None
    if amount <= 0:
        return None
    quote = jupiter_quote(position["mint"], SOL_MINT, amount, 1000)
    if not quote:
        return None
    sig = jupiter_swap(quote, state.wallet)
    if not sig:
        return None
    exit_sol = int(quote.get("outAmount", 0)) / 1_000_000_000
    profit = exit_sol - position["entry_sol"]
    return {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "time": datetime.now().strftime("%H:%M:%S"),
        "symbol": position["symbol"],
        "entry_sol": position["entry_sol"],
        "exit_sol": exit_sol,
        "profit": profit,
    }

# ================================================================
# ENGINE
# ================================================================
def engine_loop(state, ai):
    state.log("JARVIS trading engine online")
    while True:
        try:
            if not state.running:
                time.sleep(2)
                continue
            
            for pos in list(state.positions):
                quote = jupiter_quote(pos["mint"], SOL_MINT, pos.get("out_amount", 1), 1000)
                if quote:
                    current = int(quote.get("outAmount", 0)) / 1_000_000_000
                    pnl = ((current - pos["entry_sol"]) / pos["entry_sol"]) * 100
                    pos["peak_pnl_pct"] = max(pos.get("peak_pnl_pct", 0), pnl)
                    
                    if pnl >= state.config["take_profit_pct"] or pnl <= -state.config["trailing_stop_pct"]:
                        trade = do_sell(state, pos)
                        if trade:
                            state.positions.remove(pos)
                            state.trades.append(trade)
                            state.log(f"Sold {pos['symbol']} {trade['profit']:+.4f} SOL")
                            save_state(state)
            
            if len(state.positions) < state.config["max_positions"] and state.wallet:
                tokens = discover_tokens()
                for token in tokens:
                    if len(state.positions) >= state.config["max_positions"]:
                        break
                    
                    score = 0
                    if token["liquidity"] >= state.config["min_liquidity_usd"]:
                        score += 30
                    if token["volume_24h"] >= state.config["min_volume_24h"]:
                        score += 20
                    if token["buys_24h"] > token["sells_24h"] * 1.5:
                        score += 15
                    
                    if state.config["use_ai"]:
                        ai_score = ai.get_ai_score(token)
                        if ai_score:
                            score = int((score + ai_score) / 2)
                    
                    token["score"] = score
                    
                    if score >= state.config["min_score"]:
                        pos = do_buy(state, token, state.config["snipe_amount"])
                        if pos:
                            state.positions.append(pos)
                            state.log(f"Bought {token['symbol']} score={score}")
                            save_state(state)
                            time.sleep(1)
            
            time.sleep(SCAN_INTERVAL_SECONDS)
        except Exception as e:
            state.log(f"Error: {e}")
            time.sleep(5)

@st.cache_resource
def start_engine(_state, _ai):
    t = threading.Thread(target=engine_loop, args=(_state, _ai), daemon=True)
    t.start()
    return t

# ================================================================
# J.A.R.V.I.S. // IMMERSIVE HUD INTERFACE
# ================================================================
st.set_page_config(
    page_title="J.A.R.V.I.S.",
    page_icon="◉",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600&family=Orbitron:wght@300;400;500;600&display=swap');

:root {
    --cyan: #62e6ff;
    --cyan2: #1bb9df;
    --blue: #3d8cff;
    --white: #eafaff;
    --muted: #66808c;
    --line: rgba(98,230,255,.18);
    --glass: rgba(4,13,20,.68);
}

.stApp {
    background:
        radial-gradient(circle at 50% 45%,
            rgba(20,115,145,.12) 0%,
            rgba(5,13,20,.25) 25%,
            #02070b 65%,
            #010305 100%);
    color: var(--white);
    font-family: 'Inter', sans-serif;
}

.main .block-container {
    max-width: 1500px;
    padding-top: 20px;
    padding-left: 35px;
    padding-right: 35px;
    padding-bottom: 100px;
}

section[data-testid="stSidebar"] {
    display: none;
}

header[data-testid="stHeader"] {
    background: transparent;
}

#MainMenu { visibility: hidden; }
footer { visibility: hidden; }

.hud-top {
    height: 55px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    border-bottom: 1px solid var(--line);
    color: var(--muted);
    font-family: 'Orbitron', sans-serif;
    font-size: 10px;
    letter-spacing: 3px;
    margin-bottom: 15px;
}

.hud-brand { color: var(--cyan); font-size: 12px; letter-spacing: 5px; }
.hud-status { color: #62e6ff; }
.hud-wallet { color: #7795a2; }

.jarvis-stage {
    position: relative;
    min-height: 680px;
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
    border-bottom: 1px solid rgba(98,230,255,.08);
}

.jarvis-stage::before {
    content: "";
    position: absolute;
    inset: 0;
    background-image:
        linear-gradient(rgba(98,230,255,.025) 1px, transparent 1px),
        linear-gradient(90deg, rgba(98,230,255,.025) 1px, transparent 1px);
    background-size: 45px 45px;
    mask-image: radial-gradient(ellipse at center, black 0%, transparent 72%);
}

.jarvis-core {
    position: relative;
    width: 360px;
    height: 360px;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 5;
    background: radial-gradient(circle, rgba(90,230,255,.22) 0%, rgba(20,130,170,.08) 28%, transparent 62%);
    box-shadow: 0 0 30px rgba(58,210,255,.18), 0 0 100px rgba(58,210,255,.10);
}

.jarvis-core::before, .jarvis-core::after {
    content: "";
    position: absolute;
    border-radius: 50%;
    border: 1px solid rgba(98,230,255,.28);
    animation: rotateHUD 18s linear infinite;
}

.jarvis-core::before {
    inset: -25px;
    border-left-color: transparent;
    border-right-color: rgba(98,230,255,.65);
}

.jarvis-core::after {
    inset: -50px;
    border-color: rgba(98,230,255,.10);
    border-top-color: rgba(98,230,255,.55);
    animation-duration: 28s;
    animation-direction: reverse;
}

@keyframes rotateHUD {
    from { transform: rotate(0deg); }
    to { transform: rotate(360deg); }
}

.core-inner {
    width: 230px;
    height: 230px;
    border-radius: 50%;
    border: 1px solid rgba(98,230,255,.5);
    box-shadow: inset 0 0 35px rgba(98,230,255,.10), 0 0 25px rgba(98,230,255,.15);
    display: flex;
    flex-direction: column;
    justify-content: center;
    align-items: center;
    background: radial-gradient(circle, rgba(20,90,120,.25), rgba(1,7,11,.88));
}

.core-eye {
    width: 52px;
    height: 52px;
    border-radius: 50%;
    background: radial-gradient(circle, #e8fbff 0%, #62e6ff 20%, #168cad 45%, transparent 70%);
    box-shadow: 0 0 15px #62e6ff, 0 0 50px rgba(98,230,255,.6);
    animation: pulseCore 3s ease-in-out infinite;
}

@keyframes pulseCore {
    0%,100% { transform: scale(.9); opacity: .75; }
    50% { transform: scale(1.08); opacity: 1; }
}

.core-name {
    margin-top: 25px;
    color: #dffaff;
    font-family: 'Orbitron', sans-serif;
    font-size: 20px;
    font-weight: 400;
    letter-spacing: 7px;
}

.core-state {
    margin-top: 8px;
    color: #5d8794;
    font-size: 9px;
    letter-spacing: 3px;
}

.hud-panel {
    position: absolute;
    width: 230px;
    padding: 15px 18px;
    background: linear-gradient(135deg, rgba(8,24,32,.78), rgba(2,9,14,.55));
    border: 1px solid rgba(98,230,255,.16);
    backdrop-filter: blur(10px);
    z-index: 4;
}

.hud-panel::before {
    content: "";
    position: absolute;
    top: -1px;
    left: -1px;
    width: 25px;
    height: 25px;
    border-top: 1px solid var(--cyan);
    border-left: 1px solid var(--cyan);
}

.panel-title {
    color: var(--cyan);
    font-family: 'Orbitron', sans-serif;
    font-size: 9px;
    letter-spacing: 3px;
    margin-bottom: 13px;
}

.panel-value {
    color: #e5faff;
    font-family: 'Orbitron', sans-serif;
    font-size: 21px;
}

.panel-sub {
    color: #58727d;
    font-size: 9px;
    margin-top: 5px;
    letter-spacing: 1px;
}

.panel-left-top { left: 3%; top: 14%; }
.panel-left-bottom { left: 3%; bottom: 13%; }
.panel-right-top { right: 3%; top: 14%; }
.panel-right-bottom { right: 3%; bottom: 13%; }

.signal-line {
    height: 2px;
    background: linear-gradient(90deg, transparent, var(--cyan), transparent);
    margin-top: 10px;
    opacity: .55;
}

.command-title {
    text-align: center;
    color: #58727d;
    font-family: 'Orbitron', sans-serif;
    font-size: 9px;
    letter-spacing: 4px;
    margin-top: 25px;
}

div[data-testid="stChatInput"] textarea {
    background: rgba(3,12,17,.82) !important;
    border: 1px solid rgba(98,230,255,.35) !important;
    border-radius: 2px !important;
    color: #e8fbff !important;
    font-family: 'Inter', sans-serif !important;
    padding: 16px 20px !important;
}

.stButton > button {
    background: rgba(4,16,22,.75) !important;
    color: var(--cyan) !important;
    border: 1px solid rgba(98,230,255,.3) !important;
    border-radius: 2px !important;
    font-family: 'Orbitron', sans-serif !important;
    font-size: 9px !important;
    letter-spacing: 2px !important;
}

.stButton > button:hover {
    background: rgba(98,230,255,.12) !important;
    border-color: var(--cyan) !important;
}

.jarvis-message {
    max-width: 800px;
    margin: 12px auto;
    padding: 15px 20px;
    border-left: 2px solid var(--cyan);
    background: rgba(5,18,24,.55);
    color: #c8e1e8;
    line-height: 1.6;
    font-size: 13px;
}

.jarvis-message-user {
    border-left: 2px solid #426a77;
    color: #7e9ba4;
}

.log-line {
    color: #4f737e;
    font-family: 'Courier New', monospace;
    font-size: 10px;
    line-height: 1.8;
}

@media (max-width: 900px) {
    .jarvis-stage { min-height: 550px; }
    .jarvis-core { width: 260px; height: 260px; }
    .core-inner { width: 170px; height: 170px; }
    .hud-panel { width: 160px; padding: 10px; }
    .panel-left-top { left: 1%; top: 5%; }
    .panel-left-bottom { left: 1%; bottom: 5%; }
    .panel-right-top { right: 1%; top: 5%; }
    .panel-right-bottom { right: 1%; bottom: 5%; }
}
</style>
""", unsafe_allow_html=True)

# ================================================================
# INIT
# ================================================================
state = get_state()
ai = AIEngine(FREELLM_API_KEY)

if ENV_PRIVATE_KEY and state.wallet is None:
    try:
        state.wallet = Keypair.from_base58_string(ENV_PRIVATE_KEY)
        state.wallet_address = str(state.wallet.pubkey())
    except Exception:
        pass

start_engine(state, ai)

balance = get_balance(state.wallet_address) if state.wallet else 0
total_trades = len(state.trades)
wins = sum(1 for t in state.trades if t.get("profit", 0) > 0)
win_rate = (wins / total_trades * 100) if total_trades else 0
net_pnl = sum(t.get("profit", 0) for t in state.trades)

# ================================================================
# TOP HUD
# ================================================================
engine_state = "ONLINE" if state.running else "STANDBY"
wallet_text = state.wallet_address[:6] + "..." + state.wallet_address[-4:] if state.wallet_address else "NOT CONNECTED"

st.markdown(f"""
<div class="hud-top">
    <div class="hud-brand">J.A.R.V.I.S.</div>
    <div class="hud-status">◉ SYSTEM {engine_state}</div>
    <div class="hud-wallet">{wallet_text}</div>
</div>
""", unsafe_allow_html=True)

# ================================================================
# MAIN JARVIS STAGE
# ================================================================
pnl_color = "#62e6ff" if net_pnl >= 0 else "#ff5e67"

st.markdown(f"""
<div class="jarvis-stage">
    <div class="hud-panel panel-left-top">
        <div class="panel-title">CORE BALANCE</div>
        <div class="panel-value">{balance:.4f}</div>
        <div class="panel-sub">SOL AVAILABLE</div>
        <div class="signal-line"></div>
    </div>

    <div class="hud-panel panel-left-bottom">
        <div class="panel-title">ACTIVE TARGETS</div>
        <div class="panel-value">{len(state.positions)}</div>
        <div class="panel-sub">/ {state.config["max_positions"]} ALLOCATED</div>
    </div>

    <div class="hud-panel panel-right-top">
        <div class="panel-title">PERFORMANCE</div>
        <div class="panel-value">{win_rate:.1f}%</div>
        <div class="panel-sub">{wins} WINS / {total_trades} TRADES</div>
        <div class="signal-line"></div>
    </div>

    <div class="hud-panel panel-right-bottom">
        <div class="panel-title">NET P/L</div>
        <div class="panel-value" style="color:{pnl_color}">{net_pnl:+.4f}</div>
        <div class="panel-sub">SOL REALIZED</div>
    </div>

    <div class="jarvis-core">
        <div class="core-inner">
            <div class="core-eye"></div>
            <div class="core-name">JARVIS</div>
            <div class="core-state">{engine_state}</div>
        </div>
    </div>
</div>
""", unsafe_allow_html=True)

# ================================================================
# COMMAND INTERFACE
# ================================================================
st.markdown('<div class="command-title">SPEAK TO J.A.R.V.I.S.</div>', unsafe_allow_html=True)

for msg in state.chat[-4:]:
    if msg["role"] == "user":
        st.markdown(f'<div class="jarvis-message jarvis-message-user"><b>YOU</b><br>{msg["content"]}</div>', unsafe_allow_html=True)
    else:
        st.markdown(f'<div class="jarvis-message"><b style="color:#62e6ff">J.A.R.V.I.S.</b><br>{msg["content"]}</div>', unsafe_allow_html=True)

prompt = st.chat_input("Command J.A.R.V.I.S. ...")

if prompt:
    state.chat.append({"role": "user", "content": prompt})
    with st.spinner("JARVIS PROCESSING..."):
        response = ai.jarvis_speak(prompt)
        if response:
            state.chat.append({"role": "assistant", "content": response})
        else:
            state.chat.append({"role": "assistant", "content": "My conversational core is currently unavailable."})
    st.rerun()

# ================================================================
# SYSTEM CONTROLS
# ================================================================
st.markdown("---")

control_left, control_mid, control_right = st.columns([1, 1, 1])

with control_left:
    if not state.running:
        if st.button("ACTIVATE SYSTEM", use_container_width=True):
            if state.wallet:
                state.running = True
                state.log("JARVIS manually activated")
                st.rerun()
            else:
                st.error("Wallet not configured.")
    else:
        if st.button("DEACTIVATE SYSTEM", use_container_width=True):
            state.running = False
            state.log("JARVIS manually deactivated")
            st.rerun()

with control_mid:
    show_settings = st.toggle("SYSTEM SETTINGS", value=False)

with control_right:
    if st.button("REFRESH HUD", use_container_width=True):
        st.rerun()

# ================================================================
# SETTINGS
# ================================================================
if show_settings:
    s1, s2, s3 = st.columns(3)
    
    with s1:
        snipe = st.number_input("BUY SIZE", 0.01, MAX_TRADE_SOL_CAP, float(state.config["snipe_amount"]), 0.01)
        min_liq = st.number_input("MIN LIQUIDITY", 1000.0, 200000.0, float(state.config["min_liquidity_usd"]), 1000.0)
    
    with s2:
        tp = st.number_input("TAKE PROFIT %", 5.0, 300.0, float(state.config["take_profit_pct"]), 5.0)
        trail = st.number_input("TRAILING STOP %", 1.0, 50.0, float(state.config["trailing_stop_pct"]), 1.0)
    
    with s3:
        min_score = st.number_input("MINIMUM SCORE", 0, 100, int(state.config["min_score"]), 5)
        max_pos = st.number_input("MAX POSITIONS", 1, 10, int(state.config["max_positions"]), 1)
    
    with state.lock:
        state.config.update({
            "snipe_amount": float(snipe),
            "take_profit_pct": float(tp),
            "trailing_stop_pct": float(trail),
            "min_liquidity_usd": float(min_liq),
            "min_score": int(min_score),
            "max_positions": int(max_pos),
        })

# ================================================================
# SYSTEM LOG
# ================================================================
with st.expander("SYSTEM TELEMETRY"):
    for log in state.logs[:20]:
        st.markdown(f'<div class="log-line">{log}</div>', unsafe_allow_html=True)

# ================================================================
# AUTO REFRESH
# ================================================================
if HAS_AUTOREFRESH:
    st_autorefresh(interval=6000, key="jarvis_hud_refresh")

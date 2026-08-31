# app.py - J.A.R.V.I.S. // Complete Fixed Version with Rotating Rings
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
import streamlit.components.v1 as components

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
APP_VERSION = "15.0 FINAL"

SOL_MINT = "So11111111111111111111111111111111111111112"

HELIUS_KEY = os.getenv("HELIUS_KEY", "").strip()
FREELLM_API_KEY = os.getenv("FREELLM_API_KEY", "").strip()
ENV_PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY", "").strip()

HELIUS_RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_KEY}" if HELIUS_KEY else ""
JUPITER_API_BASE = "https://quote-api.jup.ag/v6"
DEXSCREENER_API = "https://api.dexscreener.com"
FREELLM_BASE = "https://api.freellmapi.com/v1"
COINGECKO_API = "https://api.coingecko.com/api/v3"

STATE_FILE = "cyber_sniper_state.json"
MAX_TRADE_SOL_CAP = 0.50

# ================================================================
# AI ENGINE
# ================================================================
class AIEngine:
    def __init__(self, api_key):
        self.api_key = api_key
    
    def query(self, model, prompt, max_tokens=500):
        if not self.api_key:
            return None
        try:
            headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
            payload = {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens}
            r = requests.post(f"{FREELLM_BASE}/chat/completions", headers=headers, json=payload, timeout=20, verify=False)
            if r.status_code == 200:
                return r.json().get("choices", [{}])[0].get("message", {}).get("content", "")
        except:
            pass
        return None
    
    def jarvis_speak(self, prompt):
        return self.query("groq/gpt-oss-120b", f"You are JARVIS. Be concise.\nUser: {prompt}\nJARVIS:")
    
    def get_ai_score(self, token):
        r = self.query("groq/gpt-oss-20b", f"Score 0-100: {token.get('symbol')} liq=${token.get('liquidity',0):,.0f}. Return number only.", 50)
        if r:
            nums = re.findall(r'\d+', r)
            if nums:
                return min(100, max(0, int(nums[0])))
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
        "min_score": 45,
    })
    
    def log(self, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        with self.lock:
            self.logs.insert(0, f"[{ts}] {msg}")
            self.logs = self.logs[:50]

@st.cache_resource
def get_state():
    s = EngineState()
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                d = json.load(f)
            s.positions = d.get("positions", [])
            s.trades = d.get("trades", [])
        except:
            pass
    return s

def save_state(s):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({"positions": s.positions, "trades": s.trades}, f)
    except:
        pass

# ================================================================
# LIVE MARKET
# ================================================================
def get_live_prices():
    try:
        r = requests.get(f"{COINGECKO_API}/simple/price", params={
            "ids": "bitcoin,ethereum,solana,binancecoin,ripple,cardano,dogecoin",
            "vs_currencies": "usd", "include_24hr_change": "true"
        }, timeout=10, verify=False)
        if r.status_code == 200:
            return r.json()
    except:
        pass
    return {}

# ================================================================
# TRADING
# ================================================================
HTTP = requests.Session()

def rpc_call(method, params):
    if not HELIUS_RPC_URL:
        return None
    try:
        r = HTTP.post(HELIUS_RPC_URL, json={"jsonrpc":"2.0","id":1,"method":method,"params":params}, timeout=15, verify=False)
        return r.json().get("result") if r.status_code == 200 else None
    except:
        return None

def get_balance(pubkey):
    if not pubkey:
        return 0.0
    result = rpc_call("getBalance", [pubkey])
    return float(result.get("value", 0)) / 1_000_000_000 if result else 0.0

def discover_tokens():
    tokens = []
    try:
        r = HTTP.get(f"{DEXSCREENER_API}/token-boosts/latest/v1", timeout=15, verify=False)
        if r.status_code == 200:
            mints = [x.get("tokenAddress") for x in r.json() if x.get("chainId") == "solana"]
            for i in range(0, min(len(mints), 30), 30):
                r2 = HTTP.get(f"{DEXSCREENER_API}/tokens/v1/solana/{','.join(mints[i:i+30])}", timeout=15, verify=False)
                if r2.status_code == 200:
                    for pair in r2.json():
                        if pair.get("chainId") == "solana":
                            base = pair.get("baseToken", {})
                            tokens.append({
                                "mint": base.get("address"),
                                "symbol": base.get("symbol", "UNKNOWN"),
                                "liquidity": float((pair.get("liquidity") or {}).get("usd", 0)),
                                "volume_24h": float((pair.get("volume") or {}).get("h24", 0)),
                            })
    except:
        pass
    return tokens[:30]

def jupiter_quote(i, o, a, s=500):
    try:
        r = HTTP.get(f"{JUPITER_API_BASE}/quote", params={"inputMint":i,"outputMint":o,"amount":a,"slippageBps":s}, timeout=15, verify=False)
        return r.json() if r.status_code == 200 else None
    except:
        return None

def jupiter_swap(q, w):
    try:
        r = HTTP.post(f"{JUPITER_API_BASE}/swap", json={"quoteResponse":q,"userPublicKey":str(w.pubkey()),"wrapAndUnwrapSol":True}, timeout=25, verify=False)
        if r.status_code != 200:
            return None
        raw = base64.b64decode(r.json()["swapTransaction"])
        unsigned = VersionedTransaction.from_bytes(raw)
        signed = VersionedTransaction(unsigned.message, [w])
        encoded = base64.b64encode(bytes(signed)).decode()
        r2 = HTTP.post(HELIUS_RPC_URL, json={"jsonrpc":"2.0","id":1,"method":"sendTransaction","params":[encoded,{"encoding":"base64"}]}, timeout=20, verify=False)
        if r2.status_code == 200:
            time.sleep(3)
            return r2.json().get("result")
    except:
        pass
    return None

def do_buy(state, token, amount):
    if not state.wallet:
        return None
    q = jupiter_quote(SOL_MINT, token["mint"], int(amount * 1e9))
    if not q:
        return None
    sig = jupiter_swap(q, state.wallet)
    if not sig:
        return None
    return {"mint": token["mint"], "symbol": token.get("symbol","UNKNOWN"), "entry_sol": amount, "out_amount": int(q.get("outAmount",0)), "sig": sig, "peak": 0.0, "score": token.get("score",0)}

def do_sell(state, pos):
    if not state.wallet:
        return None
    result = rpc_call("getTokenAccountsByOwner", [state.wallet_address, {"mint": pos["mint"]}, {"encoding":"jsonParsed"}])
    if not result:
        return None
    try:
        amount = int(result["value"][0]["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
    except:
        return None
    q = jupiter_quote(pos["mint"], SOL_MINT, amount, 1000)
    if not q:
        return None
    sig = jupiter_swap(q, state.wallet)
    if not sig:
        return None
    exit_sol = int(q.get("outAmount",0)) / 1e9
    return {"date": datetime.now().strftime("%Y-%m-%d"), "symbol": pos["symbol"], "entry_sol": pos["entry_sol"], "exit_sol": exit_sol, "profit": exit_sol - pos["entry_sol"]}

# ================================================================
# ENGINE
# ================================================================
def engine_loop(state, ai):
    while True:
        try:
            if not state.running:
                time.sleep(2)
                continue
            for pos in list(state.positions):
                q = jupiter_quote(pos["mint"], SOL_MINT, pos.get("out_amount",1), 1000)
                if q:
                    cur = int(q.get("outAmount",0)) / 1e9
                    pnl = ((cur - pos["entry_sol"]) / pos["entry_sol"]) * 100
                    if pnl >= state.config["take_profit_pct"] or pnl <= -state.config["trailing_stop_pct"]:
                        t = do_sell(state, pos)
                        if t:
                            state.positions.remove(pos)
                            state.trades.append(t)
                            state.log(f"Sold {pos['symbol']} {t['profit']:+.4f} SOL")
                            save_state(state)
            if len(state.positions) < state.config["max_positions"] and state.wallet:
                for token in discover_tokens():
                    if len(state.positions) >= state.config["max_positions"]:
                        break
                    score = 0
                    if token["liquidity"] >= state.config["min_liquidity_usd"]:
                        score += 50
                    if token["volume_24h"] >= state.config["min_volume_24h"]:
                        score += 30
                    ai_score = ai.get_ai_score(token)
                    if ai_score:
                        score = int((score + ai_score) / 2)
                    token["score"] = score
                    if score >= state.config["min_score"]:
                        p = do_buy(state, token, state.config["snipe_amount"])
                        if p:
                            state.positions.append(p)
                            state.log(f"Bought {token['symbol']} score={score}")
                            save_state(state)
                            time.sleep(1)
            time.sleep(15)
        except Exception as e:
            time.sleep(5)

@st.cache_resource
def start_engine(_s, _a):
    t = threading.Thread(target=engine_loop, args=(_s, _a), daemon=True)
    t.start()
    return t

# ================================================================
# INIT
# ================================================================
state = get_state()
ai = AIEngine(FREELLM_API_KEY)

if ENV_PRIVATE_KEY and not state.wallet:
    try:
        state.wallet = Keypair.from_base58_string(ENV_PRIVATE_KEY)
        state.wallet_address = str(state.wallet.pubkey())
    except:
        pass

start_engine(state, ai)

balance = get_balance(state.wallet_address) if state.wallet else 0
prices = get_live_prices()
sol_price = prices.get("solana", {}).get("usd", 140)
total_trades = len(state.trades)
wins = sum(1 for t in state.trades if t.get("profit", 0) > 0)
win_rate = (wins / total_trades * 100) if total_trades else 0
net_pnl = sum(t.get("profit", 0) for t in state.trades)

# ================================================================
# PAGE
# ================================================================
st.set_page_config(page_title="J.A.R.V.I.S.", page_icon="◉", layout="wide")

# Build market data for circles
market_coins = {
    "BTC": prices.get("bitcoin", {}).get("usd", 0),
    "ETH": prices.get("ethereum", {}).get("usd", 0),
    "SOL": prices.get("solana", {}).get("usd", 0),
    "BNB": prices.get("binancecoin", {}).get("usd", 0),
    "XRP": prices.get("ripple", {}).get("usd", 0),
    "ADA": prices.get("cardano", {}).get("usd", 0),
    "DOGE": prices.get("dogecoin", {}).get("usd", 0),
}

# Create the JARVIS HTML with rotating rings and market circles
jarvis_html = f"""
<!DOCTYPE html>
<html>
<head>
<style>
body {{
    margin: 0;
    background: #000000;
    overflow: hidden;
    font-family: 'Orbitron', sans-serif;
}}

.container {{
    position: relative;
    width: 100%;
    height: 600px;
    display: flex;
    align-items: center;
    justify-content: center;
}}

/* Central core */
.core {{
    position: absolute;
    width: 150px;
    height: 150px;
    border-radius: 50%;
    background: radial-gradient(circle, #00ff88 0%, #006633 40%, transparent 70%);
    box-shadow: 0 0 50px #00ff88, 0 0 100px #00ff88, inset 0 0 50px #00ff88;
    z-index: 10;
    animation: pulse 3s ease-in-out infinite;
}}

@keyframes pulse {{
    0%, 100% {{ transform: scale(1); opacity: 0.9; }}
    50% {{ transform: scale(1.1); opacity: 1; }}
}}

/* Rotating rings */
.ring {{
    position: absolute;
    border-radius: 50%;
    border: 1px solid rgba(0, 255, 136, 0.4);
    animation: rotate linear infinite;
}}

.ring-1 {{ width: 220px; height: 220px; animation-duration: 10s; border-color: rgba(0,255,136,.6); }}
.ring-2 {{ width: 300px; height: 300px; animation-duration: 15s; animation-direction: reverse; }}
.ring-3 {{ width: 380px; height: 380px; animation-duration: 20s; border-style: dashed; }}
.ring-4 {{ width: 460px; height: 460px; animation-duration: 25s; animation-direction: reverse; border-color: rgba(0,255,136,.2); }}
.ring-5 {{ width: 540px; height: 540px; animation-duration: 30s; border-style: dotted; }}

@keyframes rotate {{
    from {{ transform: rotate(0deg); }}
    to {{ transform: rotate(360deg); }}
}}

/* Market circles on rings */
.coin {{
    position: absolute;
    width: 45px;
    height: 45px;
    border-radius: 50%;
    background: #001a0d;
    border: 1px solid #00ff88;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    font-size: 8px;
    color: #00ff88;
    box-shadow: 0 0 15px rgba(0,255,136,.5);
    animation: counter-rotate linear infinite;
}}

.coin-symbol {{ font-weight: bold; }}
.coin-price {{ color: #fff; margin-top: 2px; font-size: 7px; }}

.counter-rotate-1 {{ animation-duration: 10s; }}
.counter-rotate-2 {{ animation-duration: 15s; }}
.counter-rotate-3 {{ animation-duration: 20s; }}
.counter-rotate-4 {{ animation-duration: 25s; }}
.counter-rotate-5 {{ animation-duration: 30s; }}

@keyframes counter-rotate {{
    from {{ transform: rotate(0deg) translateX(var(--radius)) rotate(0deg); }}
    to {{ transform: rotate(360deg) translateX(var(--radius)) rotate(-360deg); }}
}}

/* HUD Panels */
.hud-panel {{
    position: absolute;
    background: rgba(0, 26, 13, 0.9);
    border: 1px solid #00ff88;
    padding: 12px 18px;
    border-radius: 5px;
    box-shadow: 0 0 20px rgba(0,255,136,.3);
    z-index: 20;
}}

.panel-title {{
    color: #00ff88;
    font-size: 9px;
    letter-spacing: 2px;
    margin-bottom: 5px;
}}

.panel-value {{
    color: #fff;
    font-size: 18px;
    font-weight: bold;
}}

.panel-sub {{
    color: #558866;
    font-size: 8px;
    margin-top: 3px;
}}

.panel-balance {{ top: 8%; left: 8%; }}
.panel-positions {{ bottom: 8%; left: 8%; }}
.panel-performance {{ top: 8%; right: 8%; }}
.panel-pnl {{ bottom: 8%; right: 8%; }}

/* Status */
.status {{
    position: absolute;
    bottom: 15px;
    left: 50%;
    transform: translateX(-50%);
    color: #00ff88;
    font-size: 12px;
    letter-spacing: 4px;
    z-index: 20;
}}

/* Title */
.title {{
    position: absolute;
    top: 20px;
    left: 50%;
    transform: translateX(-50%);
    color: #00ff88;
    font-size: 20px;
    letter-spacing: 8px;
    z-index: 20;
    text-shadow: 0 0 20px #00ff88;
}}
</style>
</head>
<body>

<div class="container">
    <div class="title">J.A.R.V.I.S.</div>
    
    <!-- Rotating rings -->
    <div class="ring ring-1"></div>
    <div class="ring ring-2"></div>
    <div class="ring ring-3"></div>
    <div class="ring ring-4"></div>
    <div class="ring ring-5"></div>
    
    <!-- Market coins positioned on rings -->
    <div class="coin counter-rotate-1" style="--radius: 110px; top: calc(50% - 22px); left: calc(50% - 22px);">
        <span class="coin-symbol">BTC</span>
        <span class="coin-price">${market_coins['BTC']:,.0f}</span>
    </div>
    
    <div class="coin counter-rotate-2" style="--radius: 150px; top: calc(50% - 22px); left: calc(50% - 22px);">
        <span class="coin-symbol">ETH</span>
        <span class="coin-price">${market_coins['ETH']:,.0f}</span>
    </div>
    
    <div class="coin counter-rotate-3" style="--radius: 190px; top: calc(50% - 22px); left: calc(50% - 22px);">
        <span class="coin-symbol">SOL</span>
        <span class="coin-price">${market_coins['SOL']:,.0f}</span>
    </div>
    
    <div class="coin counter-rotate-4" style="--radius: 230px; top: calc(50% - 22px); left: calc(50% - 22px);">
        <span class="coin-symbol">BNB</span>
        <span class="coin-price">${market_coins['BNB']:,.0f}</span>
    </div>
    
    <div class="coin counter-rotate-5" style="--radius: 270px; top: calc(50% - 22px); left: calc(50% - 22px);">
        <span class="coin-symbol">XRP</span>
        <span class="coin-price">${market_coins['XRP']:,.2f}</span>
    </div>
    
    <!-- Central core -->
    <div class="core"></div>
    
    <!-- HUD Panels -->
    <div class="hud-panel panel-balance">
        <div class="panel-title">BALANCE</div>
        <div class="panel-value">{balance:.4f} SOL</div>
        <div class="panel-sub">${balance * sol_price:,.2f} USD</div>
    </div>
    
    <div class="hud-panel panel-positions">
        <div class="panel-title">POSITIONS</div>
        <div class="panel-value">{len(state.positions)} / {state.config['max_positions']}</div>
        <div class="panel-sub">ACTIVE TARGETS</div>
    </div>
    
    <div class="hud-panel panel-performance">
        <div class="panel-title">WIN RATE</div>
        <div class="panel-value">{win_rate:.1f}%</div>
        <div class="panel-sub">{wins} WINS / {total_trades} TRADES</div>
    </div>
    
    <div class="hud-panel panel-pnl">
        <div class="panel-title">NET P/L</div>
        <div class="panel-value" style="color:{'#00ff88' if net_pnl >= 0 else '#ff4444'}">{net_pnl:+.4f} SOL</div>
        <div class="panel-sub">${net_pnl * sol_price:+,.2f} USD</div>
    </div>
    
    <div class="status">SYSTEM {'ONLINE' if state.running else 'STANDBY'}</div>
</div>

</body>
</html>
"""

# Render JARVIS HUD
components.html(jarvis_html, height=620)

# ================================================================
# CHAT
# ================================================================
st.markdown("---")

for msg in state.chat[-4:]:
    if msg["role"] == "user":
        st.markdown(f"**YOU:** {msg['content']}")
    else:
        st.markdown(f"**JARVIS:** {msg['content']}")

prompt = st.chat_input("Command JARVIS...")

if prompt:
    state.chat.append({"role": "user", "content": prompt})
    with st.spinner("Processing..."):
        response = ai.jarvis_speak(prompt)
        state.chat.append({"role": "assistant", "content": response or "Core unavailable."})
    st.rerun()

# ================================================================
# CONTROLS
# ================================================================
c1, c2, c3 = st.columns(3)

with c1:
    if not state.running:
        if st.button("ACTIVATE", use_container_width=True):
            if state.wallet:
                state.running = True
                st.rerun()
            else:
                st.error("No wallet")
    else:
        if st.button("DEACTIVATE", use_container_width=True):
            state.running = False
            st.rerun()

with c2:
    show = st.toggle("SETTINGS", value=False)

with c3:
    if st.button("REFRESH", use_container_width=True):
        st.rerun()

if show:
    s1, s2, s3 = st.columns(3)
    with s1:
        snipe = st.number_input("BUY SOL", 0.01, 0.50, float(state.config["snipe_amount"]), 0.01)
        min_liq = st.number_input("MIN LIQ $", 1000.0, 200000.0, float(state.config["min_liquidity_usd"]), 1000.0)
    with s2:
        tp = st.number_input("TP %", 5.0, 300.0, float(state.config["take_profit_pct"]), 5.0)
        trail = st.number_input("TRAIL %", 1.0, 50.0, float(state.config["trailing_stop_pct"]), 1.0)
    with s3:
        min_score = st.number_input("MIN SCORE", 0, 100, int(state.config["min_score"]), 5)
        max_pos = st.number_input("MAX POS", 1, 10, int(state.config["max_positions"]), 1)
    
    with state.lock:
        state.config.update({
            "snipe_amount": float(snipe),
            "take_profit_pct": float(tp),
            "trailing_stop_pct": float(trail),
            "min_liquidity_usd": float(min_liq),
            "min_score": int(min_score),
            "max_positions": int(max_pos),
        })

if HAS_AUTOREFRESH:
    st_autorefresh(interval=6000, key="refresh")

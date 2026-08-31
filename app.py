# app.py - J.A.R.V.I.S. // Fixed Green HUD + Live Market
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
APP_VERSION = "14.0 GREEN HUD"

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
REQUEST_TIMEOUT = 90
SCAN_INTERVAL_SECONDS = 15

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
            payload = {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens, "temperature": 0.3}
            response = requests.post(f"{FREELLM_BASE}/chat/completions", headers=headers, json=payload, timeout=20, verify=False)
            if response.status_code == 200:
                return response.json().get("choices", [{}])[0].get("message", {}).get("content", "")
            return None
        except:
            return None
    
    def jarvis_speak(self, prompt):
        system = "You are J.A.R.V.I.S., an AI assistant. Be calm, professional, and concise."
        return self.query("groq/gpt-oss-120b", f"{system}\n\nUser: {prompt}\nJARVIS:")
    
    def get_ai_score(self, token_data):
        prompt = f"Score 0-100 for trading: {token_data.get('symbol')} liq=${token_data.get('liquidity',0):,.0f}. Return only number."
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
        "min_score": 45,
        "use_ai": True,
    })
    
    def log(self, message):
        ts = datetime.now().strftime("%H:%M:%S")
        with self.lock:
            self.logs.insert(0, f"[{ts}] {message}")
            self.logs = self.logs[:50]

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
# LIVE MARKET DATA
# ================================================================
def get_live_crypto_prices():
    """Get live crypto prices from CoinGecko"""
    try:
        response = requests.get(
            f"{COINGECKO_API}/simple/price",
            params={
                "ids": "bitcoin,ethereum,solana,binancecoin,ripple,cardano,dogecoin,avalanche-2,polkadot,chainlink",
                "vs_currencies": "usd",
                "include_24hr_change": "true"
            },
            timeout=10,
            verify=False
        )
        if response.status_code == 200:
            return response.json()
    except:
        pass
    return {}

def get_solana_price():
    prices = get_live_crypto_prices()
    return prices.get("solana", {}).get("usd", 140.0)

# ================================================================
# TRADING FUNCTIONS
# ================================================================
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "JARVIS/14.0"})

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
    try:
        r = HTTP.get(f"{DEXSCREENER_API}/token-boosts/latest/v1", timeout=15, verify=False)
        if r.status_code == 200:
            data = r.json()
            mints = [x.get("tokenAddress") for x in data if x.get("chainId") == "solana"]
            for start in range(0, min(len(mints), 30), 30):
                r2 = HTTP.get(f"{DEXSCREENER_API}/tokens/v1/solana/{','.join(mints[start:start+30])}", timeout=15, verify=False)
                if r2.status_code == 200:
                    for pair in r2.json():
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
    except:
        pass
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
    return {"date": datetime.now().strftime("%Y-%m-%d"), "time": datetime.now().strftime("%H:%M:%S"), "symbol": position["symbol"], "entry_sol": position["entry_sol"], "exit_sol": exit_sol, "profit": profit}

# ================================================================
# ENGINE
# ================================================================
def engine_loop(state, ai):
    state.log("JARVIS engine online")
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
# GREEN HUD CSS
# ================================================================
st.set_page_config(page_title="J.A.R.V.I.S.", page_icon="◉", layout="wide", initial_sidebar_state="collapsed")

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@300;400;500;700&family=Share+Tech+Mono&display=swap');

:root {
    --green: #00ff88;
    --green-dim: #00cc66;
    --green-dark: #006633;
    --black: #000000;
    --panel: #001a0d;
    --text: #e0ffe0;
    --muted: #558866;
}

.stApp {
    background: #000000 !important;
    color: var(--text);
    font-family: 'Orbitron', sans-serif;
}

.main .block-container {
    max-width: 1400px;
    padding: 20px 30px 80px 30px;
}

section[data-testid="stSidebar"] { display: none; }
header[data-testid="stHeader"] { background: #000000; }
#MainMenu { visibility: hidden; }
footer { visibility: hidden; }

/* TOP BAR */
.hud-top {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 15px 0;
    border-bottom: 1px solid var(--green-dark);
    margin-bottom: 25px;
}

.hud-brand {
    color: var(--green);
    font-size: 14px;
    letter-spacing: 5px;
}

.hud-status {
    color: var(--green);
    font-size: 11px;
    letter-spacing: 3px;
}

.hud-wallet {
    color: var(--muted);
    font-size: 10px;
}

/* LIVE MARKET TICKER */
.market-ticker {
    display: flex;
    gap: 10px;
    padding: 15px 0;
    border-bottom: 1px solid var(--green-dark);
    margin-bottom: 25px;
    overflow-x: auto;
}

.ticker-item {
    background: var(--panel);
    border: 1px solid var(--green-dark);
    padding: 10px 15px;
    border-radius: 3px;
    min-width: 120px;
    text-align: center;
}

.ticker-symbol {
    color: var(--green);
    font-size: 10px;
    letter-spacing: 2px;
}

.ticker-price {
    color: var(--text);
    font-size: 14px;
    margin-top: 5px;
}

.ticker-change {
    font-size: 10px;
    margin-top: 3px;
}

/* MAIN CORE */
.jarvis-stage {
    position: relative;
    min-height: 500px;
    display: flex;
    align-items: center;
    justify-content: center;
    margin: 20px 0;
}

.jarvis-core {
    width: 300px;
    height: 300px;
    border-radius: 50%;
    border: 2px solid var(--green);
    display: flex;
    align-items: center;
    justify-content: center;
    box-shadow: 0 0 40px rgba(0, 255, 136, 0.3), inset 0 0 40px rgba(0, 255, 136, 0.1);
    animation: pulse 3s ease-in-out infinite;
}

@keyframes pulse {
    0%, 100% { box-shadow: 0 0 20px rgba(0,255,136,.3), inset 0 0 20px rgba(0,255,136,.1); }
    50% { box-shadow: 0 0 60px rgba(0,255,136,.6), inset 0 0 60px rgba(0,255,136,.2); }
}

.core-eye {
    width: 60px;
    height: 60px;
    border-radius: 50%;
    background: radial-gradient(circle, #fff 0%, var(--green) 30%, var(--green-dark) 60%, transparent 70%);
    box-shadow: 0 0 30px var(--green);
}

.core-text {
    text-align: center;
    margin-top: 20px;
}

.core-name {
    color: var(--green);
    font-size: 24px;
    letter-spacing: 8px;
}

.core-state {
    color: var(--muted);
    font-size: 10px;
    letter-spacing: 3px;
    margin-top: 8px;
}

/* HUD PANELS */
.hud-panel {
    position: absolute;
    background: var(--panel);
    border: 1px solid var(--green-dark);
    padding: 15px 20px;
    width: 200px;
}

.panel-title {
    color: var(--green);
    font-size: 9px;
    letter-spacing: 2px;
    margin-bottom: 8px;
}

.panel-value {
    color: var(--text);
    font-size: 18px;
}

.panel-sub {
    color: var(--muted);
    font-size: 8px;
    margin-top: 4px;
}

.panel-left-top { left: 5%; top: 10%; }
.panel-left-bottom { left: 5%; bottom: 10%; }
.panel-right-top { right: 5%; top: 10%; }
.panel-right-bottom { right: 5%; bottom: 10%; }

/* CHAT */
.command-title {
    text-align: center;
    color: var(--green);
    font-size: 10px;
    letter-spacing: 4px;
    margin: 20px 0;
}

div[data-testid="stChatInput"] textarea {
    background: #000000 !important;
    border: 1px solid var(--green) !important;
    border-radius: 3px !important;
    color: var(--green) !important;
    padding: 15px !important;
}

.jarvis-message {
    background: var(--panel);
    border-left: 2px solid var(--green);
    padding: 12px 18px;
    margin: 8px 0;
    color: var(--text);
    font-size: 12px;
}

.jarvis-message-user {
    border-left-color: var(--muted);
    color: var(--muted);
}

/* BUTTONS */
.stButton > button {
    background: #000000 !important;
    color: var(--green) !important;
    border: 1px solid var(--green) !important;
    border-radius: 2px !important;
    font-family: 'Orbitron', sans-serif !important;
    font-size: 9px !important;
    letter-spacing: 2px !important;
}

.stButton > button:hover {
    background: var(--green) !important;
    color: #000000 !important;
    box-shadow: 0 0 20px rgba(0,255,136,.5);
}

/* LOG */
.log-line {
    color: var(--muted);
    font-family: 'Share Tech Mono', monospace;
    font-size: 10px;
    line-height: 1.6;
}

/* INPUTS */
.stNumberInput input, .stTextInput input {
    background: #000000 !important;
    border: 1px solid var(--green-dark) !important;
    color: var(--green) !important;
}

.stToggle {
    color: var(--green);
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
    except:
        pass

start_engine(state, ai)

balance = get_balance(state.wallet_address) if state.wallet else 0
total_trades = len(state.trades)
wins = sum(1 for t in state.trades if t.get("profit", 0) > 0)
win_rate = (wins / total_trades * 100) if total_trades else 0
net_pnl = sum(t.get("profit", 0) for t in state.trades)
solana_price = get_solana_price()
crypto_prices = get_live_crypto_prices()

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
# LIVE MARKET TICKER
# ================================================================
if crypto_prices:
    crypto_map = {
        "bitcoin": "BTC", "ethereum": "ETH", "solana": "SOL",
        "binancecoin": "BNB", "ripple": "XRP", "cardano": "ADA",
        "dogecoin": "DOGE", "avalanche-2": "AVAX", "polkadot": "DOT", "chainlink": "LINK"
    }
    
    ticker_html = '<div class="market-ticker">'
    for crypto_id, symbol in crypto_map.items():
        data = crypto_prices.get(crypto_id, {})
        price = data.get("usd", 0)
        change = data.get("usd_24h_change", 0)
        color = "#00ff88" if change >= 0 else "#ff4444"
        ticker_html += f'''
        <div class="ticker-item">
            <div class="ticker-symbol">{symbol}</div>
            <div class="ticker-price">${price:,.2f}</div>
            <div class="ticker-change" style="color:{color}">{change:+.2f}%</div>
        </div>
        '''
    ticker_html += '</div>'
    st.markdown(ticker_html, unsafe_allow_html=True)

# ================================================================
# MAIN JARVIS STAGE
# ================================================================
pnl_color = "#00ff88" if net_pnl >= 0 else "#ff4444"

st.markdown(f"""
<div class="jarvis-stage">
    <div class="hud-panel panel-left-top">
        <div class="panel-title">CORE BALANCE</div>
        <div class="panel-value">{balance:.4f} SOL</div>
        <div class="panel-sub">${balance * solana_price:,.2f} USD</div>
    </div>

    <div class="hud-panel panel-left-bottom">
        <div class="panel-title">ACTIVE TARGETS</div>
        <div class="panel-value">{len(state.positions)} / {state.config["max_positions"]}</div>
        <div class="panel-sub">POSITIONS</div>
    </div>

    <div class="hud-panel panel-right-top">
        <div class="panel-title">PERFORMANCE</div>
        <div class="panel-value">{win_rate:.1f}%</div>
        <div class="panel-sub">{wins} WINS / {total_trades} TRADES</div>
    </div>

    <div class="hud-panel panel-right-bottom">
        <div class="panel-title">NET P/L</div>
        <div class="panel-value" style="color:{pnl_color}">{net_pnl:+.4f} SOL</div>
        <div class="panel-sub">${net_pnl * solana_price:+,.2f} USD</div>
    </div>

    <div class="jarvis-core">
        <div class="core-eye"></div>
    </div>
</div>
<div class="core-text">
    <div class="core-name">JARVIS</div>
    <div class="core-state">{engine_state}</div>
</div>
""", unsafe_allow_html=True)

# ================================================================
# CHAT
# ================================================================
st.markdown('<div class="command-title">SPEAK TO J.A.R.V.I.S.</div>', unsafe_allow_html=True)

for msg in state.chat[-4:]:
    if msg["role"] == "user":
        st.markdown(f'<div class="jarvis-message jarvis-message-user"><b>YOU</b><br>{msg["content"]}</div>', unsafe_allow_html=True)
    else:
        st.markdown(f'<div class="jarvis-message"><b style="color:#00ff88">J.A.R.V.I.S.</b><br>{msg["content"]}</div>', unsafe_allow_html=True)

prompt = st.chat_input("Command J.A.R.V.I.S. ...")

if prompt:
    state.chat.append({"role": "user", "content": prompt})
    with st.spinner("PROCESSING..."):
        response = ai.jarvis_speak(prompt)
        state.chat.append({"role": "assistant", "content": response or "Core unavailable."})
    st.rerun()

# ================================================================
# CONTROLS
# ================================================================
st.markdown("---")

c1, c2, c3 = st.columns(3)

with c1:
    if not state.running:
        if st.button("ACTIVATE", use_container_width=True):
            if state.wallet:
                state.running = True
                state.log("Activated")
                st.rerun()
            else:
                st.error("No wallet")
    else:
        if st.button("DEACTIVATE", use_container_width=True):
            state.running = False
            st.rerun()

with c2:
    show_settings = st.toggle("SETTINGS", value=False)

with c3:
    if st.button("REFRESH", use_container_width=True):
        st.rerun()

# ================================================================
# SETTINGS
# ================================================================
if show_settings:
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

# ================================================================
# LOG
# ================================================================
with st.expander("TELEMETRY"):
    for log in state.logs[:20]:
        st.markdown(f'<div class="log-line">{log}</div>', unsafe_allow_html=True)

if HAS_AUTOREFRESH:
    st_autorefresh(interval=6000, key="refresh")

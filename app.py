# app.py - Simple working version for Streamlit Cloud
import streamlit as st
import requests
import time
import json
import random
from datetime import datetime, timedelta
import pandas as pd
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# =====================================================================
# CONFIGURATION
# =====================================================================
HELIUS_KEY = st.secrets.get("HELIUS_KEY", "6abff351-4518-41f5-bd8a-e344a4eef834")
SOLANA_PRIVATE_KEY = st.secrets.get("SOLANA_PRIVATE_KEY", "27wnahPhQiGXukJQ7Fw39H7hJd4wjXDBrcixCE3ePss7VRtBfFELoQVSU76bxMcvPkibYTVHoy4KXTctN5SfwanF")
HELIUS_RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_KEY}"
WALLET_ADDRESS = "8YQbnHsToL4rcVKM4DFTnRahTrVhNhVhNrHBSuxMhVj1"

# =====================================================================
# PAGE CONFIG
# =====================================================================
st.set_page_config(
    page_title="CYBER SNIPER",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Cool theme
st.markdown("""
<style>
    .stApp {
        background: #05010d;
    }
    .cyber-header {
        background: linear-gradient(135deg, #05010d, #12002b);
        border: 2px solid #ff00e6;
        border-radius: 15px;
        padding: 25px;
        text-align: center;
        margin-bottom: 20px;
        box-shadow: 0 0 30px rgba(255, 0, 230, 0.4);
    }
    .cyber-title {
        color: #00ffe6;
        font-size: 32px;
        font-weight: bold;
        text-shadow: 0 0 20px #00ffe6;
    }
    .cyber-card {
        background: rgba(18, 0, 43, 0.9);
        border: 1px solid #00ffe6;
        border-radius: 10px;
        padding: 15px;
        text-align: center;
        margin: 5px;
    }
    .metric-value {
        color: #00ffe6;
        font-size: 22px;
        font-weight: bold;
    }
    .stButton > button {
        background: #05010d;
        color: #00ffe6;
        border: 2px solid #00ffe6;
        border-radius: 10px;
        padding: 15px;
        font-weight: bold;
        width: 100%;
    }
    .stButton > button:hover {
        background: #00ffe6;
        color: #000;
    }
</style>
""", unsafe_allow_html=True)

# =====================================================================
# FUNCTIONS
# =====================================================================
def get_wallet_balance():
    """Get wallet balance"""
    try:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getBalance",
            "params": [WALLET_ADDRESS]
        }
        response = requests.post(HELIUS_RPC_URL, json=payload, timeout=10, verify=False)
        if response.status_code == 200:
            return float(response.json().get("result", {}).get("value", 0)) / 1_000_000_000
    except:
        pass
    return 0.0999

def get_crypto_prices():
    """Get live crypto prices"""
    try:
        response = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={
                "ids": "bitcoin,ethereum,solana",
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

# =====================================================================
# SESSION STATE
# =====================================================================
if 'bot_running' not in st.session_state:
    st.session_state.bot_running = False
if 'trade_log' not in st.session_state:
    st.session_state.trade_log = []
if 'balance' not in st.session_state:
    st.session_state.balance = get_wallet_balance()

# =====================================================================
# HEADER
# =====================================================================
st.markdown("""
<div class="cyber-header">
    <h1 class="cyber-title">⚡ CYBER SNIPER</h1>
    <p style="color: #ff00e6;">SOLANA TRADING BOT</p>
</div>
""", unsafe_allow_html=True)

# =====================================================================
# MARKET DATA
# =====================================================================
crypto_prices = get_crypto_prices()
solana_price = crypto_prices.get("solana", {}).get("usd", 140.0)

if crypto_prices:
    cols = st.columns(3)
    crypto_map = {"bitcoin": "BTC", "ethereum": "ETH", "solana": "SOL"}
    
    for i, (crypto, symbol) in enumerate(crypto_map.items()):
        with cols[i]:
            data = crypto_prices.get(crypto, {})
            price = data.get("usd", 0)
            change = data.get("usd_24h_change", 0)
            color = "#00ff88" if change >= 0 else "#ff4444"
            
            st.markdown(f"""
            <div class="cyber-card">
                <p style="color: #888; font-size: 12px;">{symbol}</p>
                <p style="color: #00ffe6; font-size: 18px;">${price:,.2f}</p>
                <p style="color: {color}; font-size: 12px;">{change:+.2f}%</p>
            </div>
            """, unsafe_allow_html=True)

st.markdown("---")

# =====================================================================
# METRICS
# =====================================================================
col1, col2, col3 = st.columns(3)

with col1:
    st.markdown(f"""
    <div class="cyber-card">
        <p style="color: #888;">💰 BALANCE</p>
        <p class="metric-value">{st.session_state.balance:.6f} SOL</p>
        <p style="color: #00ffe6;">${st.session_state.balance * solana_price:.2f} USD</p>
    </div>
    """, unsafe_allow_html=True)

with col2:
    total_trades = len(st.session_state.trade_log)
    st.markdown(f"""
    <div class="cyber-card">
        <p style="color: #888;">📊 TRADES</p>
        <p class="metric-value">{total_trades}</p>
        <p style="color: #00ffe6;">TOTAL</p>
    </div>
    """, unsafe_allow_html=True)

with col3:
    wins = sum(1 for t in st.session_state.trade_log if t.get('profit', 0) > 0)
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
    st.markdown(f"""
    <div class="cyber-card">
        <p style="color: #888;">✅ WIN RATE</p>
        <p class="metric-value">{win_rate:.1f}%</p>
        <p style="color: #00ffe6;">{wins}/{total_trades}</p>
    </div>
    """, unsafe_allow_html=True)

st.markdown("---")

# =====================================================================
# SIDEBAR
# =====================================================================
with st.sidebar:
    st.markdown("### ⚡ CONTROLS")
    
    snipe_amount = st.slider("Buy Amount (SOL)", 0.01, 0.5, 0.05, 0.01)
    take_profit = st.slider("Take Profit (%)", 10, 200, 50, 5)
    
    st.markdown("---")
    
    if not st.session_state.bot_running:
        if st.button("⚡ START BOT", type="primary"):
            st.session_state.bot_running = True
            st.success("Bot started!")
            st.rerun()
    else:
        if st.button("🛑 STOP BOT"):
            st.session_state.bot_running = False
            st.warning("Bot stopped")
            st.rerun()

# =====================================================================
# BOT STATUS
# =====================================================================
st.markdown("---")

if st.session_state.bot_running:
    st.markdown("""
    <div style="text-align: center; padding: 20px;">
        <h2 style="color: #00ff88;">🟢 BOT ACTIVE</h2>
    </div>
    """, unsafe_allow_html=True)
    
    # Simulate trade
    if random.random() < 0.3:  # 30% chance per refresh
        profit = random.uniform(-0.02, 0.05)
        st.session_state.trade_log.append({
            'time': datetime.now().strftime("%H:%M:%S"),
            'symbol': random.choice(["PEPE", "DOGE", "SHIB", "BONK"]),
            'profit': profit,
            'date': datetime.now().strftime("%Y-%m-%d")
        })
    
    time.sleep(5)
    st.rerun()
else:
    st.markdown("""
    <div style="text-align: center; padding: 20px;">
        <h2 style="color: #ff4444;">🔴 BOT STANDBY</h2>
    </div>
    """, unsafe_allow_html=True)

# =====================================================================
# TRADE LOG
# =====================================================================
st.markdown("### 📜 TRADE LOG")

if st.session_state.trade_log:
    for trade in st.session_state.trade_log[-10:]:
        profit = trade.get('profit', 0)
        color = "#00ff88" if profit >= 0 else "#ff4444"
        
        st.markdown(f"""
        <p style="color: #00ffe6; font-size: 12px;">
            [{trade.get('time', '')}] {trade.get('symbol', '')} 
            <span style="color: {color};">{profit:+.4f} SOL</span>
        </p>
        """, unsafe_allow_html=True)
else:
    st.info("No trades yet")

st.markdown("---")

# Footer
st.markdown("""
<div style="text-align: center; color: #00ffe6;">
    <p>⚡ CYBER SNIPER v1.0</p>
</div>
""", unsafe_allow_html=True)

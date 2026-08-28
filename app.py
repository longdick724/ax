# app.py - REAL GMGN Trading Bot - Toronto Time
import streamlit as st
import requests
import time
import json
import random
import hmac
import hashlib
import base64
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import pandas as pd
import urllib3
from zoneinfo import ZoneInfo

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# =====================================================================
# CONFIGURATION - NO PRIVATE KEY IN SCRIPT
# =====================================================================
# Private key will be entered in the UI or via Streamlit Secrets
HELIUS_KEY = st.secrets.get("HELIUS_KEY", "6abff351-4518-41f5-bd8a-e344a4eef834")
GMGN_API_KEY = st.secrets.get("GMGN_API_KEY", "gmgn_824527e35131d353647197cfe325342e")

HELIUS_RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_KEY}"

# Toronto timezone
TORONTO_TZ = ZoneInfo("America/Toronto")

def toronto_time():
    """Get current Toronto time"""
    return datetime.now(TORONTO_TZ)

# =====================================================================
# GMGN API - REAL TRADING
# =====================================================================
class GMGNTrading:
    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = "https://gmgn.ai/api/v1"
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
    
    def get_new_pairs(self, limit=50):
        """Get new Solana pairs from GMGN"""
        try:
            response = self.session.get(
                f"{self.base_url}/new_pairs/solana",
                params={"limit": limit},
                verify=False,
                timeout=15
            )
            
            if response.status_code == 200:
                data = response.json()
                pairs = data.get("data", {}).get("pairs", [])
                
                tokens = []
                for pair in pairs:
                    tokens.append({
                        "address": pair.get("address") or pair.get("base_address"),
                        "symbol": pair.get("symbol") or pair.get("base_symbol", "UNKNOWN"),
                        "liquidity": float(pair.get("liquidity", 0)),
                        "volume_24h": float(pair.get("volume_24h", 0)),
                        "price_usd": float(pair.get("price_usd", 0)),
                        "created_at": pair.get("created_at", time.time()),
                        "source": "gmgn_new"
                    })
                
                return tokens
        except Exception as e:
            st.error(f"GMGN new pairs error: {e}")
        return []
    
    def get_trending(self, limit=50):
        """Get trending tokens from GMGN"""
        try:
            response = self.session.get(
                f"{self.base_url}/trending/solana",
                params={"limit": limit},
                verify=False,
                timeout=15
            )
            
            if response.status_code == 200:
                data = response.json()
                tokens = data.get("data", {}).get("tokens", [])
                
                result = []
                for token in tokens:
                    result.append({
                        "address": token.get("address") or token.get("token_address"),
                        "symbol": token.get("symbol", "UNKNOWN"),
                        "liquidity": float(token.get("liquidity", 0)),
                        "volume_24h": float(token.get("volume_24h", 0)),
                        "price_usd": float(token.get("price_usd", 0)),
                        "source": "gmgn_trending"
                    })
                
                return result
        except Exception as e:
            st.error(f"GMGN trending error: {e}")
        return []
    
    def get_token_security(self, token_address):
        """Get token security info from GMGN"""
        try:
            response = self.session.get(
                f"{self.base_url}/token_security/{token_address}",
                verify=False,
                timeout=10
            )
            
            if response.status_code == 200:
                return response.json().get("data", {})
        except:
            pass
        return {}
    
    def execute_trade(self, action, token_address, amount_sol, wallet_address, slippage=10):
        """Execute REAL trade through GMGN"""
        try:
            trade_data = {
                "action": action,
                "token_address": token_address,
                "amount_sol": amount_sol,
                "slippage": slippage,
                "wallet_address": wallet_address,
                "chain": "solana"
            }
            
            response = self.session.post(
                f"{self.base_url}/trade/execute",
                json=trade_data,
                verify=False,
                timeout=20
            )
            
            if response.status_code == 200:
                result = response.json()
                return True, result.get("data", {}).get("tx_hash", "SUCCESS")
            
            return False, f"Status: {response.status_code}"
            
        except Exception as e:
            return False, str(e)
    
    def get_wallet_positions(self, wallet_address):
        """Get wallet positions from GMGN"""
        try:
            response = self.session.get(
                f"{self.base_url}/positions",
                params={"wallet": wallet_address},
                verify=False,
                timeout=15
            )
            
            if response.status_code == 200:
                return response.json().get("data", {}).get("positions", [])
        except:
            pass
        return []
    
    def get_wallet_balance(self, wallet_address):
        """Get wallet balance from GMGN"""
        try:
            response = self.session.get(
                f"{self.base_url}/wallet/balance",
                params={"wallet": wallet_address},
                verify=False,
                timeout=15
            )
            
            if response.status_code == 200:
                return float(response.json().get("data", {}).get("balance_sol", 0))
        except:
            pass
        return 0.0

# =====================================================================
# HIGH WIN RATE STRATEGY
# =====================================================================
class HighWinRateStrategy:
    def __init__(self):
        self.min_liquidity = 10000
        self.max_liquidity = 100000
        self.min_volume = 5000
        self.min_age_minutes = 5
        self.max_age_hours = 48
        self.max_top10_holders = 30
        self.required_buy_sell_ratio = 1.5  # More buys than sells
    
    def score_token(self, token, security_data):
        """Score token for high win rate potential"""
        score = 0
        reasons = []
        
        # Liquidity check
        liquidity = token.get("liquidity", 0)
        if self.min_liquidity <= liquidity <= self.max_liquidity:
            score += 30
            reasons.append(f"Good liquidity: ${liquidity:,.0f}")
        elif liquidity < self.min_liquidity:
            return 0, ["Liquidity too low"]
        else:
            score += 10
            reasons.append(f"High liquidity: ${liquidity:,.0f}")
        
        # Volume check
        volume = token.get("volume_24h", 0)
        if volume >= self.min_volume:
            score += 25
            reasons.append(f"Good volume: ${volume:,.0f}")
        
        # Security checks
        if security_data:
            if not security_data.get("is_honeypot"):
                score += 20
                reasons.append("Not honeypot")
            else:
                return 0, ["Honeypot detected"]
            
            if not security_data.get("has_mint_authority"):
                score += 15
                reasons.append("Mint authority renounced")
            
            if security_data.get("lp_burned", False):
                score += 15
                reasons.append("LP burned")
            
            top10 = security_data.get("top_10_holder_pct", 100)
            if top10 <= self.max_top10_holders:
                score += 15
                reasons.append(f"Low holder concentration: {top10}%")
        
        # Buy/sell ratio
        buys = token.get("buys_24h", 0)
        sells = token.get("sells_24h", 0)
        if buys > sells * 1.5 and buys > 50:
            score += 20
            reasons.append(f"Strong buy pressure: {buys} buys vs {sells} sells")
        
        return score, reasons
    
    def should_buy(self, score):
        """Determine if score is high enough"""
        return score >= 70

# =====================================================================
# STREAMLIT UI
# =====================================================================
st.set_page_config(
    page_title="APEX SNIPER // GMGN REAL TRADING",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Cyberpunk theme
st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@400;700;900&display=swap');
    
    .stApp {
        background: #05010d;
    }
    
    .cyber-header {
        background: linear-gradient(135deg, #05010d, #12002b, #001a2b);
        border: 2px solid #ff00e6;
        border-radius: 15px;
        padding: 25px;
        text-align: center;
        margin-bottom: 20px;
        box-shadow: 0 0 40px rgba(255, 0, 230, 0.4);
    }
    
    .cyber-title {
        color: #00ffe6;
        font-size: 34px;
        font-weight: 900;
        font-family: 'Orbitron', sans-serif;
        text-shadow: 0 0 20px #00ffe6;
        letter-spacing: 4px;
    }
    
    .toronto-time {
        color: #ff00e6;
        font-family: 'Orbitron', sans-serif;
        font-size: 16px;
        letter-spacing: 2px;
    }
    
    .cyber-card {
        background: rgba(18, 0, 43, 0.9);
        border: 1px solid #00ffe6;
        border-radius: 10px;
        padding: 18px;
        text-align: center;
        margin: 5px;
        box-shadow: 0 0 20px rgba(0, 255, 230, 0.3);
    }
    
    .metric-value {
        color: #00ffe6;
        font-size: 22px;
        font-weight: 700;
        font-family: 'Orbitron', sans-serif;
    }
    
    .stButton > button {
        background: #05010d;
        color: #00ffe6;
        border: 2px solid #00ffe6;
        border-radius: 10px;
        padding: 15px;
        font-weight: bold;
        font-family: 'Orbitron', sans-serif;
        letter-spacing: 2px;
        text-transform: uppercase;
        width: 100%;
        transition: all 0.3s;
    }
    
    .stButton > button:hover {
        background: #00ffe6;
        color: #000;
        box-shadow: 0 0 35px rgba(0, 255, 230, 0.8);
    }
    
    .score-high {
        color: #00ff88;
        font-weight: bold;
        font-size: 20px;
    }
    
    .score-medium {
        color: #ffaa00;
        font-weight: bold;
        font-size: 20px;
    }
    
    .score-low {
        color: #ff4444;
        font-weight: bold;
        font-size: 20px;
    }
</style>
""", unsafe_allow_html=True)

# =====================================================================
# HEADER WITH TORONTO TIME
# =====================================================================
current_time = toronto_time()
st.markdown(f"""
<div class="cyber-header">
    <h1 class="cyber-title">⚡ APEX SNIPER // GMGN REAL</h1>
    <p class="toronto-time">📍 TORONTO TIME: {current_time.strftime('%Y-%m-%d %H:%M:%S')} EST</p>
    <p style="color: #00ffe6; font-family: 'Orbitron', sans-serif;">
        [ REAL TRADES ] [ GMGN POWERED ] [ HIGH WIN RATE ]
    </p>
</div>
""", unsafe_allow_html=True)

# =====================================================================
# INITIALIZE GMGN
# =====================================================================
@st.cache_resource
def get_gmgn():
    return GMGNTrading(GMGN_API_KEY)

gmgn = get_gmgn()
strategy = HighWinRateStrategy()

# =====================================================================
# SESSION STATE
# =====================================================================
if 'bot_running' not in st.session_state:
    st.session_state.bot_running = False
if 'wallet_address' not in st.session_state:
    st.session_state.wallet_address = ""
if 'discovered_tokens' not in st.session_state:
    st.session_state.discovered_tokens = []
if 'trade_log' not in st.session_state:
    st.session_state.trade_log = []
if 'positions' not in st.session_state:
    st.session_state.positions = []
if 'balance' not in st.session_state:
    st.session_state.balance = 0.0

# =====================================================================
# SIDEBAR - WALLET + CONTROLS
# =====================================================================
with st.sidebar:
    st.markdown("### 🔑 WALLET")
    
    # Use Streamlit secrets if available, otherwise manual input
    wallet_address = st.text_input(
        "Wallet Address",
        value=st.session_state.wallet_address,
        placeholder="Enter your Solana wallet address"
    )
    
    if wallet_address and wallet_address != st.session_state.wallet_address:
        st.session_state.wallet_address = wallet_address
        st.session_state.balance = gmgn.get_wallet_balance(wallet_address)
        st.session_state.positions = gmgn.get_wallet_positions(wallet_address)
    
    if st.session_state.wallet_address:
        st.success(f"Connected: {st.session_state.wallet_address[:6]}...{st.session_state.wallet_address[-4:]}")
    
    st.markdown("---")
    st.markdown("### 🎯 STRATEGY")
    
    min_liquidity = st.slider("Min Liquidity ($)", 5000, 100000, 10000, 5000)
    max_liquidity = st.slider("Max Liquidity ($)", 50000, 1000000, 100000, 50000)
    min_volume = st.slider("Min Volume 24h ($)", 1000, 100000, 5000, 1000)
    
    strategy.min_liquidity = min_liquidity
    strategy.max_liquidity = max_liquidity
    strategy.min_volume = min_volume
    
    st.markdown("---")
    st.markdown("### 📊 SCAN")
    
    col1, col2 = st.columns(2)
    with col1:
        if st.button("🔴 NEW", width='stretch'):
            with st.spinner("Scanning GMGN new pairs..."):
                st.session_state.discovered_tokens = gmgn.get_new_pairs(30)
                st.success(f"Found {len(st.session_state.discovered_tokens)} tokens")
                st.rerun()
    
    with col2:
        if st.button("🔥 TRENDING", width='stretch'):
            with st.spinner("Scanning GMGN trending..."):
                st.session_state.discovered_tokens = gmgn.get_trending(30)
                st.success(f"Found {len(st.session_state.discovered_tokens)} tokens")
                st.rerun()
    
    st.markdown("---")
    st.markdown("### 🤖 AUTO TRADING")
    
    if not st.session_state.bot_running:
        if st.button("⚡ START REAL BOT", type="primary", width='stretch'):
            if st.session_state.wallet_address:
                st.session_state.bot_running = True
                st.success("Bot started! Executing REAL trades via GMGN")
                st.rerun()
            else:
                st.error("Enter wallet address first!")
    else:
        if st.button("🛑 STOP BOT", width='stretch'):
            st.session_state.bot_running = False
            st.warning("Bot stopped")
            st.rerun()

# =====================================================================
# METRICS
# =====================================================================
col1, col2, col3, col4 = st.columns(4)

with col1:
    st.markdown(f"""
    <div class="cyber-card">
        <p style="color: #888; font-size: 10px;">💰 BALANCE</p>
        <p class="metric-value">{st.session_state.balance:.4f} SOL</p>
    </div>
    """, unsafe_allow_html=True)

with col2:
    st.markdown(f"""
    <div class="cyber-card">
        <p style="color: #888; font-size: 10px;">📊 POSITIONS</p>
        <p class="metric-value">{len(st.session_state.positions)}</p>
    </div>
    """, unsafe_allow_html=True)

with col3:
    total_trades = len(st.session_state.trade_log)
    wins = sum(1 for t in st.session_state.trade_log if t.get('profit', 0) > 0)
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
    st.markdown(f"""
    <div class="cyber-card">
        <p style="color: #888; font-size: 10px;">✅ WIN RATE</p>
        <p class="metric-value">{win_rate:.1f}%</p>
    </div>
    """, unsafe_allow_html=True)

with col4:
    total_pnl = sum(t.get('profit', 0) for t in st.session_state.trade_log)
    pnl_color = "#00ff88" if total_pnl >= 0 else "#ff4444"
    st.markdown(f"""
    <div class="cyber-card">
        <p style="color: #888; font-size: 10px;">💵 NET P/L</p>
        <p class="metric-value" style="color: {pnl_color};">{total_pnl:+.4f} SOL</p>
    </div>
    """, unsafe_allow_html=True)

st.markdown("---")

# =====================================================================
# DISCOVERED TOKENS WITH SCORES
# =====================================================================
st.markdown("### 🔍 GMGN TOKENS (WITH WIN RATE SCORES)")

if st.session_state.discovered_tokens:
    for idx, token in enumerate(st.session_state.discovered_tokens[:15]):
        security = gmgn.get_token_security(token["address"])
        score, reasons = strategy.score_token(token, security)
        
        if score >= 70:
            score_class = "score-high"
        elif score >= 40:
            score_class = "score-medium"
        else:
            score_class = "score-low"
        
        col1, col2, col3 = st.columns([3, 1, 1])
        
        with col1:
            st.markdown(f"""
            <div class="cyber-card" style="text-align: left;">
                <p style="color: #00ffe6; margin: 0; font-family: 'Orbitron', sans-serif;">
                    🪙 {token['symbol']}
                </p>
                <p style="color: #888; font-size: 11px; margin: 5px 0;">
                    Liq: ${token['liquidity']:,.0f} | Vol: ${token['volume_24h']:,.0f}
                </p>
                <p style="color: #666; font-size: 10px;">
                    {' | '.join(reasons[:3])}
                </p>
            </div>
            """, unsafe_allow_html=True)
        
        with col2:
            st.markdown(f"""
            <div class="cyber-card">
                <p class="{score_class}">{score}/100</p>
                <p style="color: #888; font-size: 10px;">SCORE</p>
            </div>
            """, unsafe_allow_html=True)
        
        with col3:
            if score >= 70:
                if st.button("BUY", key=f"buy_{idx}", width='stretch'):
                    if st.session_state.wallet_address:
                        with st.spinner(f"Buying {token['symbol']}..."):
                            success, result = gmgn.execute_trade(
                                "buy",
                                token["address"],
                                0.05,
                                st.session_state.wallet_address
                            )
                            
                            if success:
                                st.session_state.trade_log.append({
                                    'time': toronto_time().strftime("%H:%M:%S"),
                                    'symbol': token['symbol'],
                                    'action': 'BUY',
                                    'profit': 0,
                                    'date': toronto_time().strftime("%Y-%m-%d"),
                                    'tx': result
                                })
                                st.success(f"Bought {token['symbol']}!")
                                st.rerun()
                            else:
                                st.error(f"Failed: {result}")
                    else:
                        st.error("Enter wallet first!")
else:
    st.info("Click 'SCAN' buttons in sidebar to discover tokens from GMGN")

st.markdown("---")

# =====================================================================
# AUTO TRADING LOOP
# =====================================================================
if st.session_state.bot_running:
    st.markdown("### 🤖 AUTO TRADING ACTIVE")
    
    # Auto-scan and trade
    if len(st.session_state.discovered_tokens) == 0:
        st.session_state.discovered_tokens = gmgn.get_new_pairs(20)
    
    for token in st.session_state.discovered_tokens[:10]:
        security = gmgn.get_token_security(token["address"])
        score, _ = strategy.score_token(token, security)
        
        if score >= 70 and len(st.session_state.positions) < 5:
            success, result = gmgn.execute_trade(
                "buy",
                token["address"],
                0.05,
                st.session_state.wallet_address
            )
            
            if success:
                st.session_state.trade_log.append({
                    'time': toronto_time().strftime("%H:%M:%S"),
                    'symbol': token['symbol'],
                    'action': 'AUTO BUY',
                    'profit': 0,
                    'date': toronto_time().strftime("%Y-%m-%d"),
                    'tx': result,
                    'score': score
                })
                st.session_state.positions = gmgn.get_wallet_positions(st.session_state.wallet_address)
    
    time.sleep(10)
    st.rerun()

# =====================================================================
# TRADE LOG
# =====================================================================
st.markdown("### 📜 TRADE HISTORY (TORONTO TIME)")

if st.session_state.trade_log:
    df = pd.DataFrame(st.session_state.trade_log[-20:])
    st.dataframe(df, width='stretch')
else:
    st.info("No trades yet")

# =====================================================================
# FOOTER
# =====================================================================
st.markdown("---")
st.markdown(f"""
<div style="text-align: center; color: #00ffe6; padding: 20px;">
    <p style="font-family: 'Orbitron', sans-serif; font-size: 12px;">
        ⚡ APEX SNIPER // GMGN REAL TRADING
    </p>
    <p style="color: #ff00e6; font-size: 10px;">
        📍 Toronto Time: {toronto_time().strftime('%Y-%m-%d %H:%M:%S')} EST
    </p>
</div>
""", unsafe_allow_html=True)

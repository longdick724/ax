# app.py - J.A.R.V.I.S. // Complete Atom DNA HUD - Multi-Source Trading
import os
import json
import time
import base64
import threading
import random
import re
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from flask import Flask, render_template_string, jsonify, request
import requests

from dotenv import load_dotenv
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

load_dotenv()

# ================================================================
# CONFIG
# ================================================================
APP_VERSION = "18.0 ATOM DNA MULTI-AI"

SOL_MINT = "So11111111111111111111111111111111111111112"
WSOL_MINT = "So11111111111111111111111111111111111111112"

# Toronto Time (UTC-5 winter, UTC-4 summer DST)
def get_toronto_time():
    """Get current Toronto time accounting for DST"""
    now = datetime.now(timezone.utc)
    # Determine if DST (second Sunday March to first Sunday November)
    year = now.year
    march_second_sunday = datetime(year, 3, 8, 2) + timedelta(days=(6 - datetime(year, 3, 8).weekday()) % 7)
    november_first_sunday = datetime(year, 11, 1, 2) + timedelta(days=(6 - datetime(year, 11, 1).weekday()) % 7)
    
    is_dst = march_second_sunday <= now.replace(tzinfo=None) < november_first_sunday
    offset = -4 if is_dst else -5
    
    return now.astimezone(timezone(timedelta(hours=offset)))

HELIUS_KEY = os.getenv("HELIUS_KEY", "").strip()
FREELLM_API_KEY = os.getenv("FREELLM_API_KEY", "").strip()
ENV_PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY", "").strip()
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY", "").strip()

HELIUS_RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_KEY}" if HELIUS_KEY else ""
JUPITER_API_BASE = "https://quote-api.jup.ag/v6"
DEXSCREENER_API = "https://api.dexscreener.com"
BIRDEYE_API = "https://public-api.birdeye.so"
FREELLM_BASE = "https://api.freellmapi.com/v1"
COINGECKO_API = "https://api.coingecko.com/api/v3"
GMGN_API = "https://gmgn.ai/api/v1"

STATE_FILE = "jarvis_state.json"
MAX_TRADE_SOL_CAP = 0.50

# ================================================================
# ALL AI MODELS
# ================================================================
AI_MODELS = {
    # Groq Models
    "groq/gpt-oss-120b": {"name": "GPT-OSS 120B", "role": "Deep Analysis", "tokens": "6.0M"},
    "groq/gpt-oss-20b": {"name": "GPT-OSS 20B", "role": "Quick Scoring", "tokens": "6.0M"},
    "groq/compound": {"name": "Compound", "role": "Strategy", "tokens": "6.0M"},
    "groq/compound-mini": {"name": "Compound Mini", "role": "Fast Decisions", "tokens": "6.0M"},
    "groq/gpt-oss-safeguard-20b": {"name": "Safeguard 20B", "role": "Safety Check", "tokens": "6.0M"},
    "groq/allam-2-7b": {"name": "ALLaM 2 7B", "role": "Multilingual", "tokens": "15.0M"},
    "groq/qwen3.6-27b": {"name": "Qwen 3.6 27B", "role": "Pattern Recognition", "tokens": "15.0M"},
    
    # OpenRouter Models
    "openrouter/nemotron-3-super-120b": {"name": "Nemotron 120B", "role": "Risk Assessment", "tokens": "6.0M"},
    "openrouter/gemma-4-31b": {"name": "Gemma 31B", "role": "Sentiment", "tokens": "6.0M"},
    "openrouter/gemma-4-26b-a4b": {"name": "Gemma 26B", "role": "Quick Analysis", "tokens": "6.0M"},
    "openrouter/nemotron-3-nano-30b": {"name": "Nemotron Nano", "role": "Reasoning", "tokens": "6.0M"},
    "openrouter/north-mini-code": {"name": "North Mini Code", "role": "Code Analysis", "tokens": "6.0M"},
    "openrouter/poolside-laguna-s-2.1": {"name": "Laguna S", "role": "Strategy", "tokens": "6.0M"},
    "openrouter/poolside-laguna-xs-2.1": {"name": "Laguna XS", "role": "Fast Strategy", "tokens": "6.0M"},
}

# ================================================================
# MULTI-AI ENGINE
# ================================================================
class MultiAIEngine:
    def __init__(self, api_key):
        self.api_key = api_key
        self.model_status = {}
        self.cache = {}
        self.cache_ttl = 60  # 1 minute cache
        self.test_all_models()
    
    def test_all_models(self):
        """Test all available models"""
        for model_id in AI_MODELS.keys():
            response = self.query(model_id, "Say OK", 10)
            self.model_status[model_id] = response is not None
    
    def query(self, model, prompt, max_tokens=300):
        if not self.api_key:
            return None
        
        cache_key = f"{model}:{hash(prompt)}"
        if cache_key in self.cache:
            if time.time() - self.cache[cache_key]["time"] < self.cache_ttl:
                return self.cache[cache_key]["response"]
        
        try:
            headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "temperature": 0.3
            }
            r = requests.post(f"{FREELLM_BASE}/chat/completions", headers=headers, json=payload, timeout=15, verify=False)
            if r.status_code == 200:
                response = r.json().get("choices", [{}])[0].get("message", {}).get("content", "")
                self.cache[cache_key] = {"time": time.time(), "response": response}
                return response
        except:
            pass
        return None
    
    def get_multi_ai_consensus(self, token_data):
        """Get consensus score from multiple AI models"""
        prompt = f"""
        Score this Solana memecoin 0-100 for trading potential.
        Return ONLY a number.
        
        Symbol: {token_data.get('symbol', 'UNKNOWN')}
        Liquidity: ${token_data.get('liquidity', 0):,.0f}
        Volume 24h: ${token_data.get('volume_24h', 0):,.0f}
        Buys: {token_data.get('buys_24h', 0)}
        Sells: {token_data.get('sells_24h', 0)}
        Market Cap: ${token_data.get('market_cap', 0):,.0f}
        """
        
        scores = []
        # Use multiple models for consensus
        scoring_models = [
            "groq/gpt-oss-20b",
            "groq/compound-mini",
            "groq/qwen3.6-27b",
            "openrouter/gemma-4-26b-a4b",
            "openrouter/nemotron-3-nano-30b",
        ]
        
        for model in scoring_models:
            if self.model_status.get(model):
                response = self.query(model, prompt, 20)
                if response:
                    numbers = re.findall(r'\d+', response)
                    if numbers:
                        score = int(numbers[0])
                        if 0 <= score <= 100:
                            scores.append(score)
        
        if scores:
            return sum(scores) / len(scores)
        return None
    
    def get_strategy_advice(self, performance):
        """Get strategy optimization from multiple AIs"""
        prompt = f"""
        Optimize trading strategy:
        Trades: {performance.get('total_trades', 0)}
        Win Rate: {performance.get('win_rate', 0):.1f}%
        Net P/L: {performance.get('net_pnl', 0):+.4f} SOL
        
        Return: TP=X TRAIL=Y MINLIQ=Z
        """
        
        strategies = []
        strategy_models = ["groq/compound", "openrouter/nemotron-3-super-120b", "openrouter/poolside-laguna-s-2.1"]
        
        for model in strategy_models:
            if self.model_status.get(model):
                response = self.query(model, prompt, 100)
                if response:
                    strategies.append(response)
        
        return strategies
    
    def jarvis_speak(self, prompt):
        """Main JARVIS conversational response"""
        response = self.query("groq/gpt-oss-120b", f"You are JARVIS. Be concise and professional.\nUser: {prompt}\nJARVIS:", 500)
        if not response:
            response = self.query("openrouter/nemotron-3-super-120b", f"You are JARVIS.\nUser: {prompt}\nJARVIS:", 500)
        return response

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
    
    config: Dict = field(default_factory=lambda: {
        "snipe_amount": 0.05,
        "take_profit_pct": 50.0,
        "trailing_stop_pct": 15.0,
        "min_liquidity_usd": 10000.0,
        "min_volume_24h": 3000.0,
        "max_positions": 5,
        "min_score": 50,
    })

state = EngineState()

if os.path.exists(STATE_FILE):
    try:
        with open(STATE_FILE) as f:
            d = json.load(f)
        state.positions = d.get("positions", [])
        state.trades = d.get("trades", [])
    except:
        pass

def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({"positions": state.positions, "trades": state.trades}, f)
    except:
        pass

# ================================================================
# HTTP SESSION
# ================================================================
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "JARVIS/18.0"})

# ================================================================
# MULTI-SOURCE TOKEN DISCOVERY
# ================================================================
def discover_tokens_multi_source():
    """Discover tokens from multiple sources"""
    tokens = []
    
    # Source 1: DexScreener
    try:
        r = HTTP.get(f"{DEXSCREENER_API}/token-boosts/latest/v1", timeout=10, verify=False)
        if r.status_code == 200:
            mints = [x.get("tokenAddress") for x in r.json() if x.get("chainId") == "solana"]
            for i in range(0, min(len(mints), 30), 30):
                r2 = HTTP.get(f"{DEXSCREENER_API}/tokens/v1/solana/{','.join(mints[i:i+30])}", timeout=10, verify=False)
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
                                "market_cap": float(pair.get("marketCap", 0)),
                                "source": "dexscreener"
                            })
    except:
        pass
    
    # Source 2: Birdeye
    if BIRDEYE_API_KEY:
        try:
            headers = {"X-API-KEY": BIRDEYE_API_KEY, "x-chain": "solana"}
            r = HTTP.get(f"{BIRDEYE_API}/defi/v2/tokens/new_listing", headers=headers, params={"limit": 20, "meme_platform_enabled": "true"}, timeout=10, verify=False)
            if r.status_code == 200:
                data = r.json().get("data", {})
                items = data.get("items", data if isinstance(data, list) else [])
                for token in items:
                    tokens.append({
                        "mint": token.get("address"),
                        "symbol": token.get("symbol", "UNKNOWN"),
                        "liquidity": float(token.get("liquidity", 0)),
                        "volume_24h": float(token.get("volume24hUSD", 0)),
                        "buys_24h": int(token.get("buy24h", 0)),
                        "sells_24h": int(token.get("sell24h", 0)),
                        "market_cap": float(token.get("marketCap", 0)),
                        "source": "birdeye"
                    })
        except:
            pass
    
    # Source 3: GMGN
    try:
        headers = {"Authorization": f"Bearer {FREELLM_API_KEY}", "Content-Type": "application/json"}
        r = HTTP.get(f"{GMGN_API}/new_pairs/solana", headers=headers, params={"limit": 20}, timeout=10, verify=False)
        if r.status_code == 200:
            pairs = r.json().get("data", {}).get("pairs", [])
            for pair in pairs:
                tokens.append({
                    "mint": pair.get("address") or pair.get("base_address"),
                    "symbol": pair.get("symbol") or pair.get("base_symbol", "UNKNOWN"),
                    "liquidity": float(pair.get("liquidity", 0)),
                    "volume_24h": float(pair.get("volume_24h", 0)),
                    "buys_24h": int(pair.get("buys_24h", 0)),
                    "sells_24h": int(pair.get("sells_24h", 0)),
                    "market_cap": float(pair.get("market_cap", 0)),
                    "source": "gmgn"
                })
    except:
        pass
    
    # Deduplicate
    seen = set()
    unique = []
    for token in tokens:
        mint = token.get("mint")
        if mint and mint not in seen:
            seen.add(mint)
            unique.append(token)
    
    return unique[:50]

# ================================================================
# RPC
# ================================================================
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

# ================================================================
# JUPITER
# ================================================================
def jupiter_quote(input_mint, output_mint, amount, slippage_bps=500):
    try:
        r = HTTP.get(f"{JUPITER_API_BASE}/quote", params={"inputMint":input_mint,"outputMint":output_mint,"amount":amount,"slippageBps":slippage_bps}, timeout=10, verify=False)
        return r.json() if r.status_code == 200 else None
    except:
        return None

def jupiter_swap(quote, wallet):
    try:
        r = HTTP.post(f"{JUPITER_API_BASE}/swap", json={"quoteResponse":quote,"userPublicKey":str(wallet.pubkey()),"wrapAndUnwrapSol":True}, timeout=20, verify=False)
        if r.status_code != 200:
            return None
        raw = base64.b64decode(r.json()["swapTransaction"])
        unsigned = VersionedTransaction.from_bytes(raw)
        signed = VersionedTransaction(unsigned.message, [wallet])
        encoded = base64.b64encode(bytes(signed)).decode()
        r2 = HTTP.post(HELIUS_RPC_URL, json={"jsonrpc":"2.0","id":1,"method":"sendTransaction","params":[encoded,{"encoding":"base64"}]}, timeout=20, verify=False)
        if r2.status_code == 200:
            time.sleep(2)
            return r2.json().get("result")
    except:
        pass
    return None

# ================================================================
# LIVE MARKET
# ================================================================
def get_live_prices():
    try:
        r = requests.get(f"{COINGECKO_API}/simple/price", params={
            "ids": "bitcoin,ethereum,solana,binancecoin,ripple,cardano,dogecoin",
            "vs_currencies": "usd", "include_24hr_change": "true"
        }, timeout=8, verify=False)
        if r.status_code == 200:
            return r.json()
    except:
        pass
    return {}

# ================================================================
# ENGINE
# ================================================================
def engine_loop(ai_engine):
    state.log("JARVIS multi-source engine online")
    
    while True:
        try:
            if not state.running:
                time.sleep(3)
                continue
            
            # Manage positions
            for pos in list(state.positions):
                quote = jupiter_quote(pos["mint"], SOL_MINT, pos.get("out_amount", 1), 1000)
                if quote:
                    current = int(quote.get("outAmount", 0)) / 1e9
                    pnl = ((current - pos["entry_sol"]) / pos["entry_sol"]) * 100
                    if pnl >= state.config["take_profit_pct"] or pnl <= -state.config["trailing_stop_pct"]:
                        # Sell
                        result = rpc_call("getTokenAccountsByOwner", [state.wallet_address, {"mint": pos["mint"]}, {"encoding":"jsonParsed"}])
                        if result:
                            try:
                                amount = int(result["value"][0]["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
                                sell_quote = jupiter_quote(pos["mint"], SOL_MINT, amount, 1000)
                                if sell_quote:
                                    sig = jupiter_swap(sell_quote, state.wallet)
                                    if sig:
                                        exit_sol = int(sell_quote.get("outAmount", 0)) / 1e9
                                        profit = exit_sol - pos["entry_sol"]
                                        state.positions.remove(pos)
                                        state.trades.append({"date": get_toronto_time().strftime("%Y-%m-%d"), "symbol": pos["symbol"], "profit": profit})
                                        state.log(f"Sold {pos['symbol']} {profit:+.4f} SOL")
                                        save_state()
                            except:
                                pass
            
            # Buy new tokens
            if len(state.positions) < state.config["max_positions"] and state.wallet:
                tokens = discover_tokens_multi_source()
                
                for token in tokens:
                    if len(state.positions) >= state.config["max_positions"]:
                        break
                    
                    # Traditional score
                    score = 0
                    if token["liquidity"] >= state.config["min_liquidity_usd"]:
                        score += 25
                    if token["volume_24h"] >= state.config["min_volume_24h"]:
                        score += 20
                    if token["buys_24h"] > token["sells_24h"] * 1.5:
                        score += 15
                    
                    # Multi-source bonus
                    if token.get("source") == "birdeye":
                        score += 10
                    if token.get("source") == "gmgn":
                        score += 10
                    
                    # AI consensus
                    ai_score = ai_engine.get_multi_ai_consensus(token)
                    if ai_score:
                        score = int((score * 0.4) + (ai_score * 0.6))
                    
                    token["score"] = score
                    
                    if score >= state.config["min_score"]:
                        quote = jupiter_quote(SOL_MINT, token["mint"], int(state.config["snipe_amount"] * 1e9))
                        if quote:
                            sig = jupiter_swap(quote, state.wallet)
                            if sig:
                                state.positions.append({
                                    "mint": token["mint"],
                                    "symbol": token["symbol"],
                                    "entry_sol": state.config["snipe_amount"],
                                    "out_amount": int(quote.get("outAmount", 0)),
                                    "score": score,
                                })
                                state.log(f"Bought {token['symbol']} score={score}")
                                save_state()
                                time.sleep(1)
            
            time.sleep(10)
        except Exception as e:
            state.log(f"Error: {e}")
            time.sleep(5)

# ================================================================
# FLASK APP
# ================================================================
app = Flask(__name__)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>J.A.R.V.I.S.</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        
        body {
            background: #000000;
            overflow: hidden;
            height: 100vh;
            font-family: 'Courier New', monospace;
        }
        
        canvas { position: fixed; top: 0; left: 0; z-index: 1; }
        
        .title {
            position: fixed;
            top: 20px;
            left: 50%;
            transform: translateX(-50%);
            color: #00ff88;
            font-size: 24px;
            letter-spacing: 10px;
            text-shadow: 0 0 30px #00ff88, 0 0 60px #00ff88;
            z-index: 10;
        }
        
        .hud-panel {
            position: fixed;
            background: rgba(0, 20, 10, 0.85);
            border: 1px solid #00ff88;
            padding: 15px 20px;
            border-radius: 5px;
            box-shadow: 0 0 25px rgba(0, 255, 136, 0.4);
            z-index: 10;
        }
        
        .panel-title { color: #00ff88; font-size: 9px; letter-spacing: 3px; margin-bottom: 5px; }
        .panel-value { color: #ffffff; font-size: 18px; font-weight: bold; }
        .panel-sub { color: #558866; font-size: 8px; margin-top: 3px; }
        
        #panel-balance { top: 80px; left: 20px; }
        #panel-positions { bottom: 80px; left: 20px; }
        #panel-performance { top: 80px; right: 20px; }
        #panel-pnl { bottom: 80px; right: 20px; }
        
        .status {
            position: fixed;
            bottom: 20px;
            left: 50%;
            transform: translateX(-50%);
            color: #00ff88;
            font-size: 12px;
            letter-spacing: 5px;
            text-shadow: 0 0 20px #00ff88;
            z-index: 10;
        }
        
        .chat-container {
            position: fixed;
            bottom: 50px;
            left: 50%;
            transform: translateX(-50%);
            width: 80%;
            max-width: 450px;
            z-index: 20;
        }
        
        .chat-input {
            width: 100%;
            background: rgba(0, 20, 10, 0.9);
            border: 1px solid #00ff88;
            border-radius: 5px;
            padding: 12px;
            color: #00ff88;
            font-size: 12px;
            outline: none;
        }
        
        .chat-message {
            background: rgba(0, 20, 10, 0.9);
            border-left: 2px solid #00ff88;
            padding: 8px 12px;
            margin: 3px 0;
            color: #ffffff;
            font-size: 11px;
        }
        
        .controls {
            position: fixed;
            top: 70px;
            left: 50%;
            transform: translateX(-50%);
            z-index: 20;
            display: flex;
            gap: 10px;
        }
        
        .btn {
            background: rgba(0, 20, 10, 0.9);
            border: 1px solid #00ff88;
            color: #00ff88;
            padding: 8px 18px;
            border-radius: 3px;
            cursor: pointer;
            font-size: 10px;
            letter-spacing: 3px;
        }
        
        .btn:hover { background: #00ff88; color: #000; }
        
        @media (max-width: 768px) {
            .hud-panel { padding: 10px 12px; }
            .panel-value { font-size: 14px; }
            #panel-balance { top: 70px; left: 10px; }
            #panel-positions { bottom: 70px; left: 10px; }
            #panel-performance { top: 70px; right: 10px; }
            #panel-pnl { bottom: 70px; right: 10px; }
            .title { font-size: 18px; }
        }
    </style>
</head>
<body>
    <div class="title">J.A.R.V.I.S.</div>
    
    <div class="controls">
        <button class="btn" onclick="toggleEngine()">TOGGLE</button>
        <button class="btn" onclick="refreshData()">REFRESH</button>
    </div>
    
    <div class="hud-panel" id="panel-balance">
        <div class="panel-title">BALANCE</div>
        <div class="panel-value" id="balance">0.0000 SOL</div>
        <div class="panel-sub" id="balance-usd">$0.00</div>
    </div>
    
    <div class="hud-panel" id="panel-positions">
        <div class="panel-title">POSITIONS</div>
        <div class="panel-value" id="positions">0 / 5</div>
        <div class="panel-sub">ACTIVE</div>
    </div>
    
    <div class="hud-panel" id="panel-performance">
        <div class="panel-title">WIN RATE</div>
        <div class="panel-value" id="winrate">0.0%</div>
        <div class="panel-sub" id="trades">0/0</div>
    </div>
    
    <div class="hud-panel" id="panel-pnl">
        <div class="panel-title">NET P/L</div>
        <div class="panel-value" id="pnl">+0.0000</div>
        <div class="panel-sub" id="pnl-usd">$+0.00</div>
    </div>
    
    <div class="chat-container">
        <div id="messages"></div>
        <input class="chat-input" type="text" placeholder="COMMAND JARVIS..." id="chatInput" onkeypress="if(event.key==='Enter')sendCommand()">
    </div>
    
    <div class="status" id="status">SYSTEM STANDBY</div>
    
    <canvas id="canvas"></canvas>
    
    <script>
        const canvas = document.getElementById('canvas');
        const ctx = canvas.getContext('2d');
        canvas.width = window.innerWidth;
        canvas.height = window.innerHeight;
        
        const centerX = canvas.width / 2;
        const centerY = canvas.height / 2;
        
        let marketData = { BTC: 0, ETH: 0, SOL: 0, BNB: 0, XRP: 0, ADA: 0, DOGE: 0 };
        
        // Atom rings
        const rings = [
            { radius: 100, speed: 0.015, color: 'rgba(0,255,136,0.6)' },
            { radius: 160, speed: -0.012, color: 'rgba(0,255,136,0.4)' },
            { radius: 220, speed: 0.009, color: 'rgba(0,255,136,0.3)' },
            { radius: 280, speed: -0.007, color: 'rgba(0,255,136,0.25)' },
            { radius: 340, speed: 0.005, color: 'rgba(0,255,136,0.2)' },
        ];
        
        // Electrons
        const electrons = [
            { ring: 0, angle: 0, symbol: 'BTC' },
            { ring: 1, angle: Math.PI / 2, symbol: 'ETH' },
            { ring: 2, angle: Math.PI, symbol: 'SOL' },
            { ring: 3, angle: Math.PI * 1.5, symbol: 'BNB' },
            { ring: 4, angle: 0, symbol: 'XRP' },
            { ring: 1, angle: Math.PI, symbol: 'ADA' },
            { ring: 3, angle: Math.PI / 2, symbol: 'DOGE' },
        ];
        
        function drawGrid() {
            ctx.strokeStyle = 'rgba(0, 255, 136, 0.04)';
            ctx.lineWidth = 0.5;
            const gridSize = 50;
            for (let x = 0; x < canvas.width; x += gridSize) {
                ctx.beginPath();
                ctx.moveTo(x, 0);
                ctx.lineTo(x, canvas.height);
                ctx.stroke();
            }
            for (let y = 0; y < canvas.height; y += gridSize) {
                ctx.beginPath();
                ctx.moveTo(0, y);
                ctx.lineTo(canvas.width, y);
                ctx.stroke();
            }
        }
        
        function drawAtom() {
            rings.forEach((ring) => {
                ctx.beginPath();
                ctx.arc(centerX, centerY, ring.radius, 0, Math.PI * 2);
                ctx.strokeStyle = ring.color;
                ctx.lineWidth = 1;
                ctx.stroke();
            });
            
            electrons.forEach((electron) => {
                const ring = rings[electron.ring];
                electron.angle += ring.speed;
                const x = centerX + Math.cos(electron.angle) * ring.radius;
                const y = centerY + Math.sin(electron.angle) * ring.radius;
                
                ctx.beginPath();
                ctx.arc(x, y, 8, 0, Math.PI * 2);
                ctx.fillStyle = '#00ff88';
                ctx.shadowColor = '#00ff88';
                ctx.shadowBlur = 20;
                ctx.fill();
                ctx.shadowBlur = 0;
                
                ctx.fillStyle = '#ffffff';
                ctx.font = '8px Courier New';
                ctx.textAlign = 'center';
                ctx.fillText(electron.symbol, x, y - 12);
                ctx.fillText('$' + (marketData[electron.symbol] || 0).toFixed(0), x, y + 18);
            });
            
            // Core
            const pulse = 1 + Math.sin(Date.now() / 500) * 0.15;
            const coreRadius = 40 * pulse;
            const gradient = ctx.createRadialGradient(centerX, centerY, 0, centerX, centerY, coreRadius);
            gradient.addColorStop(0, '#ffffff');
            gradient.addColorStop(0.3, '#00ff88');
            gradient.addColorStop(0.7, '#006633');
            gradient.addColorStop(1, 'transparent');
            
            ctx.beginPath();
            ctx.arc(centerX, centerY, coreRadius, 0, Math.PI * 2);
            ctx.fillStyle = gradient;
            ctx.shadowColor = '#00ff88';
            ctx.shadowBlur = 50;
            ctx.fill();
            ctx.shadowBlur = 0;
        }
        
        function drawDNA() {
            const time = Date.now() / 1000;
            const dnaX = centerX + 250;
            const dnaY = centerY;
            const dnaLength = 150;
            const dnaRadius = 25;
            const dnaAmplitude = 120;
            
            for (let i = 0; i < dnaLength; i++) {
                const t = i / dnaLength;
                const angle = t * Math.PI * 6 + time;
                const y = dnaY - dnaAmplitude + t * dnaAmplitude * 2;
                const x1 = dnaX + Math.cos(angle) * dnaRadius;
                const x2 = dnaX + Math.cos(angle + Math.PI) * dnaRadius;
                
                ctx.fillStyle = 'rgba(0, 255, 136, 0.8)';
                ctx.fillRect(x1, y, 2, 2);
                ctx.fillRect(x2, y, 2, 2);
                
                if (i % 3 === 0) {
                    ctx.strokeStyle = 'rgba(0, 255, 136, 0.3)';
                    ctx.lineWidth = 0.5;
                    ctx.beginPath();
                    ctx.moveTo(x1, y);
                    ctx.lineTo(x2, y);
                    ctx.stroke();
                }
            }
        }
        
        function animate() {
            ctx.clearRect(0, 0, canvas.width, canvas.height);
            ctx.fillStyle = '#000000';
            ctx.fillRect(0, 0, canvas.width, canvas.height);
            drawGrid();
            drawAtom();
            drawDNA();
            requestAnimationFrame(animate);
        }
        
        animate();
        
        window.addEventListener('resize', () => {
            canvas.width = window.innerWidth;
            canvas.height = window.innerHeight;
        });
        
        function updateData() {
            fetch('/api/status')
                .then(r => r.json())
                .then(data => {
                    document.getElementById('balance').textContent = data.balance.toFixed(4) + ' SOL';
                    document.getElementById('balance-usd').textContent = '$' + data.balance_usd.toFixed(2);
                    document.getElementById('positions').textContent = data.positions + ' / ' + data.max_positions;
                    document.getElementById('winrate').textContent = data.win_rate.toFixed(1) + '%';
                    document.getElementById('trades').textContent = data.wins + '/' + data.total_trades;
                    document.getElementById('pnl').textContent = (data.net_pnl >= 0 ? '+' : '') + data.net_pnl.toFixed(4) + ' SOL';
                    document.getElementById('pnl-usd').textContent = '$' + (data.net_pnl_usd >= 0 ? '+' : '') + data.net_pnl_usd.toFixed(2);
                    document.getElementById('status').textContent = data.running ? 'SYSTEM ONLINE' : 'SYSTEM STANDBY';
                    marketData = data.market;
                });
        }
        
        function toggleEngine() { fetch('/api/toggle', {method: 'POST'}).then(() => updateData()); }
        function refreshData() { updateData(); }
        
        function sendCommand() {
            const input = document.getElementById('chatInput');
            const msg = input.value;
            if (!msg) return;
            const messages = document.getElementById('messages');
            messages.innerHTML += '<div class="chat-message"><b>YOU:</b> ' + msg + '</div>';
            input.value = '';
            fetch('/api/chat', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({message: msg})
            })
            .then(r => r.json())
            .then(data => {
                messages.innerHTML += '<div class="chat-message"><b>JARVIS:</b> ' + data.response + '</div>';
            });
        }
        
        updateData();
        setInterval(updateData, 5000);
    </script>
</body>
</html>
"""

@app.route('/')
def home():
    return HTML_TEMPLATE

@app.route('/api/status')
def api_status():
    balance = get_balance(state.wallet_address) if state.wallet else 0
    prices = get_live_prices()
    sol_price = prices.get("solana", {}).get("usd", 140.0)
    toronto_time = get_toronto_time()
    
    market = {
        "BTC": prices.get("bitcoin", {}).get("usd", 0),
        "ETH": prices.get("ethereum", {}).get("usd", 0),
        "SOL": prices.get("solana", {}).get("usd", 0),
        "BNB": prices.get("binancecoin", {}).get("usd", 0),
        "XRP": prices.get("ripple", {}).get("usd", 0),
        "ADA": prices.get("cardano", {}).get("usd", 0),
        "DOGE": prices.get("dogecoin", {}).get("usd", 0),
    }
    
    total_trades = len(state.trades)
    wins = sum(1 for t in state.trades if t.get("profit", 0) > 0)
    win_rate = (wins / total_trades * 100) if total_trades else 0
    net_pnl = sum(t.get("profit", 0) for t in state.trades)
    
    return jsonify({
        "balance": balance,
        "balance_usd": balance * sol_price,
        "positions": len(state.positions),
        "max_positions": state.config["max_positions"],
        "win_rate": win_rate,
        "wins": wins,
        "total_trades": total_trades,
        "net_pnl": net_pnl,
        "net_pnl_usd": net_pnl * sol_price,
        "running": state.running,
        "market": market,
        "toronto_time": toronto_time.strftime("%Y-%m-%d %H:%M:%S"),
        "timezone": "EST" if toronto_time.utcoffset().total_seconds() == -18000 else "EDT",
    })

@app.route('/api/toggle', methods=['POST'])
def api_toggle():
    state.running = not state.running
    return jsonify({"running": state.running})

@app.route('/api/chat', methods=['POST'])
def api_chat():
    data = request.json
    message = data.get("message", "")
    ai = MultiAIEngine(FREELLM_API_KEY)
    response = ai.jarvis_speak(message)
    return jsonify({"response": response or "Core unavailable."})

if __name__ == '__main__':
    if ENV_PRIVATE_KEY:
        try:
            state.wallet = Keypair.from_base58_string(ENV_PRIVATE_KEY)
            state.wallet_address = str(state.wallet.pubkey())
            print(f"Wallet connected: {state.wallet_address[:8]}...")
        except Exception as e:
            print(f"Wallet error: {e}")
    
    ai_engine = MultiAIEngine(FREELLM_API_KEY)
    threading.Thread(target=engine_loop, args=(ai_engine,), daemon=True).start()
    
    toronto = get_toronto_time()
    print(f"\nJ.A.R.V.I.S. running on http://0.0.0.0:5000")
    print(f"Toronto Time: {toronto.strftime('%Y-%m-%d %H:%M:%S')} {'EST' if toronto.utcoffset().total_seconds() == -18000 else 'EDT'}\n")
    
    app.run(host="0.0.0.0", port=5000, debug=False)

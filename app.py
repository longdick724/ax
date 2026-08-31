# app.py - J.A.R.V.I.S. // All keys from .env file
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

# Load .env file FIRST before anything else
load_dotenv()

# ================================================================
# CONFIG - ALL FROM .env FILE
# ================================================================
APP_VERSION = "21.0 ENV FIXED"
SOL_MINT = "So11111111111111111111111111111111111111112"

def get_toronto_time():
    """Toronto: UTC-5 EST winter, UTC-4 EDT summer"""
    now = datetime.now(timezone.utc)
    year = now.year
    mar8 = datetime(year, 3, 8)
    march_2nd_sun = mar8 + timedelta(days=(6 - mar8.weekday()) % 7)
    nov1 = datetime(year, 11, 1)
    nov_1st_sun = nov1 + timedelta(days=(6 - nov1.weekday()) % 7)
    is_dst = march_2nd_sun.replace(tzinfo=timezone.utc) <= now < nov_1st_sun.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone(timedelta(hours=-4 if is_dst else -5)))

# Read from .env file - NO hardcoded fallbacks
HELIUS_KEY = os.getenv("HELIUS_KEY", "")
FREELLM_API_KEY = os.getenv("FREELLM_API_KEY", "")
ENV_PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY", "")
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY", "")
FREELLM_BASE = os.getenv("FREELLM_BASE", "https://api.freellmapi.com/v1")

# Print key status for debugging
print("=" * 50)
print("J.A.R.V.I.S. ENV CHECK")
print("=" * 50)
print(f"HELIUS_KEY: {'SET' if HELIUS_KEY else 'MISSING'}")
print(f"FREELLM_API_KEY: {'SET' if FREELLM_API_KEY else 'MISSING'}")
print(f"SOLANA_PRIVATE_KEY: {'SET' if ENV_PRIVATE_KEY else 'MISSING'}")
print(f"BIRDEYE_API_KEY: {'SET' if BIRDEYE_API_KEY else 'MISSING'}")
print(f"FREELLM_BASE: {FREELLM_BASE}")
print("=" * 50)

HELIUS_RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_KEY}" if HELIUS_KEY else ""
JUPITER_API_BASE = "https://quote-api.jup.ag/v6"
DEXSCREENER_API = "https://api.dexscreener.com"
BIRDEYE_API = "https://public-api.birdeye.so"
COINGECKO_API = "https://api.coingecko.com/api/v3"
STATE_FILE = "jarvis_state.json"
MAX_TRADE_SOL_CAP = 0.50

# Try to import solders
try:
    from solders.keypair import Keypair
    from solders.transaction import VersionedTransaction
    SOLDERS_AVAILABLE = True
except ImportError:
    SOLDERS_AVAILABLE = False
    Keypair = None
    VersionedTransaction = None
    print("WARNING: solders not installed. Trading disabled.")

# ================================================================
# AI MODELS
# ================================================================
AI_MODELS = {
    "groq/gpt-oss-120b": {"name": "GPT-OSS 120B"},
    "groq/gpt-oss-20b": {"name": "GPT-OSS 20B"},
    "groq/compound": {"name": "Compound"},
    "groq/compound-mini": {"name": "Compound Mini"},
    "groq/qwen3.6-27b": {"name": "Qwen 3.6"},
    "openrouter/nemotron-3-super-120b": {"name": "Nemotron 120B"},
    "openrouter/gemma-4-31b": {"name": "Gemma 31B"},
    "openrouter/gemma-4-26b-a4b": {"name": "Gemma 26B"},
    "openrouter/nemotron-3-nano-30b": {"name": "Nemotron Nano"},
    "openrouter/poolside-laguna-s-2.1": {"name": "Laguna S"},
}

class MultiAIEngine:
    def __init__(self, api_key):
        self.api_key = api_key
        self.model_status = {}
        self.cache = {}
        self.cache_ttl = 45

    def query(self, model, prompt, max_tokens=300):
        if not self.api_key:
            return None
        key = f"{model}:{hash(prompt)}"
        if key in self.cache and time.time() - self.cache[key]["t"] < self.cache_ttl:
            return self.cache[key]["r"]
        try:
            headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
            payload = {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens, "temperature": 0.25}
            r = requests.post(f"{FREELLM_BASE}/chat/completions", headers=headers, json=payload, timeout=18, verify=False)
            if r.status_code == 200:
                content = r.json().get("choices", [{}])[0].get("message", {}).get("content", "")
                if content:
                    self.cache[key] = {"t": time.time(), "r": content}
                    self.model_status[model] = True
                    return content
        except Exception as e:
            print(f"AI Query error ({model}): {e}")
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
        for m in ["groq/gpt-oss-120b", "openrouter/nemotron-3-super-120b", "groq/compound"]:
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

# ================================================================
# HTTP
# ================================================================
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json"})

# ================================================================
# DISCOVERY
# ================================================================
def discover_tokens():
    tokens = []
    # DexScreener
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
    except Exception as e:
        state.log(f"DexScreener: {e}")

    # Birdeye
    if BIRDEYE_API_KEY:
        try:
            headers = {"X-API-KEY": BIRDEYE_API_KEY, "x-chain": "solana"}
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
                        "source": "birdeye"
                    })
        except Exception as e:
            state.log(f"Birdeye: {e}")

    # Deduplicate
    seen = set()
    unique = []
    for t in tokens:
        mint = t.get("mint")
        if mint and mint not in seen:
            seen.add(mint)
            unique.append(t)
    return unique[:50]

# ================================================================
# RPC + JUPITER
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
        r2 = HTTP.post(HELIUS_RPC_URL, json={"jsonrpc":"2.0","id":1,"method":"sendTransaction","params":[encoded,{"encoding":"base64","skipPreflight":True}]}, timeout=25, verify=False)
        if r2.status_code == 200:
            result = r2.json().get("result")
            if result:
                time.sleep(1.5)
                return result
    except Exception as e:
        state.log(f"Swap: {e}")
    return None

# ================================================================
# LIVE PRICES
# ================================================================
def get_live_prices():
    try:
        r = HTTP.get(f"{COINGECKO_API}/simple/price", params={"ids":"bitcoin,ethereum,solana,binancecoin,ripple,cardano,dogecoin","vs_currencies":"usd","include_24hr_change":"true"}, timeout=8, verify=False)
        if r.status_code == 200:
            return r.json()
    except:
        pass
    return {}

# ================================================================
# SCORING
# ================================================================
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

# ================================================================
# ENGINE
# ================================================================
def engine_loop(ai_engine):
    state.log(f"JARVIS {APP_VERSION} online")
    while True:
        try:
            if not state.running:
                time.sleep(2)
                continue
            
            # Check positions for sells
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
                                except:
                                    pass
            
            # Buy new
            if len(state.positions) < state.config["max_positions"] and state.wallet:
                tokens = discover_tokens()
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
                            state.positions.append({
                                "mint": token["mint"],
                                "symbol": token["symbol"],
                                "entry_sol": state.config["snipe_amount"],
                                "out_amount": int(q.get("outAmount") or 0),
                                "score": token["score"],
                            })
                            state.log(f"BOUGHT {token['symbol']} score={token['score']}")
                            time.sleep(1)
            
            time.sleep(8)
        except Exception as e:
            state.log(f"Engine: {e}")
            time.sleep(4)

# ================================================================
# FLASK
# ================================================================
app = Flask(__name__)

@app.route("/")
def home():
    return render_template_string("""
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>J.A.R.V.I.S.</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#000814;color:#00d4ff;font-family:'Courier New',monospace;height:100vh;overflow:hidden}
.title{position:fixed;top:20px;left:50%;transform:translateX(-50%);font-size:24px;letter-spacing:10px;color:#00e5ff;text-shadow:0 0 30px #00b4ff;z-index:10}
.panel{position:fixed;background:rgba(0,18,42,.85);border:1px solid rgba(0,180,255,.55);padding:15px 20px;border-radius:4px;box-shadow:0 0 25px rgba(0,150,255,.22);z-index:10}
.pt{font-size:9px;letter-spacing:3px;color:#00b4ff;margin-bottom:5px}
.pv{font-size:18px;font-weight:bold;color:#fff}
.ps{font-size:8px;color:#5aa0c8;margin-top:3px}
#p-bal{top:80px;left:20px}
#p-pos{bottom:80px;left:20px}
#p-wr{top:80px;right:20px}
#p-pnl{bottom:80px;right:20px}
.status{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);font-size:12px;letter-spacing:5px;color:#00ff88;text-shadow:0 0 20px #00ff88;z-index:10}
.chat{position:fixed;bottom:50px;left:50%;transform:translateX(-50%);width:80%;max-width:450px;z-index:20}
.chat input{width:100%;background:rgba(0,20,45,.96);border:1px solid #00b4ff;padding:12px;color:#00d4ff;font-size:12px;outline:none}
.msg{background:rgba(0,25,50,.92);border-left:3px solid #00b4ff;padding:8px 12px;margin:3px 0;color:#c8e8ff;font-size:11px}
.btn{position:fixed;top:70px;left:50%;transform:translateX(-50%);background:rgba(0,30,60,.9);border:1px solid #00b4ff;color:#00d4ff;padding:8px 20px;font-size:10px;letter-spacing:3px;cursor:pointer;z-index:20}
.btn:hover{background:#00b4ff;color:#000}
canvas{position:fixed;top:0;left:0;z-index:1}
</style>
</head>
<body>
<div class="title">J.A.R.V.I.S.</div>
<button class="btn" onclick="toggleEngine()">TOGGLE</button>
<div class="panel" id="p-bal"><div class="pt">BALANCE</div><div class="pv" id="balance">0.0000 SOL</div><div class="ps" id="balance-usd">$0.00</div></div>
<div class="panel" id="p-pos"><div class="pt">POSITIONS</div><div class="pv" id="positions">0 / 5</div><div class="ps">ACTIVE</div></div>
<div class="panel" id="p-wr"><div class="pt">WIN RATE</div><div class="pv" id="winrate">0.0%</div><div class="ps" id="trades">0/0</div></div>
<div class="panel" id="p-pnl"><div class="pt">NET P/L</div><div class="pv" id="pnl">+0.0000</div><div class="ps" id="pnl-usd">$+0.00</div></div>
<div class="chat"><div id="messages"></div><input type="text" placeholder="COMMAND JARVIS..." id="chatInput" onkeypress="if(event.key==='Enter')sendCommand()"></div>
<div class="status" id="status">SYSTEM STANDBY</div>
<canvas id="canvas"></canvas>
<script>
const canvas=document.getElementById('canvas'),ctx=canvas.getContext('2d');
canvas.width=innerWidth;canvas.height=innerHeight;
const CX=innerWidth/2,CY=innerHeight/2;
let market={BTC:0,ETH:0,SOL:0,BNB:0,XRP:0,ADA:0,DOGE:0};
const rings=[{r:100,s:0.01},{r:160,s:-0.008},{r:220,s:0.006},{r:280,s:-0.005},{r:340,s:0.004}];
const electrons=[{ring:0,a:0,sym:'BTC'},{ring:1,a:1.5,sym:'ETH'},{ring:2,a:3,sym:'SOL'},{ring:3,a:4.5,sym:'BNB'},{ring:4,a:0,sym:'XRP'}];
function drawAtom(t){rings.forEach(r=>{ctx.beginPath();ctx.arc(CX,CY,r.r,0,Math.PI*2);ctx.strokeStyle='rgba(0,255,136,0.3)';ctx.stroke()});
electrons.forEach(e=>{const r=rings[e.ring];e.a+=r.s;const x=CX+Math.cos(e.a)*r.r,y=CY+Math.sin(e.a)*r.r;
ctx.beginPath();ctx.arc(x,y,8,0,Math.PI*2);ctx.fillStyle='#00ff88';ctx.shadowColor='#00ff88';ctx.shadowBlur=20;ctx.fill();ctx.shadowBlur=0;
ctx.fillStyle='#fff';ctx.font='8px monospace';ctx.textAlign='center';ctx.fillText(e.sym,x,y-12);ctx.fillText('$'+(market[e.sym]||0).toFixed(0),x,y+18)});
const pulse=1+Math.sin(t/500)*0.15;const grd=ctx.createRadialGradient(CX,CY,0,CX,CY,40*pulse);
grd.addColorStop(0,'#fff');grd.addColorStop(0.3,'#00ff88');grd.addColorStop(0.7,'#006633');grd.addColorStop(1,'transparent');
ctx.beginPath();ctx.arc(CX,CY,40*pulse,0,Math.PI*2);ctx.fillStyle=grd;ctx.fill()}
function drawDNA(t){const dx=CX+250,dy=CY;for(let i=0;i<150;i++){const tt=i/150,ang=tt*Math.PI*6+t/1000,y=dy-100+tt*200,x1=dx+Math.cos(ang)*25,x2=dx+Math.cos(ang+Math.PI)*25;
ctx.fillStyle='rgba(0,255,136,0.7)';ctx.fillRect(x1,y,2,2);ctx.fillRect(x2,y,2,2);if(i%3===0){ctx.strokeStyle='rgba(0,255,136,0.2)';ctx.beginPath();ctx.moveTo(x1,y);ctx.lineTo(x2,y);ctx.stroke()}}}
function animate(){const t=Date.now();ctx.fillStyle='#000';ctx.fillRect(0,0,canvas.width,canvas.height);drawAtom(t);drawDNA(t);requestAnimationFrame(animate)}
animate();
function updateData(){fetch('/api/status').then(r=>r.json()).then(d=>{
document.getElementById('balance').textContent=d.balance.toFixed(4)+' SOL';
document.getElementById('balance-usd').textContent='$'+d.balance_usd.toFixed(2);
document.getElementById('positions').textContent=d.positions+' / '+d.max_positions;
document.getElementById('winrate').textContent=d.win_rate.toFixed(1)+'%';
document.getElementById('trades').textContent=d.wins+'/'+d.total_trades;
document.getElementById('pnl').textContent=(d.net_pnl>=0?'+':'')+d.net_pnl.toFixed(4)+' SOL';
document.getElementById('status').textContent=d.running?'SYSTEM ONLINE':'SYSTEM STANDBY';
market=d.market})}
function toggleEngine(){fetch('/api/toggle',{method:'POST'}).then(()=>updateData())}
function sendCommand(){const i=document.getElementById('chatInput'),m=i.value;if(!m)return;
document.getElementById('messages').innerHTML+='<div class="msg"><b>YOU:</b> '+m+'</div>';i.value='';
fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:m})})
.then(r=>r.json()).then(d=>{document.getElementById('messages').innerHTML+='<div class="msg"><b>JARVIS:</b> '+(d.response||'...')+'</div>'})}
updateData();setInterval(updateData,5000);
</script>
</body>
</html>
""")

@app.route("/api/status")
def api_status():
    balance = get_balance(state.wallet_address) if state.wallet_address else 0.0
    prices = get_live_prices()
    sol_price = float((prices.get("solana") or {}).get("usd") or 140.0)
    toronto = get_toronto_time()
    tz = "EDT" if (toronto.utcoffset() or timedelta(hours=-5)).total_seconds() == -14400 else "EST"
    
    market = {
        "BTC": float((prices.get("bitcoin") or {}).get("usd") or 0),
        "ETH": float((prices.get("ethereum") or {}).get("usd") or 0),
        "SOL": float((prices.get("solana") or {}).get("usd") or 0),
        "BNB": float((prices.get("binancecoin") or {}).get("usd") or 0),
        "XRP": float((prices.get("ripple") or {}).get("usd") or 0),
        "ADA": float((prices.get("cardano") or {}).get("usd") or 0),
        "DOGE": float((prices.get("dogecoin") or {}).get("usd") or 0),
    }
    
    total = len(state.trades)
    wins = sum(1 for t in state.trades if (t.get("profit") or 0) > 0)
    wr = (wins / total * 100) if total else 0.0
    net = sum(t.get("profit") or 0 for t in state.trades)
    
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
        "toronto_time": toronto.strftime("%H:%M:%S"),
        "timezone": tz,
    })

@app.route("/api/toggle", methods=["POST"])
def api_toggle():
    state.running = not state.running
    return jsonify({"running": state.running})

@app.route("/api/chat", methods=["POST"])
def api_chat():
    data = request.json or {}
    msg = (data.get("message") or "").strip()
    if not msg:
        return jsonify({"response": "Awaiting input."})
    ai = MultiAIEngine(FREELLM_API_KEY)
    return jsonify({"response": ai.jarvis_speak(msg) or "Core unavailable."})

if __name__ == "__main__":
    if ENV_PRIVATE_KEY and SOLDERS_AVAILABLE:
        try:
            state.wallet = Keypair.from_base58_string(ENV_PRIVATE_KEY)
            state.wallet_address = str(state.wallet.pubkey())
            print(f"Wallet: {state.wallet_address[:8]}...")
        except Exception as e:
            print(f"Wallet error: {e}")
    
    ai_engine = MultiAIEngine(FREELLM_API_KEY)
    threading.Thread(target=engine_loop, args=(ai_engine,), daemon=True).start()
    
    print(f"\nJ.A.R.V.I.S. running on http://0.0.0.0:5000\n")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)

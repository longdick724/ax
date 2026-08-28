# app.py — CYBER SNIPER v6.1 — single-file, 24/7 background engine + dashboard
# ------------------------------------------------------------------------
# pip install streamlit requests pandas solders python-dotenv streamlit-autorefresh
# (NOTE: the pypi `solana` package is intentionally NOT used — it breaks on
#  newer Python versions like 3.14. All RPC calls below use raw JSON-RPC
#  over `requests`, and `solders` handles keypair/transaction signing.)
# ------------------------------------------------------------------------
# Run persistently: nohup streamlit run app.py --server.headless true &
# (or wire it into systemd for real 24/7 — see notes below)
#
# ⚠️ Sniping new/trending Solana tokens is extremely high risk. Filters below
# reduce obvious rug/honeypot exposure — they do NOT guarantee profit.
# Start in PAPER MODE. Use a dedicated wallet. Never risk funds you need.
# ------------------------------------------------------------------------

import os
import json
import time
import base64
import threading
import random
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import requests
import pandas as pd
import streamlit as st
import urllib3

from dotenv import load_dotenv
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

try:
    from streamlit_autorefresh import st_autorefresh
    HAS_AUTOREFRESH = True
except ImportError:
    HAS_AUTOREFRESH = False

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
load_dotenv()

# =====================================================================
# CONFIG
# =====================================================================
ENV_PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY", "")
HELIUS_KEY = os.getenv("HELIUS_KEY", "6abff351-4518-41f5-bd8a-e344a4eef834")
HELIUS_RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_KEY}"
JUPITER_API = "https://quote-api.jup.ag/v6"
DEXSCREENER_API = "https://api.dexscreener.com"
RUGCHECK_API = "https://api.rugcheck.xyz/v1"
WSOL_MINT = "So11111111111111111111111111111111111111112"

MAX_TRADE_SOL_CAP = 0.5
STATE_FILE = "cyber_sniper_state.json"

# =====================================================================
# SHARED ENGINE STATE (thread-safe singleton across all sessions/reruns)
# =====================================================================
@dataclass
class EngineState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    running: bool = False
    paper_mode: bool = True
    wallet: Optional[Keypair] = None
    wallet_address: str = ""
    positions: List[Dict] = field(default_factory=list)
    trades: List[Dict] = field(default_factory=list)
    logs: List[str] = field(default_factory=list)
    discovered: List[Dict] = field(default_factory=list)
    config: Dict = field(default_factory=lambda: {
        "snipe_amount": 0.05,
        "take_profit_pct": 50,
        "trailing_stop_pct": 15,
        "min_liquidity_usd": 15000,
        "max_top10_pct": 40,
        "max_positions": 5,
        "daily_loss_limit": 0.2,
        "min_lp_locked_pct": 50,
    })

    def log(self, msg: str, tag: str = ""):
        ts = datetime.now().strftime("%H:%M:%S")
        with self.lock:
            self.logs.insert(0, f'<div class="line {tag}">[{ts}] {msg}</div>')
            self.logs = self.logs[:80]
        print(f"[{ts}] {msg}")

@st.cache_resource
def get_state() -> EngineState:
    s = EngineState()
    load_persisted(s)
    return s

def load_persisted(s: EngineState):
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
            s.positions = data.get("positions", [])
            s.trades = data.get("trades", [])
        except Exception:
            pass

def save_persisted(s: EngineState):
    try:
        with s.lock:
            data = {"positions": s.positions, "trades": s.trades}
        with open(STATE_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        print(f"persist error: {e}")

# =====================================================================
# RAW RPC HELPERS (no `solana` pypi package needed — avoids 3.14 breakage)
# =====================================================================
def get_wallet_balance(pubkey: str) -> float:
    if not pubkey:
        return 0.0
    try:
        payload = {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [pubkey]}
        r = requests.post(HELIUS_RPC_URL, json=payload, timeout=15, verify=False)
        if r.status_code == 200:
            return float(r.json().get("result", {}).get("value", 0)) / 1_000_000_000
    except Exception:
        pass
    return 0.0

def get_token_balance(pubkey: str, mint: str):
    try:
        payload = {"jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
                   "params": [pubkey, {"mint": mint}, {"encoding": "jsonParsed"}]}
        r = requests.post(HELIUS_RPC_URL, json=payload, timeout=15, verify=False).json()
        accounts = r.get("result", {}).get("value", [])
        if accounts:
            info = accounts[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]
            return int(info["amount"]), int(info["decimals"])
    except Exception:
        pass
    return 0, 0

def send_raw_transaction_rpc(signed_tx_bytes: bytes) -> Optional[str]:
    try:
        tx_b64 = base64.b64encode(signed_tx_bytes).decode("utf-8")
        payload = {
            "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
            "params": [tx_b64, {"skipPreflight": True, "encoding": "base64",
                                 "preflightCommitment": "processed", "maxRetries": 3}]
        }
        r = requests.post(HELIUS_RPC_URL, json=payload, timeout=20, verify=False)
        if r.status_code == 200:
            result = r.json()
            if "result" in result:
                return result["result"]
            print(f"sendTransaction error: {result.get('error')}")
    except Exception as e:
        print(f"send_raw_transaction_rpc error: {e}")
    return None

def confirm_transaction_rpc(signature: str, timeout: int = 30) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        try:
            payload = {
                "jsonrpc": "2.0", "id": 1, "method": "getSignatureStatuses",
                "params": [[signature], {"searchTransactionHistory": True}]
            }
            r = requests.post(HELIUS_RPC_URL, json=payload, timeout=10, verify=False)
            if r.status_code == 200:
                statuses = r.json().get("result", {}).get("value", [])
                if statuses and statuses[0] is not None:
                    status = statuses[0]
                    if status.get("err") is None and status.get("confirmationStatus") in ("confirmed", "finalized"):
                        return True
                    if status.get("err") is not None:
                        return False
        except Exception as e:
            print(f"confirm_transaction_rpc error: {e}")
        time.sleep(2)
    return False

# =====================================================================
# MARKET DATA HELPERS
# =====================================================================
def get_crypto_prices() -> Dict:
    try:
        r = requests.get("https://api.coingecko.com/api/v3/simple/price",
                          params={"ids": "bitcoin,ethereum,solana,binancecoin,ripple,cardano,dogecoin",
                                  "vs_currencies": "usd", "include_24hr_change": "true"},
                          timeout=10, verify=False)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return {}

def get_dexscreener_tokens() -> List[Dict]:
    try:
        r = requests.get(f"{DEXSCREENER_API}/token-boosts/latest/v1", timeout=15, verify=False)
        if r.status_code != 200:
            return []
        sol_tokens = [t for t in r.json() if t.get("chainId") == "solana"]
        out = []
        for t in sol_tokens[:15]:
            mint = t.get("tokenAddress")
            pr = requests.get(f"{DEXSCREENER_API}/latest/dex/tokens/{mint}", timeout=10, verify=False)
            if pr.status_code == 200:
                pairs = pr.json().get("pairs") or []
                if pairs:
                    p = pairs[0]
                    out.append({
                        "mint": mint,
                        "symbol": p.get("baseToken", {}).get("symbol", "???"),
                        "liquidity": float(p.get("liquidity", {}).get("usd", 0) or 0),
                        "volume_24h": float(p.get("volume", {}).get("h24", 0) or 0),
                    })
        return out
    except Exception as e:
        print(f"dexscreener error: {e}")
        return []

# =====================================================================
# JUPITER QUOTE / SWAP (sync, raw RPC based)
# =====================================================================
def jupiter_quote(input_mint: str, output_mint: str, amount: int, slippage_bps: int = 500) -> Optional[Dict]:
    try:
        r = requests.get(f"{JUPITER_API}/quote", params={
            "inputMint": input_mint, "outputMint": output_mint,
            "amount": amount, "slippageBps": slippage_bps
        }, timeout=15, verify=False)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"quote error: {e}")
    return None

def jupiter_swap(quote: Dict, wallet: Keypair) -> Optional[str]:
    try:
        r = requests.post(f"{JUPITER_API}/swap", json={
            "quoteResponse": quote,
            "userPublicKey": str(wallet.pubkey()),
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": "auto",
        }, timeout=20, verify=False)
        if r.status_code != 200:
            print(f"swap build failed: {r.text}")
            return None
        raw = base64.b64decode(r.json()["swapTransaction"])
        unsigned = VersionedTransaction.from_bytes(raw)
        signed = VersionedTransaction(unsigned.message, [wallet])

        sig = send_raw_transaction_rpc(bytes(signed))
        if not sig:
            return None
        confirm_transaction_rpc(sig)
        return sig
    except Exception as e:
        print(f"swap execute error: {e}")
        return None

# =====================================================================
# SAFETY / WIN-RATE FILTERS
# =====================================================================
def get_rugcheck_report(mint: str) -> Optional[Dict]:
    try:
        r = requests.get(f"{RUGCHECK_API}/tokens/{mint}/report", timeout=10, verify=False)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None

def passes_safety_filters(mint: str, liquidity_usd: float, cfg: Dict) -> (bool, str):
    if liquidity_usd < cfg["min_liquidity_usd"]:
        return False, f"liquidity ${liquidity_usd:,.0f} below min ${cfg['min_liquidity_usd']:,.0f}"

    report = get_rugcheck_report(mint)
    if report is None:
        return False, "rugcheck data unavailable — skipped for safety"

    if report.get("mintAuthority") not in (None, ""):
        return False, "mint authority not renounced"
    if report.get("freezeAuthority") not in (None, ""):
        return False, "freeze authority not renounced"

    top_holders = report.get("topHolders", [])
    top10_pct = sum(h.get("pct", 0) for h in top_holders[:10])
    if top10_pct > cfg["max_top10_pct"]:
        return False, f"top10 holders own {top10_pct:.1f}%"

    try:
        lp_locked = report.get("markets", [{}])[0].get("lp", {}).get("lpLockedPct", 0)
    except Exception:
        lp_locked = 0
    if lp_locked < cfg["min_lp_locked_pct"]:
        return False, f"only {lp_locked:.0f}% LP locked/burned"

    return True, "ok"

def simulate_sell_check(mint: str, test_amount: int = 1000) -> bool:
    """Honeypot check — can we even quote selling this token back to SOL?"""
    quote = jupiter_quote(mint, WSOL_MINT, test_amount)
    return quote is not None and int(quote.get("outAmount", 0)) > 0

# =====================================================================
# BUY / SELL EXECUTION
# =====================================================================
def do_buy(state: EngineState, mint: str, symbol: str, sol_amount: float) -> Optional[Dict]:
    lamports = int(sol_amount * 1_000_000_000)
    quote = jupiter_quote(WSOL_MINT, mint, lamports)
    if not quote:
        return None

    if state.paper_mode:
        sig = "PAPER"
        out_amount = int(quote.get("outAmount", 0))
    else:
        sig = jupiter_swap(quote, state.wallet)
        out_amount = int(quote.get("outAmount", 0))
        if not sig:
            return None

    return {"mint": mint, "symbol": symbol, "entry_sol": sol_amount, "out_amount": out_amount,
            "opened_at": datetime.now().isoformat(), "buy_sig": sig, "peak_pnl_pct": 0.0}

def do_sell(state: EngineState, position: Dict) -> Optional[Dict]:
    if state.paper_mode:
        pnl_pct = random.uniform(-30, 60)
        profit = position["entry_sol"] * (pnl_pct / 100)
        sig = "PAPER"
        exit_sol = position["entry_sol"] + profit
    else:
        balance, _ = get_token_balance(state.wallet_address, position["mint"])
        if balance <= 0:
            return None
        quote = jupiter_quote(position["mint"], WSOL_MINT, balance)
        if not quote:
            return None
        sig = jupiter_swap(quote, state.wallet)
        if not sig:
            return None
        exit_sol = int(quote.get("outAmount", 0)) / 1_000_000_000
        profit = exit_sol - position["entry_sol"]

    return {"date": datetime.now().strftime("%Y-%m-%d"), "time": datetime.now().strftime("%H:%M:%S"),
            "symbol": position["symbol"], "mint": position["mint"], "entry_sol": position["entry_sol"],
            "exit_sol": exit_sol, "profit": profit, "sell_sig": sig}

def get_current_pnl_pct(state: EngineState, position: Dict) -> Optional[float]:
    if state.paper_mode:
        return None  # handled via random drift in engine loop for paper positions
    quote = jupiter_quote(position["mint"], WSOL_MINT, position["out_amount"] or 1)
    if not quote:
        return None
    current_sol = int(quote.get("outAmount", 0)) / 1_000_000_000
    if position["entry_sol"] == 0:
        return None
    return ((current_sol - position["entry_sol"]) / position["entry_sol"]) * 100

def daily_pnl(state: EngineState) -> float:
    today = datetime.now().strftime("%Y-%m-%d")
    with state.lock:
        return sum(t["profit"] for t in state.trades if t["date"] == today)

# =====================================================================
# BACKGROUND ENGINE LOOP (runs forever in a daemon thread)
# =====================================================================
def engine_loop(state: EngineState):
    state.log("Engine thread started" + (" [PAPER]" if state.paper_mode else " [LIVE]"), "sys")
    while True:
        try:
            if not state.running:
                time.sleep(2)
                continue

            if daily_pnl(state) <= -abs(state.config["daily_loss_limit"]):
                state.log("🛑 Daily loss limit hit — auto-stopping engine", "sell-loss")
                with state.lock:
                    state.running = False
                time.sleep(3)
                continue

            # ---- manage open positions ----
            with state.lock:
                positions_snapshot = list(state.positions)

            for pos in positions_snapshot:
                pnl_pct = get_current_pnl_pct(state, pos)
                if state.paper_mode:
                    pnl_pct = pos.get("peak_pnl_pct", 0) + random.uniform(-8, 12)
                if pnl_pct is None:
                    continue

                with state.lock:
                    pos["peak_pnl_pct"] = max(pos.get("peak_pnl_pct", 0), pnl_pct)
                    peak = pos["peak_pnl_pct"]

                trailing_trigger = peak - state.config["trailing_stop_pct"]
                should_close, reason = False, ""
                if pnl_pct >= state.config["take_profit_pct"]:
                    should_close, reason = True, "take profit"
                elif peak > 0 and pnl_pct <= trailing_trigger:
                    should_close, reason = True, "trailing stop"
                elif peak <= 0 and pnl_pct <= -state.config["trailing_stop_pct"]:
                    should_close, reason = True, "stop loss"

                if should_close:
                    trade = do_sell(state, pos)
                    if trade:
                        with state.lock:
                            if pos in state.positions:
                                state.positions.remove(pos)
                            state.trades.append(trade)
                        tag = "sell-win" if trade["profit"] >= 0 else "sell-loss"
                        state.log(f"CLOSED {pos['symbol']} ({reason}) — {trade['profit']:+.4f} SOL", tag)
                        save_persisted(state)

            # ---- discover + buy new candidates ----
            with state.lock:
                open_count = len(state.positions)
                cfg = dict(state.config)
                can_trade = state.wallet is not None or state.paper_mode

            if open_count < cfg["max_positions"] and can_trade:
                candidates = get_dexscreener_tokens()
                with state.lock:
                    state.discovered = candidates
                for tok in candidates:
                    with state.lock:
                        if len(state.positions) >= cfg["max_positions"]:
                            break
                        already_holding = any(p["mint"] == tok["mint"] for p in state.positions)
                    if already_holding:
                        continue

                    ok, reason = passes_safety_filters(tok["mint"], tok["liquidity"], cfg)
                    if not ok:
                        state.log(f"SKIP {tok['symbol']}: {reason}", "sys")
                        continue
                    if not simulate_sell_check(tok["mint"]):
                        state.log(f"SKIP {tok['symbol']}: failed honeypot sell simulation", "sell-loss")
                        continue

                    pos = do_buy(state, tok["mint"], tok["symbol"], cfg["snipe_amount"])
                    if pos:
                        with state.lock:
                            state.positions.append(pos)
                        state.log(f"BOUGHT {tok['symbol']} for {cfg['snipe_amount']} SOL", "buy")
                        save_persisted(state)

            time.sleep(10)
        except Exception as e:
            state.log(f"engine error: {e}", "sell-loss")
            time.sleep(5)

@st.cache_resource
def start_engine_thread(_state: EngineState):
    t = threading.Thread(target=engine_loop, args=(_state,), daemon=True)
    t.start()
    return t

# =====================================================================
# PAGE CONFIG + CYBERPUNK THEME
# =====================================================================
st.set_page_config(page_title="CYBER SNIPER // NEON PROTOCOL", page_icon="🟣", layout="wide")

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@400;700;900&family=Share+Tech+Mono&display=swap');
.stApp { background:#05010d; }
.stApp::before{content:"";position:fixed;top:0;left:0;width:100%;height:100%;
 background:radial-gradient(circle at 20% 20%,rgba(255,0,200,.06) 0%,transparent 45%),
 radial-gradient(circle at 80% 80%,rgba(0,255,255,.06) 0%,transparent 45%),
 linear-gradient(rgba(0,255,150,.03) 1px,transparent 1px),
 linear-gradient(90deg,rgba(0,255,150,.03) 1px,transparent 1px);
 background-size:100% 100%,100% 100%,32px 32px,32px 32px;animation:drift 25s linear infinite;pointer-events:none;z-index:-2;}
@keyframes drift{0%{background-position:0 0,0 0,0 0,0 0;}100%{background-position:0 0,0 0,0 640px,640px 0;}}
.stApp::after{content:"";position:fixed;top:0;left:0;width:100%;height:100%;
 background:repeating-linear-gradient(0deg,rgba(0,0,0,.15) 0px,rgba(0,0,0,.15) 1px,transparent 1px,transparent 3px);
 pointer-events:none;z-index:-1;opacity:.4;}
.cyber-header{background:linear-gradient(135deg,#05010d,#12002b,#001a2b);border:2px solid #ff00e6;border-radius:14px;
 padding:28px;text-align:center;margin-bottom:20px;box-shadow:0 0 45px rgba(255,0,230,.4),0 0 45px rgba(0,255,255,.15) inset;}
.cyber-title{color:#00ffe6;font-size:38px;font-weight:900;font-family:'Orbitron',sans-serif;letter-spacing:5px;
 text-shadow:0 0 10px #00ffe6,0 0 25px #ff00e6,0 0 2px #fff;animation:glitch 3s infinite;}
@keyframes glitch{0%,100%{transform:translate(0,0);}2%{transform:translate(-2px,1px);}4%{transform:translate(2px,-1px);text-shadow:2px 0 #ff00e6,-2px 0 #00ffe6;}6%{transform:translate(0,0);}}
.sub{color:#ff00e6;font-family:'Share Tech Mono',monospace;font-size:13px;letter-spacing:2px;}
.cyber-card{background:linear-gradient(135deg,rgba(5,1,13,.95),rgba(18,0,43,.85));border:1px solid #00ffe6;border-radius:10px;
 padding:18px;text-align:center;margin:4px;box-shadow:0 0 18px rgba(0,255,230,.25);transition:.25s;}
.cyber-card:hover{box-shadow:0 0 32px rgba(255,0,230,.5);transform:translateY(-3px);border-color:#ff00e6;}
.metric-value{color:#00ffe6;font-size:22px;font-weight:700;font-family:'Orbitron',sans-serif;text-shadow:0 0 12px #00ffe6;}
.terminal{background:#000;border:1px solid #00ff88;border-radius:8px;padding:14px;font-family:'Share Tech Mono',monospace;
 font-size:12px;color:#00ff88;height:220px;overflow-y:auto;box-shadow:0 0 20px rgba(0,255,136,.2) inset;}
.terminal .buy{color:#00ffe6;} .terminal .sell-win{color:#00ff88;} .terminal .sell-loss{color:#ff2266;} .terminal .sys{color:#ff00e6;}
section[data-testid="stSidebar"]{background:linear-gradient(180deg,#05010d,#0d0022);border-right:2px solid #ff00e6;
 box-shadow:5px 0 20px rgba(255,0,230,.25);}
.sidebar-header{background:linear-gradient(135deg,#00ffe6,#ff00e6);-webkit-background-clip:text;-webkit-text-fill-color:transparent;
 font-family:'Orbitron',sans-serif;font-size:19px;font-weight:900;text-align:center;padding:8px;letter-spacing:2px;}
.stButton>button{background:linear-gradient(135deg,#05010d,#12002b);color:#00ffe6;border:2px solid #00ffe6;border-radius:8px;
 padding:12px;font-weight:bold;font-family:'Orbitron',sans-serif;letter-spacing:2px;text-transform:uppercase;transition:.25s;width:100%;}
.stButton>button:hover{background:linear-gradient(135deg,#ff00e6,#00ffe6);color:#000;box-shadow:0 0 35px rgba(255,0,230,.7);transform:scale(1.02);}
.stAlert{background:#0d0022;border:1px solid #ff00e6;color:#00ffe6;border-radius:10px;}
</style>
""", unsafe_allow_html=True)

# =====================================================================
# INIT STATE + START BACKGROUND THREAD (once, cached across sessions)
# =====================================================================
state = get_state()
start_engine_thread(state)

# =====================================================================
# HEADER
# =====================================================================
st.markdown("""
<div class="cyber-header">
    <h1 class="cyber-title">🟣 CYBER SNIPER // NEON PROTOCOL</h1>
    <p class="sub">[ 24/7 BACKGROUND ENGINE ] [ SOLANA + JUPITER ] [ PAPER / LIVE ]</p>
</div>
""", unsafe_allow_html=True)

# =====================================================================
# LIVE MARKET TICKER
# =====================================================================
crypto_prices = get_crypto_prices()
solana_price = crypto_prices.get("solana", {}).get("usd", 140.0)

if crypto_prices:
    st.markdown("### 📡 LIVE MARKET FEED")
    cols = st.columns(7)
    crypto_map = {"bitcoin":"BTC","ethereum":"ETH","solana":"SOL","binancecoin":"BNB","ripple":"XRP","cardano":"ADA","dogecoin":"DOGE"}
    for i, (crypto, symbol) in enumerate(crypto_map.items()):
        with cols[i]:
            data = crypto_prices.get(crypto, {})
            price = data.get("usd", 0)
            change = data.get("usd_24h_change", 0)
            color = "#00ff88" if change >= 0 else "#ff2266"
            st.markdown(f"""<div class="cyber-card"><p style="color:#888;font-size:10px;">{symbol}</p>
            <p style="color:#00ffe6;font-size:14px;margin:5px 0;">${price:,.2f}</p>
            <p style="color:{color};font-size:11px;">{change:+.2f}%</p></div>""", unsafe_allow_html=True)

st.markdown("---")

# =====================================================================
# SIDEBAR — WALLET, MODE, STRATEGY, CONTROLS
# =====================================================================
with st.sidebar:
    st.markdown('<div class="sidebar-header">🔑 WALLET ACCESS</div>', unsafe_allow_html=True)

    if ENV_PRIVATE_KEY and state.wallet is None:
        try:
            with state.lock:
                state.wallet = Keypair.from_base58_string(ENV_PRIVATE_KEY.strip())
                state.wallet_address = str(state.wallet.pubkey())
            st.success("Loaded wallet from .env")
        except Exception as e:
            st.error(f"Bad key in .env: {e}")

    if state.wallet:
        st.info(f"Connected: {state.wallet_address[:6]}...{state.wallet_address[-4:]}")
    else:
        key_input = st.text_input("Solana Private Key (base58)", type="password",
                                   help="Kept in server memory only. Prefer setting SOLANA_PRIVATE_KEY in .env for 24/7 use.")
        if st.button("🔌 CONNECT WALLET", width='stretch'):
            try:
                wallet = Keypair.from_base58_string(key_input.strip())
                with state.lock:
                    state.wallet = wallet
                    state.wallet_address = str(wallet.pubkey())
                st.success("Connected")
                st.rerun()
            except Exception as e:
                st.error(f"Invalid key: {e}")

    st.markdown("---")
    st.markdown('<div class="sidebar-header">⚠️ TRADING MODE</div>', unsafe_allow_html=True)
    new_paper = st.toggle("📝 PAPER TRADING (simulated, safe)", value=state.paper_mode)
    if new_paper != state.paper_mode:
        with state.lock:
            state.paper_mode = new_paper

    risk_confirmed = True
    if not state.paper_mode:
        st.warning("LIVE MODE sends real on-chain transactions with real funds.")
        confirm_text = st.text_input('Type "I ACCEPT RISK" to unlock live trading')
        risk_confirmed = confirm_text.strip().upper() == "I ACCEPT RISK"
        if not risk_confirmed:
            st.info("Live trading locked until confirmed.")

    st.markdown("---")
    st.markdown('<div class="sidebar-header">🎯 STRATEGY</div>', unsafe_allow_html=True)
    risk_level = st.selectbox("Preset", ["🛡️ Conservative", "⚖️ Moderate", "🔥 Aggressive"], index=1)
    presets = {
        "🛡️ Conservative": {"tp": 20, "trail": 8, "amount": 0.02, "min_liq": 25000, "max_top10": 30},
        "⚖️ Moderate": {"tp": 50, "trail": 15, "amount": 0.05, "min_liq": 15000, "max_top10": 40},
        "🔥 Aggressive": {"tp": 100, "trail": 25, "amount": 0.1, "min_liq": 8000, "max_top10": 50},
    }
    preset = presets[risk_level]

    snipe_amount = st.slider("Buy Amount (SOL)", 0.01, MAX_TRADE_SOL_CAP, preset["amount"], 0.01)
    take_profit = st.slider("Take Profit (%)", 10, 300, preset["tp"], 5)
    trailing_stop = st.slider("Trailing Stop (%)", 5, 50, preset["trail"], 5)
    min_liquidity = st.number_input("Min Liquidity (USD)", 1000, 200000, preset["min_liq"], 1000)
    max_top10 = st.slider("Max Top-10 Holder %", 10, 80, preset["max_top10"], 5)
    max_positions = st.slider("Max Open Positions", 1, 10, 5)
    daily_loss_limit = st.number_input("Daily Loss Kill-Switch (SOL)", 0.01, 10.0, 0.2, 0.01)

    with state.lock:
        state.config.update({
            "snipe_amount": snipe_amount, "take_profit_pct": take_profit,
            "trailing_stop_pct": trailing_stop, "min_liquidity_usd": min_liquidity,
            "max_top10_pct": max_top10, "max_positions": max_positions,
            "daily_loss_limit": daily_loss_limit,
        })

    st.markdown("---")
    st.markdown('<div class="sidebar-header">🤖 ENGINE</div>', unsafe_allow_html=True)
    st.caption("Runs 24/7 in a background thread as long as this server process is alive.")

    can_start = (state.paper_mode or (state.wallet is not None and risk_confirmed))
    if not state.running:
        if st.button("⚡ START ENGINE", type="primary", width='stretch', disabled=not can_start):
            with state.lock:
                state.running = True
            state.log("Engine activated by operator", "sys")
            st.rerun()
        if not can_start:
            st.caption("Connect wallet + confirm risk (if live) to enable.")
    else:
        if st.button("🛑 STOP ENGINE", width='stretch'):
            with state.lock:
                state.running = False
            state.log("Engine stopped by operator", "sys")
            st.rerun()

    if st.button("🔍 FORCE SCAN NOW", width='stretch'):
        with state.lock:
            tokens = get_dexscreener_tokens()
            state.discovered = tokens
        st.success(f"Found {len(tokens)} tokens")

# =====================================================================
# METRICS
# =====================================================================
if state.wallet:
    live_balance = get_wallet_balance(state.wallet_address)
else:
    live_balance = 0.0

with state.lock:
    positions_copy = list(state.positions)
    trades_copy = list(state.trades)
    discovered_copy = list(state.discovered)
    logs_copy = list(state.logs)
    running = state.running
    paper_mode = state.paper_mode

col1, col2, col3, col4 = st.columns(4)
with col1:
    st.markdown(f"""<div class="cyber-card"><p style="color:#888;font-size:10px;">💰 BALANCE</p>
    <p class="metric-value">{live_balance:.6f} SOL</p>
    <p style="color:#00ffe6;font-size:10px;">${live_balance*solana_price:.2f} USD</p></div>""", unsafe_allow_html=True)
with col2:
    st.markdown(f"""<div class="cyber-card"><p style="color:#888;font-size:10px;">📊 OPEN POSITIONS</p>
    <p class="metric-value">{len(positions_copy)}</p>
    <p style="color:#00ffe6;font-size:10px;">MAX {state.config['max_positions']}</p></div>""", unsafe_allow_html=True)
with col3:
    total_trades = len(trades_copy)
    wins = sum(1 for t in trades_copy if t.get("profit", 0) > 0)
    win_rate = (wins/total_trades*100) if total_trades else 0
    st.markdown(f"""<div class="cyber-card"><p style="color:#888;font-size:10px;">✅ WIN RATE</p>
    <p class="metric-value">{win_rate:.1f}%</p>
    <p style="color:#00ffe6;font-size:10px;">{wins}/{total_trades}</p></div>""", unsafe_allow_html=True)
with col4:
    total_pnl = sum(t.get("profit", 0) for t in trades_copy)
    pnl_color = "#00ff88" if total_pnl >= 0 else "#ff2266"
    st.markdown(f"""<div class="cyber-card"><p style="color:#888;font-size:10px;">💵 NET P/L</p>
    <p class="metric-value" style="color:{pnl_color};">{total_pnl:+.6f} SOL</p>
    <p style="color:{pnl_color};font-size:10px;">${total_pnl*solana_price:+.2f} USD</p></div>""", unsafe_allow_html=True)

st.markdown("---")

# =====================================================================
# DISCOVERED TOKENS — manual buy option
# =====================================================================
st.markdown("### 🔍 DISCOVERED TOKENS")
if discovered_copy:
    for idx, token in enumerate(discovered_copy[:10]):
        symbol = token.get("symbol", "UNKNOWN")
        mint = token.get("mint", "")
        liquidity = token.get("liquidity", 0)
        volume = token.get("volume_24h", 0)
        c1, c2 = st.columns([4, 1])
        with c1:
            st.markdown(f"""<div class="cyber-card" style="text-align:left;">
                <p style="color:#00ffe6;margin:0;">🪙 {symbol}</p>
                <p style="color:#888;margin:5px 0;font-size:11px;">Liquidity: ${liquidity:,.0f} | Volume: ${volume:,.0f} | {mint[:6]}...{mint[-4:] if mint else ''}</p>
            </div>""", unsafe_allow_html=True)
        with c2:
            disabled = (not paper_mode and state.wallet is None) or len(positions_copy) >= state.config["max_positions"]
            if st.button(f"BUY {snipe_amount} SOL", key=f"buy_{idx}", disabled=disabled):
                ok, reason = passes_safety_filters(mint, liquidity, state.config)
                if not ok:
                    st.error(f"Blocked by safety filter: {reason}")
                elif not simulate_sell_check(mint):
                    st.error("Blocked: failed honeypot sell simulation")
                else:
                    pos = do_buy(state, mint, symbol, snipe_amount)
                    if pos:
                        with state.lock:
                            state.positions.append(pos)
                        state.log(f"MANUAL BUY {symbol} for {snipe_amount} SOL", "buy")
                        save_persisted(state)
                        st.success(f"Bought {symbol}")
                        st.rerun()
                    else:
                        st.error("Buy failed — check liquidity/slippage")
else:
    st.info("Click 'FORCE SCAN NOW' in sidebar, or wait for the engine's auto-scan.")

st.markdown("---")

# =====================================================================
# OPEN POSITIONS
# =====================================================================
st.markdown("### 📌 OPEN POSITIONS")
if positions_copy:
    for idx, pos in enumerate(positions_copy):
        pnl_pct = pos.get("peak_pnl_pct", 0)
        pnl_color = "#00ff88" if pnl_pct >= 0 else "#ff2266"
        c1, c2 = st.columns([4, 1])
        with c1:
            st.markdown(f"""<div class="cyber-card" style="text-align:left;">
                <p style="color:#00ffe6;margin:0;">🎯 {pos['symbol']}</p>
                <p style="color:#888;margin:5px 0;font-size:11px;">Entry: {pos['entry_sol']} SOL</p>
                <p style="color:{pnl_color};margin:0;font-weight:bold;">peak {pnl_pct:+.2f}%</p>
            </div>""", unsafe_allow_html=True)
        with c2:
            if st.button("SELL NOW", key=f"sell_{idx}"):
                trade = do_sell(state, pos)
                if trade:
                    with state.lock:
                        if pos in state.positions:
                            state.positions.remove(pos)
                        state.trades.append(trade)
                    tag = "sell-win" if trade["profit"] >= 0 else "sell-loss"
                    state.log(f"MANUAL SELL {pos['symbol']} — {trade['profit']:+.4f} SOL", tag)
                    save_persisted(state)
                    st.rerun()
                else:
                    st.error("Sell failed")
else:
    st.info("No open positions.")

st.markdown("---")

# =====================================================================
# TERMINAL LOG
# =====================================================================
st.markdown("### 🖥️ LIVE TERMINAL")
term_html = "".join(logs_copy) or '<div class="sys">// awaiting activity...</div>'
st.markdown(f'<div class="terminal">{term_html}</div>', unsafe_allow_html=True)

st.markdown("---")

# =====================================================================
# PNL CALENDAR
# =====================================================================
st.markdown("### 📅 PNL CALENDAR")
today = datetime.now()
calendar_data = []
for i in range(30):
    day = today - timedelta(days=i)
    day_trades = [t for t in trades_copy if t.get("date", "") == day.strftime("%Y-%m-%d")]
    day_pnl = sum(t.get("profit", 0) for t in day_trades)
    calendar_data.append({
        "Date": day.strftime("%m/%d"),
        "P/L (SOL)": f"{day_pnl:+.6f}",
        "Trades": len(day_trades),
        "Win Rate": f"{(sum(1 for t in day_trades if t.get('profit',0)>0)/len(day_trades)*100) if day_trades else 0:.0f}%",
    })
st.dataframe(pd.DataFrame(calendar_data), width='stretch')

st.markdown("---")

# =====================================================================
# STATUS + AUTOREFRESH
# =====================================================================
mode_txt = "PAPER" if paper_mode else "LIVE"
if running:
    st.markdown(f"""<div style="text-align:center;padding:16px;">
        <h2 style="color:#00ff88;font-family:'Orbitron',sans-serif;text-shadow:0 0 20px #00ff88;">
            🟢 ENGINE RUNNING 24/7 — {mode_txt} MODE
        </h2></div>""", unsafe_allow_html=True)
else:
    st.markdown("""<div style="text-align:center;padding:16px;">
        <h2 style="color:#ff2266;font-family:'Orbitron',sans-serif;">🔴 ENGINE STOPPED</h2></div>""", unsafe_allow_html=True)

if HAS_AUTOREFRESH:
    st_autorefresh(interval=6000, key="dash_refresh")
else:
    st.caption("pip install streamlit-autorefresh for live auto-updates.")

st.markdown("""
<div style="text-align:center;padding:16px;color:#00ffe6;">
    <p style="font-family:'Orbitron',sans-serif;font-size:10px;">
        🟣 CYBER SNIPER // NEON PROTOCOL v6.1 — RAW RPC, NO SOLANA-PY DEPENDENCY
    </p>
</div>
""", unsafe_allow_html=True)

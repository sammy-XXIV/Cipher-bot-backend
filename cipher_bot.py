"""
CIPHER TRADING BOT
==================
- Scans top tokens every hour
- Calculates TA indicators
- Picks best setups using Claude AI
- Sends Telegram notification for approval
- Asks margin type, % balance, leverage
- Executes on MEXC Futures
- Monitors position stats
"""

import os, json, time, hmac, hashlib, math, logging, threading, requests
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS

# ============================================================
# CONFIG
# ============================================================
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
TELEGRAM_TOKEN     = os.environ.get("TELEGRAM_TOKEN", "8704846732:AAEWE1hML2blUGlSW6-iYScW5RyQ9YhuUP8")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "7394075113")
MEXC_ACCESS_KEY    = os.environ.get("MEXC_ACCESS_KEY", "mx0vgls2weJIUHs0Xv")
MEXC_SECRET_KEY    = os.environ.get("MEXC_SECRET_KEY", "bb4d33cadd21491c9ef04e1ddc151796")
MEXC_BASE          = "https://contract.mexc.com"

SCAN_INTERVAL      = 3600   # 1 hour in seconds
TOP_N              = 30     # scan top 30 tokens

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger("CIPHER-BOT")

# ============================================================
# FLASK
# ============================================================
app = Flask(__name__)
CORS(app, origins="*")

bot_state = {
    "running": False,
    "last_scan": None,
    "pending_trade": None,
    "active_position": None,
    "trade_history": [],
    "scan_results": [],
    "waiting_for": None,
    "setup": {},
    "timeframe": "4h",   # default timeframe
    "tf_label": "4H",
    "tf_limit": 90,
    "tf_mexc": "Hour4",
    "tf_bybit": "240",
}

# ============================================================
# TELEGRAM
# ============================================================
def tg(text, buttons=None):
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    }
    if buttons:
        payload["reply_markup"] = json.dumps({
            "inline_keyboard": buttons
        })
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json=payload, timeout=10
        )
    except Exception as e:
        log.error(f"TG error: {e}")

def tg_get_updates(offset=None):
    try:
        params = {"timeout": 10, "allowed_updates": ["message","callback_query"]}
        if offset:
            params["offset"] = offset
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params=params, timeout=15
        )
        return r.json().get("result", [])
    except:
        return []

def tg_answer_callback(callback_id):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery",
            json={"callback_query_id": callback_id}, timeout=5
        )
    except:
        pass

# ============================================================
# MARKET DATA
# ============================================================
def get_top_tokens():
    # Try Binance first
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/24hr", timeout=10)
        data = r.json()
        if isinstance(data, list) and len(data) > 10:
            pairs = [t for t in data if isinstance(t, dict) and t.get("symbol","").endswith("USDT") and float(t.get("quoteVolume",0)) > 5_000_000]
            pairs.sort(key=lambda x: float(x["quoteVolume"]), reverse=True)
            log.info(f"Tokens from BINANCE: {len(pairs[:TOP_N])}")
            return [{"symbol": t["symbol"].replace("USDT",""), "price": float(t["lastPrice"]), "change": float(t["priceChangePercent"]), "volume": float(t["quoteVolume"])} for t in pairs[:TOP_N]]
    except Exception as e:
        log.error(f"Binance ticker error: {e}")

    # Fallback: Bybit
    try:
        r = requests.get("https://api.bybit.com/v5/market/tickers?category=spot", timeout=10)
        data = r.json()
        lst = data.get("result", {}).get("list", [])
        pairs = [t for t in lst if t.get("symbol","").endswith("USDT") and float(t.get("turnover24h",0)) > 0]
        pairs.sort(key=lambda x: float(x.get("turnover24h",0)), reverse=True)
        if pairs:
            log.info(f"Tokens from BYBIT: {len(pairs[:TOP_N])}")
            return [{"symbol": t["symbol"].replace("USDT",""), "price": float(t["lastPrice"]), "change": float(t.get("price24hPcnt",0))*100, "volume": float(t.get("turnover24h",0))} for t in pairs[:TOP_N]]
    except Exception as e:
        log.error(f"Bybit ticker error: {e}")

    # Fallback: OKX
    try:
        r = requests.get("https://www.okx.com/api/v5/market/tickers?instType=SPOT", timeout=10)
        data = r.json()
        lst = data.get("data", [])
        pairs = [t for t in lst if t.get("instId","").endswith("-USDT") and float(t.get("volCcy24h",0)) > 0]
        pairs.sort(key=lambda x: float(x.get("volCcy24h",0)), reverse=True)
        if pairs:
            log.info(f"Tokens from OKX: {len(pairs[:TOP_N])}")
            return [{"symbol": t["instId"].replace("-USDT",""), "price": float(t["last"]), "change": ((float(t["last"])-float(t["open24h"]))/float(t["open24h"]))*100 if float(t.get("open24h",1))>0 else 0, "volume": float(t.get("volCcy24h",0))} for t in pairs[:TOP_N]]
    except Exception as e:
        log.error(f"OKX ticker error: {e}")

    # Fallback: MEXC
    try:
        r = requests.get("https://www.mexc.com/open/api/v2/market/ticker", timeout=10)
        data = r.json()
        lst = data.get("data", [])
        pairs = [t for t in lst if t.get("symbol","").endswith("_USDT") and float(t.get("volume",0)) > 0]
        pairs.sort(key=lambda x: float(x.get("amount",0)), reverse=True)
        if pairs:
            log.info(f"Tokens from MEXC: {len(pairs[:TOP_N])}")
            return [{"symbol": t["symbol"].replace("_USDT",""), "price": float(t["last"]), "change": float(t.get("priceChangePercent",0)), "volume": float(t.get("volume",0))} for t in pairs[:TOP_N]]
    except Exception as e:
        log.error(f"MEXC ticker error: {e}")

    log.error("All token sources failed!")
    return []

def get_candles(symbol, interval=None, limit=None):
    tf = interval or bot_state["timeframe"]
    lim = limit or bot_state["tf_limit"]
    mexc_tf = bot_state["tf_mexc"]
    bybit_tf = bot_state["tf_bybit"]
    sources = [
        ("binance", f"https://api.binance.com/api/v3/klines?symbol={symbol}USDT&interval={tf}&limit={lim}", "binance"),
        ("bybit",   f"https://api.bybit.com/v5/market/kline?category=spot&symbol={symbol}USDT&interval={bybit_tf}&limit={lim}", "bybit"),
        ("mexc",    f"https://contract.mexc.com/api/v1/contract/kline/{symbol}_USDT?interval={mexc_tf}&limit={lim}", "mexc"),
    ]
    for name, url, fmt in sources:
        try:
            r = requests.get(url, timeout=6)
            if not r.ok:
                continue
            data = r.json()
            if fmt == "binance" and isinstance(data, list) and len(data) > 20:
                return [{"o":float(c[1]),"h":float(c[2]),"l":float(c[3]),"c":float(c[4]),"v":float(c[5])} for c in data]
            if fmt == "bybit":
                lst = data.get("result",{}).get("list",[])
                if lst:
                    lst = list(reversed(lst))
                    return [{"o":float(c[1]),"h":float(c[2]),"l":float(c[3]),"c":float(c[4]),"v":float(c[5])} for c in lst]
            if fmt == "mexc":
                d = data.get("data",{})
                if d and d.get("time"):
                    return [{"o":float(d["open"][i]),"h":float(d["high"][i]),"l":float(d["low"][i]),"c":float(d["close"][i]),"v":float(d["vol"][i])} for i in range(len(d["time"]))]
        except:
            continue
    return []

# ============================================================
# TECHNICAL ANALYSIS
# ============================================================
def ema(closes, p):
    k = 2/(p+1); e = closes[0]
    result = []
    for c in closes:
        e = c*k + e*(1-k); result.append(e)
    return result

def calc_indicators(candles):
    if len(candles) < 30:
        return None
    closes = [c["c"] for c in candles]
    highs  = [c["h"] for c in candles]
    lows   = [c["l"] for c in candles]
    vols   = [c["v"] for c in candles]
    n = len(closes)

    # RSI
    g=l=0
    for i in range(1,15):
        d=closes[i]-closes[i-1]
        if d>0: g+=d
        else: l-=d
    rs = g/l if l>0 else 100
    rsi = round(100-(100/(1+rs)))

    # MACD
    m12=ema(closes,12); m26=ema(closes,26)
    ml=[m12[i]-m26[i] for i in range(n)]
    sig=ema(ml,9)
    macd=ml[-1]-sig[-1]

    # ATR
    atr_sum=0
    for i in range(n-14,n):
        tr=max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
        atr_sum+=tr
    atr=atr_sum/14

    # EMA
    e20=ema(closes,20); e50=ema(closes,50)

    # Bollinger
    sl=closes[-20:]; bm=sum(sl)/20
    bstd=math.sqrt(sum((x-bm)**2 for x in sl)/20)
    bb_upper=bm+2*bstd; bb_lower=bm-2*bstd

    # Support/Resistance
    sup=min(lows[-20:]); res=max(highs[-20:])

    # ADX
    adx=0
    try:
        trs,pdms,mdms=[],[],[]
        for i in range(1,n):
            tr=max(highs[i]-lows[i],abs(highs[i]-closes[i-1]),abs(lows[i]-closes[i-1]))
            up=highs[i]-highs[i-1]; dn=lows[i-1]-lows[i]
            trs.append(tr); pdms.append(up if up>dn and up>0 else 0); mdms.append(dn if dn>up and dn>0 else 0)
        atr14=sum(trs[:14]); pdm14=sum(pdms[:14]); mdm14=sum(mdms[:14])
        dxs=[]
        for i in range(14,len(trs)):
            atr14=atr14-atr14/14+trs[i]; pdm14=pdm14-pdm14/14+pdms[i]; mdm14=mdm14-mdm14/14+mdms[i]
            pdi=100*pdm14/atr14; mdi=100*mdm14/atr14
            dx=100*abs(pdi-mdi)/(pdi+mdi) if (pdi+mdi)>0 else 0
            dxs.append(dx)
        adx=round(sum(dxs[-14:])/min(14,len(dxs)))
    except: pass

    # Volume
    avg_vol=sum(vols[-20:])/20
    vol_ratio=vols[-1]/avg_vol if avg_vol>0 else 1
    vol_strength = 'HIGH' if vol_ratio>1.5 else 'ABOVE AVG' if vol_ratio>1 else 'LOW'

    # Stochastic RSI
    stoch_rsi = 50
    try:
        rsi_arr=[]
        for i in range(14, n):
            gg=ll=0
            for j in range(i-13, i+1):
                d=closes[j]-closes[j-1]
                if d>0: gg+=d
                else: ll-=d
            rsi_arr.append(100-(100/(1+(gg/ll if ll>0 else 100))))
        if len(rsi_arr)>=14:
            rmin=min(rsi_arr[-14:]); rmax=max(rsi_arr[-14:])
            stoch_rsi=round((rsi_arr[-1]-rmin)/(rmax-rmin)*100) if rmax!=rmin else 50
    except: pass

    # VWAP
    vwap=0
    try:
        cumTPV=cumVol=0
        for i in range(n):
            tp=(highs[i]+lows[i]+closes[i])/3
            cumTPV+=tp*vols[i]; cumVol+=vols[i]
        vwap=round(cumTPV/cumVol,4) if cumVol>0 else closes[-1]
    except: pass

    # Fibonacci Retracement
    swing_high=max(highs[-50:]) if n>=50 else max(highs)
    swing_low=min(lows[-50:])   if n>=50 else min(lows)
    fib_range=swing_high-swing_low
    fib382=round(swing_high-fib_range*0.382, 4)
    fib500=round(swing_high-fib_range*0.500, 4)
    fib618=round(swing_high-fib_range*0.618, 4)

    # Candlestick Pattern (last candle)
    candle_pattern = 'NONE'
    try:
        last=candles[-1]; prev=candles[-2]
        body=abs(last['c']-last['o']); rng=last['h']-last['l']
        upper_wick=last['h']-max(last['o'],last['c'])
        lower_wick=min(last['o'],last['c'])-last['l']
        is_green=last['c']>last['o']
        prev_green=prev['c']>prev['o']
        if rng>0 and body/rng<0.1:
            candle_pattern='DOJI'
        elif not is_green and lower_wick>body*2 and upper_wick<body*0.5:
            candle_pattern='HAMMER (bullish)'
        elif is_green and upper_wick>body*2 and lower_wick<body*0.5:
            candle_pattern='SHOOTING STAR (bearish)'
        elif is_green and not prev_green and last['o']<prev['c'] and last['c']>prev['o']:
            candle_pattern='BULLISH ENGULFING'
        elif not is_green and prev_green and last['o']>prev['c'] and last['c']<prev['o']:
            candle_pattern='BEARISH ENGULFING'
    except: pass

    # RSI Divergence
    divergence = 'NONE'
    try:
        mid = n // 2
        price_end=closes[-1]; price_mid=closes[mid]
        rsi_end=rsi
        gg=ll=0
        for i in range(mid-13, mid+1):
            d=closes[i]-closes[i-1]
            if d>0: gg+=d
            else: ll-=d
        rsi_mid=100-(100/(1+(gg/ll if ll>0 else 100)))
        if price_end>price_mid and rsi_end<rsi_mid: divergence='BEARISH DIVERGENCE'
        elif price_end<price_mid and rsi_end>rsi_mid: divergence='BULLISH DIVERGENCE'
    except: pass

    # Score — confluence of signals (enhanced)
    score=0
    if rsi<40: score+=2
    elif rsi<50: score+=1
    if macd>0: score+=2
    if closes[-1]>e20[-1]: score+=1
    if closes[-1]>e50[-1]: score+=1
    if closes[-1]>vwap: score+=1
    if adx>25: score+=1
    if vol_ratio>1.2: score+=1
    if stoch_rsi<25: score+=1
    if 'bullish' in candle_pattern.lower() or 'HAMMER' in candle_pattern: score+=1
    if divergence=='BULLISH DIVERGENCE': score+=2

    return {
        "rsi": rsi, "macd": round(macd,4), "atr": round(atr,4),
        "e20": round(e20[-1],4), "e50": round(e50[-1],4),
        "bb_upper": round(bb_upper,4), "bb_lower": round(bb_lower,4),
        "sup": round(sup,4), "res": round(res,4),
        "adx": adx, "vol_ratio": round(vol_ratio,2), "vol_strength": vol_strength,
        "stoch_rsi": stoch_rsi, "vwap": vwap,
        "fib382": fib382, "fib500": fib500, "fib618": fib618,
        "candle_pattern": candle_pattern, "divergence": divergence,
        "swing_high": round(swing_high,4), "swing_low": round(swing_low,4),
        "price": closes[-1], "score": score,
    }

# ============================================================
# AI SIGNAL
# ============================================================
def get_fear_greed():
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=5)
        d = r.json()
        val = int(d["data"][0]["value"])
        label = d["data"][0]["value_classification"].upper()
        return f"{label} ({val}/100)"
    except:
        return "UNKNOWN"

def get_ai_signal(symbol, indicators, fear_greed="UNKNOWN"):
    if not ANTHROPIC_API_KEY:
        return None
    ind = indicators
    prompt = f"""You are CIPHER, elite crypto technical analyst. Analyze {symbol} for a trade ({bot_state['tf_label']} timeframe).

CURRENT PRICE: ${ind['price']}
MARKET SENTIMENT: Fear & Greed — {fear_greed}

TREND:
- EMA20: ${ind['e20']} — price {'ABOVE (bullish)' if ind['price']>ind['e20'] else 'BELOW (bearish)'}
- EMA50: ${ind['e50']} — price {'ABOVE (bullish)' if ind['price']>ind['e50'] else 'BELOW (bearish)'}
- VWAP: ${ind['vwap']} — price {'ABOVE (bullish)' if ind['price']>ind['vwap'] else 'BELOW (bearish)'}
- ADX: {ind['adx']} {'(STRONG TREND)' if ind['adx']>25 else '(WEAK/RANGING — caution)'}

MOMENTUM:
- RSI(14): {ind['rsi']} {'(OVERSOLD)' if ind['rsi']<30 else '(OVERBOUGHT)' if ind['rsi']>70 else '(NEUTRAL)'}
- Stochastic RSI: {ind['stoch_rsi']} {'(OVERSOLD)' if ind['stoch_rsi']<20 else '(OVERBOUGHT)' if ind['stoch_rsi']>80 else '(NEUTRAL)'}
- MACD: {'BULLISH' if ind['macd']>0 else 'BEARISH'} ({ind['macd']})
- RSI Divergence: {ind['divergence']}

VOLATILITY:
- ATR(14): ${ind['atr']}
- Bollinger Upper: ${ind['bb_upper']} Lower: ${ind['bb_lower']}

KEY LEVELS:
- Support: ${ind['sup']} | Resistance: ${ind['res']}
- Swing High: ${ind['swing_high']} | Swing Low: ${ind['swing_low']}
- Fib 38.2%: ${ind['fib382']} | 50%: ${ind['fib500']} | 61.8%: ${ind['fib618']}

PRICE ACTION:
- Candlestick Pattern: {ind['candle_pattern']}
- Volume: {ind['vol_strength']} ({ind['vol_ratio']}x average)

Respond ONLY with JSON (no markdown):
{{"signal":"LONG or SHORT or SKIP","confidence":50-95,"entry":"price","stop":"1.5x ATR stop loss","target":"3x ATR take profit","reasoning":"2-3 sentences","rr":"ratio e.g. 1:2","caution":"1 sentence on main risk"}}"""

    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
            json={"model": "claude-sonnet-4-20250514", "max_tokens": 300, "messages": [{"role": "user", "content": prompt}]},
            timeout=30
        )
        raw = r.json()["content"][0]["text"].strip().replace("```json","").replace("```","").strip()
        return json.loads(raw)
    except Exception as e:
        log.error(f"AI signal error: {e}")
        return None

# ============================================================
# MEXC FUTURES TRADING
# ============================================================
def mexc_sign(params):
    query = "&".join(f"{k}={v}" for k,v in sorted(params.items()))
    sig = hmac.new(MEXC_SECRET_KEY.encode(), query.encode(), hashlib.sha256).hexdigest()
    return sig

def mexc_request(method, path, params=None, body=None):
    ts = str(int(time.time() * 1000))
    if params is None:
        params = {}

    if method == "GET":
        # GET: sign query string
        params["timestamp"] = ts
        sorted_params = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        sig = hmac.new(MEXC_SECRET_KEY.encode(), sorted_params.encode(), hashlib.sha256).hexdigest()
        params["signature"] = sig
        headers = {
            "ApiKey": MEXC_ACCESS_KEY,
            "Request-Time": ts,
            "Content-Type": "application/json",
        }
        try:
            r = requests.get(MEXC_BASE + path, params=params, headers=headers, timeout=10)
            return r.json()
        except Exception as e:
            log.error(f"MEXC GET error: {e}")
            return {"error": str(e)}
    else:
        # POST: sign body
        body = body or {}
        body_str = json.dumps(body)
        sign_str = ts + MEXC_ACCESS_KEY + ts + body_str
        sig = hmac.new(MEXC_SECRET_KEY.encode(), sign_str.encode(), hashlib.sha256).hexdigest()
        headers = {
            "ApiKey": MEXC_ACCESS_KEY,
            "Request-Time": ts,
            "Signature": sig,
            "Content-Type": "application/json",
        }
        try:
            r = requests.post(MEXC_BASE + path, json=body, headers=headers, timeout=10)
            return r.json()
        except Exception as e:
            log.error(f"MEXC POST error: {e}")
            return {"error": str(e)}

def get_account_balance():
    try:
        r = mexc_request("GET", "/api/v1/private/account/assets")
        log.info(f"Balance response: {str(r)[:200]}")
        assets = r.get("data", [])
        if isinstance(assets, list):
            usdt = next((a for a in assets if str(a.get("currency","")).upper() == "USDT"), None)
            if usdt:
                return float(usdt.get("availableBalance", 0))
        elif isinstance(assets, dict):
            # some endpoints return dict with currency as key
            usdt = assets.get("USDT", {})
            return float(usdt.get("availableBalance", 0))
    except Exception as e:
        log.error(f"Balance error: {e}")
    return 0

def get_position_stats(symbol):
    try:
        r = mexc_request("GET", "/api/v1/private/position/open_positions", {"symbol": f"{symbol}_USDT"})
        positions = r.get("data", [])
        if positions:
            p = positions[0]
            return {
                "symbol": symbol,
                "side": p.get("positionType",""),
                "size": p.get("holdVol",""),
                "entry_price": p.get("openAvgPrice",""),
                "liquidation": p.get("liquidatePrice",""),
                "unrealized_pnl": p.get("unrealisedPnl",""),
                "margin": p.get("im",""),
                "leverage": p.get("leverage",""),
                "margin_type": "CROSS" if p.get("autoAddIm") else "ISOLATED",
            }
    except Exception as e:
        log.error(f"Position error: {e}")
    return None

def open_position(symbol, side, size, leverage, margin_type, stop_loss, take_profit):
    """Open a futures position on MEXC"""
    try:
        # Set leverage
        mexc_request("POST", "/api/v1/private/position/change_leverage", body={
            "symbol": f"{symbol}_USDT",
            "leverage": leverage,
            "openType": 1 if margin_type=="ISOLATED" else 2,
            "positionType": 1 if side=="LONG" else 2,
        })

        # Place order
        order_side = 1 if side == "LONG" else 3  # 1=open long, 3=open short
        body = {
            "symbol": f"{symbol}_USDT",
            "side": order_side,
            "orderType": 5,  # market order
            "vol": size,
            "openType": 1 if margin_type=="ISOLATED" else 2,
            "leverage": leverage,
        }
        r = mexc_request("POST", "/api/v1/private/order/submit", body=body)
        order_id = r.get("data")

        if order_id:
            # Set stop loss
            mexc_request("POST", "/api/v1/private/planorder/place", body={
                "symbol": f"{symbol}_USDT",
                "side": 2 if side=="LONG" else 4,  # close direction
                "orderType": 3,  # stop market
                "triggerPrice": stop_loss,
                "vol": size,
                "triggerType": 2,
                "executeCycle": 87600,
            })
            # Set take profit
            mexc_request("POST", "/api/v1/private/planorder/place", body={
                "symbol": f"{symbol}_USDT",
                "side": 2 if side=="LONG" else 4,
                "orderType": 3,
                "triggerPrice": take_profit,
                "vol": size,
                "triggerType": 1,
                "executeCycle": 87600,
            })
            return {"success": True, "order_id": order_id}

    except Exception as e:
        log.error(f"Open position error: {e}")
    return {"success": False}

def close_position(symbol):
    try:
        pos = get_position_stats(symbol)
        if not pos:
            return False
        side = 2 if "LONG" in str(pos["side"]) else 4
        mexc_request("POST", "/api/v1/private/order/submit", body={
            "symbol": f"{symbol}_USDT",
            "side": side,
            "orderType": 5,
            "vol": pos["size"],
            "openType": 1,
        })
        return True
    except Exception as e:
        log.error(f"Close position error: {e}")
        return False

# ============================================================
# SCAN ENGINE
# ============================================================
def run_scan():
    log.info(f"Starting scan on {bot_state['tf_label']} timeframe...")
    tg(f"🔍 <b>CIPHER BOT</b> — Starting scan of top {TOP_N} tokens on <b>{bot_state['tf_label']}</b> timeframe...")

    tokens = get_top_tokens()
    if not tokens:
        tg("⚠️ Failed to fetch token list. Retrying next hour.")
        return

    # Fetch Fear & Greed once for all tokens
    fear_greed = get_fear_greed()
    log.info(f"Fear & Greed: {fear_greed}")

    results = []
    for t in tokens:
        candles = get_candles(t["symbol"])
        if not candles:
            continue
        ind = calc_indicators(candles)
        if not ind:
            continue
        if ind["score"] >= 5 and ind["adx"] > 20:
            signal = get_ai_signal(t["symbol"], ind, fear_greed)
            if signal and signal.get("signal") != "SKIP" and signal.get("confidence", 0) >= 70:
                results.append({
                    "symbol": t["symbol"],
                    "price": t["price"],
                    "indicators": ind,
                    "signal": signal,
                    "score": ind["score"],
                })
        time.sleep(0.3)

    results.sort(key=lambda x: x["signal"].get("confidence", 0), reverse=True)
    bot_state["scan_results"] = results
    bot_state["last_scan"] = datetime.now().isoformat()

    if not results:
        tg("📊 Scan complete. <b>No strong setups found</b> this hour. Will retry in 1 hour.")
        return

    # Send top 3 results
    top = results[:3]
    msg = f"📊 <b>SCAN COMPLETE</b> — Found {len(results)} setups\n\n"
    msg += "🏆 <b>TOP PICKS:</b>\n\n"

    for i, r in enumerate(top, 1):
        s = r["signal"]
        ind = r["indicators"]
        emoji = "🟢" if s["signal"]=="LONG" else "🔴"
        msg += f"{i}. {emoji} <b>{r['symbol']}</b> — {s['signal']}\n"
        msg += f"   💰 Price: ${r['price']}\n"
        msg += f"   📈 Confidence: {s.get('confidence')}%\n"
        msg += f"   🎯 Entry: {s.get('entry')} | TP: {s.get('target')} | SL: {s.get('stop')}\n"
        msg += f"   ⚖️ R/R: {s.get('rr','1:2')}\n"
        msg += f"   📝 {s.get('reasoning','')}\n"
        if s.get('caution'):
            msg += f"   ⚠️ {s.get('caution')}\n"
        msg += "\n"

    msg += "Select a token to trade or skip:"

    buttons = []
    for r in top:
        s = r["signal"]
        buttons.append([{"text": f"{'🟢 LONG' if s['signal']=='LONG' else '🔴 SHORT'} {r['symbol']} ({s.get('confidence')}%)", "callback_data": f"trade_{r['symbol']}"}])
    buttons.append([{"text": "⏭ Skip — No trade this hour", "callback_data": "skip"}])

    tg(msg, buttons)
    bot_state["waiting_for"] = "trade_selection"

# ============================================================
# MESSAGE HANDLER
# ============================================================
def handle_update(update):
    # Handle button clicks
    if "callback_query" in update:
        cb = update["callback_query"]
        data = cb.get("data","")
        tg_answer_callback(cb["id"])

        if data.startswith("tf_") and bot_state["waiting_for"] == "timeframe_select":
            tf_map = {
                "tf_15m": {"timeframe":"15m","tf_label":"15M","tf_limit":80,"tf_mexc":"Min15","tf_bybit":"15","style":"⚡ Scalping (hold minutes)"},
                "tf_1h":  {"timeframe":"1h", "tf_label":"1H", "tf_limit":80,"tf_mexc":"Min60","tf_bybit":"60","style":"📊 Day Trading (hold hours)"},
                "tf_4h":  {"timeframe":"4h", "tf_label":"4H", "tf_limit":90,"tf_mexc":"Hour4","tf_bybit":"240","style":"🔄 Swing Trading (hold 1-3 days)"},
                "tf_1d":  {"timeframe":"1d", "tf_label":"1D", "tf_limit":60,"tf_mexc":"Day1","tf_bybit":"D","style":"📅 Position Trading (hold weeks)"},
            }
            if data in tf_map:
                cfg = tf_map[data]
                bot_state["timeframe"] = cfg["timeframe"]
                bot_state["tf_label"]  = cfg["tf_label"]
                bot_state["tf_limit"]  = cfg["tf_limit"]
                bot_state["tf_mexc"]   = cfg["tf_mexc"]
                bot_state["tf_bybit"]  = cfg["tf_bybit"]
                bot_state["waiting_for"] = None
                tg(
                    f"✅ Timeframe set to <b>{cfg['tf_label']}</b>\n"
                    f"{cfg['style']}\n\n"
                    f"Next scan will use this timeframe.\nType /scan to run now."
                )

        elif data.startswith("trade_") and bot_state["waiting_for"] == "trade_selection":
            symbol = data.replace("trade_","")
            result = next((r for r in bot_state["scan_results"] if r["symbol"]==symbol), None)
            if result:
                bot_state["pending_trade"] = result
                bot_state["setup"] = {}
                bot_state["waiting_for"] = "margin_type"
                tg(
                    f"✅ Selected <b>{symbol}</b>\n\n"
                    f"Step 1️⃣ — Choose margin type:",
                    [[
                        {"text": "🔒 ISOLATED (safer)", "callback_data": "margin_isolated"},
                        {"text": "🔓 CROSS", "callback_data": "margin_cross"},
                    ]]
                )

        elif data in ["margin_isolated","margin_cross"] and bot_state["waiting_for"] == "margin_type":
            bot_state["setup"]["margin"] = "ISOLATED" if data=="margin_isolated" else "CROSS"
            bot_state["waiting_for"] = "balance_pct"
            tg(
                f"✅ Margin: <b>{bot_state['setup']['margin']}</b>\n\n"
                f"Step 2️⃣ — How much % of your balance to use?\n"
                f"(Your balance will be fetched live)",
                [[
                    {"text": "10%", "callback_data": "pct_10"},
                    {"text": "20%", "callback_data": "pct_20"},
                    {"text": "30%", "callback_data": "pct_30"},
                    {"text": "50%", "callback_data": "pct_50"},
                ]]
            )

        elif data.startswith("pct_") and bot_state["waiting_for"] == "balance_pct":
            bot_state["setup"]["pct"] = int(data.replace("pct_",""))
            bot_state["waiting_for"] = "leverage"
            tg(
                f"✅ Balance usage: <b>{bot_state['setup']['pct']}%</b>\n\n"
                f"Step 3️⃣ — Choose leverage:",
                [[
                    {"text": "5x",  "callback_data": "lev_5"},
                    {"text": "10x", "callback_data": "lev_10"},
                    {"text": "20x", "callback_data": "lev_20"},
                    {"text": "50x", "callback_data": "lev_50"},
                ]]
            )

        elif data.startswith("lev_") and bot_state["waiting_for"] == "leverage":
            bot_state["setup"]["leverage"] = int(data.replace("lev_",""))
            # Fetch balance and show summary
            balance = get_account_balance()
            pct = bot_state["setup"]["pct"]
            lev = bot_state["setup"]["leverage"]
            margin = bot_state["setup"]["margin"]
            trade = bot_state["pending_trade"]
            signal = trade["signal"]

            margin_used = round(balance * pct / 100, 2)
            position_size = round(margin_used * lev, 2)
            liq_pct = round((margin_used / position_size) * 100, 2) if margin=="ISOLATED" else round((balance / position_size) * 100, 2)

            summary = (
                f"📋 <b>TRADE SUMMARY</b>\n\n"
                f"Token: <b>{trade['symbol']}</b>\n"
                f"Signal: <b>{signal['signal']}</b> ({signal.get('confidence')}% confidence)\n\n"
                f"💰 Wallet Balance: <b>${balance}</b>\n"
                f"📊 Margin Used: <b>${margin_used}</b> ({pct}%)\n"
                f"⚡ Leverage: <b>{lev}x</b>\n"
                f"📦 Position Size: <b>${position_size}</b> notional\n"
                f"🔐 Margin Type: <b>{margin}</b>\n"
                f"⚠️ Liquidation: <b>{liq_pct}% move against you</b>\n\n"
                f"🎯 Entry: <b>{signal.get('entry')}</b>\n"
                f"✅ Take Profit: <b>{signal.get('target')}</b>\n"
                f"🛑 Stop Loss: <b>{signal.get('stop')}</b>\n"
                f"⚖️ R/R: <b>{signal.get('rr','1:2')}</b>\n\n"
                f"⚠️ <i>NOT FINANCIAL ADVICE</i>\n\n"
                f"Confirm trade?"
            )

            bot_state["setup"]["balance"] = balance
            bot_state["setup"]["margin_used"] = margin_used
            bot_state["setup"]["position_size"] = position_size
            bot_state["waiting_for"] = "confirm"

            tg(summary, [[
                {"text": "✅ CONFIRM TRADE", "callback_data": "confirm_yes"},
                {"text": "❌ CANCEL", "callback_data": "confirm_no"},
            ]])

        elif data == "confirm_yes" and bot_state["waiting_for"] == "confirm":
            trade = bot_state["pending_trade"]
            setup = bot_state["setup"]
            signal = trade["signal"]
            tg(f"⚡ Executing <b>{trade['symbol']}</b> {signal['signal']} on MEXC...")

            result = open_position(
                symbol=trade["symbol"],
                side=signal["signal"],
                size=setup["position_size"],
                leverage=setup["leverage"],
                margin_type=setup["margin"],
                stop_loss=signal.get("stop"),
                take_profit=signal.get("target"),
            )

            if result.get("success"):
                bot_state["active_position"] = {
                    "symbol": trade["symbol"],
                    "side": signal["signal"],
                    "entry": signal.get("entry"),
                    "stop": signal.get("stop"),
                    "target": signal.get("target"),
                    "size": setup["position_size"],
                    "leverage": setup["leverage"],
                    "margin": setup["margin"],
                    "opened_at": datetime.now().isoformat(),
                    "order_id": result.get("order_id"),
                }
                bot_state["trade_history"].append(bot_state["active_position"].copy())
                tg(
                    f"✅ <b>POSITION OPENED!</b>\n\n"
                    f"🪙 {trade['symbol']} {signal['signal']}\n"
                    f"📦 Size: ${setup['position_size']} @ {setup['leverage']}x\n"
                    f"🎯 TP: {signal.get('target')} | SL: {signal.get('stop')}\n\n"
                    f"Type /stats to monitor your position."
                )
            else:
                tg("❌ <b>Order failed.</b> Check MEXC account and try again.")

            bot_state["pending_trade"] = None
            bot_state["waiting_for"] = None
            bot_state["setup"] = {}

        elif data in ["confirm_no","skip"]:
            bot_state["pending_trade"] = None
            bot_state["waiting_for"] = None
            bot_state["setup"] = {}
            tg("⏭ Trade skipped. Bot will scan again next hour.")

    # Handle text commands
    elif "message" in update:
        msg = update["message"]
        text = msg.get("text","").strip()

        if text == "/start":
            tg(
                "🤖 <b>CIPHER TRADING BOT</b>\n\n"
                "I scan top 30 tokens every hour and find the best trading setups.\n\n"
                "Commands:\n"
                "/scan — Run scan now\n"
                "/timeframe — Change scan timeframe\n"
                "/stats — View open position\n"
                "/close — Close active position\n"
                "/history — Trade history\n"
                "/balance — Check MEXC balance\n"
                "/stop — Stop bot\n"
            )

        elif text == "/timeframe":
            current = bot_state["tf_label"]
            tg(
                f"⏱ <b>SCAN TIMEFRAME</b>\n\nCurrent: <b>{current}</b>\n\nSelect new timeframe:",
                [[
                    {"text": "15m ⚡ Scalping",    "callback_data": "tf_15m"},
                    {"text": "1H 📊 Day Trade",    "callback_data": "tf_1h"},
                ],[
                    {"text": "4H 🔄 Swing",        "callback_data": "tf_4h"},
                    {"text": "1D 📅 Position",     "callback_data": "tf_1d"},
                ]]
            )
            bot_state["waiting_for"] = "timeframe_select"

        elif text == "/scan":
            threading.Thread(target=run_scan, daemon=True).start()

        elif text == "/stats":
            pos = bot_state.get("active_position")
            if not pos:
                tg("📊 No active position.")
                return
            live = get_position_stats(pos["symbol"])
            if live:
                pnl = float(live.get("unrealized_pnl",0))
                tg(
                    f"📊 <b>POSITION STATS</b>\n\n"
                    f"🪙 Symbol: <b>{live['symbol']}</b>\n"
                    f"📈 Side: <b>{live['side']}</b>\n"
                    f"💰 Entry: <b>{live['entry_price']}</b>\n"
                    f"📦 Size: <b>{live['size']}</b>\n"
                    f"⚡ Leverage: <b>{live['leverage']}x</b>\n"
                    f"🔐 Margin: <b>{live['margin_type']}</b>\n"
                    f"⚠️ Liquidation: <b>{live['liquidation']}</b>\n"
                    f"{'🟢' if pnl>=0 else '🔴'} Unrealized PnL: <b>${pnl:.2f}</b>\n"
                    f"🎯 TP: {pos['target']} | SL: {pos['stop']}\n"
                )
            else:
                tg("📊 Position may be closed or data unavailable.")

        elif text == "/close":
            pos = bot_state.get("active_position")
            if not pos:
                tg("No active position to close.")
                return
            tg(f"Closing {pos['symbol']} position...", [[
                {"text": "✅ Yes, close it", "callback_data": "close_yes"},
                {"text": "❌ No", "callback_data": "close_no"},
            ]])
            bot_state["waiting_for"] = "close_confirm"

        elif text == "/balance":
            bal = get_account_balance()
            tg(f"💰 MEXC Futures Balance: <b>${bal:.2f} USDT</b>")

        elif text == "/history":
            history = bot_state.get("trade_history", [])
            if not history:
                tg("📜 No trade history yet.")
                return
            msg = "📜 <b>TRADE HISTORY</b>\n\n"
            for t in history[-5:]:
                msg += f"• {t['symbol']} {t['side']} — Entry: {t['entry']} | Size: ${t['size']}\n"
            tg(msg)

        elif text == "/stop":
            bot_state["running"] = False
            tg("🛑 Bot stopped.")

    # Handle close confirm
    if "callback_query" in update:
        cb = update["callback_query"]
        data = cb.get("data","")
        if data == "close_yes" and bot_state["waiting_for"] == "close_confirm":
            pos = bot_state["active_position"]
            success = close_position(pos["symbol"])
            if success:
                tg(f"✅ <b>{pos['symbol']}</b> position closed.")
                bot_state["active_position"] = None
            else:
                tg("❌ Failed to close position. Check MEXC manually.")
            bot_state["waiting_for"] = None
        elif data == "close_no" and bot_state["waiting_for"] == "close_confirm":
            bot_state["waiting_for"] = None
            tg("Position kept open.")

# ============================================================
# BOT POLLING LOOP
# ============================================================
def polling_loop():
    offset = None
    log.info("Polling loop started")
    while bot_state["running"]:
        updates = tg_get_updates(offset)
        for u in updates:
            offset = u["update_id"] + 1
            try:
                handle_update(u)
            except Exception as e:
                log.error(f"Handle update error: {e}")
        time.sleep(1)

def scan_loop():
    log.info("Scan loop started")
    while bot_state["running"]:
        run_scan()
        for _ in range(SCAN_INTERVAL):
            if not bot_state["running"]:
                break
            time.sleep(1)

# ============================================================
# FLASK ROUTES (position stats for CIPHER web app)
# ============================================================
@app.after_request
def cors_headers(r):
    r.headers['Access-Control-Allow-Origin'] = '*'
    r.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    r.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    return r

@app.route('/ping')
def ping():
    return jsonify({"status": "CIPHER BOT online", "running": bot_state["running"]})

@app.route('/position')
def position():
    pos = bot_state.get("active_position")
    if not pos:
        return jsonify({"position": None})
    live = get_position_stats(pos["symbol"])
    return jsonify({"position": live or pos})

@app.route('/history')
def history():
    return jsonify({"history": bot_state["trade_history"]})

@app.route('/balance')
def balance():
    bal = get_account_balance()
    return jsonify({"balance": bal, "currency": "USDT"})

@app.route('/start_bot', methods=['POST'])
def start_bot():
    if bot_state["running"]:
        return jsonify({"status": "already running"})
    bot_state["running"] = True
    threading.Thread(target=polling_loop, daemon=True).start()
    threading.Thread(target=scan_loop, daemon=True).start()
    tg("🤖 <b>CIPHER BOT STARTED</b>\nType /scan to run now or wait for hourly scan.")
    return jsonify({"status": "started"})

@app.route('/stop_bot', methods=['POST'])
def stop_bot():
    bot_state["running"] = False
    tg("🛑 CIPHER BOT stopped.")
    return jsonify({"status": "stopped"})

if __name__ == '__main__':
    # Auto-start bot
    bot_state["running"] = True
    threading.Thread(target=polling_loop, daemon=True).start()
    threading.Thread(target=scan_loop, daemon=True).start()
    tg("🤖 <b>CIPHER BOT ONLINE</b>\n\nType /start for commands.")
    port = int(os.environ.get("PORT", 5001))
    app.run(host='0.0.0.0', port=port, debug=False)


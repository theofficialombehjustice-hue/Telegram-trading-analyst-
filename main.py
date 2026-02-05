import aiohttp, asyncio, pandas as pd, ta, datetime, numpy as np, json, os
from binance.client import Client
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes

TELEGRAM_TOKEN = "8435849845:AAE709exeyXbaQYvtU9_3BfIA0ngp3oZq_w"
TWELVEDATA_KEY = "064dbdcd38734f70b71db008e502f5e0"
STATE_FILE = "bot_state.json"
BINANCE_API_KEY = "7orWh37gHVtd1oEEJ8a3RRHMdaEMIVLBe02MmVYAPaRMyOSwhx47ySTAaW5h016N"
BINANCE_API_SECRET = "zCelW6CBfALhJhdhcInFqF9VC8Hc95JN1MahcnO6Xukr2oDVRHE3VDqyeJNttwn2"

MIN_CANDLES = 150
SCAN_INTERVAL = 3600
TRACK_INTERVAL = 60
COOLDOWN_BASE = 3600
VOLATILITY_THRESHOLD = 0.003

CRYPTOS = {
    "ETHUSDT":"ETH/USD","BNBUSDT":"BNB/USD","XRPUSDT":"XRP/USD","SOLUSDT":"SOL/USD",
    "ADAUSDT":"ADA/USD","DOGEUSDT":"DOGE/USD","AVAXUSDT":"AVAX/USD","DOTUSDT":"DOT/USD",
    "MATICUSDT":"MATIC/USD","LTCUSDT":"LTC/USD","LINKUSDT":"LINK/USD","TRXUSDT":"TRX/USD",
    "ATOMUSDT":"ATOM/USD","UNIUSDT":"UNI/USD","SHIBUSDT":"SHIB/USD","FTMUSDT":"FTM/USD",
    "NEARUSDT":"NEAR/USD","AAVEUSDT":"AAVE/USD","EOSUSDT":"EOS/USD","XLMUSDT":"XLM/USD",
    "SUSHIUSDT":"SUSHI/USD","ALGOUSDT":"ALGO/USD","CHZUSDT":"CHZ/USD","KSMUSDT":"KSM/USD",
    "ZILUSDT":"ZIL/USD","ENJUSDT":"ENJ/USD","GRTUSDT":"GRT/USD","BATUSDT":"BAT/USD","RVNUSDT":"RVN/USD"
}

active_trades = {}
cooldowns = {}
live_trades = {}
learning_weight = 1.0
loss_streak = 0
auto_trade_active = False
capital = 20.0
max_loss_percent = 5
trade_percent = 25
leverage = 5

binance = Client(BINANCE_API_KEY, BINANCE_API_SECRET, testnet=False)

if os.path.exists(STATE_FILE):
    with open(STATE_FILE,"r") as f:
        d=json.load(f)
        learning_weight=d.get("learning_weight",learning_weight)
        loss_streak=d.get("loss_streak",loss_streak)
        capital=d.get("capital",capital)
        auto_trade_active=d.get("auto_trade_active",auto_trade_active)

def persist():
    with open(STATE_FILE,"w") as f:
        json.dump({
            "learning_weight":learning_weight,
            "loss_streak":loss_streak,
            "capital":capital,
            "auto_trade_active":auto_trade_active
        },f)

def evaluate_signal(score):
    return "Strong Signal 🚀" if score>=3 else "No strong signals"

async def fetch(session, symbol, interval="15min", outputsize=1000):
    try:
        async with session.get("https://api.twelvedata.com/time_series",
            params={"symbol":CRYPTOS[symbol],"interval":interval,"outputsize":outputsize,"apikey":TWELVEDATA_KEY},
            timeout=10) as r:
            j = await r.json()
            if "values" in j:
                rows=[{"c":float(v["close"]),"h":float(v["high"]),"l":float(v["low"]),"v":float(v.get("volume",0))} for v in reversed(j["values"])]
                return pd.DataFrame(rows)
    except:
        pass
    return pd.DataFrame()

def enrich(df):
    if len(df)<MIN_CANDLES:
        return pd.DataFrame()
    df["RSI"]=ta.momentum.RSIIndicator(df["c"],14).rsi()
    df["EMA50"]=ta.trend.EMAIndicator(df["c"],50).ema_indicator()
    df["EMA200"]=ta.trend.EMAIndicator(df["c"],200).ema_indicator()
    df["MACD"]=ta.trend.MACD(df["c"]).macd_diff()
    df["ADX"]=ta.trend.ADXIndicator(df["h"],df["l"],df["c"]).adx()
    df["ATR"]=ta.volatility.AverageTrueRange(df["h"],df["l"],df["c"]).average_true_range()
    df["BOLL_H"]=ta.volatility.BollingerBands(df["c"]).bollinger_hband()
    df["BOLL_L"]=ta.volatility.BollingerBands(df["c"]).bollinger_lband()
    df["VWAP"]=ta.volume.VolumeWeightedAveragePrice(df["h"],df["l"],df["c"],df["v"]).volume_weighted_average_price()
    df["SuperTrend"]=ta.trend.STCIndicator(df["c"]).stc()
    df["StochRSI"]=ta.momentum.StochRSIIndicator(df["c"]).stochrsi()
    df["OBV"]=ta.volume.OnBalanceVolumeIndicator(df["c"],df["v"]).on_balance_volume()
    df["VOLUSD"]=df["v"]*df["c"]
    return df.dropna()

def signal(df, weight=1):
    last=df.iloc[-1]
    if last["ATR"]/last["c"]<VOLATILITY_THRESHOLD:
        return None
    s=0
    s+=2.5 if last["EMA50"]>last["EMA200"] else -2.5
    s+=1.2 if last["RSI"]>55 else -1.2 if last["RSI"]<45 else 0
    s+=1.5 if last["MACD"]>0 else -1.5
    s+=1.2 if last["ADX"]>20 else 0
    s+=1.0 if last["SuperTrend"]>last["c"] else -1.0
    s+=0.8 if last["StochRSI"]>0.8 else -0.8 if last["StochRSI"]<0.2 else 0
    s*=weight
    if s<3:
        return None
    p=last["c"]
    atr=last["ATR"]
    return {"dir":"BUY","entry":p,"sl":p-atr,"tp":p+atr*2,"score":s}

async def scan(context):
    if not auto_trade_active:
        return
    now=datetime.datetime.utcnow().timestamp()
    async with aiohttp.ClientSession() as s:
        signals=[]
        for sym in CRYPTOS:
            if sym in active_trades:
                continue
            if sym in cooldowns and cooldowns[sym]>now:
                continue
            df=enrich(await fetch(s,sym,"1h"))
            if df.empty:
                continue
            sig=signal(df,learning_weight)
            if sig:
                signals.append((sym,sig))
        signals=sorted(signals,key=lambda x:abs(x[1]["score"]),reverse=True)[:3]
        for sym,sig in signals:
            active_trades[sym]=sig
            live_trades[sym]=sig
        if signals:
            msg="🚀 TOP SIGNALS\n\n"
            for sym,sig in signals:
                msg+=f"{sym}\n{sig['dir']} | Score {sig['score']} | {evaluate_signal(sig['score'])}\n\n"
            await context.bot.send_message(chat_id=context.job.chat_id,text=msg)

async def track(context):
    global learning_weight, loss_streak, capital, auto_trade_active
    if not auto_trade_active:
        return
    now=datetime.datetime.utcnow().timestamp()
    remove=[]
    async with aiohttp.ClientSession() as s:
        for sym,t in active_trades.items():
            df=enrich(await fetch(s,sym,"15min",200))
            if df.empty:
                continue
            price=df.iloc[-1]["c"]
            if price>=t["tp"]:
                capital += (capital*trade_percent/100)*leverage*0.1
                learning_weight=min(1.3,learning_weight+0.03)
                remove.append(sym)
            elif price<=t["sl"]:
                capital -= (capital*trade_percent/100)*leverage*0.1
                loss_streak+=1
                learning_weight=max(0.6,learning_weight-0.07)
                cooldowns[sym]=now+COOLDOWN_BASE*(1+loss_streak*1.5)
                remove.append(sym)
    for r in remove:
        active_trades.pop(r,None)
        live_trades.pop(r,None)
    if capital<=20*(1-max_loss_percent/100):
        auto_trade_active=False
    persist()

async def live(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if not live_trades:
        await update.message.reply_text("No active trades currently.")
        return
    msg="📊 LIVE TRADES\n\n"
    for sym,t in live_trades.items():
        msg+=f"{sym} | {t['dir']} | Entry {t['entry']} | SL {t['sl']} | TP {t['tp']} | Score {t['score']}\n"
    await update.message.reply_text(msg)

async def start(update:Update,context:ContextTypes.DEFAULT_TYPE):
    kb=[[InlineKeyboardButton(k,callback_data=k)] for k in CRYPTOS]
    await update.message.reply_text("📡 Select a coin or wait for auto scan",reply_markup=InlineKeyboardMarkup(kb))
    context.job_queue.run_repeating(scan,SCAN_INTERVAL,chat_id=update.effective_chat.id)
    context.job_queue.run_repeating(track,TRACK_INTERVAL,chat_id=update.effective_chat.id)

async def analyze_callback(update:Update,context:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query
    await q.answer()
    sym=q.data
    async with aiohttp.ClientSession() as s:
        df=enrich(await fetch(s,sym,"1h"))
        if df.empty:
            await q.edit_message_text("No data")
            return
        sig=signal(df,learning_weight)
        if not sig:
            await q.edit_message_text("No signal")
            return
        await q.edit_message_text(f"{sym}\n{sig['dir']}\nEntry {sig['entry']}\nSL {sig['sl']}\nTP {sig['tp']}\nScore {sig['score']}")

async def activate(update:Update,context:ContextTypes.DEFAULT_TYPE):
    global auto_trade_active
    auto_trade_active=True
    persist()
    await update.message.reply_text("✅ Auto-trading activated.")

async def deactivate(update:Update,context:ContextTypes.DEFAULT_TYPE):
    global auto_trade_active
    auto_trade_active=False
    persist()
    await update.message.reply_text("⏸️ Auto-trading deactivated.")

if __name__=="__main__":
    app=ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",start))
    app.add_handler(CommandHandler("activate",activate))
    app.add_handler(CommandHandler("deactivate",deactivate))
    app.add_handler(CommandHandler("live",live))
    app.add_handler(CallbackQueryHandler(analyze_callback))
    app.run_polling()
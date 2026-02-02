import aiohttp, asyncio, pandas as pd, ta, datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes

TELEGRAM_TOKEN = "8597020255:AAF20Lvuy1fLBTU7h1CUYOXqOnCyfzLUTFA"
TWELVEDATA_KEY = "ca1acbf0cedb4488b130c59252891c5e"
NEWS_API_KEY = "qDGIzb9o2OttTxWNvBLMDyZD9KbdQ0qaPHvupsjH"

MIN_CANDLES = 120
MIN_ATR_PCT = 0.3
MIN_BACKTEST_WR = 55
SCAN_INTERVAL = 180
TRACK_INTERVAL = 60
NEWS_LIMIT = 3

CRYPTOS = {
    "BTCUSDT":"BTC/USD","ETHUSDT":"ETH/USD","BNBUSDT":"BNB/USD",
    "XRPUSDT":"XRP/USD","SOLUSDT":"SOL/USD","ADAUSDT":"ADA/USD",
    "DOGEUSDT":"DOGE/USD","AVAXUSDT":"AVAX/USD","DOTUSDT":"DOT/USD",
    "MATICUSDT":"MATIC/USD","LTCUSDT":"LTC/USD","LINKUSDT":"LINK/USD",
    "TRXUSDT":"TRX/USD","ATOMUSDT":"ATOM/USD","UNIUSDT":"UNI/USD"
}

active_trades = {}
stats = {"wins":0,"losses":0,"be":0}

def session_ok():
    h = datetime.datetime.utcnow().hour
    return 7 <= h <= 20

async def fetch(session, symbol, interval="15min"):
    try:
        async with session.get(
            "https://api.twelvedata.com/time_series",
            params={"symbol":CRYPTOS[symbol],"interval":interval,"outputsize":500,"apikey":TWELVEDATA_KEY},
            timeout=10
        ) as r:
            j = await r.json()
            if "values" not in j:
                return pd.DataFrame()
            rows=[{"c":float(v["close"]),"h":float(v["high"]),"l":float(v["low"])} for v in reversed(j["values"])]
            return pd.DataFrame(rows)
    except:
        return pd.DataFrame()

def enrich(df):
    if len(df) < MIN_CANDLES:
        return pd.DataFrame()
    df["RSI"]=ta.momentum.RSIIndicator(df["c"],14).rsi()
    df["EMA50"]=ta.trend.EMAIndicator(df["c"],50).ema_indicator()
    df["EMA200"]=ta.trend.EMAIndicator(df["c"],200).ema_indicator()
    df["MACD"]=ta.trend.MACD(df["c"]).macd_diff()
    df["ADX"]=ta.trend.ADXIndicator(df["h"],df["l"],df["c"]).adx()
    df["ATR"]=ta.volatility.AverageTrueRange(df["h"],df["l"],df["c"]).average_true_range()
    return df.dropna()

def backtest_score(df):
    wins=0; losses=0
    for i in range(50,len(df)-5):
        r=df.iloc[i]
        s=0
        s+=2 if r["EMA50"]>r["EMA200"] else -2
        s+=1 if r["RSI"]>55 else -1 if r["RSI"]<45 else 0
        s+=1 if r["MACD"]>0 else -1
        s+=1 if r["ADX"]>20 else 0
        if s>=3:
            if df["c"].iloc[i+3]>r["c"]: wins+=1
            else: losses+=1
        if s<=-3:
            if df["c"].iloc[i+3]<r["c"]: wins+=1
            else: losses+=1
    t=wins+losses
    return (wins/t)*100 if t else 0

async def fetch_news(session, symbol, limit=NEWS_LIMIT):
    try:
        query = symbol.replace("USDT","") + " crypto"
        async with session.get(
            "https://newsapi.org/v2/everything",
            params={"q":query,"pageSize":limit,"sortBy":"publishedAt","apiKey":NEWS_API_KEY},
            timeout=10
        ) as r:
            j = await r.json()
            if "articles" not in j: return []
            return [{"title":a["title"],"url":a["url"]} for a in j["articles"]]
    except:
        return []

def signal(df):
    last=df.iloc[-1]
    atr_pct=(last["ATR"]/last["c"])*100
    if atr_pct < MIN_ATR_PCT:
        return None
    if backtest_score(df) < MIN_BACKTEST_WR:
        return None
    s=0
    s+=2 if last["EMA50"]>last["EMA200"] else -2
    s+=1 if last["RSI"]>55 else -1 if last["RSI"]<45 else 0
    s+=1 if last["MACD"]>0 else -1
    s+=1 if last["ADX"]>20 else 0
    if s>=3: d="BUY"
    elif s<=-3: d="SELL"
    else: return None
    p=last["c"]; atr=last["ATR"]
    return {"dir":d,"entry":p,"sl":p-atr*1.5 if d=="BUY" else p+atr*1.5,"tp":p+atr*3 if d=="BUY" else p-atr*3,"atr":atr,"score":s,"be":False}

async def btc_trend(session):
    df=enrich(await fetch(session,"BTCUSDT"))
    if df.empty: return None
    r=df.iloc[-1]
    return "BUY" if r["EMA50"]>r["EMA200"] else "SELL"

async def multi_tf_signal(session, symbol):
    if not session_ok(): return None
    btc_dir = await btc_trend(session)
    df15 = enrich(await fetch(session,symbol,"15min"))
    df5 = enrich(await fetch(session,symbol,"5min"))
    if df15.empty or df5.empty: return None
    sig15 = signal(df15)
    sig5 = signal(df5)
    if not sig15 or not sig5: return None
    if sig15["dir"]!=sig5["dir"]: return None
    if symbol!="BTCUSDT" and sig15["dir"]!=btc_dir: return None
    return sig15

async def scan(context):
    signals=[]
    async with aiohttp.ClientSession() as s:
        for sym in CRYPTOS:
            if sym in active_trades: continue
            sig = await multi_tf_signal(s,sym)
            if sig: signals.append((sym,sig))
    signals.sort(key=lambda x: abs(x[1]["score"]),reverse=True)
    top3=signals[:3]
    if top3:
        msg="🚀 TOP CRYPTO SIGNALS\n\n"
        async with aiohttp.ClientSession() as s:
            for i,(sym,s) in enumerate(top3,1):
                news = await fetch_news(s,sym)
                msg+=f"{i}. {sym}\nDir: {s['dir']}\nEntry: {round(s['entry'],5)}\nSL: {round(s['sl'],5)}\nTP: {round(s['tp'],5)}\nScore: {s['score']}\n"
                if news:
                    msg+="📰 News:\n"
                    for n in news:
                        msg+=f"{n['title']}\n{n['url']}\n"
                msg+="\n"
        await context.bot.send_message(chat_id=context.job.chat_id,text=msg)
        for sym,s in top3: active_trades[sym]=s

async def track(context):
    async with aiohttp.ClientSession() as s:
        for sym in list(active_trades):
            df=await fetch(s,sym)
            if df.empty: continue
            price=df["c"].iloc[-1]
            t=active_trades[sym]
            if not t["be"]:
                if t["dir"]=="BUY" and price>=t["entry"]+t["atr"]:
                    t["sl"]=t["entry"]; t["be"]=True
                if t["dir"]=="SELL" and price<=t["entry"]-t["atr"]:
                    t["sl"]=t["entry"]; t["be"]=True
            tp = price>=t["tp"] if t["dir"]=="BUY" else price<=t["tp"]
            sl = price<=t["sl"] if t["dir"]=="BUY" else price>=t["sl"]
            if tp or sl:
                if tp: stats["wins"]+=1
                elif t["be"]: stats["be"]+=1
                else: stats["losses"]+=1
                total=stats["wins"]+stats["losses"]
                wr=round((stats["wins"]/total)*100,2) if total else 0
                await context.bot.send_message(
                    chat_id=context.job.chat_id,
                    text=f"{sym} {'TP' if tp else 'SL'} @ {round(price,5)}\nWins {stats['wins']} Loss {stats['losses']} BE {stats['be']}\nWinRate {wr}%"
                )
                del active_trades[sym]

async def start(update:Update,context:ContextTypes.DEFAULT_TYPE):
    kb=[[InlineKeyboardButton(k,callback_data=k)] for k in CRYPTOS]
    await update.message.reply_text("📡 Click crypto to analyze or wait for top signals:",reply_markup=InlineKeyboardMarkup(kb))
    context.job_queue.run_repeating(scan,SCAN_INTERVAL,chat_id=update.effective_chat.id)
    context.job_queue.run_repeating(track,TRACK_INTERVAL,chat_id=update.effective_chat.id)

async def analyze_callback(update:Update,context:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query
    await q.answer()
    sym=q.data
    async with aiohttp.ClientSession() as s:
        sig=await multi_tf_signal(s,sym)
        if not sig:
            await q.edit_message_text(f"⚠️ No clear signal for {sym} right now")
            return
        active_trades[sym]=sig
        news = await fetch_news(s,sym)
        msg=f"{sym} Analysis\nDir: {sig['dir']}\nEntry: {round(sig['entry'],5)}\nSL: {round(sig['sl'],5)}\nTP: {round(sig['tp'],5)}\nScore: {sig['score']}\n"
        if news:
            msg+="📰 News:\n"
            for n in news:
                msg+=f"{n['title']}\n{n['url']}\n"
        await q.edit_message_text(msg)

if __name__=="__main__":
    app=ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",start))
    app.add_handler(CallbackQueryHandler(analyze_callback))
    app.run_polling()
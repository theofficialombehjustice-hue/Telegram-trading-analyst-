import os, time, json, hmac, sqlite3, asyncio, hashlib, math
from urllib.parse import urlencode
from datetime import datetime
import aiohttp
import pandas as pd
import numpy as np
import ta
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes

TELEGRAM_TOKEN="8354975270:AAE-IiNUXCT42UpqH_0Z4tusKMQ8N64EwPE"
TWELVEDATA_KEY="064dbdcd38734f70b71db008e502f5e0"
BYBIT_API_KEY="rVvfdhsRaz2XKBo6Pe"
BYBIT_API_SECRET="pLH8ZMkL328aFxtkA1S9IsmtnaBicVe6phx4"
DB_FILE="state.sqlite3"

DRY_RUN=False
USE_TESTNET=False

MIN_CANDLES=250
SCAN_INTERVAL=90
TRACK_INTERVAL=20
VOLATILITY_THRESHOLD=0.0025
RISK_PER_TRADE_PCT=0.8
LEVERAGE=5
MAX_CONCURRENT_TRADES=8
MAX_DAILY_LOSS_PCT=8.0
COOLDOWN_BASE=1800

CRYPTOS={
"BTCUSDT":"BTC/USD","ETHUSDT":"ETH/USD","BNBUSDT":"BNB/USD","XRPUSDT":"XRP/USD","SOLUSDT":"SOL/USD",
"ADAUSDT":"ADA/USD","DOGEUSDT":"DOGE/USD","AVAXUSDT":"AVAX/USD","DOTUSDT":"DOT/USD","MATICUSDT":"MATIC/USD",
"LTCUSDT":"LTC/USD","LINKUSDT":"LINK/USD","TRXUSDT":"TRX/USD","ATOMUSDT":"ATOM/USD","UNIUSDT":"UNI/USD",
"SHIBUSDT":"SHIB/USD","NEARUSDT":"NEAR/USD","AAVEUSDT":"AAVE/USD","EOSUSDT":"EOS/USD","XLMUSDT":"XLM/USD",
"ALGOUSDT":"ALGO/USD","CHZUSDT":"CHZ/USD","ZILUSDT":"ZIL/USD","ENJUSDT":"ENJ/USD","GRTUSDT":"GRT/USD",
"BATUSDT":"BAT/USD","RVNUSDT":"RVN/USD","1INCHUSDT":"1INCH/USD","ANKRUSDT":"ANKR/USD","ARBUSDT":"ARB/USD",
"OPUSDT":"OP/USD","SANDUSDT":"SAND/USD","MANAUSDT":"MANA/USD","ICPUSDT":"ICP/USD","FILUSDT":"FIL/USD",
"ETCUSDT":"ETC/USD","XTZUSDT":"XTZ/USD","EGLDUSDT":"EGLD/USD","RUNEUSDT":"RUNE/USD","FLOWUSDT":"FLOW/USD",
"GALAUSDT":"GALA/USD","APEUSDT":"APE/USD","CRVUSDT":"CRV/USD","DYDXUSDT":"DYDX/USD","IMXUSDT":"IMX/USD",
"LRCUSDT":"LRC/USD","MASKUSDT":"MASK/USD","KAVAUSDT":"KAVA/USD","KNCUSDT":"KNC/USD","ZRXUSDT":"ZRX/USD",
"ROSEUSDT":"ROSE/USD","ONEUSDT":"ONE/USD","QTUMUSDT":"QTUM/USD","RSRUSDT":"RSR/USD","SNXUSDT":"SNX/USD",
"STMXUSDT":"STMX/USD","STORJUSDT":"STORJ/USD","THETAUSDT":"THETA/USD","TLMUSDT":"TLM/USD","UMAUSDT":"UMA/USD",
"VETUSDT":"VET/USD","WAVESUSDT":"WAVES/USD","XEMUSDT":"XEM/USD","XMRUSDT":"XMR/USD","YFIUSDT":"YFI/USD",
"ZECUSDT":"ZEC/USD","ZENUSDT":"ZEN/USD","COMPUSDT":"COMP/USD","BALUSDT":"BAL/USD","BANDUSDT":"BAND/USD",
"COTIUSDT":"COTI/USD","CTSIUSDT":"CTSI/USD","DASHUSDT":"DASH/USD","ENSUSDT":"ENS/USD","FETUSDT":"FET/USD",
"HNTUSDT":"HNT/USD","IOTXUSDT":"IOTX/USD","JSTUSDT":"JST/USD","MKRUSDT":"MKR/USD","NKNUSDT":"NKN/USD"
}

BYBIT_BASE="https://api.bybit.com"
HTTP=None
DB=None

auto_trade=False
capital=1000.0
daily_start_capital=capital
last_day=datetime.utcnow().date()
learning_weight=1.0
loss_streak=0

def db_init():
    global DB
    DB=sqlite3.connect(DB_FILE,check_same_thread=False)
    c=DB.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS trades(id INTEGER PRIMARY KEY AUTOINCREMENT,symbol TEXT,side TEXT,qty REAL,entry REAL,sl REAL,tp REAL,status TEXT,created INTEGER,closed INTEGER,pnl REAL)")
    c.execute("CREATE TABLE IF NOT EXISTS cooldown(symbol TEXT PRIMARY KEY,until_ts INTEGER)")
    DB.commit()

def sign(params):
    s=urlencode(sorted(params.items()))
    return hmac.new(BYBIT_API_SECRET.encode(),s.encode(),hashlib.sha256).hexdigest()

async def bybit_order(symbol,side,qty,tp,sl):
    if DRY_RUN:
        return True,str(int(time.time()*1000))
    ts=int(time.time()*1000)
    p={"api_key":BYBIT_API_KEY,"symbol":symbol,"side":side,"order_type":"Market","qty":qty,"time_in_force":"GoodTillCancel","timestamp":ts,"take_profit":tp,"stop_loss":sl}
    p["sign"]=sign(p)
    async with HTTP.post(BYBIT_BASE+"/private/linear/order/create",data=p) as r:
        j=await r.json()
        return j.get("ret_code")==0,j.get("result",{}).get("order_id")

async def fetch(symbol,interval,limit):
    async with HTTP.get("https://api.twelvedata.com/time_series",params={"symbol":CRYPTOS[symbol],"interval":interval,"outputsize":limit,"apikey":TWELVEDATA_KEY}) as r:
        j=await r.json()
        if "values" not in j:
            return pd.DataFrame()
        rows=[{"c":float(v["close"]),"h":float(v["high"]),"l":float(v["low"]),"v":float(v.get("volume",0))} for v in reversed(j["values"])]
        return pd.DataFrame(rows)

def enrich(df):
    if len(df)<MIN_CANDLES:
        return pd.DataFrame()
    df=df.copy()
    df["EMA50"]=ta.trend.EMAIndicator(df["c"],50).ema_indicator()
    df["EMA200"]=ta.trend.EMAIndicator(df["c"],200).ema_indicator()
    df["MACD"]=ta.trend.MACD(df["c"]).macd_diff()
    df["RSI"]=ta.momentum.RSIIndicator(df["c"],14).rsi()
    df["ADX"]=ta.trend.ADXIndicator(df["h"],df["l"],df["c"]).adx()
    df["ATR"]=ta.volatility.AverageTrueRange(df["h"],df["l"],df["c"]).average_true_range()
    df["VWAP"]=ta.volume.VolumeWeightedAveragePrice(df["h"],df["l"],df["c"],df["v"]).volume_weighted_average_price()
    return df.dropna()

def signal(df,w):
    r=df.iloc[-1]
    if r["ATR"]/r["c"]<VOLATILITY_THRESHOLD:
        return None
    s=0
    if r["EMA50"]>r["EMA200"]:
        s+=3
        side="Buy"
        s+=1 if r["MACD"]>0 else -1
        s+=1 if r["RSI"]>55 else -1 if r["RSI"]<45 else 0
        if r["c"]<r["VWAP"]+0.2*r["ATR"]:
            return None
    else:
        s+=3
        side="Sell"
        s+=1 if r["MACD"]<0 else -1
        s+=1 if r["RSI"]<45 else -1 if r["RSI"]>55 else 0
        if r["c"]>r["VWAP"]-0.2*r["ATR"]:
            return None
    s+=1 if r["ADX"]>25 else 0
    s*=w
    if s<6:
        return None
    p=r["c"]
    a=r["ATR"]
    tp_mult=1.8 if a/p>0.03 else 2
    return {"side":side,"entry":p,"sl":p-a,"tp":p+a*tp_mult,"score":s}

def qty_for(entry):
    risk=capital*(RISK_PER_TRADE_PCT/100)
    return round((risk*LEVERAGE)/entry,6)

async def scan(ctx):
    global auto_trade
    if not auto_trade:
        return
    c=DB.cursor()
    if c.execute("SELECT COUNT(*) FROM trades WHERE status='OPEN'").fetchone()[0]>=MAX_CONCURRENT_TRADES:
        return
    found=[]
    for s in CRYPTOS:
        cd=c.execute("SELECT until_ts FROM cooldown WHERE symbol=?",(s,)).fetchone()
        if cd and cd[0]>time.time():
            continue
        df=enrich(await fetch(s,"1h",500))
        if df.empty:
            continue
        sig=signal(df,learning_weight)
        if sig:
            found.append((s,sig))
        await asyncio.sleep(0.15)
    found=sorted(found,key=lambda x:x[1]["score"],reverse=True)[:3]
    for s,sig in found:
        q=qty_for(sig["entry"])
        ok,_=await bybit_order(s,sig["side"],q,sig["tp"],sig["sl"])
        if ok:
            DB.execute("INSERT INTO trades(symbol,side,qty,entry,sl,tp,status,created) VALUES(?,?,?,?,?,?,?,?)",(s,sig["side"],q,sig["entry"],sig["sl"],sig["tp"],"OPEN",int(time.time())))
            DB.commit()
            await ctx.bot.send_message(chat_id=ctx.job.chat_id,text=f"{s} {sig['side']} {q}")

async def track(ctx):
    global capital,loss_streak,auto_trade,last_day,daily_start_capital
    if not auto_trade:
        return
    if datetime.utcnow().date()!=last_day:
        last_day=datetime.utcnow().date()
        daily_start_capital=capital
    c=DB.cursor()
    for t in c.execute("SELECT id,symbol,qty,entry,sl,tp FROM trades WHERE status='OPEN'").fetchall():
        df=enrich(await fetch(t[1],"1m",80))
        if df.empty:
            continue
        p=df.iloc[-1]["c"]
        if (t[3]<t[5] and p>=t[5]) or (t[3]>t[5] and p<=t[5]) or p<=t[4] or p>=t[4]:
            pnl=t[2]*(p-t[3]) if t[3]<t[5] else t[2]*(t[3]-p)
            capital+=pnl
            DB.execute("UPDATE trades SET status='CLOSED',closed=?,pnl=? WHERE id=?",(int(time.time()),pnl,t[0]))
            if pnl<0:
                loss_streak+=1
                DB.execute("INSERT OR REPLACE INTO cooldown VALUES(?,?)",(t[1],int(time.time()+COOLDOWN_BASE*(1+loss_streak))))
            else:
                loss_streak=max(0,loss_streak-1)
            DB.commit()
            await ctx.bot.send_message(chat_id=ctx.job.chat_id,text=f"{t[1]} PnL {pnl:.2f}")
        if (daily_start_capital-capital)/daily_start_capital*100>=MAX_DAILY_LOSS_PCT:
            auto_trade=False
            await ctx.bot.send_message(chat_id=ctx.job.chat_id,text="HALTED")

async def backtest_cmd(update,ctx):
    sym=ctx.args[0] if ctx.args else None
    if sym not in CRYPTOS:
        await update.message.reply_text("BAD SYMBOL")
        return
    df=enrich(await fetch(sym,"1h",2000))
    wins=losses=0
    pnl=0
    for i in range(200,len(df)-1):
        d=df.iloc[:i]
        sig=signal(d,1)
        if not sig:
            continue
        e=df.iloc[i+1]["c"]
        if (sig["side"]=="Buy" and df.iloc[i+1]["h"]>=sig["tp"]) or (sig["side"]=="Sell" and df.iloc[i+1]["l"]<=sig["tp"]):
            wins+=1
            pnl+=(sig["tp"]-e) if sig["side"]=="Buy" else (e-sig["tp"])
        elif (sig["side"]=="Buy" and df.iloc[i+1]["l"]<=sig["sl"]) or (sig["side"]=="Sell" and df.iloc[i+1]["h"]>=sig["sl"]):
            losses+=1
            pnl+=(sig["sl"]-e) if sig["side"]=="Buy" else (e-sig["sl"])
    await update.message.reply_text(f"{sym} W:{wins} L:{losses} PnL:{pnl:.2f}")

async def start(update,ctx):
    kb=[[InlineKeyboardButton(k,callback_data=k)] for k in list(CRYPTOS)[:25]]
    await update.message.reply_text("READY",reply_markup=InlineKeyboardMarkup(kb))
    ctx.job_queue.run_repeating(scan,SCAN_INTERVAL,chat_id=update.effective_chat.id)
    ctx.job_queue.run_repeating(track,TRACK_INTERVAL,chat_id=update.effective_chat.id)

async def activate(update,ctx):
    global auto_trade
    auto_trade=True
    await update.message.reply_text("LIVE")

async def deactivate(update,ctx):
    global auto_trade
    auto_trade=False
    await update.message.reply_text("STOPPED")

async def analyze(update,ctx):
    q=update.callback_query
    await q.answer()
    df=enrich(await fetch(q.data,"1h",500))
    sig=signal(df,learning_weight) if not df.empty else None
    await q.edit_message_text(json.dumps(sig) if sig else "NO SIGNAL")

async def main():
    global HTTP
    db_init()
    HTTP=aiohttp.ClientSession()
    app=ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",start))
    app.add_handler(CommandHandler("activate",activate))
    app.add_handler(CommandHandler("deactivate",deactivate))
    app.add_handler(CommandHandler("backtest",backtest_cmd))
    app.add_handler(CallbackQueryHandler(analyze))
    await app.initialize()
    await app.start()
    await app.bot.initialize()
    await app.updater.start_polling()
    await asyncio.Event().wait()

if __name__=="__main__":
    asyncio.run(main())
import asyncio
import logging
import os

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from kucoin_futures import KuCoinFuturesClient
from position_manager import PositionManager

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

app = FastAPI(title="TradingView → KuCoin Bot")

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

kucoin = KuCoinFuturesClient(
    api_key=os.getenv("KUCOIN_API_KEY", ""),
    api_secret=os.getenv("KUCOIN_API_SECRET", ""),
    api_passphrase=os.getenv("KUCOIN_API_PASSPHRASE", ""),
)
pm = PositionManager(kucoin)


@app.get("/health")
async def health():
    return {"status": "ok", "positions": list(pm.positions.keys())}


@app.post("/webhook")
async def webhook(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    # Validate shared secret if configured
    if WEBHOOK_SECRET and body.get("secret") != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")

    action = body.get("action", "").lower()
    symbol = body.get("symbol", os.getenv("SYMBOL", "XBTUSDTM"))

    logger.info(f"Webhook received: action={action} symbol={symbol}")

    leverage = body.get("leverage")

    if action == "long_entry":
        await pm.open_long(symbol, leverage=leverage)
    elif action == "short_entry":
        await pm.open_short(symbol, leverage=leverage)
    elif action == "long_exit":
        await pm.close_long(symbol, reason="hist_flip")
    elif action == "short_exit":
        await pm.close_short(symbol, reason="hist_flip")
    else:
        logger.warning(f"Unknown action: {action}")
        return JSONResponse({"status": "ignored", "reason": "unknown action"})

    return JSONResponse({"status": "ok", "action": action, "symbol": symbol})


@app.on_event("startup")
async def startup():
    logger.info("Bot server starting — launching position monitor")
    asyncio.create_task(pm.monitor_loop())

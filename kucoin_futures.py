import base64
import hashlib
import hmac
import json
import logging
import time

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api-futures.kucoin.com"


class KuCoinFuturesClient:
    def __init__(self, api_key: str, api_secret: str, api_passphrase: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.api_passphrase = api_passphrase

    # ── Auth ──────────────────────────────────────────────────────────────────

    def _sign(self, timestamp: str, method: str, endpoint: str, body: str = "") -> tuple[str, str]:
        message = f"{timestamp}{method}{endpoint}{body}"
        signature = base64.b64encode(
            hmac.new(
                self.api_secret.encode("utf-8"),
                message.encode("utf-8"),
                hashlib.sha256,
            ).digest()
        ).decode("utf-8")

        passphrase = base64.b64encode(
            hmac.new(
                self.api_secret.encode("utf-8"),
                self.api_passphrase.encode("utf-8"),
                hashlib.sha256,
            ).digest()
        ).decode("utf-8")

        return signature, passphrase

    def _headers(self, method: str, endpoint: str, body: str = "") -> dict:
        timestamp = str(int(time.time() * 1000))
        signature, passphrase = self._sign(timestamp, method, endpoint, body)
        return {
            "KC-API-KEY": self.api_key,
            "KC-API-SIGN": signature,
            "KC-API-TIMESTAMP": timestamp,
            "KC-API-PASSPHRASE": passphrase,
            "KC-API-KEY-VERSION": "2",
            "Content-Type": "application/json",
        }

    # ── HTTP helpers ──────────────────────────────────────────────────────────

    def _post(self, endpoint: str, data: dict) -> dict:
        body = json.dumps(data)
        headers = self._headers("POST", endpoint, body)
        resp = requests.post(BASE_URL + endpoint, headers=headers, data=body, timeout=10)
        result = resp.json()
        if result.get("code") != "200000":
            raise Exception(f"KuCoin error [{result.get('code')}]: {result.get('msg')}")
        return result.get("data", {})

    def _delete(self, endpoint: str) -> dict:
        headers = self._headers("DELETE", endpoint)
        resp = requests.delete(BASE_URL + endpoint, headers=headers, timeout=10)
        return resp.json()

    def _get(self, endpoint: str) -> dict | None:
        headers = self._headers("GET", endpoint)
        resp = requests.get(BASE_URL + endpoint, headers=headers, timeout=10)
        result = resp.json()
        if result.get("code") != "200000":
            logger.error(f"KuCoin GET error: {result}")
            return None
        return result.get("data")

    # ── Market data ───────────────────────────────────────────────────────────

    def get_mark_price(self, symbol: str) -> float | None:
        data = self._get(f"/api/v1/mark-price/{symbol}/current")
        if data:
            return float(data["value"])
        return None

    def get_position(self, symbol: str) -> dict | None:
        return self._get(f"/api/v1/position?symbol={symbol}")

    # ── Orders ────────────────────────────────────────────────────────────────

    def place_market_order(
        self, symbol: str, side: str, size: int, leverage: int, reduce_only: bool = False
    ) -> dict:
        """Market entry or close order. side = 'buy' | 'sell'."""
        data = {
            "clientOid": str(int(time.time() * 1000)),
            "symbol": symbol,
            "side": side,
            "type": "market",
            "size": size,
            "leverage": str(leverage),
            "reduceOnly": reduce_only,
        }
        logger.info(f"Placing market order: {data}")
        return self._post("/api/v1/orders", data)

    def place_stop_market_order(
        self,
        symbol: str,
        side: str,
        size: int,
        stop_price: float,
        stop_direction: str,
        leverage: int,
        reduce_only: bool = True,
    ) -> dict:
        """Stop market order. stop_direction = 'down' (long SL) | 'up' (short SL)."""
        data = {
            "clientOid": str(int(time.time() * 1000)),
            "symbol": symbol,
            "side": side,
            "type": "market",
            "size": size,
            "leverage": str(leverage),
            "stop": stop_direction,
            "stopPrice": str(round(stop_price, 1)),
            "stopPriceType": "MP",  # Mark price
            "reduceOnly": reduce_only,
        }
        logger.info(f"Placing stop order: {data}")
        return self._post("/api/v1/orders", data)

    def cancel_order(self, order_id: str) -> dict:
        logger.info(f"Cancelling order: {order_id}")
        return self._delete(f"/api/v1/orders/{order_id}")

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Optional

from kucoin_futures import KuCoinFuturesClient

logger = logging.getLogger(__name__)


@dataclass
class Position:
    symbol: str
    direction: str          # "long" or "short"
    entry_price: float
    size: int               # total contracts opened
    leverage: int
    stop_order_id: Optional[str] = None
    trail_extreme: float = 0.0  # highest price seen (long) / lowest (short)
    trail_fired: bool = False   # True once 80% has been closed
    be_order_id: Optional[str] = None


class PositionManager:
    def __init__(self, kucoin: KuCoinFuturesClient):
        self.kucoin = kucoin
        self.positions: dict[str, Position] = {}

        self.sl_pct        = float(os.getenv("SL_PCT",        "4.0"))
        self.trail_pct     = float(os.getenv("TRAIL_PCT",     "7.0"))
        self.trail_qty_pct = float(os.getenv("TRAIL_QTY_PCT", "80.0"))
        self.position_usd  = float(os.getenv("POSITION_SIZE_USD", "1000"))
        self.leverage      = int(os.getenv("LEVERAGE", "1"))

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _contracts(self, price: float) -> int:
        """Convert USD position size + leverage to number of contracts.
        XBTUSDTM: 1 contract = 0.001 BTC, so 1 contract ≈ price * 0.001 USDT.
        """
        notional = self.position_usd * self.leverage
        contracts = int(notional / (price * 0.001))
        return max(1, contracts)

    # ── Entry ─────────────────────────────────────────────────────────────────

    async def open_long(self, symbol: str):
        if symbol in self.positions:
            logger.warning(f"Already in {symbol} position — long entry ignored")
            return

        price = self.kucoin.get_mark_price(symbol)
        if not price:
            logger.error(f"Cannot get mark price for {symbol}")
            return

        size     = self._contracts(price)
        sl_price = round(price * (1 - self.sl_pct / 100), 1)

        logger.info(f"LONG {symbol} | size={size} entry≈{price} SL={sl_price} lev={self.leverage}x")

        self.kucoin.place_market_order(symbol, "buy", size, self.leverage)
        sl = self.kucoin.place_stop_market_order(symbol, "sell", size, sl_price, "down", self.leverage)

        self.positions[symbol] = Position(
            symbol=symbol,
            direction="long",
            entry_price=price,
            size=size,
            leverage=self.leverage,
            stop_order_id=sl.get("orderId"),
            trail_extreme=price,
        )
        logger.info(f"LONG open. SL order id: {sl.get('orderId')}")

    async def open_short(self, symbol: str):
        if symbol in self.positions:
            logger.warning(f"Already in {symbol} position — short entry ignored")
            return

        price = self.kucoin.get_mark_price(symbol)
        if not price:
            logger.error(f"Cannot get mark price for {symbol}")
            return

        size     = self._contracts(price)
        sl_price = round(price * (1 + self.sl_pct / 100), 1)

        logger.info(f"SHORT {symbol} | size={size} entry≈{price} SL={sl_price} lev={self.leverage}x")

        self.kucoin.place_market_order(symbol, "sell", size, self.leverage)
        sl = self.kucoin.place_stop_market_order(symbol, "buy", size, sl_price, "up", self.leverage)

        self.positions[symbol] = Position(
            symbol=symbol,
            direction="short",
            entry_price=price,
            size=size,
            leverage=self.leverage,
            stop_order_id=sl.get("orderId"),
            trail_extreme=price,
        )
        logger.info(f"SHORT open. SL order id: {sl.get('orderId')}")

    # ── Exit ──────────────────────────────────────────────────────────────────

    async def close_long(self, symbol: str, reason: str = "signal"):
        pos = self.positions.get(symbol)
        if not pos or pos.direction != "long":
            logger.warning(f"No long position on {symbol} to close")
            return

        self._cancel_orders(pos)

        pos_data   = self.kucoin.get_position(symbol)
        remaining  = int(pos_data.get("currentQty", 0)) if pos_data else pos.size

        if remaining > 0:
            self.kucoin.place_market_order(symbol, "sell", remaining, pos.leverage, reduce_only=True)
            logger.info(f"LONG closed ({reason}): {remaining} contracts")

        del self.positions[symbol]

    async def close_short(self, symbol: str, reason: str = "signal"):
        pos = self.positions.get(symbol)
        if not pos or pos.direction != "short":
            logger.warning(f"No short position on {symbol} to close")
            return

        self._cancel_orders(pos)

        pos_data   = self.kucoin.get_position(symbol)
        remaining  = abs(int(pos_data.get("currentQty", 0))) if pos_data else pos.size

        if remaining > 0:
            self.kucoin.place_market_order(symbol, "buy", remaining, pos.leverage, reduce_only=True)
            logger.info(f"SHORT closed ({reason}): {remaining} contracts")

        del self.positions[symbol]

    def _cancel_orders(self, pos: Position):
        for oid in [pos.stop_order_id, pos.be_order_id]:
            if oid:
                try:
                    self.kucoin.cancel_order(oid)
                except Exception as e:
                    logger.warning(f"Could not cancel order {oid}: {e}")
        pos.stop_order_id = None
        pos.be_order_id   = None

    # ── Trailing stop ─────────────────────────────────────────────────────────

    def _check_trail(self, pos: Position, price: float):
        if pos.trail_fired:
            return

        trail_size = max(1, int(pos.size * (self.trail_qty_pct / 100)))

        if pos.direction == "long":
            if price > pos.trail_extreme:
                pos.trail_extreme = price

            trigger = pos.trail_extreme * (1 - self.trail_pct / 100)
            if price <= trigger:
                logger.info(
                    f"Trailing stop LONG {pos.symbol}: price={price} "
                    f"high={pos.trail_extreme} trigger={trigger:.1f}"
                )
                self.kucoin.place_market_order(
                    pos.symbol, "sell", trail_size, pos.leverage, reduce_only=True
                )
                pos.trail_fired = True
                self._move_stop_to_breakeven(pos, trail_size)

        elif pos.direction == "short":
            if price < pos.trail_extreme:
                pos.trail_extreme = price

            trigger = pos.trail_extreme * (1 + self.trail_pct / 100)
            if price >= trigger:
                logger.info(
                    f"Trailing stop SHORT {pos.symbol}: price={price} "
                    f"low={pos.trail_extreme} trigger={trigger:.1f}"
                )
                self.kucoin.place_market_order(
                    pos.symbol, "buy", trail_size, pos.leverage, reduce_only=True
                )
                pos.trail_fired = True
                self._move_stop_to_breakeven(pos, trail_size)

    def _move_stop_to_breakeven(self, pos: Position, closed_size: int):
        # Cancel existing stop loss
        if pos.stop_order_id:
            try:
                self.kucoin.cancel_order(pos.stop_order_id)
            except Exception as e:
                logger.warning(f"Could not cancel SL {pos.stop_order_id}: {e}")
            pos.stop_order_id = None

        remaining = pos.size - closed_size
        if remaining <= 0:
            return

        # Place breakeven stop for remaining position
        try:
            if pos.direction == "long":
                be = self.kucoin.place_stop_market_order(
                    pos.symbol, "sell", remaining, pos.entry_price, "down", pos.leverage
                )
            else:
                be = self.kucoin.place_stop_market_order(
                    pos.symbol, "buy", remaining, pos.entry_price, "up", pos.leverage
                )
            pos.be_order_id = be.get("orderId")
            logger.info(f"Breakeven stop set at {pos.entry_price} (order {pos.be_order_id})")
        except Exception as e:
            logger.error(f"Failed to place breakeven stop: {e}")

    # ── Monitor loop ──────────────────────────────────────────────────────────

    async def monitor_loop(self):
        logger.info("Position monitor running (5s interval)")
        while True:
            for symbol, pos in list(self.positions.items()):
                try:
                    price = self.kucoin.get_mark_price(symbol)
                    if not price:
                        continue

                    self._check_trail(pos, price)

                    # Detect if stop loss was hit externally (position gone)
                    pos_data = self.kucoin.get_position(symbol)
                    if pos_data and pos_data.get("currentQty", 0) == 0:
                        logger.info(f"Position {symbol} closed externally (SL or liquidation)")
                        del self.positions[symbol]

                except Exception as e:
                    logger.error(f"Monitor error [{symbol}]: {e}")

            await asyncio.sleep(5)

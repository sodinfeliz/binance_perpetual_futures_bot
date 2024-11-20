from typing import Literal, Optional

import numpy as np

from .client import BinanceClient


class GridBot:

    def __init__(
        self,
        base_asset: str,
        quote_asset: str,
        grid_number: int,
        entry_price: float,
        lower_price: float,
        upper_price: float,
        qty_per_order: float,
    ):
        self.base_asset = base_asset
        self.quote_asset = quote_asset
        self.grid_number = grid_number
        self.entry_price = entry_price
        self.lower_price = lower_price
        self.upper_price = upper_price
        self.qty_per_order = qty_per_order

        self._generate_grid_levels()

    @property
    def grid_levels(self) -> np.ndarray:
        return self._grid_levels

    def _generate_grid_levels(self):
        tick_size = BinanceClient.get_tick_size(self.base_asset, self.quote_asset)
        self.grid_interval = (self.upper_price - self.lower_price) / self.grid_number
        
        self._grid_levels = []
        for i in range(self.grid_number):
            price = self.lower_price + i * self.grid_interval
            price = (price // tick_size) * tick_size  # round to the nearest tick size
            self._grid_levels.append(price)

        self._grid_levels.append(self.upper_price)
        self._grid_levels = np.array(self._grid_levels)

    def order_count(self, price: float, align: bool = False) -> tuple[int, int]:
        if align:
            price = self.closest_grid_level(price)
        buy_count = (self._grid_levels < price).sum()
        sell_count = (self._grid_levels > price).sum()
        return int(buy_count), int(sell_count)
    
    def closest_grid_level(self, price: float) -> float:
        return float(self._grid_levels[np.argmin(np.abs(self._grid_levels - price))])


class FuturesGridBot(GridBot):

    _MAINTENANCE_MARGIN_RATE = 0.004  # ETHUSDT Perp, Notional Value < 50000 USDT
    
    def __init__(
        self,
        base_asset: str,
        quote_asset: str,
        grid_number: int,
        entry_price: float,
        lower_price: float,
        upper_price: float,
        qty_per_order: float,
        leverage: int = 1,
        strategy: Literal["long", "short", "neutral"] = "long",
    ):
        super().__init__(base_asset, quote_asset, grid_number, entry_price, lower_price, upper_price, qty_per_order)
        self.leverage = leverage
        self.strategy = strategy

    def set_leverage(self, leverage: int):
        if leverage < 1 or leverage > 125:
            raise ValueError("Leverage must be between 1 and 125")
        self.leverage = leverage

    def set_maintenance_margin_rate(self, rate: float):
        if rate < 0:
            raise ValueError("Maintenance margin rate must be non-negative")
        self._MAINTENANCE_MARGIN_RATE = rate

    def initial_position_size(self, price: Optional[float] = None) -> float:
        if price is None:
            price = self.entry_price

        if self.strategy == "long":
            _, sell_count = self.order_count(price, align=False)
            return self.qty_per_order * (sell_count - 1)
        elif self.strategy == "short":
            buy_count, _ = self.order_count(price, align=False)
            return self.qty_per_order * (buy_count - 1)
        elif self.strategy == "neutral":
            return 0.0  # no initial position size for neutral strategy
        else:
            raise ValueError("Invalid strategy")
        
    def initial_margin_required(self, price: Optional[float] = None) -> float:
        if price is None:
            price = self.entry_price
        return self.initial_position_size(price) * price / self.leverage
    
    def liquidation_price(self, invested_amount: float, price: Optional[float] = None) -> float:

        total_position_size, total_qty = 0.0, 0.0
        unrealized_pnl = 0.0

        for i in range(self.grid_number):
            grid_price = self.grid_levels[i]
            if grid_price >= price:
                total_position_size += self.qty_per_order * grid_price
                total_qty += self.qty_per_order
                unrealized_pnl -= self.qty_per_order * (grid_price - price)

        maintenance_margin = total_position_size * self._MAINTENANCE_MARGIN_RATE
        cross_margin = invested_amount + unrealized_pnl
        multiplier = 1 - 1 / self.leverage

        return float(self.entry_price - (cross_margin - maintenance_margin) / (total_qty * multiplier))
    
    def best_deleverage_price(self, invested_amount: float) -> float:
        """Calculate the best price to deleverage at to avoid liquidation
        
        Since the cross margin and maintenance margin would be affected by the unrealized PnL,
        and the liquidation price provided by Binance is calculated based on the current price,
        we need to iterate through the prices from the highest to the lowest to find the best
        price to deleverage at to avoid liquidation.
        """

        tick_size = BinanceClient.get_tick_size(self.base_asset, self.quote_asset)
        price = self.grid_levels[-2]

        while price >= self.lower_price:
            if self.liquidation_price(invested_amount, price) < price:
                price -= tick_size
                continue
            else:
                break

        return float(price)
"""策略插件输出字段回归测试。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.strategies.base import BaseBacktestParams
from app.strategies.registry import STRATEGIES


EXPECTED_OVERLAYS = {
    "bollinger_reversion": {"bb_upper", "bb_middle", "bb_lower"},
    "donchian_breakout": {"donchian_high", "donchian_low"},
    "ema_crossover": {"fast_ma", "slow_ma"},
    "ma_crossover": {"fast_ma", "slow_ma"},
    "macd": {"macd", "macd_signal", "macd_hist"},
    "rsi_reversal": {"rsi"},
    "stochastic_cross": {"stoch_k", "stoch_d"},
    "volume_ma_pulse": {"volume_ma", "vol_ratio", "high_th", "low_th"},
}


def _sample_ohlcv(rows: int = 80) -> pd.DataFrame:
    x = np.arange(rows, dtype=float)
    close = 20.0 + np.sin(x / 3.0) * 2.0 + x * 0.03
    open_ = close + np.cos(x / 5.0) * 0.2
    high = np.maximum(open_, close) + 0.4
    low = np.minimum(open_, close) - 0.4
    volume = 100000.0 + (x % 11) * 5000.0
    return pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        },
        index=pd.date_range("2024-01-01", periods=rows, freq="D"),
    )


def test_registered_strategies_emit_expected_price_overlays():
    df = _sample_ohlcv()
    base = BaseBacktestParams(initial_cash=100000.0, commission=0.0003)

    for strategy_id, expected_keys in EXPECTED_OVERLAYS.items():
        spec = STRATEGIES[strategy_id]
        params = spec.params_model()
        result = spec.run(df, base, params)

        assert len(result.equity) == len(df)
        assert len(result.price) == len(df)
        assert {"initial_cash", "final_equity", "num_trades"}.issubset(result.metrics)
        assert expected_keys.issubset(result.price[0])

"""Pure pandas/numpy technical indicators — no paid data/indicator API needed."""
import pandas as pd
import numpy as np


def to_df(candles):
    df = pd.DataFrame(candles)
    return df


def rsi(closes: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI."""
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50)


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def macd(closes: pd.Series, fast=12, slow=26, signal=9):
    macd_line = ema(closes, fast) - ema(closes, slow)
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def stoch_rsi(closes: pd.Series, period: int = 14, smooth_k: int = 3, smooth_d: int = 3):
    """Stochastic RSI — more sensitive than plain RSI for catching short-term
    oversold reversals on the fast timeframes (1h/15m entry timing)."""
    r = rsi(closes, period)
    lo = r.rolling(period).min()
    hi = r.rolling(period).max()
    raw_k = ((r - lo) / (hi - lo).replace(0, np.nan) * 100).fillna(50)
    k = raw_k.rolling(smooth_k).mean().fillna(raw_k)
    d = k.rolling(smooth_d).mean().fillna(k)
    return k, d


def bollinger_bands(closes: pd.Series, period: int = 20, num_std: float = 2.0):
    """Returns (upper, mid, lower, percent_b). percent_b near 0 = price at
    the lower band (oversold/reversal zone), near 1 = at the upper band."""
    mid = closes.rolling(period).mean()
    std = closes.rolling(period).std()
    upper = mid + num_std * std
    lower = mid - num_std * std
    band_width = (upper - lower).replace(0, np.nan)
    percent_b = ((closes - lower) / band_width).fillna(0.5)
    return upper, mid, lower, percent_b


def swing_low(df: pd.DataFrame, lookback: int = 20) -> float:
    return float(df["low"].tail(lookback).min())


def swing_high(df: pd.DataFrame, lookback: int = 20) -> float:
    return float(df["high"].tail(lookback).max())


def obv(df: pd.DataFrame) -> pd.Series:
    """On-Balance Volume — running total of volume signed by price direction.
    Rising OBV while price is flat = buying pressure building quietly, often
    ahead of a breakout ('accumulation')."""
    close = df["close"]
    vol = df["volume"]
    direction = np.sign(close.diff().fillna(0))
    return (direction * vol).cumsum()


def bb_width_percentile(closes: pd.Series, period: int = 20, lookback: int = 60) -> float:
    """Where does the CURRENT Bollinger Band width rank vs the last `lookback`
    periods (0=tightest ever seen, 1=widest)? A very low value = a volatility
    squeeze — price has coiled tight, often the setup right before a big move
    (direction not guaranteed — squeezes resolve either way)."""
    _, mid, _, _ = bollinger_bands(closes, period)
    std = closes.rolling(period).std()
    width = (4 * std / mid.replace(0, np.nan)).dropna()
    if len(width) < 10:
        return 0.5
    recent = width.tail(lookback)
    current = width.iloc[-1]
    return float((recent < current).mean())

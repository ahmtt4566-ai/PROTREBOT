from __future__ import annotations

from statistics import fmean, pstdev


def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2 / (period + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append(value * alpha + result[-1] * (1 - alpha))
    return result


def rsi(values: list[float], period: int = 14) -> float:
    changes = [b - a for a, b in zip(values, values[1:])]
    if len(changes) < period:
        return 50.0
    gains = [max(change, 0) for change in changes]
    losses = [max(-change, 0) for change in changes]
    avg_gain = fmean(gains[:period])
    avg_loss = fmean(losses[:period])
    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0
    return 100 - 100 / (1 + avg_gain / avg_loss)


def atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float:
    ranges = [
        max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        for i in range(1, len(closes))
    ]
    if not ranges:
        return 0.0
    value = fmean(ranges[:period])
    for item in ranges[period:]:
        value = (value * (period - 1) + item) / period
    return value


def adx(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float:
    if len(closes) < period * 2 + 1:
        return 0.0
    tr, plus_dm, minus_dm = [], [], []
    for i in range(1, len(closes)):
        tr.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
        up, down = highs[i] - highs[i - 1], lows[i - 1] - lows[i]
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
    smooth_tr, smooth_plus, smooth_minus = sum(tr[:period]), sum(plus_dm[:period]), sum(minus_dm[:period])
    dx_values = []
    for i in range(period, len(tr)):
        smooth_tr = smooth_tr - smooth_tr / period + tr[i]
        smooth_plus = smooth_plus - smooth_plus / period + plus_dm[i]
        smooth_minus = smooth_minus - smooth_minus / period + minus_dm[i]
        if smooth_tr == 0:
            continue
        plus_di, minus_di = 100 * smooth_plus / smooth_tr, 100 * smooth_minus / smooth_tr
        total = plus_di + minus_di
        dx_values.append(100 * abs(plus_di - minus_di) / total if total else 0)
    return fmean(dx_values[-period:]) if dx_values else 0.0


def analyze(candles: list[dict], *, observation_scores: dict[str, int] | None = None) -> dict:
    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    volumes = [c["volume"] for c in candles]
    times = [c["time"] for c in candles]
    e20, e50, e200 = ema(closes, 20), ema(closes, 50), ema(closes, 200)
    fast, slow = ema(closes, 12), ema(closes, 26)
    macd_line = [a - b for a, b in zip(fast, slow)]
    signal_line = ema(macd_line, 9)
    macd_hist = macd_line[-1] - signal_line[-1]
    rsi_value = rsi(closes)
    atr_value = atr(highs, lows, closes)
    adx_value = adx(highs, lows, closes)
    window = closes[-20:]
    bb_mid = fmean(window)
    deviation = pstdev(window)
    bb_upper, bb_lower = bb_mid + 2 * deviation, bb_mid - 2 * deviation
    volume_avg = fmean(volumes[-21:-1]) if len(volumes) > 21 else fmean(volumes)
    volume_ratio = volumes[-1] / volume_avg if volume_avg else 1.0
    lookback = min(80, len(closes))
    support, resistance = min(lows[-lookback:]), max(highs[-lookback:])
    price = closes[-1]

    long_score = 25
    short_score = 25
    for condition, points in (
        (price > e20[-1], 10), (e20[-1] > e50[-1], 12), (e50[-1] > e200[-1], 14),
        (macd_hist > 0, 10), (52 <= rsi_value <= 72, 9), (price > bb_mid, 6),
        (adx_value >= 20 and e20[-1] > e50[-1], 8), (volume_ratio >= 1.05, 6),
    ):
        if condition:
            long_score += points
    for condition, points in (
        (price < e20[-1], 10), (e20[-1] < e50[-1], 12), (e50[-1] < e200[-1], 14),
        (macd_hist < 0, 10), (28 <= rsi_value <= 48, 9), (price < bb_mid, 6),
        (adx_value >= 20 and e20[-1] < e50[-1], 8), (volume_ratio >= 1.05, 6),
    ):
        if condition:
            short_score += points

    difference = long_score - short_score
    if observation_scores is not None:
        observation_scores.update(long_score=long_score, short_score=short_score)
    direction = "LONG" if difference >= 10 else "SHORT" if difference <= -10 else "BEKLE"
    confidence = min(95, max(long_score, short_score)) if direction != "BEKLE" else min(69, 50 + abs(difference))
    if direction == "LONG":
        stop = min(price - 1.5 * atr_value, support - 0.15 * atr_value)
        risk = max(price - stop, atr_value)
        targets = [price + risk * ratio for ratio in (1, 2, 3)]
    elif direction == "SHORT":
        stop = max(price + 1.5 * atr_value, resistance + 0.15 * atr_value)
        risk = max(stop - price, atr_value)
        targets = [price - risk * ratio for ratio in (1, 2, 3)]
    else:
        stop, risk = price - atr_value, atr_value
        targets = [price + atr_value * ratio for ratio in (1, 2, 3)]

    trend = "Güçlü yükseliş" if e20[-1] > e50[-1] > e200[-1] else "Güçlü düşüş" if e20[-1] < e50[-1] < e200[-1] else "Karışık"
    momentum = "Pozitif" if macd_hist > 0 and rsi_value > 50 else "Negatif" if macd_hist < 0 and rsi_value < 50 else "Nötr"
    recent = candles[-8:]
    bodies = [abs(candle["close"] - candle["open"]) for candle in recent]
    upper_wicks = [candle["high"] - max(candle["open"], candle["close"]) for candle in recent]
    lower_wicks = [min(candle["open"], candle["close"]) - candle["low"] for candle in recent]
    body_total = max(sum(bodies), atr_value * 0.05, 0.00000001)
    upper_wick_ratio = sum(upper_wicks) / body_total
    lower_wick_ratio = sum(lower_wicks) / body_total
    recent_range = fmean(highs[-8:]) - fmean(lows[-8:])
    older_range = fmean(highs[-30:-8]) - fmean(lows[-30:-8]) if len(highs) >= 30 else recent_range
    squeeze_ratio = recent_range / older_range if older_range else 1.0
    extension_atr = abs(price - e20[-1]) / max(atr_value, 0.00000001)

    trap_score = 10
    if direction == "LONG":
        trap_score += 24 if volume_ratio < 0.9 else 0
        trap_score += 20 if upper_wick_ratio > 1.5 else 0
        trap_score += 18 if extension_atr > 2 else 0
        trap_score += 15 if (resistance - price) / max(atr_value, 0.00000001) < 1 else 0
        trap_score += 10 if rsi_value > 72 else 0
    elif direction == "SHORT":
        trap_score += 24 if volume_ratio < 0.9 else 0
        trap_score += 20 if lower_wick_ratio > 1.5 else 0
        trap_score += 18 if extension_atr > 2 else 0
        trap_score += 15 if (price - support) / max(atr_value, 0.00000001) < 1 else 0
        trap_score += 10 if rsi_value < 28 else 0
    else:
        trap_score += 12
    trap_score = min(95, trap_score)
    trap_level = "YÜKSEK" if trap_score >= 60 else "ORTA" if trap_score >= 35 else "DÜŞÜK"

    breakout_quality = 35
    breakout_quality += 20 if volume_ratio >= 1.15 else 5 if volume_ratio >= 1 else 0
    breakout_quality += 15 if adx_value >= 20 else 0
    breakout_quality += 15 if trend.startswith("Güçlü") and direction != "BEKLE" else 0
    breakout_quality += 10 if momentum != "Nötr" and direction != "BEKLE" else 0
    breakout_quality -= trap_score * 0.35
    breakout_quality = round(max(5, min(95, breakout_quality)))
    entry_timing = "İZLE" if direction == "BEKLE" else "GERİ ÇEKİLME BEKLE" if trap_score >= 60 else "GİRİŞ UYGUN" if breakout_quality >= 65 else "MUM KAPANIŞINI BEKLE"
    squeeze = "YÜKSEK" if squeeze_ratio < 0.65 else "ORTA" if squeeze_ratio < 0.85 else "YOK"
    wick_signal = "Üst fitil baskısı" if upper_wick_ratio > 1.5 else "Alt fitil toplama" if lower_wick_ratio > 1.5 else "Belirgin fitil tuzağı yok"
    radar = {
        "trap_score": round(trap_score), "trap_level": trap_level,
        "breakout_quality": breakout_quality, "entry_timing": entry_timing,
        "squeeze": squeeze, "fomo_risk": "YÜKSEK" if extension_atr > 2 else "DÜŞÜK",
        "wick_signal": wick_signal,
    }
    reasons = [f"Trend {trend.lower()}", f"RSI {rsi_value:.1f}", f"hacim ortalamanın {volume_ratio:.2f} katı", f"ADX {adx_value:.1f}"]
    return {
        "direction": direction, "confidence": round(confidence), "entry": price,
        "stop_loss": stop, "tp1": targets[0], "tp2": targets[1], "tp3": targets[2],
        "risk_reward": 3.0, "trend": trend, "momentum": momentum,
        "rsi": rsi_value, "macd": macd_hist, "adx": adx_value, "atr": atr_value,
        "volume_ratio": volume_ratio, "support": support, "resistance": resistance,
        "bollinger": {"upper": bb_upper, "middle": bb_mid, "lower": bb_lower},
        "ema": {"ema20": e20[-1], "ema50": e50[-1], "ema200": e200[-1]},
        "radar": radar,
        "explanation": ". ".join(reasons) + f". Sonuç: {direction}.",
        "series": {
            "ema20": [{"time": t, "value": v} for t, v in zip(times, e20)],
            "ema50": [{"time": t, "value": v} for t, v in zip(times, e50)],
            "ema200": [{"time": t, "value": v} for t, v in zip(times, e200)],
        },
    }

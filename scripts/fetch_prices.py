"""
Yahoo Finance 주가 수집.
앱의 YahooFinanceService 포팅.
초당 요청 제한으로 차단 방지.
"""
import time
import requests
from dataclasses import dataclass
from typing import List, Optional

HEADERS = {"User-Agent": "Mozilla/5.0 (StockTrend Server)"}


@dataclass
class ChartData:
    closes: List[float]           # 유효 종가 리스트
    current_price: Optional[float]
    previous_close: Optional[float]
    year_low: Optional[float]
    year_high: Optional[float]


def fetch_chart(ticker: str, retries: int = 2) -> Optional[ChartData]:
    """
    1년치 일봉 + 메타 정보 수집.
    실패 시 retries회 재시도.
    """
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    params = {"range": "1y", "interval": "1d"}

    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
            if resp.status_code == 429:
                # Too Many Requests → 백오프
                time.sleep(2 * (attempt + 1))
                continue
            resp.raise_for_status()
            data = resp.json()

            result = data.get("chart", {}).get("result")
            if not result:
                return None
            r = result[0]

            timestamps = r.get("timestamp")
            indicators = r.get("indicators", {}).get("quote", [{}])[0]
            closes_raw = indicators.get("close")
            if not timestamps or not closes_raw:
                return None

            # None 제거 (유효 종가만)
            closes = [c for c in closes_raw if c is not None]
            if len(closes) < 2:
                return None

            meta = r.get("meta", {})
            return ChartData(
                closes=closes,
                current_price=meta.get("regularMarketPrice"),
                previous_close=meta.get("previousClose"),
                year_low=meta.get("fiftyTwoWeekLow"),
                year_high=meta.get("fiftyTwoWeekHigh"),
            )
        except Exception as e:
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            return None
    return None

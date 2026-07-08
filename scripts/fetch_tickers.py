"""
종목 목록 수집.
한국: KRX 공식 (kind.krx.co.kr)
미국: rreichel3/US-Stock-Symbols (약 7000개)
앱의 KrxApiService / UsaStockService 포팅.
"""
import io
import requests
from dataclasses import dataclass
from typing import List


@dataclass
class Stock:
    code: str      # 야후 티커 (005930.KS / AAPL)
    name: str
    market: str    # "KOSPI" / "KOSDAQ" / "US"


HEADERS = {"User-Agent": "Mozilla/5.0 (StockTrend Server)"}


def fetch_krx_market(market_type: str, market_name: str) -> List[Stock]:
    """
    KRX KIND에서 시장별 종목 목록 다운로드.
    market_type: "stockMkt"(KOSPI) / "kosdaqMkt"(KOSDAQ)
    """
    url = "https://kind.krx.co.kr/corpgeneral/corpList.do"
    params = {
        "method": "download",
        "marketType": market_type,
    }
    resp = requests.post(url, params=params, headers=HEADERS, timeout=30)
    resp.encoding = "euc-kr"  # KRX는 euc-kr

    import pandas as pd
    tables = pd.read_html(io.StringIO(resp.text))
    df = tables[0]

    stocks = []
    for _, row in df.iterrows():
        code_raw = str(row["종목코드"]).zfill(6)  # 6자리 패딩
        name = str(row["회사명"]).strip()
        # SPAC/유효하지 않은 티커 필터
        if not code_raw.isdigit():
            continue
        suffix = ".KS" if market_type == "stockMkt" else ".KQ"
        stocks.append(Stock(code=code_raw + suffix, name=name, market=market_name))
    return stocks


def fetch_korea() -> List[Stock]:
    kospi = fetch_krx_market("stockMkt", "KOSPI")
    kosdaq = fetch_krx_market("kosdaqMkt", "KOSDAQ")
    all_stocks = kospi + kosdaq
    all_stocks.sort(key=lambda s: s.code)
    print(f"[KR] KOSPI {len(kospi)} + KOSDAQ {len(kosdaq)} = {len(all_stocks)}")
    return all_stocks


def fetch_usa() -> List[Stock]:
    url = "https://raw.githubusercontent.com/rreichel3/US-Stock-Symbols/main/all/all_tickers.txt"
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()

    stocks = []
    seen = set()
    for line in resp.text.splitlines():
        symbol = line.strip()
        if not symbol:
            continue
        # 5글자 초과/특수증권 필터
        if len(symbol) > 5:
            continue
        if not all(c.isalpha() or c == "." for c in symbol):
            continue
        yahoo_symbol = symbol.replace(".", "-")  # BRK.B -> BRK-B
        if yahoo_symbol in seen:
            continue
        seen.add(yahoo_symbol)
        stocks.append(Stock(code=yahoo_symbol, name=symbol, market="US"))
    stocks.sort(key=lambda s: s.code)
    print(f"[US] {len(stocks)} tickers")
    return stocks

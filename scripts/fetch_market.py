"""
증시 현황 수집 스크립트.
Yahoo Finance에서 주요 지수/환율을 수집하여 market.json으로 저장.

실행:
  python fetch_market.py

저장 위치:
  market.json (루트 - jamshands/stock365-data 레포에 push됨)
"""

import json
import os
import time
import requests
from datetime import datetime, timezone, timedelta
from typing import Optional

# ── 설정 ──────────────────────────────────────────────
KST = timezone(timedelta(hours=9))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (StockTrend Server)",
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
}

SYMBOLS = {
    "^KS11":  "kospi",
    "^KQ11":  "kosdaq",
    "^GSPC":  "sp500",
    "^IXIC":  "nasdaq",
    "^DJI":   "dow",
    "^VIX":   "vix",
    "KRW=X":  "usdkrw",
}

OUTPUT_PATH = "market.json"


class MarketDataProvider:
    """증시 데이터 제공자 인터페이스."""

    def fetch(self, symbol: str) -> Optional[dict]:
        raise NotImplementedError


class YahooFinanceProvider(MarketDataProvider):
    """Yahoo Finance v8 chart API를 사용하는 구현체.

    등락률 계산 우선순위:
      1) meta.regularMarketChangePercent / regularMarketChange
         - Yahoo가 자체적으로 "현재 시점 기준" 등락을 계산해서 주는 값.
           우리가 5d 종가 배열을 직접 비교하는 것보다 기준일 어긋남 위험이 적다.
      2) 위 값이 없으면, 5일치 종가 배열에서 날짜 매칭으로 전일 종가를 찾아 직접 계산 (fallback)
      3) 그마저 안 되면 meta.previousClose / chartPreviousClose로 계산 (최종 fallback)
    """

    BASE_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

    def fetch(self, symbol: str) -> Optional[dict]:
        url = self.BASE_URL.format(symbol=symbol)
        params = {
            "range": "5d",
            "interval": "1d",
            "_": str(int(time.time() * 1000)),  # 캐시 무효화
        }

        for attempt in range(3):
            try:
                resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
                if resp.status_code == 429:
                    time.sleep(3 * (attempt + 1))
                    continue
                resp.raise_for_status()
                data = resp.json()

                result = data.get("chart", {}).get("result")
                if not result:
                    return None
                r = result[0]
                meta = r.get("meta", {})

                price = meta.get("regularMarketPrice")
                if price is None:
                    return None

                # ── 1순위: Yahoo가 직접 제공하는 당일 등락 ──
                change = meta.get("regularMarketChange")
                change_pct = meta.get("regularMarketChangePercent")

                if change is not None and change_pct is not None:
                    print(f"    [debug] {symbol}: using meta.regularMarketChange* "
                          f"price={price}, change={change}, change_pct={change_pct}")
                    return {
                        "price": round(price, 2),
                        "change": round(change, 4),
                        "changePercent": round(change_pct, 2),
                    }

                # ── 2순위: 5일치 종가 배열에서 날짜 매칭으로 직접 계산 (fallback) ──
                timestamps = r.get("timestamp") or []
                closes_raw = r.get("indicators", {}).get("quote", [{}])[0].get("close", [])
                pairs = [(ts, c) for ts, c in zip(timestamps, closes_raw) if c is not None]

                by_date: dict[str, float] = {}
                for ts, c in pairs:
                    date_str = datetime.fromtimestamp(ts, KST).strftime("%Y-%m-%d")
                    by_date[date_str] = c

                sorted_dates = sorted(by_date.keys())
                today_str = datetime.now(KST).strftime("%Y-%m-%d")

                prev_close = None
                if sorted_dates:
                    if sorted_dates[-1] == today_str:
                        if len(sorted_dates) >= 2:
                            prev_close = by_date[sorted_dates[-2]]
                    else:
                        prev_close = by_date[sorted_dates[-1]]

                # ── 3순위: meta.previousClose 폴백 ──
                if prev_close is None:
                    prev_close = meta.get("previousClose") or meta.get("chartPreviousClose")

                calc_change = None
                calc_change_pct = None
                if prev_close and prev_close != 0:
                    calc_change = round(price - prev_close, 4)
                    calc_change_pct = round((price - prev_close) / prev_close * 100, 2)

                print(f"    [debug] {symbol}: fallback calc, price={price}, "
                      f"sorted_dates={sorted_dates}, prev_close={prev_close}, "
                      f"meta.previousClose={meta.get('previousClose')}")

                return {
                    "price": round(price, 2),
                    "change": calc_change,
                    "changePercent": calc_change_pct,
                }

            except Exception as e:
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                print(f"  [{symbol}] 실패: {e}")
                return None

        return None


def load_existing() -> dict:
    if os.path.exists(OUTPUT_PATH):
        try:
            with open(OUTPUT_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def fetch_market(provider: MarketDataProvider) -> None:
    existing = load_existing()
    now_kst = datetime.now(KST)
    updated_at = now_kst.strftime("%Y-%m-%dT%H:%M:%S+09:00")

    print(f"[Market] 수집 시작: {updated_at}")

    success_count = 0
    fail_count = 0
    payload = {}

    for symbol, key in SYMBOLS.items():
        result = provider.fetch(symbol)
        time.sleep(0.2)

        if result is not None:
            payload[key] = result
            success_count += 1
            print(f"  ✅ {key:10s} price={result['price']:>12.2f}  "
                  f"change={result['change']:>+8.2f}  "
                  f"({result['changePercent']:>+6.2f}%)")
        else:
            if key in existing:
                payload[key] = existing[key]
            fail_count += 1
            print(f"  ❌ {key:10s} 실패 → 기존값 유지")

    if success_count == 0:
        print(f"[Market] 전체 실패 → market.json 유지 (덮어쓰지 않음)")
        return

    output = {"updatedAt": updated_at}
    for key in SYMBOLS.values():
        if key in payload:
            output[key] = payload[key]

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, separators=(",", ":"))

    print(f"[Market] 저장 완료: {OUTPUT_PATH} "
          f"(성공 {success_count}/{len(SYMBOLS)}, 실패 {fail_count})")


def main():
    provider = YahooFinanceProvider()
    fetch_market(provider)


if __name__ == "__main__":
    main()

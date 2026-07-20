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

HEADERS = {"User-Agent": "Mozilla/5.0 (StockTrend Server)"}

# 수집 대상 심볼 → market.json 키 매핑
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


# ── 데이터 제공 인터페이스 ────────────────────────────
class MarketDataProvider:
    """증시 데이터 제공자 인터페이스."""

    def fetch(self, symbol: str) -> Optional[dict]:
        raise NotImplementedError


class YahooFinanceProvider(MarketDataProvider):
    """Yahoo Finance v8 chart API를 사용하는 구현체."""

    BASE_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

    def fetch(self, symbol: str) -> Optional[dict]:
        url = self.BASE_URL.format(symbol=symbol)
        params = {"range": "2d", "interval": "1d"}  # 2일치면 전일 종가 확보 가능

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

                # closes 배열(같은 요청 내 연속 일봉)을 1순위로 사용.
                # previousClose/chartPreviousClose는 종종 regularMarketPrice와 다른
                # 기준일을 가리켜 등락률이 어긋나는 문제가 있었음
                # (run_detect.py에서 동일 버그를 이미 겪고 수정한 방식과 동일하게 처리).
                closes_raw = r.get("indicators", {}).get("quote", [{}])[0].get("close", [])
                closes = [c for c in closes_raw if c is not None]

                prev_close = closes[-2] if len(closes) >= 2 else (
                    meta.get("previousClose") or meta.get("chartPreviousClose")
                )

                change = None
                change_pct = None
                if prev_close and prev_close != 0:
                    change = round(price - prev_close, 4)
                    change_pct = round((price - prev_close) / prev_close * 100, 2)

                return {
                    "price": round(price, 2),
                    "change": change,
                    "changePercent": change_pct,
                }

            except Exception as e:
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                print(f"  [{symbol}] 실패: {e}")
                return None

        return None


# ── 메인 로직 ─────────────────────────────────────────

def load_existing() -> dict:
    """기존 market.json 로드. 없으면 빈 딕셔너리 반환."""
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

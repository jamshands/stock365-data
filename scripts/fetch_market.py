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
        # 5일치로 넉넉히 요청 - 주말/휴장일이 껴도 최근 2개 거래일을 안전하게 확보하기 위함
        params = {"range": "5d", "interval": "1d"}

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

                timestamps = r.get("timestamp") or []
                closes_raw = r.get("indicators", {}).get("quote", [{}])[0].get("close", [])

                # timestamp-close 쌍을 만들고, close가 None인 항목(미완성 봉)은 제외
                pairs = [
                    (ts, c) for ts, c in zip(timestamps, closes_raw) if c is not None
                ]

                # timestamp(UTC epoch)를 KST 날짜로 변환해서 "날짜별 최신 종가"만 남긴다.
                by_date: dict[str, float] = {}
                for ts, c in pairs:
                    date_str = datetime.fromtimestamp(ts, KST).strftime("%Y-%m-%d")
                    by_date[date_str] = c

                sorted_dates = sorted(by_date.keys())

                # 핵심: "오늘 날짜 캔들이 배열에 이미 존재하는지" 먼저 확인한다.
                # 장중에는 보통 오늘 캔들이 아직 없거나(=배열 마지막이 어제),
                # 장 마감 후 API가 갱신되면 오늘 캔들이 생길 수 있다(=배열 마지막이 오늘).
                # 이 두 경우를 구분하지 않고 무조건 "마지막에서 2번째"를 쓰면,
                # 오늘 캔들이 없을 때 실제로는 "어제"를 가져와야 하는데
                # 배열 마지막(어제)의 하나 전(그저께 이전, 주말이면 며칠 전)을 잘못 가져오게 된다.
                today_str = datetime.now(KST).strftime("%Y-%m-%d")

                prev_close = None
                if sorted_dates:
                    if sorted_dates[-1] == today_str:
                        # 오늘 캔들이 이미 존재 → 그 직전 날짜가 전일 종가
                        if len(sorted_dates) >= 2:
                            prev_close = by_date[sorted_dates[-2]]
                    else:
                        # 오늘 캔들이 아직 없음(일반적인 장중 상황) → 배열의 마지막 날짜가 전일 종가
                        prev_close = by_date[sorted_dates[-1]]

                if prev_close is None:
                    prev_close = meta.get("previousClose") or meta.get("chartPreviousClose")

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

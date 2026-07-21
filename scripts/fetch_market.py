"""
증시 현황 수집 스크립트.
한국 지수(코스피/코스닥)는 네이버 금융 실시간 API를 사용 - Yahoo Finance의 지수(^KS11, ^KQ11)
데이터가 기준일이 어긋나는 문제가 반복적으로 발생해 더 신뢰 가능한 소스로 전환.
미국 지수/VIX/환율은 기존 Yahoo Finance를 유지.

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

YAHOO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (StockTrend Server)",
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
}
NAVER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://m.stock.naver.com/",
}

OUTPUT_PATH = "market.json"


# ── 데이터 제공 인터페이스 ────────────────────────────
class MarketDataProvider:
    """증시 데이터 제공자 인터페이스."""

    def fetch(self, symbol: str) -> Optional[dict]:
        raise NotImplementedError


# ── 네이버 금융 (한국 지수 전용) ──────────────────────
class NaverIndexProvider(MarketDataProvider):
    """
    네이버 금융 실시간 지수 API.
    Yahoo Finance의 ^KS11/^KQ11 previousClose가 기준일이 어긋나 등락률이
    반복적으로 틀리는 문제가 있어, 더 신뢰 가능한 네이버 데이터로 대체.

    엔드포인트: https://polling.finance.naver.com/api/realtime/domestic/index/{code}
    code: "KOSPI" 또는 "KOSDAQ"

    응답 스키마가 문서화되어 있지 않아(비공개 API), 여러 가능한 필드명을 순차 시도한다.
    실패 시 raw JSON을 로그에 남겨서 다음 수정 때 정확한 스키마를 바로 확인할 수 있게 한다.
    """

    BASE_URL = "https://polling.finance.naver.com/api/realtime/domestic/index/{code}"

    def fetch(self, code: str) -> Optional[dict]:
        url = self.BASE_URL.format(code=code)
        params = {"_": str(int(time.time() * 1000))}  # 캐시 무효화

        for attempt in range(3):
            try:
                resp = requests.get(url, params=params, headers=NAVER_HEADERS, timeout=15)
                if resp.status_code == 429:
                    time.sleep(3 * (attempt + 1))
                    continue
                resp.raise_for_status()
                data = resp.json()

                parsed = self._parse(data)
                if parsed is not None:
                    print(f"    [debug] NAVER {code}: parsed={parsed}")
                    return parsed

                print(f"    [debug] NAVER {code}: 파싱 실패, raw={json.dumps(data, ensure_ascii=False)[:1000]}")
                return None

            except Exception as e:
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                print(f"  [NAVER {code}] 실패: {e}")
                return None

        return None

    def _parse(self, data) -> Optional[dict]:
        """여러 가능한 네이버 응답 스키마를 순차적으로 시도해서 파싱."""
        try:
            item = None
            if isinstance(data, dict) and "datas" in data and data["datas"]:
                item = data["datas"][0]
            elif isinstance(data, list) and data:
                item = data[0]
            elif isinstance(data, dict):
                item = data

            if item:
                price = self._pick_number(item, ["nv", "closePrice", "now", "price", "closeValue"])
                change = self._pick_number(item, ["cv", "compareToPreviousClosePrice", "change", "changeValue"])
                change_pct = self._pick_number(item, ["cr", "fluctuationsRatio", "changeRate", "risefallRate"])

                if price is not None:
                    result = {"price": round(price, 2)}
                    if change is not None:
                        result["change"] = round(change, 4)
                    if change_pct is not None:
                        result["changePercent"] = round(change_pct, 2)
                    if "change" in result and "changePercent" in result:
                        return result
                    if "changePercent" in result and "change" not in result and price:
                        prev = price / (1 + result["changePercent"] / 100)
                        result["change"] = round(price - prev, 4)
                        return result
        except Exception:
            pass

        return None

    @staticmethod
    def _pick_number(item: dict, keys: list) -> Optional[float]:
        for k in keys:
            if k in item and item[k] not in (None, ""):
                try:
                    raw = str(item[k]).replace(",", "").replace("%", "").strip()
                    return float(raw)
                except (ValueError, TypeError):
                    continue
        return None


# ── Yahoo Finance (미국 지수 / VIX / 환율) ────────────
class YahooFinanceProvider(MarketDataProvider):
    """Yahoo Finance v8 chart API를 사용하는 구현체.

    등락률 계산 우선순위:
      1) meta.regularMarketChangePercent / regularMarketChange
      2) 5일치 종가 배열에서 날짜 매칭으로 직접 계산 (fallback)
      3) meta.previousClose / chartPreviousClose (최종 fallback)
    """

    BASE_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

    def fetch(self, symbol: str) -> Optional[dict]:
        url = self.BASE_URL.format(symbol=symbol)
        params = {
            "range": "5d",
            "interval": "1d",
            "_": str(int(time.time() * 1000)),
        }

        for attempt in range(3):
            try:
                resp = requests.get(url, params=params, headers=YAHOO_HEADERS, timeout=15)
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

                change = meta.get("regularMarketChange")
                change_pct = meta.get("regularMarketChangePercent")

                if change is not None and change_pct is not None:
                    return {
                        "price": round(price, 2),
                        "change": round(change, 4),
                        "changePercent": round(change_pct, 2),
                    }

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

                if prev_close is None:
                    prev_close = meta.get("previousClose") or meta.get("chartPreviousClose")

                calc_change = None
                calc_change_pct = None
                if prev_close and prev_close != 0:
                    calc_change = round(price - prev_close, 4)
                    calc_change_pct = round((price - prev_close) / prev_close * 100, 2)

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


# ── 심볼 → (provider, provider가 쓰는 심볼/코드, market.json 키) 매핑 ──
naver_provider = NaverIndexProvider()
yahoo_provider = YahooFinanceProvider()

FETCH_PLAN = [
    (naver_provider, "KOSPI",  "kospi"),
    (naver_provider, "KOSDAQ", "kosdaq"),
    (yahoo_provider, "^GSPC",  "sp500"),
    (yahoo_provider, "^IXIC",  "nasdaq"),
    (yahoo_provider, "^DJI",   "dow"),
    (yahoo_provider, "^VIX",   "vix"),
    (yahoo_provider, "KRW=X",  "usdkrw"),
]

KEY_ORDER = ["kospi", "kosdaq", "sp500", "nasdaq", "dow", "vix", "usdkrw"]


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


def fetch_market() -> None:
    existing = load_existing()
    now_kst = datetime.now(KST)
    updated_at = now_kst.strftime("%Y-%m-%dT%H:%M:%S+09:00")

    print(f"[Market] 수집 시작: {updated_at}")

    success_count = 0
    fail_count = 0
    payload = {}

    for provider, fetch_symbol, key in FETCH_PLAN:
        result = provider.fetch(fetch_symbol)
        time.sleep(0.2)

        if result is not None and result.get("price") is not None:
            payload[key] = result
            success_count += 1
            change = result.get("change")
            change_pct = result.get("changePercent")
            change_str = f"{change:+.2f}" if change is not None else "N/A"
            pct_str = f"{change_pct:+.2f}" if change_pct is not None else "N/A"
            print(f"  ✅ {key:10s} price={result['price']:>12.2f}  "
                  f"change={change_str:>8s}  ({pct_str}%)")
        else:
            if key in existing:
                payload[key] = existing[key]
            fail_count += 1
            print(f"  ❌ {key:10s} 실패 → 기존값 유지")

    if success_count == 0:
        print(f"[Market] 전체 실패 → market.json 유지 (덮어쓰지 않음)")
        return

    output = {"updatedAt": updated_at}
    for key in KEY_ORDER:
        if key in payload:
            output[key] = payload[key]

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, separators=(",", ":"))

    print(f"[Market] 저장 완료: {OUTPUT_PATH} "
          f"(성공 {success_count}/{len(FETCH_PLAN)}, 실패 {fail_count})")


def main():
    fetch_market()


if __name__ == "__main__":
    main()

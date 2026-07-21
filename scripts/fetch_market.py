"""
증시 현황 수집 스크립트.
- 한국 지수(코스피/코스닥): 네이버 실시간 지수 API + 투자자별 매매동향(외국인/기관/개인)
- 미국 지수/VIX/환율: Yahoo Finance

실행:
  python fetch_market.py

저장 위치:
  market.json (루트 - jamshands/stock365-data 레포에 push됨)
"""

import json
import os
import re
import time
import requests
from datetime import datetime, timezone, timedelta
from typing import Optional
from bs4 import BeautifulSoup

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
    "Referer": "https://finance.naver.com",
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
    """

    BASE_URL = "https://polling.finance.naver.com/api/realtime/domestic/index/{code}"

    def fetch(self, code: str) -> Optional[dict]:
        url = self.BASE_URL.format(code=code)
        params = {"_": str(int(time.time() * 1000))}

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


# ── 네이버 금융 (투자자별 매매동향 전용) ──────────────
class NaverInvestorTrendProvider:
    """
    네이버 지수 페이지(sise_index.naver)에 함께 표시되는
    "투자자별 매매동향"(당일 누적, 억원 단위) 크롤링.

    페이지 텍스트 구조상 "개인"/"외국인"/"기관"이라는 정확한 한 줄 다음에
    부호 있는 숫자 줄, 그 다음 "억" 단위 줄이 오는 패턴이 안정적으로 확인됨
    (CSS 클래스명은 언제든 바뀔 수 있어 텍스트 패턴 매칭이 더 견고함).

    실패해도 market.json 전체 저장을 막지 않도록 항상 예외를 흡수하고 None 반환.
    """

    BASE_URL = "https://finance.naver.com/sise/sise_index.naver"

    def fetch(self, code: str) -> Optional[dict]:
        """code: "KOSPI" 또는 "KOSDAQ" """
        params = {"code": code}
        for attempt in range(2):
            try:
                resp = requests.get(self.BASE_URL, params=params, headers=NAVER_HEADERS, timeout=15)
                resp.raise_for_status()
                resp.encoding = "euc-kr"
                soup = BeautifulSoup(resp.text, "html.parser")

                full_text = soup.get_text(separator="\n")
                lines = [l.strip() for l in full_text.split("\n") if l.strip()]

                result: dict[str, int] = {}
                label_map = {"개인": "individual", "외국인": "foreign", "기관": "institution"}

                for i, line in enumerate(lines):
                    if line in label_map and i + 1 < len(lines):
                        num_str = lines[i + 1]
                        if re.fullmatch(r'[+-]?\d[\d,]*', num_str):
                            value = int(num_str.replace(",", ""))
                            result[label_map[line]] = value

                # 세 항목(개인/외국인/기관) 모두 확보됐을 때만 유효한 결과로 인정
                if all(k in result for k in ("individual", "foreign", "institution")):
                    return result

                print(f"    [debug] NAVER 투자자동향 {code}: 일부만 파싱됨 result={result}")
                return None

            except Exception as e:
                if attempt < 1:
                    time.sleep(1.5)
                    continue
                print(f"  [NAVER 투자자동향 {code}] 실패: {e}")
                return None

        return None


# ── Yahoo Finance (미국 지수 / VIX / 환율) ────────────
class YahooFinanceProvider(MarketDataProvider):
    """Yahoo Finance v8 chart API를 사용하는 구현체."""

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


# ── 미국 시장 투자자동향 (구조만 - 추후 데이터 소스 확정 시 구현) ──
class UsInvestorTrendProvider:
    """
    미국은 한국(KRX)과 같은 '외국인/기관/개인' 분류 체계가 존재하지 않음.
    데이터 소스가 확정되면 이 클래스에 구현. 지금은 항상 None 반환하며,
    market.json에는 investorTrend 필드 자체가 생략됨(앱에서 null 처리).
    """

    def fetch(self, index_key: str) -> Optional[dict]:
        return None


# ── 심볼/코드 매핑 ─────────────────────────────────────
naver_index_provider = NaverIndexProvider()
naver_investor_provider = NaverInvestorTrendProvider()
yahoo_provider = YahooFinanceProvider()
us_investor_provider = UsInvestorTrendProvider()

# (지수 provider, 지수 fetch용 코드, market.json 키, 투자자동향 provider 또는 None, 투자자동향 fetch용 코드)
FETCH_PLAN = [
    (naver_index_provider, "KOSPI",  "kospi",  naver_investor_provider, "KOSPI"),
    (naver_index_provider, "KOSDAQ", "kosdaq", naver_investor_provider, "KOSDAQ"),
    (yahoo_provider,       "^GSPC",  "sp500",  us_investor_provider,    "sp500"),
    (yahoo_provider,       "^IXIC",  "nasdaq", us_investor_provider,    "nasdaq"),
    (yahoo_provider,       "^DJI",   "dow",    us_investor_provider,    "dow"),
    (yahoo_provider,       "^VIX",   "vix",    None,                    None),
    (yahoo_provider,       "KRW=X",  "usdkrw", None,                    None),
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

    for index_provider, fetch_symbol, key, investor_provider, investor_code in FETCH_PLAN:
        result = index_provider.fetch(fetch_symbol)
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
            continue  # 지수 자체가 실패하면 투자자동향도 스킵

        # ── 투자자별 매매동향 (있는 경우만) ──
        if investor_provider is not None:
            trend = investor_provider.fetch(investor_code)
            time.sleep(0.2)
            if trend is not None:
                payload[key]["investorTrend"] = trend
                print(f"      └ 투자자동향: 외국인{trend['foreign']:+d} "
                      f"기관{trend['institution']:+d} 개인{trend['individual']:+d} (억원)")
            else:
                # 실패 시 기존 값 유지 (있었다면)
                existing_trend = existing.get(key, {}).get("investorTrend")
                if existing_trend:
                    payload[key]["investorTrend"] = existing_trend
                print(f"      └ 투자자동향: 실패 → {'기존값 유지' if existing_trend else '데이터 없음'}")

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

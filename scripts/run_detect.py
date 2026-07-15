"""
메인 탐지 실행 스크립트.
전종목 데이터 1회 수집 → 전체 조합(160) 탐지 → JSON 저장.

조합: 연속 1~20일 × 방향 UP/DOWN × 예외 0~3회 = 160

실행:
  python run_detect.py KR   # 한국
  python run_detect.py US   # 미국
"""
import sys
import json
import time
import os
import concurrent.futures
from datetime import datetime, timezone, timedelta
from typing import List, Dict

from fetch_tickers import fetch_korea, fetch_usa, Stock
from fetch_prices import fetch_chart, ChartData
from trend_detector import detect

# ── Marcap (한국 시가총액) ──
import pandas as pd

_MARCAP_YEAR = datetime.now().year
_MARCAP_URL = f"https://raw.githubusercontent.com/FinanceData/marcap/master/data/marcap-{_MARCAP_YEAR}.parquet"


def load_marcap_data() -> dict:
    """KRX 시가총액 데이터 1회 로드. 실패해도 빈 딕셔너리 반환 (서버 중단 금지)."""
    try:
        df = pd.read_parquet(_MARCAP_URL)
        latest = df.sort_values("Date").groupby("Code").tail(1)
        result = dict(zip(latest["Code"], latest["Marcap"]))
        print(f"[Marcap] 로드 완료: {len(result)}종목")
        return result
    except Exception as e:
        print(f"[Marcap] 로드 실패 (시총 null 처리): {e}")
        return {}

# ── 조합 정의 ──
DAYS_RANGE = range(1, 21)          # 1~20일
DIRECTIONS = ["UP", "DOWN"]
EXCEPTIONS = range(0, 4)           # 0~3회

MAX_WORKERS = 8                     # 동시 요청 (차단 방지)
REQUEST_DELAY = 0.1                 # 요청 간 최소 간격(초)

KST = timezone(timedelta(hours=9))


def _resolve_market_cap(stock: Stock, chart: ChartData, marcap_data: dict):
    """한국 종목은 marcap에서 시총 조회, 미국은 야후 그대로 사용."""
    if stock.market in ("KOSPI", "KOSDAQ") and marcap_data:
        # 야후 코드(005930.KS / 005930.KQ)에서 접미사 제거
        pure_code = stock.code.split(".")[0]
        cap = marcap_data.get(pure_code)
        return int(cap) if cap is not None else None
    return chart.market_cap


def analyze_stock(stock: Stock, marcap_data: dict = {}) -> Dict:
    """
    한 종목의 차트를 수집하고, 모든 조합에 대해 탐지.
    반환: {"stock": {...}, "detections": [{key, days, direction, ...}]}
    """
    chart = fetch_chart(stock.code)
    time.sleep(REQUEST_DELAY)
    if chart is None or len(chart.closes) < 2:
        return {"code": stock.code, "detections": []}

    closes = chart.closes
    # 당일 변화율.
    # [버그 수정] 예전엔 야후 meta의 currentPrice/previousClose 조합을 우선 사용했는데,
    # 이 둘이 서로 다른 기준일을 가리키는 경우가 있어(야후 쪽 데이터 정합성 문제 -
    # previousClose가 currentPrice와 다른 날짜 기준인 케이스) 변화율이 실제와
    # 반대 부호/다른 날짜 값으로 나오는 버그가 있었다.
    # 실사례: 피에스텍(002230) - currentPrice는 정확히 오늘(4,890원)인데
    # previousClose 기반 계산은 어제자 등락률(-2.95%)을 그대로 보여줌.
    # 실제 정답은 +10.01%(네이버 확인).
    # closes 배열은 같은 요청 안에서 뽑힌 연속된 일봉 히스토리라 서로 기준일이
    # 어긋날 수 없으므로, 이제 이걸 우선 소스로 쓴다. 데이터가 부족할 때만
    # meta 값으로 폴백한다.
    change_pct = None
    if len(closes) >= 2 and closes[-2] != 0:
        change_pct = ((closes[-1] - closes[-2]) / closes[-2]) * 100.0
    elif chart.current_price and chart.previous_close and chart.previous_close != 0:
        change_pct = ((chart.current_price - chart.previous_close) / chart.previous_close) * 100.0

    detections = []
    for direction in DIRECTIONS:
        for days in DAYS_RANGE:
            for exc in EXCEPTIONS:
                res = detect(closes, days, direction, exc, include_today=True)
                if res is not None and res.consecutive_days == days:
                    # 딱 해당 일수인 것만 (앱 필터 로직과 동일: 초과분은 상위 일수에서 잡힘)
                    detections.append({
                        "key": f"{direction}-{days}-{exc}",
                        "direction": direction,
                        "days": days,
                        "exception": exc,
                        "consecutiveDays": res.consecutive_days,
                        "totalChangePct": round(res.total_change_pct, 2) if res.total_change_pct else None,
                    })

    return {
        "code": stock.code,
        "name": stock.name,
        "market": stock.market,
        "currentPrice": chart.current_price,
        "currentChangePct": round(change_pct, 2) if change_pct is not None else None,
        "yearLow": chart.year_low,
        "yearHigh": chart.year_high,
        "marketCap": _resolve_market_cap(stock, chart, marcap_data),
        "detections": detections,
    }


def build_results(stocks: List[Stock], country: str) -> Dict:
    """
    전종목 분석 후 조합별로 그룹핑한 JSON 구조 생성.
    """
    print(f"[{country}] {len(stocks)} 종목 분석 시작...")
    start = time.time()

    # 한국 배치일 때만 marcap 로드 (미국은 빈 딕셔너리 → 야후 그대로 사용)
    marcap_data = load_marcap_data() if country == "KR" else {}

    analyzed = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(analyze_stock, s, marcap_data): s for s in stocks}
        done = 0
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            analyzed.append(result)
            done += 1
            if done % 200 == 0:
                elapsed = time.time() - start
                print(f"  {done}/{len(stocks)} ({elapsed:.0f}s)")

    results: Dict[str, List] = {}
    for item in analyzed:
        if not item.get("detections"):
            continue
        for det in item["detections"]:
            key = det["key"]
            results.setdefault(key, []).append({
                "code": item["code"],
                "name": item["name"],
                "market": item["market"],
                "currentPrice": item["currentPrice"],
                "currentChangePct": item["currentChangePct"],
                "yearLow": item["yearLow"],
                "yearHigh": item["yearHigh"],
                "marketCap": item["marketCap"],
                "consecutiveDays": det["consecutiveDays"],
                "totalChangePct": det["totalChangePct"],
                "direction": det["direction"],
            })

    # 전체 조합(160개) 모두 채워서 반환 - 탐지 0건인 조합도 빈 배열로 명시.
    # 이렇게 해야 매일 모든 조합 파일이 "오늘 날짜"로 갱신되고,
    # 예전 탐지결과가 남아있는 파일이 최신인 것처럼 보이는 문제를 방지한다.
    all_results: Dict[str, List] = {}
    for direction in DIRECTIONS:
        for days in DAYS_RANGE:
            for exc in EXCEPTIONS:
                key = f"{direction}-{days}-{exc}"
                all_results[key] = results.get(key, [])
    results = all_results

    for key, arr in results.items():
        is_up = key.startswith("UP")
        arr.sort(key=lambda x: (x["totalChangePct"] or 0), reverse=is_up)

    elapsed = time.time() - start
    detected_combos = sum(1 for arr in results.values() if arr)
    print(f"[{country}] 완료: {elapsed:.0f}s, {detected_combos}/{len(results)} 조합에 탐지 결과 있음, "
          f"탐지 종목 {sum(len(v) for v in results.values())}건")

    now = datetime.now(KST)
    return {
        "meta": {
            "country": country,
            "date": now.strftime("%Y-%m-%d"),
            "updatedAt": now.strftime("%Y-%m-%d %H:%M:%S KST"),
            "status": "COMPLETED",
            "totalTickers": len(stocks),
            "detectedCombos": detected_combos,
            "source": "yahoo",
        },
        "results": results,
    }


def save_split(results: Dict[str, List], meta: Dict, country: str):
    out_dir = f"data/{country.lower()}"
    os.makedirs(out_dir, exist_ok=True)

    index = {"meta": meta, "combos": {}}

    for key, arr in results.items():
        with open(f"{out_dir}/{key}.json", "w", encoding="utf-8") as f:
            json.dump({"meta": meta, "results": arr}, f, ensure_ascii=False, separators=(",", ":"))
        index["combos"][key] = len(arr)

    with open(f"{out_dir}/index.json", "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, separators=(",", ":"))

    print(f"[{country}] {len(results)}개 조합 파일 저장 완료 → {out_dir}/")


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("KR", "US"):
        print("사용법: python run_detect.py [KR|US]")
        sys.exit(1)

    country = sys.argv[1]

    if country == "KR":
        stocks = fetch_korea()
    else:
        stocks = fetch_usa()

    data = build_results(stocks, country)
    save_split(data["results"], data["meta"], country)


if __name__ == "__main__":
    main()

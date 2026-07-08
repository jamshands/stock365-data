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

# ── 조합 정의 ──
DAYS_RANGE = range(1, 21)          # 1~20일
DIRECTIONS = ["UP", "DOWN"]
EXCEPTIONS = range(0, 4)           # 0~3회

MAX_WORKERS = 8                     # 동시 요청 (차단 방지)
REQUEST_DELAY = 0.1                 # 요청 간 최소 간격(초)

KST = timezone(timedelta(hours=9))


def analyze_stock(stock: Stock) -> Dict:
    """
    한 종목의 차트를 수집하고, 모든 조합에 대해 탐지.
    반환: {"stock": {...}, "detections": [{key, days, direction, ...}]}
    """
    chart = fetch_chart(stock.code)
    time.sleep(REQUEST_DELAY)
    if chart is None or len(chart.closes) < 2:
        return {"code": stock.code, "detections": []}

    closes = chart.closes
    # 당일 변화율
    change_pct = None
    if chart.current_price and chart.previous_close and chart.previous_close != 0:
        change_pct = ((chart.current_price - chart.previous_close) / chart.previous_close) * 100.0
    elif len(closes) >= 2 and closes[-2] != 0:
        change_pct = ((closes[-1] - closes[-2]) / closes[-2]) * 100.0

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
        "detections": detections,
    }


def build_results(stocks: List[Stock], country: str) -> Dict:
    """
    전종목 분석 후 조합별로 그룹핑한 JSON 구조 생성.
    결과 구조:
    {
      "meta": {...},
      "results": {
        "UP-3-0": [ {종목...}, ... ],
        "DOWN-5-1": [ ... ],
      }
    }
    """
    print(f"[{country}] {len(stocks)} 종목 분석 시작...")
    start = time.time()

    analyzed = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(analyze_stock, s): s for s in stocks}
        done = 0
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            analyzed.append(result)
            done += 1
            if done % 200 == 0:
                elapsed = time.time() - start
                print(f"  {done}/{len(stocks)} ({elapsed:.0f}s)")

    # 조합별로 그룹핑
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
                "consecutiveDays": det["consecutiveDays"],
                "totalChangePct": det["totalChangePct"],
                "direction": det["direction"],
            })

    # 각 조합 내 정렬 (상승 내림차순 / 하락 오름차순)
    for key, arr in results.items():
        is_up = key.startswith("UP")
        arr.sort(key=lambda x: (x["totalChangePct"] or 0), reverse=is_up)

    elapsed = time.time() - start
    print(f"[{country}] 완료: {elapsed:.0f}s, {len(results)} 조합, "
          f"탐지 종목 {sum(len(v) for v in results.values())}건")

    now = datetime.now(KST)
    return {
        "meta": {
            "country": country,
            "date": now.strftime("%Y-%m-%d"),
            "updatedAt": now.strftime("%Y-%m-%d %H:%M:%S KST"),
            "status": "COMPLETED",
            "totalTickers": len(stocks),
            "detectedCombos": len(results),
            "source": "yahoo",
        },
        "results": results,
    }


def save_split(results: Dict[str, List], meta: Dict, country: str):
    """
    조합별로 개별 JSON 파일 저장.
    data/kr/UP-3-0.json 처럼 저장 → 앱은 필요한 파일만 다운로드.
    index.json에는 각 조합의 종목 개수(count)만 담아 홈 화면 요약에 활용.

    전체 저장 (제한 없음): 개별 파일 최대 크기가 미국 DOWN-1-0 기준
    약 400KB 수준으로 확인되어(2026-07-08 실측), 상위 N개로 자를 필요 없이
    전량 저장한다. 예전엔 상위 200개만 저장했으나, 실측 결과 파일 크기가
    생각보다 작아 제한을 없앰 (한국 전체 1.7MB, 미국 전체 4.1MB 수준).
    """
    out_dir = f"data/{country.lower()}"
    os.makedirs(out_dir, exist_ok=True)

    index = {"meta": meta, "combos": {}}

    for key, arr in results.items():
        with open(f"{out_dir}/{key}.json", "w", encoding="utf-8") as f:
            json.dump({"meta": meta, "results": arr}, f, ensure_ascii=False, separators=(",", ":"))
        index["combos"][key] = len(arr)  # 실제 전체 개수 (표시용, 이제 파일 내용과 항상 일치)

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

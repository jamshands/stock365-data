"""
연속 상승/하락 탐지 알고리즘.
앱의 TrendDetector.kt를 그대로 포팅.
"""
from typing import List, Optional
from dataclasses import dataclass


@dataclass
class DetectResult:
    start_index: int
    end_index: int
    consecutive_days: int
    total_change_pct: Optional[float]


def detect(
    closes: List[float],
    required_days: int,
    direction: str,          # "UP" or "DOWN"
    allowed_exceptions: int,
    include_today: bool = True,   # 서버는 장 마감 후 실행 → 당일 포함
) -> Optional[DetectResult]:
    """
    앱 TrendDetector.detect()와 동일 로직.
    include_today=True 이면 마지막 종가(오늘)까지 포함.
    """
    # 장 마감 후 실행이므로 기본 include_today=True (dropLast 안 함)
    effective = closes if include_today else closes[:-1] if len(closes) > required_days + 1 else closes

    if len(effective) < required_days + 1:
        return None

    # 등락 방향 매칭 리스트
    matches = []
    for i in range(1, len(effective)):
        diff = effective[i] - effective[i - 1]
        if direction == "UP":
            matches.append(diff > 0)
        else:  # DOWN
            matches.append(diff < 0)

    # 마지막 날이 방향 불일치면 탐지 불가
    if not matches[-1]:
        return None

    # 최신부터 역방향 탐색
    consec_count = 0
    exception_count = 0
    end_idx = len(matches) - 1
    start_idx = end_idx

    i = end_idx
    while i >= 0:
        if matches[i]:
            consec_count += 1
            start_idx = i
            i -= 1
        else:
            exception_count += 1
            if exception_count > allowed_exceptions:
                break
            i -= 1

    if consec_count < required_days:
        return None

    base = effective[start_idx]
    end = effective[end_idx + 1]
    total_change = ((end - base) / base) * 100.0 if base != 0 else None

    return DetectResult(
        start_index=start_idx + 1,
        end_index=end_idx + 1,
        consecutive_days=consec_count,
        total_change_pct=total_change,
    )

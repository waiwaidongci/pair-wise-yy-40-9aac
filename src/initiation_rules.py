"""加固立项台的纯判断规则。

立项判断（同意/资金/疏散容量、可调整窗口）全部集中在这里，
不读写任何存档，便于单独测试，与“存档”“操作页面”分开承担。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .domain import ValidationError

TITLE = '加固立项台'
ENTITY = '加固立项'
ID_PREFIX = 'RF'

# 待立项：同意缺失或资金不足，或疏散容量不满足，都只能留在待立项。
# 已立项：批复通过；改动批复值后回到待立项并留下失效的原批复。
PENDING = 'pending'
APPROVED = 'approved'
STATUS = [PENDING, APPROVED]

APPROVE_ROLES = set(['review_board'])
CREATE_ROLES = set(['initiator', 'review_board'])
ZONE_ROLES = set(['initiator', 'review_board'])
VIEW_ROLES = set(['initiator', 'review_board', 'viewer', 'assessor', 'structural_engineer'])

# 可调整窗口的搜索半径（向原窗口前后各滑动这么多天）。
WINDOW_SEARCH_DAYS = 90
MAX_SUGGESTIONS = 3

CONSENT_BLOCKER = '产权单位尚未签署加固同意书'


def funding_gap(estimated_cost: int, available_fund: int) -> int:
    """资金缺口=估价-可动用维修资金；不足为正时即为卡点。"""
    return int(estimated_cost) - int(available_fund)


def parse_iso(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError("日期必须是YYYY-MM-DD") from exc


def windows_overlap(start_a: date, end_a: date,
                    start_b: date, end_b: date) -> bool:
    """施工窗口按[起,止)半开区间判重叠。"""
    return start_a < end_b and start_b < end_a


def window_overlaps_iso(proj: Dict[str, Any], start: date, end: date) -> bool:
    return windows_overlap(parse_iso(proj['start_date']),
                           parse_iso(proj['end_date']), start, end)


def peak_overlap_headcount(project: Dict[str, Any],
                           approved: Iterable[Dict[str, Any]]) -> int:
    """同一疏散分区内，与给定窗口重叠的已立项项目的最大同时在置人数。"""
    start = parse_iso(project['start_date'])
    end = parse_iso(project['end_date'])
    zone = project['zone_code']
    touches: List[Tuple[date, int]] = []
    for other in approved:
        if other.get('id') == project.get('id'):
            continue
        if other['zone_code'] != zone:
            continue
        other_start = parse_iso(other['start_date'])
        other_end = parse_iso(other['end_date'])
        if windows_overlap(start, end, other_start, other_end):
            touches.append((other_start, int(other['resettlement_people'])))
            touches.append((other_end, -int(other['resettlement_people'])))
    if not touches:
        return 0
    touches.sort(key=lambda item: item[0])
    peak = current = 0
    day = touches[0][0]
    same_day = 0
    for at, delta in touches:
        if at != day:
            current += same_day
            peak = max(peak, current)
            day, same_day = at, delta
        else:
            same_day += delta
    current += same_day
    return max(peak, current)


def candidate_load(cand_start: date, cand_end: date, zone: str,
                   approved: Iterable[Dict[str, Any]],
                   exclude_id: Optional[int] = None) -> int:
    """候选窗口在每个重叠项目的边界日扫描，取同时在置人数峰值。"""
    boundaries = {cand_start, cand_end}
    peers: List[Tuple[date, date, int]] = []
    for other in approved:
        if other.get('id') == exclude_id:
            continue
        if other['zone_code'] != zone:
            continue
        other_start = parse_iso(other['start_date'])
        other_end = parse_iso(other['end_date'])
        if windows_overlap(cand_start, cand_end, other_start, other_end):
            peers.append((other_start, other_end, int(other['resettlement_people'])))
            boundaries.add(other_start)
            boundaries.add(other_end)
    peak = 0
    for boundary in boundaries:
        if not (cand_start <= boundary < cand_end):
            continue
        load = sum(people for start, end, people in peers
                   if start <= boundary < end)
        peak = max(peak, load)
    return peak


def suggest_windows(project: Dict[str, Any], capacity: Optional[int],
                    approved: Iterable[Dict[str, Any]]) -> List[Dict[str, str]]:
    """容量冲突时给出保持工期不变、整体滑动的可调整窗口。

    只解决容量卡点（同意、资金无法靠挪窗口解决）；无容量或本项目人数本身
    超过分区容量时返回空列表。
    """
    if capacity is None:
        return []
    people = int(project['resettlement_people'])
    if people > int(capacity):
        return []
    duration = (parse_iso(project['end_date'])
                - parse_iso(project['start_date'])).days
    if duration <= 0:
        return []
    original_start = parse_iso(project['start_date'])
    base_feasible = candidate_load(original_start,
                                   original_start + timedelta(days=duration),
                                   project['zone_code'], approved,
                                   project.get('id')) + people <= int(capacity)
    found: List[Tuple[int, date]] = []
    for offset in range(1, WINDOW_SEARCH_DAYS + 1):
        for cand_start in (original_start - timedelta(days=offset),
                          original_start + timedelta(days=offset)):
            cand_end = cand_start + timedelta(days=duration)
            load = candidate_load(cand_start, cand_end, project['zone_code'],
                                  approved, project.get('id'))
            if load + people <= int(capacity):
                found.append((offset, cand_start))
        if len(found) >= MAX_SUGGESTIONS:
            break
    suggestions: List[Dict[str, str]] = []
    for _, cand_start in found[:MAX_SUGGESTIONS]:
        suggestions.append({
            'start_date': cand_start.isoformat(),
            'end_date': (cand_start + timedelta(days=duration)).isoformat(),
        })
    return [] if base_feasible else suggestions


def evaluate(project: Dict[str, Any], capacity: Optional[int],
             approved: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """立项判断：返回卡点列表与可调整窗口；无卡点才可批准。"""
    blockers: List[str] = []
    if not bool(project.get('owner_consent')):
        blockers.append(CONSENT_BLOCKER)
    gap = funding_gap(int(project['estimated_cost']),
                      int(project['available_fund']))
    if gap > 0:
        blockers.append(f'维修资金不足，资金缺口{gap}元')
    capacity_blocker: Optional[str] = None
    peak = peak_overlap_headcount(project, approved)
    total = peak + int(project['resettlement_people'])
    if capacity is None:
        capacity_blocker = '疏散分区容量未登记，无法核对临时安置容量'
    elif total > int(capacity):
        capacity_blocker = (f"同一疏散分区重叠窗口峰值{total}人"
                            f"（已有{peak}人），超过容量{int(capacity)}人")
    if capacity_blocker:
        blockers.append(capacity_blocker)
    suggestions = suggest_windows(project, capacity, approved)
    return {'approvable': not blockers, 'blockers': blockers,
            'funding_gap': gap, 'overlap_peak': peak,
            'window_suggestions': suggestions}


# 批复值：这些登记项一旦在批准后被改动，原批复立即失效并重算。
PROTECTED_FIELDS = ('owner_unit', 'estimated_cost', 'available_fund',
                    'start_date', 'end_date', 'resettlement_people',
                    'zone_code', 'owner_consent')


def approval_snapshot(project: Dict[str, Any]) -> Dict[str, Any]:
    return {field: project[field] for field in PROTECTED_FIELDS}


def changed_protected_fields(before: Dict[str, Any],
                             after: Dict[str, Any]) -> List[str]:
    return [field for field in PROTECTED_FIELDS
            if before.get(field) != after.get(field)]

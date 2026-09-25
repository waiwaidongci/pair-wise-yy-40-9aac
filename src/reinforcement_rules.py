"""加固立项规则：纯函数判断，不涉及存储与HTTP。

立项卡点有三类：
- owner_consent：产权单位未书面同意；
- funding：可用维修资金不足以覆盖估价（funding_gap>0）；
- capacity：同一疏散分区内，施工窗口重叠的在批安置人数超过分区容量。

只有三类卡点全部为空时项目才能从“待立项”进入“已批准”。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from .domain import ValidationError, require_date

ENTITY = '加固立项'

PENDING = 'pending'      # 待立项
APPROVED = 'approved'    # 已批准
PROJECT_STATES = [PENDING, APPROVED]

# 批准后一旦被改动即失效的字段（立项批复覆盖的判断值）
GUARDED_FIELDS = [
    'owner_name', 'owner_consent', 'estimate', 'funds_available',
    'zone', 'construction_start', 'construction_end', 'resettlement_count',
]

CREATE_ROLES = {'assessor', 'structural_engineer'}
APPROVE_ROLES = {'review_board'}
ZONE_ROLES = {'review_board'}
VIEW_ROLES = {'assessor', 'structural_engineer', 'review_board', 'viewer'}

# 在原窗口前后搜索可调整窗口的最大天数
SEARCH_HORIZON_DAYS = 400


def funding_gap(estimate: float, funds_available: float) -> float:
    return round(max(0.0, float(estimate) - float(funds_available)), 2)


def windows_overlap(start_a: str, end_a: str, start_b: str, end_b: str) -> bool:
    """闭区间施工窗口是否重叠（首尾相接不算重叠）。"""
    return not (end_a < start_b or end_b < start_a)


def _peak_occupancy(projects: List[Dict[str, Any]], start: str, end: str,
                    exclude_id: Optional[int] = None) -> int:
    """窗口内任一时刻在批项目（排除自身）的安置人数峰值。"""
    occupants: Dict[str, int] = {}
    for other in projects:
        if other['id'] == exclude_id or other['status'] != APPROVED:
            continue
        if not windows_overlap(start, end, other['construction_start'],
                               other['construction_end']):
            continue
        day = date.fromisoformat(max(start, other['construction_start']))
        last = date.fromisoformat(min(end, other['construction_end']))
        while day <= last:
            key = day.isoformat()
            occupants[key] = occupants.get(key, 0) + int(other['resettlement_count'])
            day += timedelta(days=1)
    return max(occupants.values()) if occupants else 0


def feasible_window(zone_capacity: Optional[int], count: int,
                    start: str, end: str, projects: List[Dict[str, Any]],
                    exclude_id: Optional[int] = None,
                    horizon_days: int = SEARCH_HORIZON_DAYS) -> Optional[Dict[str, str]]:
    """从原开工日起逐日顺延，返回第一个容量达标的等长窗口；原窗口可行时原样返回。

    返回 None 表示在搜索范围内找不到可调整窗口（如分区无容量或单项目即超容量）。
    """
    if zone_capacity is None or count > zone_capacity:
        return None
    span = (date.fromisoformat(end) - date.fromisoformat(start)).days
    base = date.fromisoformat(start)
    for offset in range(horizon_days + 1):
        cand_start = (base + timedelta(days=offset)).isoformat()
        cand_end = (base + timedelta(days=offset + span)).isoformat()
        if _peak_occupancy(projects, cand_start, cand_end, exclude_id) + count <= zone_capacity:
            return {'construction_start': cand_start, 'construction_end': cand_end}
    return None


def evaluate_project(project: Dict[str, Any],
                     zone_capacity: Optional[int],
                     approved_projects: List[Dict[str, Any]]) -> Dict[str, Any]:
    """计算资金缺口、立项卡点与可调整窗口（立项判断的唯一入口）。"""
    gap = funding_gap(project['estimate'], project['funds_available'])
    blockers: List[Dict[str, str]] = []
    if not project['owner_consent']:
        blockers.append({'code': 'owner_consent',
                         'message': f"产权单位未书面同意（{project['owner_name']}）" if project['owner_name']
                                    else '产权单位未书面同意'})
    if gap > 0:
        blockers.append({'code': 'funding',
                         'message': f'维修资金不足，资金缺口{gap:.2f}元'})
    suggestion: Optional[Dict[str, str]] = None
    if zone_capacity is None:
        blockers.append({'code': 'capacity',
                         'message': f"疏散分区{project['zone']}未登记安置容量"})
    else:
        peak = _peak_occupancy(approved_projects, project['construction_start'],
                               project['construction_end'], project.get('id'))
        total = peak + int(project['resettlement_count'])
        if total > zone_capacity:
            blockers.append({
                'code': 'capacity',
                'message': (f"疏散分区{project['zone']}重叠窗口安置{total}人，"
                            f'超过容量{zone_capacity}人'),
            })
            suggestion = feasible_window(zone_capacity,
                                         int(project['resettlement_count']),
                                         project['construction_start'],
                                         project['construction_end'],
                                         approved_projects, project.get('id'))
    return {
        'funding_gap': gap,
        'zone_capacity': zone_capacity,
        'blockers': blockers,
        'blocker_codes': [b['code'] for b in blockers],
        'approvable': not blockers and project['status'] == PENDING,
        'suggested_window': suggestion,
    }


def parse_window(payload: Dict[str, Any]) -> Dict[str, str]:
    start = require_date(payload.get('construction_start'), 'construction_start')
    end = require_date(payload.get('construction_end'), 'construction_end')
    if end < start:
        raise ValidationError('construction_end不能早于construction_start')
    return {'construction_start': start, 'construction_end': end}

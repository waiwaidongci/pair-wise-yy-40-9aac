"""加固立项台用例编排：把立项判断、存档和操作请求串起来。"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from .domain import (ConflictError, ensure_role, require_bool, require_int,
                     require_iso_date, require_text)
from .initiation_rules import (APPROVE_ROLES, CREATE_ROLES,
                               ENTITY, ID_PREFIX, PROTECTED_FIELDS, VIEW_ROLES,
                               ZONE_ROLES, approval_snapshot,
                               changed_protected_fields, evaluate)
from .repository import Repository


class DecisionConflict(ConflictError):
    """立项判断未通过：附带卡点与可调整窗口供操作页面展示。"""

    def __init__(self, result: Dict[str, Any]):
        super().__init__("；".join(result["blockers"]))
        self.blockers = result["blockers"]
        self.window_suggestions = result["window_suggestions"]


EDITABLE_FIELDS = ("title", "owner_unit", "estimated_cost", "available_fund",
                   "start_date", "end_date", "resettlement_people", "zone_code")


class InitiationService:
    def __init__(self, repository: Repository):
        self.repository = repository

    # -- 登记 ------------------------------------------------------------
    def register(self, payload: Dict[str, Any], actor: str,
                 role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        data = self._validate(payload, partial=False)
        project = self.repository.create_initiation_project(data, actor)
        self.repository.append_audit("register", ENTITY, project["id"], actor, {
            "ref": ID_PREFIX + str(project["id"]),
            "owner_unit": project["owner_unit"],
            "estimated_cost": project["estimated_cost"],
            "funding_gap": project["estimated_cost"] - project["available_fund"],
            "zone_code": project["zone_code"],
        })
        return self.view(project)

    def revise(self, project_id: int, payload: Dict[str, Any], actor: str,
               role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        expected_version = payload.get("expected_version")
        if not isinstance(expected_version, int) or expected_version < 1:
            from .domain import ValidationError
            raise ValidationError("expected_version必须是正整数")
        current = self.repository.get_initiation_project(project_id)
        merged = dict(current)
        merged.update({key: payload[key] for key in EDITABLE_FIELDS
                       if key in payload})
        if "owner_consent" in payload:
            merged["owner_consent"] = payload["owner_consent"]
        data = self._validate(merged, partial=True)
        changed = changed_protected_fields(current, data)
        invalidate = current["status"] == "approved" and bool(changed)
        reason = ("批复登记值被改动：" + "、".join(changed)
                  if changed else "登记信息修订")
        updated = self.repository.revise_initiation_project(
            project_id, data, expected_version, actor, invalidate, reason)
        if invalidate:
            self.repository.append_audit("invalidate", ENTITY, project_id,
                                         actor, {"changed_fields": changed})
        self.repository.append_audit("revise", ENTITY, project_id, actor, {
            "changed_fields": changed, "approval_invalidated": invalidate,
        })
        return self.view(updated)

    # -- 批准 ------------------------------------------------------------
    def approve(self, project_id: int, expected_version: int, actor: str,
                role: str) -> Dict[str, Any]:
        ensure_role(role, APPROVE_ROLES)
        actor = require_text(actor, "actor", 100)
        if not isinstance(expected_version, int) or expected_version < 1:
            from .domain import ValidationError
            raise ValidationError("expected_version必须是正整数")
        project = self.repository.get_initiation_project(project_id)
        if project["version"] != expected_version:
            raise ConflictError("版本冲突，请刷新后重试")
        if project["status"] == "approved" \
                and self.repository.get_valid_approval(project_id):
            raise ConflictError("该项目已有有效批复")
        result = self._decision(project)
        if not result["approvable"]:
            self.repository.append_audit("approve_rejected", ENTITY,
                                         project_id, actor,
                                         {"blockers": result["blockers"]})
            raise DecisionConflict(result)
        self.repository.mark_project_approved(project_id)
        self.repository.add_approval(project_id,
                                     approval_snapshot(project), actor)
        self.repository.append_audit("approve", ENTITY, project_id, actor, {
            "zone_code": project["zone_code"],
            "resettlement_people": project["resettlement_people"],
            "window": [project["start_date"], project["end_date"]],
        })
        return self.view(self.repository.get_initiation_project(project_id))

    # -- 查询 ------------------------------------------------------------
    def list_projects(self, role: str) -> List[Dict[str, Any]]:
        ensure_role(role, VIEW_ROLES)
        projects = self.repository.list_initiation_projects()
        return [self.view(project) for project in projects]

    def get_project(self, project_id: int, role: str) -> Dict[str, Any]:
        ensure_role(role, VIEW_ROLES)
        return self.view(self.repository.get_initiation_project(project_id))

    def list_approvals(self, role: str,
                       project_id: Optional[int] = None) -> List[Dict[str, Any]]:
        ensure_role(role, VIEW_ROLES)
        return self.repository.list_approvals(project_id)

    # -- 疏散分区容量 ----------------------------------------------------
    def list_zones(self, role: str) -> List[Dict[str, Any]]:
        ensure_role(role, VIEW_ROLES)
        return self.repository.list_zones()

    def upsert_zone(self, payload: Dict[str, Any], actor: str,
                    role: str) -> Dict[str, Any]:
        ensure_role(role, ZONE_ROLES)
        actor = require_text(actor, "actor", 100)
        zone_code = require_text(payload.get("zone_code"), "zone_code", 50)
        capacity = require_int(payload.get("capacity"), "capacity", 0)
        zone = self.repository.upsert_zone(zone_code, capacity, actor)
        self.repository.append_audit("zone_upsert", ENTITY, 0, actor, {
            "zone_code": zone_code, "capacity": capacity,
        })
        return zone

    # -- 内部 ------------------------------------------------------------
    def _validate(self, payload: Dict[str, Any], partial: bool) -> Dict[str, Any]:
        def pick(name: str):
            value = payload.get(name)
            if not partial and value is None:
                from .domain import ValidationError
                raise ValidationError(f"{name}不能为空")
            return value

        data: Dict[str, Any] = {}
        data["title"] = require_text(pick("title"), "title", 200)
        data["owner_unit"] = require_text(pick("owner_unit"), "owner_unit", 200)
        data["estimated_cost"] = require_int(pick("estimated_cost"),
                                             "estimated_cost", 0)
        data["available_fund"] = require_int(pick("available_fund"),
                                             "available_fund", 0)
        start = require_iso_date(pick("start_date"), "start_date")
        end = require_iso_date(pick("end_date"), "end_date")
        if date.fromisoformat(start) >= date.fromisoformat(end):
            from .domain import ValidationError
            raise ValidationError("施工止日必须晚于起日")
        data["start_date"] = start
        data["end_date"] = end
        data["resettlement_people"] = require_int(
            pick("resettlement_people"), "resettlement_people", 0)
        data["zone_code"] = require_text(pick("zone_code"), "zone_code", 50)
        consent = pick("owner_consent")
        if consent is None and partial:
            consent = False
        data["owner_consent"] = require_bool(consent, "owner_consent")
        if "external_ref" in payload and payload["external_ref"] is not None:
            data["external_ref"] = require_text(payload["external_ref"],
                                                "external_ref", 100)
        return data

    def _decision(self, project: Dict[str, Any]) -> Dict[str, Any]:
        zone = self.repository.get_zone(project["zone_code"])
        capacity = zone["capacity"] if zone else None
        approved = [other for other in
                    self.repository.list_approved_projects(project["zone_code"])
                    if other["id"] != project["id"]]
        result = evaluate(project, capacity, approved)
        result["zone_registered"] = zone is not None
        result["zone_capacity"] = capacity
        return result

    def view(self, project: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(project)
        result["ref"] = ID_PREFIX + str(project["id"])
        result["funding_gap"] = (project["estimated_cost"]
                                 - project["available_fund"])
        latest = self.repository.list_approvals(project["id"])
        result["approval"] = None
        if latest:
            record = latest[0]
            if record["valid"] and project["status"] == "approved":
                # 存档中的登记值若已偏离批复快照，原批复失效并需要重算。
                drifted = changed_protected_fields(record["snapshot"], project)
                result["approval"] = {
                    "id": record["id"], "decided_by": record["decided_by"],
                    "decided_at": record["decided_at"],
                    "valid": not drifted, "drifted_fields": drifted,
                }
            else:
                drifted = changed_protected_fields(record["snapshot"], project)
                result["approval"] = {
                    "id": record["id"], "decided_by": record["decided_by"],
                    "decided_at": record["decided_at"], "valid": False,
                    "invalidation_reason": record["invalidation_reason"],
                    "drifted_fields": drifted,
                }
        decision = self._decision(project)
        result["approvable"] = decision["approvable"]
        result["blockers"] = decision["blockers"]
        result["overlap_peak"] = decision["overlap_peak"]
        result["zone_capacity"] = decision["zone_capacity"]
        result["zone_registered"] = decision["zone_registered"]
        result["window_suggestions"] = decision["window_suggestions"]
        return result


def seed_demo(service: InitiationService, actor: str = "demo") -> None:
    """灌入演示数据：一个可批、一个缺同意、一个资金不足、一个容量超限。"""
    if service.repository.list_initiation_projects():
        return
    service.upsert_zone({"zone_code": "Z-A", "capacity": 100}, actor,
                        "review_board")
    today = date.today()

    def at(offset: int) -> str:
        return (today + timedelta(days=offset)).isoformat()

    ready = service.register({
        "title": "甲号楼加固", "owner_unit": "甲产权单位",
        "owner_consent": True, "estimated_cost": 800000,
        "available_fund": 800000, "start_date": at(10), "end_date": at(40),
        "resettlement_people": 30, "zone_code": "Z-A",
        "external_ref": "RF-DEMO-1"}, actor, "initiator")
    service.register({
        "title": "乙号楼加固", "owner_unit": "乙产权单位",
        "owner_consent": False, "estimated_cost": 500000,
        "available_fund": 500000, "start_date": at(15), "end_date": at(35),
        "resettlement_people": 20, "zone_code": "Z-A",
        "external_ref": "RF-DEMO-2"}, actor, "initiator")
    service.register({
        "title": "丙号楼加固", "owner_unit": "丙产权单位",
        "owner_consent": True, "estimated_cost": 900000,
        "available_fund": 600000, "start_date": at(20), "end_date": at(50),
        "resettlement_people": 10, "zone_code": "Z-A",
        "external_ref": "RF-DEMO-3"}, actor, "initiator")
    service.register({
        "title": "丁号楼加固", "owner_unit": "丁产权单位",
        "owner_consent": True, "estimated_cost": 400000,
        "available_fund": 400000, "start_date": at(12), "end_date": at(42),
        "resettlement_people": 80, "zone_code": "Z-A",
        "external_ref": "RF-DEMO-4"}, actor, "initiator")
    service.approve(ready["id"], ready["version"], actor, "review_board")

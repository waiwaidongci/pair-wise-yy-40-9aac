from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ConflictError, ensure_role, normalize_severity,
                     require_bool, require_int, require_number, require_text)
from . import reinforcement_rules as rr
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, RECORD_ROLES, TITLE,
                    VIEW_ROLES, completion_blockers, escalation_required,
                    priority_score, response_deadline_hours, role_for_transition,
                    validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            from .domain import ConflictError
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result

    # ---- 加固立项台 ----

    def _parse_reinforcement_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        data = {
            "building_name": require_text(payload.get("building_name"), "building_name", 200),
            "owner_name": require_text(payload.get("owner_name"), "owner_name", 200),
            "owner_consent": require_bool(payload.get("owner_consent", False), "owner_consent"),
            "estimate": require_number(payload.get("estimate", 0), "estimate"),
            "funds_available": require_number(payload.get("funds_available", 0), "funds_available"),
            "zone": require_text(payload.get("zone"), "zone", 100),
            "resettlement_count": require_int(
                payload.get("resettlement_count", 0), "resettlement_count"),
        }
        data.update(rr.parse_window(payload))
        return data

    def _evaluate(self, project: Dict[str, Any]) -> Dict[str, Any]:
        zone = self.repository.get_zone(project["zone"])
        capacity = zone["capacity"] if zone else None
        approved = self.repository.list_approved_in_zone(
            project["zone"], exclude_id=project["id"])
        return rr.evaluate_project(project, capacity, approved)

    def _enrich_reinforcement(self, project: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(project)
        result.update(self._evaluate(project))
        return result

    def register_zone(self, payload: Dict[str, Any], actor: str,
                      role: str) -> Dict[str, Any]:
        ensure_role(role, rr.ZONE_ROLES)
        actor = require_text(actor, "actor", 100)
        zone = require_text(payload.get("zone"), "zone", 100)
        capacity = require_int(payload.get("capacity"), "capacity")
        saved = self.repository.upsert_zone(zone, capacity, actor)
        self.repository.append_audit("zone_capacity", rr.ENTITY, 0, actor, {
            "zone": zone, "capacity": capacity,
        })
        return saved

    def list_zones(self, role: str) -> list:
        self._view(role)
        return self.repository.list_zones()

    def create_reinforcement(self, payload: Dict[str, Any], actor: str,
                             role: str) -> Dict[str, Any]:
        ensure_role(role, rr.CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        data = self._parse_reinforcement_payload(payload)
        project = self.repository.create_reinforcement(data, actor)
        self.repository.append_audit("reinforcement_create", rr.ENTITY,
                                     project["id"], actor, {
            "building_name": data["building_name"], "owner_name": data["owner_name"],
            "estimate": data["estimate"], "funds_available": data["funds_available"],
        })
        return self._enrich_reinforcement(project)

    def list_reinforcement(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        if status and status not in rr.PROJECT_STATES:
            from .domain import ValidationError
            raise ValidationError("status必须是pending或approved")
        return [self._enrich_reinforcement(p)
                for p in self.repository.list_reinforcement(status)]

    def get_reinforcement(self, project_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self._enrich_reinforcement(self.repository.get_reinforcement(project_id))

    def update_reinforcement(self, project_id: int, payload: Dict[str, Any],
                             actor: str, role: str) -> Dict[str, Any]:
        """登记值被改动；若原已批准，改动任一受护字段即令原批复失效并回到待立项重算。"""
        ensure_role(role, rr.CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        expected_version = require_int(payload.get("expected_version"),
                                       "expected_version", minimum=1)
        before = self.repository.get_reinforcement(project_id)
        data = self._parse_reinforcement_payload({
            "building_name": payload.get("building_name", before["building_name"]),
            "owner_name": payload.get("owner_name", before["owner_name"]),
            "owner_consent": payload.get("owner_consent", before["owner_consent"]),
            "estimate": payload.get("estimate", before["estimate"]),
            "funds_available": payload.get("funds_available", before["funds_available"]),
            "zone": payload.get("zone", before["zone"]),
            "construction_start": payload.get("construction_start", before["construction_start"]),
            "construction_end": payload.get("construction_end", before["construction_end"]),
            "resettlement_count": payload.get("resettlement_count",
                                              before["resettlement_count"]),
        })
        changed = [field for field in rr.GUARDED_FIELDS if data[field] != before[field]]
        void_reason = None
        if before["status"] == rr.APPROVED and changed:
            void_reason = "批准后登记值发生变更：" + ",".join(changed)
        updated = self.repository.update_reinforcement(
            project_id, data, expected_version, actor, void_reason)
        if void_reason:
            self.repository.append_audit("reinforcement_void", rr.ENTITY,
                                         project_id, actor, {
                "reason": void_reason,
                "changed_fields": changed,
                "approval_snapshot": before["approval_snapshot"],
            })
        self.repository.append_audit("reinforcement_update", rr.ENTITY,
                                     project_id, actor, {
            "changed_fields": changed, "voided": bool(void_reason),
            "version": updated["version"],
        })
        return self._enrich_reinforcement(updated)

    def approve_reinforcement(self, project_id: int, expected_version: int,
                              actor: str, role: str) -> Dict[str, Any]:
        """批准立项：三类卡点必须全部为空，容量不足时返回可调整窗口。"""
        ensure_role(role, rr.APPROVE_ROLES)
        actor = require_text(actor, "actor", 100)
        if not isinstance(expected_version, int) or expected_version < 1:
            from .domain import ValidationError
            raise ValidationError("expected_version必须是正整数")
        project = self.repository.get_reinforcement(project_id)
        if project["status"] != rr.PENDING:
            raise ConflictError("项目已批准，登记值变更后需重新立项")
        evaluation = self._evaluate(project)
        if evaluation["blockers"]:
            raise ConflictError("立项卡点未消除，不能批准", details={
                "blockers": evaluation["blockers"],
                "suggested_window": evaluation["suggested_window"],
            })
        snapshot = {field: project[field] for field in rr.GUARDED_FIELDS}
        snapshot["funding_gap"] = evaluation["funding_gap"]
        approved = self.repository.approve_reinforcement(
            project_id, snapshot, expected_version, actor)
        self.repository.append_audit("reinforcement_approve", rr.ENTITY,
                                     project_id, actor, {
            "snapshot": snapshot, "version": approved["version"],
        })
        return self._enrich_reinforcement(approved)

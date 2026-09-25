from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from .domain import (ConflictError, DomainError, NotFoundError, PermissionDenied,
                     ValidationError)
from .initiation_service import DecisionConflict, InitiationService
from .service import Service


def make_handler(service: Service, static_dir: str,
                 initiation: InitiationService = None):
    root = Path(static_dir)

    class Handler(BaseHTTPRequestHandler):
        server_version = "ModularHell/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _html(self, path: Path) -> None:
            if not path.exists():
                self._json(404, {"error": "not_found"})
                return
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _identity(self) -> Tuple[str, str]:
            return self.headers.get("X-Actor", ""), self.headers.get("X-Role", "")

        def _body(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length <= 0:
                return {}
            if length > 2_000_000:
                raise ValidationError("请求体过大")
            try:
                value = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError("请求体不是有效JSON") from exc
            if not isinstance(value, dict):
                raise ValidationError("请求体必须是JSON对象")
            return value

        def _send_error(self, exc: Exception) -> None:
            if isinstance(exc, ValidationError):
                status = 422
            elif isinstance(exc, NotFoundError):
                status = 404
            elif isinstance(exc, PermissionDenied):
                status = 403
            elif isinstance(exc, DecisionConflict):
                status = 409
            elif isinstance(exc, ConflictError):
                status = 409
            elif isinstance(exc, ValueError):
                status = 422
            elif isinstance(exc, DomainError):
                status = 400
            else:
                status = 500
            payload: Dict[str, Any] = {"error": exc.__class__.__name__,
                                       "message": str(exc)}
            if isinstance(exc, DecisionConflict):
                payload["blockers"] = exc.blockers
                payload["window_suggestions"] = exc.window_suggestions
            self._json(status, payload)

        def do_GET(self) -> None:
            try:
                path = urlparse(self.path).path
                if path == "/health":
                    self._json(200, {"status": "ok"})
                elif path == "/":
                    self._html(root / "index.html")
                elif path == "/initiation":
                    self._html(root / "initiation.html")
                elif path == "/api/items":
                    actor, role = self._identity()
                    del actor
                    self._json(200, {"items": service.list_items(role)})
                elif path.startswith("/api/items/") and path.endswith("/records"):
                    item_id = int(path.split("/")[3])
                    actor, role = self._identity()
                    del actor
                    self._json(200, {"records": service.list_records(item_id, role)})
                elif path.startswith("/api/items/"):
                    item_id = int(path.rsplit("/", 1)[-1])
                    actor, role = self._identity()
                    del actor
                    self._json(200, service.get_item(item_id, role))
                elif path == "/api/audit":
                    actor, role = self._identity()
                    del actor
                    self._json(200, {"events": service.audit(role)})
                elif initiation is not None and self._initiation_get(path):
                    return
                else:
                    self._json(404, {"error": "not_found"})
            except Exception as exc:
                self._send_error(exc)

        def _initiation_get(self, path: str) -> bool:
            actor, role = self._identity()
            parts = [part for part in path.split("/") if part]
            if parts[:2] == ["api", "initiation"]:
                if len(parts) == 3 and parts[2] == "projects":
                    self._json(200, {"projects": initiation.list_projects(role)})
                elif len(parts) == 4 and parts[2] == "projects":
                    self._json(200, initiation.get_project(int(parts[3]), role))
                elif len(parts) == 3 and parts[2] == "approvals":
                    self._json(200, {"approvals": initiation.list_approvals(role)})
                elif len(parts) == 5 and parts[2] == "projects" \
                        and parts[4] == "approvals":
                    self._json(200, {"approvals": initiation.list_approvals(
                        role, int(parts[3]))})
                elif len(parts) == 3 and parts[2] == "zones":
                    self._json(200, {"zones": initiation.list_zones(role)})
                else:
                    self._json(404, {"error": "not_found"})
                return True
            return False

        def do_POST(self) -> None:
            try:
                path = urlparse(self.path).path
                actor, role = self._identity()
                body = self._body()
                if path == "/api/items":
                    self._json(201, service.create_item(body, actor, role))
                elif path.startswith("/api/items/") and path.endswith("/records"):
                    item_id = int(path.split("/")[3])
                    self._json(201, service.add_record(item_id, body, actor, role))
                elif path.startswith("/api/items/") and path.endswith("/transition"):
                    item_id = int(path.split("/")[3])
                    target = body.get("target")
                    expected = body.get("expected_version")
                    self._json(200, service.transition(
                        item_id, target, expected, actor, role))
                elif initiation is not None and \
                        self._initiation_post(path, body, actor, role):
                    return
                else:
                    self._json(404, {"error": "not_found"})
            except Exception as exc:
                self._send_error(exc)

        def _initiation_post(self, path: str, body: Dict[str, Any],
                             actor: str, role: str) -> bool:
            parts = [part for part in path.split("/") if part]
            if parts[:2] != ["api", "initiation"]:
                return False
            if len(parts) == 3 and parts[2] == "projects":
                self._json(201, initiation.register(body, actor, role))
            elif len(parts) == 4 and parts[2] == "projects":
                self._json(200, initiation.revise(int(parts[3]), body,
                                                  actor, role))
            elif len(parts) == 5 and parts[2] == "projects" \
                    and parts[4] == "approvals":
                self._json(200, initiation.approve(
                    int(parts[3]), body.get("expected_version"), actor, role))
            elif len(parts) == 3 and parts[2] == "zones":
                self._json(200, initiation.upsert_zone(body, actor, role))
            else:
                self._json(404, {"error": "not_found"})
            return True

    return Handler

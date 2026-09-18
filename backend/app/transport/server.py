"""标准库 HTTP 服务。"""

from __future__ import annotations

import json
import inspect
import logging
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..application import (
    BriefService,
    CatalogService,
    ComparisonService,
    ObservationService,
)
from ..config import RuntimeConfig
from ..errors import DomainError
from ..jobs import JobService
from ..persistence import Repository
from ..persistence.repository import request_fingerprint
from ..security import (
    AuthorizationService,
    IdentityService,
    RequestContext,
    request_scope,
)
from .handlers import ApiHandlers, build_router
from .router import Handler, Router


LOGGER = logging.getLogger("orchard-phenology-atlas")


class AtlasHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        *,
        router: Router,
        config: RuntimeConfig,
        repository: Repository,
        authorization: AuthorizationService,
    ) -> None:
        super().__init__(server_address, handler_class)
        self.router = router
        self.config = config
        self.repository = repository
        self.authorization = authorization


class AtlasRequestHandler(BaseHTTPRequestHandler):
    server: AtlasHTTPServer
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self._handle_request("GET")

    def do_PUT(self) -> None:
        self._handle_request("PUT")

    def do_PATCH(self) -> None:
        self._handle_request("PATCH")

    def do_DELETE(self) -> None:
        self._handle_request("DELETE")

    def log_message(self, format_string: str, *args: object) -> None:
        LOGGER.info(
            "%s - %s",
            self.address_string(),
            format_string % args,
        )

    def _handle_request(self, method: str) -> None:
        try:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query, keep_blank_values=False)
            route, params = self.server.router.resolve(method, parsed.path)
            body = (
                self._read_json_body()
                if method in {"PUT", "PATCH", "DELETE"}
                else {}
            )
            actor_id = self.headers.get("X-Actor-Id", "").strip()
            idempotency_key = self.headers.get("X-Idempotency-Key", "").strip() or None
            if route.capability is not None:
                self.server.authorization.require(
                    actor_id=actor_id,
                    capability=route.capability,
                    resource_kind=route.resource_kind or "unknown",
                    resource_id=_resource_id(route, params, body),
                )
            context = RequestContext(
                actor_id=actor_id or "anonymous",
                idempotency_key=idempotency_key,
                request_method=method,
                request_path=parsed.path,
                request_hash=request_fingerprint(
                    actor_id=actor_id or "anonymous",
                    method=method,
                    path=parsed.path,
                    body=body,
                ),
                route_template=route.template,
            )
            with request_scope(context):
                result = self._call_handler(route.handler, params, query, body)
            self._send_json(HTTPStatus.OK, result)
        except DomainError as exc:
            self._send_json(exc.status, exc.to_payload())
        except json.JSONDecodeError:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {
                    "error": {
                        "code": "invalid_json",
                        "message": "请求体不是有效 JSON",
                        "details": {},
                    }
                },
            )
        except Exception as exc:
            LOGGER.exception("未处理的服务异常")
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "error": {
                        "code": "internal_error",
                        "message": "服务处理请求时发生未知错误",
                        "details": {"type": exc.__class__.__name__},
                    }
                },
            )

    def _call_handler(
        self,
        handler: Handler,
        params: dict[str, str],
        query: dict[str, list[str]],
        body: dict[str, Any],
    ) -> object:
        parameters = inspect.signature(handler).parameters
        keywords: dict[str, Any] = {}
        if "params" in parameters:
            keywords["params"] = params
        if "query" in parameters:
            keywords["query"] = query
        if "body" in parameters:
            keywords["body"] = body
        return handler(**keywords)

    def _read_json_body(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return {}
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise DomainError(
                "invalid_content_length",
                "Content-Length 无效",
                400,
            ) from exc
        if length <= 0:
            return {}
        if length > self.server.config.request_limit:
            raise DomainError(
                "request_too_large",
                "请求体超过服务限制",
                413,
                {"limit": self.server.config.request_limit},
            )
        content_type = self.headers.get("Content-Type", "")
        if "application/json" not in content_type.lower():
            raise DomainError(
                "unsupported_media_type",
                "写入请求必须使用 application/json",
                415,
            )
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise DomainError(
                "invalid_encoding",
                "请求体必须使用 UTF-8",
                400,
            ) from exc
        if not isinstance(parsed, dict):
            raise DomainError(
                "invalid_body",
                "请求体顶层必须是 JSON 对象",
                400,
            )
        return parsed

    def _send_json(self, status: int | HTTPStatus, payload: object) -> None:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "private, max-age=0")
        self.end_headers()
        self.wfile.write(encoded)


def create_server(
    config: RuntimeConfig,
    repository: Repository,
) -> AtlasHTTPServer:
    catalog = CatalogService(repository)
    observations = ObservationService(repository)
    comparisons = ComparisonService(repository)
    briefs = BriefService(repository)
    jobs = JobService(repository.database)
    identity = IdentityService(repository.database)

    # 安装实时双写护栏并执行启动恢复；无活动迁移时两者均为低开销空操作。
    from ..migration.live_guard import LiveWriteGuard
    from ..migration.recovery import MigrationRecovery

    repository.live_write_guard = LiveWriteGuard(repository)
    MigrationRecovery(repository).recover()

    handlers = ApiHandlers(
        catalog,
        observations,
        comparisons,
        briefs,
        repository,
        jobs,
        identity,
    )
    router = build_router(handlers)
    authorization = AuthorizationService(repository.database)
    return AtlasHTTPServer(
        (config.host, config.port),
        AtlasRequestHandler,
        router=router,
        config=config,
        repository=repository,
        authorization=authorization,
    )


def _resource_id(
    route: object,
    params: dict[str, str],
    body: dict[str, Any],
) -> str | None:
    parameter = getattr(route, "resource_id_param", None)
    if parameter and parameter in params:
        return params[parameter]
    for key in (
        "plot_id",
        "tree_id",
        "observation_id",
        "comparison_id",
        "brief_id",
        "job_id",
    ):
        value = body.get(key)
        if value:
            return str(value)
    return None

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit


DOCKER_PROXY_HOST = "docker-socket-proxy"
DOCKER_PROXY_PORT = 2375
MAX_DOCKER_RESPONSE_BYTES = 2 * 1024 * 1024
CONTAINER_ID = re.compile(r"[0-9a-f]{12,64}\Z")
FULL_CONTAINER_ID = re.compile(r"[0-9a-f]{64}\Z")
DIGEST_IMAGE = re.compile(
    r"[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}\Z"
)
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")


class InspectorError(RuntimeError):
    pass


def _docker_request(method: str, path: str) -> tuple[int, Any]:
    if method != "GET" or not path.startswith("/") or "\r" in path or "\n" in path:
        raise InspectorError("Docker runtime request was invalid")
    connection = http.client.HTTPConnection(
        DOCKER_PROXY_HOST, DOCKER_PROXY_PORT, timeout=5
    )
    response: http.client.HTTPResponse | None = None
    try:
        connection.request(method, path, headers={"Connection": "close"})
        response = connection.getresponse()
        raw_length = response.getheader("Content-Length")
        if raw_length is not None:
            try:
                content_length = int(raw_length)
            except ValueError as error:
                raise InspectorError("Docker runtime response was invalid") from error
            if content_length < 0 or content_length > MAX_DOCKER_RESPONSE_BYTES:
                raise InspectorError("Docker runtime response exceeded its limit")
        raw = response.read(MAX_DOCKER_RESPONSE_BYTES + 1)
        if len(raw) > MAX_DOCKER_RESPONSE_BYTES:
            raise InspectorError("Docker runtime response exceeded its limit")
        if not raw:
            payload: Any = None
        else:
            try:
                payload = json.loads(raw)
            except (UnicodeError, ValueError) as error:
                raise InspectorError("Docker runtime response was invalid") from error
        return response.status, payload
    except (OSError, http.client.HTTPException) as error:
        raise InspectorError("Docker runtime request failed") from error
    finally:
        if response is not None:
            response.close()
        connection.close()


def _docker_json(path: str) -> Any:
    status, payload = _docker_request("GET", path)
    if status != 200:
        raise InspectorError("Docker runtime inspection failed")
    return payload


def _mapping(value: Any, description: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise InspectorError(f"{description} was invalid")
    return value


def _string(value: Any, description: str) -> str:
    if type(value) is not str or not value or len(value) > 4096:
        raise InspectorError(f"{description} was invalid")
    return value


def _environment(values: Any) -> dict[str, str]:
    if not isinstance(values, list) or len(values) > 1024:
        raise InspectorError("Ollama container environment was invalid")
    result: dict[str, str] = {}
    for item in values:
        if type(item) is not str or "=" not in item or len(item) > 8192:
            raise InspectorError("Ollama container environment was invalid")
        key, value = item.split("=", 1)
        if key in result:
            raise InspectorError("Ollama container environment was ambiguous")
        result[key] = value
    return result


@dataclass(frozen=True)
class InspectedRuntime:
    endpoint: str
    model: str
    runtime_instance_id: str
    runtime_handle: str
    handle_kind: str
    process_id: None
    container_id: str
    container_image_reference: str
    container_image_id: str
    process_started_at: None
    executable_sha256: None
    executable_size: None
    firewall_rule_token: None
    egress_policy: str
    egress_enforced: bool
    cloud_disabled: bool
    isolation_rule_id: str


def inspect_ollama_runtime(model: str) -> InspectedRuntime:
    if MODEL.fullmatch(model) is None or model.startswith((".", "/")) or ".." in model.split("/"):
        raise InspectorError("Requested Ollama model was invalid")
    try:
        self_id = Path("/etc/hostname").read_text(encoding="ascii").strip().casefold()
    except (OSError, UnicodeError) as error:
        raise InspectorError("Inspector container identity was unavailable") from error
    if CONTAINER_ID.fullmatch(self_id) is None:
        raise InspectorError("Inspector container identity was invalid")
    self_inspection = _mapping(
        _docker_json(f"/containers/{quote(self_id, safe='')}/json"),
        "Inspector container metadata",
    )
    self_config = _mapping(self_inspection.get("Config"), "Inspector configuration")
    labels = _mapping(self_config.get("Labels"), "Inspector labels")
    project = _string(labels.get("com.docker.compose.project"), "Compose project")
    filters = json.dumps(
        {
            "label": [
                f"com.docker.compose.project={project}",
                "com.docker.compose.service=ollama",
            ]
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    containers = _docker_json(
        f"/containers/json?all=0&filters={quote(filters, safe='')}"
    )
    if not isinstance(containers, list) or len(containers) != 1:
        raise InspectorError("Compose Ollama container identity was ambiguous")
    summary = _mapping(containers[0], "Ollama container summary")
    container_id = _string(summary.get("Id"), "Ollama container ID").casefold()
    if FULL_CONTAINER_ID.fullmatch(container_id) is None:
        raise InspectorError("Ollama container ID was not concrete")
    inspection = _mapping(
        _docker_json(f"/containers/{container_id}/json"),
        "Ollama container metadata",
    )
    if _string(inspection.get("Id"), "Ollama container ID").casefold() != container_id:
        raise InspectorError("Ollama container identity changed")
    state = _mapping(inspection.get("State"), "Ollama container state")
    process_id = state.get("Pid")
    started_at = _string(state.get("StartedAt"), "Ollama start identity")
    if state.get("Running") is not True or state.get("Status") != "running":
        raise InspectorError("Ollama container was not running")
    if type(process_id) is not int or process_id <= 0:
        raise InspectorError("Ollama container process identity was invalid")
    config = _mapping(inspection.get("Config"), "Ollama configuration")
    image_reference = _string(config.get("Image"), "Ollama image reference")
    image_id = _string(inspection.get("Image"), "Ollama image ID").casefold()
    if (
        DIGEST_IMAGE.fullmatch(image_reference) is None
        or image_reference != image_reference.casefold()
        or IMAGE_ID.fullmatch(image_id) is None
    ):
        raise InspectorError("Ollama image was not immutable")
    environment = _environment(config.get("Env"))
    if (
        environment.get("OLLAMA_NO_CLOUD") != "1"
        or environment.get("OLLAMA_HOST") != "0.0.0.0:11434"
        or environment.get("CAREEROPS_OLLAMA_MODEL") != model
    ):
        raise InspectorError("Ollama cloud or model configuration was invalid")
    host_config = _mapping(inspection.get("HostConfig"), "Ollama host configuration")
    if host_config.get("ReadonlyRootfs") is not True:
        raise InspectorError("Ollama root filesystem was not read-only")
    network_settings = _mapping(
        inspection.get("NetworkSettings"), "Ollama network settings"
    )
    ports = network_settings.get("Ports")
    if not isinstance(ports, dict) or any(value is not None for value in ports.values()):
        raise InspectorError("Ollama published a host port")
    networks = _mapping(network_settings.get("Networks"), "Ollama networks")
    if len(networks) != 1:
        raise InspectorError("Ollama was attached to an unverified network")
    network_name, attachment_value = next(iter(networks.items()))
    attachment = _mapping(attachment_value, "Ollama network attachment")
    network_id = _string(attachment.get("NetworkID"), "Ollama network ID").casefold()
    if FULL_CONTAINER_ID.fullmatch(network_id) is None:
        raise InspectorError("Ollama network identity was invalid")
    network = _mapping(
        _docker_json(f"/networks/{network_id}"), "Ollama network metadata"
    )
    if (
        _string(network.get("Id"), "Ollama network ID").casefold() != network_id
        or network.get("Internal") is not True
        or network.get("Driver") != "bridge"
        or network.get("Name") != network_name
    ):
        raise InspectorError("Ollama no-egress network was not enforced")
    aliases = attachment.get("Aliases")
    if not isinstance(aliases, list) or "ollama" not in aliases:
        raise InspectorError("Ollama service endpoint identity was invalid")
    try:
        address = ipaddress.ip_address(
            _string(attachment.get("IPAddress"), "Ollama network address")
        )
    except ValueError as error:
        raise InspectorError("Ollama network address was invalid") from error
    if (
        not address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
    ):
        raise InspectorError("Ollama network address was outside the private boundary")
    runtime_hash = hashlib.sha256(
        (
            f"{container_id}\0{started_at}\0{network_id}\0{process_id}\0"
            f"{image_reference}\0{image_id}"
        ).encode("ascii")
    ).hexdigest()
    return InspectedRuntime(
        endpoint="http://ollama:11434",
        model=model,
        runtime_instance_id=f"runtime-{runtime_hash[:32]}",
        runtime_handle=f"container:{container_id}",
        handle_kind="container",
        process_id=None,
        container_id=container_id,
        container_image_reference=image_reference,
        container_image_id=image_id,
        process_started_at=None,
        executable_sha256=None,
        executable_size=None,
        firewall_rule_token=None,
        egress_policy="container_internal_no_egress",
        egress_enforced=True,
        cloud_disabled=True,
        isolation_rule_id=f"network-{network_id[:32]}",
    )


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        try:
            if parsed.path == "/health" and not parsed.query:
                status, _payload = _docker_request("GET", "/version")
                if status != 200:
                    raise InspectorError("Docker runtime was unavailable")
                self._send(200, {"status": "ok"})
                return
            query = parse_qs(parsed.query, strict_parsing=True)
            if parsed.path != "/v1/ollama-runtime" or set(query) != {"model"} or len(query["model"]) != 1:
                self._send(404, {"error": "not_found"})
                return
            runtime = inspect_ollama_runtime(query["model"][0])
            self._send(200, asdict(runtime))
        except (InspectorError, ValueError):
            self._send(503, {"error": "runtime_unverified"})

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, _format: str, *_args: object) -> None:
        return None


def main() -> int:
    server = ThreadingHTTPServer(("0.0.0.0", 8766), _Handler)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

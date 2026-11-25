"""Servidor HTTP local com SSE para conectar duas IAs em tempo real."""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import shutil
import struct
import tempfile
import threading
import time
import urllib.parse
import uuid
import webbrowser
import zlib
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from ..adapters import (
    AnthropicCreator,
    AnthropicVerifier,
    CLICreator,
    CLIVerifier,
    CallableCreator,
    CallableVerifier,
    GeminiCreator,
    GeminiVerifier,
    OpenAICompatibleCreator,
    OpenAICompatibleVerifier,
)
from ..models import (
    ArtifactKind,
    CreatorSubmission,
    IterationRecord,
    LoopResult,
    VerificationPacket,
    VerificationReport,
    VerifyBridgeError,
)
from ..manual import CHAT_URLS, ManualChannel, ManualCreator, ManualVerifier, parse_reply_payload
from ..orchestrator import CreatorAgent, LoopConfig, VerifierAgent, run_verification_loop
from ..packet import render_raw_packet_text
from ..sanitizer import SanitizerConfig, collect_and_sanitize
from .. import subscriptions

SUBSCRIPTION_PREFIX = "sub_"

STATIC_DIR = Path(__file__).resolve().parent / "static"
SAFE_FILENAME_RE = re.compile(r"[^a-zA-Z0-9._-]+")
MAX_REQUEST_BYTES = 25 * 1024 * 1024  # 25 MB para uploads multimodais locais
SESSION_RETENTION_SECONDS = 30 * 60  # pasta da execucao e apagada 30 min apos terminar
STATIC_MIME_MAP: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
    ".pdf": "application/pdf",
    ".txt": "text/plain; charset=utf-8",
}


@dataclass
class RunSession:
    """Estado de uma execucao do loop entre duas IAs."""

    run_id: str
    workdir: Path
    cancel_event: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    condition: threading.Condition = field(init=False)
    events: list[dict[str, Any]] = field(default_factory=list)
    finished: bool = False
    result: dict[str, Any] | None = None
    channel: ManualChannel | None = None

    def __post_init__(self) -> None:
        self.condition = threading.Condition(self.lock)

    def push_event(self, event_type: str, data: dict[str, Any]) -> None:
        with self.condition:
            self.events.append({"event": event_type, "data": data})
            if event_type == "done":
                self.finished = True
                self.result = data
            self.condition.notify_all()


class VerifyBridgeServer(ThreadingHTTPServer):
    """Servidor HTTP local com registro de sessoes ativas."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        demo_delay: float = 0.6,
        session_retention_seconds: float = SESSION_RETENTION_SECONDS,
    ) -> None:
        super().__init__(server_address, _RequestHandler)
        self.demo_delay = demo_delay
        self.session_retention_seconds = session_retention_seconds
        self._sessions_lock = threading.Lock()
        self._sessions: dict[str, RunSession] = {}
        self._expiry_timers: dict[str, threading.Timer] = {}

    def handle_error(self, request: Any, client_address: Any) -> None:
        # O navegador fechar a conexao (aba fechada, recarregar) e normal:
        # nao poluir o terminal do usuario com traceback por isso.
        import sys

        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)

    def register_session(self, session: RunSession) -> None:
        with self._sessions_lock:
            self._sessions[session.run_id] = session

    def get_session(self, run_id: str) -> RunSession | None:
        with self._sessions_lock:
            return self._sessions.get(run_id)

    def schedule_session_removal(self, run_id: str) -> None:
        """Apaga a pasta temporaria da execucao algum tempo depois de terminar.

        A espera permite que a tela ainda carregue as imagens das rodadas.
        """
        timer = threading.Timer(self.session_retention_seconds, self.remove_session, (run_id,))
        timer.daemon = True
        with self._sessions_lock:
            if run_id not in self._sessions:
                return
            self._expiry_timers[run_id] = timer
        timer.start()

    def remove_session(self, run_id: str) -> None:
        with self._sessions_lock:
            sess = self._sessions.pop(run_id, None)
            timer = self._expiry_timers.pop(run_id, None)
        if timer is not None:
            timer.cancel()
        if sess is not None:
            sess.cancel_event.set()
            shutil.rmtree(sess.workdir, ignore_errors=True)

    def cleanup(self) -> None:
        with self._sessions_lock:
            run_ids = list(self._sessions)
        for run_id in run_ids:
            self.remove_session(run_id)


class _RequestHandler(BaseHTTPRequestHandler):
    server: VerifyBridgeServer  # type: ignore[assignment]

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Mantem terminal limpo; erros criticos sao enviados via HTTP/SSE.
        return

    def _allowed_hosts(self) -> set[str]:
        port = self.server.server_address[1]
        return {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}

    def _host_is_allowed(self) -> bool:
        """Bloqueia DNS rebinding: so aceita o Host do proprio servidor local."""
        host = (self.headers.get("Host") or "").strip().lower()
        if host in self._allowed_hosts():
            return True
        self._send_json({"error": "Host nao permitido."}, HTTPStatus.FORBIDDEN)
        return False

    def _origin_is_allowed(self) -> bool:
        """Bloqueia CSRF: sites externos nao podem disparar acoes nesta API local.

        Navegadores sempre enviam `Origin` em POST vindo de outro site; clientes
        locais (CLI, testes, curl) normalmente nao enviam e sao aceitos.
        """
        origin = (self.headers.get("Origin") or "").strip().lower()
        if not origin:
            return True
        parsed = urllib.parse.urlparse(origin)
        if parsed.scheme == "http" and parsed.netloc in self._allowed_hosts():
            return True
        self._send_json({"error": "Origem nao permitida."}, HTTPStatus.FORBIDDEN)
        return False

    def do_GET(self) -> None:
        if not self._host_is_allowed():
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "/index.html":
            self._serve_static_file(STATIC_DIR / "index.html")
            return

        if path.startswith("/static/"):
            rel = path[len("/static/") :]
            target = (STATIC_DIR / rel).resolve()
            if target.parent == STATIC_DIR.resolve() and target.is_file():
                self._serve_static_file(target)
                return
            self._send_json({"error": "Arquivo estatico nao encontrado."}, HTTPStatus.NOT_FOUND)
            return

        if path == "/api/env":
            self._send_json(
                {
                    "env_keys": {
                        "gemini": bool(os.environ.get("GEMINI_API_KEY")),
                        "openai": bool(os.environ.get("OPENAI_API_KEY")),
                        "anthropic": bool(os.environ.get("ANTHROPIC_API_KEY")),
                    },
                    "defaults": {
                        "gemini": {"model": "gemini-2.5-flash", "base_url": ""},
                        "openai": {
                            "model": "gpt-4o-mini",
                            "base_url": "https://api.openai.com/v1/chat/completions",
                        },
                        "anthropic": {"model": "claude-sonnet-5-5", "base_url": ""},
                    },
                }
            )
            return

        if path == "/api/example":
            self._send_json(_build_golden_fish_example())
            return

        if path == "/api/subscriptions":
            refresh = "refresh=1" in (parsed.query or "")
            self._send_json(
                {
                    "tools": subscriptions.check_all_status(use_cache=not refresh),
                    "chats": [
                        {"id": k, "url": v} for k, v in CHAT_URLS.items()
                    ],
                }
            )
            return

        # /api/runs/<id> , /api/runs/<id>/events , /api/runs/<id>/files/<name>
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 3 and parts[0] == "api" and parts[1] == "runs":
            run_id = parts[2]
            session = self.server.get_session(run_id)
            if session is None:
                self._send_json({"error": "Execucao nao encontrada."}, HTTPStatus.NOT_FOUND)
                return

            if len(parts) == 3:
                with session.lock:
                    self._send_json(
                        {
                            "run_id": run_id,
                            "finished": session.finished,
                            "events": list(session.events),
                            "result": session.result,
                        }
                    )
                return

            if len(parts) == 4 and parts[3] == "events":
                self._serve_sse(session)
                return

            if len(parts) == 5 and parts[3] == "files":
                filename = Path(urllib.parse.unquote(parts[4])).name
                target = (session.workdir / filename).resolve()
                if target.parent == session.workdir.resolve() and target.is_file():
                    self._serve_static_file(target)
                    return
                self._send_json({"error": "Arquivo nao encontrado."}, HTTPStatus.NOT_FOUND)
                return

        self._send_json({"error": "Rota nao encontrada."}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if not self._host_is_allowed() or not self._origin_is_allowed():
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/preview":
            body = self._read_json_body()
            if body is None:
                return
            self._handle_preview(body)
            return

        if path == "/api/runs":
            body = self._read_json_body()
            if body is None:
                return
            self._handle_start_run(body)
            return

        parts = [p for p in path.split("/") if p]
        if len(parts) == 4 and parts[:2] == ["api", "subscriptions"] and parts[3] == "login":
            try:
                info = subscriptions.start_login(parts[2])
            except VerifyBridgeError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._send_json(info)
            return

        if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "reply":
            session = self.server.get_session(parts[2])
            if session is None or session.channel is None:
                self._send_json({"error": "Execucao nao encontrada."}, HTTPStatus.NOT_FOUND)
                return
            body = self._read_json_body()
            if body is None:
                return
            try:
                reply = parse_reply_payload(body)
            except VerifyBridgeError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            if not session.channel.submit(str(body.get("request_id") or ""), reply):
                self._send_json(
                    {"error": "Nenhum pedido aguardando resposta (ou ja respondido)."},
                    HTTPStatus.CONFLICT,
                )
                return
            session.push_event("input_received", {"request_id": body.get("request_id")})
            self._send_json({"accepted": True})
            return

        if len(parts) == 4 and parts[0] == "api" and parts[1] == "runs" and parts[3] == "stop":
            session = self.server.get_session(parts[2])
            if session is None:
                self._send_json({"error": "Execucao nao encontrada."}, HTTPStatus.NOT_FOUND)
                return
            session.cancel_event.set()
            session.push_event(
                "status",
                {
                    "phase": "cancelling",
                    "iteration": 0,
                    "message": "Parada solicitada pelo usuario...",
                },
            )
            self._send_json({"stopped": True, "run_id": parts[2]})
            return

        self._send_json({"error": "Rota nao encontrada."}, HTTPStatus.NOT_FOUND)

    def _handle_preview(self, body: dict[str, Any]) -> None:
        with tempfile.TemporaryDirectory(prefix="vb-preview-") as tmp:
            workdir = Path(tmp)
            try:
                submission, cfg = _build_submission_from_payload(body, workdir)
                packet, audit = collect_and_sanitize(submission, config=cfg)
            except VerifyBridgeError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._send_json(
                {
                    "raw_packet_text": render_raw_packet_text(packet),
                    "packet": packet.to_dict(),
                    "sanitization": audit.to_dict(),
                }
            )

    def _handle_start_run(self, body: dict[str, Any]) -> None:
        run_id = uuid.uuid4().hex[:12]
        workdir = Path(tempfile.mkdtemp(prefix=f"vb-run-{run_id}-"))
        cancel_event = threading.Event()
        session = RunSession(run_id=run_id, workdir=workdir, cancel_event=cancel_event)
        session.channel = ManualChannel(
            lambda payload: session.push_event("awaiting_input", payload),
            cancel_event=cancel_event,
        )

        try:
            submission, sanitizer_cfg = _build_submission_from_payload(body, workdir)
            # Valida entrada rapidamente antes de iniciar a thread
            collect_and_sanitize(submission, config=sanitizer_cfg)

            creator_cfg = body.get("creator") if isinstance(body.get("creator"), dict) else {}
            verifier_cfg = body.get("verifier") if isinstance(body.get("verifier"), dict) else {}

            max_iterations = max(1, min(15, int(body.get("max_iterations", 5))))
            stagnation_limit = max(1, min(10, int(body.get("stagnation_limit", 2))))
            include_acceptable = bool(body.get("include_acceptable", False))

            verifier, creator = _build_agent_pair(
                creator_cfg=creator_cfg,
                verifier_cfg=verifier_cfg,
                workdir=workdir,
                submission=submission,
                demo_delay=self.server.demo_delay,
                cancel_event=cancel_event,
                channel=session.channel,
                run_id=run_id,
            )
        except (VerifyBridgeError, ValueError, TypeError) as exc:
            shutil.rmtree(workdir, ignore_errors=True)
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return

        self.server.register_session(session)

        def worker() -> None:
            def on_status(phase: str, iteration: int, message: str) -> None:
                session.push_event(
                    "status",
                    {"phase": phase, "iteration": iteration, "message": message},
                )

            def on_iteration(rec: IterationRecord) -> None:
                previews = _snapshot_artifacts(rec.packet, session.workdir, run_id, rec.iteration)
                session.push_event(
                    "iteration",
                    {
                        "iteracao": rec.iteration,
                        "raw_packet_text": render_raw_packet_text(rec.packet),
                        "pacote": rec.packet.to_dict(),
                        "sanitizacao": rec.sanitization.to_dict(),
                        "relatorio": rec.report.to_dict() if rec.report else None,
                        "prompt_correcao": (
                            rec.correction_prompt.prompt_text if rec.correction_prompt else None
                        ),
                        "erro": rec.error,
                        "previews": previews,
                    },
                )

            cfg = LoopConfig(
                max_iterations=max_iterations,
                stagnation_limit=stagnation_limit,
                include_acceptable_in_prompt=include_acceptable,
                sanitizer=sanitizer_cfg,
                on_iteration=on_iteration,
                on_status=on_status,
                cancel_event=session.cancel_event,
            )
            try:
                result: LoopResult = run_verification_loop(
                    submission, verifier, creator, config=cfg
                )
                session.push_event("done", result.to_dict())
            except Exception as exc:  # noqa: BLE001
                # Garante que a tela nunca fica presa esperando um 'done' que nao vem.
                session.push_event(
                    "done",
                    {
                        "aprovado": False,
                        "motivo_parada": "erro_interno",
                        "total_iteracoes": 0,
                        "erro": f"Falha interna na execucao: {exc}",
                    },
                )
            finally:
                self.server.schedule_session_removal(run_id)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

        self._send_json({"run_id": run_id}, HTTPStatus.ACCEPTED)

    def _serve_sse(self, session: RunSession) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        idx = 0
        try:
            while True:
                with session.condition:
                    while idx >= len(session.events) and not session.finished:
                        session.condition.wait(timeout=15.0)
                        if idx >= len(session.events) and not session.finished:
                            # Keep-alive comment
                            self.wfile.write(b": ping\n\n")
                            self.wfile.flush()
                    batch = session.events[idx:]
                    idx = len(session.events)
                    done = session.finished

                for item in batch:
                    payload = json.dumps(item["data"], ensure_ascii=False)
                    frame = f"event: {item['event']}\ndata: {payload}\n\n".encode("utf-8")
                    self.wfile.write(frame)
                self.wfile.flush()

                if done and idx >= len(session.events):
                    break
        except (BrokenPipeError, ConnectionResetError):
            return

    def _read_json_body(self) -> dict[str, Any] | None:
        # Exigir application/json forca preflight CORS em pedidos de outros sites
        # (que este servidor nunca autoriza), fechando o envio via text/plain.
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            self._send_json(
                {"error": "Content-Type deve ser application/json."},
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
            )
            return None
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json({"error": "Content-Length invalido."}, HTTPStatus.BAD_REQUEST)
            return None
        if length <= 0 or length > MAX_REQUEST_BYTES:
            self._send_json(
                {"error": "Corpo da requisicao vazio ou acima do limite permitido."},
                HTTPStatus.BAD_REQUEST,
            )
            return None
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json({"error": "JSON invalido no corpo."}, HTTPStatus.BAD_REQUEST)
            return None
        if not isinstance(data, dict):
            self._send_json({"error": "Esperado objeto JSON."}, HTTPStatus.BAD_REQUEST)
            return None
        return data

    def _serve_static_file(self, path: Path) -> None:
        content = path.read_bytes()
        mime = STATIC_MIME_MAP.get(path.suffix.lower())
        if mime is None:
            guessed = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            mime = (
                f"{guessed}; charset=utf-8"
                if guessed.startswith("text/") or guessed == "application/javascript"
                else guessed
            )
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def _send_json(self, data: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)


def _safe_name(name: str, fallback: str) -> str:
    base = Path(name).name.strip()
    cleaned = SAFE_FILENAME_RE.sub("_", base).lstrip(".")
    return cleaned or fallback


def _write_uploaded_files(
    items: Any,
    workdir: Path,
    prefix: str,
) -> tuple[Path, ...]:
    if not isinstance(items, list):
        return ()
    written: list[Path] = []
    for idx, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        raw_name = str(item.get("name") or f"{prefix}_{idx}.bin")
        b64 = str(item.get("data_b64") or "")
        if not b64:
            continue
        # Remove prefixo data:...;base64, se vier do FileReader do navegador
        if "," in b64 and b64.startswith("data:"):
            b64 = b64.split(",", 1)[1]
        try:
            blob = base64.b64decode(b64)
        except Exception as exc:  # noqa: BLE001
            raise VerifyBridgeError(f"Arquivo '{raw_name}' com Base64 invalido.") from exc

        safe = _safe_name(raw_name, f"{prefix}_{idx}")
        target = workdir / f"{prefix}_{idx}_{safe}"
        target.write_bytes(blob)
        written.append(target)
    return tuple(written)


def _build_submission_from_payload(
    body: dict[str, Any],
    workdir: Path,
) -> tuple[CreatorSubmission, SanitizerConfig]:
    markdown = str(body.get("markdown") or "")
    refs = _write_uploaded_files(body.get("reference_files"), workdir, "ref")
    outs = _write_uploaded_files(body.get("deliverable_files"), workdir, "out")
    allow_structured = bool(body.get("allow_structured", False))
    sub = CreatorSubmission(
        markdown_text=markdown,
        reference_files=refs,
        deliverable_files=outs,
        base_dir=workdir,
    )
    # Na interface web o MD vem do navegador: caminhos citados nele so podem
    # apontar para arquivos enviados (dentro do workdir), nunca para o disco local.
    return sub, SanitizerConfig(
        allow_structured_outputs=allow_structured,
        confine_to_base_dir=True,
    )


def _snapshot_artifacts(
    packet: VerificationPacket,
    workdir: Path,
    run_id: str,
    iteration: int,
) -> list[dict[str, Any]]:
    """Salva copia versionada dos artefatos de cada rodada para preview na UI."""
    previews: list[dict[str, Any]] = []
    for art in packet.artifacts:
        info: dict[str, Any] = {
            "label": art.label,
            "role": art.role.value,
            "kind": art.kind.value,
            "metadata": art.metadata_line,
        }
        if art.kind is ArtifactKind.IMAGE:
            snap_name = f"snap_r{iteration}_{art.label}{art.path.suffix.lower()}"
            shutil.copy2(art.path, workdir / snap_name)
            info["url"] = f"/api/runs/{run_id}/files/{snap_name}"
        elif art.kind is ArtifactKind.TEXT and art.text_excerpt:
            info["text"] = art.text_excerpt[:2000]
        previews.append(info)
    return previews


def _build_agent_pair(
    *,
    creator_cfg: dict[str, Any],
    verifier_cfg: dict[str, Any],
    workdir: Path,
    submission: CreatorSubmission,
    demo_delay: float,
    cancel_event: threading.Event | None = None,
    channel: ManualChannel | None = None,
    run_id: str = "",
) -> tuple[VerifierAgent, CreatorAgent]:
    c_prov = str(creator_cfg.get("provider") or "demo").lower()
    v_prov = str(verifier_cfg.get("provider") or "demo").lower()

    if c_prov == "demo" and v_prov == "demo":
        return _make_demo_pair(workdir, submission, demo_delay)

    verifier = _make_special_agent("verifier", verifier_cfg, workdir, cancel_event, channel, run_id)
    if verifier is None:
        verifier = _make_verifier_from_cfg(verifier_cfg, demo_delay, cancel_event)
    creator = _make_special_agent("creator", creator_cfg, workdir, cancel_event, channel, run_id)
    if creator is None:
        creator = _make_creator_from_cfg(creator_cfg, workdir, submission, demo_delay, cancel_event)
    return verifier, creator  # type: ignore[return-value]


def _make_special_agent(
    role: str,
    cfg: dict[str, Any],
    workdir: Path,
    cancel_event: threading.Event | None,
    channel: ManualChannel | None,
    run_id: str,
) -> VerifierAgent | CreatorAgent | None:
    """Agentes por assinatura (`sub_<ferramenta>`) e Modo Chat (`chat`)."""
    prov = str(cfg.get("provider") or "").lower()
    model = str(cfg.get("model") or "").strip()

    if prov.startswith(SUBSCRIPTION_PREFIX):
        tool_id = prov[len(SUBSCRIPTION_PREFIX):]
        subscriptions.get_tool(tool_id)  # valida
        if role == "verifier":
            return subscriptions.SubscriptionVerifier(
                tool_id=tool_id, model=model, cancel_event=cancel_event
            )
        return subscriptions.SubscriptionCreator(
            tool_id=tool_id, workdir=workdir, model=model, cancel_event=cancel_event
        )

    if prov == "chat":
        if channel is None:
            raise VerifyBridgeError("Modo Chat indisponivel nesta execucao.")
        chat = str(cfg.get("chat") or "claude").lower()
        if chat not in CHAT_URLS:
            raise VerifyBridgeError(f"Chat invalido: {chat}")
        if role == "verifier":
            return ManualVerifier(channel=channel, workdir=workdir, run_id=run_id, chat=chat)
        return ManualCreator(channel=channel, workdir=workdir, run_id=run_id, chat=chat)

    return None


def _make_verifier_from_cfg(
    cfg: dict[str, Any],
    demo_delay: float,
    cancel_event: threading.Event | None = None,
) -> VerifierAgent:
    prov = str(cfg.get("provider") or "demo").lower()
    model = str(cfg.get("model") or "").strip()
    api_key = str(cfg.get("api_key") or "").strip() or None
    base_url = str(cfg.get("base_url") or "").strip()
    cli_cmd = str(cfg.get("cli_cmd") or "").strip()

    if prov == "gemini":
        return GeminiVerifier(
            model=model or "gemini-2.5-flash",
            api_key=api_key,
            base_url=base_url or "https://generativelanguage.googleapis.com/v1beta/models",
        )
    if prov == "openai":
        return OpenAICompatibleVerifier(
            model=model or "gpt-4o-mini",
            api_key=api_key,
            base_url=base_url or "https://api.openai.com/v1/chat/completions",
        )
    if prov == "anthropic":
        return AnthropicVerifier(
            model=model or "claude-sonnet-5-5",
            api_key=api_key,
            base_url=base_url or "https://api.anthropic.com/v1/messages",
        )
    if prov == "cli":
        if not cli_cmd:
            raise VerifyBridgeError("Informe o comando CLI do Agente Verificador.")
        return CLIVerifier(command=cli_cmd, cancel_event=cancel_event)
    if prov == "demo":
        v, _ = _make_demo_pair(Path.cwd(), CreatorSubmission(markdown_text="x"), demo_delay)
        return v
    raise VerifyBridgeError(f"Provedor do verificador invalido: {prov}")


def _make_creator_from_cfg(
    cfg: dict[str, Any],
    workdir: Path,
    submission: CreatorSubmission,
    demo_delay: float,
    cancel_event: threading.Event | None = None,
) -> CreatorAgent:
    prov = str(cfg.get("provider") or "demo").lower()
    model = str(cfg.get("model") or "").strip()
    api_key = str(cfg.get("api_key") or "").strip() or None
    base_url = str(cfg.get("base_url") or "").strip()
    cli_cmd = str(cfg.get("cli_cmd") or "").strip()
    sys_prompt = str(cfg.get("system_prompt") or "").strip()

    if prov == "gemini":
        return GeminiCreator(
            model=model or "gemini-2.5-flash",
            api_key=api_key,
            base_url=base_url or "https://generativelanguage.googleapis.com/v1beta/models",
            output_dir=workdir,
            **({"system_prompt": sys_prompt} if sys_prompt else {}),
        )
    if prov == "openai":
        return OpenAICompatibleCreator(
            model=model or "gpt-4o-mini",
            api_key=api_key,
            base_url=base_url or "https://api.openai.com/v1/chat/completions",
            output_dir=workdir,
            **({"system_prompt": sys_prompt} if sys_prompt else {}),
        )
    if prov == "anthropic":
        return AnthropicCreator(
            model=model or "claude-sonnet-5-5",
            api_key=api_key,
            base_url=base_url or "https://api.anthropic.com/v1/messages",
            output_dir=workdir,
            **({"system_prompt": sys_prompt} if sys_prompt else {}),
        )
    if prov == "cli":
        if not cli_cmd:
            raise VerifyBridgeError("Informe o comando CLI do Agente Criador.")
        return CLICreator(command=cli_cmd, workdir=workdir, cancel_event=cancel_event)
    if prov == "demo":
        _, c = _make_demo_pair(workdir, submission, demo_delay)
        return c
    raise VerifyBridgeError(f"Provedor do criador invalido: {prov}")


def _make_demo_pair(
    workdir: Path,
    submission: CreatorSubmission,
    demo_delay: float,
) -> tuple[VerifierAgent, CreatorAgent]:
    """Par simulado offline (cenario peixe dourado em 3 rodadas) para testar a tela ao vivo."""
    responses = [
        json.dumps(
            {
                "criterios": [
                    {"id": "C1", "requisito": "Peixe dourado identico a REF-1", "peso": "essencial", "atendido": False},
                    {"id": "C2", "requisito": "Imagem pequena (ate 256x256)", "peso": "importante", "atendido": False},
                    {"id": "C3", "requisito": "Fundo limpo sem ruidos", "peso": "importante", "atendido": False},
                ],
                "falhas": [
                    {
                        "id": "F1",
                        "criterio_id": "C1",
                        "severidade": "critico",
                        "observado": "Peixe azulado, cor divergente da referencia REF-1",
                        "esperado": "Peixe dourado identico a REF-1",
                        "acao": "Corrigir cor do peixe para dourado identico a REF-1",
                        "evidencia": "OUT-1 vs REF-1",
                    },
                    {
                        "id": "F2",
                        "criterio_id": "C2",
                        "severidade": "moderado",
                        "observado": "Dimensoes acima do solicitado (320x320px)",
                        "esperado": "Imagem pequena ate 256x256px",
                        "acao": "Redimensionar entrega para 160x160px",
                        "evidencia": "OUT-1",
                    },
                    {
                        "id": "F3",
                        "criterio_id": "C3",
                        "severidade": "moderado",
                        "observado": "Ruidos escuros espalhados no fundo",
                        "esperado": "Fundo completamente limpo",
                        "acao": "Remover manchas e ruidos do fundo",
                        "evidencia": "OUT-1",
                    },
                ],
            },
            ensure_ascii=False,
        ),
        json.dumps(
            {
                "criterios": [
                    {"id": "C1", "requisito": "Peixe dourado identico a REF-1", "peso": "essencial", "atendido": True},
                    {"id": "C2", "requisito": "Imagem pequena (ate 256x256)", "peso": "importante", "atendido": True},
                    {"id": "C3", "requisito": "Fundo limpo sem ruidos", "peso": "importante", "atendido": False},
                ],
                "falhas": [
                    {
                        "id": "F1",
                        "criterio_id": "C3",
                        "severidade": "moderado",
                        "observado": "Borda cinza residual nos cantos do fundo",
                        "esperado": "Fundo limpo sem marcas nos cantos",
                        "acao": "Limpar residuos cinzas dos cantos preservando o peixe",
                        "evidencia": "OUT-1",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        json.dumps(
            {
                "criterios": [
                    {"id": "C1", "requisito": "Peixe dourado identico a REF-1", "peso": "essencial", "atendido": True},
                    {"id": "C2", "requisito": "Imagem pequena (ate 256x256)", "peso": "importante", "atendido": True},
                    {"id": "C3", "requisito": "Fundo limpo sem ruidos", "peso": "importante", "atendido": True},
                ],
                "falhas": [
                    {
                        "id": "F1",
                        "criterio_id": "C1",
                        "severidade": "aceitavel",
                        "observado": "Leve diferenca de 2% no brilho da nadadeira",
                        "esperado": "Tom dourado fiel a REF-1",
                        "acao": "Dentro da tolerancia; sem correcao obrigatoria",
                        "evidencia": "OUT-1 vs REF-1",
                    }
                ],
            },
            ensure_ascii=False,
        ),
    ]
    state = {"round": 0}

    def demo_verifier(_packet: VerificationPacket, _sys: str, _repair: str | None) -> str:
        if demo_delay > 0:
            time.sleep(demo_delay)
        idx = min(state["round"], len(responses) - 1)
        state["round"] += 1
        return responses[idx]

    def demo_creator(
        correction, prev: CreatorSubmission, _rep: VerificationReport
    ) -> CreatorSubmission:
        if demo_delay > 0:
            time.sleep(demo_delay)
        img_target = next(
            (p for p in prev.deliverable_files if p.suffix.lower() == ".png"),
            None,
        )
        if img_target is not None:
            if correction.iteration == 1:
                # Rodada 2: peixe dourado 160x160 mas ainda com cantos sujos
                img_target.write_bytes(_draw_fish_png(160, 160, gold=True, noisy=False, corner_marks=True))
            else:
                # Rodada 3: peixe dourado 160x160 limpo e fiel
                img_target.write_bytes(_draw_fish_png(160, 160, gold=True, noisy=False, corner_marks=False))
        return prev

    return CallableVerifier(demo_verifier), CallableCreator(demo_creator)


def _draw_fish_png(
    width: int,
    height: int,
    *,
    gold: bool,
    noisy: bool,
    corner_marks: bool,
) -> bytes:
    """Desenha um peixe pixel-art valido em PNG puro (zero dependencias)."""
    bg = (248, 250, 252)
    body = (245, 178, 26) if gold else (56, 130, 232)
    fin = (218, 132, 12) if gold else (34, 88, 185)
    eye = (20, 24, 34)
    rows: list[bytes] = []

    cx = width * 0.54
    cy = height * 0.50
    rx = width * 0.28
    ry = height * 0.19

    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            r, g, b = bg

            # Ruido espalhado (para simular entrega suja da rodada 1)
            if noisy and ((x * 37 + y * 17) % 97 == 0):
                r, g, b = (110, 118, 130)

            # Marcas residuais nos cantos (para simular rodada 2)
            if corner_marks and (
                (x < width * 0.10 and y < height * 0.10)
                or (x > width * 0.90 and y > height * 0.90)
            ):
                r, g, b = (170, 176, 186)

            # Cauda triangular a esquerda
            dx_tail = x - width * 0.18
            dy_tail = abs(y - cy)
            if 0 <= dx_tail <= width * 0.14 and dy_tail <= (width * 0.14 - dx_tail) * 1.15:
                r, g, b = fin

            # Nadadeira superior
            if width * 0.44 <= x <= width * 0.60 and height * 0.24 <= y <= height * 0.34:
                if (y - height * 0.24) >= abs(x - width * 0.52) * 0.6:
                    r, g, b = fin

            # Corpo oval do peixe
            nx = (x - cx) / rx
            ny = (y - cy) / ry
            if nx * nx + ny * ny <= 1.0:
                r, g, b = body

            # Olho
            ex = width * 0.68
            ey = height * 0.46
            er = max(2.0, width * 0.032)
            if (x - ex) ** 2 + (y - ey) ** 2 <= er * er:
                r, g, b = eye

            row.extend((r, g, b))
        rows.append(bytes(row))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(tag + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(b"".join(rows), level=6))
        + chunk(b"IEND", b"")
    )


def _build_golden_fish_example() -> dict[str, Any]:
    ref_png = _draw_fish_png(160, 160, gold=True, noisy=False, corner_marks=False)
    out_png = _draw_fish_png(320, 320, gold=False, noisy=True, corner_marks=False)
    py_blob = b"def gerar_peixe():\n    return render(cor='azul', tamanho=320)\n"

    md = (
        "# Pedido Inicial\n"
        "Gerar uma imagem de um peixe dourado identico ao da imagem enviada pelo usuario, "
        "pequena (ate 256x256), limpa e com maior qualidade.\n\n"
        "## Criterios de Aceite\n"
        "- Mesma cor dourada e formato da referencia\n"
        "- Resolucao pequena (ate 256x256)\n"
        "- Fundo completamente limpo, sem ruidos\n\n"
        "## Justificativa do Agente Criador\n"
        "Eu optei por deixar o peixe azul em 320x320 porque achei mais moderno.\n"
        "```python\n"
        "render(color='blue', size=(320, 320))\n"
        "```\n"
    )
    return {
        "markdown": md,
        "reference_files": [
            {
                "name": "peixe_usuario_referencia.png",
                "mime": "image/png",
                "data_b64": base64.b64encode(ref_png).decode("ascii"),
            }
        ],
        "deliverable_files": [
            {
                "name": "entrega_inicial_v1.png",
                "mime": "image/png",
                "data_b64": base64.b64encode(out_png).decode("ascii"),
            },
            {
                "name": "script_gerador.py",
                "mime": "text/x-python",
                "data_b64": base64.b64encode(py_blob).decode("ascii"),
            },
        ],
    }


def create_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    demo_delay: float = 0.6,
    session_retention_seconds: float = SESSION_RETENTION_SECONDS,
) -> VerifyBridgeServer:
    """Cria instancia do servidor local sem iniciar o loop de bloqueio."""
    return VerifyBridgeServer(
        (host, port),
        demo_delay=demo_delay,
        session_retention_seconds=session_retention_seconds,
    )


def run_ui(host: str = "127.0.0.1", port: int = 8765, *, open_browser: bool = True) -> int:
    """Inicia a interface web local e abre automaticamente no navegador."""
    chosen_port = port
    server: VerifyBridgeServer | None = None
    for candidate in (port, 0):
        try:
            server = create_server(host, candidate)
            chosen_port = server.server_address[1]
            break
        except OSError:
            continue

    if server is None:
        raise RuntimeError("Nao foi possivel iniciar o servidor local.")

    url = f"http://{host}:{chosen_port}"
    print(f"Verify Bridge aberto em: {url}")
    print("Pressione Ctrl+C para encerrar.")
    if open_browser:
        threading.Timer(0.35, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nEncerrando Verify Bridge...")
    finally:
        server.cleanup()
        server.server_close()
    return 0

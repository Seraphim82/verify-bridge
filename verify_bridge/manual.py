"""Modo Chat (manual assistido): usa qualquer assinatura pelo site oficial do chat.

O programa NAO automatiza o site (isso viola os termos de uso das assinaturas e
quebra a cada mudanca visual). Ele prepara o pedido pronto, abre o chat e espera
o usuario colar a resposta. O loop continua sozinho a partir dai.
"""

from __future__ import annotations

import base64
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .adapters import CREATOR_DEFAULT_SYSTEM_PROMPT, CancelledByUser, _apply_text_revision
from .models import (
    AdapterError,
    CorrectionPrompt,
    CreatorSubmission,
    VerificationPacket,
    VerificationReport,
)
from .packet import render_raw_packet_text

CHAT_URLS: dict[str, str] = {
    "claude": "https://claude.ai/new",
    "chatgpt": "https://chatgpt.com/",
    "gemini": "https://gemini.google.com/app",
}
CHAT_LABELS: dict[str, str] = {
    "claude": "Claude",
    "chatgpt": "ChatGPT",
    "gemini": "Gemini",
}

MAX_REPLY_FILES = 10


@dataclass
class ManualReply:
    text: str = ""
    files: list[tuple[str, bytes]] = field(default_factory=list)


@dataclass
class _Pending:
    request_id: str
    role: str
    done: threading.Event = field(default_factory=threading.Event)
    reply: ManualReply | None = None


class ManualChannel:
    """Canal entre o loop (thread do worker) e a tela (requisicoes HTTP)."""

    def __init__(
        self,
        publish: Callable[[dict[str, Any]], None],
        cancel_event: threading.Event | None = None,
        timeout_seconds: float = 6 * 3600,
    ) -> None:
        self._publish = publish
        self._cancel = cancel_event
        self._timeout = timeout_seconds
        self._lock = threading.Lock()
        self._pending: _Pending | None = None

    def ask(self, role: str, payload: dict[str, Any]) -> ManualReply:
        pending = _Pending(request_id=uuid.uuid4().hex[:12], role=role)
        with self._lock:
            self._pending = pending
        self._publish({"request_id": pending.request_id, "role": role, **payload})

        waited = 0.0
        try:
            while not pending.done.wait(0.2):
                waited += 0.2
                if self._cancel is not None and self._cancel.is_set():
                    raise CancelledByUser("Execucao interrompida pelo usuario.")
                if waited >= self._timeout:
                    raise AdapterError("Tempo esgotado esperando a resposta colada do chat.")
        finally:
            with self._lock:
                if self._pending is pending:
                    self._pending = None
        assert pending.reply is not None
        return pending.reply

    def submit(self, request_id: str, reply: ManualReply) -> bool:
        with self._lock:
            pending = self._pending
            if pending is None or pending.request_id != request_id or pending.done.is_set():
                return False
            pending.reply = reply
            pending.done.set()
            return True


def parse_reply_payload(body: dict[str, Any]) -> ManualReply:
    text = str(body.get("text") or "")
    files: list[tuple[str, bytes]] = []
    raw_files = body.get("files")
    if isinstance(raw_files, list):
        for item in raw_files[:MAX_REPLY_FILES]:
            if not isinstance(item, dict):
                continue
            b64 = str(item.get("data_b64") or "")
            if "," in b64 and b64.startswith("data:"):
                b64 = b64.split(",", 1)[1]
            if not b64:
                continue
            try:
                blob = base64.b64decode(b64, validate=False)
            except Exception as exc:  # noqa: BLE001
                raise AdapterError("Arquivo da resposta com Base64 invalido.") from exc
            files.append((str(item.get("name") or "arquivo.bin"), blob))
    if not text.strip() and not files:
        raise AdapterError("Cole a resposta do chat ou anexe o arquivo devolvido.")
    return ManualReply(text=text, files=files)


def _chat(chat: str) -> tuple[str, str]:
    key = chat if chat in CHAT_URLS else "claude"
    return CHAT_URLS[key], CHAT_LABELS[key]


@dataclass
class ManualVerifier:
    """Verificador pelo chat: entrega o pedido pronto e espera a resposta colada."""

    channel: ManualChannel
    workdir: Path
    run_id: str
    chat: str = "claude"
    _round: int = 0

    def verify(
        self,
        packet: VerificationPacket,
        system_prompt: str,
        *,
        repair_instruction: str | None = None,
    ) -> str:
        self._round += 1
        files: list[dict[str, str]] = []
        for art in packet.artifacts:
            neutral = f"{art.label}{art.path.suffix.lower()}"
            served = f"chat_v{self._round}_{neutral}"
            (self.workdir / served).write_bytes(art.path.read_bytes())
            files.append({"name": neutral, "url": f"/api/runs/{self.run_id}/files/{served}"})

        sys_text = system_prompt if not repair_instruction else f"{system_prompt}\n\n{repair_instruction}"
        prompt = f"{sys_text}\n\n---\n\n{render_raw_packet_text(packet)}"
        if files:
            prompt += (
                "\nARQUIVOS ANEXADOS NESTA CONVERSA: "
                + ", ".join(f["name"] for f in files)
                + "\nResponda SOMENTE com o objeto JSON pedido."
            )
        url, label = _chat(self.chat)
        reply = self.channel.ask(
            "verifier",
            {
                "chat_label": label,
                "chat_url": url,
                "prompt": prompt,
                "files": files,
                "instructions": (
                    f"Abra um chat NOVO no {label} (sem historico, para a verificacao ser cega), "
                    "cole o pedido, anexe os arquivos listados e cole aqui a resposta completa."
                ),
                "expects_files": False,
            },
        )
        return reply.text


@dataclass
class ManualCreator:
    """Criador pelo chat: entrega as correcoes e recebe o texto ou o arquivo revisado."""

    channel: ManualChannel
    workdir: Path
    run_id: str
    chat: str = "claude"

    def revise(
        self,
        correction: CorrectionPrompt,
        previous_submission: CreatorSubmission,
        report: VerificationReport,
    ) -> CreatorSubmission:
        files = []
        for p in previous_submission.deliverable_files:
            p = Path(p)
            if p.is_file() and p.resolve().is_relative_to(self.workdir.resolve()):
                files.append({"name": p.name, "url": f"/api/runs/{self.run_id}/files/{p.name}"})

        prompt = f"{CREATOR_DEFAULT_SYSTEM_PROMPT}\n\n{correction.prompt_text}"
        url, label = _chat(self.chat)
        reply = self.channel.ask(
            "creator",
            {
                "chat_label": label,
                "chat_url": url,
                "prompt": prompt,
                "files": files,
                "instructions": (
                    f"Cole o pedido no chat do {label} (pode ser a mesma conversa em que a entrega foi criada). "
                    "Depois cole aqui o texto revisado OU anexe o arquivo revisado (ex.: a imagem nova)."
                ),
                "expects_files": True,
            },
        )

        if reply.files:
            new_paths: list[Path] = []
            for idx, (name, blob) in enumerate(reply.files, start=1):
                safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in Path(name).name).lstrip(".")
                target = self.workdir / f"rev{correction.iteration}_{idx}_{safe or 'entrega.bin'}"
                target.write_bytes(blob)
                new_paths.append(target)
            return CreatorSubmission(
                markdown_path=previous_submission.markdown_path,
                markdown_text=previous_submission.markdown_text,
                reference_files=previous_submission.reference_files,
                deliverable_files=tuple(new_paths),
                base_dir=previous_submission.base_dir,
            )
        return _apply_text_revision(reply.text, previous_submission, self.workdir)

"""Conectores (adapters) sem dependencias externas para agentes de IA.

Suporta APIs HTTP multimodais (OpenAI-compatible, Anthropic, Gemini),
agentes via linha de comando (CLI) em diretorio isolado e callables Python.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .models import (
    AdapterError,
    ArtifactKind,
    CorrectionPrompt,
    CreatorSubmission,
    VerificationPacket,
    VerificationReport,
)
from .packet import encode_artifact_base64, render_raw_packet_text, write_raw_packet_file


class CallableVerifier:
    """Adapter leve para funcoes Python ou mocks de teste."""

    def __init__(self, fn: Callable[[VerificationPacket, str, str | None], str]) -> None:
        self._fn = fn

    def verify(
        self,
        packet: VerificationPacket,
        system_prompt: str,
        *,
        repair_instruction: str | None = None,
    ) -> str:
        return self._fn(packet, system_prompt, repair_instruction)


class CallableCreator:
    """Adapter leve para agentes criadores definidos como funcao Python."""

    def __init__(
        self,
        fn: Callable[[CorrectionPrompt, CreatorSubmission, VerificationReport], CreatorSubmission],
    ) -> None:
        self._fn = fn

    def revise(
        self,
        correction: CorrectionPrompt,
        previous_submission: CreatorSubmission,
        report: VerificationReport,
    ) -> CreatorSubmission:
        return self._fn(correction, previous_submission, report)


@dataclass(frozen=True)
class OpenAICompatibleVerifier:
    """Conector HTTP para APIs compativeis com OpenAI Chat Completions.

    Compativel com OpenAI, OpenRouter, Ollama, LM Studio, vLLM, Groq, etc.
    """

    model: str
    api_key: str | None = None
    base_url: str = "https://api.openai.com/v1/chat/completions"
    temperature: float = 0.0
    timeout_seconds: float = 90.0
    force_json_mode: bool = True

    def build_payload(
        self,
        packet: VerificationPacket,
        system_prompt: str,
        *,
        repair_instruction: str | None = None,
    ) -> dict[str, Any]:
        user_content: list[dict[str, Any]] = [
            {"type": "text", "text": render_raw_packet_text(packet)}
        ]
        for art in packet.artifacts:
            if art.kind is ArtifactKind.IMAGE:
                b64 = encode_artifact_base64(art)
                user_content.append({"type": "text", "text": f"Imagem {art.label}:"})
                user_content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{art.mime_type};base64,{b64}"},
                    }
                )
        if repair_instruction:
            user_content.append({"type": "text", "text": repair_instruction})

        payload: dict[str, Any] = {
            "model": self.model,
            "temperature": self.temperature,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
        }
        if self.force_json_mode:
            payload["response_format"] = {"type": "json_object"}
        return payload

    def verify(
        self,
        packet: VerificationPacket,
        system_prompt: str,
        *,
        repair_instruction: str | None = None,
    ) -> str:
        token = self.api_key or os.environ.get("OPENAI_API_KEY", "")
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        payload = self.build_payload(
            packet, system_prompt, repair_instruction=repair_instruction
        )
        data = _post_json(self.base_url, payload, headers, self.timeout_seconds)
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AdapterError("Resposta OpenAI sem campo choices[0].message.content.") from exc
        if isinstance(content, list):
            return "\n".join(
                str(part.get("text", "")) for part in content if isinstance(part, dict)
            )
        return str(content)


@dataclass(frozen=True)
class AnthropicVerifier:
    """Conector HTTP para a Anthropic Messages API (Claude) com suporte multimodal."""

    model: str
    api_key: str | None = None
    base_url: str = "https://api.anthropic.com/v1/messages"
    max_tokens: int = 1024
    temperature: float = 0.0
    timeout_seconds: float = 90.0
    api_version: str = "2023-06-01"

    def build_payload(
        self,
        packet: VerificationPacket,
        system_prompt: str,
        *,
        repair_instruction: str | None = None,
    ) -> dict[str, Any]:
        content: list[dict[str, Any]] = [
            {"type": "text", "text": render_raw_packet_text(packet)}
        ]
        for art in packet.artifacts:
            if art.kind is ArtifactKind.IMAGE:
                b64 = encode_artifact_base64(art)
                content.append({"type": "text", "text": f"Imagem {art.label}:"})
                content.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": art.mime_type,
                            "data": b64,
                        },
                    }
                )
            elif art.kind is ArtifactKind.DOCUMENT:
                b64 = encode_artifact_base64(art)
                content.append({"type": "text", "text": f"Documento {art.label}:"})
                content.append(
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": art.mime_type,
                            "data": b64,
                        },
                    }
                )
        if repair_instruction:
            content.append({"type": "text", "text": repair_instruction})

        return {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "system": system_prompt,
            "messages": [{"role": "user", "content": content}],
        }

    def verify(
        self,
        packet: VerificationPacket,
        system_prompt: str,
        *,
        repair_instruction: str | None = None,
    ) -> str:
        token = self.api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not token:
            raise AdapterError("ANTHROPIC_API_KEY nao configurada.")
        headers = {
            "Content-Type": "application/json",
            "x-api-key": token,
            "anthropic-version": self.api_version,
        }
        payload = self.build_payload(
            packet, system_prompt, repair_instruction=repair_instruction
        )
        data = _post_json(self.base_url, payload, headers, self.timeout_seconds)
        blocks = data.get("content")
        if not isinstance(blocks, list) or not blocks:
            raise AdapterError("Resposta Anthropic sem blocos em 'content'.")
        texts = [str(b.get("text", "")) for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(texts).strip()


@dataclass(frozen=True)
class GeminiVerifier:
    """Conector HTTP para a Google Gemini REST API com suporte multimodal."""

    model: str = "gemini-2.5-flash"
    api_key: str | None = None
    base_url: str = "https://generativelanguage.googleapis.com/v1beta/models"
    temperature: float = 0.0
    timeout_seconds: float = 90.0

    def build_payload(
        self,
        packet: VerificationPacket,
        system_prompt: str,
        *,
        repair_instruction: str | None = None,
    ) -> dict[str, Any]:
        parts: list[dict[str, Any]] = [{"text": render_raw_packet_text(packet)}]
        for art in packet.artifacts:
            if art.kind in (ArtifactKind.IMAGE, ArtifactKind.DOCUMENT):
                b64 = encode_artifact_base64(art)
                parts.append({"text": f"Artefato {art.label}:"})
                parts.append(
                    {
                        "inlineData": {
                            "mimeType": art.mime_type,
                            "data": b64,
                        }
                    }
                )
        if repair_instruction:
            parts.append({"text": repair_instruction})

        return {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "temperature": self.temperature,
                "responseMimeType": "application/json",
            },
        }

    def verify(
        self,
        packet: VerificationPacket,
        system_prompt: str,
        *,
        repair_instruction: str | None = None,
    ) -> str:
        token = self.api_key or os.environ.get("GEMINI_API_KEY", "")
        if not token:
            raise AdapterError("GEMINI_API_KEY nao configurada.")
        url = f"{self.base_url.rstrip('/')}/{self.model}:generateContent"
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": token,
        }
        payload = self.build_payload(
            packet, system_prompt, repair_instruction=repair_instruction
        )
        data = _post_json(url, payload, headers, self.timeout_seconds)
        try:
            parts = data["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AdapterError("Resposta Gemini sem candidates[0].content.parts.") from exc
        return "\n".join(str(p.get("text", "")) for p in parts if isinstance(p, dict)).strip()


@dataclass(frozen=True)
class CLIVerifier:
    """Conector que executa um agente verificador local via CLI em pasta isolada.

    Copia apenas os artefatos validados com nomes neutros (REF-1.*, OUT-1.*) e o
    pacote cru para um diretorio temporario, garantindo que o verificador nunca
    tenha acesso ao codigo-fonte ou aos nomes originais dos arquivos.

    Placeholders suportados em `command`:
      {packet_file}, {system_file}, {workdir}
    Se nenhum placeholder for usado, envia system + pacote via stdin.
    """

    command: str | tuple[str, ...]
    timeout_seconds: float = 120.0
    cancel_event: threading.Event | None = None

    def verify(
        self,
        packet: VerificationPacket,
        system_prompt: str,
        *,
        repair_instruction: str | None = None,
    ) -> str:
        with tempfile.TemporaryDirectory(prefix="verify-bridge-") as tmp:
            workdir = Path(tmp)
            packet_file = write_raw_packet_file(packet, workdir / "packet.md")
            system_file = workdir / "system.txt"
            sys_text = system_prompt
            if repair_instruction:
                sys_text = f"{system_prompt}\n\n{repair_instruction}"
            system_file.write_text(sys_text, encoding="utf-8")

            # Copia apenas artefatos validados com nomes neutros (REF-1.png, OUT-1.png).
            for art in packet.artifacts:
                dest = workdir / f"{art.label}{art.path.suffix.lower()}"
                shutil.copy2(art.path, dest)

            tokens = (
                _split_command(self.command)
                if isinstance(self.command, str)
                else self.command
            )
            if not tokens:
                raise AdapterError("Comando CLI do verificador vazio.")

            uses_placeholder = any(
                ph in arg
                for arg in tokens
                for ph in ("{packet_file}", "{system_file}", "{workdir}")
            )
            argv = [
                arg.format(
                    packet_file=str(packet_file),
                    system_file=str(system_file),
                    workdir=str(workdir),
                )
                for arg in tokens
            ]
            stdin_data = (
                None
                if uses_placeholder
                else f"{sys_text}\n\n---\n\n{packet_file.read_text(encoding='utf-8')}"
            )

            try:
                proc = _run_cancellable(
                    argv,
                    stdin_data=stdin_data,
                    cwd=str(workdir),
                    timeout_seconds=self.timeout_seconds,
                    cancel_event=self.cancel_event,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise AdapterError(f"Falha ao executar CLI verificador: {exc}") from exc

            if proc.returncode != 0:
                stderr = (proc.stderr or "").strip()[:400]
                raise AdapterError(
                    f"CLI verificador retornou codigo {proc.returncode}: {stderr}"
                )
            return proc.stdout


@dataclass(frozen=True)
class CLICreator:
    """Conector que aciona um agente criador via CLI para aplicar correcoes.

    Placeholders suportados em `command`:
      {correction_file}, {report_file}, {iteration}
    Apos a execucao, reenvia a submissao (assumindo que o criador atualizou os
    arquivos de entrega no local combinado).
    """

    command: str | tuple[str, ...]
    workdir: Path | None = None
    timeout_seconds: float = 300.0
    cancel_event: threading.Event | None = None

    def revise(
        self,
        correction: CorrectionPrompt,
        previous_submission: CreatorSubmission,
        report: VerificationReport,
    ) -> CreatorSubmission:
        with tempfile.TemporaryDirectory(prefix="verify-bridge-fix-") as tmp:
            tmp_dir = Path(tmp)
            corr_file = tmp_dir / "correction.md"
            report_file = tmp_dir / "report.json"
            corr_file.write_text(correction.prompt_text, encoding="utf-8")
            report_file.write_text(
                json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            tokens = (
                _split_command(self.command)
                if isinstance(self.command, str)
                else self.command
            )
            if not tokens:
                raise AdapterError("Comando CLI do criador vazio.")

            uses_placeholder = any(
                ph in arg
                for arg in tokens
                for ph in ("{correction_file}", "{report_file}", "{iteration}")
            )
            argv = [
                arg.format(
                    correction_file=str(corr_file),
                    report_file=str(report_file),
                    iteration=correction.iteration,
                )
                for arg in tokens
            ]
            stdin_data = None if uses_placeholder else correction.prompt_text

            try:
                proc = _run_cancellable(
                    argv,
                    stdin_data=stdin_data,
                    cwd=str(self.workdir or previous_submission.base_dir or Path.cwd()),
                    timeout_seconds=self.timeout_seconds,
                    cancel_event=self.cancel_event,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise AdapterError(f"Falha ao executar CLI criador: {exc}") from exc

            if proc.returncode != 0:
                stderr = (proc.stderr or "").strip()[:400]
                raise AdapterError(
                    f"CLI criador retornou codigo {proc.returncode}: {stderr}"
                )

        return previous_submission


CREATOR_DEFAULT_SYSTEM_PROMPT = (
    "Voce e o agente criador/executor. Aplique estritamente as correcoes obrigatorias "
    "para atender ao pedido inicial. Retorne apenas a entrega revisada, sem justificativas "
    "nem comentarios sobre como fez."
)


def _build_creator_user_text(
    correction: CorrectionPrompt,
    previous_submission: CreatorSubmission,
) -> str:
    parts = [correction.prompt_text.strip()]
    for path in previous_submission.deliverable_files:
        if path.exists() and path.is_file() and path.suffix.lower() in (".txt", ".md", ".csv", ".json", ".html", ".svg", ".xml"):
            content = path.read_text(encoding="utf-8", errors="replace")[:16000]
            parts.append(f"\nENTREGA ANTERIOR ({path.name}):\n{content}")
    return "\n".join(parts)


def _apply_text_revision(
    revised_text: str,
    previous_submission: CreatorSubmission,
    output_dir: Path | None,
) -> CreatorSubmission:
    """Grava a revisao do agente criador em arquivo de entrega para a proxima rodada."""
    target: Path | None = None
    for path in previous_submission.deliverable_files:
        if path.suffix.lower() in (".txt", ".md", ".csv", ".json", ".html", ".svg", ".xml"):
            target = path
            break

    if target is None:
        base = output_dir or previous_submission.base_dir or Path.cwd()
        base.mkdir(parents=True, exist_ok=True)
        target = base / "entrega_revisada.txt"

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(revised_text.strip() + "\n", encoding="utf-8")

    new_deliverables = list(previous_submission.deliverable_files)
    if target not in new_deliverables:
        new_deliverables.append(target)

    return CreatorSubmission(
        markdown_path=previous_submission.markdown_path,
        markdown_text=previous_submission.markdown_text,
        reference_files=previous_submission.reference_files,
        deliverable_files=tuple(new_deliverables),
        base_dir=previous_submission.base_dir or target.parent,
    )


@dataclass(frozen=True)
class OpenAICompatibleCreator:
    """Agente criador via API compativel com OpenAI (OpenAI, OpenRouter, Ollama, etc.)."""

    model: str
    api_key: str | None = None
    base_url: str = "https://api.openai.com/v1/chat/completions"
    system_prompt: str = CREATOR_DEFAULT_SYSTEM_PROMPT
    temperature: float = 0.2
    timeout_seconds: float = 90.0
    output_dir: Path | None = None

    def build_payload(
        self,
        correction: CorrectionPrompt,
        previous_submission: CreatorSubmission,
    ) -> dict[str, Any]:
        return {
            "model": self.model,
            "temperature": self.temperature,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": _build_creator_user_text(correction, previous_submission)},
            ],
        }

    def revise(
        self,
        correction: CorrectionPrompt,
        previous_submission: CreatorSubmission,
        report: VerificationReport,
    ) -> CreatorSubmission:
        token = self.api_key or os.environ.get("OPENAI_API_KEY", "")
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        payload = self.build_payload(correction, previous_submission)
        data = _post_json(self.base_url, payload, headers, self.timeout_seconds)
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AdapterError("Resposta OpenAI (criador) sem choices[0].message.content.") from exc
        revised = (
            "\n".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
            if isinstance(content, list)
            else str(content)
        )
        return _apply_text_revision(revised, previous_submission, self.output_dir)


@dataclass(frozen=True)
class AnthropicCreator:
    """Agente criador via Anthropic Messages API (Claude)."""

    model: str
    api_key: str | None = None
    base_url: str = "https://api.anthropic.com/v1/messages"
    system_prompt: str = CREATOR_DEFAULT_SYSTEM_PROMPT
    max_tokens: int = 2048
    temperature: float = 0.2
    timeout_seconds: float = 90.0
    api_version: str = "2023-06-01"
    output_dir: Path | None = None

    def build_payload(
        self,
        correction: CorrectionPrompt,
        previous_submission: CreatorSubmission,
    ) -> dict[str, Any]:
        return {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "system": self.system_prompt,
            "messages": [
                {"role": "user", "content": _build_creator_user_text(correction, previous_submission)}
            ],
        }

    def revise(
        self,
        correction: CorrectionPrompt,
        previous_submission: CreatorSubmission,
        report: VerificationReport,
    ) -> CreatorSubmission:
        token = self.api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not token:
            raise AdapterError("ANTHROPIC_API_KEY nao configurada para o criador.")
        headers = {
            "Content-Type": "application/json",
            "x-api-key": token,
            "anthropic-version": self.api_version,
        }
        payload = self.build_payload(correction, previous_submission)
        data = _post_json(self.base_url, payload, headers, self.timeout_seconds)
        blocks = data.get("content")
        if not isinstance(blocks, list) or not blocks:
            raise AdapterError("Resposta Anthropic (criador) sem blocos em 'content'.")
        revised = "\n".join(
            str(b.get("text", "")) for b in blocks if isinstance(b, dict) and b.get("type") == "text"
        )
        return _apply_text_revision(revised, previous_submission, self.output_dir)


@dataclass(frozen=True)
class GeminiCreator:
    """Agente criador via Google Gemini REST API."""

    model: str = "gemini-2.5-flash"
    api_key: str | None = None
    base_url: str = "https://generativelanguage.googleapis.com/v1beta/models"
    system_prompt: str = CREATOR_DEFAULT_SYSTEM_PROMPT
    temperature: float = 0.2
    timeout_seconds: float = 90.0
    output_dir: Path | None = None

    def build_payload(
        self,
        correction: CorrectionPrompt,
        previous_submission: CreatorSubmission,
    ) -> dict[str, Any]:
        return {
            "systemInstruction": {"parts": [{"text": self.system_prompt}]},
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": _build_creator_user_text(correction, previous_submission)}],
                }
            ],
            "generationConfig": {"temperature": self.temperature},
        }

    def revise(
        self,
        correction: CorrectionPrompt,
        previous_submission: CreatorSubmission,
        report: VerificationReport,
    ) -> CreatorSubmission:
        token = self.api_key or os.environ.get("GEMINI_API_KEY", "")
        if not token:
            raise AdapterError("GEMINI_API_KEY nao configurada para o criador.")
        url = f"{self.base_url.rstrip('/')}/{self.model}:generateContent"
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": token,
        }
        payload = self.build_payload(correction, previous_submission)
        data = _post_json(url, payload, headers, self.timeout_seconds)
        try:
            parts = data["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AdapterError("Resposta Gemini (criador) sem candidates[0].content.parts.") from exc
        revised = "\n".join(str(p.get("text", "")) for p in parts if isinstance(p, dict))
        return _apply_text_revision(revised, previous_submission, self.output_dir)


def _split_command(command: str) -> tuple[str, ...]:
    """Divide um comando digitado pelo usuario em argumentos.

    No Windows, `shlex` em modo nao-POSIX mantem as aspas dentro do argumento
    (`"C:\\Minha Pasta\\x"` viraria um caminho com aspas e falharia); aqui as
    aspas externas sao removidas, preservando as barras invertidas do caminho.
    """
    if os.name != "nt":
        return tuple(shlex.split(command, posix=True))
    tokens = []
    for tok in shlex.split(command, posix=False):
        if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "\"'":
            tok = tok[1:-1]
        tokens.append(tok)
    return tuple(tokens)


class CancelledByUser(AdapterError):
    """O usuario pediu parada enquanto um agente CLI estava rodando."""


def _kill_process_tree(proc: subprocess.Popen[str]) -> None:
    """Encerra o processo e os filhos dele (ex.: shell que chamou outro programa)."""
    if proc.poll() is not None:
        return
    try:
        if os.name != "nt":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            # /T encerra tambem os processos filhos (ex.: claude.cmd -> node).
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                check=False,
            )
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()


def _run_cancellable(
    argv: list[str],
    *,
    stdin_data: str | None,
    cwd: str,
    timeout_seconds: float,
    cancel_event: threading.Event | None,
    poll_interval: float = 0.1,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Como `subprocess.run`, mas mata o processo na hora se `cancel_event` disparar.

    Levanta `subprocess.TimeoutExpired` no estouro de tempo (mesmo contrato do
    `subprocess.run`) e `CancelledByUser` quando o usuario aperta "Parar".
    """
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
        env=env,
        # Grupo de processos proprio para conseguir matar tambem os filhos.
        start_new_session=(os.name != "nt"),
    )
    outcome: dict[str, Any] = {}

    def _communicate() -> None:
        try:
            outcome["out"] = proc.communicate(input=stdin_data)
        except Exception as exc:  # noqa: BLE001
            outcome["exc"] = exc

    reader = threading.Thread(target=_communicate, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout_seconds

    while reader.is_alive():
        reader.join(poll_interval)
        if not reader.is_alive():
            break
        if cancel_event is not None and cancel_event.is_set():
            _kill_process_tree(proc)
            reader.join(5)
            raise CancelledByUser("Execucao CLI interrompida pelo usuario.")
        if time.monotonic() >= deadline:
            _kill_process_tree(proc)
            reader.join(5)
            raise subprocess.TimeoutExpired(argv, timeout_seconds)

    if "exc" in outcome:
        raise outcome["exc"]
    stdout, stderr = outcome.get("out", ("", ""))
    return subprocess.CompletedProcess(argv, proc.returncode, stdout or "", stderr or "")


def _post_json(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout_seconds: float,
) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")[:400]
        raise AdapterError(f"Erro HTTP {exc.code} em {url}: {err_body}") from exc
    except urllib.error.URLError as exc:
        raise AdapterError(f"Falha de rede em {url}: {exc.reason}") from exc

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AdapterError("API retornou corpo que nao e JSON valido.") from exc
    if not isinstance(parsed, dict):
        raise AdapterError("API retornou JSON inesperado (nao e objeto).")
    return parsed

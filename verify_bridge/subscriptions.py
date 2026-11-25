"""Conectores por ASSINATURA (sem chave de API).

Usa os programas oficiais de cada assinatura, que ja guardam o login do usuario:

- Claude Pro/Max ........ Claude Code  (`claude`)
- Google (Gemini) ....... Antigravity  (`agy`)  -- o Gemini CLI nao aceita mais
                                                  login de conta pessoal
- ChatGPT Plus/Pro ...... Codex CLI    (`codex`)

Regras de seguranca e de cegueira:
- As chaves de API conhecidas sao REMOVIDAS do ambiente do processo filho, para
  a cobranca ir para a assinatura e nunca para uma API por engano.
- O verificador roda numa pasta temporaria isolada, so com os arquivos ja
  filtrados e renomeados (REF-1.png, OUT-1.png), e so com ferramenta de leitura.
- No Claude Code, `--setting-sources ""` impede que hooks, memorias e CLAUDE.md
  do usuario entrem no contexto do verificador.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import tempfile
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .adapters import CREATOR_DEFAULT_SYSTEM_PROMPT, _run_cancellable
from .models import (
    AdapterError,
    CorrectionPrompt,
    CreatorSubmission,
    VerificationPacket,
    VerificationReport,
)
from .packet import render_raw_packet_text

# ---------------------------------------------------------------------------
# Catalogo das ferramentas de assinatura
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SubscriptionTool:
    """Descreve como acionar o programa oficial de uma assinatura."""

    id: str
    label: str
    plan: str
    binary: str
    strip_env: tuple[str, ...]
    chat_url: str
    login_hint: str
    build_verifier_argv: Callable[[str, list[str], str], tuple[list[str], str | None]]
    build_creator_argv: Callable[[str, str], tuple[list[str], str | None]]


def _claude_verifier(prompt: str, files: list[str], model: str) -> tuple[list[str], str | None]:
    argv = [
        "claude", "-p",
        "--setting-sources", "",
        "--no-session-persistence",
        "--tools", "Read",
        "--allowedTools", "Read",
    ]
    if model:
        argv += ["--model", model]
    return argv, prompt  # prompt via stdin


def _claude_creator(prompt: str, model: str) -> tuple[list[str], str | None]:
    argv = [
        "claude", "-p",
        "--setting-sources", "",
        "--no-session-persistence",
        "--tools", "Read,Edit,Write",
        "--allowedTools", "Read,Edit,Write",
        "--permission-mode", "acceptEdits",
    ]
    if model:
        argv += ["--model", model]
    return argv, prompt


def _agy_verifier(prompt: str, files: list[str], model: str) -> tuple[list[str], str | None]:
    argv = ["agy", "--mode", "plan", "--disable-slash-commands"]
    if model:
        argv += ["--model", model]
    return argv + ["-p", prompt], None


def _agy_creator(prompt: str, model: str) -> tuple[list[str], str | None]:
    argv = ["agy", "--mode", "accept-edits", "--disable-slash-commands"]
    if model:
        argv += ["--model", model]
    return argv + ["-p", prompt], None


def _codex_verifier(prompt: str, files: list[str], model: str) -> tuple[list[str], str | None]:
    argv = ["codex", "exec", "--skip-git-repo-check", "--sandbox", "read-only"]
    for f in files:
        if Path(f).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
            argv += ["-i", f]
    if model:
        argv += ["-m", model]
    return argv + [prompt], None


def _codex_creator(prompt: str, model: str) -> tuple[list[str], str | None]:
    argv = ["codex", "exec", "--skip-git-repo-check", "--sandbox", "workspace-write"]
    if model:
        argv += ["-m", model]
    return argv + [prompt], None


TOOLS: dict[str, SubscriptionTool] = {
    "claude": SubscriptionTool(
        id="claude",
        label="Claude (Claude Code)",
        plan="Claude Pro / Max",
        binary="claude",
        strip_env=("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
        chat_url="https://claude.ai/new",
        login_hint="Abre a pagina de login do Claude no navegador.",
        build_verifier_argv=_claude_verifier,
        build_creator_argv=_claude_creator,
    ),
    "antigravity": SubscriptionTool(
        id="antigravity",
        label="Google Gemini (Antigravity)",
        plan="Conta Google / Google AI Pro",
        binary="agy",
        strip_env=("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        chat_url="https://gemini.google.com/app",
        login_hint="Abre a janela de login do Antigravity.",
        build_verifier_argv=_agy_verifier,
        build_creator_argv=_agy_creator,
    ),
    "codex": SubscriptionTool(
        id="codex",
        label="ChatGPT (Codex)",
        plan="ChatGPT Plus / Pro",
        binary="codex",
        strip_env=("OPENAI_API_KEY",),
        chat_url="https://chatgpt.com/",
        login_hint="Abre a pagina de login do ChatGPT no navegador.",
        build_verifier_argv=_codex_verifier,
        build_creator_argv=_codex_creator,
    ),
}

INSTALL_URLS: dict[str, str] = {
    "claude": "https://claude.com/claude-code",
    "antigravity": "https://antigravity.google",
    "codex": "https://developers.openai.com/codex/cli",
}


def get_tool(tool_id: str) -> SubscriptionTool:
    tool = TOOLS.get(tool_id)
    if tool is None:
        raise AdapterError(f"Assinatura desconhecida: {tool_id}")
    return tool


def subscription_env(tool: SubscriptionTool, base: dict[str, str] | None = None) -> dict[str, str]:
    """Ambiente do processo filho sem as chaves de API daquela plataforma."""
    env = dict(os.environ if base is None else base)
    for key in tool.strip_env:
        env.pop(key, None)
    return env


# ---------------------------------------------------------------------------
# Deteccao: instalado? logado?
# ---------------------------------------------------------------------------


@dataclass
class _StatusCache:
    ttl_seconds: float = 30.0
    lock: threading.Lock = field(default_factory=threading.Lock)
    data: dict[str, tuple[float, dict]] = field(default_factory=dict)


_CACHE = _StatusCache()


def _resolve(argv: list[str]) -> list[str]:
    """Caminho completo do programa. Necessario no Windows, onde `claude`/`codex`
    costumam ser `claude.cmd`/`codex.cmd` e o sistema nao os acha so pelo nome."""
    return [shutil.which(argv[0]) or argv[0], *argv[1:]]


def _quick(argv: list[str], env: dict[str, str], timeout: float = 20.0) -> subprocess.CompletedProcess[str] | None:
    argv = _resolve(argv)
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env=env,
            cwd=tempfile.gettempdir(),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def check_status(tool_id: str, *, use_cache: bool = True) -> dict:
    """Retorna {id, label, plan, installed, logged_in, detail, chat_url, install_url}."""
    if use_cache:
        with _CACHE.lock:
            hit = _CACHE.data.get(tool_id)
            if hit and time.monotonic() - hit[0] < _CACHE.ttl_seconds:
                return dict(hit[1])

    tool = get_tool(tool_id)
    info: dict = {
        "id": tool.id,
        "label": tool.label,
        "plan": tool.plan,
        "installed": False,
        "logged_in": False,
        "detail": "",
        "chat_url": tool.chat_url,
        "install_url": INSTALL_URLS.get(tool.id, ""),
        "login_hint": tool.login_hint,
    }
    if shutil.which(tool.binary) is None:
        info["detail"] = "Programa nao instalado neste computador."
    else:
        info["installed"] = True
        env = subscription_env(tool)
        if tool.id == "claude":
            proc = _quick(["claude", "auth", "status"], env)
            try:
                st = json.loads(proc.stdout) if proc and proc.stdout.strip() else {}
            except json.JSONDecodeError:
                st = {}
            if st.get("loggedIn") and st.get("authMethod") == "claude.ai":
                info["logged_in"] = True
                info["detail"] = "Logado com a assinatura."
            elif st.get("loggedIn"):
                info["detail"] = "Logado via API (Console), nao pela assinatura."
            else:
                info["detail"] = "Instalado, mas sem login."
        elif tool.id == "antigravity":
            proc = _quick(["agy", "models"], env)
            if proc is not None and proc.returncode == 0:
                info["logged_in"] = True
                info["detail"] = "Logado com a conta Google."
            else:
                info["detail"] = "Instalado, mas sem login."
        elif tool.id == "codex":
            proc = _quick(["codex", "login", "status"], env)
            if proc is not None and proc.returncode == 0:
                info["logged_in"] = True
                info["detail"] = "Logado com a conta ChatGPT."
            else:
                info["detail"] = "Instalado, mas sem login."

    with _CACHE.lock:
        _CACHE.data[tool_id] = (time.monotonic(), dict(info))
    return info


def check_all_status(*, use_cache: bool = True) -> list[dict]:
    return [check_status(t, use_cache=use_cache) for t in TOOLS]


def invalidate_status_cache() -> None:
    with _CACHE.lock:
        _CACHE.data.clear()


def start_login(tool_id: str) -> dict:
    """Dispara o login oficial da assinatura (abre navegador ou janela de login).

    Nao espera terminar: a tela consulta o status de novo depois.
    """
    tool = get_tool(tool_id)
    invalidate_status_cache()
    if shutil.which(tool.binary) is None:
        url = INSTALL_URLS.get(tool.id, "")
        if url:
            webbrowser.open(url)
        return {"started": False, "message": f"{tool.label} nao esta instalado. Abri a pagina de instalacao.", "url": url}

    env = subscription_env(tool)
    if tool.id == "claude":
        argv = ["claude", "auth", "login", "--claudeai"]
    elif tool.id == "codex":
        argv = ["codex", "login"]
    else:  # antigravity: o login acontece na primeira abertura interativa do agy
        if platform.system() == "Darwin":
            subprocess.Popen(["open", "-a", "Terminal", shutil.which("agy") or "agy"])  # noqa: S603,S607
            return {"started": True, "message": "Abri a janela de login do Antigravity. Entre com sua conta Google e feche a janela.", "url": ""}
        return {"started": False, "message": "Abra o programa 'agy' uma vez para entrar com sua conta Google.", "url": INSTALL_URLS["antigravity"]}

    log_path = Path(tempfile.gettempdir()) / f"verify-bridge-login-{tool.id}.log"
    log = log_path.open("w", encoding="utf-8")
    subprocess.Popen(  # noqa: S603
        _resolve(argv),
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        env=env,
        cwd=tempfile.gettempdir(),
        start_new_session=(os.name != "nt"),
    )
    return {"started": True, "message": tool.login_hint + " Conclua o login no navegador e volte para esta tela.", "url": ""}


# ---------------------------------------------------------------------------
# Agentes
# ---------------------------------------------------------------------------

VERIFIER_FILE_INSTRUCTION = (
    "ARQUIVOS PARA ANALISAR (estao na pasta atual; abra cada um antes de avaliar): {files}\n"
    "Responda SOMENTE com o objeto JSON pedido, sem texto antes ou depois."
)


@dataclass(frozen=True)
class SubscriptionVerifier:
    """Verificador cego usando o programa oficial da assinatura (sem API)."""

    tool_id: str
    model: str = ""
    timeout_seconds: float = 600.0
    cancel_event: threading.Event | None = None

    def verify(
        self,
        packet: VerificationPacket,
        system_prompt: str,
        *,
        repair_instruction: str | None = None,
    ) -> str:
        tool = get_tool(self.tool_id)
        if shutil.which(tool.binary) is None:
            raise AdapterError(f"{tool.label} nao esta instalado neste computador.")

        with tempfile.TemporaryDirectory(prefix="verify-bridge-sub-") as tmp:
            workdir = Path(tmp)
            names: list[str] = []
            for art in packet.artifacts:
                name = f"{art.label}{art.path.suffix.lower()}"
                shutil.copy2(art.path, workdir / name)
                names.append(name)

            sys_text = system_prompt if not repair_instruction else f"{system_prompt}\n\n{repair_instruction}"
            prompt = f"{sys_text}\n\n---\n\n{render_raw_packet_text(packet)}"
            if names:
                prompt += "\n" + VERIFIER_FILE_INSTRUCTION.format(files=", ".join(names))

            argv, stdin_data = tool.build_verifier_argv(prompt, [str(workdir / n) for n in names], self.model)
            proc = _run_tool(tool, argv, stdin_data, str(workdir), self.timeout_seconds, self.cancel_event, "verificador")
            return proc.stdout


@dataclass(frozen=True)
class SubscriptionCreator:
    """Criador usando o programa oficial da assinatura; edita as entregas no lugar."""

    tool_id: str
    workdir: Path
    model: str = ""
    timeout_seconds: float = 900.0
    cancel_event: threading.Event | None = None

    def revise(
        self,
        correction: CorrectionPrompt,
        previous_submission: CreatorSubmission,
        report: VerificationReport,
    ) -> CreatorSubmission:
        tool = get_tool(self.tool_id)
        if shutil.which(tool.binary) is None:
            raise AdapterError(f"{tool.label} nao esta instalado neste computador.")

        def rel(paths: tuple[Path, ...]) -> str:
            out = []
            for p in paths:
                try:
                    out.append(str(Path(p).resolve().relative_to(self.workdir.resolve())))
                except ValueError:
                    out.append(str(p))
            return ", ".join(out) or "(nenhum)"

        prompt = (
            f"{CREATOR_DEFAULT_SYSTEM_PROMPT}\n\n"
            f"{correction.prompt_text}\n\n"
            f"ARQUIVOS DE ENTREGA (edite estes arquivos diretamente, na pasta atual): "
            f"{rel(previous_submission.deliverable_files)}\n"
            f"ARQUIVOS DE REFERENCIA (somente leitura): {rel(previous_submission.reference_files)}\n"
            "Nao crie arquivos novos alem das entregas. Ao terminar, responda apenas: PRONTO."
        )
        argv, stdin_data = tool.build_creator_argv(prompt, self.model)
        _run_tool(tool, argv, stdin_data, str(self.workdir), self.timeout_seconds, self.cancel_event, "criador")
        return previous_submission


def _run_tool(
    tool: SubscriptionTool,
    argv: list[str],
    stdin_data: str | None,
    cwd: str,
    timeout_seconds: float,
    cancel_event: threading.Event | None,
    role: str,
) -> subprocess.CompletedProcess[str]:
    argv = _resolve(argv)
    try:
        proc = _run_cancellable(
            argv,
            stdin_data=stdin_data,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            cancel_event=cancel_event,
            env=subscription_env(tool),
        )
    except subprocess.TimeoutExpired as exc:
        raise AdapterError(f"{tool.label} ({role}) excedeu {timeout_seconds:.0f}s sem responder.") from exc
    except OSError as exc:
        raise AdapterError(f"Falha ao abrir {tool.label} ({role}): {exc}") from exc

    if proc.returncode != 0:
        detail = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip()[:400]
        if any(k in detail.lower() for k in ("not logged in", "/login", "login", "unauthorized", "authenticate")):
            raise AdapterError(f"{tool.label}: e preciso fazer login. Use o botao 'Entrar' na tela. ({detail})")
        raise AdapterError(f"{tool.label} ({role}) retornou codigo {proc.returncode}: {detail}")
    return proc

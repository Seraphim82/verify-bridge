"""Interface de linha de comando (CLI) do verify-bridge."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .adapters import (
    AnthropicVerifier,
    CLICreator,
    CLIVerifier,
    GeminiVerifier,
    OpenAICompatibleVerifier,
)
from .models import (
    CreatorSubmission,
    VerifyBridgeError,
)
from .orchestrator import LoopConfig, VerifierAgent, run_verification_loop, verify_once
from .packet import render_raw_packet_text, write_raw_packet_file
from .sanitizer import SanitizerConfig, collect_and_sanitize
from .subscriptions import SubscriptionCreator, SubscriptionVerifier

SUB_PROVIDERS = ("sub_claude", "sub_antigravity", "sub_codex")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="verify-bridge",
        description="Coletor e ponte de verificacao cega entre agente criador e verificador.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # compile
    p_compile = sub.add_parser(
        "compile",
        help="Sanitiza MD e arquivos do criador e gera o pacote cru para verificacao.",
    )
    _add_input_args(p_compile)
    p_compile.add_argument("-o", "--output", type=Path, help="Salva pacote cru em arquivo.")
    p_compile.add_argument(
        "--audit", action="store_true", help="Exibe relatorio local do que foi removido."
    )

    # verify
    p_verify = sub.add_parser(
        "verify",
        help="Executa uma rodada de verificacao cega e gera o prompt de correcao.",
    )
    _add_input_args(p_verify)
    _add_verifier_args(p_verify)
    p_verify.add_argument("--json", action="store_true", help="Saida completa em JSON.")
    p_verify.add_argument(
        "--include-acceptable",
        action="store_true",
        help="Inclui observacoes aceitaveis no prompt de correcao.",
    )

    # loop
    p_loop = sub.add_parser(
        "loop",
        help="Executa o loop criador <-> verificador ate aprovacao ou parada segura.",
    )
    _add_input_args(p_loop)
    _add_verifier_args(p_loop)
    p_loop.add_argument(
        "--creator-cmd",
        default="",
        help="Comando CLI do agente criador (suporta {correction_file}, {report_file}, {iteration}).",
    )
    p_loop.add_argument(
        "--creator-provider",
        choices=SUB_PROVIDERS,
        default=None,
        help="Usa a assinatura (sem API) como agente criador, editando as entregas no lugar.",
    )
    p_loop.add_argument("--creator-model", default="", help="Modelo do criador por assinatura (opcional).")
    p_loop.add_argument("--max-iter", type=int, default=5, help="Limite maximo de rodadas.")
    p_loop.add_argument(
        "--stagnation",
        type=int,
        default=2,
        help="Para se as mesmas falhas bloqueantes repetirem N vezes.",
    )
    p_loop.add_argument("--report-out", type=Path, help="Salva relatorio completo do loop em JSON.")

    # ui
    p_ui = sub.add_parser(
        "ui",
        help="Abre a interface visual no navegador para linkar as duas IAs e acompanhar o loop.",
    )
    p_ui.add_argument("--host", default="127.0.0.1", help="Host local (padrao: 127.0.0.1).")
    p_ui.add_argument("--port", type=int, default=8765, help="Porta HTTP (padrao: 8765).")
    p_ui.add_argument(
        "--no-browser",
        action="store_true",
        help="Inicia o servidor sem abrir automaticamente o navegador.",
    )
    return parser


def _add_input_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("markdown", type=Path, help="Arquivo Markdown vindo do agente criador.")
    parser.add_argument(
        "--ref",
        type=Path,
        action="append",
        default=[],
        help="Arquivo de referencia do usuario (pode repetir).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        action="append",
        default=[],
        help="Arquivo entregue pelo criador (pode repetir).",
    )
    parser.add_argument(
        "--allow-structured",
        action="store_true",
        help="Permite artefatos .html/.svg/.json/.xml como entrega verificavel.",
    )


def _add_verifier_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--provider",
        choices=("openai", "anthropic", "gemini", "cli", *SUB_PROVIDERS),
        required=True,
        help="Conector do agente verificador (sub_* usa a assinatura, sem chave de API).",
    )
    parser.add_argument("--model", default="", help="Nome do modelo de IA.")
    parser.add_argument("--base-url", default="", help="URL customizada (ex.: Ollama/OpenRouter).")
    parser.add_argument(
        "--cli-cmd",
        default="",
        help="Comando CLI quando --provider=cli (suporta {packet_file}, {system_file}, {workdir}).",
    )


def _make_verifier(args: argparse.Namespace) -> VerifierAgent:
    provider = args.provider
    if provider in SUB_PROVIDERS:
        return SubscriptionVerifier(tool_id=provider[len("sub_"):], model=args.model)
    if provider == "openai":
        return OpenAICompatibleVerifier(
            model=args.model or "gpt-4o-mini",
            base_url=args.base_url or "https://api.openai.com/v1/chat/completions",
        )
    if provider == "anthropic":
        return AnthropicVerifier(
            model=args.model or "claude-sonnet-5-5",
            base_url=args.base_url or "https://api.anthropic.com/v1/messages",
        )
    if provider == "gemini":
        return GeminiVerifier(
            model=args.model or "gemini-2.5-flash",
            base_url=args.base_url or "https://generativelanguage.googleapis.com/v1beta/models",
        )
    if provider == "cli":
        if not args.cli_cmd:
            raise VerifyBridgeError("--cli-cmd e obrigatorio quando --provider=cli.")
        return CLIVerifier(command=args.cli_cmd)
    raise VerifyBridgeError(f"Provider desconhecido: {provider}")


def _cmd_compile(args: argparse.Namespace) -> int:
    sub = CreatorSubmission(
        markdown_path=args.markdown,
        reference_files=tuple(args.ref),
        deliverable_files=tuple(args.out),
    )
    cfg = SanitizerConfig(allow_structured_outputs=args.allow_structured)
    packet, audit = collect_and_sanitize(sub, config=cfg)

    if args.output:
        write_raw_packet_file(packet, args.output)
        print(f"Pacote cru salvo em: {args.output}")
    else:
        print(render_raw_packet_text(packet), end="")

    if args.audit:
        print("\n--- AUDITORIA LOCAL (NAO ENVIADA AO VERIFICADOR) ---", file=sys.stderr)
        print(json.dumps(audit.to_dict(), ensure_ascii=False, indent=2), file=sys.stderr)
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    sub = CreatorSubmission(
        markdown_path=args.markdown,
        reference_files=tuple(args.ref),
        deliverable_files=tuple(args.out),
    )
    cfg = SanitizerConfig(allow_structured_outputs=args.allow_structured)
    verifier = _make_verifier(args)
    packet, audit, report, correction = verify_once(
        sub,
        verifier,
        sanitizer_config=cfg,
        include_acceptable_in_prompt=args.include_acceptable,
    )

    if args.json:
        print(
            json.dumps(
                {
                    "pacote": packet.to_dict(),
                    "sanitizacao": audit.to_dict(),
                    "relatorio": report.to_dict(),
                    "prompt_correcao": correction.prompt_text,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(f"STATUS: {report.status.value.upper()} | Contagem: {report.counts}")
        print("")
        print(correction.prompt_text, end="")

    return 0 if report.approved else 2


def _cmd_loop(args: argparse.Namespace) -> int:
    sub = CreatorSubmission(
        markdown_path=args.markdown,
        reference_files=tuple(args.ref),
        deliverable_files=tuple(args.out),
    )
    verifier = _make_verifier(args)
    if args.creator_provider:
        out_dirs = {Path(p).resolve().parent for p in args.out}
        if len(out_dirs) != 1:
            raise VerifyBridgeError(
                "--creator-provider exige que todas as entregas (--out) estejam na mesma pasta."
            )
        creator = SubscriptionCreator(
            tool_id=args.creator_provider[len("sub_"):],
            workdir=out_dirs.pop(),
            model=args.creator_model,
        )
    elif args.creator_cmd:
        creator = CLICreator(command=args.creator_cmd)
    else:
        raise VerifyBridgeError("Informe --creator-cmd ou --creator-provider.")
    cfg = LoopConfig(
        max_iterations=args.max_iter,
        stagnation_limit=args.stagnation,
        sanitizer=SanitizerConfig(allow_structured_outputs=args.allow_structured),
        on_iteration=lambda rec: print(
            f"[Rodada {rec.iteration}] "
            + (
                f"{rec.report.status.value.upper()} {rec.report.counts}"
                if rec.report
                else f"ERRO: {rec.error}"
            ),
            file=sys.stderr,
        ),
    )
    result = run_verification_loop(sub, verifier, creator, config=cfg)

    if args.report_out:
        args.report_out.parent.mkdir(parents=True, exist_ok=True)
        args.report_out.write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    print(
        f"RESULTADO FINAL: {'APROVADO' if result.approved else 'NAO APROVADO'} "
        f"({result.stop_reason.value}, {len(result.iterations)} rodada(s))"
    )
    if result.final_correction_prompt and not result.approved:
        print("")
        print(result.final_correction_prompt.prompt_text, end="")
    return 0 if result.approved else 2


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "compile":
            return _cmd_compile(args)
        if args.command == "verify":
            return _cmd_verify(args)
        if args.command == "loop":
            return _cmd_loop(args)
        if args.command == "ui":
            from .web import run_ui

            return run_ui(host=args.host, port=args.port, open_browser=not args.no_browser)
    except VerifyBridgeError as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

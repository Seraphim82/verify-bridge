"""Interface de linha de comando (CLI) do verify-bridge."""

from __future__ import annotations

import argparse
import json
import struct
import sys
import tempfile
import zlib
from pathlib import Path

from .adapters import (
    AnthropicVerifier,
    CLICreator,
    CLIVerifier,
    CallableCreator,
    CallableVerifier,
    GeminiVerifier,
    OpenAICompatibleVerifier,
)
from .models import (
    CreatorSubmission,
    VerificationPacket,
    VerificationReport,
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

    # demo
    sub.add_parser(
        "demo",
        help="Executa demonstracao offline completa (cenario do peixe dourado em 3 rodadas).",
    )

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


def _make_minimal_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """Gera PNG valido minimo em memoria sem dependencias externas."""
    r, g, b = rgb
    raw_rows = b"".join(b"\x00" + bytes([r, g, b]) * width for _ in range(height))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(tag + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw_rows))
        + chunk(b"IEND", b"")
    )


def _cmd_demo() -> int:
    with tempfile.TemporaryDirectory(prefix="verify-bridge-demo-") as tmp:
        root = Path(tmp)
        ref_img = root / "usuario_peixe.png"
        out_img = root / "entrega_peixe.png"
        code_file = root / "gerador.py"
        req_md = root / "pedido_criador.md"

        ref_img.write_bytes(_make_minimal_png(128, 128, (235, 180, 30)))
        out_img.write_bytes(_make_minimal_png(512, 512, (60, 120, 220)))
        code_file.write_text("print('codigo interno')\n", encoding="utf-8")

        req_md.write_text(
            "# Pedido Inicial\n"
            "Gere uma imagem de um peixe dourado identico ao da imagem de referencia,\n"
            "pequena (ate 256x256), limpa e com maior qualidade.\n"
            "Justificativa: eu usei um filtro azul porque ficou mais bonito.\n\n"
            "## Justificativa do Agente\n"
            "Eu tentei manter a cor mas alterei o script para 512x512.\n\n"
            "```python\n"
            "render(color='blue', size=512)\n"
            "```\n\n"
            "## Referencias\n"
            f"- {ref_img.name}\n\n"
            "## Entrega\n"
            f"- {out_img.name}\n"
            f"- {code_file.name}\n",
            encoding="utf-8",
        )

        round_responses = [
            # Rodada 1: 1 critico + 1 moderado
            json.dumps(
                {
                    "criterios": [
                        {"id": "C1", "requisito": "Peixe dourado identico a REF-1", "peso": "essencial", "atendido": False},
                        {"id": "C2", "requisito": "Imagem pequena (ate 256x256)", "peso": "importante", "atendido": False},
                        {"id": "C3", "requisito": "Imagem limpa e nitida", "peso": "importante", "atendido": True},
                    ],
                    "falhas": [
                        {
                            "id": "F1",
                            "criterio_id": "C1",
                            "severidade": "critico",
                            "observado": "Peixe azulado, diferente da cor e silhueta de REF-1",
                            "esperado": "Peixe dourado identico a REF-1",
                            "acao": "Corrigir cor e formato do peixe para coincidir exatamente com REF-1",
                            "evidencia": "OUT-1 vs REF-1",
                        },
                        {
                            "id": "F2",
                            "criterio_id": "C2",
                            "severidade": "moderado",
                            "observado": "OUT-1 tem 512x512px",
                            "esperado": "Resolucao pequena ate 256x256px",
                            "acao": "Redimensionar OUT-1 para no maximo 256x256px",
                            "evidencia": "OUT-1",
                        },
                    ],
                },
                ensure_ascii=False,
            ),
            # Rodada 2: critico resolvido, sobra 1 moderado
            json.dumps(
                {
                    "criterios": [
                        {"id": "C1", "requisito": "Peixe dourado identico a REF-1", "peso": "essencial", "atendido": True},
                        {"id": "C2", "requisito": "Imagem pequena (ate 256x256)", "peso": "importante", "atendido": True},
                        {"id": "C3", "requisito": "Imagem limpa e nitida", "peso": "importante", "atendido": False},
                    ],
                    "falhas": [
                        {
                            "id": "F1",
                            "criterio_id": "C3",
                            "severidade": "moderado",
                            "observado": "Residuo cinza nas bordas do fundo",
                            "esperado": "Fundo limpo sem artefatos",
                            "acao": "Remover residuos das bordas deixando o fundo limpo",
                            "evidencia": "OUT-1",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            # Rodada 3: apenas 1 detalhe aceitavel -> APROVADO
            json.dumps(
                {
                    "criterios": [
                        {"id": "C1", "requisito": "Peixe dourado identico a REF-1", "peso": "essencial", "atendido": True},
                        {"id": "C2", "requisito": "Imagem pequena (ate 256x256)", "peso": "importante", "atendido": True},
                        {"id": "C3", "requisito": "Imagem limpa e nitida", "peso": "importante", "atendido": True},
                    ],
                    "falhas": [
                        {
                            "id": "F1",
                            "criterio_id": "C1",
                            "severidade": "aceitavel",
                            "observado": "Brilho 2% mais claro na nadadeira",
                            "esperado": "Tom dourado fiel a REF-1",
                            "acao": "Nenhuma acao obrigatoria",
                            "evidencia": "OUT-1 vs REF-1",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
        ]

        call_idx = {"n": 0}

        def fake_verifier(
            packet: VerificationPacket, _sys: str, _repair: str | None
        ) -> str:
            idx = call_idx["n"]
            call_idx["n"] += 1
            if idx == 0:
                print("=== PACOTE CRU ENVIADO AO VERIFICADOR (RODADA 1) ===")
                print(render_raw_packet_text(packet))
            return round_responses[idx]

        def fake_creator(
            correction, prev_sub: CreatorSubmission, _rep: VerificationReport
        ) -> CreatorSubmission:
            print(f"=== PROMPT ENVIADO AO CRIADOR (APOS RODADA {correction.iteration}) ===")
            print(correction.prompt_text)
            # Simula entrega corrigida (128x128 dourado)
            out_img.write_bytes(_make_minimal_png(128, 128, (235, 180, 30)))
            return prev_sub

        result = run_verification_loop(
            CreatorSubmission(markdown_path=req_md),
            CallableVerifier(fake_verifier),
            CallableCreator(fake_creator),
            config=LoopConfig(max_iterations=5),
        )

        print("=== RESUMO FINAL DA DEMO ===")
        first_audit = result.iterations[0].sanitization
        print(f"Secoes removidas do MD: {list(first_audit.removed_sections)}")
        print(f"Blocos de codigo removidos: {first_audit.removed_code_blocks}")
        print(f"Linhas de justificativa removidas: {first_audit.removed_commentary_lines}")
        print(f"Arquivos bloqueados: {[r.to_dict() for r in first_audit.rejected_files]}")
        for rec in result.iterations:
            assert rec.report is not None
            print(f"Rodada {rec.iteration}: {rec.report.status.value.upper()} -> {rec.report.counts}")
        print(f"Status final: {'APROVADO' if result.approved else 'REPROVADO'} ({result.stop_reason.value})")
        return 0 if result.approved else 1


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
        if args.command == "demo":
            return _cmd_demo()
        if args.command == "ui":
            from .web import run_ui

            return run_ui(host=args.host, port=args.port, open_browser=not args.no_browser)
    except VerifyBridgeError as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

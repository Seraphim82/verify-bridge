"""Testes dos adapters (HTTP/CLI) e do orquestrador do loop de verificacao."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from verify_bridge.adapters import (
    AnthropicVerifier,
    CLICreator,
    CLIVerifier,
    CallableCreator,
    CallableVerifier,
    GeminiVerifier,
    OpenAICompatibleVerifier,
)
from verify_bridge.cli import main as cli_main
from verify_bridge.models import (
    CreatorSubmission,
    LoopStopReason,
    VerificationPacket,
)
from verify_bridge.orchestrator import LoopConfig, run_verification_loop, verify_once
from verify_bridge.rubric import build_verifier_system_prompt
from verify_bridge.sanitizer import collect_and_sanitize

from .test_sanitizer import make_png


class TestAdapters(unittest.TestCase):
    def test_multimodal_payload_builders_include_base64_images_and_labels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ref = root / "ref.png"
            out = root / "out.png"
            ref.write_bytes(make_png(16, 16))
            out.write_bytes(make_png(16, 16))
            packet, _ = collect_and_sanitize(
                CreatorSubmission(
                    markdown_text="Peixe dourado identico a referencia.",
                    reference_files=(ref,),
                    deliverable_files=(out,),
                    base_dir=root,
                )
            )
            sys_prompt = build_verifier_system_prompt()

            oai = OpenAICompatibleVerifier(model="gpt-4o").build_payload(packet, sys_prompt)
            self.assertEqual(oai["response_format"], {"type": "json_object"})
            user_parts = oai["messages"][1]["content"]
            img_parts = [p for p in user_parts if p["type"] == "image_url"]
            self.assertEqual(len(img_parts), 2)
            self.assertTrue(img_parts[0]["image_url"]["url"].startswith("data:image/png;base64,"))

            ant = AnthropicVerifier(model="claude-sonnet-4-20250514").build_payload(
                packet, sys_prompt, repair_instruction="Somente JSON."
            )
            ant_imgs = [b for b in ant["messages"][0]["content"] if b["type"] == "image"]
            self.assertEqual(len(ant_imgs), 2)
            self.assertEqual(ant["messages"][0]["content"][-1]["text"], "Somente JSON.")

            gem = GeminiVerifier(model="gemini-2.5-flash").build_payload(packet, sys_prompt)
            gem_parts = gem["contents"][0]["parts"]
            inline = [p for p in gem_parts if "inlineData" in p]
            self.assertEqual(len(inline), 2)
            self.assertEqual(gem["generationConfig"]["responseMimeType"], "application/json")

    def test_cli_verifier_isolates_workdir_and_neutralizes_filenames(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ref = root / "original_do_usuario.png"
            out = root / "versao_final_aprovada_por_mim.png"
            secret_code = root / "segredo.py"
            ref.write_bytes(make_png(10, 10))
            out.write_bytes(make_png(10, 10))
            secret_code.write_text("SECRET = 123\n", encoding="utf-8")

            packet, _ = collect_and_sanitize(
                CreatorSubmission(
                    markdown_text="Verificar imagem.",
                    reference_files=(ref,),
                    deliverable_files=(out, secret_code),
                    base_dir=root,
                )
            )

            # Script verificador inspeciona arquivos visiveis no diretorio isolado
            probe_script = root / "probe.py"
            probe_script.write_text(
                "import json, os, sys\n"
                "files = sorted(os.listdir(sys.argv[1]))\n"
                "assert 'REF-1.png' in files and 'OUT-1.png' in files, files\n"
                "assert 'segredo.py' not in files, files\n"
                "assert 'versao_final_aprovada_por_mim.png' not in files, files\n"
                "print(json.dumps({'criterios': [], 'falhas': []}))\n",
                encoding="utf-8",
            )

            verifier = CLIVerifier(
                command=(sys.executable, str(probe_script), "{workdir}")
            )
            raw = verifier.verify(packet, build_verifier_system_prompt())
            self.assertEqual(json.loads(raw)["falhas"], [])

    def test_cli_creator_receives_correction_and_report_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / "creator_saw.txt"
            fixer = root / "fixer.py"
            fixer.write_text(
                "import pathlib, sys\n"
                "corr = pathlib.Path(sys.argv[1]).read_text(encoding='utf-8')\n"
                "rep = pathlib.Path(sys.argv[2]).read_text(encoding='utf-8')\n"
                f"pathlib.Path({str(marker)!r}).write_text(f'{{sys.argv[3]}}|{{len(corr)>0}}|{{len(rep)>0}}')\n",
                encoding="utf-8",
            )
            sub = CreatorSubmission(markdown_text="Peixe dourado.", base_dir=root)
            verifier = CallableVerifier(
                lambda _p, _s, _r: json.dumps(
                    {
                        "criterios": [],
                        "falhas": [
                            {
                                "id": "F1",
                                "severidade": "critico",
                                "observado": "Errado",
                                "esperado": "Certo",
                                "acao": "Corrigir",
                            }
                        ],
                    }
                )
            )
            creator = CLICreator(
                command=(
                    sys.executable,
                    str(fixer),
                    "{correction_file}",
                    "{report_file}",
                    "{iteration}",
                ),
                workdir=root,
            )
            res = run_verification_loop(
                sub, verifier, creator, config=LoopConfig(max_iterations=2, stagnation_limit=5)
            )
            self.assertEqual(res.stop_reason, LoopStopReason.MAX_ITERATIONS)
            self.assertEqual(marker.read_text(encoding="utf-8"), "1|True|True")


class TestOrchestrator(unittest.TestCase):
    def test_loop_converges_from_critical_to_moderate_to_acceptable_approved(self) -> None:
        responses = iter(
            [
                json.dumps(
                    {
                        "criterios": [
                            {"id": "C1", "requisito": "Cor dourada", "peso": "essencial", "atendido": False}
                        ],
                        "falhas": [
                            {
                                "id": "F1",
                                "criterio_id": "C1",
                                "severidade": "critico",
                                "observado": "Azul",
                                "esperado": "Dourado",
                                "acao": "Mudar para dourado",
                            }
                        ],
                    }
                ),
                json.dumps(
                    {
                        "criterios": [
                            {"id": "C1", "requisito": "Cor dourada", "peso": "essencial", "atendido": True},
                            {"id": "C2", "requisito": "Fundo limpo", "peso": "importante", "atendido": False},
                        ],
                        "falhas": [
                            {
                                "id": "F1",
                                "criterio_id": "C2",
                                "severidade": "moderado",
                                "observado": "Mancha no fundo",
                                "esperado": "Fundo limpo",
                                "acao": "Limpar fundo",
                            }
                        ],
                    }
                ),
                json.dumps(
                    {
                        "criterios": [
                            {"id": "C1", "requisito": "Cor dourada", "peso": "essencial", "atendido": True},
                            {"id": "C2", "requisito": "Fundo limpo", "peso": "importante", "atendido": True},
                        ],
                        "falhas": [
                            {
                                "id": "F1",
                                "criterio_id": "C1",
                                "severidade": "aceitavel",
                                "observado": "Leve reflexo",
                                "esperado": "Dourado uniforme",
                                "acao": "Opcional",
                            }
                        ],
                    }
                ),
            ]
        )

        creator_prompts: list[str] = []

        def verify_fn(_p: VerificationPacket, _s: str, _r: str | None) -> str:
            return next(responses)

        def revise_fn(corr, prev, _rep) -> CreatorSubmission:
            creator_prompts.append(corr.prompt_text)
            return prev

        result = run_verification_loop(
            CreatorSubmission(markdown_text="Peixe dourado com fundo limpo."),
            CallableVerifier(verify_fn),
            CallableCreator(revise_fn),
            config=LoopConfig(max_iterations=5),
        )

        self.assertTrue(result.approved)
        self.assertEqual(result.stop_reason, LoopStopReason.APPROVED)
        self.assertEqual(len(result.iterations), 3)
        self.assertEqual(len(creator_prompts), 2)
        self.assertIn("[CRITICO]", creator_prompts[0])
        self.assertIn("[MODERADO]", creator_prompts[1])

    def test_parse_retry_recovers_from_malformed_response(self) -> None:
        calls: list[str | None] = []

        def flaky_verifier(_p: VerificationPacket, _s: str, repair: str | None) -> str:
            calls.append(repair)
            if repair is None:
                return "Desculpe, esqueci o JSON!"
            return json.dumps({"criterios": [], "falhas": []})

        _pkt, _aud, report, _corr = verify_once(
            CreatorSubmission(markdown_text="Teste simples."),
            CallableVerifier(flaky_verifier),
            max_parse_retries=1,
        )
        self.assertTrue(report.approved)
        self.assertEqual(len(calls), 2)
        self.assertIsNone(calls[0])
        self.assertIsNotNone(calls[1])

    def test_stagnation_stops_infinite_loop(self) -> None:
        same_failure = json.dumps(
            {
                "criterios": [],
                "falhas": [
                    {
                        "id": "F1",
                        "severidade": "moderado",
                        "observado": "Borda serrilhada",
                        "esperado": "Borda suave",
                        "acao": "Suavizar borda",
                    }
                ],
            }
        )
        result = run_verification_loop(
            CreatorSubmission(markdown_text="Imagem com borda suave."),
            CallableVerifier(lambda _p, _s, _r: same_failure),
            CallableCreator(lambda _c, prev, _r: prev),
            config=LoopConfig(max_iterations=5, stagnation_limit=2),
        )
        self.assertFalse(result.approved)
        self.assertEqual(result.stop_reason, LoopStopReason.STAGNATED)
        self.assertEqual(len(result.iterations), 2)

    def test_graceful_handling_of_creator_and_verifier_errors(self) -> None:
        res_v = run_verification_loop(
            CreatorSubmission(markdown_text="Teste."),
            CallableVerifier(lambda _p, _s, _r: (_ for _ in ()).throw(RuntimeError("timeout"))),
            CallableCreator(lambda _c, prev, _r: prev),
        )
        self.assertEqual(res_v.stop_reason, LoopStopReason.VERIFIER_ERROR)
        self.assertIn("timeout", res_v.error or "")

        res_c = run_verification_loop(
            CreatorSubmission(markdown_text="Teste."),
            CallableVerifier(
                lambda _p, _s, _r: json.dumps(
                    {
                        "criterios": [],
                        "falhas": [
                            {
                                "id": "F1",
                                "severidade": "critico",
                                "observado": "Falhou",
                                "esperado": "Ok",
                                "acao": "Corrigir",
                            }
                        ],
                    }
                )
            ),
            CallableCreator(lambda _c, _p, _r: (_ for _ in ()).throw(RuntimeError("criador caiu"))),
        )
        self.assertEqual(res_c.stop_reason, LoopStopReason.CREATOR_ERROR)
        self.assertIn("criador caiu", res_c.error or "")

    def test_cli_compile_command_and_demo_removed(self) -> None:
        example_md = Path(__file__).resolve().parent.parent / "examples" / "pedido_exemplo.md"
        self.assertEqual(cli_main(["compile", str(example_md), "--audit"]), 0)
        # O comando "demo" (simulador do peixe) foi removido na 0.3.
        with self.assertRaises(SystemExit):
            cli_main(["demo"])


if __name__ == "__main__":
    unittest.main()

"""Testes do avaliador deterministico de severidade e do gerador de feedback."""

from __future__ import annotations

import json
import unittest

from verify_bridge.evaluator import evaluate_verifier_response
from verify_bridge.feedback import build_correction_prompt
from verify_bridge.models import (
    ResponseParseError,
    Severity,
    VerificationPacket,
    VerificationStatus,
)


class TestEvaluatorAndFeedback(unittest.TestCase):
    def test_approves_when_zero_findings(self) -> None:
        raw = json.dumps(
            {
                "criterios": [
                    {"id": "C1", "requisito": "Peixe dourado", "peso": "essencial", "atendido": True}
                ],
                "falhas": [],
            }
        )
        rep = evaluate_verifier_response(raw)
        self.assertTrue(rep.approved)
        self.assertEqual(rep.status, VerificationStatus.APROVADO)
        self.assertEqual(rep.counts, {"critico": 0, "moderado": 0, "aceitavel": 0})

    def test_approves_when_only_acceptable_findings(self) -> None:
        raw = (
            "Aqui esta a analise:\n```json\n"
            + json.dumps(
                {
                    "criterios": [
                        {"id": "C1", "requisito": "Peixe dourado", "peso": "essencial", "atendido": True}
                    ],
                    "falhas": [
                        {
                            "id": "F1",
                            "criterio_id": "C1",
                            "severidade": "aceitável",
                            "observado": "Leve variacao de brilho",
                            "esperado": "Tom dourado igual",
                            "acao": "Opcional",
                        }
                    ],
                }
            )
            + "\n```\nFim."
        )
        rep = evaluate_verifier_response(raw)
        self.assertTrue(rep.approved)
        self.assertEqual(rep.counts["aceitavel"], 1)
        self.assertEqual(len(rep.blocking_findings), 0)

    def test_promotes_finding_when_essential_criterion_failed(self) -> None:
        raw = json.dumps(
            {
                "aprovado": True,
                "criterios": [
                    {
                        "id": "C1",
                        "requisito": "Peixe dourado identico a REF-1",
                        "peso": "essencial",
                        "atendido": False,
                    }
                ],
                "falhas": [
                    {
                        "id": "F1",
                        "criterio_id": "C1",
                        "severidade": "minor",
                        "observado": "Peixe verde",
                        "esperado": "Peixe dourado",
                        "acao": "Trocar cor para dourado",
                    }
                ],
            }
        )
        rep = evaluate_verifier_response(raw)
        self.assertFalse(rep.approved)
        self.assertEqual(rep.findings[0].severity, Severity.CRITICO)
        self.assertEqual(rep.findings[0].promoted_from, Severity.ACEITAVEL)
        self.assertTrue(any("promovida" in n for n in rep.evaluator_notes))
        self.assertTrue(any("forcado para reprovado" in n for n in rep.evaluator_notes))

    def test_synthesizes_findings_for_failed_criteria_without_finding(self) -> None:
        raw = json.dumps(
            {
                "criterios": [
                    {"id": "C1", "requisito": "Mesma especie", "peso": "essencial", "atendido": False},
                    {"id": "C2", "requisito": "Fundo limpo", "peso": "importante", "atendido": False},
                ],
                "falhas": [],
            }
        )
        rep = evaluate_verifier_response(raw)
        self.assertFalse(rep.approved)
        self.assertEqual(rep.counts, {"critico": 1, "moderado": 1, "aceitavel": 0})

    def test_unknown_severity_defaults_to_moderado_and_deduplicates(self) -> None:
        raw = json.dumps(
            {
                "criterios": [],
                "falhas": [
                    {
                        "id": "F1",
                        "severidade": "estranho",
                        "observado": "Borda borrada",
                        "esperado": "Borda nitida",
                        "acao": "Melhorar nitidez da borda",
                    },
                    {
                        "id": "F2",
                        "severidade": "estranho",
                        "observado": "Borda  borrada",
                        "esperado": "Borda nitida",
                        "acao": "Melhorar nitidez da borda",
                    },
                ],
            }
        )
        rep = evaluate_verifier_response(raw)
        self.assertFalse(rep.approved)
        self.assertEqual(len(rep.findings), 1)
        self.assertEqual(rep.findings[0].severity, Severity.MODERADO)

    def test_invalid_json_raises_parse_error(self) -> None:
        for bad in ("", "   ", "nao e json", '{"outra_chave": 1}'):
            with self.assertRaises(ResponseParseError):
                evaluate_verifier_response(bad)

    def test_feedback_prompt_orders_critical_first_and_omits_acceptable_by_default(self) -> None:
        packet = VerificationPacket(
            original_prompt="Peixe dourado pequeno e limpo.",
            acceptance_criteria=("Ate 256x256",),
        )
        raw = json.dumps(
            {
                "criterios": [],
                "falhas": [
                    {
                        "id": "F1",
                        "severidade": "aceitavel",
                        "observado": "Brilho minimo",
                        "esperado": "Igual",
                        "acao": "Nada",
                    },
                    {
                        "id": "F2",
                        "severidade": "moderado",
                        "observado": "512x512",
                        "esperado": "256x256",
                        "acao": "Redimensionar",
                    },
                    {
                        "id": "F3",
                        "severidade": "critico",
                        "observado": "Peixe azul",
                        "esperado": "Peixe dourado",
                        "acao": "Corrigir cor",
                        "evidencia": "OUT-1 vs REF-1",
                    },
                ],
            }
        )
        rep = evaluate_verifier_response(raw)

        corr = build_correction_prompt(packet, rep, iteration=2)
        self.assertEqual(corr.blocking_count, 2)
        self.assertEqual(corr.acceptable_count, 1)
        self.assertLess(
            corr.prompt_text.index("[CRITICO]"), corr.prompt_text.index("[MODERADO]")
        )
        self.assertNotIn("ACEITAVEL", corr.prompt_text)

        corr_with_acc = build_correction_prompt(
            packet, rep, iteration=2, include_acceptable=True
        )
        self.assertIn("ACEITAVEL", corr_with_acc.prompt_text)


if __name__ == "__main__":
    unittest.main()

"""Gerador de prompt de correcao enxuto e direto para o agente criador."""

from __future__ import annotations

from .models import CorrectionPrompt, VerificationPacket, VerificationReport


def build_correction_prompt(
    packet: VerificationPacket,
    report: VerificationReport,
    *,
    iteration: int = 1,
    include_acceptable: bool = False,
) -> CorrectionPrompt:
    """Constroi o prompt objetivo de correcoes com base estrita no pedido inicial.

    Lista apenas falhas bloqueantes (critico -> moderado) para economizar tokens
    e evitar alteracoes desnecessarias ou requisitos inventados.
    """
    blocking = report.blocking_findings
    acceptable = report.acceptable_findings

    lines: list[str] = [
        "PEDIDO INICIAL:",
        packet.original_prompt,
    ]

    if packet.acceptance_criteria:
        lines.append("")
        lines.append("CRITERIOS:")
        for item in packet.acceptance_criteria:
            lines.append(f"- {item}")

    if blocking:
        lines.append("")
        lines.append(f"CORRECOES OBRIGATORIAS (rodada {iteration}):")
        for idx, finding in enumerate(blocking, start=1):
            sev_tag = finding.severity.value.upper()
            ev_part = f" [{finding.evidence}]" if finding.evidence else ""
            lines.append(
                f"{idx}. [{sev_tag}]{ev_part} {finding.action} "
                f"| Observado: {finding.observed} "
                f"| Esperado: {finding.expected}"
            )

        if include_acceptable and acceptable:
            lines.append("")
            lines.append("OBSERVACOES ACEITAVEIS (opcionais, nao bloqueiam):")
            for finding in acceptable:
                lines.append(f"- [ACEITAVEL] {finding.observed} -> {finding.action}")

        lines.append("")
        lines.append("REGRAS:")
        lines.append("- Corrija apenas os itens obrigatorios listados acima.")
        lines.append("- Preserve o que ja atende ao pedido inicial e nao invente elementos novos.")
    else:
        lines.append("")
        lines.append(f"VERIFICACAO APROVADA (rodada {iteration}): nenhuma correcao obrigatoria.")
        if include_acceptable and acceptable:
            lines.append("Observacoes aceitaveis registradas:")
            for finding in acceptable:
                lines.append(f"- {finding.observed}")

    return CorrectionPrompt(
        iteration=iteration,
        prompt_text="\n".join(lines).strip() + "\n",
        blocking_count=len(blocking),
        acceptable_count=len(acceptable),
    )

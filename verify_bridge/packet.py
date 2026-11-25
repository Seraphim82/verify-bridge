"""Compilador do pacote cru enviado ao agente verificador.

Gera uma representacao enxuta, sem vies e sem desperdicio de tokens.
"""

from __future__ import annotations

import base64
from pathlib import Path

from .models import Artifact, ArtifactKind, VerificationPacket


def render_raw_packet_text(packet: VerificationPacket) -> str:
    """Renderiza o pacote cru em texto minimo para envio ao agente verificador."""
    parts: list[str] = [
        "PEDIDO_INICIAL:",
        packet.original_prompt,
    ]

    if packet.acceptance_criteria:
        parts.append("")
        parts.append("CRITERIOS_EXPLICITOS:")
        for item in packet.acceptance_criteria:
            parts.append(f"- {item}")

    if packet.artifacts:
        parts.append("")
        parts.append("ARTEFATOS:")
        for art in packet.artifacts:
            parts.append(f"- {art.metadata_line}")

        text_artifacts = [
            art for art in packet.artifacts if art.kind is ArtifactKind.TEXT and art.text_excerpt
        ]
        for art in text_artifacts:
            parts.append("")
            parts.append(f"CONTEUDO {art.label}:")
            parts.append(art.text_excerpt or "")

    return "\n".join(parts).strip() + "\n"


def encode_artifact_base64(artifact: Artifact) -> str:
    """Codifica um artefato binario em Base64 sob demanda."""
    return base64.b64encode(artifact.path.read_bytes()).decode("ascii")


def write_raw_packet_file(packet: VerificationPacket, output_path: Path) -> Path:
    """Salva o pacote cru em disco para inspecao ou integracao com agentes CLI."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_raw_packet_text(packet), encoding="utf-8")
    return output_path

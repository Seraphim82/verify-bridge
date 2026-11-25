"""Modelos de dados tipados e excecoes do verify-bridge."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class VerifyBridgeError(Exception):
    """Excecao base para erros controlados do verify-bridge."""


class EmptyPromptError(VerifyBridgeError):
    """Levantada quando nenhum pedido inicial valido resta apos a sanitizacao."""


class ResponseParseError(VerifyBridgeError):
    """Levantada quando a resposta do agente verificador nao pode ser interpretada."""


class AdapterError(VerifyBridgeError):
    """Levantada quando uma chamada HTTP ou CLI a um agente falha."""


class Severity(str, Enum):
    """Nivel de severidade de uma falha apontada pelo agente verificador."""

    CRITICO = "critico"
    MODERADO = "moderado"
    ACEITAVEL = "aceitavel"

    @property
    def is_blocking(self) -> bool:
        """Retorna True se a severidade impede a aprovacao no loop."""
        return self in (Severity.CRITICO, Severity.MODERADO)

    @property
    def priority(self) -> int:
        """Ordem de prioridade para exibicao (menor valor = maior urgencia)."""
        if self is Severity.CRITICO:
            return 0
        if self is Severity.MODERADO:
            return 1
        return 2


class CriterionWeight(str, Enum):
    """Peso de um criterio extraido estritamente do pedido inicial."""

    ESSENCIAL = "essencial"
    IMPORTANTE = "importante"
    DETALHE = "detalhe"


class ArtifactRole(str, Enum):
    """Papel de um arquivo enviado para verificacao."""

    REFERENCE = "referencia"
    DELIVERABLE = "entrega"


class ArtifactKind(str, Enum):
    """Categoria de midia verificavel."""

    IMAGE = "imagem"
    TEXT = "texto"
    DOCUMENT = "documento"


class VerificationStatus(str, Enum):
    """Resultado consolidado de uma rodada de verificacao."""

    APROVADO = "aprovado"
    REPROVADO = "reprovado"


class LoopStopReason(str, Enum):
    """Motivo de encerramento do loop criador <-> verificador."""

    APPROVED = "aprovado"
    MAX_ITERATIONS = "limite_iteracoes"
    STAGNATED = "estagnado"
    CANCELLED = "cancelado"
    VERIFIER_ERROR = "erro_verificador"
    CREATOR_ERROR = "erro_criador"
    INPUT_ERROR = "erro_entrada"


@dataclass(frozen=True)
class Artifact:
    """Artefato verificavel validado (nunca codigo-fonte)."""

    label: str
    role: ArtifactRole
    kind: ArtifactKind
    path: Path
    mime_type: str
    size_bytes: int
    width: int | None = None
    height: int | None = None
    text_excerpt: str | None = None

    @property
    def metadata_line(self) -> str:
        """Resumo factual curto do arquivo, sem opinioes nem caminhos internos."""
        parts = [f"{self.label} ({self.role.value}, {self.mime_type}, {_format_bytes(self.size_bytes)})"]
        if self.width is not None and self.height is not None:
            parts.append(f"{self.width}x{self.height}px")
        return " | ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "role": self.role.value,
            "kind": self.kind.value,
            "path": str(self.path),
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "width": self.width,
            "height": self.height,
        }


@dataclass(frozen=True)
class RejectedFile:
    """Registro de arquivo bloqueado ou invalido durante a coleta."""

    path: str
    role: ArtifactRole
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "role": self.role.value, "reason": self.reason}


@dataclass(frozen=True)
class SanitizationReport:
    """Auditoria local do que foi removido antes do envio ao verificador.

    Nunca e enviado ao agente verificador.
    """

    removed_sections: tuple[str, ...] = ()
    removed_code_blocks: int = 0
    removed_commentary_lines: int = 0
    rejected_files: tuple[RejectedFile, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "removed_sections": list(self.removed_sections),
            "removed_code_blocks": self.removed_code_blocks,
            "removed_commentary_lines": self.removed_commentary_lines,
            "rejected_files": [rf.to_dict() for rf in self.rejected_files],
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class CreatorSubmission:
    """Entrada entregue pelo agente criador para uma rodada de verificacao."""

    markdown_path: Path | None = None
    markdown_text: str | None = None
    reference_files: tuple[Path, ...] = ()
    deliverable_files: tuple[Path, ...] = ()
    base_dir: Path | None = None


@dataclass(frozen=True)
class VerificationPacket:
    """Pacote cru e imparcial enviado ao agente verificador.

    Contem apenas o pedido inicial, criterios objetivos do usuario (se houver)
    e os artefatos verificaveis. Sem codigo, sem justificativas do criador.
    """

    original_prompt: str
    acceptance_criteria: tuple[str, ...] = ()
    artifacts: tuple[Artifact, ...] = ()

    @property
    def references(self) -> tuple[Artifact, ...]:
        return tuple(a for a in self.artifacts if a.role is ArtifactRole.REFERENCE)

    @property
    def deliverables(self) -> tuple[Artifact, ...]:
        return tuple(a for a in self.artifacts if a.role is ArtifactRole.DELIVERABLE)

    def to_dict(self) -> dict[str, Any]:
        return {
            "original_prompt": self.original_prompt,
            "acceptance_criteria": list(self.acceptance_criteria),
            "artifacts": [a.to_dict() for a in self.artifacts],
        }


@dataclass(frozen=True)
class CriterionResult:
    """Resultado de avaliacao de um criterio extraido do pedido inicial."""

    id: str
    requirement: str
    weight: CriterionWeight
    passed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "requisito": self.requirement,
            "peso": self.weight.value,
            "atendido": self.passed,
        }


@dataclass(frozen=True)
class Finding:
    """Falha objetiva encontrada pelo agente verificador."""

    id: str
    severity: Severity
    observed: str
    expected: str
    action: str
    criterion_id: str | None = None
    evidence: str | None = None
    promoted_from: Severity | None = None

    @property
    def is_blocking(self) -> bool:
        return self.severity.is_blocking

    def fingerprint(self) -> str:
        """Assinatura normalizada para detectar estagnacao no loop."""
        parts = [
            self.severity.value,
            (self.criterion_id or "").strip().lower(),
            " ".join(self.observed.lower().split()),
            " ".join(self.expected.lower().split()),
        ]
        return "|".join(parts)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "severidade": self.severity.value,
            "observado": self.observed,
            "esperado": self.expected,
            "acao": self.action,
        }
        if self.criterion_id:
            data["criterio_id"] = self.criterion_id
        if self.evidence:
            data["evidencia"] = self.evidence
        if self.promoted_from:
            data["promovido_de"] = self.promoted_from.value
        return data


@dataclass(frozen=True)
class VerificationReport:
    """Relatorio estruturado e validado de uma verificacao."""

    status: VerificationStatus
    criteria: tuple[CriterionResult, ...]
    findings: tuple[Finding, ...]
    raw_response: str = ""
    evaluator_notes: tuple[str, ...] = ()

    @property
    def approved(self) -> bool:
        return self.status is VerificationStatus.APROVADO

    @property
    def blocking_findings(self) -> tuple[Finding, ...]:
        return tuple(
            sorted(
                (f for f in self.findings if f.is_blocking),
                key=lambda f: (f.severity.priority, f.id),
            )
        )

    @property
    def acceptable_findings(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity is Severity.ACEITAVEL)

    @property
    def counts(self) -> dict[str, int]:
        return {
            Severity.CRITICO.value: sum(1 for f in self.findings if f.severity is Severity.CRITICO),
            Severity.MODERADO.value: sum(1 for f in self.findings if f.severity is Severity.MODERADO),
            Severity.ACEITAVEL.value: sum(1 for f in self.findings if f.severity is Severity.ACEITAVEL),
        }

    def blocking_signature(self) -> tuple[str, ...]:
        return tuple(sorted(f.fingerprint() for f in self.blocking_findings))

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "aprovado": self.approved,
            "contagem": self.counts,
            "criterios": [c.to_dict() for c in self.criteria],
            "falhas": [f.to_dict() for f in self.findings],
            "notas_avaliador": list(self.evaluator_notes),
        }


@dataclass(frozen=True)
class CorrectionPrompt:
    """Prompt direto gerado para o agente criador corrigir as falhas."""

    iteration: int
    prompt_text: str
    blocking_count: int
    acceptable_count: int


@dataclass(frozen=True)
class IterationRecord:
    """Registro completo de uma iteracao do loop."""

    iteration: int
    packet: VerificationPacket
    sanitization: SanitizationReport
    report: VerificationReport | None = None
    correction_prompt: CorrectionPrompt | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "iteracao": self.iteration,
            "pacote": self.packet.to_dict(),
            "sanitizacao": self.sanitization.to_dict(),
            "relatorio": self.report.to_dict() if self.report else None,
            "prompt_correcao": self.correction_prompt.prompt_text if self.correction_prompt else None,
            "erro": self.error,
        }


@dataclass(frozen=True)
class LoopResult:
    """Resultado final da execucao do loop de verificacao."""

    approved: bool
    stop_reason: LoopStopReason
    iterations: tuple[IterationRecord, ...] = field(default_factory=tuple)
    final_report: VerificationReport | None = None
    final_correction_prompt: CorrectionPrompt | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "aprovado": self.approved,
            "motivo_parada": self.stop_reason.value,
            "total_iteracoes": len(self.iterations),
            "relatorio_final": self.final_report.to_dict() if self.final_report else None,
            "ultimo_prompt_correcao": (
                self.final_correction_prompt.prompt_text if self.final_correction_prompt else None
            ),
            "erro": self.error,
            "iteracoes": [it.to_dict() for it in self.iterations],
        }


def _format_bytes(size: int) -> str:
    if size < 1024:
        return f"{size}B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f}KB"
    return f"{size / (1024 * 1024):.2f}MB"

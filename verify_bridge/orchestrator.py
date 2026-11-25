"""Orquestrador da ponte e do loop de verificacao entre criador e verificador."""

from __future__ import annotations

from dataclasses import dataclass, field
import threading
from typing import Callable, Protocol

from .evaluator import evaluate_verifier_response
from .feedback import build_correction_prompt
from .models import (
    CorrectionPrompt,
    CreatorSubmission,
    IterationRecord,
    LoopResult,
    LoopStopReason,
    ResponseParseError,
    SanitizationReport,
    VerificationPacket,
    VerificationReport,
    VerifyBridgeError,
)
from .rubric import build_json_repair_prompt, build_verifier_system_prompt
from .sanitizer import SanitizerConfig, collect_and_sanitize


class VerifierAgent(Protocol):
    """Contrato minimo para qualquer agente verificador."""

    def verify(
        self,
        packet: VerificationPacket,
        system_prompt: str,
        *,
        repair_instruction: str | None = None,
    ) -> str:
        """Executa verificacao cega sobre o pacote cru e retorna texto bruto."""
        ...


class CreatorAgent(Protocol):
    """Contrato minimo para qualquer agente criador/executor."""

    def revise(
        self,
        correction: CorrectionPrompt,
        previous_submission: CreatorSubmission,
        report: VerificationReport,
    ) -> CreatorSubmission:
        """Aplica as correcoes apontadas e retorna nova submissao."""
        ...


@dataclass(frozen=True)
class LoopConfig:
    """Configuracoes de controle do loop de verificacao."""

    max_iterations: int = 5
    stagnation_limit: int = 2
    max_parse_retries: int = 1
    include_acceptable_in_prompt: bool = False
    sanitizer: SanitizerConfig = field(default_factory=SanitizerConfig)
    on_iteration: Callable[[IterationRecord], None] | None = None
    on_status: Callable[[str, int, str], None] | None = None
    cancel_event: threading.Event | None = None

    def __post_init__(self) -> None:
        if self.max_iterations < 1:
            raise ValueError("max_iterations deve ser >= 1")
        if self.stagnation_limit < 1:
            raise ValueError("stagnation_limit deve ser >= 1")
        if self.max_parse_retries < 0:
            raise ValueError("max_parse_retries deve ser >= 0")


def verify_once(
    submission: CreatorSubmission,
    verifier: VerifierAgent,
    *,
    sanitizer_config: SanitizerConfig | None = None,
    max_parse_retries: int = 1,
    iteration: int = 1,
    include_acceptable_in_prompt: bool = False,
) -> tuple[VerificationPacket, SanitizationReport, VerificationReport, CorrectionPrompt]:
    """Executa uma rodada completa: coleta -> sanitiza -> verifica -> gera feedback."""
    packet, sanitization = collect_and_sanitize(submission, config=sanitizer_config)
    report = _invoke_verifier_with_retry(
        verifier=verifier,
        packet=packet,
        max_parse_retries=max_parse_retries,
    )
    correction = build_correction_prompt(
        packet=packet,
        report=report,
        iteration=iteration,
        include_acceptable=include_acceptable_in_prompt,
    )
    return packet, sanitization, report, correction


def run_verification_loop(
    initial_submission: CreatorSubmission,
    verifier: VerifierAgent,
    creator: CreatorAgent,
    config: LoopConfig | None = None,
) -> LoopResult:
    """Executa o loop criador <-> conector <-> verificador ate aprovacao ou parada segura.

    Condicoes de parada:
    - APROVADO: zero falhas criticas e zero falhas moderadas (somente sem falhas ou aceitaveis).
    - STAGNATED: mesmas falhas bloqueantes repetidas consecutivamente.
    - MAX_ITERATIONS: limite de rodadas atingido.
    - INPUT_ERROR / VERIFIER_ERROR / CREATOR_ERROR: falha capturada sem quebrar o processo.
    """
    cfg = config or LoopConfig()
    submission = initial_submission
    iterations: list[IterationRecord] = []
    last_signature: tuple[str, ...] | None = None
    same_signature_streak = 0
    last_report: VerificationReport | None = None
    last_correction: CorrectionPrompt | None = None

    for step in range(1, cfg.max_iterations + 1):
        if cfg.cancel_event is not None and cfg.cancel_event.is_set():
            return LoopResult(
                approved=False,
                stop_reason=LoopStopReason.CANCELLED,
                iterations=tuple(iterations),
                final_report=last_report,
                final_correction_prompt=last_correction,
            )

        _emit_status(cfg, "sanitizing", step, f"Rodada {step}: sanitizando entrada...")
        try:
            packet, sanitization = collect_and_sanitize(submission, config=cfg.sanitizer)
        except VerifyBridgeError as exc:
            return LoopResult(
                approved=False,
                stop_reason=LoopStopReason.INPUT_ERROR,
                iterations=tuple(iterations),
                final_report=last_report,
                final_correction_prompt=last_correction,
                error=str(exc),
            )

        _emit_status(cfg, "verifying", step, f"Rodada {step}: IA verificadora analisando...")
        try:
            report = _invoke_verifier_with_retry(
                verifier=verifier,
                packet=packet,
                max_parse_retries=cfg.max_parse_retries,
            )
        except Exception as exc:  # noqa: BLE001
            if _is_cancelled(cfg):
                return LoopResult(
                    approved=False,
                    stop_reason=LoopStopReason.CANCELLED,
                    iterations=tuple(iterations),
                    final_report=last_report,
                    final_correction_prompt=last_correction,
                )
            rec = IterationRecord(
                iteration=step,
                packet=packet,
                sanitization=sanitization,
                error=f"Erro no verificador: {exc}",
            )
            iterations.append(rec)
            _notify(cfg, rec)
            return LoopResult(
                approved=False,
                stop_reason=LoopStopReason.VERIFIER_ERROR,
                iterations=tuple(iterations),
                final_report=last_report,
                final_correction_prompt=last_correction,
                error=str(exc),
            )

        last_report = report
        correction = build_correction_prompt(
            packet=packet,
            report=report,
            iteration=step,
            include_acceptable=cfg.include_acceptable_in_prompt,
        )
        last_correction = correction

        rec = IterationRecord(
            iteration=step,
            packet=packet,
            sanitization=sanitization,
            report=report,
            correction_prompt=correction,
        )
        iterations.append(rec)
        _notify(cfg, rec)

        if report.approved:
            return LoopResult(
                approved=True,
                stop_reason=LoopStopReason.APPROVED,
                iterations=tuple(iterations),
                final_report=report,
                final_correction_prompt=correction,
            )

        # Detecta estagnacao (mesmas falhas bloqueantes repetidas)
        sig = report.blocking_signature()
        if sig and sig == last_signature:
            same_signature_streak += 1
        else:
            last_signature = sig
            same_signature_streak = 1

        if same_signature_streak >= cfg.stagnation_limit:
            return LoopResult(
                approved=False,
                stop_reason=LoopStopReason.STAGNATED,
                iterations=tuple(iterations),
                final_report=report,
                final_correction_prompt=correction,
                error=(
                    f"Loop estagnado: mesmas falhas bloqueantes repetidas "
                    f"{same_signature_streak} vezes seguidas."
                ),
            )

        if step >= cfg.max_iterations:
            return LoopResult(
                approved=False,
                stop_reason=LoopStopReason.MAX_ITERATIONS,
                iterations=tuple(iterations),
                final_report=report,
                final_correction_prompt=correction,
            )

        if cfg.cancel_event is not None and cfg.cancel_event.is_set():
            return LoopResult(
                approved=False,
                stop_reason=LoopStopReason.CANCELLED,
                iterations=tuple(iterations),
                final_report=report,
                final_correction_prompt=correction,
            )

        _emit_status(cfg, "revising", step, f"Rodada {step}: IA criadora aplicando correcoes...")
        try:
            next_submission = creator.revise(correction, submission, report)
            if not isinstance(next_submission, CreatorSubmission):
                raise TypeError("CreatorAgent.revise deve retornar um CreatorSubmission.")
            submission = next_submission
        except Exception as exc:  # noqa: BLE001
            if _is_cancelled(cfg):
                return LoopResult(
                    approved=False,
                    stop_reason=LoopStopReason.CANCELLED,
                    iterations=tuple(iterations),
                    final_report=report,
                    final_correction_prompt=correction,
                )
            return LoopResult(
                approved=False,
                stop_reason=LoopStopReason.CREATOR_ERROR,
                iterations=tuple(iterations),
                final_report=report,
                final_correction_prompt=correction,
                error=f"Erro no agente criador (rodada {step}): {exc}",
            )

    return LoopResult(
        approved=False,
        stop_reason=LoopStopReason.MAX_ITERATIONS,
        iterations=tuple(iterations),
        final_report=last_report,
        final_correction_prompt=last_correction,
    )


def _invoke_verifier_with_retry(
    verifier: VerifierAgent,
    packet: VerificationPacket,
    max_parse_retries: int,
) -> VerificationReport:
    system_prompt = build_verifier_system_prompt()
    repair: str | None = None
    last_error: ResponseParseError | None = None

    for attempt in range(max_parse_retries + 1):
        raw = verifier.verify(packet, system_prompt, repair_instruction=repair)
        try:
            return evaluate_verifier_response(raw)
        except ResponseParseError as exc:
            last_error = exc
            if attempt < max_parse_retries:
                repair = build_json_repair_prompt()

    assert last_error is not None
    raise last_error


def _is_cancelled(cfg: LoopConfig) -> bool:
    return cfg.cancel_event is not None and cfg.cancel_event.is_set()


def _notify(cfg: LoopConfig, record: IterationRecord) -> None:
    if cfg.on_iteration is not None:
        cfg.on_iteration(record)


def _emit_status(cfg: LoopConfig, phase: str, iteration: int, message: str) -> None:
    if cfg.on_status is not None:
        cfg.on_status(phase, iteration, message)

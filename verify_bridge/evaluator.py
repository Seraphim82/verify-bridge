"""Avaliador deterministico das respostas do agente verificador.

Faz parsing resiliente do JSON retornado pela IA verificadora, normaliza niveis
de severidade e aplica regras deterministicas de coerencia e aprovacao.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .models import (
    CriterionResult,
    CriterionWeight,
    Finding,
    ResponseParseError,
    Severity,
    VerificationReport,
    VerificationStatus,
)
from .sanitizer import normalize_label

_SEVERITY_MAP: dict[str, Severity] = {
    "critico": Severity.CRITICO,
    "critical": Severity.CRITICO,
    "blocker": Severity.CRITICO,
    "bloqueante": Severity.CRITICO,
    "grave": Severity.CRITICO,
    "alto": Severity.CRITICO,
    "high": Severity.CRITICO,
    "severo": Severity.CRITICO,
    "moderado": Severity.MODERADO,
    "moderate": Severity.MODERADO,
    "medio": Severity.MODERADO,
    "medium": Severity.MODERADO,
    "major": Severity.MODERADO,
    "aceitavel": Severity.ACEITAVEL,
    "acceptable": Severity.ACEITAVEL,
    "minor": Severity.ACEITAVEL,
    "leve": Severity.ACEITAVEL,
    "baixo": Severity.ACEITAVEL,
    "low": Severity.ACEITAVEL,
    "trivial": Severity.ACEITAVEL,
    "ok": Severity.ACEITAVEL,
}

_WEIGHT_MAP: dict[str, CriterionWeight] = {
    "essencial": CriterionWeight.ESSENCIAL,
    "essential": CriterionWeight.ESSENCIAL,
    "obrigatorio": CriterionWeight.ESSENCIAL,
    "core": CriterionWeight.ESSENCIAL,
    "critico": CriterionWeight.ESSENCIAL,
    "importante": CriterionWeight.IMPORTANTE,
    "important": CriterionWeight.IMPORTANTE,
    "moderado": CriterionWeight.IMPORTANTE,
    "detalhe": CriterionWeight.DETALHE,
    "detail": CriterionWeight.DETALHE,
    "menor": CriterionWeight.DETALHE,
    "minor": CriterionWeight.DETALHE,
}

_FENCE_JSON_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def evaluate_verifier_response(raw_response: str) -> VerificationReport:
    """Interpreta e valida a resposta do agente verificador.

    Regras deterministicas de aprovacao:
    - Criterio essencial reprovado promove falha vinculada para CRITICO.
    - Criterio essencial/importante reprovado sem falha gera falha sintetizada.
    - Status APROVADO somente quando critico == 0 e moderado == 0.
    """
    if not isinstance(raw_response, str) or not raw_response.strip():
        raise ResponseParseError("Resposta vazia do agente verificador.")

    payload = _extract_json_object(raw_response)
    notes: list[str] = []

    raw_criteria = payload.get("criterios")
    if raw_criteria is None:
        raw_criteria = payload.get("criteria")
    raw_findings = payload.get("falhas")
    if raw_findings is None:
        raw_findings = (
            payload.get("findings")
            if payload.get("findings") is not None
            else payload.get("issues")
        )

    if raw_criteria is None and raw_findings is None:
        raise ResponseParseError(
            "JSON do verificador nao contem as chaves esperadas ('criterios' ou 'falhas')."
        )

    criteria = _parse_criteria(raw_criteria, notes)
    criteria_by_id = {c.id: c for c in criteria}

    findings = _parse_findings(raw_findings, criteria_by_id, notes)
    findings = _enforce_criterion_consistency(criteria, findings, notes)
    findings = _deduplicate_findings(findings)

    has_blocking = any(f.is_blocking for f in findings)
    status = VerificationStatus.REPROVADO if has_blocking else VerificationStatus.APROVADO

    # Verifica se o verificador declarou aprovado=true em contradicao com falhas bloqueantes.
    declared_approved = payload.get("aprovado")
    if declared_approved is None:
        declared_approved = payload.get("approved")
    if declared_approved is True and has_blocking:
        notes.append(
            "Verificador marcou aprovado=true com falhas bloqueantes; status forcado para reprovado."
        )

    return VerificationReport(
        status=status,
        criteria=tuple(criteria),
        findings=tuple(sorted(findings, key=lambda f: (f.severity.priority, f.id))),
        raw_response=raw_response,
        evaluator_notes=tuple(notes),
    )


def _extract_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()

    # 1. Tentativa direta
    parsed = _try_json_dict(stripped)
    if parsed is not None:
        return parsed

    # 2. Tentativa dentro de bloco cercado ```json ... ```
    for match in _FENCE_JSON_RE.finditer(stripped):
        parsed = _try_json_dict(match.group(1).strip())
        if parsed is not None:
            return parsed

    # 3. Varredura balanceada de objetos JSON no texto
    for candidate in _iter_balanced_braces(stripped):
        parsed = _try_json_dict(candidate)
        if parsed is not None:
            return parsed

    raise ResponseParseError("Nao foi possivel extrair um objeto JSON valido da resposta.")


def _try_json_dict(candidate: str) -> dict[str, Any] | None:
    if not candidate.startswith("{"):
        return None
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _iter_balanced_braces(text: str) -> list[str]:
    results: list[str] = []
    start: int | None = None
    depth = 0
    in_string = False
    escaped = False

    for idx, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = idx
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start is not None:
                results.append(text[start : idx + 1])
                start = None

    return results


def _parse_criteria(raw: Any, notes: list[str]) -> list[CriterionResult]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        notes.append("Campo 'criterios' ignorado por nao ser lista.")
        return []

    parsed: list[CriterionResult] = []
    for idx, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            continue
        cid = str(item.get("id") or f"C{idx}").strip()
        req = str(item.get("requisito") or item.get("requirement") or "").strip()
        if not req:
            continue
        weight_raw = str(item.get("peso") or item.get("weight") or "importante")
        weight = _WEIGHT_MAP.get(normalize_label(weight_raw), CriterionWeight.IMPORTANTE)
        passed_val = item.get("atendido")
        if passed_val is None:
            passed_val = item.get("passed", True)
        passed = _to_bool(passed_val, default=True)
        parsed.append(CriterionResult(id=cid, requirement=req, weight=weight, passed=passed))
    return parsed


def _parse_findings(
    raw: Any,
    criteria_by_id: dict[str, CriterionResult],
    notes: list[str],
) -> list[Finding]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        notes.append("Campo 'falhas' ignorado por nao ser lista.")
        return []

    parsed: list[Finding] = []
    for idx, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            continue

        fid = str(item.get("id") or f"F{idx}").strip()
        cid_raw = item.get("criterio_id") or item.get("criterion_id")
        cid = str(cid_raw).strip() if cid_raw else None

        observed = str(item.get("observado") or item.get("observed") or "").strip()
        expected = str(item.get("esperado") or item.get("expected") or "").strip()
        action = str(item.get("acao") or item.get("action") or item.get("correcao") or "").strip()
        evidence_raw = item.get("evidencia") or item.get("evidence")
        evidence = str(evidence_raw).strip() if evidence_raw else None

        if not (observed or expected or action):
            continue

        if not action:
            action = f"Ajustar para atender: {expected or observed}"
        if not observed:
            observed = "Nao conforme com o esperado."
        if not expected:
            expected = (
                criteria_by_id[cid].requirement
                if cid and cid in criteria_by_id
                else "Conformidade com o pedido inicial."
            )

        sev_raw = str(
            item.get("severidade") or item.get("severity") or item.get("nivel") or ""
        ).strip()
        severity = _SEVERITY_MAP.get(normalize_label(sev_raw))
        if severity is None:
            severity = Severity.MODERADO
            notes.append(
                f"Severidade desconhecida '{sev_raw}' em {fid}; tratada como 'moderado'."
            )

        promoted_from: Severity | None = None
        if cid and cid in criteria_by_id:
            crit = criteria_by_id[cid]
            if crit.weight is CriterionWeight.ESSENCIAL and not crit.passed:
                if severity is not Severity.CRITICO:
                    promoted_from = severity
                    severity = Severity.CRITICO
                    notes.append(
                        f"Falha {fid} promovida de {promoted_from.value} para critico "
                        f"(criterio essencial {cid} reprovado)."
                    )

        parsed.append(
            Finding(
                id=fid,
                severity=severity,
                observed=observed,
                expected=expected,
                action=action,
                criterion_id=cid,
                evidence=evidence,
                promoted_from=promoted_from,
            )
        )
    return parsed


def _enforce_criterion_consistency(
    criteria: list[CriterionResult],
    findings: list[Finding],
    notes: list[str],
) -> list[Finding]:
    """Garante que nenhum criterio essencial/importante reprovado passe sem falha."""
    covered_ids = {f.criterion_id for f in findings if f.criterion_id}
    next_idx = len(findings) + 1
    result = list(findings)

    for crit in criteria:
        if crit.passed or crit.id in covered_ids:
            continue
        if crit.weight is CriterionWeight.ESSENCIAL:
            sev = Severity.CRITICO
        elif crit.weight is CriterionWeight.IMPORTANTE:
            sev = Severity.MODERADO
        else:
            continue

        fid = f"F{next_idx}"
        next_idx += 1
        result.append(
            Finding(
                id=fid,
                severity=sev,
                observed=f"Criterio {crit.id} marcado como nao atendido.",
                expected=crit.requirement,
                action=f"Corrigir a entrega para atender ao criterio: {crit.requirement}",
                criterion_id=crit.id,
            )
        )
        notes.append(
            f"Falha {fid} ({sev.value}) sintetizada para criterio reprovado sem achado ({crit.id})."
        )

    return result


def _deduplicate_findings(findings: list[Finding]) -> list[Finding]:
    seen: set[str] = set()
    unique: list[Finding] = []
    for f in findings:
        fp = f.fingerprint()
        if fp not in seen:
            seen.add(fp)
            unique.append(f)
    return unique


def _to_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        norm = normalize_label(value)
        if norm in ("true", "sim", "yes", "1", "ok", "aprovado", "atendido"):
            return True
        if norm in ("false", "nao", "no", "0", "falhou", "reprovado"):
            return False
    return default

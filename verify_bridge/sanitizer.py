"""Coletor e sanitizador de entradas do agente criador.

Remove justificativas, explicacoes, passos de implementacao, blocos de codigo
e arquivos de codigo-fonte para garantir verificacao 100% cega e imparcial.
"""

from __future__ import annotations

import mimetypes
import re
import struct
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .models import (
    Artifact,
    ArtifactKind,
    ArtifactRole,
    CreatorSubmission,
    EmptyPromptError,
    RejectedFile,
    SanitizationReport,
    VerificationPacket,
)

# Extensoes de codigo-fonte e configuracao estritamente bloqueadas para o verificador.
BLOCKED_CODE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".py",
        ".pyi",
        ".pyc",
        ".js",
        ".mjs",
        ".cjs",
        ".jsx",
        ".ts",
        ".tsx",
        ".mts",
        ".cts",
        ".java",
        ".kt",
        ".kts",
        ".go",
        ".rs",
        ".c",
        ".cc",
        ".cpp",
        ".cxx",
        ".h",
        ".hh",
        ".hpp",
        ".cs",
        ".rb",
        ".php",
        ".swift",
        ".scala",
        ".dart",
        ".lua",
        ".r",
        ".pl",
        ".sh",
        ".bash",
        ".zsh",
        ".fish",
        ".ps1",
        ".bat",
        ".cmd",
        ".sql",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".conf",
        ".env",
        ".lock",
        ".dockerfile",
        ".makefile",
        ".cmake",
        ".gradle",
        ".vue",
        ".svelte",
        ".css",
        ".scss",
        ".sass",
        ".less",
        ".map",
        ".ipynb",
        ".patch",
        ".diff",
        ".log",
    }
)

BLOCKED_FILENAMES: frozenset[str] = frozenset(
    {
        "makefile",
        "dockerfile",
        "jenkinsfile",
        "vagrantfile",
        "procfile",
        ".gitignore",
        ".env",
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "poetry.lock",
        "cargo.lock",
    }
)

IMAGE_EXTENSIONS: frozenset[str] = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"})
TEXT_EXTENSIONS: frozenset[str] = frozenset({".txt", ".md", ".csv"})
DOCUMENT_EXTENSIONS: frozenset[str] = frozenset({".pdf"})
STRUCTURED_OUTPUT_EXTENSIONS: frozenset[str] = frozenset({".html", ".htm", ".svg", ".json", ".xml"})

PROMPT_HEADINGS: frozenset[str] = frozenset(
    {
        "pedido inicial",
        "pedido",
        "pedido original",
        "prompt inicial",
        "prompt original",
        "prompt",
        "solicitacao",
        "solicitacao inicial",
        "objetivo",
        "tarefa",
        "requisito",
        "requisitos",
        "original prompt",
        "user prompt",
        "request",
        "goal",
        "task",
    }
)

CRITERIA_HEADINGS: frozenset[str] = frozenset(
    {
        "criterios",
        "criterios de aceite",
        "criterios de verificacao",
        "regras do usuario",
        "restricoes",
        "acceptance criteria",
        "criteria",
        "constraints",
    }
)

REFERENCE_HEADINGS: frozenset[str] = frozenset(
    {
        "referencia",
        "referencias",
        "arquivo de referencia",
        "arquivos de referencia",
        "imagem de referencia",
        "imagens de referencia",
        "entrada",
        "entradas",
        "reference",
        "references",
        "input",
        "inputs",
    }
)

DELIVERABLE_HEADINGS: frozenset[str] = frozenset(
    {
        "entrega",
        "entregas",
        "saida",
        "saidas",
        "resultado",
        "resultados",
        "arquivo gerado",
        "arquivos gerados",
        "arquivos para verificacao",
        "arquivo para verificacao",
        "artefato",
        "artefatos",
        "deliverable",
        "deliverables",
        "output",
        "outputs",
        "artifact",
        "artifacts",
    }
)

BLOCKED_SECTION_KEYWORDS: tuple[str, ...] = (
    "justificativa",
    "explicacao",
    "o que foi feito",
    "o que eu fiz",
    "passos",
    "passo a passo",
    "etapas",
    "implementacao",
    "detalhes tecnicos",
    "decisoes",
    "notas",
    "nota do agente",
    "observacoes",
    "observacao",
    "raciocinio",
    "changelog",
    "codigo",
    "logs",
    "debug",
    "limitacoes",
    "comentarios",
    "resumo do agente",
    "resumo da execucao",
    "como foi feito",
    "contexto tecnico",
    "tentativas",
    "ajustes feitos",
    "alteracoes feitas",
    "rationale",
    "explanation",
    "implementation",
    "developer notes",
    "agent notes",
    "reasoning",
    "steps taken",
    "what i did",
    "changes made",
    "limitations",
    "workaround",
)

# Linhas que claramente representam justificativa/raciocinio do criador.
COMMENTARY_LINE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"^\s*(?:[-*]\s*)?(?:justificativa|explicacao|explica[cç][aã]o|motivo|nota(?:s)?(?:\s+do\s+(?:agente|criador|desenvolvedor))?|obs(?:erva[cç][aã]o)?|raciocinio|racioc[ií]nio|implementa[cç][aã]o|decis[aã]o\s+t[eé]cnica|limita[cç][aã]o|rationale|note|agent\s+note|explanation|reason)\s*:",
        re.IGNORECASE,
    ),
    re.compile(
        r"^\s*(?:[-*]\s*)?(?:eu\s+(?:fiz|optei|decidi|alterei|usei|tentei|apliquei|criei|ajustei|mantive|preferi)|optei\s+por|decidi\s+(?:usar|fazer|aplicar|manter)|alterei\s+o\s+c[oó]digo|no\s+c[oó]digo|para\s+resolver\s+isso\s+(?:eu\s+)?(?:fiz|usei|alterei)|infelizmente\s+(?:n[aã]o\s+foi|nao\s+foi)|passo\s+\d+\s*:|etapa\s+\d+\s*:)",
        re.IGNORECASE,
    ),
)

CODE_PATH_MENTION_RE = re.compile(
    r"`[^`]+\.(?:py|js|ts|tsx|jsx|go|rs|java|c|cpp|h|cs|rb|php|sh|sql|ya?ml|toml|css|scss)`",
    re.IGNORECASE,
)
MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+[\"'][^\"']*[\"'])?\)")
BACKTICK_PATH_RE = re.compile(r"`([^`\n]+)`")
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
YAML_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)
HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")


@dataclass(frozen=True)
class SanitizerConfig:
    """Parametros de seguranca e limpeza do coletor."""

    max_image_bytes: int = 10 * 1024 * 1024
    max_text_bytes: int = 32 * 1024
    max_document_bytes: int = 10 * 1024 * 1024
    max_prompt_chars: int = 8000
    allow_structured_outputs: bool = False
    # Quando True, so aceita arquivos dentro de `base_dir` (apos resolver links
    # simbolicos e `..`). Usado pela interface web, onde o MD vem do navegador.
    confine_to_base_dir: bool = False


@dataclass
class _Section:
    title: str | None
    normalized_title: str
    lines: list[str]


def normalize_label(text: str) -> str:
    """Normaliza string para comparacao sem acentos, pontuacao ou caixa."""
    decomposed = unicodedata.normalize("NFKD", text)
    ascii_only = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    cleaned = re.sub(r"[^a-zA-Z0-9\s]", " ", ascii_only).lower()
    return " ".join(cleaned.split())


def collect_and_sanitize(
    submission: CreatorSubmission,
    config: SanitizerConfig | None = None,
) -> tuple[VerificationPacket, SanitizationReport]:
    """Coleta MD e arquivos do criador, removendo codigo e justificativas.

    Retorna o VerificationPacket cru (seguro para o verificador) e o
    SanitizationReport (auditoria local).
    """
    cfg = config or SanitizerConfig()
    warnings: list[str] = []
    removed_sections: list[str] = []
    rejected_files: list[RejectedFile] = []

    raw_md, resolved_base = _load_markdown(submission, warnings)
    cleaned_md, removed_code_blocks, frontmatter_prompt = _strip_global_noise(raw_md)
    sections = _split_sections(cleaned_md)

    prompt_parts: list[str] = []
    criteria_items: list[str] = []
    md_ref_paths: list[Path] = []
    md_out_paths: list[Path] = []
    fallback_parts: list[str] = []
    removed_commentary_lines = 0

    for sec in sections:
        kind = _classify_section(sec.normalized_title)
        if kind == "blocked":
            removed_sections.append(sec.title or "(sem titulo)")
            continue

        if kind == "reference":
            md_ref_paths.extend(_extract_paths(sec.lines, resolved_base))
            continue

        if kind == "deliverable":
            md_out_paths.extend(_extract_paths(sec.lines, resolved_base))
            continue

        kept_lines, dropped = _filter_commentary_lines(sec.lines)
        removed_commentary_lines += dropped
        if not kept_lines:
            continue

        if kind == "prompt":
            prompt_parts.append("\n".join(kept_lines))
        elif kind == "criteria":
            criteria_items.extend(_extract_list_items(kept_lines))
        else:
            fallback_parts.append("\n".join(kept_lines))

    if not prompt_parts and frontmatter_prompt:
        kept_lines, dropped = _filter_commentary_lines(frontmatter_prompt.splitlines())
        removed_commentary_lines += dropped
        if kept_lines:
            prompt_parts.append("\n".join(kept_lines))

    if not prompt_parts and fallback_parts:
        prompt_parts = fallback_parts

    original_prompt = _compact_text("\n\n".join(prompt_parts))
    if len(original_prompt) > cfg.max_prompt_chars:
        original_prompt = original_prompt[: cfg.max_prompt_chars].rstrip()
        warnings.append(f"Pedido inicial truncado para {cfg.max_prompt_chars} caracteres.")

    if not original_prompt:
        raise EmptyPromptError(
            "Nenhum pedido inicial valido restou apos a sanitizacao do Markdown."
        )

    # Junta arquivos explicitos + declarados no MD sem duplicar.
    all_refs = _merge_paths(submission.reference_files, md_ref_paths, resolved_base)
    all_outs = _merge_paths(submission.deliverable_files, md_out_paths, resolved_base)

    if cfg.confine_to_base_dir:
        all_refs = _confine_paths(all_refs, resolved_base, ArtifactRole.REFERENCE, rejected_files)
        all_outs = _confine_paths(all_outs, resolved_base, ArtifactRole.DELIVERABLE, rejected_files)

    artifacts: list[Artifact] = []
    artifacts.extend(_validate_artifacts(all_refs, ArtifactRole.REFERENCE, cfg, rejected_files))
    artifacts.extend(_validate_artifacts(all_outs, ArtifactRole.DELIVERABLE, cfg, rejected_files))

    packet = VerificationPacket(
        original_prompt=original_prompt,
        acceptance_criteria=tuple(dict.fromkeys(c for c in criteria_items if c)),
        artifacts=tuple(artifacts),
    )
    report = SanitizationReport(
        removed_sections=tuple(removed_sections),
        removed_code_blocks=removed_code_blocks,
        removed_commentary_lines=removed_commentary_lines,
        rejected_files=tuple(rejected_files),
        warnings=tuple(warnings),
    )
    return packet, report


def _load_markdown(submission: CreatorSubmission, warnings: list[str]) -> tuple[str, Path]:
    if submission.markdown_text is not None:
        base = submission.base_dir or (
            submission.markdown_path.parent if submission.markdown_path else Path.cwd()
        )
        return _normalize_raw_text(submission.markdown_text), base.resolve()

    if submission.markdown_path is None:
        raise EmptyPromptError("Forneca 'markdown_path' ou 'markdown_text' na submissao.")

    path = Path(submission.markdown_path)
    if not path.exists() or not path.is_file():
        raise EmptyPromptError(f"Arquivo Markdown nao encontrado: {path}")

    raw_bytes = path.read_bytes()
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw_bytes.decode("utf-8", errors="replace")
        warnings.append(f"Encoding invalido corrigido em {path.name}.")

    base = (submission.base_dir or path.parent).resolve()
    return _normalize_raw_text(text), base


def _normalize_raw_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Remove NUL e caracteres de controle invisiveis (preserva \n e \t).
    text = "".join(ch for ch in text if ch in ("\n", "\t") or ord(ch) >= 32)
    return text


def _strip_global_noise(text: str) -> tuple[str, int, str | None]:
    frontmatter_prompt: str | None = None
    match = YAML_FRONTMATTER_RE.match(text)
    if match:
        fm_body = match.group(1)
        for line in fm_body.splitlines():
            kv = re.match(r"^\s*(?:prompt|pedido|pedido_inicial|objetivo)\s*:\s*(.+)$", line, re.I)
            if kv:
                frontmatter_prompt = kv.group(1).strip().strip("\"'")
                break
        text = text[match.end() :]

    text = HTML_COMMENT_RE.sub("", text)

    # Remove blocos de codigo cercados (``` ou ~~~) mesmo se nao fechados.
    out_lines: list[str] = []
    in_fence = False
    fence_char = ""
    fence_len = 0
    removed_blocks = 0

    for line in text.splitlines():
        m = FENCE_RE.match(line)
        if not in_fence and m:
            in_fence = True
            token = m.group(1)
            fence_char = token[0]
            fence_len = len(token)
            removed_blocks += 1
            continue
        if in_fence:
            stripped = line.strip()
            if (
                stripped
                and set(stripped) == {fence_char}
                and len(stripped) >= fence_len
            ):
                in_fence = False
            continue
        out_lines.append(line)

    return "\n".join(out_lines), removed_blocks, frontmatter_prompt


def _split_sections(text: str) -> list[_Section]:
    sections: list[_Section] = []
    current = _Section(title=None, normalized_title="", lines=[])

    for line in text.splitlines():
        m = HEADING_RE.match(line)
        if m:
            if current.title is not None or any(l.strip() for l in current.lines):
                sections.append(current)
            title = m.group(2).strip()
            current = _Section(title=title, normalized_title=normalize_label(title), lines=[])
        else:
            current.lines.append(line)

    if current.title is not None or any(l.strip() for l in current.lines):
        sections.append(current)
    return sections


def _classify_section(norm_title: str) -> str:
    if not norm_title:
        return "unlabeled"
    for kw in BLOCKED_SECTION_KEYWORDS:
        if norm_title == kw or kw in norm_title:
            return "blocked"
    if norm_title in PROMPT_HEADINGS:
        return "prompt"
    if norm_title in CRITERIA_HEADINGS:
        return "criteria"
    if norm_title in REFERENCE_HEADINGS:
        return "reference"
    if norm_title in DELIVERABLE_HEADINGS:
        return "deliverable"
    return "unlabeled"


def _filter_commentary_lines(lines: list[str]) -> tuple[list[str], int]:
    kept: list[str] = []
    dropped = 0
    for line in lines:
        stripped = line.strip()
        if not stripped:
            kept.append("")
            continue
        if any(pat.match(stripped) for pat in COMMENTARY_LINE_PATTERNS):
            dropped += 1
            continue
        # Remove mencoes inline a arquivos de codigo-fonte se a linha e apenas sobre codigo.
        cleaned_line = CODE_PATH_MENTION_RE.sub("", line).rstrip()
        if not cleaned_line.strip():
            dropped += 1
            continue
        kept.append(cleaned_line)
    return kept, dropped


def _extract_list_items(lines: list[str]) -> list[str]:
    items: list[str] = []
    for line in lines:
        cleaned = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", line).strip()
        if cleaned:
            items.append(cleaned)
    return items


def _extract_paths(lines: list[str], base_dir: Path) -> list[Path]:
    found: list[Path] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        candidates: list[str] = []
        candidates.extend(MARKDOWN_LINK_RE.findall(stripped))
        if not candidates:
            candidates.extend(BACKTICK_PATH_RE.findall(stripped))
        if not candidates:
            item = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", stripped)
            item = re.sub(r"^[A-Za-z0-9_-]+\s*:\s*", "", item).strip()
            # Pega primeiro token com extensao de arquivo, se existir.
            token = item.split()[0].strip("\"'<>(),;") if item else ""
            if token:
                token_name = Path(token).name
                if (
                    "." in token_name
                    or token_name.lower() in BLOCKED_FILENAMES
                    or (base_dir / token).exists()
                ):
                    candidates.append(token)

        for cand in candidates:
            if re.match(r"^https?://", cand, re.I):
                continue
            p = Path(cand)
            resolved = p if p.is_absolute() else (base_dir / p)
            found.append(resolved)
    return found


def _merge_paths(
    explicit: Iterable[Path],
    from_md: Iterable[Path],
    base_dir: Path,
) -> list[Path]:
    seen: set[str] = set()
    merged: list[Path] = []
    for raw in (*explicit, *from_md):
        p = Path(raw)
        resolved = p if p.is_absolute() else (base_dir / p)
        key = str(resolved.resolve(strict=False))
        if key not in seen:
            seen.add(key)
            merged.append(resolved)
    return merged


def _confine_paths(
    paths: list[Path],
    base_dir: Path,
    role: ArtifactRole,
    rejected: list[RejectedFile],
) -> list[Path]:
    """Rejeita caminhos que escapam de `base_dir` (absolutos, `..` ou symlinks)."""
    base = base_dir.resolve()
    kept: list[Path] = []
    for path in paths:
        if path.resolve(strict=False).is_relative_to(base):
            kept.append(path)
        else:
            rejected.append(RejectedFile(str(path), role, "fora da pasta permitida"))
    return kept


def _validate_artifacts(
    paths: list[Path],
    role: ArtifactRole,
    cfg: SanitizerConfig,
    rejected: list[RejectedFile],
) -> list[Artifact]:
    prefix = "REF" if role is ArtifactRole.REFERENCE else "OUT"
    valid: list[Artifact] = []
    counter = 1

    for path in paths:
        name_lower = path.name.lower()
        ext = path.suffix.lower()

        if name_lower in BLOCKED_FILENAMES or ext in BLOCKED_CODE_EXTENSIONS:
            rejected.append(RejectedFile(str(path), role, "codigo-fonte/config bloqueado"))
            continue

        if not path.exists():
            rejected.append(RejectedFile(str(path), role, "arquivo nao encontrado"))
            continue

        if not path.is_file():
            rejected.append(RejectedFile(str(path), role, "caminho nao e um arquivo regular"))
            continue

        size = path.stat().st_size
        if size == 0:
            rejected.append(RejectedFile(str(path), role, "arquivo vazio"))
            continue

        if ext in IMAGE_EXTENSIONS:
            if size > cfg.max_image_bytes:
                rejected.append(RejectedFile(str(path), role, "imagem excede limite de tamanho"))
                continue
            header = path.read_bytes()[:65536]
            detected = _inspect_image_header(header)
            if detected is None:
                rejected.append(
                    RejectedFile(str(path), role, "conteudo nao corresponde a imagem valida")
                )
                continue
            mime_type, width, height = detected
            valid.append(
                Artifact(
                    label=f"{prefix}-{counter}",
                    role=role,
                    kind=ArtifactKind.IMAGE,
                    path=path.resolve(),
                    mime_type=mime_type,
                    size_bytes=size,
                    width=width,
                    height=height,
                )
            )
            counter += 1
            continue

        if ext in DOCUMENT_EXTENSIONS:
            if size > cfg.max_document_bytes:
                rejected.append(RejectedFile(str(path), role, "documento excede limite de tamanho"))
                continue
            if not path.read_bytes()[:5].startswith(b"%PDF-"):
                rejected.append(RejectedFile(str(path), role, "PDF invalido"))
                continue
            valid.append(
                Artifact(
                    label=f"{prefix}-{counter}",
                    role=role,
                    kind=ArtifactKind.DOCUMENT,
                    path=path.resolve(),
                    mime_type="application/pdf",
                    size_bytes=size,
                )
            )
            counter += 1
            continue

        allowed_text = TEXT_EXTENSIONS | (
            STRUCTURED_OUTPUT_EXTENSIONS if cfg.allow_structured_outputs else frozenset()
        )
        if ext in allowed_text:
            if size > cfg.max_text_bytes:
                rejected.append(RejectedFile(str(path), role, "arquivo de texto excede limite"))
                continue
            raw_text = path.read_bytes().decode("utf-8", errors="replace")
            mime = mimetypes.guess_type(path.name)[0] or "text/plain"
            valid.append(
                Artifact(
                    label=f"{prefix}-{counter}",
                    role=role,
                    kind=ArtifactKind.TEXT,
                    path=path.resolve(),
                    mime_type=mime,
                    size_bytes=size,
                    text_excerpt=_compact_text(_normalize_raw_text(raw_text)),
                )
            )
            counter += 1
            continue

        rejected.append(
            RejectedFile(
                str(path),
                role,
                f"extensao '{ext or '(sem extensao)'}' nao permitida para verificacao cega",
            )
        )

    return valid


def _inspect_image_header(data: bytes) -> tuple[str, int | None, int | None] | None:
    """Valida magic bytes e extrai dimensoes de PNG/JPEG/GIF/WEBP/BMP sem deps externas."""
    # PNG
    if len(data) >= 24 and data.startswith(b"\x89PNG\r\n\x1a\n") and data[12:16] == b"IHDR":
        width, height = struct.unpack(">II", data[16:24])
        return "image/png", width, height

    # GIF
    if len(data) >= 10 and data[:6] in (b"GIF87a", b"GIF89a"):
        width, height = struct.unpack("<HH", data[6:10])
        return "image/gif", width, height

    # BMP
    if len(data) >= 26 and data[:2] == b"BM":
        width, height = struct.unpack("<ii", data[18:26])
        return "image/bmp", abs(width), abs(height)

    # WEBP
    if len(data) >= 30 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        chunk = data[12:16]
        if chunk == b"VP8X" and len(data) >= 30:
            w = int.from_bytes(data[24:27], "little") + 1
            h = int.from_bytes(data[27:30], "little") + 1
            return "image/webp", w, h
        if chunk == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
            w, h = struct.unpack("<HH", data[26:30])
            return "image/webp", w & 0x3FFF, h & 0x3FFF
        if chunk == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
            bits = int.from_bytes(data[21:25], "little")
            w = (bits & 0x3FFF) + 1
            h = ((bits >> 14) & 0x3FFF) + 1
            return "image/webp", w, h
        return "image/webp", None, None

    # JPEG
    if len(data) >= 4 and data[:2] == b"\xff\xd8":
        w, h = _parse_jpeg_dimensions(data)
        return "image/jpeg", w, h

    return None


def _parse_jpeg_dimensions(data: bytes) -> tuple[int | None, int | None]:
    idx = 2
    n = len(data)
    sof_markers = {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }
    while idx < n - 1:
        if data[idx] != 0xFF:
            idx += 1
            continue
        while idx < n and data[idx] == 0xFF:
            idx += 1
        if idx >= n:
            break
        marker = data[idx]
        idx += 1
        if marker in (0xD8, 0xD9, 0x01) or (0xD0 <= marker <= 0xD7):
            continue
        if idx + 2 > n:
            break
        seg_len = struct.unpack(">H", data[idx : idx + 2])[0]
        if seg_len < 2 or idx + seg_len > n:
            break
        if marker in sof_markers and seg_len >= 7:
            height, width = struct.unpack(">HH", data[idx + 3 : idx + 7])
            return width, height
        idx += seg_len
    return None, None


def _compact_text(text: str) -> str:
    lines = [line.rstrip() for line in text.splitlines()]
    compact: list[str] = []
    blank_run = 0
    for line in lines:
        if not line:
            blank_run += 1
            if blank_run <= 1 and compact:
                compact.append("")
        else:
            blank_run = 0
            compact.append(line)
    return "\n".join(compact).strip()

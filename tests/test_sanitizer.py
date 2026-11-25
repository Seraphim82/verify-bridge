"""Testes do coletor/sanitizador de MD e artefatos (incluindo edge cases)."""

from __future__ import annotations

import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from verify_bridge.models import ArtifactKind, ArtifactRole, CreatorSubmission, EmptyPromptError
from verify_bridge.packet import render_raw_packet_text
from verify_bridge.sanitizer import SanitizerConfig, collect_and_sanitize


def make_png(width: int, height: int) -> bytes:
    raw_rows = b"".join(b"\x00" + b"\xff\xc0\x00" * width for _ in range(height))

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


def make_gif(width: int, height: int) -> bytes:
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00\x00\x00"


def make_jpeg(width: int, height: int) -> bytes:
    sof0 = struct.pack(">BHHB", 8, height, width, 3) + b"\x01\x11\x00\x02\x11\x00\x03\x11\x00"
    return b"\xff\xd8\xff\xc0" + struct.pack(">H", len(sof0) + 2) + sof0 + b"\xff\xd9"


class TestSanitizer(unittest.TestCase):
    def test_strips_justifications_code_blocks_and_blocks_source_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ref_img = root / "usuario_original.png"
            out_img = root / "final_perfeito_v9.png"
            py_file = root / "gerador.py"
            dockerfile = root / "Dockerfile"
            ref_img.write_bytes(make_png(64, 48))
            out_img.write_bytes(make_png(128, 96))
            py_file.write_text("x = 1\n", encoding="utf-8")
            dockerfile.write_text("FROM python:3.12\n", encoding="utf-8")

            md = (
                "---\nautor: agente-dev\n---\n"
                "<!-- comentario secreto -->\n"
                "# Pedido Inicial\n"
                "Imagem de um peixe dourado identico a referencia, pequena e limpa.\n"
                "Justificativa: eu aumentei o brilho porque achei melhor.\n"
                "Eu fiz um recorte manual no script `gerador.py`.\n\n"
                "## Criterios de Aceite\n"
                "- Mesma cor dourada\n"
                "- Fundo limpo\n\n"
                "## O que foi feito\n"
                "Passo 1: rodei o pipeline.\n"
                "```python\nprint('nao deve vazar')\n```\n\n"
                "## Referencias\n"
                f"- [ref]({ref_img.name})\n\n"
                "## Entrega\n"
                f"- `{out_img.name}`\n"
                f"- {py_file.name}\n"
                f"- {dockerfile.name}\n"
            )

            packet, audit = collect_and_sanitize(
                CreatorSubmission(markdown_text=md, base_dir=root)
            )

            self.assertEqual(
                packet.original_prompt,
                "Imagem de um peixe dourado identico a referencia, pequena e limpa.",
            )
            self.assertEqual(packet.acceptance_criteria, ("Mesma cor dourada", "Fundo limpo"))
            self.assertEqual(len(packet.artifacts), 2)

            ref_art, out_art = packet.artifacts
            self.assertEqual(ref_art.label, "REF-1")
            self.assertEqual(ref_art.role, ArtifactRole.REFERENCE)
            self.assertEqual((ref_art.width, ref_art.height), (64, 48))
            self.assertEqual(out_art.label, "OUT-1")
            self.assertEqual(out_art.role, ArtifactRole.DELIVERABLE)
            self.assertEqual((out_art.width, out_art.height), (128, 96))

            # Garante que texto renderizado nao vaza justificativas, codigo ou nomes enviesados
            raw_text = render_raw_packet_text(packet)
            for forbidden in (
                "Justificativa",
                "nao deve vazar",
                "comentario secreto",
                "final_perfeito_v9",
                "gerador.py",
                "Dockerfile",
            ):
                self.assertNotIn(forbidden, raw_text)

            self.assertEqual(audit.removed_sections, ("O que foi feito",))
            self.assertEqual(audit.removed_code_blocks, 1)
            self.assertEqual(audit.removed_commentary_lines, 2)
            self.assertEqual(len(audit.rejected_files), 2)

    def test_unstructured_markdown_and_unclosed_code_fence(self) -> None:
        md = (
            "Crie um banner minimalista com fundo branco.\n"
            "Optei por usar fonte Arial porque estava instalada.\n"
            "```js\nconsole.log('vazamento');\n"
        )
        packet, audit = collect_and_sanitize(CreatorSubmission(markdown_text=md))
        self.assertEqual(packet.original_prompt, "Crie um banner minimalista com fundo branco.")
        self.assertEqual(audit.removed_code_blocks, 1)
        self.assertEqual(audit.removed_commentary_lines, 1)

    def test_spoofed_corrupt_missing_empty_and_oversized_files_do_not_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spoofed_png = root / "falso.png"
            empty_png = root / "vazio.png"
            big_png = root / "grande.png"
            subdir = root / "pasta"
            valid_gif = root / "ok.gif"
            valid_jpg = root / "ok.jpg"

            spoofed_png.write_text("import os\nprint('codigo disfarcado')\n", encoding="utf-8")
            empty_png.write_bytes(b"")
            big_png.write_bytes(make_png(100, 100))
            subdir.mkdir()
            valid_gif.write_bytes(make_gif(32, 16))
            valid_jpg.write_bytes(make_jpeg(200, 150))

            sub = CreatorSubmission(
                markdown_text="# Pedido\nComparar imagens.",
                reference_files=(valid_gif, root / "inexistente.png", subdir),
                deliverable_files=(valid_jpg, spoofed_png, empty_png, big_png),
                base_dir=root,
            )
            cfg = SanitizerConfig(max_image_bytes=200)
            packet, audit = collect_and_sanitize(sub, config=cfg)

            self.assertEqual(len(packet.artifacts), 2)
            self.assertEqual((packet.artifacts[0].width, packet.artifacts[0].height), (32, 16))
            self.assertEqual((packet.artifacts[1].width, packet.artifacts[1].height), (200, 150))
            self.assertEqual(len(audit.rejected_files), 5)
            reasons = {Path(r.path).name: r.reason for r in audit.rejected_files}
            self.assertIn("imagem valida", reasons["falso.png"])
            self.assertIn("vazio", reasons["vazio.png"])
            self.assertIn("limite", reasons["grande.png"])
            self.assertIn("nao encontrado", reasons["inexistente.png"])
            self.assertIn("arquivo regular", reasons["pasta"])

    def test_bad_encoding_and_control_chars_from_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            md_path = Path(tmp) / "pedido.md"
            # Bytes invalidos em UTF-8 + NUL + CRLF
            md_path.write_bytes(b"# Pedido\r\nImagem limpa\x00 \xff\xfe de peixe.\r\n")
            packet, audit = collect_and_sanitize(CreatorSubmission(markdown_path=md_path))
            self.assertIn("Imagem limpa", packet.original_prompt)
            self.assertNotIn("\x00", packet.original_prompt)
            self.assertTrue(any("Encoding" in w for w in audit.warnings))

    def test_empty_or_only_commentary_raises_empty_prompt_error(self) -> None:
        with self.assertRaises(EmptyPromptError):
            collect_and_sanitize(
                CreatorSubmission(
                    markdown_text="## Justificativa\nEu fiz tudo certo.\n```py\npass\n```"
                )
            )

    def test_structured_output_blocked_by_default_and_allowed_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            html = root / "pagina.html"
            html.write_text("<h1>Oi</h1>", encoding="utf-8")

            sub = CreatorSubmission(
                markdown_text="Verificar titulo.",
                deliverable_files=(html,),
                base_dir=root,
            )
            p1, a1 = collect_and_sanitize(sub)
            self.assertEqual(len(p1.artifacts), 0)
            self.assertEqual(len(a1.rejected_files), 1)

            p2, a2 = collect_and_sanitize(sub, config=SanitizerConfig(allow_structured_outputs=True))
            self.assertEqual(len(p2.artifacts), 1)
            self.assertEqual(p2.artifacts[0].kind, ArtifactKind.TEXT)
            self.assertEqual(len(a2.rejected_files), 0)


if __name__ == "__main__":
    unittest.main()

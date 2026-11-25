"""Utilitarios compartilhados pelos testes (sem dependencias externas).

Fornecem uma imagem PNG neutra e "IAs falsas" feitas com scripts Python, para
exercitar o loop completo sem chamar nenhuma IA de verdade e em qualquer sistema.
"""

from __future__ import annotations

import json
import struct
import sys
import zlib
from pathlib import Path


def make_png(width: int, height: int, rgb: tuple[int, int, int] = (200, 160, 40)) -> bytes:
    """PNG valido de cor solida com uma faixa escura (para nao ser trivial)."""
    rows = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            row.extend((30, 30, 30) if y == height // 2 else rgb)
        rows.append(bytes(row))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(tag + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(b"".join(rows)))
        + chunk(b"IEND", b"")
    )


def verdict(*severities: str) -> str:
    """Resposta JSON de verificador com uma falha por severidade informada."""
    falhas = [
        {
            "id": f"F{i}",
            "severidade": sev,
            "observado": f"problema {i}",
            "esperado": f"correto {i}",
            "acao": f"Corrigir item {i}",
            "evidencia": "OUT-1",
        }
        for i, sev in enumerate(severities, start=1)
    ]
    return json.dumps({"criterios": [], "falhas": falhas})


APPROVED = verdict()


def scripted_verifier_cmd(folder: Path, responses: list[str]) -> str:
    """Comando CLI de um verificador falso que devolve `responses` em ordem.

    A ultima resposta se repete se houver mais rodadas que respostas.
    """
    data = folder / "respostas.json"
    counter = folder / "rodada.txt"
    data.write_text(json.dumps(responses), encoding="utf-8")
    script = folder / "verificador_falso.py"
    script.write_text(
        "import json, pathlib\n"
        f"data = json.loads(pathlib.Path({str(data)!r}).read_text(encoding='utf-8'))\n"
        f"c = pathlib.Path({str(counter)!r})\n"
        "n = int(c.read_text()) if c.exists() else 0\n"
        "c.write_text(str(n + 1))\n"
        "print(data[min(n, len(data) - 1)])\n",
        encoding="utf-8",
    )
    return f'"{sys.executable}" "{script}"'


def noop_creator_cmd() -> str:
    """Comando CLI de um criador falso que nao altera nada."""
    return f'"{sys.executable}" -c "pass"'


def scripted_agents(folder: Path, responses: list[str]) -> dict:
    """Configuracao {verifier, creator} pronta para POST /api/runs."""
    return {
        "verifier": {"provider": "cli", "cli_cmd": scripted_verifier_cmd(folder, responses)},
        "creator": {"provider": "cli", "cli_cmd": noop_creator_cmd()},
    }

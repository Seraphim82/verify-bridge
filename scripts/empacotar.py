"""Monta um .zip por sistema (Windows, Mac, Linux) a partir do mesmo codigo.

O programa dentro dos tres pacotes e identico; muda apenas o atalho de abrir
e o LEIA-ME. Uso:

    python scripts/empacotar.py                 # gera dist/verify-bridge-<so>.zip
    python scripts/empacotar.py --testar windows  # gera e roda os testes dentro do pacote extraido
"""

from __future__ import annotations

import argparse
import re
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
PASTA_NO_ZIP = "verify-bridge"

# Arquivos comuns aos tres pacotes.
COMUNS_PASTAS = ("verify_bridge", "tests", "examples")
COMUNS_ARQUIVOS = ("pyproject.toml", "README.md", "MANUAL-DE-UTILIZACAO.md", "LICENSE")
IGNORAR = {"__pycache__", ".DS_Store"}

SISTEMAS = {
    "windows": {"atalho": "Abrir-Windows.bat", "crlf": True},
    "mac": {"atalho": "Abrir-Mac.command", "crlf": False},
    "linux": {"atalho": "Abrir-Linux.sh", "crlf": False},
}


def versao() -> str:
    texto = (RAIZ / "pyproject.toml").read_text(encoding="utf-8")
    achado = re.search(r'^version\s*=\s*"([^"]+)"', texto, re.MULTILINE)
    if not achado:
        raise SystemExit("versao nao encontrada no pyproject.toml")
    return achado.group(1)


def _arquivos_comuns() -> list[Path]:
    arquivos = [RAIZ / nome for nome in COMUNS_ARQUIVOS]
    for pasta in COMUNS_PASTAS:
        for caminho in sorted((RAIZ / pasta).rglob("*")):
            if caminho.is_file() and not (IGNORAR & set(caminho.parts)) and caminho.suffix != ".pyc":
                arquivos.append(caminho)
    return arquivos


def _texto_com_fim_de_linha(caminho: Path, crlf: bool) -> bytes:
    texto = caminho.read_text(encoding="utf-8").replace("\r\n", "\n")
    if crlf:
        texto = texto.replace("\n", "\r\n")
    return texto.encode("utf-8")


def _gravar(zf: zipfile.ZipFile, nome: str, dados: bytes, executavel: bool = False) -> None:
    info = zipfile.ZipInfo(f"{PASTA_NO_ZIP}/{nome}", date_time=(2026, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    modo = 0o755 if executavel else 0o644
    info.external_attr = (stat.S_IFREG | modo) << 16
    info.create_system = 3  # Unix: preserva a permissao de execucao ao extrair
    zf.writestr(info, dados)


def montar(sistema: str, destino: Path) -> Path:
    cfg = SISTEMAS[sistema]
    destino.mkdir(parents=True, exist_ok=True)
    saida = destino / f"verify-bridge-{sistema}.zip"
    with zipfile.ZipFile(saida, "w") as zf:
        for caminho in _arquivos_comuns():
            _gravar(zf, caminho.relative_to(RAIZ).as_posix(), caminho.read_bytes())
        atalho = RAIZ / cfg["atalho"]
        _gravar(
            zf,
            atalho.name,
            _texto_com_fim_de_linha(atalho, cfg["crlf"]),
            executavel=not cfg["crlf"],
        )
        leia = RAIZ / "pacotes" / f"LEIA-ME-{sistema}.txt"
        _gravar(zf, "LEIA-ME.txt", _texto_com_fim_de_linha(leia, cfg["crlf"]))
    return saida


def testar(pacote: Path) -> None:
    """Extrai o pacote numa pasta limpa e roda os testes la dentro."""
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(pacote) as zf:
            zf.extractall(tmp)
        pasta = Path(tmp) / PASTA_NO_ZIP
        nomes = sorted(p.name for p in pasta.iterdir())
        print(f"Conteudo de {pacote.name}: {', '.join(nomes)}")
        subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
            cwd=pasta,
            check=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--destino", default=str(RAIZ / "dist"))
    parser.add_argument("--testar", choices=sorted(SISTEMAS), help="roda os testes dentro deste pacote")
    args = parser.parse_args()

    destino = Path(args.destino)
    pacotes = {so: montar(so, destino) for so in SISTEMAS}
    for so, caminho in pacotes.items():
        print(f"{so:8} {caminho} ({caminho.stat().st_size} bytes)")
    if args.testar:
        testar(pacotes[args.testar])


if __name__ == "__main__":
    main()

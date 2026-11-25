# verify-bridge

[![Testes](https://github.com/Seraphim82/verify-bridge/actions/workflows/testes.yml/badge.svg)](https://github.com/Seraphim82/verify-bridge/actions/workflows/testes.yml)

## ⬇️ Baixar (versao mais recente)

| Sistema | Download | Como abrir |
|---|---|---|
| **Windows** | [verify-bridge-windows.zip](https://github.com/Seraphim82/verify-bridge/releases/latest/download/verify-bridge-windows.zip) | Extrair e dar dois cliques em `Abrir-Windows.bat` |
| **Mac** | [verify-bridge-mac.zip](https://github.com/Seraphim82/verify-bridge/releases/latest/download/verify-bridge-mac.zip) | Extrair e dar dois cliques em `Abrir-Mac.command` |
| **Linux** | [verify-bridge-linux.zip](https://github.com/Seraphim82/verify-bridge/releases/latest/download/verify-bridge-linux.zip) | Extrair e rodar `./Abrir-Linux.sh` |
| **Apresentacao (PDF)** | [Verify-Bridge-Apresentacao.pdf](https://github.com/Seraphim82/verify-bridge/releases/latest/download/Verify-Bridge-Apresentacao.pdf) | Explica tudo, com comparacao entre versoes |

Requisito: Python 3.10+. O programa dentro dos tres pacotes e o mesmo; muda so o atalho e o LEIA-ME.

**Versoes anteriores (tags):** [todas as Releases](https://github.com/Seraphim82/verify-bridge/releases) · [todas as tags](https://github.com/Seraphim82/verify-bridge/tags) — `v0.3.1` (atual, um pacote por sistema), `v0.3.0`, `v0.2.0` (ultima com o exemplo do peixe).

Para gerar os pacotes localmente: `python3 scripts/empacotar.py` (saem em `dist/`).

---

Coletor e conector de **verificacao cega** entre um **Agente Criador** e um **Agente Verificador** de IA, com loop automatico de correcoes.

- **Stack:** Python 3.10+ (somente biblioteca padrao, **zero dependencias externas**).
- **Entrada:** arquivo `.md` + artefatos vindos do agente criador.
- **Filtro cego:** remove justificativas, explicacoes, blocos de codigo, comentarios do criador e bloqueia arquivos de codigo-fonte antes de enviar ao verificador.
- **Classificacao objetiva:** rubrica baseada em criterios extraidos do pedido inicial + validacao deterministica de severidade (`critico`, `moderado`, `aceitavel`).
- **Saida:** prompt enxuto e direto com apenas as correcoes obrigatorias para o criador, repetindo em loop ate que nao restem falhas criticas nem moderadas.

## Arquitetura

| Modulo | Responsabilidade |
|---|---|
| `verify_bridge/models.py` | Tipos, enums (`Severity`, `LoopStopReason`, etc.) e dataclasses imutaveis. |
| `verify_bridge/sanitizer.py` | Le o MD e arquivos do criador, remove justificativas/codigo, valida magic bytes e dimensoes de imagens, atribui rotulos neutros (`REF-1`, `OUT-1`). |
| `verify_bridge/packet.py` | Compila o pacote cru enxuto (minimo de tokens, zero vies). |
| `verify_bridge/rubric.py` | Prompt de sistema curto e schema JSON estrito para a IA verificadora. |
| `verify_bridge/evaluator.py` | Parsing resiliente da resposta + regras deterministicas de coerencia e aprovacao. |
| `verify_bridge/feedback.py` | Gera o prompt direto de correcao para o agente criador. |
| `verify_bridge/adapters.py` | Conectores prontos para OpenAI-compatible, Anthropic, Gemini, CLI isolado e callables Python; execucao de CLI cancelavel. |
| `verify_bridge/subscriptions.py` | Conectores por **assinatura sem API**: Claude Code, Antigravity e Codex (status, login, verificador cego e criador). |
| `verify_bridge/manual.py` | **Modo Chat** assistido: a tela entrega o pedido pronto e recebe a resposta colada do chat. |
| `verify_bridge/orchestrator.py` | Motor do loop (`verify_once`, `run_verification_loop`) com retry de JSON, protecao contra estagnacao e limite de rodadas. |
| `verify_bridge/cli.py` | CLI (`ui`, `compile`, `verify`, `loop`). |

## Como a severidade e decidida

1. A IA verificadora recebe apenas `PEDIDO_INICIAL`, `CRITERIOS_EXPLICITOS` (se existirem) e os artefatos rotulados de forma neutra (`REF-1`, `OUT-1`).
2. Ela decompoe o pedido em **criterios atomicos** com peso (`essencial`, `importante`, `detalhe`) e aponta cada falha em JSON estruturado como `critico`, `moderado` ou `aceitavel`.
3. O `evaluator.py` aplica garantias deterministicas em codigo:
   - Se um criterio `essencial` falhar, qualquer achado vinculado e promovido a `critico` (e se nenhum achado foi emitido, um `critico` e sintetizado).
   - Se um criterio `importante` falhar sem achado, um `moderado` e sintetizado.
   - A rodada so e `APROVADO` quando **`critico == 0` e `moderado == 0`** (sem falhas ou apenas `aceitavel`).

## Uso rapido

### Abrir a tela visual (conectar as duas IAs no navegador)

- **No Finder (Mac):** de duplo clique em `Abrir-Mac.command` dentro da pasta `verify-bridge/`.
- **Ou no Terminal:**
```bash
cd verify-bridge   # a pasta extraida do .zip ou clonada
python3 -m verify_bridge ui
```
Abre automaticamente em `http://127.0.0.1:8765`, onde voce linka a **IA 1 (Criadora)** e a **IA 2 (Verificadora)** (assinatura Claude/Google/ChatGPT sem chave de API, Modo Chat, APIs Gemini/OpenAI/Ollama/Claude ou CLI local), envia o `.md` + arquivos e acompanha as rodadas ao vivo.

### Via Terminal (CLI)

```bash
cd verify-bridge

# 1. Abrir a tela visual
python3 -m verify_bridge ui

# 2. Inspecionar apenas o pacote cru gerado (com auditoria local do que foi removido)
python3 -m verify_bridge compile examples/pedido_exemplo.md --audit

# 3. Rodar uma verificacao cega com Gemini, OpenAI, Anthropic ou CLI local
GEMINI_API_KEY=... python3 -m verify_bridge verify pedido.md --ref ref.png --out out.png --provider gemini
OPENAI_API_KEY=... python3 -m verify_bridge verify pedido.md --ref ref.png --out out.png --provider openai --model gpt-4o
python3 -m verify_bridge verify pedido.md --ref ref.png --out out.png --provider cli --cli-cmd "meu-agente-verificador {packet_file}"

# 4. Rodar o loop completo criador <-> verificador
python3 -m verify_bridge loop pedido.md --ref ref.png --out out.png \
  --provider gemini \
  --creator-cmd "meu-agente-criador {correction_file}" \
  --max-iter 5 \
  --report-out relatorio.json

# 5. Rodar testes automatizados
python3 -m unittest discover -s tests -t . -v
```

## Uso como biblioteca Python

```python
from pathlib import Path
from verify_bridge import (
    CreatorSubmission,
    GeminiVerifier,
    LoopConfig,
    CallableCreator,
    run_verification_loop,
)

result = run_verification_loop(
    initial_submission=CreatorSubmission(
        markdown_path=Path("pedido_criador.md"),
        reference_files=(Path("ref.png"),),
        deliverable_files=(Path("out.png"),),
    ),
    verifier=GeminiVerifier(model="gemini-2.5-flash"),
    creator=CallableCreator(meu_agente_criador),
    config=LoopConfig(max_iterations=5, stagnation_limit=2),
)
print(result.approved, result.stop_reason)
```

## Licenca

[MIT](LICENSE): pode usar, modificar e redistribuir, mantendo o aviso de copyright.

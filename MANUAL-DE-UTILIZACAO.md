# Manual de Utilização — Verify Bridge

Ponte de **verificação cega** entre duas IAs: uma **IA Criadora** gera ou ajusta a entrega, o **Verify Bridge** limpa justificativas e bloqueia código-fonte, e uma **IA Verificadora** analisa friamente o resultado em loop até que o pedido inicial seja totalmente atendido.

```text
  [ IA 1 · Criadora ] ──► MD + arquivos ──► [ 🛡️ Verify Bridge ] ──► pacote cru ──► [ IA 2 · Verificadora ]
          ▲                                 (remove código e                             │
          │                                  justificativas)                             ▼
          └────────── prompt enxuto só com correções obrigatórias ◄── CRÍTICO / MODERADO / ACEITÁVEL
```

---

## 1. Pré-requisito único

- **Python 3.10 ou superior** instalado.
- **Não precisa instalar nenhuma biblioteca** (`pip install` não é necessário).

Se ainda não tiver Python no Windows, baixe em https://www.python.org/downloads/ e, na tela de instalação, marque **"Add Python to PATH"**.

---

## 2. Como abrir o programa

### Windows
1. Extraia o arquivo `.zip` em uma pasta (não execute de dentro do ZIP sem extrair).
2. Dê duplo clique em **`Abrir-Windows.bat`**.
3. O navegador abrirá automaticamente em `http://127.0.0.1:8765`.

*(Alternativa pelo Prompt/PowerShell dentro da pasta: `python -m verify_bridge ui`)*

### Linux
1. Extraia o `.zip` e abra o terminal dentro da pasta `verify-bridge`.
2. Execute:
   ```bash
   chmod +x Abrir-Linux.sh
   ./Abrir-Linux.sh
   ```
*(Alternativa direta: `python3 -m verify_bridge ui`)*

### macOS (Mac)
1. Dê duplo clique em **`Abrir-Mac.command`**.
2. Se o macOS avisar sobre arquivo baixado da internet, clique com o botão direito → **Abrir** (ou rode no Terminal: `python3 -m verify_bridge ui`).

> **Importante:** mantenha a janela do terminal aberta enquanto estiver usando a tela no navegador. Para encerrar o servidor, feche o terminal ou aperte `Ctrl + C`.

---

## 3. Primeiro teste rápido (sem chave de API)

Assim que a tela abrir no navegador:

1. Clique no botão **"Carregar Exemplo (Peixe)"** no canto superior direito.
   - Ele carrega um pedido de imagem de peixe dourado, um `.md` cheio de justificativas e código (para você ver o filtro limpando tudo), a imagem de referência (`REF-1`), uma entrega inicial errada (`OUT-1`) e um arquivo `script_gerador.py` (que será bloqueado).
2. *(Opcional)* Clique em **"Testar Filtro Cego"** para ver exatamente o que atravessa a ponte e o que é descartado antes de gastar qualquer token.
3. Clique em **"▶ Iniciar Conversa"**.
4. Acompanhe as **3 rodadas ao vivo**:
   - **Rodada 1:** erros `CRÍTICO` + `MODERADO` → gera prompt direto → Criador corrige.
   - **Rodada 2:** resta 1 erro `MODERADO` → Criador corrige.
   - **Rodada 3:** apenas 1 detalhe `ACEITÁVEL` → **APROVADO** e o loop para sozinho.

---

## 4. Como linkar suas duas IAs na tela

Na parte superior da tela existem dois painéis: **IA 1 · Criadora** (esquerda) e **IA 2 · Verificadora** (direita). Você pode combinar provedores diferentes (por exemplo, **Claude** criando e **Gemini** verificando).

### 4.1 Usando a sua assinatura (sem chave de API) — recomendado

Quem tem uma assinatura comum (Claude Pro/Max, ChatGPT Plus/Pro ou conta Google) **não precisa de chave de API**. O Verify Bridge usa o programa oficial de cada assinatura, que já guarda o seu login:

| Opção na tela | Assinatura | Programa usado | Status testado |
|---|---|---|---|
| **Claude Pro/Max · Claude Code** | Claude Pro ou Max | `claude` (Claude Code) | ✅ verificador e criador testados |
| **Google Gemini · Antigravity** | Conta Google / Google AI Pro | `agy` (Antigravity) | ✅ verificador testado |
| **ChatGPT Plus/Pro · Codex** | ChatGPT Plus ou Pro | `codex` (Codex CLI) | ⚠️ implementado, ainda não testado |

Como usar:
1. Escolha a opção no campo **Provedor**. A tela mostra na hora se o programa está instalado e se você está logado.
2. Se aparecer **"Entrar"**, clique: abre a página oficial de login da assinatura. Conclua o login e volte — o status atualiza sozinho.
3. Se aparecer **"Como instalar"**, clique para abrir a página oficial de instalação.
4. O campo **Modelo** é opcional (vazio = padrão da assinatura).

> **Importante:** o Gemini CLI antigo **não aceita mais login de conta Google pessoal** (o Google migrou esse uso para o Antigravity). Para usar Gemini sem chave, use a opção **Antigravity**.

Garantias deste modo:
- **Nunca cobra API por engano:** o Verify Bridge remove as chaves de API do ambiente do programa (ex.: `ANTHROPIC_API_KEY`), forçando o uso da assinatura.
- **Verificação cega de verdade:** a IA verificadora roda numa pasta temporária só com os arquivos renomeados (`REF-1.png`, `OUT-1.png`), apenas com permissão de **leitura**, e — no Claude Code — sem carregar suas memórias, hooks ou `CLAUDE.md`.
- **Criadora edita só a entrega:** a IA criadora trabalha na pasta da execução e edita os arquivos de entrega no lugar.
- **Limitação:** Claude Code e Antigravity editam **texto** muito bem, mas não desenham imagens. Para entregas em imagem, use o **Modo Chat** como criadora (o chat gera a imagem e você anexa o arquivo).

### 4.2 Modo Chat (copiar e colar) — funciona com qualquer assinatura

Use quando não quiser instalar nada ou quando a entrega for uma imagem. A cada rodada, a tela mostra um painel **"Sua vez"** com:
1. **Copiar pedido** e **Abrir chat** (claude.ai, chatgpt.com ou gemini.google.com, conforme escolhido);
2. botões para **baixar os arquivos** que devem ser anexados no chat (já com nomes neutros);
3. uma caixa para **colar a resposta** (ou anexar o arquivo revisado) e o botão **"Enviar resposta e continuar"**.

Dicas: para a **verificadora**, abra sempre um **chat novo** (sem histórico) para a análise continuar cega. Para a **criadora**, pode usar a mesma conversa em que a entrega foi criada.

> O Verify Bridge **não automatiza** os sites de chat: os termos de uso das assinaturas proíbem acesso automatizado e a sua conta poderia ser bloqueada. Por isso o modo é assistido — você só copia e cola.

### 4.3 Chave de API (opcional)

Quem tiver chave de API pode usá-la normalmente:

| Provedor | O que preencher | Observações |
|---|---|---|
| **Simulador Offline (Demo)** | Nada | Ideal para testar a tela sem gastar tokens. |
| **Google Gemini API** | Modelo (ex.: `gemini-2.5-flash`) + API Key | Suporta comparação visual de imagens e PDFs. Detecta `GEMINI_API_KEY` do ambiente. |
| **OpenAI / Ollama / OpenRouter** | Modelo (ex.: `gpt-4o-mini`) + Base URL + API Key | Para **Ollama local**, use Base URL `http://localhost:11434/v1/chat/completions` e deixe a API Key em branco. Detecta `OPENAI_API_KEY`. |
| **Anthropic (Claude API)** | Modelo (ex.: `claude-sonnet-5-5`) + API Key | Suporta imagens e PDFs. Detecta `ANTHROPIC_API_KEY`. |
| **Agente CLI personalizado** | Comando do terminal | Integra qualquer agente local de linha de comando. |

### Placeholders para Agente CLI Local
- **No Verificador (`IA 2`):** roda em uma **pasta temporária isolada** contendo apenas `packet.md`, `system.txt` e os arquivos validados renomeados como `REF-1.*`, `OUT-1.*` (sem acesso ao seu código-fonte).
  - Placeholders: `{packet_file}`, `{system_file}`, `{workdir}`
  - Exemplo: `meu-verificador --packet {packet_file} --dir {workdir}`
- **No Criador (`IA 1`):** recebe o prompt enxuto de correção e o relatório JSON da rodada.
  - Placeholders: `{correction_file}`, `{report_file}`, `{iteration}`
  - Exemplo: `meu-criador --fix {correction_file} --round {iteration}`

> **Segurança:** chaves coladas na tela ficam **somente em memória** e nunca são gravadas em disco nem no relatório JSON. O servidor só aceita conexões locais (`127.0.0.1`) e **recusa pedidos vindos de outros sites** abertos no navegador (proteção contra CSRF e DNS rebinding). Na tela web, a IA só recebe os arquivos que você mesmo enviou — caminhos para outros arquivos do computador citados no Markdown são recusados.

---

## 5. Como preparar o Markdown e os arquivos

### Modelo recomendado de Markdown (`.md`)
Você pode colar o texto direto na caixa ou clicar em **"Importar .md"**:

```markdown
# Pedido Inicial
Gerar uma imagem de um peixe dourado idêntico ao da imagem de referência,
pequena (até 256x256), limpa e com maior qualidade.

## Critérios de Aceite
- Mesma cor dourada e silhueta da referência
- Fundo limpo sem ruídos
- Resolução até 256x256
```

Mesmo que o agente criador envie um `.md` bagunçado com seções como `## Justificativa`, `## O que eu fiz`, `## Implementação`, comentários HTML ou blocos de código (` ```python ... ``` `), o **Filtro Cego** remove tudo automaticamente antes de mandar para a IA Verificadora.

### Referências vs. Entregas
- **Referências do Usuário (`REF-*`):** arquivos enviados originalmente pelo usuário (ex.: foto original do peixe, layout de referência, texto-base).
- **Entregas do Criador (`OUT-*`):** arquivos produzidos pela IA Criadora que precisam ser verificados.

### Arquivos aceitos e arquivos bloqueados
- **Aceitos por padrão:** imagens (`.png`, `.jpg`, `.jpeg`, `.webp`, `.gif`, `.bmp` — validadas pelo conteúdo real do arquivo), documentos (`.pdf`) e texto simples (`.txt`, `.md`, `.csv`).
- **Bloqueados sempre:** qualquer código-fonte ou configuração (`.py`, `.js`, `.ts`, `.go`, `.java`, `.sh`, `.yaml`, `.toml`, `.env`, `Dockerfile`, etc.) para garantir que a IA Verificadora nunca leia o código.
- **Opcionais:** marque **"Permitir HTML/SVG/JSON"** no painel central se a entrega final for um desses formatos.

---

## 6. Classificação de erros e controle do loop

A IA Verificadora analisa apenas o pacote cru e classifica cada falha em 3 níveis (com validação determinística do Verify Bridge para evitar falsos positivos):

- **`CRÍTICO` (vermelho) — bloqueia aprovação:** viola requisito essencial do pedido inicial, entrega errada/inutilizável ou contradiz a referência.
- **`MODERADO` (amarelo) — bloqueia aprovação:** atende ao núcleo do pedido, mas falha em atributo importante (tamanho, limpeza, qualidade, detalhe explícito).
- **`ACEITÁVEL` (verde) — NÃO bloqueia:** pedido atendido; imperfeição mínima dentro da tolerância natural.

### Quando o loop para automaticamente
1. **APROVADO:** quando `CRÍTICO == 0` e `MODERADO == 0` (nenhuma falha ou apenas `ACEITÁVEL`).
2. **Estagnado:** quando as mesmas falhas bloqueantes se repetem pelo número de rodadas configurado em **Estagnação** (padrão: `2`) — evita gastar tokens em loop infinito.
3. **Limite de rodadas:** quando atinge **Máx. rodadas** (padrão: `5`).
4. **Cancelado:** quando você clica em **"■ Parar"** — inclusive se uma IA estiver no meio de uma resposta (o programa dela é encerrado na hora) ou se a tela estiver esperando você colar a resposta do chat.

Os arquivos temporários de cada execução são apagados automaticamente 30 minutos depois que ela termina (e todos ao fechar o programa).

### Recursos úteis durante e após a conversa
- **Auditoria do filtro:** em cada rodada, clique em *"Ver auditoria local..."* para conferir tudo que foi barrado pela ponte.
- **Copiar prompt:** copie o prompt enxuto de correção de qualquer rodada com 1 clique (útil caso queira colar manualmente em outra IA).
- **Baixar Relatório JSON:** salva todo o histórico das rodadas, critérios, falhas e auditoria em arquivo.

---

## 7. Uso avançado pelo Terminal (CLI)

Se preferir automatizar sem abrir o navegador:

```bash
# Abrir tela web em outra porta
python3 -m verify_bridge ui --port 9000

# Rodar demonstração offline no terminal
python3 -m verify_bridge demo

# Apenas testar o filtro cego e ver a auditoria
python3 -m verify_bridge compile examples/creator_request_peixe.md --audit

# Fazer 1 rodada de verificação com Gemini
GEMINI_API_KEY="sua-chave" python3 -m verify_bridge verify pedido.md \
  --ref referencia.png --out entrega.png --provider gemini

# Verificar usando a assinatura do Claude (sem chave de API)
python3 -m verify_bridge verify pedido.md \
  --ref referencia.png --out entrega.png --provider sub_claude

# Loop completo só com assinaturas: Claude cria, Antigravity verifica
python3 -m verify_bridge loop pedido.md \
  --ref referencia.txt --out entrega.txt \
  --provider sub_antigravity --creator-provider sub_claude

# Rodar loop completo via CLI
GEMINI_API_KEY="sua-chave" python3 -m verify_bridge loop pedido.md \
  --ref referencia.png --out entrega.png \
  --provider gemini \
  --creator-cmd "meu-criador {correction_file}" \
  --max-iter 5 \
  --report-out relatorio.json

# Rodar todos os testes automatizados
python3 -m unittest discover -s tests -t . -v
```

---

## 8. Problemas comuns

| Situação | Como resolver |
|---|---|
| **"Python 3.10 ou superior não encontrado"** (Windows) | Instale Python 3.10+ em python.org marcando *"Add Python to PATH"* e reabra o `.bat`. |
| **Permissão negada no Linux/Mac** | Rode `chmod +x Abrir-Linux.sh` (ou `chmod +x Abrir-Mac.command`) no terminal. |
| **Porta 8765 já em uso** | O Verify Bridge escolhe outra porta livre automaticamente e mostra o endereço no terminal. |
| **Arquivo foi rejeitado na auditoria** | Verifique se não é código-fonte (bloqueado de propósito), se não está vazio (0 bytes) ou se não é um arquivo renomeado com extensão errada (ex.: texto salvo como `.png`). |
| **Erro de chave de API** | Confira se colou a chave no provedor certo ou se definiu `GEMINI_API_KEY` / `OPENAI_API_KEY` / `ANTHROPIC_API_KEY`. |
| **"Programa não instalado" numa opção de assinatura** | Clique em **"Como instalar"** e instale o programa oficial (Claude Code, Antigravity ou Codex). Depois clique em **"Atualizar status"**. |
| **"É preciso fazer login"** | Clique em **"Entrar"** no painel da IA e conclua o login no navegador. |
| **Claude aparece "logado via API (Console)"** | Seu Claude Code está logado com conta de API. Clique em **"Entrar"** para trocar para a assinatura Pro/Max. |
| **Erro "Nenhum pedido inicial válido"** | O `.md` enviado tinha apenas seções de justificativa ou blocos de código. Inclua o pedido original em `# Pedido Inicial`. |

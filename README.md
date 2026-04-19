# 🗳️ Urna Eletrônica Zero-Knowledge — EESC-USP

Sistema de votação eletrônica **anônimo**, **seguro** e **auditável** para assembleias da EESC-USP.

> **Nota de transparência**: Este sistema foi desenvolvido com auxílio de inteligência artificial (Claude, Anthropic) como ferramenta de pair-programming. Todo o código gerado foi revisado, auditado e validado manualmente pelos desenvolvedores. A IA foi utilizada para acelerar a implementação, mas **todas as decisões de arquitetura de segurança foram tomadas e validadas por humanos**. O código-fonte é aberto e auditável por qualquer pessoa.

---

## Por que uma urna eletrônica? E por que confiar nela?

A votação presencial em assembleia tem dois problemas conhecidos: (1) quem não pode comparecer não vota, e (2) a contagem manual é suscetível a erros e contestações. Uma urna eletrônica resolve ambos — mas introduz uma preocupação legítima: **como garantir que o sistema é honesto?**

Este documento explica, decisão por decisão, como o sistema foi projetado para ser **matematicamente impossível de fraudar ou violar a privacidade do eleitor**, mesmo por quem tem acesso total ao servidor.

---

## 🔐 As 6 Garantias de Segurança

### 1. Só alunos da USP votam — Google OAuth `@usp.br`

**O problema**: Qualquer pessoa poderia acessar a urna e votar se não houvesse controle de identidade.

**A solução**: O login é feito exclusivamente via Google com contas `@usp.br`. O sistema valida o domínio do email em **duas camadas**:
- **Camada 1 (frontend)**: O Google só mostra contas `@usp.br` na tela de login (parâmetro `hd=usp.br`)
- **Camada 2 (backend)**: Mesmo que alguém burle a tela do Google, o servidor **rejeita qualquer email** que não termine em `@usp.br`

**Por que Google OAuth?** Porque a USP já usa o Google Workspace. Todo aluno tem uma conta `@usp.br` gerenciada pela universidade. Não criamos um sistema de login próprio (que seria mais vulnerável) — delegamos a autenticação para a infraestrutura que a USP já confia.

### 2. Só alunos da EESC votam — Validação do Atestado do Júpiter

**O problema**: Uma conta `@usp.br` pode ser de qualquer campus (Poli, FEA, FFLCH...). Como garantir que apenas alunos da EESC votem?

**A solução**: O aluno fornece o **código de controle** do seu Atestado de Matrícula, emitido pelo sistema Júpiter da USP. O backend consulta o portal oficial da USP (`portalservicos.usp.br/iddigital`), extrai o PDF do atestado, e verifica se o curso pertence à **Escola de Engenharia de São Carlos** (código de unidade `97`).

**Por que não pedir o NUSP direto?** Porque qualquer pessoa pode inventar um NUSP. O atestado do Júpiter é um documento oficial que só o aluno matriculado consegue emitir, e o portal da USP garante a autenticidade. Nós **validamos a fonte** — não confiamos em input do usuário.

**Os cursos elegíveis são configuráveis** via arquivo `.env`, permitindo adaptar o sistema para qualquer unidade ou assembleia.

### 3. Ninguém vota duas vezes — HMAC-SHA256

**O problema**: Como impedir que o mesmo aluno vote mais de uma vez, sem armazenar dados pessoais?

**A solução**: Do atestado validado, o sistema extrai o **RG** do aluno. Este RG é imediatamente convertido em um **hash criptográfico irreversível** usando o algoritmo HMAC-SHA256 com uma chave secreta (`SALT_KEY`). Apenas este hash é armazenado no banco de dados.

```
RG: 13.560.200-9 + SALT_KEY → HMAC-SHA256 → "a1b2c3d4e5f6..." (64 caracteres)
```

Antes de registrar o voto, o sistema verifica: **esse hash já existe no banco?** Se sim, o voto é rejeitado.

**Por que HMAC-SHA256 e não SHA-256 puro?**
- SHA-256 puro é vulnerável a **rainbow tables**: como o espaço de RGs é finito (~500 milhões de combinações), um atacante poderia pré-computar o hash de todos os RGs e reverter os hashes no banco.
- HMAC adiciona uma **chave secreta** (`SALT_KEY`) que só existe no servidor. Sem essa chave, é **computacionalmente impossível** reverter ou pré-computar os hashes, mesmo com acesso total ao banco de dados.
- A comparação de hashes usa `hmac.compare_digest()` — uma função **timing-safe** que previne ataques de temporização (medir o tempo de comparação para inferir o hash correto).

**O RG bruto é descartado da memória RAM imediatamente após a geração do hash.** Ele nunca é salvo em disco, banco de dados, log, ou qualquer outro lugar persistente.

### 4. Anonimato absoluto — Duas tabelas sem relação

**O problema**: Se o sistema sabe que "o aluno X votou" e sabe que "o voto Y foi Sim", é possível cruzar os dados?

**A solução**: **Não.** O banco de dados tem duas tabelas completamente isoladas:

| Tabela 1 — `voter_hashes` | Tabela 2 — `votes` |
|---|---|
| Hash HMAC do RG | UUID aleatório |
| Timestamp de registro | Opção votada (Sim/Não/Nulo) |
| | Timestamp do voto |

**Não existe nenhuma chave estrangeira (foreign key) entre as tabelas.** Não existe nenhuma coluna que permita associar um hash a um voto. O UUID é gerado aleatoriamente (`uuid.uuid4()`) no momento do voto — ele não tem nenhuma relação matemática com o hash do RG.

**Mesmo quem tem acesso total ao servidor e ao banco de dados não consegue descobrir quem votou o quê.** A separação é estrutural, não uma questão de permissão — é matematicamente impossível fazer o cruzamento.

### 5. Cada voto é verificável — Recibo de Auditoria

**O problema**: Como o eleitor sabe que seu voto foi contado corretamente?

**A solução**: Após votar, o eleitor recebe um **UUID aleatório** como recibo. Ele pode usar esse UUID a qualquer momento para verificar no sistema que seu voto está registrado e qual opção foi computada. Se o voto sumisse ou fosse alterado, o eleitor teria prova.

**O UUID é anônimo** — ele não revela a identidade do eleitor. Qualquer pessoa pode consultar um UUID, mas não tem como saber a quem pertence.

### 6. Zero dados pessoais armazenados — Conformidade LGPD

**O problema**: A Lei Geral de Proteção de Dados (LGPD) exige consentimento explícito para armazenamento de dados pessoais. Um sistema de votação que guarda RGs e nomes estaria sujeito a vazamentos e obrigações legais.

**A solução**: O sistema **não armazena nenhum dado pessoal identificável**. Em nenhum momento. O fluxo é:

1. O RG é extraído do PDF do Júpiter → **variável temporária em memória**
2. O HMAC-SHA256 é gerado → **hash irreversível armazenado**
3. A variável do RG é destruída → **garbage collected pelo Python**

O banco de dados contém apenas: hashes (irreversíveis), UUIDs (aleatórios), votos (Sim/Não/Nulo), e timestamps. **Nenhum desses campos identifica uma pessoa.**

> ### ⚠️ Disclaimer — Descarte Imediato de Dados Pessoais
>
> Este sistema foi projetado desde o primeiro dia com uma premissa inegociável: **nenhum dado pessoal é retido, em nenhum momento, em nenhum formato**.
>
> O RG, o nome, e o código de controle do Júpiter existem exclusivamente como **variáveis temporárias na memória RAM** do servidor durante os poucos segundos necessários para gerar o hash de deduplicação. Imediatamente após esse processamento, essas variáveis são destruídas pelo runtime do Python. Elas **não são gravadas em disco, não são salvas em logs, não são enviadas a terceiros, e não são armazenadas em cache**.
>
> Essa decisão não é apenas técnica — é uma posição explícita dos desenvolvedores. **Nenhum estudante quer ou deveria ter a responsabilidade de custodiar dados sensíveis de colegas.** Manter RGs, nomes ou NUSPs em um banco de dados criaria um risco real de vazamento, expondo tanto os eleitores quanto os desenvolvedores a consequências legais sob a LGPD (Lei nº 13.709/2018). A decisão de descartar tudo imediatamente elimina esse risco por completo: **não é possível vazar o que não existe.**
>
> O único dado persistido que tem relação indireta com a identidade do eleitor é o hash HMAC-SHA256 — que é **irreversível por design**. Sem a chave secreta (`SALT_KEY`), que nunca é exposta no código-fonte ou no repositório, é computacionalmente inviável reverter o hash para o RG original. E mesmo com a chave, o hash sozinho não permite identificar *como* a pessoa votou, pois não existe nenhuma ligação entre a tabela de hashes e a tabela de votos.
>
> **Em resumo**: este sistema prova que você votou, impede que você vote duas vezes, mas **torna matematicamente impossível** — para qualquer pessoa, incluindo os administradores do servidor — descobrir *o que* você votou.

---

## 📐 Arquitetura do Sistema

```
Aluno → Google OAuth (@usp.br) → Código de Controle do Júpiter
    → Playwright consulta portal USP → PDF do atestado (em memória)
    → pdfplumber extrai RG + Curso → Verifica elegibilidade (EESC?)
    → HMAC-SHA256(RG + SALT_KEY) → Checa hash no banco → Voto duplicado?
    → Registra hash (Tabela 1) → Registra voto anônimo + UUID (Tabela 2)
    → Exibe recibo com UUID
```

### Stack Tecnológica

| Componente | Tecnologia | Justificativa |
|---|---|---|
| **Backend** | Python + FastAPI | Desenvolvimento rápido, validação automática, async nativo |
| **Scraper** | Playwright | Único framework que consegue renderizar o SPA Vue.js do portal USP e lidar com Cloudflare Turnstile |
| **Extração de PDF** | pdfplumber | Biblioteca madura para extração de texto de PDFs — a USP serve o atestado como PDF, não HTML |
| **Criptografia** | HMAC-SHA256 (stdlib) | Algoritmo padrão da indústria, incluído na biblioteca padrão do Python, sem dependências externas |
| **Banco de dados** | SQLite | Arquivo local, zero configuração, suficiente para milhares de votantes. Não exposto na rede |
| **Frontend** | Jinja2 + HTML/CSS/JS | Templates server-side, sem framework JavaScript — simplicidade e velocidade |
| **Deploy** | Docker + Cloudflare Tunnel | Container isolado, sem portas expostas no roteador, WAF da Cloudflare protege contra DDoS |

---

## 🚀 Setup Rápido

```bash
# 1. Clonar
git clone https://github.com/SEU_USER/Vote-Core.git
cd Vote-Core

# 2. Ambiente virtual
python3 -m venv .venv
source .venv/bin/activate

# 3. Instalar dependências
pip install -r requirements.txt
playwright install chromium --with-deps

# 4. Configurar variáveis de ambiente
cp .env.example .env
# Editar .env com suas credenciais (SECRET_KEY, SALT_KEY, etc.)

# 5. Rodar
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

## ⚙️ Variáveis de Controle da Votação

Todas as variáveis são configuráveis via `.env` — **nenhuma mudança de código é necessária** para adaptar o sistema a diferentes votações:

| Variável | Descrição | Exemplo |
|---|---|---|
| `VOTE_TITLE` | Título da votação | `Assembleia EESC-USP — Greve 2026` |
| `VOTE_QUESTION` | Pergunta ao eleitor | `Você é a favor da greve?` |
| `VOTE_OPTIONS` | Opções de voto (vírgula) | `Sim,Não,Nulo` |
| `ELIGIBLE_UNIT_CODES` | Códigos de unidade USP | `97` (EESC) |
| `ELIGIBLE_KEYWORDS` | Keywords de elegibilidade | `Escola de Engenharia de São Carlos\|EESC` |
| `SALT_KEY` | Chave HMAC (**secreta**) | Hex de 64 caracteres |
| `SECRET_KEY` | Chave de sessão (**secreta**) | Hex de 32 caracteres |

> ⚠️ **`SALT_KEY` e `SECRET_KEY` nunca devem ser commitadas no Git.** O arquivo `.env` está no `.gitignore`. Compartilhe essas chaves apenas entre os desenvolvedores autorizados, por canal seguro.

## 🐳 Docker

```bash
docker compose up --build
```

## 📁 Estrutura do Projeto

```
Vote-Core/
├── app/
│   ├── main.py          # Rotas FastAPI e orquestração do fluxo
│   ├── config.py        # Configurações via .env (pydantic-settings)
│   ├── auth.py          # Google OAuth 2.0 + filtro @usp.br
│   ├── scraper.py       # Playwright → Portal USP → PDF → Extração
│   ├── crypto.py        # HMAC-SHA256 — deduplicação irreversível
│   ├── database.py      # SQLite async — CRUD para as duas tabelas
│   ├── models.py        # Definição das tabelas (zero foreign keys)
│   └── templates/       # Frontend Jinja2 (dark theme)
├── static/              # CSS + JavaScript
├── requirements.txt     # Dependências Python
├── .env.example         # Template de variáveis de ambiente
├── .gitignore           # Protege .env, banco, e cache
└── plan.md              # Blueprint original do projeto
```

## 👥 Time

- **Alex** — Core & Validação (scraper, cripto, backend)
- **Paiva** — Banco de Dados & Infraestrutura

## 📜 Licença

Uso interno EESC-USP. Código aberto para auditoria.

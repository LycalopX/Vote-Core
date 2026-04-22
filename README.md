# 🗳️ Urna Eletrônica Zero-Knowledge — EESC-USP

Sistema de votação eletrônica **anônimo**, **seguro** e **auditável** para assembleias da EESC-USP.

> **Nota de transparência**: Este sistema foi desenvolvido com auxílio de inteligência artificial como ferramenta de pair-programming. Todo o código gerado foi revisado, auditado e validado manualmente pelos desenvolvedores. A IA foi utilizada para acelerar a implementação, mas **todas as decisões de arquitetura de segurança foram tomadas e validadas por humanos**. O código-fonte é aberto e auditável por qualquer pessoa.

---

## Por que uma urna eletrônica? E por que confiar nela?

A votação presencial em assembleia tem dois problemas conhecidos: (1) quem não pode comparecer não vota, e (2) a contagem manual é suscetível a erros e contestações. Uma urna eletrônica resolve ambos — mas introduz uma preocupação legítima: **como garantir que o sistema é honesto?**

Este documento explica, decisão por decisão, como o sistema foi projetado para ser **matematicamente impossível de fraudar ou violar a privacidade do eleitor**, mesmo por quem tem acesso total ao servidor.

Para detalhes técnicos aprofundados (schema do banco, fluxo HTTP, vetores de ataque, decisões de design), veja [`ARCHITECTURE.md`](ARCHITECTURE.md).

---

## 🔐 As 7 Garantias de Segurança

### 1. Só alunos da USP votam — Google OAuth `@usp.br`

**O problema**: Qualquer pessoa poderia acessar a urna e votar se não houvesse controle de identidade.

**A solução**: O login é feito exclusivamente via Google com contas `@usp.br`. O sistema valida o domínio do email em **duas camadas**:
- **Camada 1 (frontend)**: O Google só mostra contas `@usp.br` na tela de login (parâmetro `hd=usp.br`)
- **Camada 2 (backend)**: Mesmo que alguém burle a tela do Google, o servidor **rejeita qualquer email** que não termine em `@usp.br`

**Por que Google OAuth?** Porque a USP já usa o Google Workspace. Todo aluno tem uma conta `@usp.br` gerenciada pela universidade. Não criamos um sistema de login próprio (que seria mais vulnerável) — delegamos a autenticação para a infraestrutura que a USP já confia.

### 2. Só alunos da EESC votam — Validação do Atestado do Júpiter

**O problema**: Uma conta `@usp.br` pode ser de qualquer campus (Poli, FEA, FFLCH...). Como garantir que apenas alunos da EESC votem?

**A solução**: O aluno fornece o **código de controle** do seu Atestado de Matrícula, emitido pelo sistema Júpiter da USP. O backend chama diretamente a **API REST** do portal oficial da USP (`portalservicos.usp.br/iddigital`), baixa o PDF do atestado em memória, e verifica se o curso pertence à **Escola de Engenharia de São Carlos** (código de unidade `97`).

**Por que não pedir o NUSP direto?** Porque qualquer pessoa pode inventar um NUSP. O atestado do Júpiter é um documento oficial que só o aluno matriculado consegue emitir, e o portal da USP garante a autenticidade. Nós **validamos a fonte** — não confiamos em input do usuário.

**Os cursos elegíveis são configuráveis** via arquivo `.env`, permitindo adaptar o sistema para qualquer unidade ou assembleia.

### 3. Ninguém vota duas vezes — HMAC-SHA256

**O problema**: Como impedir que o mesmo aluno vote mais de uma vez, sem armazenar dados pessoais?

**A solução**: Do atestado validado, o sistema extrai o **Número USP (NUSP)** do aluno. Este NUSP é imediatamente convertido em um **hash criptográfico irreversível** usando o algoritmo HMAC-SHA256 com uma chave secreta (`SALT_KEY`). Apenas este hash é armazenado no banco de dados.

```
NUSP: 12345678 + SALT_KEY → HMAC-SHA256 → "a1b2c3d4e5f6..." (64 caracteres)
```

Antes de registrar o voto, o sistema verifica: **esse hash já existe no banco?** Se sim, o voto é rejeitado.

**Por que HMAC-SHA256 e não SHA-256 puro?**
- SHA-256 puro é vulnerável a **rainbow tables**: como o espaço de NUSPs é finito (~10 milhões de combinações para 7–8 dígitos), um atacante poderia pré-computar o hash de todos os NUSPs e reverter os hashes no banco.
- HMAC adiciona uma **chave secreta** (`SALT_KEY`) que só existe no servidor. Sem essa chave, é **computacionalmente impossível** reverter ou pré-computar os hashes, mesmo com acesso total ao banco de dados.
- A comparação de hashes usa `hmac.compare_digest()` — uma função **timing-safe** que previne ataques de temporização (medir o tempo de comparação para inferir o hash correto).

**O NUSP bruto é descartado da memória RAM imediatamente após a geração do hash.** Ele nunca é salvo em disco, banco de dados, log, ou qualquer outro lugar persistente.

### 4. Anonimato absoluto — Três tabelas sem relação

**O problema**: Se o sistema sabe que "o aluno X votou" e sabe que "o voto Y foi Sim", é possível cruzar os dados?

**A solução**: **Não.** O banco de dados tem três tabelas completamente isoladas:

| Tabela 1 — `voter_hashes` | Tabela 2 — `votes` | Tabela 3 — `public_votes` |
|---|---|---|
| Hash HMAC do NUSP | UUID aleatório | UUID aleatório |
| Timestamp de registro | audit_id (HMAC de auditoria) | Opção votada |
| | Opção votada | |
| **FECHADA** — nunca exposta | **RESTRITA** — nunca exposta | **PÚBLICA** — exibida em /results |

**Não existe nenhuma chave estrangeira (foreign key) entre as tabelas.** Não existe nenhuma coluna que permita associar um hash a um voto. O UUID é gerado aleatoriamente (`uuid.uuid4()`) no momento do voto — ele não tem nenhuma relação matemática com o hash do NUSP.

**O timestamp (`created_at`) existe APENAS na Tabela 1.** A Tabela 2 não tem timestamp — se ambas tivessem, um atacante poderia correlacionar "hash inserido às 14:32:05" com "voto inserido às 14:32:05".

**A Tabela 3 (`public_votes`) é uma cópia física da Tabela 2, mas sem o campo `audit_id`.** A rota `/results` lê exclusivamente da Tabela 3. Mesmo em caso de SQL injection na rota pública, não existe `audit_id` para extrair — a separação é física, não lógica. Defesa em profundidade.

**Todas as tabelas usam `WITHOUT ROWID`.** O SQLite atribui por padrão um `rowid` implícito auto-incrementado a cada registro — se duas tabelas são preenchidas na mesma transação, o `rowid 42` na Tabela 1 e o `rowid 42` na Tabela 2 correspondem ao mesmo eleitor. `WITHOUT ROWID` elimina esse rowid. Cada tabela usa sua **chave natural** como PK direta (`hash` na T1, `uuid` nas T2/T3), armazenada em B-Tree clustered pela chave — a posição física é determinada pelo valor (aleatório), não pela ordem de inserção.

**Mesmo quem tem acesso total ao servidor e ao banco de dados não consegue descobrir quem votou o quê.** A separação é estrutural, não uma questão de permissão — é matematicamente impossível fazer o cruzamento.

### 5. Cada voto é verificável — Dupla Auditoria

**O problema**: Como o eleitor sabe que seu voto foi contado corretamente?

**A solução**: O sistema oferece **dois mecanismos** de verificação:

1. **Recibo UUID**: Após votar, o eleitor recebe um UUID aleatório. Ele pode usar esse UUID a qualquer momento para localizar seu voto na tabela pública de transparência.

2. **Auditoria por senha**: Na hora de votar, o eleitor cria uma **senha pessoal de auditoria** (mínimo 4 caracteres). Se perder o UUID, pode ir a `/audit`, digitar seu NUSP + senha, e o sistema recalcula o hash `HMAC(NUSP + senha, SALT_2)` para localizar o voto. **A senha nunca é armazenada** — apenas o hash irreversível.

**Dois SALTs independentes**: `SALT_KEY` (deduplicação) e `SALT_2` (auditoria) são chaves separadas. Comprometer uma não compromete a outra.

### 6. Transparência total — Tabela pública de votos

**O problema**: Como garantir que nenhum voto foi alterado ou removido após o registro?

**A solução**: A rota `/results` exibe **todos os votos individuais** (UUID + opção), lidos da Tabela 3 (`public_votes`). Qualquer eleitor pode:
- Ver a lista completa de todos os votos registrados
- Localizar seu próprio UUID na tabela
- Confirmar que seu voto está correto
- Verificar que a contagem bate com os votos listados

Na rota `/audit`, após verificar seu voto com NUSP + senha, o eleitor vê a mesma tabela completa com **seu voto destacado em azul**.

### 7. Zero dados pessoais armazenados — Conformidade LGPD

**O problema**: A Lei Geral de Proteção de Dados (LGPD) exige consentimento explícito para armazenamento de dados pessoais. Um sistema de votação que guarda NUSPs e nomes estaria sujeito a vazamentos e obrigações legais.

**A solução**: O sistema **não armazena nenhum dado pessoal identificável**. Em nenhum momento. O fluxo é:

1. O NUSP é extraído do PDF do Júpiter → **variável temporária em memória**
2. O HMAC-SHA256 é gerado → **hash irreversível armazenado**
3. A variável do NUSP é destruída → **garbage collected pelo Python**

O banco de dados contém apenas: hashes (irreversíveis), UUIDs (aleatórios), votos (Sim/Não/Nulo), e timestamps. **Nenhum desses campos identifica uma pessoa.**

> ### ⚠️ Disclaimer — Descarte Imediato de Dados Pessoais
>
> Este sistema foi projetado desde o primeiro dia com uma premissa inegociável: **nenhum dado pessoal é retido, em nenhum momento, em nenhum formato**.
>
> O NUSP, o nome, e o código de controle do Júpiter existem exclusivamente como **variáveis temporárias na memória RAM** do servidor durante os poucos segundos necessários para gerar os hashes. Imediatamente após esse processamento, essas variáveis são destruídas pelo runtime do Python. Elas **não são gravadas em disco, não são salvas em logs, não são enviadas a terceiros, e não são armazenadas em cache**.
>
> Essa decisão não é apenas técnica — é uma posição explícita dos desenvolvedores. **Nenhum estudante quer ou deveria ter a responsabilidade de custodiar dados sensíveis de colegas.** Manter NUSPs ou nomes em um banco de dados criaria um risco real de vazamento, expondo tanto os eleitores quanto os desenvolvedores a consequências legais sob a LGPD (Lei nº 13.709/2018). A decisão de descartar tudo imediatamente elimina esse risco por completo: **não é possível vazar o que não existe.**
>
> O único dado persistido que tem relação indireta com a identidade do eleitor é o hash HMAC-SHA256 — que é **irreversível por design**. Sem a chave secreta (`SALT_KEY`), que nunca é exposta no código-fonte ou no repositório, é computacionalmente inviável reverter o hash para o NUSP original. E mesmo com a chave, o hash sozinho não permite identificar *como* a pessoa votou, pois não existe nenhuma ligação entre a tabela de hashes e a tabela de votos.
>
> **Em resumo**: este sistema prova que você votou, impede que você vote duas vezes, mas **torna matematicamente impossível** — para qualquer pessoa, incluindo os administradores do servidor — descobrir *o que* você votou.

---

## 📐 Arquitetura do Sistema

```
Aluno → Google OAuth (@usp.br) → Código de Controle do Júpiter
    → API REST do portal USP → PDF do atestado (em memória, ~200ms)
    → pdfplumber extrai NUSP + Curso → Verifica elegibilidade (EESC?)
    → HMAC-SHA256(NUSP, SALT_KEY) → Checa hash no banco → Voto duplicado?
    → Eleitor cria senha de auditoria
    → Registra hash (Tabela 1)
    → Gera audit_id = HMAC(NUSP + "\x00" + senha, SALT_2)
    → Registra voto (Tabela 2: uuid + audit_id + vote)
    → Registra espelho público (Tabela 3: uuid + vote)
    → Exibe recibo com UUID
```

### Stack Tecnológica

| Componente | Tecnologia | Justificativa |
|---|---|---|
| **Backend** | Python 3.13 + FastAPI | Desenvolvimento rápido, validação automática, async nativo |
| **Scraper** | **httpx** (REST direto) | API REST do portal IDDigital da USP retorna o PDF diretamente via HTTP — sem necessidade de browser. ~200ms por validação vs ~30s com Playwright |
| **Extração de PDF** | pdfplumber | Biblioteca madura para extração de texto de PDFs — a USP serve o atestado como PDF, não HTML |
| **Criptografia** | HMAC-SHA256 (stdlib) | Algoritmo padrão da indústria, incluído na biblioteca padrão do Python, sem dependências externas |
| **Banco de dados** | SQLite (WAL mode) | Arquivo local, zero configuração, leitores concorrentes via WAL. Suficiente para ~200-500 eleitores |
| **Frontend** | Jinja2 + HTML/CSS/JS | Templates server-side, sem framework JavaScript — simplicidade e velocidade |
| **Deploy** | PM2 + Cloudflare Tunnel | Process manager resiliente, sem portas expostas no roteador, WAF da Cloudflare protege contra DDoS |

---

## 🚀 Setup Rápido

```bash
# 1. Clonar
git clone https://github.com/LycalopX/Vote-Core.git
cd Vote-Core

# 2. Ambiente virtual
python3 -m venv .venv
source .venv/bin/activate

# 3. Instalar dependências
pip install -r requirements.txt
# Nota: Playwright foi removido. httpx é a única dependência de rede.

# 4. Configurar variáveis de ambiente
cp .env.example .env
# Editar .env com suas credenciais:
#   SECRET_KEY  → chave para assinar cookies de sessão
#   SALT_KEY    → chave HMAC para hash de deduplicação (NUSP)
#   SALT_2      → chave HMAC para hash de auditoria (NUSP + senha)
#   GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET → OAuth 2.0

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
| `SALT_KEY` | Chave HMAC deduplicação (**secreta**) | Token aleatório |
| `SALT_2` | Chave HMAC auditoria (**secreta**) | Token aleatório (diferente de SALT_KEY) |
| `SECRET_KEY` | Chave de sessão (**secreta**) | Token aleatório de 32+ chars |

> ⚠️ **`SALT_KEY`, `SALT_2` e `SECRET_KEY` nunca devem ser commitadas no Git.** O arquivo `.env` está no `.gitignore`. Compartilhe essas chaves apenas entre os desenvolvedores autorizados, por canal seguro.

## 📁 Estrutura do Projeto

```
Vote-Core/
├── app/
│   ├── main.py          # Rotas FastAPI, rate limiter (IP + NUSP), orquestração
│   ├── config.py        # Configurações via .env (pydantic-settings)
│   ├── auth.py          # Google OAuth 2.0 + filtro @usp.br
│   ├── scraper.py       # httpx → API REST do portal USP → PDF → Extração
│   ├── crypto.py        # HMAC-SHA256 — deduplication + auditoria
│   ├── database.py      # SQLite async — CRUD para as 3 tabelas
│   ├── models.py        # Definição das 3 tabelas (zero foreign keys)
│   └── templates/       # Frontend Jinja2 (dark theme, glassmorphism)
│       ├── base.html     # Layout base
│       ├── login.html    # Login OAuth
│       ├── validate.html # Validação de matrícula
│       ├── vote.html     # Tela de votação + senha de auditoria
│       ├── receipt.html  # Recibo com UUID
│       ├── audit.html    # Auditoria pessoal NUSP + senha
│       ├── results.html  # Resultados + tabela de transparência
│       └── error.html    # Página de erro
├── static/
│   ├── style.css        # Dark theme com glassmorphism
│   └── js/app.js        # JavaScript minimal
├── tests/
│   ├── test_core.py     # 50 testes unitários (crypto, database, schema)
│   └── test_stress.py   # 28 testes de stress (concorrência, race conditions)
├── scripts/
│   └── backup.sh        # Backup ACID-safe via sqlite3.backup()
├── ARCHITECTURE.md      # Documentação técnica completa
├── requirements.txt     # Dependências Python
├── pytest.ini           # Configuração pytest-asyncio (asyncio_mode=auto)
├── .env.example         # Template de variáveis de ambiente
├── ecosystem.config.js  # Configuração PM2
└── .gitignore           # Protege .env, banco, cache, e logs
```

## 🧪 Testes

```bash
# Rodar a suite completa (78 testes)
pytest tests/ -v

# Cobertura:
#   test_core.py    — 50 testes unitários: crypto (15), database (9), schema (7), scraper (14), config (2), pragmas (3)
#   test_stress.py  — 28 testes de stress: race conditions, concorrência, rate limiter, semáforo, memória
```

## 👥 Time

- **Alex (LycalopX)** — Core & Validação (scraper, cripto, backend, frontend)
- **Eduardo Paiva** — Banco de Dados, Infraestrutura & Revisão de Segurança
- **Gabriel Yamauti** — Auditoria de Código & Documentação

## 📜 Licença

Uso interno EESC-USP. Código aberto para auditoria.

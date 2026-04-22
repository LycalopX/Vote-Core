# Vote-Core — Documentação Técnica Completa

> **Sistema de votação eletrônica anônima e auditável para a EESC-USP.**
> Versão: 2.2 (Hardening: Security Headers + Rate Limiting + Elegibilidade por Curso)
> Data: 21 de abril de 2026
> Autores: Alex (LycalopX), Eduardo Paiva (EduardoAPaiva), com assistência de IA

---

## Sumário

1. [Visão Geral](#1-visão-geral)
2. [Arquitetura de Segurança](#2-arquitetura-de-segurança)
3. [Esquema do Banco de Dados (3 Tabelas)](#3-esquema-do-banco-de-dados-3-tabelas)
4. [Fluxo Completo do Eleitor](#4-fluxo-completo-do-eleitor)
5. [Módulos do Sistema](#5-módulos-do-sistema)
6. [Decisões de Design e Justificativas](#6-decisões-de-design-e-justificativas)
7. [Vetores de Ataque e Mitigações](#7-vetores-de-ataque-e-mitigações)
8. [Configuração e Deploy](#8-configuração-e-deploy)
9. [Testes](#9-testes)
10. [Limitações Conhecidas](#10-limitações-conhecidas)
11. [Histórico de Bugs Corrigidos](#11-histórico-de-bugs-corrigidos)

---

## 1. Visão Geral

Vote-Core é um sistema web de votação eletrônica construído para assembleias da EESC-USP. O sistema garante:

- **Anonimato matemático**: Impossível correlacionar "quem votou" com "qual voto"
- **Deduplicação**: Cada aluno só vota uma vez (verificado via HMAC do NUSP)
- **Auditabilidade**: O eleitor pode verificar seu próprio voto a qualquer momento
- **Transparência**: Todos os votos são listados publicamente (uuid + voto)
- **Elegibilidade**: Apenas alunos da EESC com matrícula ativa podem votar

### Stack Tecnológica

| Componente | Tecnologia |
|---|---|
| Backend | Python 3.13 + FastAPI + Uvicorn |
| Banco de dados | SQLite + aiosqlite (modo WAL) |
| ORM | SQLAlchemy 2.0 (async) |
| Autenticação | Google OAuth 2.0 (restrito a `@usp.br`) |
| Scraper | Playwright (headless Chromium) |
| Proxy reverso | Cloudflare Tunnel (`cloudflared`) |
| Process manager | PM2 |
| Frontend | Jinja2 + CSS puro (dark theme, glassmorphism) |

---

## 2. Arquitetura de Segurança

### 2.1 Princípio Zero-Knowledge

O servidor **nunca armazena** dados pessoais identificáveis. O NUSP do eleitor existe apenas em memória durante a sessão HTTP e é destruído imediatamente após o processamento. O que é armazenado:

| Dado armazenado | O que é | Reversível? |
|---|---|---|
| `HMAC(NUSP, SALT_KEY)` | Hash de deduplicação | ❌ Impossível sem SALT_KEY |
| `HMAC(NUSP+senha, SALT_2)` | Hash de auditoria pessoal | ❌ Impossível sem SALT_2 |
| UUID v4 | Recibo aleatório | Não contém informação pessoal |
| "Sim"/"Não"/"Nulo" | O voto em si | Não vinculado a nenhum identificador |

### 2.2 Separação de Chaves (Dual-Salt)

O sistema usa **duas chaves HMAC independentes**:

- **`SALT_KEY`**: Usada exclusivamente para gerar o hash de deduplicação (`voter_hashes`)
- **`SALT_2`**: Usada exclusivamente para gerar o hash de auditoria (`audit_id` na tabela `votes`)

**Por que duas chaves?** Se um atacante comprometer `SALT_KEY` (podendo verificar quais NUSPs já votaram), ele ainda não consegue acessar `SALT_2` para descobrir qual voto corresponde a qual NUSP. O comprometimento de uma chave não compromete a outra.

### 2.3 Autenticação em Duas Camadas

1. **Google OAuth 2.0** com restrição hard-coded ao domínio `@usp.br`
   - Parâmetro `hd=usp.br` no redirect (filtro visual na tela do Google)
   - Validação server-side no callback: `email.endswith("@usp.br")` — rejeita qualquer email fora do domínio mesmo que o parâmetro `hd` seja removido
2. **Validação de matrícula** via scraper do sistema Júpiter da USP
   - O aluno fornece o código de controle do atestado de matrícula
   - O scraper acessa o Júpiter, baixa o PDF, e extrai: NUSP, nome, curso, código de curso, unidade
   - Elegibilidade verificada em cascata: `ELIGIBLE_COURSE_CODES` → `ELIGIBLE_UNIT_CODES` → `ELIGIBLE_KEYWORDS`
   - Nenhum dado pessoal (RG, nome completo) é extraído ou armazenado

---

## 3. Esquema do Banco de Dados (3 Tabelas)

### Diagrama

```
┌──────────────────────────────────────────┐
│  Tabela 1: voter_hashes                  │
│  (FECHADA — nunca exposta)               │
│  WITHOUT ROWID                           │
│──────────────────────────────────────────│
│  hash       VARCHAR(64) PRIMARY KEY      │  ← HMAC(NUSP, SALT_KEY)
│  created_at DATETIME                     │  ← ÚNICO timestamp do sistema
│  (sem rowid implícito!)                  │
└──────────────────────────────────────────┘
         ⊘ ZERO relação ⊘
┌──────────────────────────────────────────┐
│  Tabela 2: votes                         │
│  (RESTRITA — nunca exposta)              │
│  WITHOUT ROWID                           │
│──────────────────────────────────────────│
│  uuid       VARCHAR(36) PRIMARY KEY      │  ← UUID v4 aleatório
│  audit_id   VARCHAR(64) UNIQUE           │  ← HMAC(NUSP+senha, SALT_2)
│  vote       VARCHAR(10)                  │  ← "Sim" | "Não" | "Nulo"
│  (sem created_at! sem rowid!)            │
└──────────────────────────────────────────┘
         ⊘ ZERO relação ⊘
┌──────────────────────────────────────────┐
│  Tabela 3: public_votes                  │
│  (PÚBLICA — exposta em /results          │
│   e /audit após verificação)             │
│  WITHOUT ROWID                           │
│──────────────────────────────────────────│
│  uuid       VARCHAR(36) PRIMARY KEY      │  ← mesmo UUID da Tabela 2
│  vote       VARCHAR(10)                  │  ← "Sim" | "Não" | "Nulo"
│  (sem audit_id! sem timestamp!           │
│   sem rowid!)                            │
└──────────────────────────────────────────┘
```

### 3.1 Por que 3 tabelas e não 2?

A Tabela 3 (`public_votes`) é uma cópia deliberada da Tabela 2, mas **sem o campo `audit_id`**. Isso é **defesa em profundidade**:

- A rota `/results` lê exclusivamente da Tabela 3
- Mesmo que alguém injete SQL na rota pública, não há como extrair `audit_id` — o campo simplesmente não existe nessa tabela
- Não depende de "o backend filtra o campo" — a separação é física, no schema

### 3.2 Por que NÃO há `created_at` na Tabela 2?

Se ambas as tabelas tivessem timestamp, um atacante com acesso ao banco poderia correlacionar:
- "Um hash foi inserido na Tabela 1 às 14:32:05"
- "Um voto foi inserido na Tabela 2 às 14:32:05"
- → "Esse hash corresponde a esse voto"

Removendo o timestamp da Tabela 2, essa correlação é impossível.

### 3.3 Por que NÃO há Foreign Key entre as tabelas?

Foreign keys criariam uma relação formal entre "quem votou" e "qual voto". Sem FKs, as tabelas são **matematicamente independentes** — não há join possível.

### 3.4 Por que WITHOUT ROWID?

O SQLite atribui por padrão um **`rowid` implícito auto-incrementado** a cada registro — um contador inteiro sequencial que reflete a ordem de inserção. Esse rowid é o vetor de ataque mais sutil contra o anonimato:

**O ataque**: Se duas tabelas são preenchidas na mesma transação (registra hash → insere voto), o `rowid 42` na Tabela 1 e o `rowid 42` na Tabela 2 correspondem ao **mesmo eleitor** — destruindo completamente o anonimato.

Este vetor foi identificado pelo Eduardo Paiva (Ref: `temp/reference2.md`, linhas 556-570):
> *"Se os dados estão sendo salvos sequencialmente, os mesmos índices representam o mesmo voto"*

**A solução**: `WITHOUT ROWID` elimina o rowid implícito. Cada tabela usa sua **chave natural** como PK direta:

| Tabela | PK Natural | Formato |
|---|---|---|
| `voter_hashes` | `hash` | HMAC-SHA256 hex (64 chars) |
| `votes` | `uuid` | UUID v4 aleatório (36 chars) |
| `public_votes` | `uuid` | UUID v4 aleatório (36 chars) |

Os dados são armazenados em **B-Tree clustered** pela PK natural. A posição física de um registro na B-Tree é determinada pelo valor da chave (hash ou UUID), não pela ordem de inserção. Como UUIDs são aleatórios e HMACs são pseudo-aleatórios, a posição física de cada registro é efetivamente aleatória.

**Benefícios adicionais**:
- **Performance**: Consultas por PK natural (WHERE hash=? / WHERE uuid=?) são O(log n) diretas no B-Tree, sem indireção via rowid.
- **Espaço**: Economiza ~8 bytes por registro (tamanho do rowid) — marginal, mas limpo.
- **Semântica**: As tabelas não possuem um campo que "não deveria existir" — a ausência do rowid é uma garantia estrutural, não uma questão de "não usar".

Ref: [SQLite WITHOUT ROWID](https://www.sqlite.org/withoutrowid.html)

### 3.5 SQLite WAL Mode e Pragmas

```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA cache_size=-64000;
PRAGMA busy_timeout=15000;
PRAGMA wal_autocheckpoint=1000;
```

- **WAL (Write-Ahead Logging)**: Permite múltiplos leitores simultâneos enquanto um writer opera. Crucial para a assembleia onde dezenas de pessoas acessam `/results` enquanto outros votam.
- **synchronous=NORMAL**: Balanço entre performance e durabilidade. Em caso de queda de energia, no máximo a última transação pode ser perdida (aceitável — o eleitor veria um erro e poderia tentar novamente).
- **cache_size=-64000**: 64MB de cache em memória para reduzir I/O.
- **busy_timeout=15000**: Se o banco estiver bloqueado por outra escrita, a conexão espera até 15 segundos antes de retornar erro `database is locked`. Previne falhas durante picos de votação simultânea.
- **wal_autocheckpoint=1000**: O WAL faz checkpoint automático a cada 1000 páginas (~4MB), evitando crescimento descontrolado do arquivo `-wal`.

---

## 4. Fluxo Completo do Eleitor

```
Etapa 1: Login OAuth
    Eleitor acessa vote.consultoriobw.com.br
    → Clica em "Entrar com Google"
    → Autentica com conta @usp.br
    → Sessão criada com email + nome

Etapa 2: Validação de Matrícula
    Eleitor insere o código de controle do atestado do Júpiter
    → Scraper (Playwright) acessa o Júpiter via Chromium headless
    → Baixa o PDF do atestado
    → Extrai: NUSP, nome, curso, unidade
    → Valida unidade (código 97 = EESC)
    → Calcula voter_hash = HMAC(NUSP, SALT_KEY)
    → Verifica se voter_hash já existe na Tabela 1
    → Se duplicata: "Você já votou"
    → Se novo: salva NUSP e voter_hash na sessão temporária

Etapa 3: Votação
    Eleitor vê a pergunta + cria senha de auditoria (mín 4 chars)
    → JS valida: senhas iguais + mín 4 chars → habilita botões
    → Eleitor clica em Sim/Não/Nulo
    → Modal de confirmação aparece
    → Ao confirmar:
        1. Registra voter_hash na Tabela 1
        2. Gera UUID v4 aleatório
        3. Calcula audit_id = HMAC(NUSP + senha, SALT_2)
        4. Insere (uuid, audit_id, vote) na Tabela 2
        5. Insere (uuid, vote) na Tabela 3  ← inserção atômica
        6. Limpa NUSP e voter_hash da sessão
        7. Redireciona para receipt

Etapa 4: Recibo
    Eleitor vê o UUID do recibo + opção votada
    → Pode anotar o UUID para conferência futura
    → Link para /audit como fallback

Etapa 5: Auditoria (a qualquer momento)
    Eleitor acessa /audit
    → Digita NUSP + senha de auditoria
    → Backend recalcula HMAC(NUSP + senha, SALT_2)
    → Busca na Tabela 2 pelo audit_id
    → Exibe: "Seu voto: Sim" + tabela completa de todos os votos
    → O voto do eleitor aparece destacado em azul na tabela
```

---

## 5. Módulos do Sistema

### 5.1 `app/crypto.py` (76 linhas)

Três funções puras, sem side-effects:

| Função | Input | Output | Uso |
|---|---|---|---|
| `generate_voter_hash(nusp, salt_key)` | NUSP + SALT_KEY | HMAC-SHA256 hex (64 chars) | Deduplicação (Tabela 1) |
| `generate_audit_id(nusp, password, salt_2)` | NUSP + senha + SALT_2 | HMAC-SHA256 hex (64 chars) | Auditoria pessoal (Tabela 2) |
| `verify_voter_hash(nusp, salt_key, hash)` | NUSP + SALT_KEY + hash armazenado | bool | Verificação timing-safe |

`verify_voter_hash` usa `hmac.compare_digest()` para prevenir timing attacks.

### 5.2 `app/scraper.py` (~500 linhas)

Playwright-based scraper que:
1. Abre uma instância headless de Chromium
2. Navega ao sistema Júpiter da USP com o código de controle
3. Baixa o PDF do atestado de matrícula
4. Extrai dados via regex:
   - `NUSP_PATTERN`: `código\s+USP\s+(\d{7,8})` — dado primário
   - `CURSO_PATTERN`: nome do curso
   - `COURSE_CODE_PATTERN`: código numérico (ex: 97001)
   - `UNIDADE_PATTERN`: código da unidade (97 = EESC)
   - **RG não é extraído** — removido por ser dado pessoal desnecessário
5. Verificação de elegibilidade em cascata:
   - Se `ELIGIBLE_COURSE_CODES` definido → verifica código de curso (prioridade máxima)
   - Senão, se `ELIGIBLE_UNIT_CODES` definido → verifica unidade
   - Senão, se `ELIGIBLE_KEYWORDS` definido → busca texto no PDF
   - Se nenhum filtro definido → aceita todos

Concorrência limitada por `asyncio.Semaphore(4)` com `asyncio.timeout(60)`.

### 5.3 `app/auth.py` (155 linhas)

Google OAuth 2.0 com:
- `hd=usp.br` no redirect (filtro visual)
- Validação hard no callback (rejeita `!email.endswith("@usp.br")`)
- Modo dev sem OAuth quando `GOOGLE_CLIENT_ID` não está configurado

### 5.4 `app/database.py` (~305 linhas)

CRUD assíncrono para as 3 tabelas. Destaque:
- `insert_vote()` insere nas Tabelas 2 e 3 **na mesma transação**
- `get_vote_counts()` e `get_total_votes()` leem da **Tabela 3** (pública), não da Tabela 2
- `get_vote_by_audit_id()` lê da **Tabela 2** (restrita) — única função que toca o audit_id
- Pragmas configurados via listener `set_sqlite_pragma()`: WAL, busy_timeout=15s, autocheckpoint=1000

### 5.5 `app/main.py` (~550 linhas)

FastAPI app com as rotas:

| Rota | Método | Acesso | Descrição |
|---|---|---|---|
| `/` | GET | Público | Login page |
| `/auth/login` | GET | Público | Inicia fluxo OAuth |
| `/auth/callback` | GET | Público | Callback OAuth |
| `/validate` | GET/POST | Autenticado | Validação de matrícula |
| `/vote` | GET/POST | Autenticado + validado | Tela de votação |
| `/receipt` | GET | Pós-voto | Recibo com UUID |
| `/audit` | GET/POST | Público | Auditoria pessoal |
| `/results` | GET | Público | Resultados + tabela de transparência |
| `/health` | GET | Público | Health check |

---

## 6. Decisões de Design e Justificativas

### 6.1 NUSP em vez de RG

**Problema**: O sistema original usava RG para deduplicação. O RG do Eduardo Paiva terminava em dígito verificador `X`, o que causava um crash na normalização. Além disso, RGs têm formatos variados (pontos, traços) que complicam a normalização.

**Decisão**: Migrar para NUSP (Número USP), que é puramente numérico (7–8 dígitos) e não precisa de normalização.

**Justificativa**: O NUSP já era extraído pelo scraper (o `NUSP_PATTERN` existia no código desde a v1, mas nunca era usado). É o identificador oficial da USP e elimina 100% dos bugs de formatação.

### 6.2 Senha de Auditoria (não biometria, não email)

**Problema**: O eleitor precisa poder verificar seu voto depois, mas sem comprometer o anonimato.

**Alternativas consideradas**:
- **UUID do recibo**: Funciona, mas se o eleitor perder, não há como recuperar
- **Email como chave**: Permitiria ao admin correlacionar email → voto
- **Biometria**: Impraticável em votação web

**Decisão**: O eleitor cria uma senha arbitrária (mín 4 chars) na hora de votar. O `audit_id = HMAC(NUSP + senha, SALT_2)` é armazenado. Para auditar, o eleitor informa NUSP + senha e o sistema recalcula o hash.

**Justificativa**: A senha nunca é armazenada. O `audit_id` é irreversível. Mesmo que alguém saiba o NUSP de outra pessoa, precisaria da senha para correlacionar. E como `SALT_2 ≠ SALT_KEY`, comprometer a deduplicação não compromete a auditoria.

### 6.3 Tabela 3 (public_votes) como espelho físico

**Problema**: A rota `/results` precisa mostrar os votos individualmente (uuid + voto) para transparência, mas a Tabela 2 contém `audit_id`.

**Alternativa considerada**: Filtrar `audit_id` no backend (SELECT uuid, vote FROM votes).

**Decisão**: Criar uma terceira tabela física (`public_votes`) sem o campo `audit_id`.

**Justificativa**: Defesa em profundidade. Se houver um bug de SQL injection na rota pública, o atacante não consegue extrair `audit_id` porque o campo não existe na tabela que está sendo consultada. A separação é física, não lógica.

### 6.4 SQLite (não PostgreSQL)

**Justificativa**: O sistema roda em um Blackview MP60 (mini PC) com recursos limitados. SQLite com WAL mode é suficiente para a escala esperada (~200-500 eleitores em uma assembleia). Não há necessidade de servidor de banco separado. O arquivo único (`votes.db`) simplifica backup e transporte.

### 6.5 Playwright (não requests/BeautifulSoup)

**Justificativa**: O sistema Júpiter da USP usa JavaScript pesado e Cloudflare Turnstile. Requests puro não consegue renderizar a página. Playwright com Chromium headless é a única forma confiável de interagir com o Júpiter programaticamente.

---

## 7. Vetores de Ataque e Mitigações

| Vetor | Mitigação | Status |
|---|---|---|
| Voto duplo | HMAC(NUSP) + UNIQUE constraint na Tabela 1 | ✅ Implementado |
| Correlação temporal | `created_at` apenas na Tabela 1, ausente nas Tabelas 2 e 3 | ✅ Implementado |
| Correlação por FK | Zero Foreign Keys entre tabelas | ✅ Implementado |
| Correlação por rowid | WITHOUT ROWID em todas as tabelas — sem auto-increment implícito | ✅ Implementado |
| Exposição de audit_id | Tabela 3 sem audit_id + backend filtra na Tabela 2 | ✅ Implementado |
| Spam de scraper via /validate | Rate limit por IP: 5 tentativas / 2 min | ✅ Implementado |
| Brute-force de senha via /audit | Rate limit por NUSP: 5 tentativas / 1 min (chave `audit:NUSP`, não IP — evita bloqueio colateral via NAT no eduroam) | ✅ Implementado |
| Login não-USP | Hard check `@usp.br` no callback OAuth | ✅ Implementado |
| CSRF em /vote | SameSite=Lax + sessão requer OAuth + validação do PDF | ✅ Implementado |
| Timing attack no hash | `hmac.compare_digest()` (tempo constante) | ✅ Implementado |
| Clickjacking | `X-Frame-Options: DENY` + CSP `frame-ancestors 'none'` | ✅ Implementado |
| MIME-type sniffing | `X-Content-Type-Options: nosniff` | ✅ Implementado |
| Downgrade HTTP | `Strict-Transport-Security: max-age=31536000` (produção) | ✅ Implementado |
| XSS injection | CSP restritiva + Jinja2 auto-escape + `X-XSS-Protection` | ✅ Implementado |
| Aluno de outra unidade (ICMC) | Filtro por `ELIGIBLE_COURSE_CODES` (97001-97008) com prioridade sobre unidade | ✅ Implementado |
| Database locked em pico | `PRAGMA busy_timeout=15000` — espera 15s antes de falhar | ✅ Implementado |
| Vazamento de dados pessoais | RG removido do código; NUSP/nome não logados; PDF nunca salvo em disco | ✅ Implementado |
| Cache de CSS antigo | Cache-busting via `?v=N` no link do CSS | ✅ Implementado |
| Comprometimento de SALT_KEY | SALT_2 separada protege audit_ids | ✅ Implementado |

---

## 8. Configuração e Deploy

### 8.1 Variáveis de Ambiente (`.env`)

```env
# Google OAuth 2.0
GOOGLE_CLIENT_ID=xxx.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=xxx

# Segurança (TODAS obrigatórias)
SECRET_KEY=<token aleatório 32+ chars>      # Assina cookies de sessão
SALT_KEY=<chave secreta HMAC #1>             # Hash do NUSP (deduplicação)
SALT_2=<chave secreta HMAC #2>               # Hash do NUSP+senha (auditoria)

# Aplicação
BASE_URL=https://vote.consultoriobw.com.br
DATABASE_URL=sqlite+aiosqlite:///./votes.db
DEBUG=true

# Votação
VOTE_TITLE=Assembleia EESC-USP — Greve 2026
VOTE_QUESTION=Você é a favor da greve?
VOTE_OPTIONS=Sim,Não,Nulo
ELIGIBLE_UNIT_CODES=97
ELIGIBLE_COURSE_CODES=97001,97002,97003,97004,97005,97006,97007,97008
ELIGIBLE_KEYWORDS=Escola de Engenharia de São Carlos|EESC
```

### 8.2 Deploy Checklist

```bash
# 1. Limpar banco antigo (OBRIGATÓRIO antes da votação)
rm -f votes.db votes.db-wal votes.db-shm

# 2. Reiniciar o serviço
pm2 restart vote-core

# 3. Verificar startup
pm2 logs vote-core --lines 10
# Deve mostrar: "Urna Eletrônica EESC iniciada"
# Deve mostrar: "Google OAuth configurado"

# 4. Testar o fluxo completo
# - Acessar vote.consultoriobw.com.br
# - Logar com @usp.br
# - Inserir código de controle válido
# - Votar com senha de auditoria
# - Verificar em /audit
# - Conferir em /results
```

### 8.3 Backup Seguro do Banco

```bash
# NUNCA use cp/rsync para copiar votes.db (WAL pode corromper)
# Use o script de backup que usa sqlite3.backup() nativo:
./scripts/backup.sh

# Para cron durante a assembleia (a cada 15 min):
*/15 * * * * /home/lycalopx/repos/Vote-Core/scripts/backup.sh
```

O script verifica integridade do backup via `PRAGMA integrity_check` e mantém os últimos 20 backups.

### 8.4 Arquivos Ignorados pelo Git

```gitignore
.env                    # Credenciais reais
*.db / *.db-wal / *.db-shm  # Banco + WAL files
.venv/                  # Virtual environment
logs/                   # PM2 logs
backups/                # Backups do banco
temp/                   # Logs de chat sensíveis
gemini/                 # Conversas de desenvolvimento com IA
```

---

## 9. Testes

50 testes automatizados cobrindo todos os módulos de segurança:

### Crypto: `generate_voter_hash` (6 testes)
- Mesmo NUSP → mesmo hash (determinístico)
- NUSPs diferentes → hashes diferentes
- Hash tem exatamente 64 chars hexadecimais
- Salt diferente → hash diferente
- NUSP vazio → `ValueError`
- Espaços são removidos via `strip()`

### Crypto: `generate_audit_id` (6 testes)
- Mesmos inputs → mesmo output (determinístico)
- Senha diferente → audit_id diferente
- NUSP diferente → audit_id diferente
- SALT_2 diferente → audit_id diferente
- audit_id tem 64 chars hex
- audit_id ≠ voter_hash (mesmo NUSP, funções diferentes)

### Crypto: `verify_voter_hash` (3 testes)
- Verificação correta retorna `True`
- NUSP errado retorna `False`
- Hash adulterado retorna `False`

### Database (9 testes)
- Deduplicação: primeiro registro aceito
- Deduplicação: duplicata rejeitada
- Deduplicação: check after register
- insert_vote retorna UUID válido
- Contagem de votos por opção
- Total de votos incrementa
- Busca por UUID encontra/não encontra
- Busca por audit_id encontra/não encontra

### Schema — integridade do modelo (7 testes)
- Tabela `votes` NÃO tem campo `created_at`
- Tabela `votes` TEM campo `audit_id`
- Tabela `public_votes` NÃO tem campo `audit_id` (defesa em profundidade)
- Tabela `voter_hashes` usa WITHOUT ROWID (PK = `hash`, sem `id`)
- Tabela `votes` usa WITHOUT ROWID (PK = `uuid`, sem `id`)
- Tabela `public_votes` usa WITHOUT ROWID (PK = `uuid`, sem `id`)

### Scraper — estrutura de dados e elegibilidade (14 testes)
- `DocumentData` NÃO tem campo `rg` (dado pessoal removido)
- `DocumentData` TEM campo `course_code`
- `DocumentData` tem exatamente os 6 campos esperados
- `COURSE_CODE_PATTERN` extrai `97001` de texto real
- `COURSE_CODE_PATTERN` rejeita texto sem código
- Sem filtros → aceita todos
- Filtro de unidade: match (97) e no-match (55)
- Filtro de curso: match (97001) e no-match (97002)
- Curso tem prioridade sobre unidade (curso errado + unidade certa = rejeitado)
- Lista de cursos aceita se pelo menos um dá match
- Keyword match e no-match

### Config — propriedades computadas (2 testes)
- `eligible_course_codes_list` vazio → lista vazia
- `eligible_course_codes_list` com valores → lista correta

### SQLite — pragmas de segurança (3 testes)
- `busy_timeout >= 15000ms` (anti-database-locked)
- `journal_mode = wal`
- `wal_autocheckpoint = 1000`

```bash
# Executar testes
source .venv/bin/activate
pytest tests/ -v
# Resultado: 50 passed in 0.58s
```

---

## 10. Limitações Conhecidas

| Limitação | Impacto | Mitigação |
|---|---|---|
| Sem stress test em produção | Comportamento sob carga real desconhecido | Semaphore(4) + asyncio.timeout(60) + busy_timeout=15s |
| Sessão cookie não é `Secure` quando `DEBUG=true` | Cookie enviado via HTTP | `DEBUG=false` em produção → Secure=True |
| SQLite não escala para milhares simultâneos | ~50-100 escritas/s no WAL mode | Suficiente para assembleia EESC (~200-500 eleitores) |

### Limitações resolvidas nesta versão

| Era limitação | Resolução |
|---|---|
| Sem rate limit em `/audit` | ✅ 5 tentativas/60s por IP |
| Sem backup automático | ✅ `scripts/backup.sh` com `sqlite3.backup()` ACID-safe |
| NUSP_PATTERN nunca testado com PDF real | ✅ Testado e confirmado com PDF real (NUSP: 15436911) |
| Sem filtro por curso (ICMC podia votar) | ✅ `ELIGIBLE_COURSE_CODES` com prioridade sobre unidade |
| RG extraído sem necessidade | ✅ `RG_PATTERN` e campo `rg` removidos completamente |
| Sem security headers HTTP | ✅ 6 headers: CSP, HSTS, X-Frame-Options, etc. |

---

## 11. Histórico de Bugs Corrigidos

### Bug 1: RG com dígito `X` (CRÍTICO)
- **Sintoma**: Eduardo Paiva não conseguia votar
- **Causa**: `normalize_rg()` não tratava dígito verificador `X`
- **Fix**: Migração completa de RG → NUSP. `normalize_rg()` removido.

### Bug 2: `asyncio.wait_for(Semaphore)` (CRÍTICO)
- **Sintoma**: `TypeError` em runtime ao tentar limitar concorrência do scraper
- **Causa**: `asyncio.wait_for()` espera uma coroutine, não um Semaphore
- **Fix**: `async with asyncio.timeout(60): async with scraper_semaphore:`

### Bug 3: Correlação temporal entre tabelas
- **Sintoma**: Ambas as tabelas tinham `created_at`, permitindo correlação
- **Fix**: `created_at` removido da Tabela 2 (`votes`)

### Bug 4: Inputs de senha com `width: 90px`
- **Sintoma**: Caixas de senha minúsculas na tela de votação
- **Causa**: Inputs usavam classe `.code-input` (projetada para o código de controle)
- **Fix**: Classe separada `.password-input` com `width: 100%`

### Bug 5: Modal trava scroll no celular
- **Sintoma**: Botão "Sim" inacessível no mobile (modal cortado, sem scroll)
- **Causa**: `document.body.style.overflow = 'hidden'` no JS do modal
- **Fix**: Removido overflow:hidden, adicionado `overflow-y: auto` no `.modal-overlay`

### Bug 6: CSS cacheado pelo Cloudflare
- **Sintoma**: Mudanças de estilo não apareciam no browser
- **Fix**: Cache-busting via `style.css?v=N` no `base.html`

### Bug 7: Correlação por rowid sequencial (CRÍTICO)
- **Sintoma**: Registros inseridos na mesma transação (`voter_hashes` + `votes`) tinham o mesmo rowid auto-incrementado, permitindo correlação por posição
- **Causa**: O SQLite atribui por padrão um `rowid` implícito sequencial a cada registro
- **Identificado por**: Eduardo Paiva (ref: `temp/reference2.md`): *"Se os dados estão sendo salvos sequencialmente, os mesmos índices representam o mesmo voto"*
- **Fix**: `WITHOUT ROWID` em todas as 3 tabelas + PKs naturais (`hash`, `uuid`) eliminando completamente o auto-increment implícito. O swap de Paiva (shuffle pós-inserção) já existia como camada extra, mas `WITHOUT ROWID` é a solução definitiva a nível de schema.

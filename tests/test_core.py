"""
Testes unitários do Vote-Core.

Cobre os módulos críticos de segurança (crypto e database)
que não dependem de rede ou serviços externos.

Executar:
    source .venv/bin/activate
    pytest tests/ -v
"""

import asyncio
import pytest
import pytest_asyncio

from app.crypto import generate_voter_hash, generate_audit_id, verify_voter_hash
from sqlalchemy import text
from app.database import (
    init_db, close_db,
    check_if_voted, register_voter_hash,
    insert_vote, get_vote_counts, get_total_votes,
    get_vote_by_uuid, get_vote_by_audit_id,
)
from app.models import Vote


# ─── Crypto: generate_voter_hash ─────────────────────────────────


class TestGenerateVoterHash:
    SALT = "test-salt-key-32chars-xxxxxxxxxxxxx"

    def test_mesmo_nusp_mesmo_hash(self):
        h1 = generate_voter_hash("12345678", self.SALT)
        h2 = generate_voter_hash("12345678", self.SALT)
        assert h1 == h2, "Mesmo NUSP deve gerar mesmo hash"

    def test_nusps_diferentes_hashes_diferentes(self):
        h1 = generate_voter_hash("12345678", self.SALT)
        h2 = generate_voter_hash("87654321", self.SALT)
        assert h1 != h2

    def test_hash_64_chars_hexadecimal(self):
        h = generate_voter_hash("12345678", self.SALT)
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_salt_diferente_hash_diferente(self):
        h1 = generate_voter_hash("12345678", "salt-a")
        h2 = generate_voter_hash("12345678", "salt-b")
        assert h1 != h2, "Mesmo NUSP com salt diferente deve gerar hash diferente"

    def test_nusp_vazio_raise(self):
        with pytest.raises(ValueError):
            generate_voter_hash("", self.SALT)

    def test_nusp_com_espacos_normalizado(self):
        h1 = generate_voter_hash("12345678", self.SALT)
        h2 = generate_voter_hash("  12345678  ", self.SALT)
        assert h1 == h2, "Espaços devem ser removidos (strip)"


# ─── Crypto: generate_audit_id ───────────────────────────────────


class TestGenerateAuditId:
    SALT_2 = "test-salt-2-for-audit"

    def test_deterministic(self):
        a1 = generate_audit_id("12345678", "minhasenha", self.SALT_2)
        a2 = generate_audit_id("12345678", "minhasenha", self.SALT_2)
        assert a1 == a2, "Mesmos inputs devem gerar mesmo audit_id"

    def test_senha_diferente_audit_diferente(self):
        a1 = generate_audit_id("12345678", "senhaA", self.SALT_2)
        a2 = generate_audit_id("12345678", "senhaB", self.SALT_2)
        assert a1 != a2

    def test_nusp_diferente_audit_diferente(self):
        a1 = generate_audit_id("12345678", "mesmasenha", self.SALT_2)
        a2 = generate_audit_id("87654321", "mesmasenha", self.SALT_2)
        assert a1 != a2

    def test_salt2_diferente_audit_diferente(self):
        a1 = generate_audit_id("12345678", "senha", "salt-x")
        a2 = generate_audit_id("12345678", "senha", "salt-y")
        assert a1 != a2, "SALT_2 diferente deve gerar audit_id diferente"

    def test_audit_id_64_chars_hex(self):
        a = generate_audit_id("12345678", "senha", self.SALT_2)
        assert len(a) == 64
        assert all(c in "0123456789abcdef" for c in a)

    def test_audit_id_diferente_de_voter_hash(self):
        """audit_id e voter_hash para o mesmo NUSP devem ser diferentes (salts diferentes)."""
        vh = generate_voter_hash("12345678", "salt-1")
        ai = generate_audit_id("12345678", "qualquersenha", "salt-1")
        assert vh != ai, "audit_id inclui a senha, voter_hash não"


# ─── Crypto: verify_voter_hash ───────────────────────────────────


class TestVerifyVoterHash:
    SALT = "verification-salt"

    def test_verifica_correto(self):
        h = generate_voter_hash("12345678", self.SALT)
        assert verify_voter_hash("12345678", self.SALT, h)

    def test_rejeita_nusp_errado(self):
        h = generate_voter_hash("12345678", self.SALT)
        assert not verify_voter_hash("87654321", self.SALT, h)

    def test_rejeita_hash_adulterado(self):
        h = generate_voter_hash("12345678", self.SALT)
        h_adulterado = h[:-1] + ("0" if h[-1] != "0" else "1")
        assert not verify_voter_hash("12345678", self.SALT, h_adulterado)


# ─── Database ────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def event_loop():
    """Event loop compartilhado para todos os testes do módulo."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="module", autouse=True)
async def setup_db():
    """Inicializa o banco em memória para os testes."""
    await init_db()
    yield
    await close_db()


@pytest.mark.asyncio
async def test_deduplication_primeiro_registro():
    h = "a" * 64
    assert not await check_if_voted(h), "Hash novo não deve existir"
    result = await register_voter_hash(h)
    assert result is True, "Primeiro registro deve returnar True"


@pytest.mark.asyncio
async def test_deduplication_duplicata_rejeitada():
    h = "b" * 64
    await register_voter_hash(h)
    result = await register_voter_hash(h)
    assert result is False, "Duplicata deve retornar False"


@pytest.mark.asyncio
async def test_deduplication_check_after_register():
    h = "c" * 64
    assert not await check_if_voted(h)
    await register_voter_hash(h)
    assert await check_if_voted(h)


@pytest.mark.asyncio
async def test_insert_vote_retorna_uuid():
    uuid = await insert_vote("Sim", "audit_" + "x" * 60)
    assert isinstance(uuid, str)
    assert len(uuid) == 36  # formato UUID v4


@pytest.mark.asyncio
async def test_vote_counts_corretos():
    await insert_vote("Não", "audit_" + "y" * 60)
    await insert_vote("Nulo", "audit_" + "z" * 60)
    await insert_vote("Sim", "audit_" + "w" * 60)

    counts = await get_vote_counts()
    assert counts.get("Sim", 0) >= 2
    assert counts.get("Não", 0) >= 1
    assert counts.get("Nulo", 0) >= 1


@pytest.mark.asyncio
async def test_total_votes_aumenta():
    total_antes = await get_total_votes()
    await insert_vote("Sim", "audit_inc_" + "q" * 56)
    total_depois = await get_total_votes()
    assert total_depois == total_antes + 1


@pytest.mark.asyncio
async def test_get_vote_by_uuid_encontra():
    uuid = await insert_vote("Não", "audit_find_" + "r" * 55)
    vote = await get_vote_by_uuid(uuid)
    assert vote is not None
    assert vote.uuid == uuid
    assert vote.vote == "Não"


@pytest.mark.asyncio
async def test_get_vote_by_uuid_nao_encontra():
    vote = await get_vote_by_uuid("00000000-0000-0000-0000-000000000000")
    assert vote is None


# ─── Database: audit_id ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_vote_by_audit_id_encontra():
    audit = "audit_lookup_" + "s" * 53
    uuid = await insert_vote("Sim", audit)
    vote = await get_vote_by_audit_id(audit)
    assert vote is not None
    assert vote.uuid == uuid
    assert vote.vote == "Sim"


@pytest.mark.asyncio
async def test_get_vote_by_audit_id_nao_encontra():
    vote = await get_vote_by_audit_id("nonexistent_" + "0" * 54)
    assert vote is None


# ─── Model: Vote sem created_at ──────────────────────────────────


def test_vote_model_sem_created_at():
    """A Tabela 2 (votes) NÃO deve ter campo created_at — timestamp só fica na Tabela 1."""
    columns = {c.name for c in Vote.__table__.columns}
    assert "created_at" not in columns, (
        "Vote NÃO deve ter created_at! "
        "Timestamp na Tabela 2 cria vetor de correlação com a Tabela 1."
    )
    assert "audit_id" in columns, "Vote deve ter campo audit_id"


# ─── Model: WITHOUT ROWID — sem rowid implícito ─────────────────

from app.models import VoterHash, PublicVote


def test_without_rowid_voter_hashes():
    """Tabela 1 (voter_hashes) deve usar WITHOUT ROWID — sem rowid implícito."""
    info = VoterHash.__table__.dialect_options.get("sqlite", {})
    assert info.get("with_rowid") is False, (
        "voter_hashes DEVE usar WITHOUT ROWID! "
        "O rowid implícito permite correlação sequencial entre tabelas."
    )
    # PK deve ser 'hash' (texto), não 'id' (inteiro)
    pk_cols = [c.name for c in VoterHash.__table__.primary_key.columns]
    assert pk_cols == ["hash"], f"PK deve ser ['hash'], obteve {pk_cols}"
    columns = {c.name for c in VoterHash.__table__.columns}
    assert "id" not in columns, "voter_hashes NÃO deve ter coluna 'id' inteira"


def test_without_rowid_votes():
    """Tabela 2 (votes) deve usar WITHOUT ROWID — sem rowid implícito."""
    info = Vote.__table__.dialect_options.get("sqlite", {})
    assert info.get("with_rowid") is False, (
        "votes DEVE usar WITHOUT ROWID! "
        "O rowid implícito permite correlação sequencial entre tabelas."
    )
    pk_cols = [c.name for c in Vote.__table__.primary_key.columns]
    assert pk_cols == ["uuid"], f"PK deve ser ['uuid'], obteve {pk_cols}"
    columns = {c.name for c in Vote.__table__.columns}
    assert "id" not in columns, "votes NÃO deve ter coluna 'id' inteira"


def test_without_rowid_public_votes():
    """Tabela 3 (public_votes) deve usar WITHOUT ROWID — sem rowid implícito."""
    info = PublicVote.__table__.dialect_options.get("sqlite", {})
    assert info.get("with_rowid") is False, (
        "public_votes DEVE usar WITHOUT ROWID! "
        "O rowid implícito permite correlação sequencial entre tabelas."
    )
    pk_cols = [c.name for c in PublicVote.__table__.primary_key.columns]
    assert pk_cols == ["uuid"], f"PK deve ser ['uuid'], obteve {pk_cols}"
    columns = {c.name for c in PublicVote.__table__.columns}
    assert "id" not in columns, "public_votes NÃO deve ter coluna 'id' inteira"


# ─── Model: public_votes não expõe audit_id ──────────────────────


def test_public_votes_sem_audit_id():
    """Tabela 3 NÃO deve conter audit_id — defesa em profundidade."""
    columns = {c.name for c in PublicVote.__table__.columns}
    assert "audit_id" not in columns, (
        "public_votes NÃO deve ter audit_id! "
        "A Tabela 3 é exposta publicamente e audit_id deve existir apenas na Tabela 2."
    )


def test_votes_tem_audit_id():
    """Tabela 2 DEVE conter audit_id para auditoria pessoal."""
    columns = {c.name for c in Vote.__table__.columns}
    assert "audit_id" in columns


# ─── Scraper: DocumentData não contém RG ─────────────────────────

from app.scraper import DocumentData, COURSE_CODE_PATTERN, _check_eligibility


def test_document_data_sem_campo_rg():
    """DocumentData NÃO deve ter campo 'rg' — dado pessoal removido."""
    fields = {f.name for f in DocumentData.__dataclass_fields__.values()}
    assert "rg" not in fields, "DocumentData NÃO deve ter campo 'rg'"


def test_document_data_tem_course_code():
    """DocumentData deve ter campo 'course_code' para filtro de elegibilidade."""
    fields = {f.name for f in DocumentData.__dataclass_fields__.values()}
    assert "course_code" in fields


def test_document_data_campos_obrigatorios():
    """DocumentData deve ter todos os campos esperados."""
    fields = {f.name for f in DocumentData.__dataclass_fields__.values()}
    expected = {"nusp", "curso", "course_code", "unidade", "nome", "is_eligible"}
    assert fields == expected, f"Campos: {fields}, esperado: {expected}"


# ─── Scraper: COURSE_CODE_PATTERN ────────────────────────────────

import re


def test_course_code_pattern_extrai_codigo():
    """Regex deve extrair código de curso do formato 'Curso: 97001 - Nome'."""
    text = "Curso: 97001 - Engenharia de Computação"
    match = COURSE_CODE_PATTERN.search(text)
    assert match is not None
    assert match.group(1) == "97001"


def test_course_code_pattern_ignora_texto_sem_codigo():
    """Regex não deve dar match em texto sem padrão de curso."""
    text = "Este é um texto qualquer sem código de curso"
    match = COURSE_CODE_PATTERN.search(text)
    assert match is None


# ─── Scraper: _check_eligibility ─────────────────────────────────


class FakeSettings:
    """Mock de Settings para testes de elegibilidade."""
    def __init__(self, units="", courses="", keywords=""):
        self.eligible_unit_codes_list = [c.strip() for c in units.split(",") if c.strip()] if units else []
        # Espelha o contrato real: None = wildcard ('*'), [] = vazio, lista = filtro ativo
        if courses == "*":
            self.eligible_course_codes_list = None
        elif courses:
            self.eligible_course_codes_list = [c.strip() for c in courses.split(",") if c.strip()]
        else:
            self.eligible_course_codes_list = []
        self.eligible_keywords_list = [k.strip() for k in keywords.split("|") if k.strip()] if keywords else []


SAMPLE_PDF_TEXT = (
    "Unidade: 97 - Escola de Engenharia de São Carlos e Instituto de Ciências "
    "Matemáticas e de Computação\n"
    "Curso: 97001 - Engenharia de Computação\n"
)


def test_eligibility_sem_filtros_aceita_todos():
    """Sem nenhum filtro configurado, qualquer aluno é elegível."""
    assert _check_eligibility(SAMPLE_PDF_TEXT, "97001", FakeSettings()) is True


def test_eligibility_unit_match():
    """Unidade 97 deve dar match."""
    assert _check_eligibility(SAMPLE_PDF_TEXT, "97001", FakeSettings(units="97")) is True


def test_eligibility_unit_no_match():
    """Unidade 55 NÃO deve dar match."""
    assert _check_eligibility(SAMPLE_PDF_TEXT, "97001", FakeSettings(units="55")) is False


def test_eligibility_course_match():
    """Curso 97001 deve dar match."""
    assert _check_eligibility(SAMPLE_PDF_TEXT, "97001", FakeSettings(courses="97001")) is True


def test_eligibility_course_no_match():
    """Curso 97002 NÃO deve dar match (curso diferente)."""
    assert _check_eligibility(SAMPLE_PDF_TEXT, "97001", FakeSettings(courses="97002")) is False


def test_eligibility_course_prioridade_sobre_unidade():
    """Se ELIGIBLE_COURSE_CODES definido, unidade é IGNORADA."""
    # Curso errado + unidade certa = NÃO elegível (curso tem prioridade)
    assert _check_eligibility(
        SAMPLE_PDF_TEXT, "97001", FakeSettings(units="97", courses="97002")
    ) is False


def test_eligibility_course_lista_com_match():
    """Lista de cursos com pelo menos um match deve aceitar."""
    assert _check_eligibility(
        SAMPLE_PDF_TEXT, "97001", FakeSettings(courses="97002,97001")
    ) is True


def test_eligibility_keyword_match():
    """Keyword presente no texto deve dar match."""
    assert _check_eligibility(
        SAMPLE_PDF_TEXT, "97001", FakeSettings(keywords="Engenharia de Computação")
    ) is True


def test_eligibility_keyword_no_match():
    """Keyword 'EESC' NÃO aparece no PDF do Júpiter."""
    assert _check_eligibility(SAMPLE_PDF_TEXT, "97001", FakeSettings(keywords="EESC")) is False


def test_eligibility_course_wildcard_aceita_qualquer_curso():
    """ELIGIBLE_COURSE_CODES='*' deve aceitar qualquer curso (pula verificação)."""
    # Mesmo com um curso diferente do que está no texto, wildcard aceita tudo
    assert _check_eligibility(SAMPLE_PDF_TEXT, "99999", FakeSettings(courses="*")) is True


def test_eligibility_course_wildcard_cai_em_keywords():
    """Com wildcard em curso, o filtro de keywords ainda deve ser aplicado."""
    # keyword errada → bloqueado mesmo com wildcard no curso
    assert _check_eligibility(
        SAMPLE_PDF_TEXT, "97001", FakeSettings(courses="*", keywords="ICMC")
    ) is False
    # keyword certa → aceito
    assert _check_eligibility(
        SAMPLE_PDF_TEXT, "97001", FakeSettings(courses="*", keywords="Engenharia de Computação")
    ) is True


# ─── Config: propriedades computadas ─────────────────────────────

from app.config import Settings


def test_config_eligible_course_codes_list_vazio():
    """ELIGIBLE_COURSE_CODES vazio retorna lista vazia."""
    s = Settings(
        SECRET_KEY="x", SALT_KEY="y", SALT_2="z",
        ELIGIBLE_COURSE_CODES="", _env_file=None,
    )
    assert s.eligible_course_codes_list == []


def test_config_eligible_course_codes_list_com_valores():
    """ELIGIBLE_COURSE_CODES com valores retorna lista correta."""
    s = Settings(
        SECRET_KEY="x", SALT_KEY="y", SALT_2="z",
        ELIGIBLE_COURSE_CODES="97001,97002", _env_file=None,
    )
    assert s.eligible_course_codes_list == ["97001", "97002"]


def test_config_eligible_course_codes_list_wildcard():
    """ELIGIBLE_COURSE_CODES='*' retorna None (wildcard — pula verificação de curso)."""
    s = Settings(
        SECRET_KEY="x", SALT_KEY="y", SALT_2="z",
        ELIGIBLE_COURSE_CODES="*", _env_file=None,
    )
    assert s.eligible_course_codes_list is None


# ─── Database: pragmas SQLite ────────────────────────────────────


@pytest.mark.asyncio
async def test_pragma_busy_timeout():
    """Engine do app deve configurar busy_timeout >= 15000ms."""
    from app.database import _get_engine
    engine = _get_engine()
    async with engine.connect() as conn:
        result = await conn.execute(text("PRAGMA busy_timeout"))
        timeout = result.scalar()
        assert timeout >= 15000, f"busy_timeout deve ser >= 15000, obteve {timeout}"


@pytest.mark.asyncio
async def test_pragma_journal_mode_wal():
    """Engine do app deve usar journal_mode=WAL."""
    from app.database import _get_engine
    engine = _get_engine()
    async with engine.connect() as conn:
        result = await conn.execute(text("PRAGMA journal_mode"))
        mode = result.scalar()
        assert mode == "wal", f"journal_mode deve ser 'wal', obteve '{mode}'"


@pytest.mark.asyncio
async def test_pragma_wal_autocheckpoint():
    """Engine do app deve ter wal_autocheckpoint configurado."""
    from app.database import _get_engine
    engine = _get_engine()
    async with engine.connect() as conn:
        result = await conn.execute(text("PRAGMA wal_autocheckpoint"))
        cp = result.scalar()
        assert cp == 1000, f"wal_autocheckpoint deve ser 1000, obteve {cp}"

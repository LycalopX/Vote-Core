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

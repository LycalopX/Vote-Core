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

from app.crypto import generate_voter_hash, normalize_rg, verify_hash_match
from app.database import (
    init_db, close_db,
    check_if_voted, register_voter_hash,
    insert_vote, get_vote_counts, get_total_votes, get_vote_by_uuid,
)

# ─── Crypto ──────────────────────────────────────────────────────────────────


class TestNormalizeRG:
    def test_remove_pontos_e_traco(self):
        assert normalize_rg("13.560.200-9") == "135602009"

    def test_sem_formatacao(self):
        assert normalize_rg("135602009") == "135602009"

    def test_traco_sem_pontos(self):
        assert normalize_rg("13560200-9") == "135602009"

    def test_espacos_extras(self):
        assert normalize_rg("  13.560.200-9  ") == "135602009"

    def test_maiusculas_preservadas(self):
        # RG com letra (ex: SP)
        assert normalize_rg("99.999.999-X") == "99999999X"


class TestGenerateVoterHash:
    SALT = "test-salt-key-32chars-xxxxxxxxxxxxx"

    def test_mesmo_rg_mesmo_hash(self):
        h1 = generate_voter_hash("13.560.200-9", self.SALT)
        h2 = generate_voter_hash("13560200-9", self.SALT)
        assert h1 == h2, "RG formatado diferente deve gerar mesmo hash"

    def test_rgs_diferentes_hashes_diferentes(self):
        h1 = generate_voter_hash("13.560.200-9", self.SALT)
        h2 = generate_voter_hash("99.999.999-X", self.SALT)
        assert h1 != h2

    def test_hash_64_chars_hexadecimal(self):
        h = generate_voter_hash("12345678-9", self.SALT)
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_salt_diferente_hash_diferente(self):
        h1 = generate_voter_hash("12345678-9", "salt-a")
        h2 = generate_voter_hash("12345678-9", "salt-b")
        assert h1 != h2, "Mesmo RG com salt diferente deve gerar hash diferente"


class TestVerifyHashMatch:
    SALT = "verification-salt"

    def test_verifica_correto(self):
        h = generate_voter_hash("13.560.200-9", self.SALT)
        assert verify_hash_match("13.560.200-9", self.SALT, h)

    def test_rejeita_rg_errado(self):
        h = generate_voter_hash("13.560.200-9", self.SALT)
        assert not verify_hash_match("99.999.999-X", self.SALT, h)

    def test_rg_formatado_diferente_ainda_verifica(self):
        h = generate_voter_hash("13.560.200-9", self.SALT)
        assert verify_hash_match("135602009", self.SALT, h)

    def test_rejeita_hash_adulterado(self):
        h = generate_voter_hash("13.560.200-9", self.SALT)
        h_adulterado = h[:-1] + ("0" if h[-1] != "0" else "1")
        assert not verify_hash_match("13.560.200-9", self.SALT, h_adulterado)


# ─── Database ────────────────────────────────────────────────────────────────


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
    uuid = await insert_vote("Sim")
    assert isinstance(uuid, str)
    assert len(uuid) == 36  # formato UUID v4


@pytest.mark.asyncio
async def test_vote_counts_corretos():
    # Inserir votos adicionais para contar
    await insert_vote("Não")
    await insert_vote("Nulo")
    await insert_vote("Sim")

    counts = await get_vote_counts()
    assert counts.get("Sim", 0) >= 2
    assert counts.get("Não", 0) >= 1
    assert counts.get("Nulo", 0) >= 1


@pytest.mark.asyncio
async def test_total_votes_aumenta():
    total_antes = await get_total_votes()
    await insert_vote("Sim")
    total_depois = await get_total_votes()
    assert total_depois == total_antes + 1


@pytest.mark.asyncio
async def test_get_vote_by_uuid_encontra():
    uuid = await insert_vote("Não")
    vote = await get_vote_by_uuid(uuid)
    assert vote is not None
    assert vote.uuid == uuid
    assert vote.vote == "Não"


@pytest.mark.asyncio
async def test_get_vote_by_uuid_nao_encontra():
    vote = await get_vote_by_uuid("00000000-0000-0000-0000-000000000000")
    assert vote is None

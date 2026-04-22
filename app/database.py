"""
Banco de dados SQLite async com SQLAlchemy.

Funções CRUD para as três tabelas isoladas (WITHOUT ROWID):
  - voter_hashes: deduplicação — HMAC(NUSP, SALT_KEY)         [PK: hash]
  - votes: registro anônimo — uuid + audit_id + vote           [PK: uuid]
  - public_votes: transparência pública — uuid + vote          [PK: uuid]

WITHOUT ROWID: Todas as tabelas usam chaves naturais como PK (hash, uuid)
e não possuem rowid implícito. Isso impede correlação por posição sequencial
entre tabelas — o rowid auto-incrementado do SQLite padrão permitiria
que o registro #42 na Tabela 1 correspondesse ao registro #42 na Tabela 2.
"""

import uuid
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy import select, func, event

from app.models import Base, VoterHash, Vote, PublicVote
from app.config import get_settings


# ─── Engine & Session Factory ────────────────────────────────────

_engine = None
_session_factory = None


def _get_engine():
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.DATABASE_URL,
            echo=settings.DEBUG,
            pool_pre_ping=True,
        )

        @event.listens_for(_engine.sync_engine, "connect")
        def set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA cache_size=-64000")
            # Timeout de 15s para escritas concorrentes — evita "database is locked"
            # em picos de votação no hardware limitado (Blackview MP60)
            cursor.execute("PRAGMA busy_timeout=15000")
            # WAL auto-checkpoint a cada 1000 páginas (~4MB)
            cursor.execute("PRAGMA wal_autocheckpoint=1000")
            cursor.close()

    return _engine


def _get_session_factory():
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            _get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _session_factory


async def init_db():
    """Cria todas as tabelas se não existirem."""
    engine = _get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def close_db():
    """Fecha o engine do banco."""
    global _engine, _session_factory
    if _engine:
        await _engine.dispose()
        _engine = None
        _session_factory = None


@asynccontextmanager
async def get_session():
    """Context manager para sessões do banco."""
    factory = _get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ─── Operações da Tabela 1: voter_hashes ─────────────────────────


async def check_if_voted(voter_hash: str) -> bool:
    """
    Verifica se o hash do NUSP já existe na tabela de deduplicação.

    Args:
        voter_hash: HMAC-SHA256 hex string (64 chars)

    Returns:
        True se o eleitor já votou (hash encontrado)
    """
    async with get_session() as session:
        result = await session.execute(
            select(VoterHash).where(VoterHash.hash == voter_hash)
        )
        return result.scalar_one_or_none() is not None


async def register_voter_hash(voter_hash: str) -> bool:
    """
    Registra o hash do NUSP na tabela de deduplicação (atômico).

    Args:
        voter_hash: HMAC-SHA256 hex string (64 chars)

    Returns:
        True se registrado com sucesso, False se já existia (voto duplo)
    """
    from sqlalchemy.exc import IntegrityError

    async with get_session() as session:
        try:
            session.add(VoterHash(hash=voter_hash))
            await session.flush()
            return True
        except IntegrityError:
            await session.rollback()
            return False


# ─── Operações da Tabela 2: votes ────────────────────────────────


async def insert_vote(vote_choice: str, audit_id: str) -> str:
    """
    Registra um voto anônimo nas Tabelas 2 e 3 atomicamente.

    A inserção é feita numa única transação SQLAlchemy.
    Correlação por posição sequencial é impossível porque todas as tabelas
    usam WITHOUT ROWID — a posição física no B-Tree é determinada pela PK
    natural (UUID v4 aleatório), não pela ordem de inserção.
    """
    vote_uuid = str(uuid.uuid4())
    async with get_session() as session:
        session.add(Vote(uuid=vote_uuid, audit_id=audit_id, vote=vote_choice))
        session.add(PublicVote(uuid=vote_uuid, vote=vote_choice))

    return vote_uuid


async def get_vote_by_uuid(vote_uuid: str) -> Vote | None:
    """
    Busca um voto pelo UUID público de auditoria.

    Args:
        vote_uuid: UUID v4 string

    Returns:
        Objeto Vote ou None se não encontrado
    """
    async with get_session() as session:
        result = await session.execute(
            select(Vote).where(Vote.uuid == vote_uuid)
        )
        return result.scalar_one_or_none()


async def get_vote_by_audit_id(audit_id: str) -> Vote | None:
    """
    Busca um voto pelo audit_id — para auditoria pessoal via NUSP + senha.

    Args:
        audit_id: HMAC(NUSP + senha, SALT_2) recalculado pelo eleitor

    Returns:
        Objeto Vote ou None se não encontrado
    """
    async with get_session() as session:
        result = await session.execute(
            select(Vote).where(Vote.audit_id == audit_id)
        )
        return result.scalar_one_or_none()


async def get_vote_counts() -> dict[str, int]:
    """
    Retorna a contagem de votos por opção (da Tabela 3 pública).

    Returns:
        Dict ex: {'Sim': 150, 'Não': 87, 'Nulo': 12}
    """
    async with get_session() as session:
        result = await session.execute(
            select(PublicVote.vote, func.count()).group_by(PublicVote.vote)
        )
        counts = {row[0]: row[1] for row in result.all()}

    # Garante que todas as opções do .env apareçam, mesmo com 0 votos
    settings = get_settings()
    for option in settings.vote_options_list:
        if option not in counts:
            counts[option] = 0

    return counts


async def get_total_votes() -> int:
    """Retorna o total de votos registrados (da Tabela 3 pública)."""
    async with get_session() as session:
        result = await session.execute(
            select(func.count()).select_from(PublicVote)
        )
        return result.scalar_one()


async def get_all_public_votes() -> list[PublicVote]:
    """
    Retorna todos os votos da Tabela 3 pública (uuid + vote).
    Ordenados por UUID (aleatório por natureza).
    Com WITHOUT ROWID, a ordem física já é por UUID (B-Tree clustered),
    então esta query é eficiente — é um scan sequencial do B-Tree.
    """
    async with get_session() as session:
        result = await session.execute(
            select(PublicVote).order_by(PublicVote.uuid)
        )
        return list(result.scalars().all())

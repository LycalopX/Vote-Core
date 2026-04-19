"""
Banco de dados SQLite async com SQLAlchemy.

Funções CRUD para as duas tabelas isoladas:
- voter_hashes: deduplicação (hash HMAC existe? → voto duplo)
- votes: registro anônimo (UUID + voto)
"""

import uuid
from collections import Counter
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy import select, func

from app.models import Base, VoterHash, Vote
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
    Verifica se o hash do eleitor já existe na tabela de deduplicação.

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
    Registra o hash do eleitor na tabela de deduplicação.

    Args:
        voter_hash: HMAC-SHA256 hex string (64 chars)

    Returns:
        True se registrado com sucesso, False se já existia

    Note:
        Usa INSERT direto com try/except no IntegrityError da UNIQUE constraint.
        Isso elimina a race condition TOCTOU que existia no SELECT+INSERT anterior:
        dois requests simultâneos não conseguem mais ambos passar no check.
    """
    from sqlalchemy.exc import IntegrityError

    async with get_session() as session:
        try:
            session.add(VoterHash(hash=voter_hash))
            await session.flush()  # Força o INSERT antes do commit
            return True
        except IntegrityError:
            # UNIQUE constraint violada — hash já existe (voto duplo)
            await session.rollback()
            return False


# ─── Operações da Tabela 2: votes ────────────────────────────────


async def insert_vote(vote_choice: str) -> str:
    """
    Registra um voto anônimo e retorna o UUID de auditoria.

    Args:
        vote_choice: Opção escolhida ('Sim', 'Não', ou 'Nulo')

    Returns:
        UUID v4 string — o recibo de auditoria do eleitor
    """
    vote_uuid = str(uuid.uuid4())
    async with get_session() as session:
        session.add(Vote(uuid=vote_uuid, vote=vote_choice))
    return vote_uuid


async def get_vote_by_uuid(vote_uuid: str) -> Vote | None:
    """
    Busca um voto pelo UUID de auditoria.

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


async def get_vote_counts() -> dict[str, int]:
    """
    Retorna a contagem de votos por opção.

    Returns:
        Dict ex: {'Sim': 150, 'Não': 87, 'Nulo': 12}
    """
    async with get_session() as session:
        result = await session.execute(
            select(Vote.vote, func.count(Vote.id)).group_by(Vote.vote)
        )
        counts = {row[0]: row[1] for row in result.all()}

    # Garante que todas as opções do .env apareçam, mesmo com 0 votos
    settings = get_settings()
    for option in settings.vote_options_list:
        if option not in counts:
            counts[option] = 0

    return counts


async def get_total_votes() -> int:
    """Retorna o total de votos registrados."""
    async with get_session() as session:
        result = await session.execute(select(func.count(Vote.id)))
        return result.scalar_one()

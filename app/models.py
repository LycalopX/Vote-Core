"""
Modelos SQLAlchemy para o banco de dados.

DESIGN CRÍTICO: Não existe nenhuma Foreign Key entre VoterHash e Vote.
A correlação entre "quem votou" e "qual voto" é matematicamente impossível.
"""

from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, DateTime, Index
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class VoterHash(Base):
    """
    Tabela 1 — Catraca de Deduplicação (FECHADA)

    Armazena apenas o HMAC-SHA256 do RG do eleitor.
    Usado exclusivamente para verificar se o eleitor já votou.
    NÃO contém nenhum dado pessoal identificável.
    """

    __tablename__ = "voter_hashes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    hash = Column(
        String(64),
        unique=True,
        nullable=False,
        index=True,
        comment="HMAC-SHA256(RG + SALT_KEY) — 64 chars hex",
    )
    created_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<VoterHash(id={self.id}, hash='{self.hash[:12]}...')>"


class Vote(Base):
    """
    Tabela 2 — Urna de Votos (PÚBLICA para auditoria)

    Armazena o voto atrelado a um UUID aleatório gerado na hora.
    O UUID serve como recibo de auditoria para o eleitor.

    ZERO relação com a Tabela 1. Cruzamento impossível.
    """

    __tablename__ = "votes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    uuid = Column(
        String(36),
        unique=True,
        nullable=False,
        index=True,
        comment="UUID v4 aleatório — recibo de auditoria",
    )
    vote = Column(
        String(10),
        nullable=False,
        comment="Opção escolhida: Sim, Não, ou Nulo",
    )
    created_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<Vote(uuid='{self.uuid}', vote='{self.vote}')>"


# Índice adicional para contagem rápida de votos por opção
Index("ix_votes_vote", Vote.vote)

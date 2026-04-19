"""
Modelos SQLAlchemy para o banco de dados.

DESIGN CRÍTICO: Não existe nenhuma Foreign Key entre as tabelas.
A correlação entre "quem votou" e "qual voto" é matematicamente impossível.

Tabela 1 — voter_hashes: id_voto (HMAC NUSP) + timestamp
Tabela 2 — votes: uuid + audit_id + vote  (RESTRITA — contém audit_id)
Tabela 3 — public_votes: uuid + vote  (PÚBLICA — sem audit_id, sem timestamp)
"""

from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, DateTime, Index
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class VoterHash(Base):
    """
    Tabela 1 — Catraca de Deduplicação (FECHADA)

    Armazena apenas o HMAC-SHA256 do NUSP do eleitor + o timestamp do voto.
    O timestamp fica SOMENTE aqui para evitar correlação com a Tabela 2.
    NÃO contém nenhum dado pessoal identificável.
    """

    __tablename__ = "voter_hashes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    hash = Column(
        String(64),
        unique=True,
        nullable=False,
        index=True,
        comment="HMAC-SHA256(NUSP, SALT_KEY) — 64 chars hex",
    )
    created_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
        comment="Timestamp do voto — fica APENAS nesta tabela",
    )

    def __repr__(self) -> str:
        return f"<VoterHash(id={self.id}, hash='{self.hash[:12]}...')>"


class Vote(Base):
    """
    Tabela 2 — Urna de Votos (RESTRITA)

    Armazena o voto com audit_id para auditoria pessoal.
    Esta tabela NUNCA é exposta publicamente.
    A Tabela 3 (public_votes) é a versão pública com apenas uuid + vote.
    """

    __tablename__ = "votes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    uuid = Column(
        String(36),
        unique=True,
        nullable=False,
        index=True,
        comment="UUID v4 aleatório — recibo público de auditoria",
    )
    audit_id = Column(
        String(64),
        unique=True,
        nullable=False,
        index=True,
        comment="HMAC-SHA256(NUSP + senha, SALT_2) — auditoria pessoal",
    )
    vote = Column(
        String(10),
        nullable=False,
        comment="Opção escolhida: Sim, Não, ou Nulo",
    )

    def __repr__(self) -> str:
        return f"<Vote(uuid='{self.uuid}', vote='{self.vote}')>"


class PublicVote(Base):
    """
    Tabela 3 — Transparência Pública (ABERTA)

    Espelho público da Tabela 2, contendo SOMENTE uuid + vote.
    Sem audit_id, sem timestamp — impossível correlacionar.

    Inserida atomicamente junto com a Tabela 2 no momento do voto.
    É a ÚNICA fonte de dados para a página de transparência pública.

    Defesa em profundidade: mesmo com acesso direto ao banco,
    esta tabela não contém nenhum dado que permita correlação.
    """

    __tablename__ = "public_votes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    uuid = Column(
        String(36),
        unique=True,
        nullable=False,
        index=True,
        comment="UUID v4 aleatório — mesmo da Tabela 2",
    )
    vote = Column(
        String(10),
        nullable=False,
        comment="Opção escolhida: Sim, Não, ou Nulo",
    )

    def __repr__(self) -> str:
        return f"<PublicVote(uuid='{self.uuid}', vote='{self.vote}')>"


# Índices para contagem rápida de votos por opção
Index("ix_votes_vote", Vote.vote)
Index("ix_public_votes_vote", PublicVote.vote)

"""
Modelos SQLAlchemy para o banco de dados.

DESIGN CRÍTICO: Não existe nenhuma Foreign Key entre as tabelas.
A correlação entre "quem votou" e "qual voto" é matematicamente impossível.

Tabela 1 — voter_hashes: id_voto (HMAC NUSP) + timestamp
Tabela 2 — votes: uuid + audit_id + vote  (RESTRITA — contém audit_id)
Tabela 3 — public_votes: uuid + vote  (PÚBLICA — sem audit_id, sem timestamp)

TODAS as tabelas usam WITHOUT ROWID:
  O SQLite, por padrão, atribui um rowid auto-incrementado a cada registro.
  Esse rowid implícito funciona como um contador de inserções e é sequencial.
  Se duas tabelas (ex: voter_hashes e votes) são preenchidas na mesma transação,
  o rowid 42 na Tabela 1 e o rowid 42 na Tabela 2 correspondem ao mesmo voto
  — DESTRUINDO o anonimato.

  WITHOUT ROWID elimina esse rowid implícito. Cada tabela usa sua chave natural
  (hash, uuid) como primary key direta, armazenada em B-Tree sem ordem de inserção.
  Não há mais como correlacionar registros por posição sequencial.

  Ref: https://www.sqlite.org/withoutrowid.html
"""

from datetime import datetime, timezone
from sqlalchemy import Column, String, DateTime, Index
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class VoterHash(Base):
    """
    Tabela 1 — Catraca de Deduplicação (FECHADA)

    Armazena apenas o HMAC-SHA256 do NUSP do eleitor + o timestamp do voto.
    O timestamp fica SOMENTE aqui para evitar correlação com a Tabela 2.
    NÃO contém nenhum dado pessoal identificável.

    WITHOUT ROWID: A coluna `hash` é a PK natural (B-Tree clustered).
    Sem rowid implícito — impossível correlacionar com outras tabelas por posição.
    """

    __tablename__ = "voter_hashes"
    __table_args__ = {"sqlite_with_rowid": False}

    hash = Column(
        String(64),
        primary_key=True,
        nullable=False,
        comment="HMAC-SHA256(NUSP, SALT_KEY) — 64 chars hex",
    )
    created_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
        comment="Timestamp do voto — fica APENAS nesta tabela",
    )

    def __repr__(self) -> str:
        return f"<VoterHash(hash='{self.hash[:12]}...')>"


class Vote(Base):
    """
    Tabela 2 — Urna de Votos (RESTRITA)

    Armazena o voto com audit_id para auditoria pessoal.
    Esta tabela NUNCA é exposta publicamente.
    A Tabela 3 (public_votes) é a versão pública com apenas uuid + vote.

    WITHOUT ROWID: A coluna `uuid` é a PK natural (B-Tree clustered).
    Sem rowid implícito — a posição física do registro é determinada
    pela ordenação do UUID (aleatório por natureza), não pela ordem de inserção.
    """

    __tablename__ = "votes"
    __table_args__ = {"sqlite_with_rowid": False}

    uuid = Column(
        String(36),
        primary_key=True,
        nullable=False,
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

    WITHOUT ROWID: A coluna `uuid` é a PK natural (B-Tree clustered).
    Sem rowid implícito — posição física determinada pela ordenação do UUID.
    """

    __tablename__ = "public_votes"
    __table_args__ = {"sqlite_with_rowid": False}

    uuid = Column(
        String(36),
        primary_key=True,
        nullable=False,
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

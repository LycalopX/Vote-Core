"""
Configuração global do pytest.

Força todos os testes a usarem um banco SQLite em memória (:memory:)
independente do DATABASE_URL configurado no .env, eliminando colisões
de audit_id hardcoded entre execuções consecutivas.
"""

import pytest
import pytest_asyncio
import asyncio
from unittest.mock import patch

import app.database as db_module


@pytest.fixture(scope="session")
def event_loop_policy():
    return asyncio.DefaultEventLoopPolicy()


# ─── Banco em memória para toda a sessão de testes ───────────────

@pytest.fixture(scope="session", autouse=True)
def use_in_memory_db():
    """
    Substitui o engine global por um banco SQLite em memória.

    Isso garante:
    - Isolamento total do banco de produção (votes.db)
    - Estado limpo a cada 'pytest' invocação (session scope)
    - Sem colisões de UNIQUE constraint entre runs
    """
    # Reset do engine/session_factory globais ANTES de qualquer teste
    db_module._engine = None
    db_module._session_factory = None

    with patch.object(
        db_module,
        "_get_engine",
        wraps=_make_memory_engine,
    ):
        yield

    # Limpeza após todos os testes
    db_module._engine = None
    db_module._session_factory = None


_memory_engine_instance = None


def _make_memory_engine():
    """Cria (ou reutiliza) um engine SQLite em memória para os testes."""
    global _memory_engine_instance
    if _memory_engine_instance is None:
        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy import event as sa_event

        _memory_engine_instance = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            echo=False,
        )

        @sa_event.listens_for(_memory_engine_instance.sync_engine, "connect")
        def set_pragmas(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=15000")
            cursor.execute("PRAGMA wal_autocheckpoint=1000")
            cursor.close()

    return _memory_engine_instance

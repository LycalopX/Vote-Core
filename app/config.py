"""
Configurações centralizadas do sistema de votação.

Todas as variáveis de controle da urna (título, cursos elegíveis, opções de voto)
são carregadas do .env para facilitar modificação sem alterar código.
"""

from pydantic_settings import BaseSettings
from pydantic import field_validator
from functools import lru_cache


class Settings(BaseSettings):
    """Configurações do sistema, carregadas automaticamente do .env"""

    # ─── Google OAuth 2.0 ────────────────────────────────────────
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""

    # ─── Segurança ───────────────────────────────────────────────
    SECRET_KEY: str  # Obrigatório — chave para assinar cookies de sessão
    SALT_KEY: str  # Obrigatório — chave HMAC para hash dos RGs

    # ─── Aplicação ───────────────────────────────────────────────
    BASE_URL: str = "http://localhost:8000"
    DATABASE_URL: str = "sqlite+aiosqlite:///./votes.db"
    DEBUG: bool = True

    # ─── Variáveis de Controle da Votação ────────────────────────
    VOTE_TITLE: str = "Assembleia EESC-USP — Greve 2026"
    VOTE_QUESTION: str = "Você é a favor da greve?"
    VOTE_OPTIONS: str = "Sim,Não,Nulo"

    # Códigos de unidade USP elegíveis (separados por vírgula)
    # 97 = Escola de Engenharia de São Carlos
    ELIGIBLE_UNIT_CODES: str = "97"

    # Keywords para busca no texto do atestado (separadas por |)
    ELIGIBLE_KEYWORDS: str = "Escola de Engenharia de São Carlos|EESC"

    # ─── Propriedades computadas ─────────────────────────────────

    @property
    def vote_options_list(self) -> list[str]:
        """Retorna a lista de opções de voto."""
        return [opt.strip() for opt in self.VOTE_OPTIONS.split(",")]

    @property
    def eligible_unit_codes_list(self) -> list[str]:
        """Retorna a lista de códigos de unidade elegíveis."""
        if not self.ELIGIBLE_UNIT_CODES.strip():
            return []
        return [code.strip() for code in self.ELIGIBLE_UNIT_CODES.split(",")]

    @property
    def eligible_keywords_list(self) -> list[str]:
        """Retorna a lista de keywords elegíveis para busca no atestado."""
        if not self.ELIGIBLE_KEYWORDS.strip():
            return []
        return [kw.strip() for kw in self.ELIGIBLE_KEYWORDS.split("|")]

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": True,
    }


@lru_cache
def get_settings() -> Settings:
    """Singleton das configurações. Cached para performance."""
    return Settings()

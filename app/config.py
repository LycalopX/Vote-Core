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
    SALT_KEY: str    # Obrigatório — chave HMAC para hash do NUSP (id_voto)
    SALT_2: str      # Obrigatório — chave HMAC para hash do NUSP + senha (audit_id)

    # ─── Aplicação ───────────────────────────────────────────────
    BASE_URL: str = "http://localhost:8000"
    DATABASE_URL: str = "sqlite+aiosqlite:///./votes.db"
    DEBUG: bool = True

    # ─── Variáveis de Controle da Votação ────────────────────────
    VOTE_TITLE: str = "Assembleia EESC-USP — Greve 2026"
    VOTE_QUESTION: str = "Você é a favor da greve?"
    VOTE_OPTIONS: str = "Sim,Não,Nulo"

    # Códigos de unidade USP elegíveis (separados por vírgula)
    # 97 = Escola de Engenharia de São Carlos + ICMC
    ELIGIBLE_UNIT_CODES: str = "97"

    # Códigos de curso USP elegíveis (separados por vírgula)
    # Filtro mais fino que unidade — EESC e ICMC compartilham unidade 97
    # Use '*' para aceitar qualquer curso (pula verificação de curso completamente)
    # Deixe vazio para não usar filtro de curso e cair no filtro de unidade
    # Ex: 97001 = Eng. Computação, 97002 = Eng. Elétrica/Eletrônica, etc.
    ELIGIBLE_COURSE_CODES: str = ""

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
    def eligible_course_codes_list(self) -> list[str] | None:
        """
        Retorna a lista de códigos de curso elegíveis.

        Retorna None quando ELIGIBLE_COURSE_CODES='*' (wildcard — pula verificação de curso).
        Retorna [] quando não configurado (nenhum filtro de curso ativo).
        """
        raw = self.ELIGIBLE_COURSE_CODES.strip()
        if raw == "*":
            return None  # wildcard: pula verificação de curso
        if not raw:
            return []
        return [code.strip() for code in raw.split(",")]

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

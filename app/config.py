"""
Configurações centralizadas do sistema de votação.

Todas as variáveis de controle da urna (título, cursos elegíveis, opções de voto)
são carregadas do .env para facilitar modificação sem alterar código.
"""

from pydantic_settings import BaseSettings
from pydantic import field_validator
from functools import lru_cache
from dataclasses import dataclass


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


# ─── Configuração pública (sem dados sensíveis) ────────────────────

@dataclass(frozen=True)
class PublicConfig:
    """
    Subconjunto EXPLICITAMENTE não-sensível das configurações.

    Único objeto permitido de ser passado ao template /config.
    Por ser frozen e ter campos nomeados individualmente, é impossível
    expor acidentalmente SECRET_KEY, SALT_* ou credenciais OAuth.

    Regra de ouro: se um campo não está aqui, o template não pode vê-lo.
    """

    # ── Votação ──────────────────────────────────────────────
    vote_title: str
    vote_question: str
    vote_options: list[str]

    # ── Elegibilidade ───────────────────────────────────────
    course_codes_raw: str           # valor bruto (pode ser '*', lista, ou '')
    course_codes: list[str]         # lista processada ([] se wildcard ou vazio)
    unit_codes: list[str]
    keywords: list[str]

    # ── Infraestrutura (não-sensível) ──────────────────────────
    base_url: str
    database_url: str               # caminho do arquivo, sem credenciais
    debug: bool
    oauth_configured: bool          # booleano — nunca o client_id/secret

    # ── Rate Limits (constantes de código) ──────────────────────
    rate_validate_max: int
    rate_validate_window: int
    rate_audit_max: int
    rate_audit_window: int
    rate_audit_ip_max: int
    rate_audit_ip_window: int
    max_concurrent_scrapers: int

    @classmethod
    def from_settings(
        cls,
        s: "Settings",
        *,
        rate_validate_max: int,
        rate_validate_window: int,
        rate_audit_max: int,
        rate_audit_window: int,
        rate_audit_ip_max: int,
        rate_audit_ip_window: int,
        max_concurrent_scrapers: int,
    ) -> "PublicConfig":
        """
        Único ponto de extração de dados de Settings para PublicConfig.

        Campos sensíveis (SECRET_KEY, SALT_KEY, SALT_2, GOOGLE_CLIENT_*)
        não existem em PublicConfig e não podem ser passados ao template
        por nenhum caminho acidental.
        """
        course_codes_raw = s.ELIGIBLE_COURSE_CODES.strip()
        course_codes_list = s.eligible_course_codes_list  # None | [] | [str]

        return cls(
            vote_title=s.VOTE_TITLE,
            vote_question=s.VOTE_QUESTION,
            vote_options=s.vote_options_list,
            course_codes_raw=course_codes_raw,
            course_codes=course_codes_list or [],
            unit_codes=s.eligible_unit_codes_list,
            keywords=s.eligible_keywords_list,
            base_url=s.BASE_URL,
            database_url=s.DATABASE_URL,
            debug=s.DEBUG,
            oauth_configured=bool(s.GOOGLE_CLIENT_ID and s.GOOGLE_CLIENT_SECRET),
            rate_validate_max=rate_validate_max,
            rate_validate_window=rate_validate_window,
            rate_audit_max=rate_audit_max,
            rate_audit_window=rate_audit_window,
            rate_audit_ip_max=rate_audit_ip_max,
            rate_audit_ip_window=rate_audit_ip_window,
            max_concurrent_scrapers=max_concurrent_scrapers,
        )

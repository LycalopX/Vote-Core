"""
Autenticação via Google OAuth 2.0.

Restringe acesso a contas @usp.br com validação dupla:
1. Parâmetro `hd=usp.br` no redirect (UI hint para Google)
2. Validação hard no callback (rejeita emails fora do domínio)

NOTA: As credenciais Google (CLIENT_ID/SECRET) são configuradas no .env.
Sem elas, o sistema roda em "modo dev" com autenticação simulada.
"""

import logging
from starlette.requests import Request
from starlette.responses import RedirectResponse
from authlib.integrations.starlette_client import OAuth

from app.config import get_settings

logger = logging.getLogger(__name__)

ALLOWED_DOMAIN = "usp.br"

# ─── OAuth Setup ─────────────────────────────────────────────────

oauth = OAuth()


def setup_oauth():
    """Configura o provider Google OAuth."""
    settings = get_settings()

    if not settings.GOOGLE_CLIENT_ID or settings.GOOGLE_CLIENT_ID.startswith("your-"):
        logger.warning(
            "⚠️  Google OAuth não configurado (GOOGLE_CLIENT_ID ausente). "
            "Rodando em modo dev — autenticação simulada."
        )
        return

    oauth.register(
        name="google",
        client_id=settings.GOOGLE_CLIENT_ID,
        client_secret=settings.GOOGLE_CLIENT_SECRET,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={
            "scope": "openid email profile",
            "prompt": "select_account",
        },
    )
    logger.info("✅ Google OAuth configurado")


# ─── Route Handlers ──────────────────────────────────────────────


async def login(request: Request) -> RedirectResponse:
    """
    Inicia o fluxo OAuth — redireciona para Google.

    Usa `hd=usp.br` para filtrar contas na tela do Google (soft restriction).
    """
    settings = get_settings()

    # Modo dev sem OAuth
    if not settings.GOOGLE_CLIENT_ID or settings.GOOGLE_CLIENT_ID.startswith("your-"):
        # Simula login com email de teste
        request.session["user"] = {
            "email": "dev@usp.br",
            "name": "Dev Mode",
            "authenticated": True,
        }
        return RedirectResponse(url="/validate", status_code=302)

    redirect_uri = f"{settings.BASE_URL}/auth/callback"
    return await oauth.google.authorize_redirect(
        request,
        redirect_uri,
        hd=ALLOWED_DOMAIN,  # Soft restriction: mostra só contas @usp.br
    )


async def callback(request: Request) -> RedirectResponse:
    """
    Callback do Google OAuth — valida domínio e cria sessão.

    HARD CHECK: Mesmo que alguém remova o `hd` parameter,
    o backend rejeita qualquer email que não termine em @usp.br.
    """
    settings = get_settings()

    try:
        token = await oauth.google.authorize_access_token(request)
    except Exception as e:
        logger.error("Falha na autenticação Google: %s", e)
        return RedirectResponse(url="/?error=auth_failed", status_code=302)

    user_info = token.get("userinfo", {})
    email = user_info.get("email", "")
    name = user_info.get("name", "")

    # ── HARD CHECK: Validar domínio ──
    if not email.endswith(f"@{ALLOWED_DOMAIN}"):
        masked_email = f"{email[:2]}***@{ALLOWED_DOMAIN}"
        logger.warning("Tentativa de login com email não-USP: %s", masked_email)
        return RedirectResponse(url="/?error=domain_restricted", status_code=302)

    # ── Criar sessão ──
    request.session["user"] = {
        "email": email,
        "name": name,
        "authenticated": True,
    }

    masked_email = f"{email[:2]}***@{ALLOWED_DOMAIN}"
    logger.info("Login bem-sucedido: %s", masked_email)
    return RedirectResponse(url="/validate", status_code=302)


async def logout(request: Request) -> RedirectResponse:
    """Limpa a sessão do usuário."""
    request.session.clear()
    return RedirectResponse(url="/", status_code=302)


# ─── Middleware / Dependency ─────────────────────────────────────


def get_current_user(request: Request) -> dict | None:
    """
    Retorna o usuário da sessão, ou None se não autenticado.

    Use como dependency nas rotas protegidas.
    """
    user = request.session.get("user")
    if user and user.get("authenticated"):
        return user
    return None


def require_auth(request: Request) -> dict:
    """
    Dependency que exige autenticação. Redireciona para login se não autenticado.

    Uso:
        @app.get("/protected")
        async def protected(user: dict = Depends(require_auth)):
            ...
    """
    user = get_current_user(request)
    if user is None:
        raise RedirectRequired()
    return user


class RedirectRequired(Exception):
    """Exceção para indicar que redirect para login é necessário."""

    pass

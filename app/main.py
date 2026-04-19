"""
Aplicação principal FastAPI — Urna Eletrônica Zero-Knowledge EESC.

Rotas:
  /                   Landing page com botão de login
  /auth/login         Redirect para Google OAuth
  /auth/callback      Callback do Google OAuth
  /auth/logout        Limpa sessão
  /validate           Formulário do código de controle
  /vote               Tela de votação (Sim/Não/Nulo)
  /receipt/{uuid}     Recibo de auditoria
  /results            Contagem pública dos votos
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Form, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.auth import setup_oauth, login, callback, logout, get_current_user, RedirectRequired
from app.crypto import generate_voter_hash
from app.database import init_db, close_db, check_if_voted, register_voter_hash, insert_vote, get_vote_counts, get_total_votes, get_vote_by_uuid
from app.scraper import validate_document, ScraperError, DocumentNotFoundError, DocumentExpiredError, ExtractionError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)


# ─── Lifecycle ───────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: cria tabelas + configura OAuth. Shutdown: fecha DB."""
    await init_db()
    setup_oauth()
    logger.info("🗳️  Urna Eletrônica EESC iniciada")
    yield
    await close_db()
    logger.info("🗳️  Urna Eletrônica EESC encerrada")


# ─── App ─────────────────────────────────────────────────────────

settings = get_settings()

app = FastAPI(
    title="Urna Eletrônica EESC",
    description="Sistema de votação anônimo zero-knowledge para a EESC-USP",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url=None,
)

# Middleware de sessão (cookie assinado)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.SECRET_KEY,
    session_cookie="eesc_session",
    max_age=3600,  # 1 hora
    same_site="lax",
    https_only=not settings.DEBUG,
)

# Static files e templates
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="app/templates")


# ─── Exception Handlers ─────────────────────────────────────────


@app.exception_handler(RedirectRequired)
async def redirect_to_login(request: Request, exc: RedirectRequired):
    return RedirectResponse(url="/", status_code=302)


# ─── Auth Routes ─────────────────────────────────────────────────


@app.get("/auth/login")
async def auth_login(request: Request):
    return await login(request)


@app.get("/auth/callback")
async def auth_callback(request: Request):
    return await callback(request)


@app.post("/auth/logout")
async def auth_logout(request: Request):
    return await logout(request)


# ─── Page Routes ─────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def landing_page(request: Request):
    """Landing page com botão de login Google."""
    user = get_current_user(request)
    error = request.query_params.get("error", "")

    error_messages = {
        "auth_failed": "Falha na autenticação. Tente novamente.",
        "domain_restricted": "Apenas contas @usp.br são permitidas.",
    }

    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "user": user,
            "error": error_messages.get(error, ""),
            "settings": settings,
        },
    )


@app.get("/validate", response_class=HTMLResponse)
async def validate_page(request: Request):
    """Formulário para inserir o código de controle do atestado."""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/", status_code=302)

    # Se já validou e tem dados na sessão, pular para votação
    if request.session.get("validated"):
        return RedirectResponse(url="/vote", status_code=302)

    return templates.TemplateResponse(
        request,
        "validate.html",
        {
            "user": user,
            "settings": settings,
            "error": "",
        },
    )


@app.post("/validate", response_class=HTMLResponse)
async def validate_submit(request: Request, control_code: str = Form(...)):
    """Processa o código de controle: scraper → validação → HMAC."""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/", status_code=302)

    error = ""
    try:
        # ── Fase B: Validação Volátil ──
        logger.info("Validando documento para %s", user.get("email", "?"))
        doc_data = await validate_document(control_code)

        if not doc_data.is_eligible:
            error = (
                f"Seu curso ({doc_data.curso or 'não identificado'}) "
                "não está na lista de elegíveis para esta votação."
            )
            return templates.TemplateResponse(
                request,
                "error.html",
                {"user": user, "settings": settings,
                 "error_title": "Não Elegível", "error_message": error},
            )

        # ── Fase C: Catraca de Deduplicação ──
        voter_hash = generate_voter_hash(doc_data.rg, settings.SALT_KEY)

        # RG sai de escopo aqui — nunca mais é referenciado
        # doc_data será garbage-collected quando esta função retornar
        del doc_data

        if await check_if_voted(voter_hash):
            return templates.TemplateResponse(
                request,
                "error.html",
                {"user": user, "settings": settings,
                 "error_title": "Voto Já Registrado",
                 "error_message": "Este documento já foi utilizado para votar. Cada eleitor pode votar apenas uma vez."},
            )

        # Salvar hash na sessão temporariamente para o próximo step
        request.session["voter_hash"] = voter_hash
        request.session["validated"] = True

        return RedirectResponse(url="/vote", status_code=302)

    except DocumentNotFoundError:
        error = "O código de controle não corresponde a um documento emitido pela USP. Verifique e tente novamente."
    except DocumentExpiredError:
        error = "O atestado existe mas já expirou. Emita um novo atestado no Júpiter."
    except ExtractionError as e:
        error = f"Não foi possível extrair os dados do documento: {e}"
    except ScraperError as e:
        error = f"Erro ao acessar o portal da USP: {e}"
    except ValueError as e:
        error = str(e)
    except Exception as e:
        logger.exception("Erro inesperado na validação")
        error = "Erro interno. Tente novamente em alguns instantes."

    return templates.TemplateResponse(
        request,
        "validate.html",
        {"user": user, "settings": settings, "error": error},
    )


@app.get("/vote", response_class=HTMLResponse)
async def vote_page(request: Request):
    """Tela de votação — só acessível após validação."""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/", status_code=302)

    if not request.session.get("validated"):
        return RedirectResponse(url="/validate", status_code=302)

    # Verificar se já votou (hash já consumido)
    if request.session.get("voted"):
        uuid = request.session.get("vote_uuid", "")
        return RedirectResponse(url=f"/receipt/{uuid}", status_code=302)

    return templates.TemplateResponse(
        request,
        "vote.html",
        {
            "user": user,
            "settings": settings,
            "options": settings.vote_options_list,
        },
    )


@app.post("/vote", response_class=HTMLResponse)
async def vote_submit(request: Request, vote_choice: str = Form(...)):
    """Registra o voto anônimo."""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/", status_code=302)

    if not request.session.get("validated"):
        return RedirectResponse(url="/validate", status_code=302)

    if request.session.get("voted"):
        uuid = request.session.get("vote_uuid", "")
        return RedirectResponse(url=f"/receipt/{uuid}", status_code=302)

    # Validar que a opção é válida
    if vote_choice not in settings.vote_options_list:
        return templates.TemplateResponse(
            request,
            "vote.html",
            {
                "user": user,
                "settings": settings,
                "options": settings.vote_options_list,
                "error": "Opção de voto inválida.",
            },
        )

    # ── Fase C (continuação): Registrar hash ──
    voter_hash = request.session.get("voter_hash")
    if not voter_hash:
        return RedirectResponse(url="/validate", status_code=302)

    # Atômico: registrar hash + inserir voto
    hash_registered = await register_voter_hash(voter_hash)
    if not hash_registered:
        # Race condition: alguém votou com o mesmo RG entre validate e vote
        return templates.TemplateResponse(
            request,
            "error.html",
            {"user": user, "settings": settings,
             "error_title": "Voto Já Registrado",
             "error_message": "Este documento já foi utilizado para votar."},
        )

    # ── Fase D: Registrar voto anônimo ──
    vote_uuid = await insert_vote(vote_choice)

    # Limpar dados sensíveis da sessão
    request.session.pop("voter_hash", None)
    request.session["voted"] = True
    request.session["vote_uuid"] = vote_uuid

    logger.info("Voto registrado — UUID: %s", vote_uuid)
    return RedirectResponse(url=f"/receipt/{vote_uuid}", status_code=302)


@app.get("/receipt/{vote_uuid}", response_class=HTMLResponse)
async def receipt_page(request: Request, vote_uuid: str):
    """Exibe o recibo de auditoria com o UUID."""
    user = get_current_user(request)

    vote = await get_vote_by_uuid(vote_uuid)
    if not vote:
        return templates.TemplateResponse(
            request,
            "error.html",
            {"user": user, "settings": settings,
             "error_title": "Recibo Não Encontrado",
             "error_message": "UUID de auditoria não encontrado."},
        )

    counts = await get_vote_counts()
    total = await get_total_votes()

    return templates.TemplateResponse(
        request,
        "receipt.html",
        {
            "user": user,
            "settings": settings,
            "vote": vote,
            "counts": counts,
            "total": total,
        },
    )


@app.get("/results", response_class=HTMLResponse)
async def results_page(request: Request):
    """Contagem pública dos votos — sem identificação."""
    user = get_current_user(request)
    counts = await get_vote_counts()
    total = await get_total_votes()

    return templates.TemplateResponse(
        request,
        "results.html",
        {
            "user": user,
            "settings": settings,
            "counts": counts,
            "total": total,
        },
    )


# ─── Health Check ────────────────────────────────────────────────


@app.get("/health")
async def health_check():
    return {"status": "ok", "title": settings.VOTE_TITLE}

"""
Aplicação principal FastAPI — Urna Eletrônica Zero-Knowledge EESC.

Rotas:
  /                   Landing page com botão de login
  /auth/login         Redirect para Google OAuth
  /auth/callback      Callback do Google OAuth
  /auth/logout        Limpa sessão
  /validate           Formulário do código de controle
  /vote               Tela de votação (Sim/Não/Nulo) + criação de senha de auditoria
  /receipt/{uuid}     Recibo público de auditoria
  /audit              Auditoria pessoal via NUSP + senha
  /results            Contagem pública dos votos
"""

import logging
import asyncio
import time
from collections import defaultdict
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Form, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.auth import setup_oauth, login, callback, logout, get_current_user, RedirectRequired
from app.crypto import generate_voter_hash, generate_audit_id
from app.database import (
    init_db, close_db,
    check_if_voted, register_voter_hash,
    insert_vote, get_vote_counts, get_total_votes,
    get_vote_by_uuid, get_vote_by_audit_id,
    get_all_public_votes,
)
from app.scraper import validate_document, ScraperError, DocumentNotFoundError, DocumentExpiredError, ExtractionError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)

# ─── Semaphore para limitar concorrência do Scraper ──────────────
# Com httpx (sem Playwright/Chromium), cada request é uma chamada HTTP leve
# (~200-500ms). 20 concurrent é mais que suficiente para 500 usuários em
# 2 horas (~4 req/min) e respeita a infraestrutura do servidor da USP.
MAX_CONCURRENT_SCRAPERS = 20
scraper_semaphore = asyncio.Semaphore(MAX_CONCURRENT_SCRAPERS)

# ─── Rate Limiter por IP (proteção anti-spam no /validate) ──────
RATE_LIMIT_MAX_ATTEMPTS = 5       # máximo de tentativas por janela
RATE_LIMIT_WINDOW_SECONDS = 120   # janela de 2 minutos

# Rate limiter para /audit (anti-brute-force de NUSP+senha)
AUDIT_RATE_LIMIT_MAX = 5          # máximo de tentativas por janela por NUSP
AUDIT_RATE_LIMIT_WINDOW = 60      # janela de 1 minuto

# Rate limit por IP no /audit — bloqueia enumeração rotacionando NUSPs
# (ex: tentar NUSP 1..50000 com 1 req cada)
AUDIT_IP_RATE_LIMIT_MAX = 20      # máximo de tentativas por IP por janela
AUDIT_IP_RATE_LIMIT_WINDOW = 60   # janela de 1 minuto

_rate_limit_store: dict[str, list[float]] = defaultdict(list)


def _check_rate_limit(ip: str) -> bool:
    """
    Retorna True se o IP está dentro do limite.
    Retorna False se excedeu (deve ser bloqueado).
    """
    now = time.monotonic()
    valid_times = [
        t for t in _rate_limit_store[ip]
        if now - t < RATE_LIMIT_WINDOW_SECONDS
    ]
    if not valid_times:
        del _rate_limit_store[ip]   # limpa chave fantasma da memória
    else:
        _rate_limit_store[ip] = valid_times

    if len(valid_times) >= RATE_LIMIT_MAX_ATTEMPTS:
        return False
    _rate_limit_store[ip].append(now)
    return True


def _check_audit_rate_limit(nusp: str) -> bool:
    """
    Rate limiter para /audit — previne brute-force de NUSP+senha.

    A chave é o NUSP (não o IP) porque em rede universitária (eduroam)
    centenas de alunos compartilham o mesmo IP público via NAT.
    Rate limit por IP bloquearia o campus inteiro após 5 tentativas.
    Limitar por NUSP protege o recurso-alvo (anonimato de um aluno
    específico) sem bloqueio colateral.
    """
    key = f"audit:{nusp.strip()}"
    now = time.monotonic()
    valid_times = [
        t for t in _rate_limit_store[key]
        if now - t < AUDIT_RATE_LIMIT_WINDOW
    ]
    if not valid_times:
        del _rate_limit_store[key]   # limpa chave fantasma da memória
    else:
        _rate_limit_store[key] = valid_times

    if len(valid_times) >= AUDIT_RATE_LIMIT_MAX:
        return False
    _rate_limit_store[key].append(now)
    return True


def _check_audit_ip_rate_limit(ip: str) -> bool:
    """
    Rate limiter por IP para /audit.

    Complementa o rate limit por NUSP: previne ataques de enumeração onde
    o atacante rotaciona NUSPs diferentes para contornar o limite por NUSP.
    Ex: NUSP 1 (1 req) → NUSP 2 (1 req) → ... → NUSP 50000 (1 req)
    — cada NUSP fica abaixo do limite individual, mas o IP é bloqueado
    após 20 tentativas por minuto.
    """
    key = f"audit_ip:{ip}"
    now = time.monotonic()
    valid_times = [
        t for t in _rate_limit_store[key]
        if now - t < AUDIT_IP_RATE_LIMIT_WINDOW
    ]
    if not valid_times:
        del _rate_limit_store[key]
    else:
        _rate_limit_store[key] = valid_times

    if len(valid_times) >= AUDIT_IP_RATE_LIMIT_MAX:
        return False
    _rate_limit_store[key].append(now)
    return True


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
    version="2.0.0",
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


# ─── Security Headers Middleware ─────────────────────────────────


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    """
    Adiciona cabeçalhos de segurança a todas as respostas HTTP.

    - X-Content-Type-Options: impede MIME-type sniffing
    - X-Frame-Options: impede clickjacking via iframe
    - Referrer-Policy: evita vazar URLs em referrers
    - Content-Security-Policy: restringe fontes de scripts/estilos
    - Strict-Transport-Security: força HTTPS no browser
    - X-XSS-Protection: ativa filtro XSS (legado, mas não custa)
    """
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-XSS-Protection"] = "1; mode=block"

    # CSP: permite Google Fonts + inline styles (Jinja2 templates)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'"
    )

    # HSTS apenas em produção (1 ano)
    if not settings.DEBUG:
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )

    return response

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
            "eligible_courses": settings.ELIGIBLE_COURSE_CODES if settings.ELIGIBLE_COURSE_CODES.strip() else None,
            "eligible_units": settings.ELIGIBLE_UNIT_CODES if settings.ELIGIBLE_UNIT_CODES.strip() else None,
            "eligible_keywords": settings.ELIGIBLE_KEYWORDS if settings.ELIGIBLE_KEYWORDS.strip() else None,
        },
    )


@app.get("/validate", response_class=HTMLResponse)
async def validate_page(request: Request):
    """Formulário para inserir o código de controle do atestado."""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/", status_code=302)

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
    """Processa o código de controle: scraper → validação → hash do NUSP."""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/", status_code=302)

    error = ""
    try:
        # ── Rate limit por IP ──
        client_ip = request.client.host if request.client else "unknown"
        if not _check_rate_limit(client_ip):
            logger.warning("Rate limit excedido para IP %s", client_ip)
            return templates.TemplateResponse(
                request,
                "error.html",
                {
                    "user": user,
                    "settings": settings,
                    "error_title": "Muitas Tentativas",
                    "error_message": (
                        f"Você excedeu o limite de {RATE_LIMIT_MAX_ATTEMPTS} "
                        f"tentativas em {RATE_LIMIT_WINDOW_SECONDS // 60} minutos. "
                        "Aguarde e tente novamente."
                    ),
                },
            )

        # ── Fase B: Validação Volátil com limite de concorrência ──
        logger.info("Validando documento (IP: %s)", client_ip)
        try:
            async with asyncio.timeout(60):
                async with scraper_semaphore:
                    doc_data = await validate_document(control_code)
        except TimeoutError:
            return templates.TemplateResponse(
                request,
                "error.html",
                {
                    "user": user,
                    "settings": settings,
                    "error_title": "Servidor Ocupado",
                    "error_message": "Muitos alunos validando ao mesmo tempo. Aguarde 1 minuto e tente novamente.",
                },
            )

        if not doc_data.is_eligible:
            logger.warning(
                "Eleitor não elegível — curso: %s",
                doc_data.curso,
            )
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
        voter_hash = generate_voter_hash(doc_data.nusp, settings.SALT_KEY)

        if await check_if_voted(voter_hash):
            return templates.TemplateResponse(
                request,
                "error.html",
                {"user": user, "settings": settings,
                 "error_title": "Voto Já Registrado",
                 "error_message": "Este documento já foi utilizado para votar. Cada eleitor pode votar apenas uma vez."},
            )

        # Salvar hash e NUSP na sessão temporariamente para o próximo step
        # NUSP é necessário para gerar o audit_id na tela de voto
        request.session["voter_hash"] = voter_hash
        request.session["nusp"] = doc_data.nusp
        request.session["validated"] = True

        # doc_data descartado — NUSP e RG saem de escopo aqui
        del doc_data

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
    except Exception:
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

    if request.session.get("voted"):
        vote_uuid = request.session.get("vote_uuid", "")
        return RedirectResponse(url=f"/receipt/{vote_uuid}", status_code=302)

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
async def vote_submit(
    request: Request,
    vote_choice: str = Form(...),
    audit_password: str = Form(...),
):
    """Registra o voto anônimo com audit_id gerado a partir do NUSP + senha."""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/", status_code=302)

    if not request.session.get("validated"):
        return RedirectResponse(url="/validate", status_code=302)

    if request.session.get("voted"):
        vote_uuid = request.session.get("vote_uuid", "")
        return RedirectResponse(url=f"/receipt/{vote_uuid}", status_code=302)

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

    # Validar senha mínima
    if len(audit_password.strip()) < 4:
        return templates.TemplateResponse(
            request,
            "vote.html",
            {
                "user": user,
                "settings": settings,
                "options": settings.vote_options_list,
                "error": "A senha de auditoria deve ter pelo menos 4 caracteres.",
            },
        )

    voter_hash = request.session.get("voter_hash")
    nusp = request.session.get("nusp")
    if not voter_hash or not nusp:
        return RedirectResponse(url="/validate", status_code=302)

    # ── Fase C (continuação): Registrar hash de forma atômica ──
    hash_registered = await register_voter_hash(voter_hash)
    if not hash_registered:
        return templates.TemplateResponse(
            request,
            "error.html",
            {"user": user, "settings": settings,
             "error_title": "Voto Já Registrado",
             "error_message": "Este documento já foi utilizado para votar."},
        )

    # ── Fase D: Gerar audit_id e registrar voto anônimo ──
    audit_id = generate_audit_id(nusp, audit_password.strip(), settings.SALT_2)
    vote_uuid = await insert_vote(vote_choice, audit_id)

    # Limpar dados sensíveis da sessão
    request.session.pop("voter_hash", None)
    request.session.pop("nusp", None)
    request.session["voted"] = True
    request.session["vote_uuid"] = vote_uuid

    logger.info("Voto registrado — UUID: %s", vote_uuid)
    return RedirectResponse(url=f"/receipt/{vote_uuid}", status_code=302)


@app.get("/receipt/{vote_uuid}", response_class=HTMLResponse)
async def receipt_page(request: Request, vote_uuid: str):
    """Exibe o recibo público de auditoria com o UUID."""
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


@app.get("/audit", response_class=HTMLResponse)
async def audit_page(request: Request):
    """Formulário de auditoria pessoal via NUSP + senha."""
    user = get_current_user(request)
    return templates.TemplateResponse(
        request,
        "audit.html",
        {"user": user, "settings": settings,
         "result": None, "my_uuid": None, "public_votes": [], "error": ""},
    )


@app.post("/audit", response_class=HTMLResponse)
async def audit_submit(
    request: Request,
    nusp: str = Form(...),
    audit_password: str = Form(...),
):
    """
    Recalcula o audit_id a partir do NUSP + senha e busca o voto correspondente.
    Retorna apenas a opção de voto — audit_id nunca é exposto.
    """
    user = get_current_user(request)

    nusp_clean = nusp.strip()
    password_clean = audit_password.strip()

    if not nusp_clean or not password_clean:
        return templates.TemplateResponse(
            request,
            "audit.html",
            {"user": user, "settings": settings,
             "result": None, "error": "Preencha o Número USP e a senha."},
        )

    # ── Rate limit por IP (anti-enumeração de NUSPs) ──
    # Bloqueia atacantes que rotacionam NUSPs para contornar o limite por NUSP.
    client_ip = request.client.host if request.client else "unknown"
    if not _check_audit_ip_rate_limit(client_ip):
        logger.warning("Rate limit de IP no audit excedido")
        return templates.TemplateResponse(
            request,
            "audit.html",
            {"user": user, "settings": settings,
             "result": None, "my_uuid": None, "public_votes": [],
             "error": (
                 f"Muitas tentativas deste endereço. Aguarde {AUDIT_IP_RATE_LIMIT_WINDOW} "
                 "segundos e tente novamente."
             )},
        )

    # ── Rate limit por NUSP (anti-brute-force da senha) ──
    # Chave é o NUSP, não o IP, porque na rede eduroam da USP
    # centenas de alunos compartilham o mesmo IP via NAT.
    if not _check_audit_rate_limit(nusp_clean):
        logger.warning("Rate limit de auditoria excedido")
        return templates.TemplateResponse(
            request,
            "audit.html",
            {"user": user, "settings": settings,
             "result": None, "my_uuid": None, "public_votes": [],
             "error": (
                 f"Muitas tentativas para este NUSP. Aguarde {AUDIT_RATE_LIMIT_WINDOW} "
                 "segundos e tente novamente."
             )},
        )

    audit_id = generate_audit_id(nusp_clean, password_clean, settings.SALT_2)
    vote = await get_vote_by_audit_id(audit_id)

    if not vote:
        return templates.TemplateResponse(
            request,
            "audit.html",
            {"user": user, "settings": settings,
             "result": None, "my_uuid": None, "public_votes": [],
             "error": "Nenhum voto encontrado para esta combinação de NUSP e senha."},
        )

    # Voto encontrado — também busca todos os votos públicos para transparência
    public_votes = await get_all_public_votes()

    return templates.TemplateResponse(
        request,
        "audit.html",
        {"user": user, "settings": settings,
         "result": vote.vote, "my_uuid": vote.uuid,
         "public_votes": public_votes, "error": ""},
    )


@app.get("/results", response_class=HTMLResponse)
async def results_page(request: Request):
    """Contagem pública dos votos + lista de transparência (Tabela 3)."""
    user = get_current_user(request)
    counts = await get_vote_counts()
    total = await get_total_votes()
    public_votes = await get_all_public_votes()

    return templates.TemplateResponse(
        request,
        "results.html",
        {
            "user": user,
            "settings": settings,
            "counts": counts,
            "total": total,
            "public_votes": public_votes,
        },
    )


# ─── Health Check ────────────────────────────────────────────────


@app.get("/health")
async def health_check():
    return {"status": "ok", "title": settings.VOTE_TITLE}

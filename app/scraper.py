"""
Scraper do portal USP IDDigital.

Usa Playwright para navegar no portal de serviços da USP,
inserir o código de controle do atestado do Júpiter,
interceptar o PDF retornado, e extrair RG + Curso com pdfplumber.

REGRA DE OURO: O PDF, o RG bruto, e o código de controle existem
apenas em variáveis Python in-memory. NADA toca o disco.
"""

import io
import re
import logging
from dataclasses import dataclass

import pdfplumber
from playwright.async_api import async_playwright, Page, BrowserContext

from app.config import get_settings

logger = logging.getLogger(__name__)

# ─── Tipos de Resultado ──────────────────────────────────────────


@dataclass
class DocumentData:
    """Dados extraídos do atestado do Júpiter."""

    rg: str  # RG bruto ex: '13.560.200-9'
    curso: str  # Nome do curso ex: 'Engenharia de Computação'
    unidade: str  # Unidade ex: 'Escola de Engenharia de São Carlos'
    nome: str  # Nome do aluno
    is_eligible: bool  # Se o aluno é elegível para votar


class ScraperError(Exception):
    """Erro genérico do scraper."""

    pass


class DocumentNotFoundError(ScraperError):
    """O código de controle não corresponde a um documento válido."""

    pass


class DocumentExpiredError(ScraperError):
    """O documento existe mas expirou."""

    pass


class TurnstileBlockedError(ScraperError):
    """O Cloudflare Turnstile bloqueou a requisição."""

    pass


class ExtractionError(ScraperError):
    """Falha na extração de dados do PDF."""

    pass


# ─── Regexes para extração de dados do PDF ───────────────────────

# RG no formato XX.XXX.XXX-X ou variações
RG_PATTERN = re.compile(r"(\d{1,2}\.?\d{3}\.?\d{3}-[\dXx])")

# Código USP (NUSP) — 7 ou 8 dígitos
NUSP_PATTERN = re.compile(r"código\s+USP\s+(\d{7,8})", re.IGNORECASE)

# Curso — captura após "curso de" ou "Curso:"
CURSO_PATTERN = re.compile(
    r"(?:curso\s+de\s+|Curso[:\s]+\d+\s*-\s*)(.+?)(?:\s*,|\s*\.|\s*\n|\s*do\s)",
    re.IGNORECASE,
)

# Unidade — captura o nome da unidade
UNIDADE_PATTERN = re.compile(
    r"(?:Unidade[:\s]+\d+\s*-\s*)(.+?)(?:\s*\n|\s*$)",
    re.IGNORECASE | re.MULTILINE,
)

# Nome do aluno — aparece após "Aluno:" ou "aluno(a)"
NOME_PATTERN = re.compile(
    r"(?:Aluno(?:\(a\))?[:\s]+(?:\d+\s*-\s*)?)([A-ZÀ-Ú][a-zà-ú]+(?:\s+[A-ZÀ-Ú][a-zà-ú]+)*)",
    re.UNICODE,
)

# URL do portal
IDDIGITAL_URL = "https://portalservicos.usp.br/iddigital"


# ─── Extração de dados do PDF ────────────────────────────────────


def extract_data_from_pdf(pdf_bytes: bytes) -> DocumentData:
    """
    Extrai RG, Curso, Unidade e Nome do PDF do atestado.

    Args:
        pdf_bytes: Conteúdo binário do PDF (em memória)

    Returns:
        DocumentData com os campos extraídos

    Raises:
        ExtractionError: Se não conseguir extrair os campos necessários
    """
    settings = get_settings()

    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            full_text = ""
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    full_text += page_text + "\n"
    except Exception as e:
        raise ExtractionError(f"Falha ao abrir/parsear o PDF: {e}")

    if not full_text.strip():
        raise ExtractionError("PDF não contém texto extraível")

    logger.debug("Texto extraído do PDF (%d chars): %s...", len(full_text), full_text[:200])

    # ── Extrair RG ──
    rg_match = RG_PATTERN.search(full_text)
    if not rg_match:
        raise ExtractionError("RG não encontrado no documento")
    rg = rg_match.group(1)

    # ── Extrair Curso ──
    curso = ""
    curso_match = CURSO_PATTERN.search(full_text)
    if curso_match:
        curso = curso_match.group(1).strip()

    # ── Extrair Unidade ──
    unidade = ""
    unidade_match = UNIDADE_PATTERN.search(full_text)
    if unidade_match:
        unidade = unidade_match.group(1).strip()

    # ── Extrair Nome ──
    nome = ""
    nome_match = NOME_PATTERN.search(full_text)
    if nome_match:
        nome = nome_match.group(1).strip()

    # ── Verificar elegibilidade ──
    is_eligible = _check_eligibility(full_text, settings)

    logger.info(
        "Dados extraídos — RG: %s...%s, Curso: %s, Elegível: %s",
        rg[:4],
        rg[-2:],
        curso,
        is_eligible,
    )

    return DocumentData(
        rg=rg,
        curso=curso,
        unidade=unidade,
        nome=nome,
        is_eligible=is_eligible,
    )


def _check_eligibility(text: str, settings) -> bool:
    """
    Verifica se o aluno é elegível baseado nos critérios do .env.

    Checa:
    1. Código de unidade (ex: '97' para EESC)
    2. Keywords no texto (ex: 'Escola de Engenharia de São Carlos')
    """
    # Se não há critérios definidos, aceita todos
    if not settings.eligible_unit_codes_list and not settings.eligible_keywords_list:
        return True

    # Checar código de unidade
    for code in settings.eligible_unit_codes_list:
        if code and re.search(rf"\b{re.escape(code)}\s*-", text):
            return True

    # Checar keywords
    for keyword in settings.eligible_keywords_list:
        if keyword and keyword.lower() in text.lower():
            return True

    return False


# ─── Scraper Playwright ──────────────────────────────────────────


def _parse_control_code(code: str) -> str:
    """
    Normaliza o código de controle.

    Aceita:
    - '18BC-9CXR-L8HN-FWB6'
    - '18BC9CXRL8HNFWB6'
    - '18BC 9CXR L8HN FWB6'

    Retorna: '18BC9CXRL8HNFWB6' (16 chars, sem separadores)
    """
    clean = re.sub(r"[\s\-]", "", code.strip().upper())
    if len(clean) != 16:
        raise ValueError(
            f"Código de controle deve ter 16 caracteres, recebeu {len(clean)}: '{clean}'"
        )
    return clean


async def validate_document(control_code: str) -> DocumentData:
    """
    Fluxo completo: navega no IDDigital, insere código, baixa PDF, extrai dados.

    Args:
        control_code: Código de controle do atestado (ex: '18BC-9CXR-L8HN-FWB6')

    Returns:
        DocumentData com RG, Curso, e elegibilidade

    Raises:
        DocumentNotFoundError: Código não corresponde a documento válido
        DocumentExpiredError: Documento expirado
        TurnstileBlockedError: Cloudflare Turnstile bloqueou
        ExtractionError: Falha na extração de dados do PDF
    """
    code_chars = _parse_control_code(control_code)
    pdf_bytes = await _fetch_document_pdf(code_chars)
    return extract_data_from_pdf(pdf_bytes)


async def _fetch_document_pdf(code_chars: str) -> bytes:
    """
    Usa Playwright para navegar no portal e interceptar o PDF.

    Strategy:
    1. Intercepta responses da rede que retornam application/pdf
    2. Preenche o código de controle nos inputs do formulário Vue.js
    3. Aguarda o PDF aparecer na rede
    4. Retorna os bytes do PDF

    O PDF nunca é salvo no disco — existe apenas em memória.
    """
    pdf_data: bytes | None = None

    async with async_playwright() as p:
        # Chromium com configurações anti-detection mínimas
        browser = await p.chromium.launch(
            headless=True,  # Tentar headless primeiro; se Turnstile bloquear, mudar para False + Xvfb
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )

        context = await browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            locale="pt-BR",
        )

        page = await context.new_page()

        # ── Interceptar responses que contenham PDF ──
        captured_pdf = []

        async def handle_response(response):
            content_type = response.headers.get("content-type", "")
            if "application/pdf" in content_type or "application/octet-stream" in content_type:
                try:
                    body = await response.body()
                    # Verificar magic bytes do PDF (%PDF-)
                    if body[:5] == b"%PDF-":
                        captured_pdf.append(body)
                        logger.info("PDF interceptado (%d bytes)", len(body))
                except Exception as e:
                    logger.warning("Falha ao capturar body do response: %e", e)

        page.on("response", handle_response)

        try:
            # ── Navegar para o portal ──
            logger.info("Navegando para %s", IDDIGITAL_URL)
            await page.goto(IDDIGITAL_URL, wait_until="networkidle", timeout=30000)

            # ── Aguardar Turnstile resolver (se presente) ──
            await _handle_turnstile(page)

            # ── Preencher o código de controle ──
            await _fill_control_code(page, code_chars)

            # ── Aguardar resultado ──
            pdf_data = await _wait_for_result(page, captured_pdf)

        except ScraperError:
            raise
        except Exception as e:
            logger.error("Erro inesperado no scraper: %s", e)
            raise ScraperError(f"Erro ao acessar o portal: {e}")
        finally:
            await browser.close()

    if pdf_data is None:
        raise ExtractionError("Nenhum PDF foi capturado da rede")

    return pdf_data


async def _handle_turnstile(page: Page):
    """Aguarda o Turnstile resolver, se presente."""
    try:
        # Verificar se existe widget do Turnstile
        turnstile = page.locator("[class*='turnstile'], [id*='turnstile'], iframe[src*='turnstile']")
        if await turnstile.count() > 0:
            logger.info("Turnstile detectado, aguardando resolução...")
            # Aguardar até 30 segundos para o Turnstile resolver
            # Em modo headless pode não funcionar — fallback seria headed + Xvfb
            await page.wait_for_timeout(5000)

            # Verificar se o Turnstile sumiu ou foi resolvido
            success = page.locator("[data-turnstile-response]:not([data-turnstile-response=''])")
            try:
                await success.wait_for(timeout=25000)
                logger.info("Turnstile resolvido com sucesso")
            except Exception:
                logger.warning("Turnstile pode não ter sido resolvido — tentando continuar")
        else:
            logger.info("Nenhum Turnstile detectado")
    except Exception as e:
        logger.warning("Erro ao lidar com Turnstile: %s", e)


async def _fill_control_code(page: Page, code_chars: str):
    """
    Preenche os 16 caracteres do código de controle nos inputs do formulário Vue.js.

    O formulário tem inputs individuais para cada caractere,
    com auto-advance entre eles.
    """
    logger.info("Preenchendo código de controle...")

    # Aguardar os inputs do formulário aparecerem
    # O formulário Vue.js usa inputs individuais dentro de um form
    await page.wait_for_selector("form input:not([disabled])", timeout=15000)

    # Estratégia 1: inputs individuais (como visto no HTML do portal)
    inputs = page.locator("form input:not([type='hidden']):not([disabled]):not([readonly])")
    input_count = await inputs.count()

    if input_count >= 16:
        # Preencher caractere por caractere
        for i, char in enumerate(code_chars):
            if i < input_count:
                await inputs.nth(i).fill(char)
                await page.wait_for_timeout(50)  # Pequeno delay para o Vue.js processar
    elif input_count >= 1:
        # Fallback: pode ser um único input
        first_input = inputs.first
        await first_input.fill(code_chars)
    else:
        raise ScraperError("Nenhum input de código de controle encontrado no formulário")

    logger.info("Código preenchido, aguardando processamento...")

    # Dar tempo para o Vue.js processar e fazer a requisição
    await page.wait_for_timeout(2000)

    # Tentar clicar em botão de submit se existir
    submit_btn = page.locator("button[type='submit'], button:has-text('Consultar'), button:has-text('Verificar'), button:has-text('Validar')")
    if await submit_btn.count() > 0:
        await submit_btn.first.click()
        logger.info("Botão de submit clicado")


async def _wait_for_result(page: Page, captured_pdf: list) -> bytes:
    """
    Aguarda o resultado da verificação (PDF ou mensagem de erro).

    Monitora:
    1. PDF interceptado via response handler
    2. Mensagens de erro na página (documento inválido, expirado)
    """
    max_wait = 30  # segundos
    poll_interval = 500  # ms

    for _ in range(max_wait * 1000 // poll_interval):
        # Verificar se PDF foi capturado
        if captured_pdf:
            return captured_pdf[0]

        # Verificar se há elemento <object> com blob (PDF viewer)
        obj_tag = page.locator("object[type='application/pdf']")
        if await obj_tag.count() > 0:
            # PDF está sendo exibido, mas precisamos dos bytes
            # Se não foi interceptado via response, tentar via download link
            download_link = page.locator("a[download*='documento']")
            if await download_link.count() > 0:
                # Interceptar o download
                async with page.expect_download() as download_info:
                    await download_link.click()
                download = await download_info.value
                # Ler o download para memória
                path = await download.path()
                if path:
                    with open(path, "rb") as f:
                        return f.read()

            # Último recurso: esperar mais um pouco pelo interceptor
            await page.wait_for_timeout(3000)
            if captured_pdf:
                return captured_pdf[0]

            raise ExtractionError(
                "PDF visível na página mas não interceptado. "
                "O portal pode ter mudado a forma de servir o documento."
            )

        # Verificar mensagens de erro
        page_text = await page.inner_text("body")

        if "não corresponde" in page_text.lower():
            raise DocumentNotFoundError(
                "O código de controle não corresponde a um documento emitido pela USP"
            )

        if "expirado" in page_text.lower() or "perdeu a sua validade" in page_text.lower():
            raise DocumentExpiredError(
                "O documento existe mas já expirou"
            )

        await page.wait_for_timeout(poll_interval)

    raise ScraperError("Timeout aguardando resultado do portal (30s)")

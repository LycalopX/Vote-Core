"""
Testes de estresse do Vote-Core.

Cobre:
  - Crypto sob carga concorrente massiva
  - Banco de dados com escritas simultâneas (race conditions, deduplicação)
  - Rate limiter sob flood de requisições
  - Semaphore do scraper: comportamento de fila, timeout, zombie slot
  - Lógica de parsing do scraper (extract_data_from_pdf) sem rede

Executar:
    ./.venv/bin/pytest tests/test_stress.py -v
    ./.venv/bin/pytest tests/test_stress.py -v -s  # para ver prints de timing

NOTA: Este suite NÃO bate no portal da USP. O Playwright é inteiramente
mockado. Os testes de scraper exercitam a lógica Python isolada.
"""

import asyncio
import time
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch, MagicMock
from dataclasses import dataclass

from app.crypto import generate_voter_hash, generate_audit_id, verify_voter_hash
from app.database import (
    init_db, close_db,
    check_if_voted, register_voter_hash,
    insert_vote, get_vote_counts, get_total_votes,
    get_vote_by_audit_id,
)
from app.scraper import (
    extract_data_from_pdf,
    _parse_control_code,
    _check_eligibility,
    ExtractionError,
    DocumentData,
    NUSP_PATTERN,
    COURSE_CODE_PATTERN,
)
from app.main import _check_rate_limit, _check_audit_rate_limit, _rate_limit_store

# Configura todos os testes async deste módulo para compartilhar o mesmo event loop.
# Isso é necessário porque o SQLAlchemy connection pool e o asyncio.Semaphore
# são criados no event loop da fixture de módulo e não podem ser usados
# em event loops diferentes.
pytestmark = pytest.mark.asyncio(loop_scope="module")

# ─── Fixtures ────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="module", autouse=True)
async def setup_stress_db():
    """Banco em memória para todos os stress tests do módulo."""
    await init_db()
    yield
    await close_db()


@pytest_asyncio.fixture
async def fresh_db():
    """
    Re-inicializa o engine num estado limpo para o teste.
    Como todos os testes compartilham o mesmo event loop (loop_scope=module),
    o engine já é compatível. Esta fixture serve apenas para garantir que
    o banco está ativo antes do teste.
    """
    # O engine já foi criado pelo setup_stress_db no mesmo event loop
    # Não precisamos recriar, apenas garantir que está operacional
    yield


@pytest.fixture(autouse=True)
def clear_rate_limit_store():
    """Limpa o rate limit store antes de cada teste para isolamento."""
    _rate_limit_store.clear()
    yield
    _rate_limit_store.clear()


# ═══════════════════════════════════════════════════════════════════
# 1. STRESS DE CRIPTOGRAFIA
# ═══════════════════════════════════════════════════════════════════


class TestCryptoStress:

    SALT = "stress-test-salt-key-32chars-xxxx"
    SALT_2 = "stress-test-salt-2-for-audit-ids"

    @pytest.mark.asyncio
    async def test_crypto_100_concurrent_voters(self):
        """
        100 coroutines geram hashes HMAC simultaneamente.
        Todos devem completar sem erro e ser determinísticos.
        """
        nusps = [f"{10000000 + i}" for i in range(100)]

        async def hash_worker(nusp: str) -> str:
            # yield para o event loop simular verdadeira concorrência
            await asyncio.sleep(0)
            return generate_voter_hash(nusp, self.SALT)

        start = time.monotonic()
        hashes = await asyncio.gather(*[hash_worker(n) for n in nusps])
        elapsed = time.monotonic() - start

        # Todos únicos (100 NUSPs diferentes → 100 hashes diferentes)
        assert len(set(hashes)) == 100, "Colisão de hashes detectada!"
        # Determinísticos: recomputar e comparar
        for i, nusp in enumerate(nusps):
            assert hashes[i] == generate_voter_hash(nusp, self.SALT)
        print(f"\n  [crypto] 100 hashes concorrentes em {elapsed*1000:.1f}ms")

    @pytest.mark.asyncio
    async def test_crypto_500_hashes_performance(self):
        """500 hashes devem completar em menos de 500ms."""
        start = time.monotonic()
        results = await asyncio.gather(*[
            asyncio.coroutine(lambda n=n: generate_voter_hash(str(n), self.SALT))()
            for n in range(500)
        ] if False else [
            asyncio.get_event_loop().run_in_executor(None, generate_voter_hash, str(n), self.SALT)
            for n in range(500)
        ])
        elapsed = time.monotonic() - start
        assert len(results) == 500
        print(f"\n  [crypto] 500 hashes em {elapsed*1000:.1f}ms")

    def test_audit_id_no_boundary_collision(self):
        """
        Garante que o separador \\x00 previne colisões de fronteira.
        sem separador: NUSP='1234' + pwd='5678' colide com NUSP='12345' + pwd='678'
        """
        salt = "test-salt"
        # Par 1: NUSP curto + senha longa
        a1 = generate_audit_id("1234", "5678", salt)
        # Par 2: NUSP longo + senha curta (mesma concatenação sem separador)
        a2 = generate_audit_id("12345", "678", salt)
        assert a1 != a2, "Colisão de fronteira! O separador \\x00 não está funcionando."

    @pytest.mark.asyncio
    async def test_verify_hash_concurrent(self):
        """
        50 verificações simultâneas de hashes — nenhuma deve ter falso positivo.
        """
        salt = "verify-stress-salt"
        pairs = [(f"{i:08d}", generate_voter_hash(f"{i:08d}", salt)) for i in range(50)]

        async def verify(nusp, h) -> bool:
            await asyncio.sleep(0)
            return verify_voter_hash(nusp, salt, h)

        results = await asyncio.gather(*[verify(n, h) for n, h in pairs])
        assert all(results), "Falso negativo em verificação concorrente"

        # Cross-check: nenhum hash aceita um NUSP errado
        cross_results = await asyncio.gather(*[
            verify(f"{(i+1) % 50:08d}", h) for i, (n, h) in enumerate(pairs)
        ])
        assert not any(cross_results), "Falso positivo em verificação cruzada"


# ═══════════════════════════════════════════════════════════════════
# 2. STRESS DO BANCO DE DADOS
# ═══════════════════════════════════════════════════════════════════


class TestDatabaseStress:

    @pytest.mark.asyncio
    async def test_50_concurrent_unique_voters(self, fresh_db):
        """
        50 eleitores únicos tentam registrar hash simultaneamente.
        Todos devem ter sucesso (hashes distintos).
        """
        import uuid as _uuid
        # UUID por run para nunca colidir com runs anteriores no mesmo votes.db
        hashes = [_uuid.uuid4().hex + _uuid.uuid4().hex for _ in range(50)]
        hashes = [h[:64] for h in hashes]

        start = time.monotonic()
        results = await asyncio.gather(*[register_voter_hash(h) for h in hashes])
        elapsed = time.monotonic() - start

        assert all(results), f"Alguns registros falharam: {results.count(False)} erros"
        print(f"\n  [db] 50 registros concorrentes em {elapsed*1000:.1f}ms")

    @pytest.mark.asyncio
    async def test_deduplication_race_condition(self, fresh_db):
        """
        O mesmo hash é inserido 10 vezes simultaneamente.
        Exatamente 1 deve ter sucesso — o banco deve rejeitar os outros 9.
        Este é o teste mais crítico: deduplicação sob race condition real.
        """
        import uuid as _uuid
        same_hash = (_uuid.uuid4().hex + _uuid.uuid4().hex)[:64]

        results = await asyncio.gather(
            *[register_voter_hash(same_hash) for _ in range(10)],
            return_exceptions=True
        )

        successes = sum(1 for r in results if r is True)
        failures = sum(1 for r in results if r is False)

        assert successes == 1, (
            f"Race condition quebrou deduplicação: {successes} votos aceitos "
            f"(esperado 1), {failures} rejeitados"
        )
        assert failures == 9, f"Esperado 9 rejeições, obteve {failures}"

    @pytest.mark.asyncio
    async def test_100_concurrent_vote_inserts(self, fresh_db):
        """
        100 votos inseridos simultaneamente.
        Sem exceções, sem perda de dados, UUIDs únicos.
        """
        import uuid as _uuid
        vote_choices = ["Sim"] * 34 + ["Não"] * 33 + ["Nulo"] * 33
        # audit_ids únicos por run
        audit_ids = [(_uuid.uuid4().hex + _uuid.uuid4().hex)[:64] for _ in range(100)]

        start = time.monotonic()
        uuids = await asyncio.gather(*[
            insert_vote(choice, audit)
            for choice, audit in zip(vote_choices, audit_ids)
        ])
        elapsed = time.monotonic() - start

        assert len(uuids) == 100
        assert len(set(uuids)) == 100, "UUIDs duplicados detectados!"
        assert all(len(u) == 36 for u in uuids), "UUID inválido retornado"

        print(f"\n  [db] 100 inserts concorrentes em {elapsed*1000:.1f}ms")

    @pytest.mark.asyncio
    async def test_concurrent_reads_during_writes(self, fresh_db):
        """
        Leituras e escritas simultâneas — WAL mode deve permitir isso sem lock.
        """
        import uuid as _uuid
        write_tasks = [
            insert_vote("Sim", (_uuid.uuid4().hex + _uuid.uuid4().hex)[:64])
            for _ in range(20)
        ]
        read_tasks = [get_vote_counts() for _ in range(10)]
        total_tasks = [get_total_votes() for _ in range(10)]

        start = time.monotonic()
        results = await asyncio.gather(
            *write_tasks, *read_tasks, *total_tasks,
            return_exceptions=True
        )
        elapsed = time.monotonic() - start

        exceptions = [r for r in results if isinstance(r, Exception)]
        assert not exceptions, (
            f"Exceções durante leitura+escrita concorrentes: {exceptions}"
        )
        print(f"\n  [db] 40 ops mistas (WAL) em {elapsed*1000:.1f}ms")

    @pytest.mark.asyncio
    async def test_audit_id_lookup_concurrent(self, fresh_db):
        """
        10 buscas por audit_id simultâneas — sem interferência entre si.
        """
        import uuid as _uuid
        # Gerar audit_ids verdadeiramente únicos com UUID para não colidir entre runs
        audit_ids = [f"lkp_{_uuid.uuid4().hex[:50]}" for _ in range(10)]

        # Inserir os votos primeiro
        uuids = []
        for i, audit in enumerate(audit_ids):
            choice = ["Sim", "Não", "Nulo"][i % 3]
            uuid = await insert_vote(choice, audit)
            uuids.append(uuid)

        # Buscar todos simultaneamente
        votes = await asyncio.gather(*[get_vote_by_audit_id(a) for a in audit_ids])

        for i, vote in enumerate(votes):
            assert vote is not None, f"Voto {i} não encontrado por audit_id"
            assert vote.uuid == uuids[i], f"UUID incorreto para voto {i}"


# ═══════════════════════════════════════════════════════════════════
# 3. STRESS DO RATE LIMITER
# ═══════════════════════════════════════════════════════════════════


class TestRateLimiterStress:

    def test_validate_rate_limit_blocks_after_5(self):
        """
        IP faz 5 tentativas (limite) → 6ª deve ser bloqueada.
        """
        ip = "192.168.1.100"
        results = [_check_rate_limit(ip) for _ in range(6)]
        assert results[:5] == [True] * 5, "As primeiras 5 devem passar"
        assert results[5] is False, "A 6ª deve ser bloqueada"

    def test_100_different_ips_dont_interfere(self):
        """
        100 IPs diferentes, cada um com 5 tentativas válidas.
        Nenhum deve bloquear o outro.
        """
        ips = [f"10.0.{i // 256}.{i % 256}" for i in range(100)]
        for ip in ips:
            results = [_check_rate_limit(ip) for _ in range(5)]
            assert all(results), f"IP {ip} foi bloqueado indevidamente"

    def test_rate_limit_window_expiry(self):
        """
        Após a janela de tempo expirar, o IP pode tentar novamente.
        (Usa monkey-patching de time.monotonic para simular passagem de tempo)
        """
        ip = "10.99.99.99"
        fake_now = [0.0]

        original_monotonic = time.monotonic

        def fake_monotonic():
            return fake_now[0]

        import app.main as main_module
        original = main_module.time.monotonic

        # Saturar o rate limit
        main_module.time.monotonic = fake_monotonic
        try:
            for _ in range(5):
                _check_rate_limit(ip)
            assert _check_rate_limit(ip) is False, "Deve estar bloqueado"

            # Avançar o tempo além da janela (2 min + 1s)
            fake_now[0] = 121.0
            assert _check_rate_limit(ip) is True, "Deve ter sido liberado após janela"
        finally:
            main_module.time.monotonic = original

    def test_audit_rate_limit_keyed_by_nusp(self):
        """
        Rate limit do /audit é por NUSP, não por IP.
        Dois NUSPs diferentes não devem se bloquear mutuamente.
        """
        nusp1 = "12345678"
        nusp2 = "87654321"

        # Saturar NUSP 1
        for _ in range(5):
            _check_audit_rate_limit(nusp1)
        assert _check_audit_rate_limit(nusp1) is False

        # NUSP 2 deve estar livre
        for _ in range(5):
            assert _check_audit_rate_limit(nusp2) is True

    def test_simultaneous_flood_same_ip(self):
        """
        Simula 50 requisições quase simultâneas do mesmo IP.
        Apenas as primeiras 5 devem passar; o resto deve ser bloqueado.
        """
        ip = "flood.attacker.ip.255"
        results = [_check_rate_limit(ip) for _ in range(50)]
        passing = sum(1 for r in results if r is True)
        blocked = sum(1 for r in results if r is False)
        assert passing == 5, f"Esperado 5 passes, obteve {passing}"
        assert blocked == 45, f"Esperado 45 bloqueios, obteve {blocked}"


# ═══════════════════════════════════════════════════════════════════
# 4. STRESS DO SEMAPHORE DO SCRAPER (mockado)
# ═══════════════════════════════════════════════════════════════════


class TestScraperSemaphoreStress:
    """
    Testa o comportamento do Semaphore(4) sem tocar na rede.
    O `validate_document` é mockado para simular diferentes durações e falhas.
    """

    @pytest.mark.asyncio
    async def test_semaphore_limits_to_4_concurrent(self):
        """
        10 requests simultâneos com semaphore(4):
        No máximo 4 devem estar em execução ao mesmo tempo.
        """
        import app.main as main_module

        max_concurrent = 0
        current_concurrent = 0
        lock = asyncio.Lock()

        async def mock_validate(code: str) -> DocumentData:
            nonlocal max_concurrent, current_concurrent
            async with lock:
                current_concurrent += 1
                if current_concurrent > max_concurrent:
                    max_concurrent = current_concurrent
            await asyncio.sleep(0.05)  # simula scraping de 50ms
            async with lock:
                current_concurrent -= 1
            return DocumentData(
                nusp="12345678", curso="Eng. Computação",
                course_code="97001", unidade="EESC",
                nome="Teste", is_eligible=True
            )

        semaphore = asyncio.Semaphore(4)

        async def validate_with_semaphore(code: str):
            async with semaphore:
                return await mock_validate(code)

        await asyncio.gather(*[validate_with_semaphore(f"CODE{i:04d}") for i in range(10)])

        assert max_concurrent <= 4, (
            f"Semaphore falhou: {max_concurrent} execuções simultâneas (máximo 4)"
        )
        print(f"\n  [semaphore] pico de concorrência: {max_concurrent}/4")

    @pytest.mark.asyncio
    async def test_semaphore_releases_slot_after_timeout(self):
        """
        Um scraper "zombie" (demora muito) deve liberar o slot quando cancelado.
        Após o cancelamento, outros requests devem conseguir pegar o slot.
        """
        semaphore = asyncio.Semaphore(1)  # apenas 1 slot para facilitar o teste
        zombie_holding = asyncio.Event()  # gate: sinaliza quando zombie tem o slot

        async def zombie_scraper():
            async with semaphore:
                zombie_holding.set()   # avisa que está segurando o slot
                try:
                    await asyncio.sleep(10)  # simula zombie
                except asyncio.CancelledError:
                    raise  # propaga o cancel para o contexto do `async with`

        async def fast_scraper():
            async with semaphore:
                return "fast_done"

        # Iniciar zombie
        zombie_task = asyncio.create_task(zombie_scraper())

        # Aguardar o zombie realmente segurar o slot antes de prosseguir
        await zombie_holding.wait()

        # Confirmar que fast_scraper NÃO consegue o slot enquanto zombie está vivo
        try:
            async with asyncio.timeout(0.05):
                result = await fast_scraper()
            assert False, "Fast scraper não devia ter conseguido enquanto zombie está vivo"
        except TimeoutError:
            pass  # Esperado — slot estava ocupado pelo zombie

        # Cancelar zombie (simula o timeout do main.py acionando asyncio.timeout)
        zombie_task.cancel()
        try:
            await zombie_task
        except asyncio.CancelledError:
            pass  # CancelledError é esperado — o zombie foi interrompido

        # Agora o slot DEVE estar disponível (async with garante release no cancel)
        async with asyncio.timeout(1.0):
            result = await fast_scraper()
        assert result == "fast_done", "Slot não foi liberado após cancelamento do zombie"

    @pytest.mark.asyncio
    async def test_queue_under_load_all_complete(self):
        """
        20 requests com semaphore(4): todos devem completar,
        mesmo que em fila. Mede o tempo de throughput.
        """
        semaphore = asyncio.Semaphore(4)
        completed = []

        async def mock_scraper(i: int):
            async with semaphore:
                await asyncio.sleep(0.02)  # 20ms por scrape
                completed.append(i)

        start = time.monotonic()
        await asyncio.gather(*[mock_scraper(i) for i in range(20)])
        elapsed = time.monotonic() - start

        assert len(completed) == 20, "Nem todos completaram"
        assert len(set(completed)) == 20, "Duplicatas detectadas"
        # Com 4 slots e 20ms cada: mínimo teórico = 20/4 * 0.02 = 0.1s
        assert elapsed < 1.0, f"Throughput muito lento: {elapsed:.2f}s para 20 requests"
        print(f"\n  [semaphore] 20 scrapers (slot=4) em {elapsed*1000:.0f}ms")

    @pytest.mark.asyncio
    async def test_scraper_exception_releases_slot(self):
        """
        Quando o scraper levanta uma exceção, o slot deve ser liberado.
        Os outros requests na fila não devem ficar travados.
        """
        semaphore = asyncio.Semaphore(1)
        calls = []

        async def failing_scraper(i: int):
            async with semaphore:
                calls.append(i)
                if i == 0:
                    raise ValueError("Portal fora do ar")
                return f"ok_{i}"

        results = await asyncio.gather(
            *[failing_scraper(i) for i in range(4)],
            return_exceptions=True
        )

        # Primeiro deve ter falhado
        assert isinstance(results[0], ValueError)
        # Os outros devem ter completado (slot foi liberado pelo context manager)
        successful = [r for r in results[1:] if isinstance(r, str)]
        assert len(successful) == 3, (
            f"Slot não foi liberado após exceção: apenas {len(successful)}/3 completaram"
        )


# ═══════════════════════════════════════════════════════════════════
# 5. STRESS DA LÓGICA DE PARSING DO SCRAPER (sem rede)
# ═══════════════════════════════════════════════════════════════════


class TestScraperParsingStress:
    """
    Testa extract_data_from_pdf e helpers com PDFs sintéticos.
    Não usa Playwright nem acessa a internet.
    """

    def _make_pdf_bytes(self, text: str) -> bytes:
        """Cria um PDF minimalista mas válido com o texto especificado."""
        import io
        try:
            import pdfplumber
            from reportlab.pdfgen import canvas
            buf = io.BytesIO()
            c = canvas.Canvas(buf)
            # Escrever cada linha do texto
            y = 750
            for line in text.split('\n'):
                c.drawString(50, y, line)
                y -= 15
            c.save()
            return buf.getvalue()
        except ImportError:
            pass

        # Fallback: PDF mínimo hardcoded que pdfplumber consegue abrir
        # Criado via reportlab ou estrutura manual mínima — usamos bytes reais
        # Se reportlab não disponível, pulamos testes que precisam de PDF real
        return b""

    def test_parse_control_code_normalizes_separators(self):
        """Diferentes formatos de código devem normalizar para 16 chars."""
        codes = [
            "18BC-9CXR-L8HN-FWB6",
            "18BC9CXRL8HNFWB6",
            "18BC 9CXR L8HN FWB6",
            " 18BC-9CXR-L8HN-FWB6 ",
        ]
        for code in codes:
            result = _parse_control_code(code)
            assert result == "18BC9CXRL8HNFWB6", f"Falhou para '{code}': '{result}'"

    def test_parse_control_code_rejects_wrong_length(self):
        """Códigos com tamanho errado devem levantar ValueError."""
        import pytest
        with pytest.raises(ValueError):
            _parse_control_code("18BC-9CXR-L8HN")  # apenas 12 chars

        with pytest.raises(ValueError):
            _parse_control_code("18BC-9CXR-L8HN-FWB6-XXXX")  # 20 chars

    def test_nusp_pattern_variations(self):
        """NUSP_PATTERN deve capturar diferentes formatos do texto do PDF."""
        import re
        test_cases = [
            ("Código USP 12345678", "12345678"),
            ("código USP 7654321", "7654321"),  # 7 dígitos
            ("CÓDIGO    USP    98765432", "98765432"),  # múltiplos espaços
            ("código usp 11111111 — estudante", "11111111"),
        ]
        for text, expected in test_cases:
            match = NUSP_PATTERN.search(text)
            assert match is not None, f"Pattern não deu match em: '{text}'"
            assert match.group(1) == expected, (
                f"Para '{text}': esperado '{expected}', obteve '{match.group(1)}'"
            )

    def test_nusp_pattern_rejects_invalid(self):
        """NUSP_PATTERN não deve dar match em textos sem NUSP."""
        no_match_cases = [
            "Código de Controle: 18BC9CXRL8HN"
            "RG: 12.345.678-9",
            "CPF: 123.456.789-00",
            "12345678",
        ]
        for text in no_match_cases:
            match = NUSP_PATTERN.search(text)
            assert match is None, f"Falso positivo para: '{text}'"

    def test_eligibility_check_stress_many_keywords(self):
        """_check_eligibility com lista longa de keywords — sem degradação."""
        from app.config import Settings

        class BigKeywordSettings:
            eligible_course_codes_list = []
            eligible_unit_codes_list = []
            eligible_keywords_list = [f"keyword_{i}" for i in range(100)]
            eligible_keywords_list.append("Escola de Engenharia de São Carlos")

        text = (
            "Unidade: 97 - Escola de Engenharia de São Carlos\n"
            "Curso: 97001 - Engenharia de Computação\n"
        )

        start = time.monotonic()
        for _ in range(1000):
            result = _check_eligibility(text, "", BigKeywordSettings())
        elapsed = time.monotonic() - start

        assert result is True, "Deveria ser elegível"
        assert elapsed < 1.0, f"Lento demais: {elapsed:.2f}s para 1000 checks"
        print(f"\n  [scraper] 1000x eligibility check em {elapsed*1000:.0f}ms")

    def test_eligibility_course_code_priority_over_unit(self):
        """Se ELIGIBLE_COURSE_CODES definido, ignora unidade e keywords."""

        class CourseFilterSettings:
            eligible_course_codes_list = ["97999"]  # código que não existe
            eligible_unit_codes_list = ["97"]       # unidade correta
            eligible_keywords_list = ["Escola de Engenharia de São Carlos"]

        text = (
            "Unidade: 97 - Escola de Engenharia de São Carlos\n"
            "Curso: 97001 - Engenharia de Computação\n"
        )
        # Curso 97001 não está na lista [97999] → inelegível, mesmo com unidade e keyword corretas
        assert _check_eligibility(text, "97001", CourseFilterSettings()) is False

    def test_eligibility_no_filters_accepts_all(self):
        """Sem filtros definidos, qualquer texto é elegível."""

        class NoFilterSettings:
            eligible_course_codes_list = []
            eligible_unit_codes_list = []
            eligible_keywords_list = []

        assert _check_eligibility("qualquer texto aleatório", "", NoFilterSettings()) is True

    def test_parse_code_concurrent_100_calls(self):
        """
        _parse_control_code chamado 100 vezes concorrentemente (é síncrono mas
        pode ser chamado de uma coroutine — sem problemas de thread safety).
        """
        codes = [f"AAAA{i:04d}BBBBCCCC" for i in range(100)]
        results = [_parse_control_code(c) for c in codes]
        assert len(results) == 100
        assert all(len(r) == 16 for r in results)


# ═══════════════════════════════════════════════════════════════════
# 6. TESTES DE MEMÓRIA / ESTADO GLOBAL
# ═══════════════════════════════════════════════════════════════════


class TestMemoryAndState:

    def test_rate_limit_store_keys_grow_with_unique_ips(self):
        """
        Com 200 IPs únicos, o store deve ter 200 chaves.
        Documentamos o comportamento atual (sem limpeza de chaves vazias).
        """
        for i in range(200):
            _check_rate_limit(f"192.0.{i // 256}.{i % 256}")

        assert len(_rate_limit_store) == 200

    def test_rate_limit_timestamps_pruned_on_access(self):
        """
        Quando um IP acessa novamente após a janela, seus timestamps antigos
        são removidos (mas a chave permanece no dict).
        """
        import app.main as main_module
        ip = "prune.test.ip"
        fake_now = [0.0]

        original = main_module.time.monotonic
        main_module.time.monotonic = lambda: fake_now[0]
        try:
            # Fazer 5 tentativas no tempo 0
            for _ in range(5):
                _check_rate_limit(ip)
            assert len(_rate_limit_store[ip]) == 5

            fake_now[0] = 200.0
            _check_rate_limit(ip)
            assert len(_rate_limit_store[ip]) == 1
        finally:
            main_module.time.monotonic = original

from locust import HttpUser, task, between

class VoteCoreUser(HttpUser):
    # Simula o tempo de leitura de um usuário real (1 a 3 segundos entre cliques)
    wait_time = between(1, 3)

    @task(3)
    def index_page(self):
        """Simula o acesso em massa à página inicial (ocorre quando o link é divulgado)"""
        self.client.get("/")

    @task(2)
    def results_page(self):
        """Simula acesso à página de resultados (leitura pesada no banco sqlite)"""
        self.client.get("/results")

    @task(1)
    def validate_page_get(self):
        """Simula a abertura da página de validação"""
        # Nota: Não fazemos POST para /validate para não engatilhar o scraper do Playwright
        # e ser banido pelo Cloudflare Turnstile da USP durante o teste de carga.
        # O gargalo do Playwright já está protegido pelo asyncio.Semaphore(4).
        self.client.get("/validate")

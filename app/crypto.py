"""
Módulo de criptografia para deduplicação e auditoria de votos.

Dois hashes distintos, com duas SALTs distintas:
  - id_voto    = HMAC(NUSP, SALT_KEY)          → deduplicação (Tabela 1)
  - audit_id   = HMAC(NUSP + senha, SALT_2)    → auditoria pessoal (Tabela 2)

Nenhum dado bruto do eleitor toca o disco — apenas os hashes gerados aqui.
"""

import hmac
import hashlib


def generate_voter_hash(nusp: str, salt_key: str) -> str:
    """
    Gera HMAC-SHA256 irreversível do NUSP + salt para deduplicação.

    Args:
        nusp: Número USP do eleitor (7–8 dígitos, extraído do atestado)
        salt_key: Chave secreta SALT_KEY do .env

    Returns:
        Hash hexadecimal de 64 caracteres (256 bits)
    """
    nusp_clean = nusp.strip()

    if not nusp_clean:
        raise ValueError("NUSP não pode ser vazio")

    return hmac.new(
        key=salt_key.encode("utf-8"),
        msg=nusp_clean.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()


def generate_audit_id(nusp: str, password: str, salt_2: str) -> str:
    """
    Gera HMAC-SHA256 do NUSP concatenado com a senha pessoal do eleitor.

    Permite auditoria pessoal: o eleitor pode verificar seu próprio voto
    informando NUSP + senha, sem expor nenhum dado ao servidor além do hash.

    Args:
        nusp: Número USP do eleitor
        password: Senha pessoal criada pelo eleitor na hora de votar
        salt_2: Chave secreta SALT_2 do .env (separada de SALT_KEY)

    Returns:
        Hash hexadecimal de 64 caracteres — armazenado na Tabela 2
    """
    msg = (nusp.strip() + password).encode("utf-8")

    return hmac.new(
        key=salt_2.encode("utf-8"),
        msg=msg,
        digestmod=hashlib.sha256,
    ).hexdigest()


def verify_voter_hash(nusp: str, salt_key: str, stored_hash: str) -> bool:
    """
    Verifica se um NUSP corresponde a um hash armazenado (timing-safe).

    Args:
        nusp: NUSP bruto a verificar
        salt_key: Chave secreta SALT_KEY
        stored_hash: Hash armazenado na Tabela 1

    Returns:
        True se o NUSP corresponde ao hash
    """
    computed = generate_voter_hash(nusp, salt_key)
    return hmac.compare_digest(computed, stored_hash)

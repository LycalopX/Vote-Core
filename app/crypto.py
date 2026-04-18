"""
Módulo de criptografia para deduplicação de votos.

Gera HMAC-SHA256 irreversível a partir do RG do eleitor + SALT_KEY secreta.
O hash resultante é a única coisa armazenada — o RG bruto nunca toca o disco.
"""

import hmac
import hashlib
import re


def normalize_rg(rg: str) -> str:
    """
    Normaliza o RG removendo pontos, traços e espaços.

    '13.560.200-9' → '135602009'
    '13560200-9'   → '135602009'
    '13 560 200 9' → '135602009'
    """
    return re.sub(r"[.\-\s]", "", rg.strip())


def generate_voter_hash(rg: str, salt_key: str) -> str:
    """
    Gera HMAC-SHA256 irreversível do RG + salt.

    Args:
        rg: RG bruto extraído do atestado (ex: '13.560.200-9')
        salt_key: Chave secreta compartilhada (do .env SALT_KEY)

    Returns:
        Hash hexadecimal de 64 caracteres (256 bits)

    Exemplo:
        >>> generate_voter_hash('13.560.200-9', 'minha-chave')
        'a1b2c3d4...'  # 64 chars hex
    """
    rg_normalized = normalize_rg(rg)

    if not rg_normalized:
        raise ValueError("RG não pode ser vazio após normalização")

    return hmac.new(
        key=salt_key.encode("utf-8"),
        msg=rg_normalized.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()


def verify_hash_match(rg: str, salt_key: str, stored_hash: str) -> bool:
    """
    Verifica se um RG corresponde a um hash armazenado.
    Usa comparação timing-safe para prevenir timing attacks.

    Args:
        rg: RG bruto a verificar
        salt_key: Chave secreta SALT_KEY
        stored_hash: Hash armazenado na Tabela 1

    Returns:
        True se o RG corresponde ao hash
    """
    computed = generate_voter_hash(rg, salt_key)
    return hmac.compare_digest(computed, stored_hash)

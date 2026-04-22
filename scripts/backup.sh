#!/bin/bash
# ─────────────────────────────────────────────────────────
# Backup seguro do banco de dados Vote-Core (modo WAL)
#
# ATENÇÃO: NÃO use cp/rsync para copiar votes.db!
# O SQLite em modo WAL mantém dados em -wal e -shm que
# podem não estar sincronizados. Este script usa a API
# nativa de backup do SQLite via Python (não requer sqlite3 CLI).
#
# Uso:
#   ./scripts/backup.sh                     # backup padrão
#   ./scripts/backup.sh /caminho/custom     # diretório custom
#
# Recomendação: rodar via cron durante a assembleia
#   */15 * * * * /home/lycalopx/repos/Vote-Core/scripts/backup.sh
# ─────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
DB_PATH="$PROJECT_DIR/votes.db"
BACKUP_DIR="${1:-$PROJECT_DIR/backups}"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
BACKUP_FILE="$BACKUP_DIR/votes_backup_${TIMESTAMP}.db"

mkdir -p "$BACKUP_DIR"

if [ ! -f "$DB_PATH" ]; then
    echo "❌ Banco não encontrado: $DB_PATH"
    exit 1
fi

# Backup ACID-safe via API nativa do Python sqlite3
# Funciona mesmo com o banco aberto e recebendo escritas
python3 << PYEOF
import sqlite3, sys

src = sqlite3.connect("$DB_PATH")
dst = sqlite3.connect("$BACKUP_FILE")
src.backup(dst)
dst.close()
src.close()

# Verificar integridade
check = sqlite3.connect("$BACKUP_FILE")
result = check.execute("PRAGMA integrity_check").fetchone()[0]
votes = check.execute("SELECT COUNT(*) FROM votes").fetchone()[0]
check.close()

if result == "ok":
    print(f"✅ Backup OK: $BACKUP_FILE ({votes} votos)")
else:
    print(f"❌ Backup CORROMPIDO: {result}")
    sys.exit(2)
PYEOF

# Limpar backups antigos (manter últimos 20)
cd "$BACKUP_DIR"
ls -1t votes_backup_*.db 2>/dev/null | tail -n +21 | xargs -r rm -f

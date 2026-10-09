"""Explicit one-time migration; no application startup or exchange clients."""
import argparse
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import asyncpg

from backend.app import email_verification


def run_local(sqlite_path: Path, snapshot_path: Path) -> int:
    state = json.loads(snapshot_path.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or not isinstance(state.get("users"), list):
        raise TypeError("Geçerli bir hesap snapshot dosyası gerekli.")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        db_pool=None, account_settings_path=sqlite_path,
        v22_commercial={"state": state},
    )))
    return email_verification.migrate_local_accounts(request)


async def run_postgres() -> None:
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise ValueError("DATABASE_URL gerekli; veritabanı ayarını backend ortamında tanımla.")
    db = await asyncpg.connect(url)
    try:
        sql = Path(__file__).parents[1] / "migrations" / "20261009_001_email_verification_v2.sql"
        await db.execute(sql.read_text(encoding="utf-8"))
    finally:
        await db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Tek seferlik e-posta doğrulama geçişi")
    parser.add_argument("--sqlite", type=Path)
    parser.add_argument("--snapshot", type=Path)
    args = parser.parse_args()
    if bool(args.sqlite) != bool(args.snapshot):
        parser.error("Yerel geçiş için --sqlite ve --snapshot birlikte gerekli.")
    if args.sqlite:
        count = run_local(args.sqlite, args.snapshot)
        print(f"Yerel geçiş tamamlandı; {count} mevcut hesap güncellendi.")
    else:
        asyncio.run(run_postgres())
        print("PostgreSQL geçişi tamamlandı. Önceden uygulanmışsa yeni hesaplar değiştirilmedi.")


if __name__ == "__main__":
    main()

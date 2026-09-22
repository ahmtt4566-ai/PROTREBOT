import unittest
from pathlib import Path

ROOT = Path(__file__).parents[2]
MASTER = ROOT / 'MasterTrade.tsx'
SCHEMA = ROOT / 'database' / 'init.sql'


class MasterTradeV2SafetyTests(unittest.TestCase):
    def test_master_trade_has_v2_persistence_and_recovery_signals(self):
        source = MASTER.read_text(encoding='utf-8')
        self.assertIn('MASTER TRADE V2', source)
        self.assertIn('PERSISTENT HISTORY', source)
        self.assertIn('LIVE ACCOUNT SNAPSHOT', source)
        self.assertNotIn('DEMO ACCOUNT', source)
        self.assertIn('RECOVERY', source)
        self.assertIn('LIVE TRADING LOCKED', source)

    def test_database_schema_includes_trade_history_and_lifecycle_tables(self):
        source = SCHEMA.read_text(encoding='utf-8')
        self.assertIn('master_trade_trade_history', source)
        self.assertIn('master_trade_lifecycle_events', source)
        self.assertIn('master_trade_snapshot_cache', source)


if __name__ == '__main__':
    unittest.main(verbosity=2)

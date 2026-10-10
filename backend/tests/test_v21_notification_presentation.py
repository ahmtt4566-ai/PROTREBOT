import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from app import v21_demo


@pytest.mark.parametrize("kind,severity,title,target", [
    ("ACİL KORUMA", "critical", "Acil koruma gerekiyor", "risk-management"),
    ("AUTO_LOOP_CRASH", "critical", "Otomasyon beklenmedik şekilde durdu", "execution-status"),
    ("AUTO_START_ERROR", "critical", "Otomasyon başlatılamadı", "execution-status"),
    ("DAILY_LOSS_5", "warning", "Günlük zarar %5'e ulaştı", "risk-management"),
    ("DAILY_LOSS_10", "warning", "Günlük zarar %10'a ulaştı", "risk-management"),
    ("DAILY_LOSS_15", "critical", "Günlük zarar %15'e ulaştı", "risk-management"),
    ("DAILY_LOSS_20", "critical", "Günlük zarar %20'ye ulaştı", "risk-management"),
    ("KILL_SWITCH", "critical", "Acil durdurma koruması etkin", "risk-management"),
    ("CONSECUTIVE_LOSSES", "critical", "Ardışık zarar sınırına ulaşıldı", "risk-management"),
    ("ROTATION", "success", "Pozisyon rotasyonla kapatıldı", "trade-history"),
    ("SCAN_STARTED", "info", "Piyasa taraması başladı", "execution-status"),
    ("AUTO_STARTED", "success", "Demo otomasyonu başlatıldı", "execution-status"),
    ("AUTO_STOPPED", "info", "Demo otomasyonu durduruldu", "execution-status"),
])
def test_each_event_has_explicit_turkish_presentation(kind, severity, title, target):
    state = v21_demo.initial_state()
    v21_demo.emit_notification(state, kind, "Original message", event_id="event-a")
    item = v21_demo.notification_payload(state)[0]
    assert (item["severity"], item["title"], item["target"]) == (severity, title, target)
    assert item["type"] == kind
    assert item["message"] == "Original message"


@pytest.mark.parametrize("kind", ["UNKNOWN_LOSS", "API_ERROR", "RISK_FAIL_STOP", "new_future_event"])
def test_unknown_type_never_uses_keyword_guessing(kind):
    state = v21_demo.initial_state()
    v21_demo.emit_notification(state, kind, "Original message", event_id="event-a")
    item = v21_demo.notification_payload(state)[0]
    assert item["severity"] == "info"
    assert item["title"] == "Sistem bildirimi"
    assert item["target"] == "execution-status"


def test_old_records_get_new_titles_without_mutating_storage_or_risk():
    state = v21_demo.initial_state()
    state["journal"] = [{
        "id": "old-id", "kind": "NOTIFICATION", "reason": "DAILY_LOSS_5:old-event",
        "title": "DAILY LOSS 5", "severity": "error", "message": "Eski mesaj",
        "created_at": "2026-01-01T00:00:00+00:00",
    }]
    state["notifications"]["read_ids"] = ["old-id"]
    before = copy.deepcopy(state)
    item = v21_demo.notification_payload(state)[0]
    assert item["title"] == "Günlük zarar %5'e ulaştı"
    assert item["severity"] == "warning"
    assert item["message"] == "Eski mesaj"
    assert item["read"]
    assert state == before

import time

import pytest

from app.campaign_content import CampaignDraft, content_hash, email_text, token_user, unsubscribe_token, validate_text, warnings


@pytest.mark.parametrize("subject,body", [
    ("Konu\nBcc:secret", "Duyuru"), ("Konu", "<b>HTML</b>"), ("Konu", "![resim](https://kaistrade.com/a)"),
    ("Konu", "https://foreign.test/a"), ("Konu", "https://kaistrade.com@foreign.test/a"),
    ("Konu", "//foreign.test"), ("Konu", "Subject: injection"), ("Konu", "www.foreign.test"), ("Konu", "mailto:private@test.test"),
])
def test_plain_text_and_canonical_only_links(subject, body):
    with pytest.raises(ValueError):
        validate_text(subject, body)


def test_warning_is_small_shared_list_not_a_block_and_hash_is_content_bound():
    row = CampaignDraft(title="İç ad", subject="Bilgilendirme", body="Garanti getiri %20 kazanç", audience="all_users").model_dump()
    validate_text(row["subject"], row["body"])
    assert len(warnings(row)) == 1
    assert content_hash(row) != content_hash({**row, "body": "Farklı içerik"})
    validate_text("Bilgilendirme", "Yeni özellik.\n\nhttps://kaistrade.com/settings")


def test_signature_cannot_be_transferred_tampered_or_used_as_auth_token():
    secret = b"offline-announcement-secret"
    token = unsubscribe_token(secret, "member")
    assert token_user(secret, token) == "member"
    assert token_user(b"other-secret", token) is None
    assert token_user(secret, token + "x") is None
    assert token_user(secret, unsubscribe_token(secret, "member", int(time.time()) - 181 * 86400)) is None
    assert token_user(secret, unsubscribe_token(secret, "member", int(time.time()) + 100)) is None
    text = email_text({"body": "Paragraf\n\nİkinci paragraf"}, token)
    assert "https://kaistrade.com/announcements/unsubscribe" in text and "yatırım tavsiyesi değildir" in text

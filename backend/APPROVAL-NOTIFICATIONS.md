# Aşama 8 — Onay bildirimleri outbox

Dal: `stage8-notifications`, başlangıç: `stage7-approvals` (`97fa4fb`).
Migration dosyası yazıldı; veritabanına uygulanmadı. Push/deploy yapılmadı.

## Adım 0 bulguları

[email_service.py](app/email_service.py) değişmedi:

- Resend yolu `EMAIL_PROVIDER=resend`, `RESEND_API_KEY`, `EMAIL_FROM` gerektirir.
- Gönderen `KaisTrade <EMAIL_FROM adresi>`; opsiyonel yanıt adresi `EMAIL_REPLY_TO`.
- `validate_configuration`, `validate_app_base_url` ve `validate_action_url` mevcut
  doğrulama yardımcılarıdır. Production origin yalnız `https://kaistrade.com`.
- Gönderim `httpx.Client(timeout=10.0, follow_redirects=False)` kullanır. 2xx ve
  boş olmayan sağlayıcı mesaj kimliği gerekir; aksi halde `EmailDeliveryError`.
  Mevcut hata log'ları alıcıyı maskeler ve içerik/credential yazmaz.
- Genel HTML+metin göndericisi yoktur. `send_auth_email` doğrulama, parola ve
  güvenlik metinlerine bağlıdır. Onay bildirimine bu metinleri eklememek için
  [notification_email.py](app/notification_email.py) adlı küçük ayrı sarmalayıcı
  mevcut doğrulama fonksiyonlarını çağırır ve aynı Resend HTTP sözleşmesini kullanır.
  Auth gönderim fonksiyonları, mevcut şablonlar ve giriş akışı değiştirilmedi.

[main.py](app/main.py) lifespan içinde yönetilen `asyncio` görevleri zaten vardır.
Startup'ta bir kez, başarılı onay transaction'ından sonra ve admin liste/özet
çağrılarında bounded sweep tetiklenir. Her sweep en fazla 10 gönderim claim'i ve
10 eski lease kurtarması işler. Aktif görev ve 15 saniyelik cooldown coalesce eder.

Digest ve backoff gelecekte hazır olduğundan, sweep kuyruğun en yakın
`next_attempt_at` değerine hafif bir event-loop timer kurar. Böylece uyanık
serviste ilk digest için OWNER'ın paneli açması şart değildir. Yeni karar,
uzaktaki digest timer'ını cooldown sonuna çekebilir. Kuyruk boşsa timer yoktur;
shutdown timer/görevi iptal eder. Render uyursa süreç/timer kalıcılık sağlamaz:
DB outbox kalır, sonraki startup veya ilgili endpoint kuyruğu yeniden işler.

## Migration ve transaction sınırları

[20261010_008_notification_outbox.sql](migrations/20261010_008_notification_outbox.sql):

- FK yok; kind allowlist: `approval.pending_digest`, `approval.decision`.
- Outbox alıcı e-postasını, gerekçe, karar notu veya isim saklamaz.
- Digest payload: `{bucket: sayı}`, ilk claim'de eklenen sabit `{count: sayı}`.
- Karar payload: `{approval_id: opaque ID, result: sayı}`.
  Sonuç kodları: 1 onaylandı, 2 reddedildi, 3 stale, 4 failed.
- Benzersiz dedupe: digest `kind:alıcı:10-dakika-dilimi`; karar `kind:talep:sonuç`.
- Talep oluşturma aktif OWNER'lar için digest'i dilim sonunda hazır yapar.
  Aynı dilimde beş talep alıcı başına tek outbox kaydı oluşturur.
- Karar/audit geçişleri, requester'a ilgili karar kaydını oluşturur.
- Enqueue, **aynı bağlantının aynı outer transaction'ında nested savepoint** kullanır.
  Enqueue hatası savepoint'i geri alır ve yalnız exception tipi loglanır;
  talep, karar, audit veya canonical hesap değişikliğini geri almaz.
  Outer transaction gerçekten başarısızsa outbox da commit olmaz.
- Sent/dead immutable; geçişler ve attempts sınırı trigger ile korunur.
  DELETE/TRUNCATE engellidir. Erasure tombstone'u dar nested istisnayla
  recipient/payload/dedupe alanlarını temizler; gönderilmemiş kayıt dead olur.

## Gönderici ve gizlilik

[notification_worker.py](app/notification_worker.py):

- `FOR UPDATE SKIP LOCKED` ile atomik `sending` claim, attempts +1, 120 saniyelik lease.
- Attempts ile fencing: eski lease'in göndericisi tekrar gönderemez.
- Süresi geçmiş sending: attempts <8 ise pending, aksi halde dead.
- Canonical alıcı adresi gönderim anında okunur; active/verified ve erasure
  kontrolü yapılır. Digest alıcısı hâlâ OWNER olmalıdır. Karar, rolü değişmiş
  olsa da aktif requester'a genel bildirim olarak gönderilebilir.
- Delivery transaction'ı outbox satır kilidini gönderim boyunca tutar;
  erasure ile alıcı kontrolü arasına gönderim yarışı girmez.
- Başarıda sent; hata halinde failed, 8. hata/lease exhaustion sonrasında dead.
- Backoff: 1 dk, 5 dk, 15 dk, 1 saat, sonra en fazla 6 saat aralık.
- Hata alanı yalnız kısa allowlist kodudur; mesaj, adres ve token loglanmaz.
- `APPROVAL_EMAIL_ENABLED=false` veya eksik/Resend olmayan yapılandırmada
  `provider_unavailable`; hiçbir şekilde sent sayılmaz.
- HTML+metin aynı genel Türkçe mesajı ve yalnız canonical `/admin` veya
  `/moderator` bağlantısını içerir. Onayla/red endpoint'i veya işlem token'ı yoktur.
  Subject da geneldir. Alıcı e-postası yalnız sağlayıcı envelope'unda bulunur.
- Sabit hash `Idempotency-Key` kullanılır; digest count ilk claim'de dondurulur,
  böylece tekrar denemede aynı içerik sağlanır.

**Dağıtık gönderim sınırı:** Resend'in [resmî idempotency belgesine](https://resend.com/docs/dashboard/emails/idempotency-keys)
göre anahtarlar 24 saat tutulur. Normal eşzamanlı worker'lar aynı satırı iki kez
göndermez. Sağlayıcı kabulü ile DB sent commit'i arasındaki crash'te retry,
bu 24 saatlik pencere içinde sağlayıcı tarafından tekilleştirilir. Pencereyi
aşan servis kesintisinde mutlak exactly-once teslimat garantisi verilmez.
Sent, sağlayıcının kabulünü ifade eder; posta kutusuna teslimat/webhook takibi değildir.

## OWNER ekranı

Yeni [notification_admin.py](app/notification_admin.py):
`GET /api/v22/admin/approvals/notifications/summary`, canonical OWNER-only.
Gerçek pending+sending ve failed+dead sayıları; ERASED kayıtlar sayılmaz.
Storage hatası savepoint ile izole edilir, güvenli uyarı ve null sayılar döner.

[AdminApprovals.tsx](../AdminApprovals.tsx) yalnız iki satırla ayrı
[AdminNotificationSummary.tsx](../AdminNotificationSummary.tsx) bileşenine bağlandı.
Satır: “E-posta bildirimi: N bekliyor, N gönderilemedi”.
Veri yoksa “veri yok”; hatada Türkçe mesaj ve yalnız GET yenileme vardır.
Başka admin ekranı veya admin CSS değiştirilmedi.

## Değişen dosyalar

- Ürün backend: [notification_outbox.py](app/notification_outbox.py),
  [notification_email.py](app/notification_email.py), [notification_worker.py](app/notification_worker.py),
  [notification_admin.py](app/notification_admin.py).
- Bağlantılar: [moderator_approvals.py](app/moderator_approvals.py) enqueue,
  [approval_service.py](app/approval_service.py) karar enqueue/post-commit tetik,
  [main.py](app/main.py) yeni router/startup/shutdown.
- SQL: [008](migrations/20261010_008_notification_outbox.sql).
- Backend testleri: [migration](tests/test_notification_migration.py),
  [outbox](tests/test_notification_outbox.py), [worker](tests/test_notification_worker.py),
  [mail adapter](tests/test_notification_email.py).
- Fixture/inventory: [test_moderator_role.py](tests/test_moderator_role.py),
  [test_moderator_access.py](tests/test_moderator_access.py).
- Frontend: [AdminApprovals.tsx](../AdminApprovals.tsx),
  [AdminNotificationSummary.tsx](../AdminNotificationSummary.tsx),
  [notification-model.ts](../notification-model.ts), [notification-ui.tsx](../notification-ui.tsx),
  [notification-summary.test.mjs](../tools/notification-summary.test.mjs).

## Doğrulama

- Baseline: **434 backend**, **27 frontend** geçti; strict TS ve build temiz.
- Final: **477 backend**, 0 başarısız; 434 eski test korunuyor, kayıp 0,
  43 yeni test/OWNER inventory parametresi.
- Mevcut email service, URL policy, verification v2 ve account settings
  (parola sıfırlama dahil) testleri baseline/finalde aynen çalıştırıldı.
- Frontend: **30 geçti**, 0 başarısız. Strict frontend/access TS, hedefli ESLint,
  `COIN_LOGO_OFFLINE=1` production build başarılı.
- Pylance yeni dört backend ürün dosyasında syntax hatası bulmadı.
- Offline gerçek Chromium: doğru sayılar, no-data, 503 güvenli mesaj,
  GET-only retry ve sıfır page error. Ekran görüntüsü repo dışındadır.
- `git diff 97fa4fb -- email_service.py email_verification.py account_settings.py
  v22_commercial.py v24_commerce.py admin.css AdminPanel.tsx AuthGate.tsx` ilgili
  gerçek yollarla çalıştırıldığında değişiklik üretmez.
- Testler gerçek ürün kodu ve offline canonical transaction modeli kullanır.
  SQL trigger kuralları dosya sözleşmesiyle doğrulandı; gerçek PostgreSQL
  migration/trigger/isolation/erasure-lock testi **yapılmadı**.
- Gerçek Resend gönderimi, provider credential doğrulaması ve Render sleep/resume
  testi **yapılmadı**. Yalnız resmî açık idempotency dokümanı okundu.
- Migration uygulaması, push/deploy, Stripe/abonelik/ödeme/trading/kasa/giriş veya
  müşteri destek akışı değişikliği yoktur. Email service dosyası değişmemiştir.

XML, build log'u, Chromium script'i ve ekran görüntüsü oturum artifact klasöründe;
commit'e dahil değildir.

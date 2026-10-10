# Aşama 7 — Hesap durumu onayları

Aşama 8 bildirim/outbox uzantısı: [APPROVAL-NOTIFICATIONS.md](APPROVAL-NOTIFICATIONS.md).

Yerel dal: `stage7-approvals`, başlangıç: `stage6-support` (`3f2ec7e`).
Migration uygulanmadı; push ve deploy yapılmadı.

## Adım 0: mevcut işlemin etkileri

Mevcut [v22_customer_status](app/v22_commercial.py) (`POST /api/v22/customers/{user_id}/status`):

- OWNER yetkisini doğrular; OWNER hedefini reddeder.
- Pasifleştirmede mevcut açık pozisyon, emir, otomasyon, bağlantı ve recovery kontrollerini kullanır.
- Canonical `commercial_auth_users.security.active` alanını değiştirir ve `auth_version` artırır. Önceki oturum tokenları geçersizleşir.
- Pasifleştirmede aktif ajanları snapshot'ta iptal eder, token sürümlerini artırır. Aktifleştirme ajanları geri açmaz.
- Snapshot audit, yerel kayıt ve snapshot DB kalıcılığı canonical değişiklikten ayrıdır.
- Abonelik kaydını veya Stripe'ı değiştirmez.

**Ayrı, düzeltilmemiş bulgu:** Bu endpoint atomik ve idempotent değildir.
Offline gerçek endpoint reproducer'ında `active=true, auth_version=1` başlangıcından
yerel kayıt hatasıyla HTTP 503 dönmesine rağmen canonical durum `false, 2` kalmıştır.
Aynı pasifleştirme tekrarında HTTP 200 ile sürüm `3` olmuştur.
Mevcut endpoint ve yardımcı fonksiyonların gövdeleri bu aşamada değiştirilmedi.

Kullanıcı onayıyla eski endpoint yerine ayrı [approval_executor.py](app/approval_executor.py)
yazıldı. Korunan işlem kontrolleri ve mevcut ajan iptal/kalıcılık yardımcıları yeniden kullanıldı.
Canonical güncelleme, mevcut oturum iptal yardımcısının commit öncesi runtime yan etkilerinden
kaçınmak için executor'un kendi transaction bağlantısında doğrudan SQL ile yapılır.

## Dosyalar ve yüzeyler

| Alan | Dosyalar | Değişiklik |
|---|---|---|
| Migration | [006](migrations/20261010_006_approval_requests.sql), [007](migrations/20261010_007_approval_audit_actions.sql) | Talep tablosu, immutable/durum/erasure trigger'ları, audit action genişletmesi |
| Ortak backend | [approval_service.py](app/approval_service.py), [audit_log.py](app/audit_log.py), [main.py](app/main.py) | Allowlist DTO, canonical kontroller, audit, router kaydı |
| Moderatör | [moderator_approvals.py](app/moderator_approvals.py) | Oluşturma, yalnız kendi listesi, iptal |
| OWNER | [admin_approvals.py](app/admin_approvals.py), [approval_executor.py](app/approval_executor.py) | Liste/detay/özet, karar, atomik uygulama, ajan-only tekrar deneme |
| Backend testleri | [requests](tests/test_approval_requests.py), [executor](tests/test_approval_executor.py), [migration](tests/test_approval_migrations.py) | Yeni hedefli regresyonlar |
| Test entegrasyonu | [audit](tests/test_audit_log.py), [owner inventory](tests/test_moderator_access.py), [fixture](tests/test_moderator_role.py) | Audit action superset, yeni OWNER route'ları, offline router fixture |
| Frontend | [approval-model.ts](../approval-model.ts), [approval-api.ts](../approval-api.ts), [approval-ui.tsx](../approval-ui.tsx) | Doğrulanan DTO, güvenli transport, küçük ortak Türkçe bileşenler |
| Ekranlar | [ModeratorApprovals.tsx](../ModeratorApprovals.tsx), [AdminApprovals.tsx](../AdminApprovals.tsx) | Kendi talepleri/gerekçe/özet ve OWNER inceleme/karar/ajan tekrar deneme |
| Panel bağlantıları | [ModeratorPanel.tsx](../ModeratorPanel.tsx), [AdminPanel.tsx](../AdminPanel.tsx), [moderator-panel.css](../moderator-panel.css) | İzne göre ekran/form, yeni admin bölüm bağlantısı, yalnız `mod-*` stilleri |
| Frontend testleri | [moderator-approvals.test.mjs](../tools/moderator-approvals.test.mjs) | Rozetler, boş durum, form sınırları, busy, DTO, hata ve düz metin |

Admin'in mevcut tek dosya değişikliği: yeni bileşen import'u, `Section` union'ı,
bir menü girdisi ve koşullu render. `admin.css` ve mevcut admin ekran mantıkları değişmedi.
Yeni [AdminApprovals](../AdminApprovals.tsx) ayrı bileşendir; stiller mevcut tema değişkenlerini
kullanan yeni `mod-*` kapsamıyla sınırlıdır.

## Güvenlik ve uygulama sınırları

- İşlem allowlist'i: `account.deactivate`, `account.reactivate`. Payload yalnız `{}`.
- Moderatör yolları `approvals.create`, canonical MODERATOR ve MFA gerektirir.
  CUSTOMER ve OWNER bu moderatör işlem yollarına giremez.
- Aynı müşteri/moderatör okuma rate bucket'ı kullanılır: 30 istek / 60 saniye.
- CUSTOMER olmayan, kendi hesabı olan veya silinmiş hedefler 404.
- Zaten istenen durumda olan hedef, herhangi bir pending hedef talebi veya replay 409.
- Bir moderatör en fazla 5 pending ve UTC gününde 20 talep oluşturabilir.
  Ortak transaction advisory lock, limit ve duplicate kontrollerini serialize eder.
- Talep oluşturma hiçbir hesap değişikliği yapmaz; hedef snapshot'ı DB'den alınır.
- OWNER doğrulaması canonical DB'den yapılır. Karar veren talep eden olamaz;
  ayrıca DB CHECK bu ayrımı korur.
- Onay claim transaction'ı talebi kilitler, süreyi/requester rolünü/iznini/hedef üçlüsünü
  doğrular, `approved → executing` ve audit'i commit eder.
- Executor hedefi `FOR UPDATE` ile **yeniden** okur. `active`, `role`, `auth_version`
  değişmişse `stale` olur. Rol satırı önce, izin satırı sonra kilitlenir; mevcut
  izin değiştirme işlemlerinin kilit sırası korunur.
- Gerçek değişiklikte `active` ve tam bir sürüm artışı canonical transaction'dadır.
  Zaten istenen durumda olan iç uygulama idempotenttir. OWNER hedefi asla uygulanmaz.
- Canonical değişiklik, `canonical_applied` checkpoint'i ve audit aynı transaction'dadır.
  Bu audit yazılamazsa canonical değişiklik ve checkpoint tamamen geri alınır.
- Açık işlem/bağlantı/otomasyon engeli `failed / protected_positions` üretir.
  Güvenli bağlantı durumunun okunamaması `failed / execution_failed` üretir.
- Canonical commit'ten **sonra** ajan iptali ayrı snapshot kalıcılık işlemidir.
  Ajan iptal yardımcısı yalnız ACTIVE ajanlara çağrılır; bilinçli tekrarda REVOKED
  ajanların token sürümü tekrar artırılmaz, snapshot kalıcılığı tekrar doğrulanır.
- Ajan adımı hatasında `failed / agents_revoke_pending`; oturumlar yine geçersizdir.
  OWNER'ın `POST /api/v22/admin/approvals/{id}/retry-agents` işlemi yalnız ajan adımını
  tekrarlar; canonical hesap değişikliğini veya auth_version artışını tekrar yapmaz.
- `executing` talepler okuma anında veya tekrar onayla otomatik çalıştırılmaz.
  UI'da “Kontrol gerekli” gösterilir. `failed / agents_revoke_pending` için bilinçli
  ve teyitli ajan-only retry sunulur.
- Pending expiry, okuma anında aynı transaction'da audit ile lazy işaretlenir.
- Approval audit'inde gerekçe, karar metni, e-posta veya hata metni yoktur.
  `approval.executed` action'ında yalnız sabit `phase=canonical|completed` bilgisi,
  ID'lerin yanında iki transaction sınırını ayırır. Diğer snapshot alanları boştur.
- Hedef veya requester erasure tombstone'u, yalnız nested dar trigger istisnasıyla
  `reason` ve `decision_note` alanlarını boşaltır. Normal immutable kurallar ve
  DELETE/TRUNCATE yasağı korunur. Kullanıcı FK'si yoktur.
- Ürün ekranlarında sahte veri yoktur. Yokluk “veri yok”; API hata metinleri
  Türkçe allowlist mesajlarına dönüştürülür; React metinleri HTML olarak yorumlamaz.

## Doğrulama ve bilinen sınırlar

- Aşama 7 baseline: 163 hedefli backend test, 18 küçük frontend test; strict
  TypeScript ve production build başarılı.
- Son backend: 211 geçti, 0 başarısız. Baseline kimliklerinde kayıp 0; 48 yeni
  test/parametreli OWNER inventory vakası. Önceden mevcut Starlette deprecation uyarısı sürüyor.
- Son frontend: 27 geçti, 0 başarısız; eski 18 test korunuyor.
- Frontend strict TS, access TS, hedefli ESLint, production build başarılı.
  Build `COIN_LOGO_OFFLINE=1` ile dış servis hook'u olmadan çalıştırıldı.
- Pylance dört yeni backend ürün dosyasında syntax hatası bulmadı.
- Gerçek headless Chromium, yalnız yerel Vite ve tüm API'ler mock iken çalıştırıldı:
  talep/iptal, hesap değişikliği yapmama, izin gizleme, HTML kaçış, mobil taşma,
  OWNER çift tıkta tek POST, açık ajan retry, 409'da GET-only yenileme, zorunlu red
  notu ve sıfır page error doğrulandı. İlk script denemesinde yalnız option/rozet
  locator çakışması vardı; ürün veya beklenti değiştirilmeden locator scope düzeltildi.
- Testlerde canonical DB transaction modeli ve gerçek endpoint/helper kodu kullanılır.
  SQL trigger kuralları dosya içeriği sözleşmeleriyle doğrulandı. **Gerçek PostgreSQL
  üzerinde migration, trigger, isolation/kilit veya eşzamanlı transaction denenmedi.**
- Provider, gerçek müşteri, gerçek Stripe veya Binance isteği yapılmadı.
- Crash sonrası `executing` talebin manuel operasyonel incelemesi gerekir;
  otomatik replay/reconciliation eklenmedi.
- Eski OWNER durum endpoint'i, eski müşteri destek akışı, trading/ödeme/abonelik/kasa/
  giriş ürün kodları değiştirilmedi. Stage7 snapshot yazması yalnız izin verilmiş
  ajan iptali kalıcılığı için mevcut yardımcı akışından geçer; migration backfill yoktur.

Tarayıcı script'i, test XML'leri, build log'ları ve dört ekran görüntüsü repo dışında
oturum artifact klasöründedir; commit'e dahil değildir.

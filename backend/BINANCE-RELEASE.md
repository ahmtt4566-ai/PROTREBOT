# Binance manual ödeme: canlıya çıkış ve geri dönüş

Bu kontrol listesi bir uygulama/deploy onayı değildir. Canlı işlem yapılmadan
önce ayrıca onay alınmalıdır. Stripe Checkout, webhook ve deneme mantığı
değiştirilmez.

## Yerel doğrulama

- Test yalnız `127.0.0.1` veya `::1` adresindeki ayrı
  `binance_stage5_test` veritabanını kabul eder.
- Uygulamanın `DATABASE_URL` değişkenini kullanmayın; test için
  `BINANCE_TEST_POSTGRES_DSN` verin.
- `tests/test_binance_postgres.py` mevcut ticari şemayı ve eski migration'ları
  ayrı rastgele test şemasında kurar; yeni migration'ları sırayla uygular.
- Çift işlem numarası, tek onay/tek dönem/tek audit, deferred trigger ile eksik
  audit/dönemde transaction rollback, kayıt değiştirilemezliği, silinen kullanıcı
  koruması, kuyruk payload/dedupe ve rollback/reapply test edilir.
- Başka veritabanına bağlanmaz. Test bitince yalnız oluşturduğu test şemasını
  kaldırır. Yerel sunucu ayrıca durdurulmalıdır.

## Yayın öncesi

1. Doğru uygulama/veritabanı kimliğini bağımsız doğrulayın; bağlantı bilgisini
   günlük, rapor veya repoya koymayın.
2. Testleri ve build'i çalıştırın; başarısızlıkta yayın yapmayın.
3. Binance yöntemini gizleyin (`BINANCE_MANUAL_PAY_ID` boş);
   `BINANCE_MANUAL_ACCESS_ENABLED=false` ve
   `BINANCE_MANUAL_NOTIFICATIONS_ENABLED=false` bırakın.
4. Yazıcıları/işçileri kontrollü durdurun. Açık pozisyon korumasını aniden
   kesmeyin; gerekli operasyonel durdurma için ayrı işlem güvenliği prosedürünü
   uygulayın. Stripe webhook trafiğini kaybetmeyin.
5. Tam PostgreSQL yedeği, şema dump'ı ve kurtarma noktası alın. Yedekten ayrı
   test veritabanına geri yüklemeyi doğrulayın; yalnız dosya oluşturmak yeterli
   değildir. Yedekleri repo dışında güvenli saklayın.
6. Ön koşulları kontrol edin: canonical auth/erasure, subscriptions, audit_log,
   notification_outbox, kampanya outbox genişletmesi
   (`20261010_012`) ve audit genişletmesi (`20261010_013`).
   Yeni migration'lar bu eski şemayı kendiliğinden kurmaz.

## Migration ve açma sırası

1. İşçiler duruyorken `psql -X -v ON_ERROR_STOP=1` ile ayrı ayrı:
   - `migrations/20261011_001_binance_manual_payments.sql`
   - `migrations/20261011_002_binance_manual_entitlements.sql`
   - `migrations/20261011_003_binance_notifications.sql`
2. Her dosya kendi transaction'ındadır. Hata olursa sonraki dosyaya geçmeyin;
   hangi dosyanın commit edildiğini kontrol edin.
3. Tablo/indeks/kısıt ve ENABLE ALWAYS trigger'ları kontrol edin. Önceki
   Stripe ve audit/outbox korumalarının hâlâ kurulu olduğunu doğrulayın.
4. Uyumlu backend/frontend kodunu bayraklar kapalıyken yayımlayın.
5. Resend ayarlarını doğrulayın: `EMAIL_PROVIDER=resend`, `EMAIL_FROM`,
   `RESEND_API_KEY`, canonical `APP_BASE_URL=https://kaistrade.com`.
   Resend gönderici alan adının doğrulanmış olduğunu kontrol edin.
6. Önce `BINANCE_MANUAL_ACCESS_ENABLED=true`, sonra
   `BINANCE_MANUAL_NOTIFICATIONS_ENABLED=true` ile işçileri kontrollü başlatın.
   Bildirim süpürmesinin boş kuyrukta da 60 saniyelik yeniden uyanmasını,
   OWNER alıcısını ve hata/backoff günlüklerini kontrol edin.
7. Son olarak gerçek `BINANCE_MANUAL_PAY_ID` ayarını verin; yöntem görünür olur.
   Örnek/test ID kullanmayın.
8. Ayrı onaylı kontrollü ödeme ile bekleyen talep, OWNER kart bağlantısı,
   onay/red, kullanıcı erişim/bitiş tarihi, e-posta/zil ve tekli/toplu okundu
   davranışını kontrol edin. Moderatörün karar veremediğini doğrulayın.
9. Aktif ücretli Stripe kullanıcısının erişimi, checkout/webhook ve deneme
   kullanıcısının kalan günleri değişmemelidir. İlk saatlerde 503, eksik OWNER,
   dead/failed outbox ve bildirim tekrarlarını takip edin.

## Sorunda geri dönüş

### Ödeme geçmişi varsa: veriyi koruyan operasyonel geri dönüş

1. Pay ID'yi boşaltarak yeni talepleri gizleyin. Bildirim bayrağını kapatın.
2. Güvenli taraf için Binance erişim bayrağını kapatın. Bu, mevcut Binance
   üyelerine geçici erişim kesintisi yaratır; açık pozisyon güvenliği için ayrı
   operasyonel değerlendirme yapın. Stripe erişimi değişmez.
3. Ödeme/period/audit/outbox/dedupe kayıtlarını **silmeden**, uyumlu önceki
   kod sürümüne dönün. Yeni tabloları koruyun; yedeğe gelişigüzel geri yüklemek
   ödeme geçmişini veya işlem numarası tekrar engelini kaybettirebilir.
4. Sorun çözüldüğünde önce şema/veriyi doğrulayın; erişim/bildirim bayraklarını
   ve en son Pay ID'yi açın. Reconciliation eksik kuyruk kayıtlarını tamamlar.
   Gönderilmiş/terminal kayıtları sıfırlamayın.

### Henüz hiçbir Binance kaydı yoksa: şema rollback

- `migrations/rollback/20261011_binance_empty.sql`, üç migration'ı tek
  transaction içinde ters sırayla kaldırır ve eski audit/outbox sözleşmesini
  geri getirir.
- İşçiler durmalı, bütün Binance ayarları kapalı olmalıdır.
- Script payment/period/audit/outbox/read geçmişi bulursa **reddeder**;
  bu korumayı devre dışı bırakmayın. CASCADE veya ödeme/audit silme kullanmaz.
- İlk/ikinci migration aşamasında kalınmışsa bu toplu scripti körlemesine
  çalıştırmayın; önce eksik şema ve yedek üzerinden operasyonel karar alın.
- Rollback başarılıysa şemayı doğrulayın. Yeniden uygulama yine
  `001 → 002 → 003` sırasındadır.

## Ücretsiz kullanıcı bildirim sınırı

Tam başlık/mesaj yalnız canonical kullanıcıya bağlanmış, kalıcı
`binance:<32 hex>` kimlikli `BINANCE_APPROVED`, `BINANCE_REJECTED`,
`BINANCE_RENEWAL` kayıtlarına açılır. `BINANCE_PENDING`, sinyal, işlem, analiz
ve diğer bildirimler mevcut public filtreye tabidir. Aynı sınırlama GET,
tekli okundu ve toplu okundu yanıtlarında uygulanır. Ek analiz/entry alanları
ödeme bildirimi içinde bile açılmaz.

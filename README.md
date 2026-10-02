# ProTreBot Elite X V28 — Uygulama İçi Borsa Bağlantıları

Production deployment trigger verified through the repository commit pipeline.

## Master Trade / Canlı İşlem arayüzü

`/master-trade?tab=canli` koyu/yeşil temada kompakt kartlar, yatay stepper,
responsive metrik kutuları ve masaüstünde yan yana manuel emir/özet görünümü kullanır.
Eksik metrikler skeleton ile, sinyal fiyatları yalnızca gösterimde iki ondalıkla sunulur;
ham değerler tooltip'te korunur. Dar ekranlarda içerik tek kolona, metrikler iki kolona
geçer. LOCKED, ARM, consent, backend doğrulamaları ve mevcut işlem koşulları değişmez.

Doğrulama: kökte `npm run build`; `frontend` içinde
`npx playwright test master-trade-live-ui.spec.ts master-trade-readonly.spec.ts --project=chromium`.
UI testi gerçek emir/bağlantı işlemi yapmadan mock verilerle masaüstü, tablet ve mobil
layout ölçülerini, kilitli butonları ve yerel form/toggle davranışlarını denetler.
Ekran görüntüleri test çıktısına eklenir; isteğe bağlı `LIVE_UI_SCREENSHOTS` değişkeniyle
ayrı bir çıktı klasörü, `LIVE_UI_PHASE` ile dosya adı öneki belirtilebilir.
Frontend lint: kökte `npm run lint`.
Build, kredi/premium modüllerinin strict TypeScript kontrolünü de çalıştırır
(`npm run typecheck:access` ile ayrı çalıştırılabilir).
Analyst coin seçici aynı anda en fazla beş satır gösterir; diğer coinlere liste
içinden kaydırılarak erişilir. Arama sonucu azaldığında kart içerik kadar küçülür.

## Analyst kredileri ve Master Trade Premium

Tüm korumalı API çağrıları aktif, doğrulanmış üyelik oturumu gerektirir; yönetici
önizleme anahtarı tek başına üyelik yerine geçmez. Ücretsiz üyeler Master Trade'i
salt-okunur görüntüler. İşlem/bağlantı yetkisi mevcut OWNER veya aktif abonelik
kurallarından belirlenir; istemci plan bilgisi yetki vermez. Premium, mevcut
LOCKED / ARM / consent ve risk kapılarını kaldırmaz.

Sunucu ayarları: `ANALYST_DAILY_CREDITS=100`, `ANALYST_COST=1`,
`CREDIT_WINDOW_HOURS=24`, `ANALYST_CACHE_MINUTES=15`.

- `GET /api/analyst/credits`: `remaining`, `total`, `resetsAt`, `unlimited`.
- `POST /api/analyst/consume`: `{symbol, timeframe, idempotency_key}`.
  Yanıt aynı bütçe alanlarıyla birlikte `result`, `cached`, `cacheExpiresAt` döndürür.
  Sembol USDT paritesidir; mevcut `1m/5m/15m/30m/1h/4h/1d` zaman dilimleri desteklenir.
  Her yeni kullanıcı aksiyonunda yeni bir anahtar, aynı isteğin yeniden gönderiminde
  aynı anahtar kullanılmalıdır.
- İlk harcamada başlayan kullanıcıya özel 24 saatlik pencere dolunca bütçe sonraki
  istekte yeniden 100 olur; kredi devretmez. Aynı kullanıcı/sembol/zaman dilimi
  15 dakika içinde yeniden açılırsa kayıtlı sonuç ücretsiz döner.
- Kredi yetersizliği: `429` + `remaining`/`resetsAt`. Dakikada 30 consume isteği
  sınırı cache ve premium isteklerini de kapsar; rate-limit yanıtında `Retry-After`
  bulunur. Geçersiz/engellenen çağrılar loglanır.
- Analiz başarısızlığı kredi iadesiyle `502` döndürür. Harcama, cache ve idempotency
  kaydı atomiktir; aynı anahtar çift harcama veya çift iade üretmez.
- Premium için `remaining=null`, `unlimited=true`; kredi düşmez. Master Trade
  kredi endpoint'lerine çağrı yapmaz ve sayaç göstermez.

Üretimde PostgreSQL transaction ve kullanıcı satırı kilidi kullanılır.
`PROTREBOT_DURABLE_AUTH_REQUIRED=true` iken PostgreSQL yoksa servis `503` ile kapalı
kalır. Yerel geliştirmede `DATA_DIR/analyst_credits.sqlite3` kalıcı SQLite deposu
kullanılır. Mevcut otomatik testler SQLite üzerinden çalışır; canlı PostgreSQL
entegrasyonu ayrıca deployment ortamında doğrulanmalıdır.

Premium olmayanın emir/arm/dry-run/bağlantı ve alternatif otomasyon başlangıç
endpoint'leri `403 PREMIUM_REQUIRED` döndürür. Ücretsiz trading GET yanıtlarında
yalnız izinli özet/piyasa alanları gönderilir; giriş/SL/TP, karar gerekçeleri,
vakalar ve trigger detayları sunucuda kaldırılır. Arayüz kilitleri gerçek içeriği
render etmez; yalnız örnek skeleton gösterir. İlk kilit tıklaması oturumda premium
modalı (mobilde sheet), sonraki tıklamalar toast açar.

Doğrulama:
```powershell
npm run build
npm run lint
$env:PYTHONPATH='backend'
.\.venv\Scripts\python.exe -m pytest backend\tests\test_analyst_credits.py backend\tests\test_member_premium_api.py backend\tests\test_subscription.py -q
.\.venv\Scripts\python.exe -m ruff check --select E9,F63,F7,F82 backend\app\analyst_credits.py backend\app\premium_access.py backend\app\main.py backend\app\v22_commercial.py backend\app\v25_execution.py backend\tests\test_analyst_credits.py backend\tests\test_member_premium_api.py
Set-Location frontend
npx playwright test member-premium.spec.ts master-trade-live-ui.spec.ts master-trade-readonly.spec.ts --project=chromium
```

V28, V27 bulut operasyon ve kanıt altyapısını korur; Testnet ve gerçek Binance USD-M
Futures API bağlantılarını doğrudan programın içine taşır. Render'a Binance anahtarı yazmak
gerekmez. Yönetici panelindeki **Borsa Bağlantıları** sekmesinden API Key ve Secret Key
test edilir, şifreli kaydedilir, aktifleştirilir, kapatılır veya silinir.

## V28'de yeni olanlar

- Testnet ve gerçek hesap için ayrı bağlantı kartları
- Emir oluşturmayan imzalı hesap/pozisyon modu testi
- Secret'ları PostgreSQL'de Fernet ile şifreleyen sunucu kasası
- Secret değerini hiçbir API yanıtında veya arayüzde geri göstermeyen tasarım
- Bağlantı aktifleştirme, devre dışı bırakma ve kalıcı silme kontrolleri
- Bakiye, kullanılabilir bakiye, açık PnL, pozisyon sayısı ve One-way/Hedge görünümü
- Gerçek hesapta bağlantı aktivasyonu ile emir yetkisinin kesin ayrımı
- V25'in Demo kanıtı, 24 saatlik risk izni, limit onayı ve 5 dakikalık son kilidi korunur
- Anahtar değiştirilince tüm kısa süreli işlem izinleri otomatik sıfırlanır

> Kâr garantisi yoktur. Testnet sonucu gerçek piyasayı garanti etmez. Vadeli işlemlerde
> yatırılan sermayenin tamamı kaybedilebilir.

## Güvenlik modeli

1. Yönetici giriş kodu olmadan bağlantı API'lerine erişilemez.
2. API anahtarı yalnızca HTTPS isteğiyle kendi arka ucunuza gönderilir.
3. Sunucu, anahtar çiftini önce seçilen Binance hostunda imzalı ve salt-okunur olarak test eder.
4. Başarılı çift PostgreSQL'e yalnızca şifreli veri olarak yazılır.
5. Arayüze yalnızca geri döndürülemez SHA-256 anahtar izi ve güvenli hesap özeti gelir.
6. **Bağlantıyı aktifleştir** gerçek emir açmaz.
7. Para çekme/transfer uçları yazılımda desteklenmez.

## Bir defalık yayın ayarları

Render arka uç servisinde yalnızca mevcut temel değerler gerekir:

| Değişken | Değer |
|---|---|
| `DATABASE_URL` | Render PostgreSQL bağlantısı |
| `PROTREBOT_WEB_ACCESS_TOKEN` | En az 24 karakterlik yönetici kodunuz |
| `PROTREBOT_CORS_ORIGINS` | Tam Vercel adresiniz |
| `PROTREBOT_EXECUTION_MODE` | `TESTNET_FIRST` |
| `PROTREBOT_LIVE_CHANNEL_ENABLED` | `true` |

Backend Gmail API OAuth2 secrets (Render secret store):

| Variable | Description |
|---|---|
| `GMAIL_CLIENT_ID` | Google OAuth client ID |
| `GMAIL_CLIENT_SECRET` | Google OAuth client secret |
| `GMAIL_REFRESH_TOKEN` | Gmail OAuth refresh token with `gmail.send` scope |

Binance API anahtarları bu listeye eklenmez. İsterseniz mevcut yönetici kodundan ayrı bir
kasa anahtarı için `PROTREBOT_VAULT_MASTER_KEY` kullanabilirsiniz; zorunlu değildir.

Vercel ön yüzde:

| Değişken | Değer |
|---|---|
| `VITE_API_URL` | Örneğin `https://tradebt15.onrender.com` |
| `VITE_WEB_ACCESS_REQUIRED` | `true` |

## Program içinden bağlantı sırası

1. Yönetici koduyla panele girin.
2. **Borsa Bağlantıları** sekmesini açın.
3. Önce **Binance Futures Testnet** kartını seçin.
4. API Key ve Secret Key'i girip **Bağlantıyı Test Et** düğmesine basın.
5. Saklama kutusunu işaretleyip **Şifreli Kaydet** düğmesine basın.
6. **Bağlantıyı Aktifleştir** düğmesine basın.
7. Testnet Komuta ekranında bakiye, pozisyon, Stop ve TP görünümünü doğrulayın.
8. Gerçek hesap anahtarını ancak Testnet kanıt hedefleri tamamlandıktan sonra ekleyin.

## Testnet ve gerçek hesap ayrımı

- **Testnet:** Sanal bakiye kullanır. Bağlantı aktivasyonundan sonra 10 dakikalık Demo emir
  kilidi ayrıca açılır.
- **Gerçek:** Aktivasyon yalnızca salt-okunur hesap bağlantısıdır. Gerçek emir için bütün V25
  güvenlik kapıları ve kısa süreli emir kilidi ayrıca geçmelidir.

## Yerel geliştirme

Arka uç:

```bash
cd backend
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt
.venv/Scripts/python -m uvicorn app.main:app --reload --port 8000
```

Ön yüz:

```bash
cd ..
npm install
npm run dev
```

On yuz deposunun kok dizininden calistirilir. Yerel gelistirmede API adresi
ayarlanmamissa `/api` istekleri Vite tarafindan `http://127.0.0.1:8000`
adresine yonlendirilir; backend ve frontend birlikte calismalidir.
`VITE_API_BASE` veya `VITE_API_URL` tanimlanmissa bu adres kullanilir.

Ilk yonetici kurulumu normal giris ve musteri kayit ekranlarinda gosterilmez.
Yalnizca yerel Vite gelistirmesinde `/local-owner-setup` adresinden erisilir.
Backend uzak baglantilari ve `PROTREBOT_BOOTSTRAP_OWNER_EMAIL` ile eslesmeyen
hesaplari reddeder; yonetici atandiktan sonra kurulum tekrar acilamaz.
Mevcut bir hesabin ilk yoneticiye atanmasi dogru parolasini gerektirir.

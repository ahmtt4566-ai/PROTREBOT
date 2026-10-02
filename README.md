# ProTreBot Elite X V28 — Uygulama İçi Borsa Bağlantıları

Production deployment trigger verified through the repository commit pipeline.

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


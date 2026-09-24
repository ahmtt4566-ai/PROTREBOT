# Manuel Live Trade Akışı Denetim Raporu

## Kapsam

Bu rapor Master Trade manuel canlı emir akışını, readiness kapılarını, giriş sonrası Stop/TP korumasını ve disarm/consent davranışını değerlendirir. İnceleme kod ve mevcut güvenlik sözleşmesi üzerinden yapılmıştır. Canlı emir gönderilmemiştir.

## Genel Akış

1. Arayüz sembol, yön, marjin, kaldıraç, giriş tipi, Stop Loss ve TP1-TP3 değerlerini doğrular.
2. Kullanıcı ikinci onay olarak `CANLI EMİR GÖNDER` metnini girmeden `/order` çağrısı yapılmaz.
3. Backend recovery, LIVE ARM, execution lock, consent, policy, bağlantı, hesap modu, bakiye, exposure, spread ve günlük risk kapılarını tekrar doğrular.
4. Stop/TP seviyeleri yön ve risk politikası açısından doğrulanır.
5. Binance giriş emri gönderilir.
6. MARKET emir dolumundan sonra V25 Stop/TP korumalarını kurar. LIMIT emir için dolum reconciliation ile görülür ve koruma daha sonra kurulur.

## Bulgular

### 1. Consent expiry — TASARIM GEREĞİ, RİSK BULGUSU DEĞİL

Consent süresi dolduğunda yeni manuel ve Auto Trade giriş emirleri durur. Mevcut pozisyon ve V25'e ait Stop/TP koruma emirleri iptal edilmez.

Bu davranış bilinçli bir tasarım kararıdır: consent süresi doldu diye koruma emirlerini kaldırmak, açık pozisyonu korumasız bırakır. Bu nedenle mevcut pozisyonun ve korumalarının açık kalması bir risk ihmali değil, koruma sürekliliği gereğidir.

15 dakikalık yeniden yetkilendirme grace süresi sonunda yalnızca Auto Trade kapatılır. Açık pozisyonlar ve mevcut koruma emirleri otomatik olarak kapatılmaz veya iptal edilmez.

**Sınıflandırma:** Tasarım gereği / beklenen davranış.

### 2. LIMIT fill-to-protection penceresi — AÇIK RİSK

Dolmamış LIMIT giriş emri `DOLUM BEKLİYOR` durumunda tutulur. Pozisyonun dolduğu reconciliation snapshot'ında görüldükten sonra Stop/TP korumaları kurulur.

Kontrol döngüsü nominal olarak 10 saniyelik aralıklarla çalıştığı için dolum ile koruma kurulumu arasında beklenen pencere:

> **0-10 saniye + Binance/API ve işlem gecikmesi**

Bu 10 saniye sabit bir üst sınır değildir. Bağlantı, websocket, Binance API, recovery veya reconciliation problemi yaşanırsa pencere uzayabilir. Sistem yeni işlemleri kilitler ve eksik korumayı tekrar kurmayı dener; ancak o anda dolmuş ve henüz korumasız kalan pozisyonu otomatik olarak kapatmaz.

Bu nedenle fill-to-protection süresi manuel LIMIT işlemler için açık ve ayrı bir operasyonel risk olarak raporlanmalıdır. Nominal pencere bilinir, fakat bağlantı/recovery arızaları altındaki gerçek üst sınır kod tarafından garanti edilmez.

**Sınıflandırma:** Açık risk; üst sınır belirsiz.

### 3. Stop kurulum hatası sonrası kapatma — FAIL-CLOSED, KAPATMA GARANTİSİ YOK

Pozisyon dolduktan sonra sistem önce `STOP_MARKET` korumasını kurmayı dener. Stop kurulumu başarısız olursa tracked pozisyon için `reduceOnly MARKET close` gönderilir.

Bu kapatma yolu normalde tek denemedir. Emir sonucunun belirsiz olduğu `unknown execution` durumunda aynı client ID ile arama yapılarak Binance sonucu reconciliation ile doğrulanır.

Kapatma emri de başarısız olursa hata üst katmana taşınır, canlı execution kilitlenir ve recovery gerekir.

**Pozisyonun kapandığı garanti edilmez.** Bu durumda sistem pozisyonu sessizce korumalı kabul etmez; belirsiz durumu kilitler ve manuel/recovery incelemesi gerektirir.

**Sınıflandırma:** Fail-closed güvenlik davranışı; kapanış sonucunun kendisi garanti edilemez.

### 4. Disarm + bekleyen LIMIT girişi — TESPİT EDİLEN BOŞLUK

`/disarm` endpoint'i:

- yeni manuel girişleri durdurur,
- Auto Trade'i kapatır,
- LIVE ARM kilidini kapatır,
- mevcut SL/TP korumalarını iptal etmez.

Ancak mevcut uygulamada dolmamış ve V25'e ait bekleyen LIMIT giriş emri disarm sırasında iptal edilmiyor. Bu emir Binance üzerinde beklemeye devam edip sonradan dolabilir.

Bu bulgu mevcut davranışın nötr tespitidir; mevcut SL/TP korumalarının iptal edilmemesiyle aynı şey değildir. Bekleyen giriş emri yeni bir pozisyon oluşturabileceği için ayrıca ele alınmalıdır.

**Sınıflandırma:** Gerçek davranış boşluğu.

## Önerilen Düzeltme

`/disarm` endpoint'i, yeni giriş yetkisini kapatmanın yanında snapshot'taki V25 sahipli bekleyen giriş emirlerini de client ID/provenance ile doğrulayarak iptal etmelidir. Yalnızca V25'e ait giriş emirleri iptal edilmeli; yabancı veya manuel dışı emirler sahiplenilmemelidir.

İptal sonucu doğrulanmalı, belirsiz sonuçta execution kilitlenmeli ve reconciliation tekrar istenmelidir. Bu öneri bu rapor güncellemesi kapsamında **uygulanmamıştır**; yalnızca önerilen kod değişikliğidir.

## Net Özet

SL/TP güvenlik modeli fail-closed ve sağlamdır: Stop kurulumu zorunludur, TP emirleri reduce-only'dir, koruma kurulamazsa güvenlik kapatma yolu ve belirsiz durumda execution lock vardır.

Bununla birlikte iki konu ayrı ayrı flag'lenmelidir:

1. **LIMIT fill-to-protection penceresi:** Nominal 0-10 saniye + API gecikmesi vardır; bağlantı/recovery arızalarında üst sınır garanti edilmez ve dolmuş-korumasız pozisyon otomatik kapatılmayabilir.
2. **Disarm boşluğu:** Bekleyen V25 LIMIT giriş emri disarm ile şu anda iptal edilmez ve sonradan dolabilir.

Consent expiry ise bu iki bulgudan farklıdır: mevcut pozisyonu ve korumalarını açık tutması **tasarım gereği beklenen davranıştır**, güvenlik ihmali değildir.

### Ortak Risk Notu — Bulgular 2 ve 3

LIMIT fill-to-protection penceresi (Bulgu 2) ve Stop kurulum hatası sonrası kapatma (Bulgu 3) senaryolarının her ikisi de aynı sonuca çıkabilir: pozisyon geçici veya kalıcı olarak korumasız (SL/TP'siz) kalabilir ve sistem bu durumda yeni işlemleri kilitleyerek kendini korur, ancak mevcut çıplak pozisyonu otomatik kapatmaz. Bu nedenle her iki senaryoda da operatörün pozisyonu manuel olarak izlemesi ve gerekirse manuel müdahale etmesi gerekir.

## Referanslar

- [V25 execution](backend/app/v25_execution.py)
- [Execution core risk gates](backend/app/execution_core.py)
- [Live trading panel](frontend/src/LiveTradingPanel.tsx)
- [V25 live guard guide](V25-LIVE-GUARD.md)

AUDIT_SCOPE=MANUAL_LIVE_TRADE
LIVE_ORDER_SENT=FALSE
CODE_CHANGED=FALSE

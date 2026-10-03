# ProTreBot Elite X V28 — Uygulama İçi Borsa Bağlantıları

Production deployment trigger verified through the repository commit pipeline.

Ana sayfa başlık logosu 252 × 65,8 px hedef boyutuyla önceki boyuttan %40
büyüktür; dar ekranlarda başlık kontrollerini örtmemek için kullanılabilir
genişliğe sığar. Diğer çalışma ekranlarındaki logo boyutu değişmez.

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

Sunucu ayarları: `ANALYST_DAILY_CREDITS=100`, `ANALYST_COST=10`,
`CREDIT_WINDOW_HOURS=24`, `ANALYST_CACHE_MINUTES=15`.
Varsayılan analiz maliyeti `backend/app/analyst_credits.py` içindeki
`ANALYSIS_COST` sabitinden gelir; `ANALYST_COST` mevcut ortam ayarıyla değiştirilebilir.
100 kredilik bütçeyle 24 saatlik pencerede 10 taze analiz açılır; 11. taze analiz
kredi yetersizliğiyle reddedilir. Cache sonuçları bu bütçeden harcamaz.

- `GET /api/analyst/credits`: `remaining`, `total`, `analysis_cost`, `resetsAt`, `unlimited`.
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
- Analiz başarısızlığı harcanan maliyet kadar (varsayılan 10 kredi) tek iade ile
  `502` döndürür. Harcama, cache ve idempotency
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

## Kais AI yapılandırması

Asistan ayarlarının tek kaynağı `backend/app/assistant_config.py` içindeki
`AssistantConfig` / `load_assistant_config()` yapısıdır. Ayarlar backend ortamından
okunur; diğer modüller model, limit veya fiyat değerlerini tekrar sabitlememelidir.

| Ortam değişkeni | Varsayılan | Amaç |
| --- | --- | --- |
| `ASSISTANT_ENABLED` | `true` | Özelliği açma/kapatma |
| `ASSISTANT_MODEL` | `claude-haiku-4-5-20251001` | Kullanılacak model kimliği |
| `ANTHROPIC_API_KEY` | boş | Yalnız backend secret store |
| `ASSISTANT_MAX_INPUT_CHARS` | `500` | Tek mesaj karakter sınırı |
| `ASSISTANT_HISTORY_MESSAGES` | `4` | LLM'e gönderilecek geçmiş mesaj sayısı |
| `ASSISTANT_HISTORY_MESSAGE_MAX_CHARS` | `2000` | Her geçmiş mesajı sunucuda bu uzunluğa kırpılır |
| `ASSISTANT_PAGE_CONTEXT_MAX_CHARS` | `300` | Sayfa bağlamı karakter sınırı |
| `ASSISTANT_REQUEST_TIMEOUT_SECONDS` | `30` | Sağlayıcı ve yerel veritabanı bekleme sınırı |
| `ASSISTANT_PROTECTION_STALE_SECONDS` | `30` | Koruma/pozisyon verisinin taban eskime eşiği; etkin eşik `max(ayar, 2 × RECONCILE_SECONDS)` |
| `ASSISTANT_PROACTIVE_ENABLED` | `true` | Uygulama içi, LLM'siz durum yoklamaları |
| `ASSISTANT_PROACTIVE_INACTIVE_DAYS` | `7` | Asistanın ölçtüğü önceki uygulama ziyaretinden sonra hatırlatma eşiği |
| `ASSISTANT_PROACTIVE_COOLDOWN_HOURS` | `24` | Kullanıcı başına yoklamalar arası kayan bekleme; en az 24 saat |
| `ASSISTANT_PROACTIVE_POLL_SECONDS` | `300` | Görünür sayfada kontrol aralığı; en az 60 saniye |
| `ASSISTANT_SECRET_MIN_ALPHANUMERIC_CHARS` | `40` | Uzun anahtar benzeri dizilerin engellenme eşiği |
| `ASSISTANT_BUDGET_WARNING_FRACTION` | `0.8` | Aylık bütçe uyarısı eşiği |
| `ASSISTANT_PER_MINUTE_LIMIT` | `6` | Kullanıcı başına dakikalık sınır |
| `ASSISTANT_DAILY_LIMIT` | `20` | Kullanıcı başına UTC takvim günü sınırı |
| `ASSISTANT_MONTHLY_BUDGET_USD` | `50` | Aylık USD bütçesi |
| `ASSISTANT_MAX_OUTPUT_TOKENS` | `500` | Her üretim çağrısının çıktı token sınırı |
| `ASSISTANT_MAX_LLM_CALLS_PER_MESSAGE` | `3` | Tek mesajın üretim çağrısı sınırı; en fazla üç olabilir |
| `ASSISTANT_MAX_TOTAL_INPUT_TOKENS_PER_MESSAGE` | `12000` | Mesaj boyunca toplam girdi token sınırı; cache yazma/okuma dahil |
| `ASSISTANT_MAX_TOTAL_OUTPUT_TOKENS_PER_MESSAGE` | `1500` | Mesaj boyunca toplam çıktı token sınırı |
| `ASSISTANT_INPUT_PRICE_USD_PER_MILLION` | `1` | Milyon girdi token'ı başına USD |
| `ASSISTANT_OUTPUT_PRICE_USD_PER_MILLION` | `5` | Milyon çıktı token'ı başına USD |
| `ASSISTANT_CACHE_WRITE_PRICE_USD_PER_MILLION` | `1.25` | Milyon kısa süreli prompt-cache yazma token'ı başına USD |
| `ASSISTANT_CACHE_READ_PRICE_USD_PER_MILLION` | `0.10` | Milyon prompt-cache okuma token'ı başına USD |

Fiyat varsayılanları Claude Haiku 4.5'in
[resmi temel token tarifesine](https://platform.claude.com/docs/en/about-claude/pricing)
dayanır. Model seçimi yalnız `ASSISTANT_MODEL` ile değişir; maliyet hesabının doğru
kalması için seçilen modelin güncel girdi/çıktı/cache tarifeleri ilgili fiyat ayarlarında
tutulmalıdır. Para değerleri kayan nokta yerine `Decimal` olarak okunur.

Anahtar yoksa/boşsa veya özellik kapalıysa `available=False` ve
`unavailable_reason` üzerinden "Asistan kullanılamıyor" durumu alınır; config
yükleme uygulamayı çökertmez veya LLM çağrısı yapmaz. Anahtar repr ve config
serileştirmesine dahil edilmez. Geçersiz ayarlar açık doğrulama hatası üretir;
sessizce başka limitlere düşülmez. Model ve pozitif istek sınırları doğrulanır;
geçmiş sayısı, bütçe ve fiyatlar sıfır olabilir, negatif veya sonsuz olamaz.

Render anahtarı `sync: false` olarak tanımlar; anahtar değeri repoya veya frontend
`VITE_*` değişkenlerine yazılmamalıdır.

### Üyelik korumalı asistan API

- `POST /api/assistant/chat`: `{message, history?: [{role, content}], page_context?}`.
  Roller yalnız `user` / `assistant`; kimlik sadece doğrulanmış üyelik token'ından
  alınır. Gövde, geçmiş veya query içindeki `user_id` kullanılmaz.
  Yanıt `{reply, language: "tr"|"en", sources: [...]}`. Plan/fiyat/trial, kendi planı
  ve kredi sorularında aşağıdaki hızlı yol kullanılır; diğer mesajlar LLM'e gider.
  Türkçe karakter/anahtar kelime varsa TR, yoksa EN seçilir.
- `GET /api/assistant/usage`: `{remaining, total, resetsAt}`; UTC ertesi gece
  yarısında günlük hak yenilenir. Premium üyeler de aynı asistan limitlerine tabidir.
- Boş/geçersiz, yeni mesaj/sayfa bağlamı sınırını veya geçmiş mesaj sayısını aşan
  istekler `422`; geçmiş içeriği uzun olduğunda reddedilmez, kırpılır. Secret kontrolü
  geçmiş kırpılmadan önce yapılır. Secret/anahtar benzeri içerik LLM'e gönderilmez
  ve hak düşürülmeden uyarı döner. Bu desen tabanlı önlem tüm secret türlerini
  saptama garantisi değildir; kullanıcı sohbetle hiçbir kimlik bilgisi paylaşmamalıdır.
- Dakika penceresi ilk kabul edilen istekte başlar; günlük pencere UTC takvim günüdür.
  Limit aşımı `429`, yerelleştirilmiş bekleme süresi ve `Retry-After` içerir.
  Sağlayıcıya gönderilmiş başarısız çağrı da mesaj hakkından düşer; doğrulama,
  secret, bütçe ve yapılandırma nedeniyle reddedilen istekler düşmez.
  Çok turlu yanıt tek mesaj hakkı kullanır; sonraki turlar hak düşürmez.
- Aylık bütçe UTC ayına göre uygulama genelindedir. Gerçek input/output ve ayrı
  cache yazma/okuma token'ları kendi fiyatlarıyla `Decimal` maliyete çevrilir.
  Usage yoksa/geçersizse system/araç/mesaj zarfının UTF-8 bayt sayısı temkinli
  girdi tahmini olarak, çıktı için o çağrının çıktı tavanı kullanılır; girdi
  tahmini en yüksek yapılandırılmış girdi/cache tarifesiyle ücretlenir. Çağrı kaydında
  `estimated=true` ve metadata uyarısı bulunur. Asistan bu durumda çalışmayı sürdürür.
  Bütçe uyarısı her ay eşik ilk geçildiğinde yazılır.
- Bütçeye ulaşılmışsa LLM çağrısı yapılmaz (`503`, yoğunluk mesajı).
  Son kabul edilen çağrı gerçek/tahmini maliyetiyle tavanı aşabilir; bundan sonraki
  çağrılar kesilir. Bu bir ön rezervasyonla garanti edilen mutlak harcama tavanı değildir.
- Sayaçlar, aylık toplam ve yalnız metadata içeren çağrı kayıtları PostgreSQL'de
  `assistant_usage`, `assistant_monthly_spend`, `assistant_calls` tablolarında;
  DB pool yoksa mevcut `DATA_DIR` altında `assistant_usage.sqlite3` içinde tutulur.
  Cache token sütunları eski şemaya mevcut bakiye/kayıtlar korunarak eklenir;
  bu şema güncellemesi backend'ler arasında veri taşıma anlamına gelmez.
  Üretimde PostgreSQL veya kalıcı `DATA_DIR` diski gerekir. Backend değiştirmek
  mevcut kayıtların taşınmasını gerektirir; otomatik migration yapılmaz.
- Aylık satır kilidi (PostgreSQL `FOR UPDATE`, SQLite `BEGIN IMMEDIATE`) sağlayıcı
  her bir üretim çağrısı boyunca tutulur; maliyet araç çalıştırılmadan ve sonraki
  çağrıdan önce ayrı transaction ile kaydedilir. Eşzamanlı bütçe/limit kontrolünü
  korur ancak LLM çağrılarını
  aynı ay içinde global olarak sıraya koyar. PostgreSQL kullanıcı sayacı ayrıca
  satır kilidiyle korunur; ay geçişindeki eşzamanlı istekler de limiti atlayamaz.
  İstemci bağlantısı kesilse de hesaplama tamamlanır.
  Çağrı sonrası kayıt/commit başarısızlığı maliyeti belirsiz bırakırsa aylık
  `accounting_blocked` kaydı sonraki çağrıları kapatır. Veritabanına bu işaret de
  yazılamazsa yerel kalıcı `assistant-accounting-YYYY-MM.blocked` dosyası kullanılır;
  yönetici kayıtları uzlaştırmadan bu işaretler kaldırılmamalıdır. Yerel dosya
  farklı sunucular arasında paylaşılmaz; dağıtık DB kesintisinde uzlaştırma gerekir.
- Backend mesaj, geçmiş, sayfa bağlamı ve API anahtarını saklamaz veya loglamaz. Loglarda
  kullanıcı/zaman/token/maliyet/araç listesi ve tahmin işareti bulunur; sağlayıcı
  HTTP debug içerikleri ilgili çağrı sırasında bastırılır.

İşlem, ARM, consent veya abonelik değiştirme işlevi asistana bağlanmaz.
Analyst harcaması yalnız aşağıdaki ayrı açık onay endpoint'inden yapılabilir.
Aktif `TestnetFirstApp.tsx`, ayrı `AssistantChat.tsx` / `assistant.css` bileşenini
üyelik bağlamı altında kullanır. Kais AI başlık düğmesi desktop yan panelini veya
mobil sheet'i native dialog olarak açar; Escape/kapatma odağı açan düğmeye döndürür.
Başlık düğmesi mobilde de en az 44 × 44 px hedefle, diğer kontrollerin solunda
kalır; mevcut menü/işlem düğmeleri küçültülmez. Okunmamış yoklama rozeti gözün
üstünde gösterilir. Panel kendiliğinden açılmaz; çalışma alanı değişince kapanır.
Launcher yalnız header içinde, 58 px yuvarlak dokunma hedefinde 56 px göz ve
hafif turkuaz parlamayla gösterilir; sağ-alt floating yerleşimi veya ekrana
göre küçültme yoktur. Erişilebilir adı her dilde `Kais AI` olur. İlk sekme
oturumunda küçük etiket 5 saniye görünür; `sessionStorage` ile reload ve
header değişimlerinde tekrarlanmaz. Kalıcı düğme yazısı yoktur.
Gözün altındaki sabit karşılama balonu yalnız oturum açıkken yaklaşık 2 saniye
sonra çıkar ve 8 saniye görünür. Kullanıcıya özel `protrebot-kais-greeting:<id>`
localStorage anahtarında yerel tarih tutularak günde bir kez gösterilir; depolama
engelliyse gösterilmez. Gizli sekmede süreler durur, reduced-motion animasyonu
kapatır. X balonu kapatır; metin veya göz mevcut sohbeti açar. Balon LLM/API
çağrısı yapmaz, sohbet açıkken gösterilmez ve sayfa odağını kendiliğinden almaz.
Sohbet geçmişi yalnız tarayıcıda `kais-chat:v1:<encodeURIComponent(userId)>`
localStorage kaydında `{version: 1, savedAt, messages}` olarak tutulur; sunucuya
arşiv gönderilmez. Son 50 tamamlanmış mesaj, mesaj başına 4000 Unicode karakter
saklanır; 30 günden eski/bozuk/uyumsuz kayıt silinir. Bekleyen/başarısız mesajlar
ve onay nesneleri kaydedilmez. Depolama kopyasında Bearer/Basic, JWT, sk- ve
api/secret/token/key/parola değerleri, 32+ karakterlik anahtar benzeri dizeler
ve bilinen onay kanıtları `[MASKED]` yapılır; mevcut ekrandaki metin değişmez.
Depolama hatası yalnız console uyarısı üretir, sohbet bellekte çalışmaya devam eder.
Sıfırlama kaydı siler; ortak `clearUserSessionToken` çıkış/oturum temizliği tüm
`kais-chat:*` kayıtlarını siler. Yeni doğrulanmış kullanıcı açılırken diğer
kullanıcıların kayıtları temizlenir; eski sessionStorage arşivleri taşınmaz, silinir.
Sekmeler native storage olayıyla son yazan kazanır şeklinde eşitlenir; uzaktan
değişim bekleyen sohbet isteğini iptal eder. Model bağlamı sunucunun history_messages
sınırıyla ve ayrıca en fazla 12 tamamlanmış, boş olmayan, proaktif olmayan mesajla
sınırlıdır; örnek yapılandırmanın mevcut history_messages değeri 4'tür.
Profil yenilemesinde token yokluğu veya 401 ve owner erişim temizliği sohbet
arşivini de siler; auth kararları değişmez. Başka sekmedeki user-session anahtarı
silinince/değişince sohbet durdurulur. Nesil koruması eski yazarların kayıtları
yeniden oluşturmasını engeller; doğrulanmış profil sonrası yeni sohbet oturumu
başlar. Asistanın kendi 401'i yalnız mevcut kullanıcı kaydını temizler.
Üye bakım yoklamasının 401 fail-open davranışı aynen korunur. Playwright gerçek
AuthGate tıklamaları için `auth-e2e` modunda 4175, gerçek owner kapısı için root
production build preview 4176 kullanır; tam testlerden önce root build alınmalıdır.
CI bu nedenle hem root hem frontend bağımlılıklarını kurar ve preview öncesinde
root build alır. Ortak Demo/v21 UI fixture'ları durum limitlerini ve scanner,
settings, stream, history/performance koleksiyonlarını sözleşmeye uygun sağlar;
gerçek işlem veya ARM yanıtı üretmez.
Panel koyu yarı saydam cam yüzey/turkuaz vurgu, açık tema kontrast varyantı,
36 px göz ve durum başlığı, tek satır kota/yoklama ayarı kullanır.
Asistan balonunun solunda 24 px avatar, sağda turkuaz kullanıcı balonu vardır.
Öneriler sarılır; kontrollü textarea 48–120 px aralığında kendiliğinden büyür
(dar klavye görünümünde 80 px ile sınırlanır). Typing noktaları yalnız
transform/opacity ile nabız atar. Mobil sheet mevcut visualViewport'un
tam yüksekliğini ve safe-area boşluklarını kullanır; input/gönder sabit
alt bölümde kalır. Native dialog, onay, CopyProtection ve odak akışı korunur.
Masaüstü Master Trade ortak başlığı gizlediği için aynı düğme, mevcut terminal
başlığındaki boş slota portal ile taşınır; ikinci asistan oturumu oluşturulmaz.
Kök `main.tsx` → `TestnetFirstApp.tsx` → `AssistantChat.tsx` aktif zincirdir;
`frontend/src` altındaki aynı isimli eski uygulama bu tasarımın hedefi değildir.
`KaisEye.tsx` bağımsız, şeffaf arka planlı katmanlı bir SVG'dir. Dış segmentli
halka/balon kuyruğu, üst/alt kapak, göz akı, iris, bebeği, beyaz nabız çizgisi,
yansıma ve durum göstergeleri ayrı SVG gruplarıdır. `useKaisEye.ts`, idle
durumda 3–6 sn rastgele aralıkla 150 ms kırpma ve 30–60 sn aralıkla 500 ms
kısa yana bakış uygular. Masaüstü mouse örnekleri rAF ile kare başına tek kez
işlenir; iris/bebek 120 ms ease geçişle en fazla 3/2 SVG birimi kayar.
Mobilde mouse takibi yoktur; dokunulan noktaya bakış 650 ms sonra merkeze döner.
Hover/tıklama gözü açıp halkayı parlatır. Thinking durumunda halka döner/nabız
çizgisi atar; unread halkayı nabızlandırır. Kapalı/hatalı/özel alan state'leri
hareketsizdir; bütçe hatası error, kullanılamayan/sona eren oturum off gösterir.
API: `size` (varsayılan 56), `state` (`idle`, `thinking`, `private`, `error`,
`off`), `lookAt?: {x, y}` (-1..1 aralığına kırpılır), `unreadBadge?: boolean`.
`lookAt` verilirse otomatik takip/yan bakış yerine kontrollü bakış kullanılır.
`private` kapalı göz/kilit, `error` ve `off` sönük/yarı kapalı göz gösterir.
56/36/24 px boyutlarında kullanılır; küçük boyutlarda ince detaylar sadeleşir.
Renkler `tokens.css` içindeki `--kais-accent`, `--kais-glow`, `--kais-ink`,
`--kais-highlight` değişkenleriyle ayarlanır. `role="img"` ve state'e göre
sayfanın dilinde TR/EN etiketi vardır; standart `aria-label` prop'u yerelleştirilmiş etiketle
override edilebilir. SVG tanımları her instance için benzersizdir.
Göz bileşeni sayfa/form değeri okumaz, storage veya ağ çağrısı yapmaz.
Yalnız pointer koordinatları, kendi SVG geometrisi ve odaktaki/masaüstünde
üzerine gelinen alanın/atasının tam `data-private="true"` işareti kullanılır.
`useKaisPrivacy.ts` tüm gözler için tek document focusin/focusout ve
pointerover/pointerout dinleyici setini paylaşır. Odak veya hover gizli
alandaysa göz kapanır; ikisi de ayrılınca 400 ms sonra açılır. Yeniden giriş
beklemeyi iptal eder. Mobilde hover kullanılmaz. Kapak kapanışı/açılışı 150 ms
transform geçişidir; halka opacity ile sönükleşir. Reduced-motion'da bunlar
da statiktir. Marker değişimi ve hedef kaldırılması gözlenir; mutation
içeriği ve hiçbir alanın değeri okunmaz.
API Key/Secret ve tüm parola alanları (göster/gizle dahil) açıkça işaretlidir.
Yeni parola alanlarına `data-private="true"` eklemek zorunludur:
`kais/private-password` ESLint kuralı literal/koşullu password tiplerini
denetler. Eski frontend uyumluluk kopyalarına yalnız bu metadata eklenmiştir;
asistan davranışı aktif kök zincirdedir. Alan envanteri aktif import zincirini,
input/textarea türünü, konumunu ve marker'ı AST ile listeler:
`node tools\input-inventory.mjs --out <csv-dosyası>`.
Tüm göz animasyonları yalnız transform/opacity kullanır. Reduced-motion veya
gizli sekmede motion sınıfları, takip ve zamanlayıcılar kaldırılır; bakış merkezlenir.
Gizli sekmede pointer/focus dinleyicileri çıkarılır; yalnız görünürlük ve hareket
tercihi gözlemcileri yeniden etkinleştirme için kalır. Unmount hepsini temizler.
Mevcut yazıyor göstergesinin animasyonu da gizli sekmede/reduced-motion'da durur.
Yeni bağımlılık yoktur. Test-only örnek `frontend/tests/fixtures/kais-eye.html`,
iki temada tüm state/boyutları gösterir; production girişine eklenmemiştir.
Sayfa tepkileri `kais:react` adlı tek yönlü, istemci içi CustomEvent ile bağlanır.
`kais-reactions.ts` yalnız `premium-open`/`navigation` için yayıncının kendi ref
geometrisinden elde edilen viewport koordinatlarını, `error`/`unread` için yalnız
türü iletir. Metin, alan değeri, DOM içeriği, kimlik veya olay hedefi taşınmaz;
LLM/backend/ağ/storage çağrısı yoktur. Premium kartı açıldığında ve ana çalışma
alanı/Master Trade/Demo sekmeleri değiştiğinde 650 ms bakış, açıkça `error`
türündeki bildirimlerde 600 ms şaşırma, yeni okunmamış yoklamada 1200 ms halka
nabzı kullanılır. Okunmamış rozetinin mevcut mesaj/okuma akışı korunur.
60 saniye hareketsizlikte 600 ms yavaş kırpma, 3 dakikada yarı kapanma olur.
Pointer/dokunma, klavye ve scroll yalnız olay oluşumu olarak sayılır; tuş veya
içerik okunmadan göz uyanır. Private/error/off durumları tepkilere üstün gelir.
Reduced-motion ve gizli sekmede tepkiler tamamen kapalıdır; bekleyen gezinme
kareleri iptal edilir, görünürlük geri geldiğinde eski tepkiler oynatılmaz.
Premium dialoguna yalnız açılıştan sonra geometrik olay yayımı eklenir;
erişim, consent, ARM ve ticaret iş mantığı değişmez. Mobil composer `visualViewport`
yükseklik/offset değişikliklerini izler; küçük görünümde öneriler gizlenerek
klavye sırasında giriş alanına yer bırakılır.

`GET /api/assistant/usage` mevcut sayaçların yanında `limits` döndürür:
`max_input_chars`, `history_messages`, `history_message_max_chars`,
`page_context_max_chars`, `secret_min_alphanumeric_chars`. Bunlar mevcut
`AssistantConfig` değerleridir; frontend iş limitlerini tekrar sabitlemez.
Geçmiş mesaj sayısı/uzunluğu gönderilmeden önce bu ayarlara göre kırpılır;
sunucu doğrulaması ayrıca devam eder. Hata yanıtlarındaki isteğe bağlı
`error_code`, özellikle bütçe kesiciyi diğer sağlayıcı hatalarından ayırır.

Sohbet metni üyeye göre adlandırılmış `sessionStorage` kaydında, sunucuda değil
tarayıcı sekmesinde tutulur. HTML çalıştırılmaz; yalnız satır sonları ve basit
kalın metin render edilir. Kontrollü taslak mevcut `assistant_api.py`
secret deseni ve sunucudan gelen minimum uzunlukla eşleşince sohbet alanı
private işaretlenir ve altında TR/EN Secret uyarısı duyurulur. Bu yeni kontrol
yalnız sunum içindir; taslağı incelemek ağ/storage yazımı yapmaz. Önceden var
olan istemci gönderim/geçmiş kontrolü ve asıl backend engellemesi değişmez.
Bilinen secret/credential desenleri istemcide de engellenir. Secret içeren geçmiş
LLM'e gönderilmez. CopyProtection yalnız sohbet kapsayıcısı/seçimi için
istisna tanır; diğer sayfalarda mevcut davranış devam eder.

`needs_confirmation` gösterildiğinde yalnız kullanıcı **Onayla** seçerse
`POST /api/assistant/analysis/confirm` üzerinden `confirm=true`, hedef ve
sunucunun verdiği onay token'ı gönderilir; chat endpoint'ine otomatik tekrar
gönderim yapılmaz. Onay token'ı storage/geçmiş/LLM'e eklenmez.
Vazgeçmek istek göndermez; reload sonrasında bekleyen onay yeniden alınır.
Ağ hatasında aynı onayın yeniden kullanılması backend idempotency davranışını
korur. Canlı emir/ARM/consent/abonelik mutasyonları bu UI'ye bağlanmaz.

UI doğrulama (frontend'in kendi Playwright kurulumunu kullanın):
```powershell
node --test tools\eslint-private-fields.test.mjs
Set-Location frontend
npx playwright test assistant-chat.spec.ts --project chromium
npx playwright test kais-eye.spec.ts --project chromium
```

Son UI sözleşme kontrolleri: `node --test tools\eslint-private-fields.test.mjs tools\kais-ui-contracts.test.mjs`.
Kök ve frontend Vite girişleri ortak bileşenler için `react`/`react-dom`
dedupe kullanır; iki ayrı node_modules kopyası soğuk başlangıçta farklı hook
dispatcher'ları oluşturamaz. Bu ayar da sözleşme testleriyle korunur.
Live fixture'ları doğrulanmış yetkili kullanıcıyı taklit eder; premium ve
consent/ARM kapıları üretimde değişmez. Scanner eşzamanlı tarama testi ilk mock
yanıtını bekletir, işlem sırasında kontrollerin kilitli ve POST sayısının bir
olduğunu, tamamlandıktan sonra yeni taramanın hâlâ mümkün olduğunu doğrular.
Aktif `main.tsx` import zincirinin UI metinlerinde eski asistan adı ve göz
bileşeni/hook'larında input değeri veya DOM içeriği erişimi AST ile denetlenir.
Playwright 10 dakikalık sanal boşta kalmayı çalıştırır; sahip olunan listener,
timer ve rAF sayılarının büyümediğini, gizli sekme/unmount temizliğini ve GC
sonrası heap artışının 2 MiB'yi aşmadığını kontrol edip ölçümleri ekler.
Bu bir gerçek-zamanlı uzun süreli heap/retainer incelemesinin yerine geçmez.
Manuel ek kontrol: Chrome DevTools Memory'de GC sonrası başlangıç snapshot'ını
alın, görünür sayfayı 10 gerçek dakika boşta bırakın, GC + ikinci snapshot alın.
Göz/hook closure'ları ve detached SVG/input node'larında birikim olup olmadığını
karşılaştırın; 5 aç/kapat veya mount/unmount turundan sonra tekrar ölçün.
Sekmeyi gizleyip Performance kaydında göz animasyonlarının durduğunu doğrulayın.

### Asistan sözleşme ve isteğe bağlı gerçek sağlayıcı testleri

`backend/tests/test_assistant_contracts.py` kullanıcı izolasyonu, ücretsiz
çıktı filtreleri, premium dahil seviye gizleme/yönlendirme, araç/import salt-okunur sınırı,
onay/cache/iade, injection, secret, limit, bayat koruma ve dil çiftlerini
test eder. Kurala uygun mock yanıtı gerçek model davranışını kanıtlamaz;
ayrı adversarial-output testleri kurala uymayan sağlayıcı yanıtlarını sınar.

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'backend'
$assistantTests = (Get-ChildItem backend\tests\test_assistant_*.py).FullName
.\.venv\Scripts\python.exe -m pytest @assistantTests -q
```

Gerçek Anthropic testleri yalnız açık opt-in ile çalışır. API key'in mevcut
olması tek başına yeterli değildir. Sadece sentetik sorular gönderilir;
gerçek kullanıcı, pozisyon veya credential verisi gönderilmez. Üç canlı
senaryo sağlayıcı ücreti oluşturabilir; kayıtlar geçici test veritabanındadır,
üretim aylık bütçesi/kotasıyla ortak değildir. Gerçek çağrılar için:

```powershell
$env:ASSISTANT_LIVE_TESTS = '1'
# ANTHROPIC_API_KEY ve diğer asistan ayarlarını backend ortamında yapılandırın.
.\.venv\Scripts\python.exe -m pytest backend\tests\test_assistant_live.py -q
Remove-Item Env:\ASSISTANT_LIVE_TESTS
```

Frontend copy/plain-text kontrolleri:
```powershell
Set-Location frontend
npx playwright test assistant-chat.spec.ts --project chromium --grep "copy exception" --timeout 60000
```

### Uygulama içi durum yoklamaları

`GET /api/assistant/proactive/preferences` ayarı okur;
`POST /api/assistant/proactive/preferences` yalnız `{enabled: boolean}` kaydeder.
`POST /api/assistant/proactive/check-in` yalnız dil alır ve uygun durum özeti
varsa döndürür. Üç endpoint de mevcut üyelik doğrulaması altındadır; kimlik
yalnız authenticated kullanıcıdan alınır. Ücretsiz ve premium üyeler aynı
salt-okunur, kullanıcıya bağlı araç filtrelerini kullanır.

Görünür uygulamada kontrol yapılır; sohbet kendiliğinden açılmaz. Yeni özet
yalnız açma düğmesinde rozet ve sohbet içinde mesaj olarak görünür. Paneli
açmak rozeti temizler. Sohbetteki **Durum yoklamaları** anahtarı sunucuda
kullanıcıya bağlı saklanır; kapalıyken yeni yoklama üretilmez.

Önce açık pozisyon sayısı, toplam gerçekleşmemiş PnL ve doğrulanmış koruma
durumu mevcut araçlardan okunur. Veri eski/belirsizse açıkça belirtilir;
eksik PnL sıfır kabul edilmez. Pozisyon yoksa yapılandırılan uzun ziyaret
arası için kısa hatırlatma verilebilir. Ziyaret geçmişi bu özelliğin kendi
`last_seen_at` kaydıdır; ilk kullanımda geçmiş yoksa yokluk tahmin edilmez.
Kapalı ayarın yeniden açılması mevcut ziyareti kaydeder.

Sunucu yalnız ayar ve ziyaret/yoklama zamanlarını saklar; mesaj gövdelerini
saklamaz. PostgreSQL kullanıcı satırı kilidi veya SQLite işlem kilidiyle
son yoklamadan itibaren kayan bekleme atomik uygulanır; gece yarısı
sıfırlanmaz. Yanıt tesliminden önce hak ayrılır: ağda kaybolan yanıt aynı
bekleme süresinde yeniden üretilmez. İptalden önce teslim edilmiş mesajlar
geçmişte kalır.

Bu akış LLM çağırmaz, kredi harcamaz ve sohbet mesaj kotasını kullanmaz.
Push/e-posta, acil SL uyarısı, emir/ARM/consent/abonelik işlemi veya yatırım
önerisi içermez. Araçlar mevcut kullanıcıya ait backend snapshot'larını
okur; yoklamalar bağımsız canlı exchange izleme/koruma garantisi değildir.
Yoklama metinleri sonraki LLM sohbet geçmişine gönderilmez.

### Asistan araçları ve LLM'siz hızlı yol

`backend/app/assistant_tools.py` kimliği dışarıdan doğrulanmış üyeden alır ve
istekteki token ile tekrar eşleştirir. Araç şemalarında `user_id` yoktur; fazladan
kimlik/onay/mutasyon argümanları reddedilir. Tüm sonuçlar
`{data, fetched_at, stale}` ve API seviyesinde `sources` içerir.

| Araç | Salt-okunur kaynak / davranış |
| --- | --- |
| `get_plans` | `subscription_core.PLAN_CATALOG`, `TRIAL_DAYS`, `CANCELLATION_RULES`; iade politikası tanımlı değilse belirtilmez |
| `get_my_access` | `access_snapshot` premium yetkisi + mevcut `subscription_for_user` plan/durumu; yalnız bu üç alan |
| `get_my_credits` | Mevcut Analyst servisi; bakiye, bütçe, `analysis_cost`, kalan süre ve mevcut pencere/cache ayarları |
| `search_help` | `assistant_kb/tr.json` ve `en.json` içindeki kodla doğrulanmış yardım; kullanıcının dilinde en fazla `HELP_RESULT_LIMIT` sonuç; iş değerleri çalışma anında doldurulur |
| `get_analysis` | Kullanıcıya özel cache salt-okunur kontrolü; geçerli cache ücretsiz, cache yoksa sonuç yerine `needs_confirmation` |
| `get_my_positions` | V25'in yalnız doğrulanmış kullanıcı **ve** exchange oturumu eşleşen snapshot'ı; sadece sembol/yön/miktar/PnL |
| `get_protection_status` | Aynı sahiplik kontrolü, backend reconciliation ve exact-stop sınıflandırması; hiçbir borsa çağrısı/koruma onarımı yapılmaz |

- `POST /api/assistant/tools/{name}`: gövde yalnız araç argümanlarıdır.
  Örnekler: `get_analysis` için `{symbol, timeframe}`, `search_help` için
  `{query, language}`, `get_protection_status` için `{symbol, direction?, language?}`.
- Cache miss sonucundaki `needs_confirmation` hedef, gerçek harcama maliyeti ve
  kullanıcı/hedef/maliyet/son kullanma zamanına bağlı imzalı `confirmation_token`
  içerir. Premium'da gerçek harcama maliyeti sıfırdır; yeni analiz yine onay ister.
- `POST /api/assistant/analysis/confirm`:
  `{symbol, timeframe, confirm: true, confirmation_token}`.
  UI açık onayından sonra bu **ayrı** istek gönderilmelidir. Token süresi mevcut
  cache penceresinden alınır. Hedef/kimlik/maliyet değişimi veya süre aşımı reddedilir.
  Token'dan sunucuda üretilen idempotency anahtarı mevcut Analyst consume akışına
  verilir; aynı onay tekrar gönderilince çift harcama/iade olmaz.
  Chat gövdesindeki veya araç argümanındaki `confirm` analiz çalıştıramaz.
- Analiz özeti yön, Final Decision skoru, confidence, opportunity ve MTF uyumunu
  taşır. Mevcut producer piyasa zaman damgası vermediğinde veri yaşı `null`,
  `data_age_reason=ANALYSIS_TIMESTAMP_UNAVAILABLE` ve `stale=true` olur;
  cache açılma zamanı piyasa veri yaşıymış gibi sunulmaz.
- Ücretsiz çıktılarda `premium_access.public_projection` uygulanır.
  Entry/SL/TP ve strateji gerekçeleri hiçbir asistan özetine eklenmez.
  Premium dahil seviyeler yalnız Master Trade ekranından incelenir; sohbet
  ve onay sonrası analiz özeti bu ekrana yönlendirir.
  Korumanın güvenli doğrulama nedeni ayrı `verification_reason` alanıdır.
- Koruma/pozisyon etkin eşiği asistan ayarı ile mevcut reconciliation aralığından
  hesaplanır. Eşiğin üstündeki koruma verisinde `verified=false`, `stale=true`;
  mesajda `data_age_seconds` kadar önce alınmış olduğu belirtilir. Zaman/sahiplik
  bilinmiyorsa yaş uydurulmaz. Exact backend kanıtı olmayan veya hata/ambiguity
  içeren veri doğrulanmış koruma sayılmaz. `verified=true, protected=false`
  doğrulanmış stop bulunmaması anlamına gelir; güvenli/korumalı anlamına gelmez.
- `public_status` mevcut ARM durumunu normalize edebildiğinden asistan onu
  çağırmaz; yeni `read_owned_account_state` sadece sahiplik eşleştirip veri kopyalar.
  İşlem, ARM, consent ve otomasyon state'i değişmez.
- `assistant_fastpath.py` fiyat/trial, kendi planı ve kredi sorularına yalnız
  araç verisiyle TR/EN şablon yanıtı üretir. Bu yanıtlar LLM/dakika/gün kotasından
  düşmez; API anahtarı eksik veya LLM bütçesi dolu olsa da çalışır.
  `ASSISTANT_ENABLED=false` tüm asistan yollarını kapatır.
- Kais AI kimlik sorularına (`Sen kimsin?`, `Who are you?`) kısa TR/EN hızlı
  yanıt verir: yapay zeka asistanıdır; plan, kredi, API bağlantısı ve platform
  kullanımı hakkında yardım eder, işlem yapmaz. Bu kimlik yanıtı araç/model
  çağırmaz ve kredi harcamaz. Ek talimat içeren mesajlar kimlik hızlı yoluna
  alınmaz. Sistem promptu insan olduğunu iddia etmeyi ve altyapı şirketi/modeli
  uydurmayı yasaklar; altyapı sorularında bilginin paylaşılamadığını belirtir.

### Paketlenmiş TR/EN bilgi tabanı

`backend/app/assistant_kb/tr.json` ve `en.json` aynı makale kimliklerini içerir.
Her makalede başlık, içerik, arama anahtar kelimeleri ve doğrulama için
`code_sources` bulunur. API bağlantısı/Secret güvenliği, ortam farkları, üyelik,
plan/Billing, kredi, premium, aktif ekranlar, izin sayaçları, terim sözlüğü
ve risk uyarıları kapsanır. Paper kapalı dağıtım özelliği olarak açıklanır;
aktif menüye bağlı olmayan eski ticari/lisans/ajan ekranları, doğrulanmamış
iki faktörlü doğrulama veya destek hizmetleri kullanılabilir diye tanıtılmaz.

Makale metinlerinde fiyat/süre/kredi sayısı sabitlenmez. Placeholder'lar her
okumada kaynak değerlerle doldurulur; yalnız makale şablonları cache'lenir:

| Placeholder | Kaynak |
| --- | --- |
| `{ANALYST_BUDGET}`, `{ANALYSIS_COST}`, `{CREDIT_WINDOW_HOURS}`, `{ANALYST_CACHE_MINUTES}` | Mevcut Analyst servisinin `CreditConfig` değerleri |
| `{MASTER_PLAN_NAME}`, `{MASTER_MONTHLY_PRICE}`, `{TRIAL_DAYS}` | `subscription_core.PLAN_CATALOG` / `TRIAL_DAYS` |
| `{DEMO_ARM_MINUTES}` | `binance_demo.ARM_SECONDS` |
| `{LIVE_CONSENT_HOURS}` | `v25_execution.LIVE_CONSENT_SECONDS` |
| `{LIVE_ARM_HOURS}` | `v25_execution.LIVE_ARM_SECONDS` |
| `{AUTO_SESSION_MINUTES}`, `{CONSENT_GRACE_MINUTES}` | `v25_execution.LIVE_AUTO_SESSION_SECONDS` / `LIVE_CONSENT_GRACE_SECONDS` |

`assistant_help.py` başlık/anahtar kelime/gövde eşleşmelerini ağırlıklandırır;
Türkçe karakterleri normalize eder, ortak soru sözcüklerini çıkarır ve yalnız
seçilen dilde arar. Harici vektör DB veya yeni arama bağımlılığı yoktur.
LLM araç döngüsünde `search_help.language` modelin seçimi yerine sunucunun
sohbet için belirlediği dile sabitlenir. Boş/eşleşmeyen sorgu boş sonuç döner;
eksik/bozuk KB veya geçersiz placeholder açık metadata logu ve güvenli
`503` araç hatası üretir. KB dosyalarını değiştirdikten sonra backend'i
yeniden başlatmak şablon cache'ini yeniler.

### Anthropic araç döngüsü ve sistem politikası

`backend/app/assistant_llm.py` Anthropic Messages API'yi araç şemalarıyla kullanır.
`backend/app/assistant_prompt.py` kısa TR/EN yardım, doğrulanmış sayılar, bayat veri,
yatırım riski, salt-okunur yetki, premium detaylar ve prompt-injection sınırlarını
tanımlar. Sistem politikası ve son araç şeması `cache_control: ephemeral` içerir;
kullanıcıya ait dinamik mesajlara cache işareti eklenmez. Gerçek cache hit'leri
sağlayıcının model/prefix uygunluğuna bağlıdır; canlı sağlayıcı testi yapılmamıştır.

Her mesajda en fazla yapılandırılmış sayıda (üst sınır üç) üretim çağrısı yapılır.
Her tur öncesi bütçe/kota kontrolü ve Messages token-count ön kontrolü uygulanır.
Toplam girdi sınırına cache token'ları dahil edilir; çıktı tavanı kalan token
bütçesine göre küçültülür. Token-count ile gerçek usage farklı olabilir:
gerçek usage tavanı aşarsa maliyet yine kaydedilir ve yeni tur yapılmaz.
Token-count başarısızlığı üretim çağrısı/hak düşümü oluşturmaz.

Bütçe döngünün ortasında dolarsa yeni üretim/token-count çağrısı yapılmaz;
o ana kadarki güvenli araç özetleriyle yoğunluk yanıtı verilir. API hataları
genel/yerelleştirilmiş yanıt döndürür; sağlayıcı hata ayrıntıları paylaşılmaz.
Taze analiz için onay bilgisi doğrudan HTTP yanıtına çıkar; onay token'ı LLM'e
gönderilmez ve araç döngüsü analiz harcamasını onaylayamaz. Analiz/koruma
yanıtlarında backend özeti kullanılır; doğrulanmamış koruma iddiası engellenir.

`assistant_response.py` son model metnini sunucuda denetler. JSON/nested JSON,
Markdown ve düz metindeki seviye çıktıları tüm üyelerde; özel gerekçe alanları
ve analiz gerekçesi kalıpları ücretsiz üyelerde güvenli TR/EN yanıtla değiştirilir.
Sistem politikasının ayırt edici başlıkları ve talimat bölümlerinin birebir
alıntıları genel ret yanıtına çevrilir. Reddedilen içerik loglanmaz; çağrının
mevcut token/maliyet kaydı korunur. Bu kontrol bilinen metin/alan kalıpları
üzerindedir; her olası anlamsal paraphrase'i doğrulayan ayrı bir model değildir.

Doğrulama:
```powershell
$env:PYTHONPATH='backend'
.\.venv\Scripts\python.exe -m pytest backend\tests\test_assistant_config.py backend\tests\test_assistant_api.py backend\tests\test_assistant_tools.py backend\tests\test_assistant_llm.py backend\tests\test_assistant_help.py -q
.\.venv\Scripts\python.exe -m ruff check backend\app\assistant_config.py backend\app\assistant_api.py backend\app\assistant_storage.py backend\app\assistant_tools.py backend\app\assistant_fastpath.py backend\app\assistant_help.py backend\tests\test_assistant_config.py backend\tests\test_assistant_api.py backend\tests\test_assistant_tools.py
```

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

# WR Research Index

Bu doküman, `backend/experiments/` altındaki EXP-WR-* (ve ilgili EXP-001..004, EXP-ATR-*, EXP-BQ-*, EXP-MTF-001, EXP-TR-001) araştırma artifact'larının salt-okunur bir indeksidir. Yeni hesaplama yapılmadı; yalnızca mevcut MD/JSON raporları okunup özetlendi.

**Canonical baseline (değişmedi):** 78/50, kilitli (`backend/canonical_baseline_2026_09_16.json`, `status: CANONICAL_BASELINE_LOCKED`). Hiçbir deney bu politikayı değiştirmedi veya production'a taşımadı.

---

## Özet Tablo

| Deney | Durum/Etiket | Kısa Not |
|---|---|---|
| EXP-001 | Descriptive only | Tarihsel veride tekrarlayan bir "başarısızlık deseni" var; production kuralına dönüştürülmedi. |
| EXP-002 | Closed, no action | Treatment-vs-baseline karşılaştırması anlamlı bir mekanizma/eşik iyileştirmesi ortaya koymadı. |
| EXP-003 | Not validation-ready | EMA yapısı tanımlayıcı düzeyde ilgili görünüyor ama eşik ailesi incelendiğinde seyrekleşiyor/dağılıyor. |
| EXP-004 | Weak evidence, n yetersiz | Final OOS (n=9) üzerinde kayıp mekanizması analizi; en kötü 3 işlem toplam kaybın çoğunu oluşturuyor ama bu evrensel bir mekanizmanın nedensel kanıtı değil. |
| EXP-WR-001 | IN_PROGRESS (checkpoint) | Checkpoint dosyasında `"status": "IN_PROGRESS"` olarak kayıtlı; tamamlanmış bir sonuç raporu yok. |
| EXP-WR-003 | **NO_CLEAR_MECHANISM** | Kanıt-sınırlı bir sonuç, "mekanizma yok" kanıtı değil. Persisted control: 65 trade, WR %67.69, PF 1.1679. |
| EXP-WR-004 | Persistence altyapısı | Trade ledger persistence patch'i; yeniden replay yapılmadığı için bu görevde yeni ledger üretilmedi. |
| EXP-WR-006 | **PRELIMINARY_MECHANISM — needs full 5-fold replay** | Pozisyon-doluluğu/yol bağımlılığı etkisi tanımlanıyor; aynı trade'de sonuç tersine dönmesi (WIN→LOSS) gözlemlenmedi. |
| EXP-WR-007 | **DATA_LIMITED** | BQ/EMA/ADX/ADX-slope/yön hiçbiri kazanan/kaybeden işlemleri net ayırmıyor; STOP=kayıp/TP=kazanç eşleşmesi simülatör etiketlemesini yansıtıyor, entry mekanizmasını değil. |
| EXP-WR-008 | **TRAIN_ONLY_SURVIVOR** | Yalnızca train-only doğrulama; production onayı değildir. (Bkz. aşağıdaki "Sayı Tekrarı" notu.) |
| EXP-WR-009 | **BLOCKED_SELECTION_LEAKAGE** | Aday, tüm 5 expanding train fold sonucu gözlemlendikten sonra seçildiği için validation çalıştırılmadı (sızıntı riski). |
| EXP-WR-010 | **DESIGN_OPTIONS_READY** | Sadece tasarım; çalıştırma yok. İki temiz doğrulama seçeneği sunuluyor (Global Holdout / Nested Walk-Forward). |
| EXP-WR-011 | **VALIDATION_REJECTED** | Global holdout doğrulaması reddedildi. |
| EXP-WR-012 | **MIXED** | 2/1 trade'lik holdout örneklemi; beklenen filtre-attrition + pozisyon-doluluğu attrition + yetersiz güçte (underpowered) 500 mumluk validation örnekleminin birleşimi ile açıklanıyor. Framework bug'ı bulunmadı. |
| EXP-WR-013 | **DESIGN_READY** | Sadece tasarım; nested walk-forward iç/dış fold geometrisi tanımlanıyor, çalıştırma yok. |
| EXP-WR-014 | **NESTED_VALIDATION_INCONCLUSIVE** | n=11, `sample_size_warning.underpowered: true`, Wilson 95% CI çok geniş ([%15.2–%64.6]). |
| EXP-WR-H1 | **REJECTED** (H1_STATUS) | ADX-slope filtresi 5 fold'un hiçbirinde WR/expectancy/PF/DD'yi iyileştirmedi. |
| EXP-WR-H2 | **INCONCLUSIVE** (H2_STATUS) | ATR volatilite tanılaması sonuçsuz. |
| EXP-WR-CACHE-001/002 | Altyapı (cache) | Kanonik karar/execution cache'leri; kendi başına bir strateji sonucu değil. |
| EXP-ATR-001 | Research-only forensic audit | Production değişikliği yok. |
| EXP-ATR-002 | Train-only performans karşılaştırması | Ayrı bir strateji onayı değil. |
| EXP-BQ-001/002/003 | Script + JSON rapor (BQ filtre denemeleri) | Bu indekste yalnızca varlığı doğrulandı; detaylı sonuç bu doküman kapsamında tekrar özetlenmedi. |
| EXP-MTF-001 | Script + JSON rapor (MTF denemesi) | Bu indekste yalnızca varlığı doğrulandı. |
| EXP-TR-001 | Script + JSON rapor (TR denemesi) | Bu indekste yalnızca varlığı doğrulandı. |

---

## Önemli Not: EXP-WR-008 ve EXP-WR-H1-CONTROL Sayı Tekrarı

EXP-WR-008'in raporladığı "Treatment C" toplu sonucu (65 trade, WR %67.6923, Net PnL +4.007119, Expectancy +0.061648, PF 1.16794, Max DD 2.280437) ile EXP-WR-H1'in raporladığı "control" toplu sonucu (65 kapanmış trade, WR %67.6923076923077, net_pnl 4.007118835, expectancy 0.061647982076923076, profit_factor 1.16794452703014, max_drawdown 2.280437419000009) **aynı sayıları paylaşıyorlar; bunlar iki bağımsız başarı sonucu olarak değerlendirilmemelidir.**

---

## Genel Durum

`RELEASE_DECISION_MEMO.md`: **Status: HOLD / RESEARCH ONLY**. Hiçbir deney, validasyona hazır/production'a alınabilir bir kural, eşik değişikliği veya strateji değişikliği üretmedi. Canonical 78/50 tüm bu araştırma sürecinde değişmeden kaldı.

---

*Bu doküman salt-okunur bir indekstir. Herhangi bir kod, test, backtest, OOS, walk-forward veya sweep çalıştırılmadan, yalnızca mevcut artifact'lar okunarak oluşturulmuştur.*

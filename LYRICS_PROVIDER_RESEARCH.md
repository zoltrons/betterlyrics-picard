# Kelime bazlı lyrics: LiMusic araştırması ve geliştirme önerileri

Tarih: 2026-10-11. Araştırma bulgularının uygulanma durumu aşağıdaki raporda ayrıca belirtilmiştir.

## İncelenen sürümler ve kaynaklar

- [LiMusic](https://github.com/SimoHypers/limusic/tree/927cf5d5bd1528514e1f542ae9154d54ceea7934): `927cf5d5bd1528514e1f542ae9154d54ceea7934`.
- [LiMusic lyrics backend](https://github.com/SimoHypers/limusic/blob/927cf5d5bd1528514e1f542ae9154d54ceea7934/src-tauri/src/lyrics.rs): `boidu_get`, `lyricsplus_get`, `kpoe_to_lyrics`, `fetch`, `parse_ttml_spans`.
- [LiMusic gösterimi](https://github.com/SimoHypers/limusic/blob/927cf5d5bd1528514e1f542ae9154d54ceea7934/ui/src/lib/components/LyricsView.svelte): kelime ilerlemesi ve oynatma saati.
- [Better Lyrics API](https://github.com/better-lyrics/api/tree/8b0ac346ac90ae38be0636611fcadbe8fe351106): backend akışı ve erişim politikası.
- [Better Lyrics endpoint belgesi](https://docs.betterlyrics.org/reference/get-lyrics).
- [LyricsPlus endpoint belgesi](https://github.com/ibratabian17/LyricsPlus/blob/7e76631805e440018138587567dcaa6542147af5/docs/endpoints.md).
- [LyricsPlus Apple sağlayıcısı](https://github.com/ibratabian17/LyricsPlus/blob/7e76631805e440018138587567dcaa6542147af5/internal/providers/apple.go).
- [LiMusic sağlayıcı ekleme konusu](https://github.com/SimoHypers/limusic/issues/46).

## 1. LiMusic bunu nasıl yapıyor?

### Boidu / Better Lyrics

LiMusic `https://lyrics-api.boidu.dev/getLyrics` adresine GET gönderiyor:

- `s`: başlık
- `a`: sanatçı
- `d`: tam saniye cinsinden süre
- `al`: varsa albüm

Süre yoksa bu sağlayıcıyı atlıyor. Önce albümlü sorguyu deniyor; 401 gelirse
albümsüz anahtarı deniyor. LiMusic bu entegrasyonda API anahtarı göndermiyor.
Yanıttan öncelikle `ttml`, alternatif olarak `syncedLyrics`, `lyrics`, `lrc`
alanlarını okuyup kendi satır/kelime modeline çeviriyor.

Güncel Better Lyrics belgesi canonical adres olarak
`https://api.betterlyrics.org/getLyrics` kullanıyor. Önbellekteki sonuçlar
anahtarsız okunabilir; cache miss, sunucu politikasına göre `X-API-Key` ister.
401 bu yüzden "şarkının sözleri yok" anlamına gelmez. Proje, Better Lyrics'e
özel anahtarın başka projelere verilmediğini açıkça belirtiyor. Paylaşılan
uzantı anahtarına bağımlı bir Picard entegrasyonu önerilmiyor.

Backend'in güncel kaynak sırası: Postgres cache → katalogdan ISRC çözümü ve
LRC.red → diğer upstream sağlayıcı. Bulduğu yeni veriyi yapılandırılmışsa
LRC.red'e katkı olarak gönderiyor. Boidu ile LRC.red'in kapsaması bu nedenle
bağımsız sayılmamalı; sağlayıcı eklemenin gerçek katkısı ölçülmeli.

### LyricsPlus

LiMusic `https://lyricsplus.binimum.org/v2/lyrics/get` adresine
`title`, `artist`, opsiyonel `album` ve saniye cinsinden `duration` gönderiyor.
KPOE V2 yanıtını okuyor:

- `type`: `Word`, `Line` vb.
- `lyrics[].time`, `lyrics[].duration`: milisaniye.
- `lyrics[].syllabus[]`: kelime/hece metni, başlangıç ve süre.
- `transliteration`: varsa Latin harfli okunuş ve kendi zamanlamaları.
- `metadata.totalDuration`: parçanın süresi.

İki tarafta süre varsa 5 saniyeyi aşan farkı reddediyor. Bir tarafta süre yoksa
bu kontrol devre dışı kalıyor; bu yaklaşım kusursuz kimlik eşleştirmesi değil.

LyricsPlus backend'i yalnız Apple değildir: Apple, Musixmatch, Spotify, QQ ve
diğer kaynakları birleştirebilir. Apple sağlayıcısı önce ISRC/katalog eşleştirmesi
yapar, yapılandırılmış sunucu tarafı hesap bilgileriyle syllable-lyrics TTML
verisini alır, sonra KPOE'ye dönüştürür. LiMusic'in kendisi Apple oturumu açmaz.
"Apple kaynaklı" etiketi yalnız gerçek kaynak metadata'sı doğrulanınca verilmeli.

### Gösterim

LiMusic metni yapay zekâyla zamanlamıyor. Gelen kelimelerin başlangıç/bitiş
zamanlarını oynatma konumuyla karşılaştırıyor. Aktif kelimenin ilerlemesi
`(konum - başlangıç) / (bitiş - başlangıç)` ile hesaplanıp renk geçişi çiziliyor.
mpv konum güncellemeleri arasını yerel saat ve animation frame ile tamamlıyor.
Picard bir oynatıcı olmadığı için bizim önceliğimiz bu animasyonu değil, zaman
verisini kayıpsız dışa aktarmak olmalı.

## 2. Mevcut eklentide doğrulanan eksikler

`__init__.py` üzerinde, ağ kullanmayan sentetik metinlerle kontrol edildi:

1. `betterlyrics_clean_word_timestamps=True`: varsayılan LRC çıktısı kelime
   işaretlerini kaldırıyor. **Ham TTML ise korunuyor**; tüm verinin kaybolduğu
   iddia edilmiyor.
2. `lrc_to_ttml()` enhanced LRC kelime işaretlerini siliyor; zamanlı `<span>`
   üretmiyor. ELRC→TTML yolu yalnız satır zamanını koruyor.
3. `ttml_to_lrc(word_synced=True)` iki span arasındaki tail boşluğunu atlıyor.
   Sentetik `Test` + boşluk + `words`, `Test<00:02.00>words` olarak birleşiyor.
4. `parse_ttml_time("1500ms")` ValueError üretiyor. Zaman biçimleri daha geniş
   ve kontrollü işlenmeli; bozuk tek satır bütün sonucu düşürmemeli.
5. `resolve_lyrics_bundle()` içinde KPOE V2 parser'ı ve Boidu/LyricsPlus
   sağlayıcısı yok. Better Lyrics API URL/anahtar alanı tanımlı ama bağlı değil.
6. `prefer_synced` yalnız eşit metadata skorlarında etki ediyor. Word/line
   ayrımı ve indirilmiş içeriğin gerçek kalitesi ayrı bir seçim aşaması değil.
7. Ağ katmanı status/header bilgisini üst katmana taşımıyor; `None`, cache miss,
   yetki gereksinimi ve geçici ağ hatasını ayırt etmek için yetersiz.

## 3. Önerilen geliştirme sırası

### P0 — Kayıpsız zaman modeli ve dönüşüm

- Kaynak metin + satır + kelime/hece modeli: `start_ms`, `end_ms`, `text`,
  şarkıcı/rol, arka vokal, çeviri, okunuş, kaynak ve kimlik kanıtı.
- Zaman birimleri sağlayıcı adaptöründe açıkça tanımlanmalı; sayının büyüklüğüne
  bakarak saniye/milisaniye tahmini yapılmamalı.
- Orijinal TTML korunmalı; derived TTML ile native TTML ayrı etiketlenmeli.
- Çıktılar ayrı olmalı: orijinal TTML, enhanced LRC, uyumlu satır LRC, düz metin.
  Kullanıcının mevcut temizleme tercihi sessizce değiştirilmemeli.
- XML parser'ını koruyarak namespace, nested span, tail boşluğu, `begin/end/dur`,
  arka vokal ve farklı timestamp biçimlerini testlerle geliştirmek gerekir.
- Kelime bitişi bilinmiyorsa tahmin olduğu belirtilmeli; üretilen ara zamanlar
  gerçek Apple hece zamanlaması diye etiketlenmemeli.

Kabul: sentetik round-trip fixture'larında kelime metni, boşluklar ve bilinen
başlangıç/bitişler korunur; bozuk zamanlar kontrollü şekilde reddedilir.

### P1 — LyricsPlus sağlayıcısı

İki entegrasyon yolu var:

1. Hızlı pilot: `/v1/ttml/get` yanıtındaki `ttml` alanını mevcut akışa bağlamak.
   Bu endpoint **dönüştürülmüş** TTML döndürebilir; özgün Apple TTML garantisi yok.
2. Kalıcı çözüm: `/v2/lyrics/get` ve KPOE V2 adaptörü; zamanlar, okunuş,
   şarkıcılar ve gerçek kaynak metadata'sı korunur. Gerekiyorsa ve sunucu
   destekliyorsa `/v1/raw/get` ile özgün payload seçeneği ayrı sunulur.

Güncel backend belgesi LiMusic'ten daha fazla imkan sunuyor:
`isrc`, `platformId`, `source` ve ondalıklı saniye `duration`.
Picard'ın ISRC'sini kullanmak; yanıtın `metadata` ve
`processingTime.selectedSongMetadata` alanlarını doğrulamak gerekir.
`source=apple` bir kaynak tercihi olarak gönderilebilir, ama sonucun gerçekten
Apple olduğunu yine yanıt kanıtlamalıdır.

Katalog taraması gerekiyorsa `/v1/songlist/search?q=...` platform ID, ISRC ve
`durationMs` döndürüyor. Her şarkıda gereksiz katalog çağrısı yapılmamalı.

Kabul: Word/Line/None, metadata çakışması, boş sonuç, hatalı süre, 429/503 ve
desteklenmeyen şema testleri; provider kapalıyken hiç istek yapılmaması.

### P1 — Kimlik ve zamanlama kalitesini ayır

Önce kayıt kimliği ve sürümünü doğrula; sonra **aynı kayıt için güvenilir adaylar
arasında** kelime/hece > satır > düz metin tercihi uygula. Yanlış parçanın word
lyrics'i, doğru parçanın line lyrics'ini geçmemeli.

- Çoklu ISRC varsa ayrı değerlendir; albüm baskısı değişikliğini kayıt değişikliği
  ile karıştırma.
- Live/remix/radio edit/instrumental gibi sürüm bilgilerini körlemesine silme.
  LiMusic'in parantezlerin tamamını atan başlık temizliği Picard'a aynen uygun değil.
- Exact ISRC güçlü kanıt olsa da büyük süre çelişkisi ve bozuk metadata ayrıca
  uyarı/manuel onay gerektirebilir; mevcut bypass sorgulanmalı.
- Bilinmeyen alanları başarı kanıtı sayma. Yakın adaylar arasında fark yetersizse
  otomatik yazmak yerine seçim iste.
- Kaynaklar arasında aynı sözlerin zaman çizelgelerini otomatik birleştirme;
  farklı kesimlerin sözleri aynı olsa da zamanları farklı olabilir.

Kabul: aynı adlı farklı sanatçı, canlı/stüdyo, remaster, edit, çoklu sanatçı,
farklı yazı sistemi ve yanlış ISRC vakalarından etiketli bir değerlendirme seti.
Ölçüler: yanlış otomatik yazma oranı, doğru bulma oranı, word-sync kapsaması,
gecikme ve istek sayısı. Ağırlıkların "en doğru" olduğu ancak böyle ölçülebilir.

### P2 — Boidu ve diğer fallback'ler

- Better Lyrics için resmi belgelenmiş adresi kullanan opsiyonel adaptör ekle.
  Tam başlık/sanatçı/albüm/süre gönder; gerekirse sınırlı albümsüz varyant dene.
- 401'i lyrics yok diye kaydetme. Üçüncü taraf uzantı anahtarı çıkarma/kopyalama
  üzerine tasarım kurma; yalnız yetkili kullanım veya kişiye ait servis anahtarı.
- LRC.red ve Boidu aynı içeriği sunuyorsa ikinci indirmeyi önle.
- LRCLIB, word lyrics bulunamadığında satır/düz metin için faydalı fallback.
  SimpMusic ise YouTube video ID gerektirdiği için sıradan Picard dosyalarında
  daha düşük öncelikli.

### P2 — Dayanıklılık ve kullanıcı kontrolü

- `hit / not_found / auth_required / rate_limited / transport_error` sonuçları.
- `Retry-After` ve sağlayıcı başına bekleme; 429'u başka aynalara yük bindirerek
  aşmaya çalışma. Mirror değişimi yalnız onaylı alternatifler ve uygun politika ile.
- Picard `api.web_service`, iptal ve GUI thread güvenliğiyle entegrasyon.
- Başarılı cache, gerçek 404 cache ve ağ hatası ayrı davranmalı. Uzun süreli
  cache kullanıcı veri dizininde, parser sürümü ve kaynak bilgisiyle tutulmalı.
- Kaynakları sırala/kapat; manuel seçimi kilitle; offset düzeltmesini kaydet.
- Önizlemede `native TTML / generated TTML`, `word / line / plain`, kayıt kimliği,
  süre farkı, kaynak ve reddetme gerekçesi göster.
- Harici sağlayıcıların başlık/sanatçı/albüm/süre/ISRC aldığını açıkla.
- TTML'nin standart lyrics tag'ine gömülmesi her oynatıcıda karaoke desteği
  sağlamaz; sidecar ve hedef oynatıcı uyumluluğunu ayrı test et.

## 4. Bu araştırmadan uygulananlar

- `lyrics_model.py`: ortak milisaniye zaman modeli, TTML/KPOE/LRC parser'ları,
  native TTML koruması, enhanced LRC ve generated TTML çıktıları.
- `lyrics_providers.py`: isteğe bağlı LyricsPlus KPOE v2, Better Lyrics/Boidu ve
  LRCLIB adaptörleri. Yeni sağlayıcılar ayarlardan varsayılan olarak kapalıdır.
- `lyrics_http.py`: Picard'ın macOS bundled Python ortamındaki CA problemi için
  doğrulamalı TLS context; `/etc/ssl/cert.pem` additive olarak yüklenir, TLS
  doğrulaması kapatılmaz. Retry-After, cooldown, response limit ve güvenli
  durum kodları korunur.
- `lyrics_matching.py`: kimlik eşleştirmesi ile word/line/plain kalite tercihi
  ayrıldı; metadata kanıtı olmayan sağlayıcı echo'ları otomatik kabul edilmez.
- `lyrics_cache.py`: sürümlü SQLite bundle cache ve kalıcı provider/offset seçimi.
- `__init__.py`: bu modüller Picard Plugin v3 akışına bağlandı; LRC.red/Unison
  eski akışları korunurken optional provider sonuçları ortak modele geçiriliyor.

Uygulama sonrası yerel doğrulama: 23 test başarılı, Python 3.11/3.14 syntax
kontrolleri başarılı, canlı LRC.red ISRC ve LRCLIB istekleri başarılı. Kurulu
Picard 3.0 (Python 3.14.7) eklentiyi yeni modüllerle import edip enable etti;
GUI içinden gerçek track search sonucu ayrıca kullanıcı tarafından denenmeli.

## 5. Doğrulama ve sınırlar

- LiMusic backend, frontend ve iki sağlayıcının upstream belgeleri/kodu incelendi.
- LiMusic README'sindeki "yalnız Boidu word timing sağlar" ifadesi güncel kodla
  uyuşmuyor: kod LyricsPlus word timing ve okunuş desteğini de içeriyor.
- `https://lyricsplus.binimum.org/` kök adresine yapılan tek kontrol 429 döndürdü.
  Bu, `/v2/lyrics/get` endpoint'inin çalışmadığını kanıtlamaz; fakat bu araştırmada
  canlı LyricsPlus lyrics yanıtı doğrulanamadı. Limit aşma veya mirror taraması yapılmadı.
- Boidu'nun canlı şarkı yanıtı test edilmedi; erişim davranışı upstream resmi
  belge ve LiMusic uygulamasına dayanıyor.
- Mevcut Python dönüştürücüsünün boşluk kaybı, ELRC kelime kaybı ve `1500ms`
  hatası sentetik girdilerle yeniden üretildi. Gerçek şarkı sözleri rapora alınmadı.
- LiMusic GPL-3.0, mevcut eklenti MIT. LiMusic kodunu doğrudan kopyalayıp MIT
  altında dağıtmak uygun değil; API sözleşmelerine dayalı bağımsız uygulama tercih
  edilmeli. LyricsPlus backend README'si Apache-2.0 belirtiyor; kod alınacaksa
  ilgili lisans/attribution yükümlülükleri ayrıca incelenmeli. Servisin erişilebilir
  olması, lyrics içeriği için yeniden dağıtım izni anlamına gelmez.

**Sonraki öneri:** kurulu Picard GUI'sinde önce mevcut bir dosyayla LRC.red
arama akışını doğrulamak, sonra LyricsPlus/Boidu/LRCLIB seçeneklerini tek tek
açıp gerçek kapsama, 429 davranışı ve word-sync doğruluğunu ölçmek. Boidu
cache-only desteği varsayılan olarak kapalı bırakılmıştır.

# Better Lyrics - MusicBrainz Picard 3.0+ & 2.x Plugin

[MusicBrainz Picard](https://picard.musicbrainz.org/) 3.0+ ve 2.x sürümleri için [Better Lyrics](https://github.com/better-lyrics/better-lyrics) ekosistemi tabanlı, akıllı eşleşme ve şarkı sözü (LRC / TTML / Düz metin) indirme eklentisidir.

Eski `picard-lrclib` eklentisi temel alınarak, **Picard 3.0 (PyQt6)** ve **Picard 2.x (PyQt5)** mimarisiyle tam uyumlu ve çoklu Better Lyrics kaynaklarını destekleyecek şekilde sıfırdan geliştirilmiştir.

---

## 🌟 Temel Özellikler

1. **Better Lyrics Hibrit Veri Kaynakları**:
   - **LRC.red API (`https://lrc.red/api/v1`)**: Better Lyrics ekosisteminin arkasındaki ana veritabanı (w4v tarafından sağlanan kelime ve satır senkronlu sözler).
   - **Unison API (`https://unison.betterlyrics.org`)**: Better Lyrics topluluk odaklı, crowdsourced senkronize söz veritabanı.
   - **Better Lyrics API (`https://api.betterlyrics.org`)**: İsteğe bağlı API anahtarı desteği.
2. **Akıllı Eşleşme (Smart Match Scoring)**:
   - Şarkı Adı ve Sanatçı benzerlik skorlaması (`difflib.SequenceMatcher`).
   - Albüm adı eşleştirmesi.
   - Süre toleransı (varsayılan ±4 saniye) ile doğru stüdyo/albüm versiyonunu bulma.
   - **ISRC Eşleşmesi**: MusicBrainz'den gelen ISRC kodu varsa doğrudan kesin eşleşme.
3. **TTML -> LRC Otomatik Dönüştürücü**:
   - Better Lyrics ve Apple Music formatındaki zengin TTML (`Timed Text Markup Language`) sözlerini otomatik olarak standart ve temiz LRC formatına çevirir.
4. **Kelime Senkronu Temizleme (Word-Sync Cleaning)**:
   - `<mm:ss.xx>` gibi kelime düzeyindeki zengin zaman damgalarını standart `[mm:ss.xx]` satır zaman damgalarına dönüştürür.
   - Böylece Plex, Jellyfin, Apple Music, Foobar2000, VLC, Android müzik çalarlar gibi tüm oynatıcılarla kusursuz uyumluluk sağlar.
5. **Gelişmiş Etiket ve Dosya Çıktısı**:
   - ID3 / Vorbis / MP4 etiketlerine gömme (`lyrics` ve `syncedlyrics`).
   - Müzik dosyasının yanına harici `.lrc` (veya düz sözler için isteğe bağlı `.txt`) dosyası kaydetme.
6. **İnteraktif Manuel Arama Dialogu**:
   - Şarkıya sağ tıklayıp arama yapabilme, arama sorgusunu düzenleme, sonuçları (Başlık, Sanatçı, Albüm, Süre, Senkron Tipi: Word/Line/Plain, Kaynak) tablodan görüp seçebilme ve seçilen sözü önizleyebilme.
7. **Otomatik İndirme Tetikleyicileri**:
   - Şarkı Picard'a yüklendiğinde otomatik indirme (`get_on_load`).
   - Şarkı kaydedildiğinde otomatik indirme (`get_on_save`).
8. **Yetim .lrc Dosyalarını Temizleme Aracı**:
   - Müzik kütüphanesini tarayarak müzik dosyası silinmiş yetim kalmış `.lrc` dosyalarını tespit edip temizleme.
9. **Tam Picard 3.0 & 2.x Uyumluluğu**:
   - PyQt6 ve PyQt5 arasında otomatik dinamik geçiş katmanı.

---

## 🚀 Kurulum

### macOS
1. `better_lyrics.py` dosyasını Picard eklentileri dizinine kopyalayın:
   ```bash
   cp better_lyrics.py ~/Library/Preferences/MusicBrainz/Picard/plugins/
   ```
2. MusicBrainz Picard'ı açın (veya yeniden başlatın).
3. **Seçenekler (Preferences) -> Eklentiler (Plugins)** bölümüne gidin.
4. **Better Lyrics** eklentisini etkinleştirin.

### Windows
1. `better_lyrics.py` dosyasını aşağıdaki dizine kopyalayın:
   ```
   %APPDATA%\MusicBrainz\Picard\plugins\
   ```
   *(Örn: `C:\Users\<Kullanıcı>\AppData\Roaming\MusicBrainz\Picard\plugins\`)*
2. Picard'ı açıp **Seçenekler -> Eklentiler** bölümünden eklentiyi aktifleştirin.

### Linux
1. `better_lyrics.py` dosyasını aşağıdaki dizine kopyalayın:
   ```bash
   cp better_lyrics.py ~/.config/MusicBrainz/Picard/plugins/
   ```
2. Picard'ı açıp eklentiyi aktifleştirin.

---

## ⚙️ Yapılandırma ve Seçenekler

Picard içerisinde **Seçenekler -> Eklentiler -> Better Lyrics** sayfasına giderek ayarları kişiselleştirebilirsiniz:

| Ayar | Varsayılan | Açıklama |
|---|---|---|
| **Search for lyrics when loading tracks** | `Açık (True)` | Parça Picard'a yüklendiğinde sözleri otomatik indirir. |
| **Search for lyrics when saving files** | `Kapalı (False)` | Parça diske kaydedilirken sözleri indirir. |
| **Auto overwrite existing lyrics** | `Kapalı (False)` | Mevcut sözlerin üzerine soru sormadan yazar. |
| **Convert word timestamps to line-synced LRC** | `Açık (True)` | Kelime zaman damgalarını (`<00:12.34>`) temizleyip standart `[00:12.34]` yapar. |
| **Prefer synchronized lyrics** | `Açık (True)` | Düz metin yerine senkronize sözleri önceliklendirir. |
| **Ignore instrumental tracks** | `Açık (True)` | Enstrümantal parçaları atlar. |
| **Embed lyrics into 'lyrics' tag** | `Açık (True)` | Sözleri parça meta verisine gömer. |
| **Embed synced lyrics into 'syncedlyrics' tag** | `Kapalı (False)` | Destekleyen oynatıcılar için `syncedlyrics` etiketine yazar. |
| **Save external .lrc file alongside audio files** | `Açık (True)` | Müzik dosyasının yanına `.lrc` sidecar dosyası kaydeder. |
| **Save plain lyrics as .txt** | `Kapalı (False)` | Senkrondan yoksun düz sözleri `.txt` olarak kaydeder. |
| **Primary Source** | `All Sources` | `All Sources (Smart Hybrid)`, `LRC.red` veya `Unison` seçimi. |
| **Duration tolerance (seconds)** | `4` | Otomatik eşleşme için izin verilen azami süre farkı (saniye). |
| **Better Lyrics API Key** | `Boş` | Varsa `api.betterlyrics.org` özel API anahtarınız. |

---

## 🎯 Kullanım

### 1. Otomatik Kullanım
Picard üzerinden bir albümü veya şarkıları eşleştirdiğinizde, Better Lyrics eklentisi arka planda şarkı adı, sanatçı ve süre bilgilerini kullanarak en kaliteli senkronize sözü indirir ve ayarlarınıza göre etiketlere ve/veya `.lrc` dosyasına yazar.

### 2. Sağ Tık ile Manuel veya Otomatik İndirme
Picard arayüzünde herhangi bir şarkıya veya albüme sağ tıklayarak:
- **Plugins -> Get lyrics automatically with Better Lyrics**: Seçili parça(lar) için otomatik akıllı eşleşme ile sözleri indirir.
- **Plugins -> Search lyrics manually with Better Lyrics**: İnteraktif arama penceresini açar. İstediğiniz gibi arama yapabilir, gelen sonuçların senkron tipini görebilir, **Preview Selected** ile sözleri önizleyebilir ve istediğiniz versiyonu seçebilirsiniz.

---

## 📜 Lisans
Bu proje [MIT Lisansı](LICENSE) ile lisanslanmıştır.

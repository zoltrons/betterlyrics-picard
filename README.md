# Better Lyrics (TTML & LRC) - MusicBrainz Picard 3.0+ & 2.x Plugin

[MusicBrainz Picard](https://picard.musicbrainz.org/) 3.0+ ve 2.x sürümleri için [Better Lyrics](https://github.com/better-lyrics/better-lyrics) ekosistemi tabanlı, **TTML (Timed Text Markup Language)** ve **LRC** şarkı sözü indirme ve etiketleme eklentisidir.

Eski `picard-lrclib` eklentisi temel alınarak; **Picard 3.0 (PyQt6)** ve **Picard 2.x (PyQt5)** mimarisiyle tam uyumlu, Apple Music tarzı **zengin TTML XML** sözlerini doğrudan ses dosyası etiketlerine (`lyrics`, `ttml`) yazacak ve `.ttml` dosyası kaydedecek şekilde sıfırdan geliştirilmiştir.

---

## 🌟 TTML ve Temel Özellikler

1. **Birinci Sınıf TTML (Apple Music XML) Desteği**:
   - **Doğrudan Etiketlere Gömme**: Orijinal, kayıpsız Better Lyrics / Apple Music TTML XML metnini doğrudan ses dosyası meta verilerine yazar:
     - M4A / ALAC / MP4 için `©lyr` atomuna.
     - FLAC / OGG için `LYRICS` etiketine.
     - MP3 için `USLT` çerçevesine.
     - Özel `ttml` etiketine (`file.metadata["ttml"]`).
   - **Harici `.ttml` Dosyası Kaydetme**: Parçanın yanına doğrudan `Şarkı.ttml` sidecar dosyası oluşturur.
   - **Çift Format (Dual Export)**: İsteğe bağlı olarak hem `.ttml` hem de geleneksel oynatıcılar için `.lrc` dosyasını aynı anda oluşturabilir.
   - **LRC -> TTML Dönüştürücü**: Kaynakta sadece LRC mevcutsa, bunu otomatik olarak geçerli ve standart bir TTML XML belgesine dönüştürür.
2. **Better Lyrics Hibrit Veri Kaynakları**:
   - **LRC.red API (`https://lrc.red/api/v1`)**: Better Lyrics ekosisteminin birincil veri kaynağı (w4v tarafından sunulan zengin TTML ve kelime/satır senkronlu katalog).
   - **Unison API (`https://unison.betterlyrics.org`)**: Better Lyrics'in resmi açık topluluk ve crowdsourced TTML söz veritabanı.
   - **Better Lyrics API (`https://api.betterlyrics.org`)**: İsteğe bağlı özel API anahtarı desteği.
3. **Akıllı Eşleşme (Smart Match Scoring)**:
   - Şarkı Adı ve Sanatçı benzerlik skorlaması (`difflib.SequenceMatcher`).
   - Albüm adı ve süre toleransı (varsayılan ±4 sn) ile stüdyo/albüm versiyonu eşleşmesi.
   - **ISRC Eşleşmesi**: MusicBrainz ISRC koduyla doğrudan birebir kesin eşleşme.
4. **İnteraktif Manuel Arama & TTML Önizleme**:
   - Şarkıya sağ tıklayıp arama yapabilme.
   - Sonuç listesinde formatı (`TTML (Word)`, `TTML (Line)`, `Plain`) görebilme.
   - **"Preview Selected"** penceresinde **TTML (XML)** ve **LRC (Synced)** sekmeleri arasında geçiş yaparak içeriği inceleyebilme.
5. **Otomasyon & Temizlik**:
   - Parça Picard'a yüklendiğinde otomatik indirme (`get_on_load`).
   - Parça kaydedildiğinde otomatik indirme (`get_on_save`).
   - Müzik kütüphanesindeki yetim `.ttml` ve `.lrc` dosyalarını tarayıp silen temizleme aracı.
6. **Picard 3.0 & 2.x (PyQt6/PyQt5) Uyumluluğu**:
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

Picard içerisinde **Seçenekler -> Eklentiler -> Better Lyrics** sayfasından ayarları yönetebilirsiniz:

| Ayar | Varsayılan | Açıklama |
|---|---|---|
| **Primary Lyrics Format** | `TTML` | `TTML (Apple Music XML)`, `LRC` veya `Both TTML and LRC (Dual Export)` |
| **Embed into 'lyrics' tag** | `Açık (True)` | Seçilen formatı (TTML/LRC) parça etiketine gömer (M4A'da `©lyr`, MP3'te `USLT`, FLAC'ta `LYRICS`). |
| **Embed into 'ttml' tag** | `Açık (True)` | Ham TTML XML verisini doğrudan `ttml` etiketine yazar. |
| **Save external .ttml sidecar file** | `Açık (True)` | Parçanın yanına `Şarkı.ttml` dosyası kaydeder. |
| **Save external .lrc sidecar file** | `Kapalı (False)` | Parçanın yanına `Şarkı.lrc` dosyası kaydeder. |
| **Clean word timestamps in LRC** | `Açık (True)` | LRC çıktısında kelime zaman damgalarını temizleyip satır formatına indirger. |
| **Search when loading tracks** | `Açık (True)` | Parça Picard'a yüklendiğinde otomatik indirir. |
| **Search when saving files** | `Kapalı (False)` | Parça kaydedilirken otomatik indirir. |
| **Auto overwrite existing lyrics** | `Kapalı (False)` | Mevcut sözlerin üzerine onay sormadan yazar. |
| **Primary Source** | `All Sources` | `All Sources (Smart Hybrid)`, `LRC.red` veya `Unison` seçimi. |
| **Duration tolerance (seconds)** | `4` | Azami süre farkı toleransı (saniye). |

---

## 🎯 Kullanım

1. **Otomatik İndirme**: Picard'a eklediğiniz parçalar için Better Lyrics arka planda TTML sözleri bulup parça etiketine ve/veya `.ttml` dosyasına kaydeder.
2. **Manuel Arama**: Şarkıya sağ tıklayıp **Plugins -> Search lyrics (TTML / LRC) manually with Better Lyrics** seçeneğine tıklayın. Çıkan pencerede TTML ve LRC önizlemesini görüp istediğiniz sonucu şarkıya uygulayabilirsiniz.

---

## 📜 Lisans
Bu proje [MIT Lisansı](LICENSE) ile lisanslanmıştır.

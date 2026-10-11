# Picard Plugin v3 Uyumluluk Planı

Kaynak: [Picard Plugin v3 README](https://raw.githubusercontent.com/metabrainz/picard/refs/heads/master/docs/PLUGINSV3/README.md)
ve README'nin yönlendirdiği `MANIFEST.md`, `MIGRATION.md` ve `API.md` belgeleri.

## Tarama sonucu

Proje Plugin v3'ün temel yaşam döngüsüne uyuyor:

- `MANIFEST.toml` mevcut ve UUID v4, `name`, `description` ve `api` alanları tanımlı.
- `enable(api)` ve kaynak temizliği yapan `disable()` mevcut.
- Picard kayıtları `enable()` içinde yapılıyor.
- Plugin v3 API sınıfları ve PyQt6 kullanılıyor.
- Eski v2 `register()` akışı ve doğrudan `picard.tagger_instance()` erişimi kaldırıldı.

## Bu değişiklikte tamamlananlar

1. `MANIFEST.toml` içine `min_python_version = "3.10"` ve `report_bugs_to` eklendi.
2. README'deki eski `plugins/` klasörüne elle kopyalama yönergesi Plugin v3 CLI kurulumuyla değiştirildi.
3. Duration varsayılanı README ile kodda `5` saniye olarak hizalandı.
4. Eşleştirme modeli toplamı 1.0 olan açık ağırlıklara taşındı:
   - ISRC: `0.35`
   - başlık: `0.30`
   - sanatçı: `0.15`
   - albüm: `0.05`
   - süre: `0.15`
5. Eksik metadata artık sahte puan kazandırmıyor; senkronizasyon kimlik puanına eklenmeyip yalnızca eşit puanlarda tercih ediliyor.
6. ISRC çakışması ve duration tolerance dışındaki adaylar otomatik akışta eleniyor.
7. İlk adayın lyrics endpoint'i boşsa veya hata verirse sonraki uyumlu aday deneniyor.

## Kalan işler / önerilen sıra

### P1 — Picard ağ katmanına geçiş

`__init__.py` hâlâ arka plan kuyruğu içinde `urllib.request.urlopen()` kullanıyor.
Plugin v3 API dokümantasyonu, Picard ayarları, iptal mekanizması ve album task
yaşam döngüsü için `api.web_service` kullanımını öneriyor. Ağ çağrılarını bu
katmana taşıyıp plugin kapanışında bekleyen istekleri iptal etmek gerekir.

### P1 — API key davranışını tamamla

`betterlyrics_api_key` ayarı ve `BETTER_LYRICS_API_URL` sabiti mevcut, ancak
Better Lyrics API endpoint'i sorgu akışında kullanılmıyor. Bir sonraki adım:

- API key'i güvenli header/query biçiminde gerçek endpoint çağrısına bağlamak ve
  provider'ı skor akışına eklemek; veya
- desteklenmiyorsa ayar, sabit ve README iddiasını kaldırmak.

### P2 — Çeviri desteği

Kullanıcıya görünen action, options ve hata metinleri `t_()` / `api.tr()` ile
işaretlenebilir. Bu, Plugin v3 yükleme uyumluluğu için zorunlu değil; registry ve
çoklu dil kalitesi için önerilir.

### P2 — Gerçek Picard doğrulaması ve CI

Yerel ortamda `picard-cli`, PyQt6 ve pytest bulunmadığı için gerçek Picard
kurulum testi çalıştırılamadı. CI'a şu kontroller eklenmeli:

```bash
python3.11 -m py_compile __init__.py
picard-cli plugins validate .
python3.11 -m unittest discover -s tests -v
```

Picard içinde ayrıca enable/disable, options sayfası, action'lar, duration/ISRC
çatışmaları ve boş ilk adaydan sonraki fallback akışı manuel doğrulanmalı.

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

## Kararlılık sertleştirmesi (2026-10-19)

Plugin v3 API belgelerindeki yaşam döngüsü kuralları (disable sırasında
`api.register_*` kayıtlarının Picard tarafından otomatik kaldırılması, thread ve
bağlantıların plugin tarafından kapatılması, hataların tek tek yakalanması)
esas alınarak aşağıdakiler tamamlandı:

1. **Hızlı ve güvenli disable**: Kuyruk artık disable sırasında boşaltılmıyor
   (drain edilmiyor); bekleyen işler atılıyor, çalışan istek en yakın iptal
   noktasında bırakılıyor. `FetchQueue.stop()` en fazla 2 sn bekliyor; eski
   davranış her worker için 5 sn'ye kadar GUI thread'ini bloke edebiliyordu.
2. **Epoch koruması**: Her `start()` yeni bir epoch açar. Önceki oturumdan kalan
   bir worker ne iş çalıştırabilir ne de sonucu yeni oturuma uygulayabilir.
   Yeniden enable sonrası eski sonuçların metadata'ya yazılması engellendi.
3. **Disable sonrası diriliş engeli**: `_DISABLED` bayrağı ile otomatik
   processor/action yolları ve `_ensure_queue_ready()` kısa devre yapar; kapanış
   sırasında gelen geç bir callback kuyruğu yeniden başlatamaz.
4. **İptal edilebilir HTTP**: `_SHUTDOWN_EVENT`, rate limiter beklemesini ve retry
   backoff'unu keser. Sağlayıcı istemcisi `close()` ile iptal edilir. Disable,
   süresi dolmayı bekleyen bir retry yüzünden ağ trafiği üretmeye devam etmez.
5. **Sağlayıcı izolasyonu**: LRC.red, Unison, LyricsPlus, Boidu veya LRCLIB'den
   biri hata verirse yalnız o sağlayıcı boş sonuç sayılır; kalan fallback zinciri
   çalışmaya devam eder. Bozuk Unison yanıtları (liste olmayan `data`, sözlük
   olmayan satır, hash'lenemeyen id) çökme yerine atlanır.
6. **Worker thread'de config erişimi kaldırıldı**: Otomatik akış, sağlayıcı
   anahtarları ve toggle'ları GUI thread'inde bir sözlüğe kopyalar; worker
   `api.plugin_config` okumaz.
7. **GUI thread'inde sınırlı iş**: `MainThreadDispatcher.poll()` timer başına en
   fazla 25 sonuç uygular; yüzlerce eşzamanlı sonuç pencereyi dondurmaz.
8. **Hata izolasyonu**: Bir dosyanın metadata'sı yazılamazsa diğer dosyalar
   işlenmeye devam eder; bozuk bir TTML cue'su belgenin tamamını düşürmez;
   sayısal ayarlar yüklenirken aralığa kırpılır.
9. **Yaşam döngüsü sağlamlaştırması**: `enable()` iki kez çağrılırsa ikinci çağrı
   yok sayılır; SSL context bir kez üretilip yeniden kullanılır; hiç worker
   başlatılamazsa kuyruk kapalı kalır. Kapanışta önce HTTP istemcisi, sonra
   kuyruk, en son kalıcı cache kapatılır.
10. **Kurulum/güncelleme sonrası ilk çalıştırma düzeltmesi**: Picard, plugin
    kurulumunu ve güncellemesini `QThreadPool` worker'ında çalıştırır ve
    `enable()`'ı orada çağırır (`AsyncPluginManager` → `run_async` →
    `run_task`). Eski kod `MainThreadDispatcher` içinde `QTimer(self)`'ı bu
    worker thread'inde oluşturuyordu; timer'ın affinite thread'inde event loop
    olmadığı için hiç tetiklenmiyor, dolayısıyla otomatik indirilen lyrics
    uygulanmıyordu. Bu yüzden plugin, kullanıcı aç/kapa yapana kadar "çalışmıyor"
    görünüyordu. Dispatcher artık `moveToThread(QApplication.thread())` ile GUI
    thread'ine taşınıyor ve timer `QMetaObject.invokeMethod(..., QueuedConnection)`
    ile GUI thread'inde başlatılıyor. `on_file_saved` de Picard'ın kaydetme
    worker'ında çalıştığı için aynı koruma o yolu da kapsar. `stop()` worker'dan
    çağrılsa bile QTimer'a dokunmaz; temizliği GUI tick'i yapar.

Regresyon kapsamı: `tests/test_stability_lifecycle.py` (kuyruk epoch'ları, disable
sonrası submit, iptal edilen pacing, sağlayıcı izolasyonu, config snapshot, sınırlı
GUI drain, bozuk dosya izolasyonu, GUI thread'ine timer marshalling). Toplam suite:
96 test. Gerçek Qt doğrulaması: `tests/qt_dispatcher_thread_smoke.py` (worker
thread'inde enable/disable + GUI thread'inde sonuç uygulama) ve
`tests/qt_menu_smoke.py`, PyQt 6.11.0 / Qt 6.11.2 ile geçti.

## Kalan işler / önerilen sıra

### P1 — Picard ağ katmanına geçiş (opsiyonel)

`__init__.py` hâlâ arka plan kuyruğu içinde `urllib.request.urlopen()` kullanıyor.
Bu sürümde kapanış iptali kuyruk epoch'ları ve `_SHUTDOWN_EVENT` ile çözüldü;
Plugin v3 belgelerinin önerdiği `api.web_service` + `add_album_task()` geçişi,
isteklerin albüm kaldırıldığında Picard tarafından iptal edilmesini de sağlar.
Geçiş yapılırsa callback tabanlı API nedeniyle kuyruk ve iptal katmanının yeniden
tasarlanması gerekir; mevcut yaklaşım GUI thread'ini bloke etmez ve disable
sırasında ağ işini durdurur.

### P1 — API key davranışı (doğrulandı)

`betterlyrics_api_key` ayarı `lyrics_providers._boidu()` üzerinden gerçekten
kullanılıyor: anahtar varsa `https://api.betterlyrics.org` ve `X-API-Key`
başlığı, yoksa anahtarsız `https://lyrics-api.boidu.dev` denenir. Anahtar loglara
yazılmaz. Bu maddedeki eski "bağlı değil" tespiti güncelliğini yitirmiştir.

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

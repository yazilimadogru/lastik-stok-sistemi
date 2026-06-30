from flask import Flask, render_template, request, jsonify, redirect, url_for
from pathlib import Path
from datetime import datetime
import json
import shutil
import threading
import time
import re
import uuid
import os
import sqlite3
import zipfile

app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
BACKUP_DIR = DATA_DIR / "yedekler"
LOG_DIR = BASE_DIR / "logs"
STOK_FILE = DATA_DIR / "stok.json"
SATIS_FILE = DATA_DIR / "cikis_fisleri.json"
HAREKET_FILE = DATA_DIR / "stok_hareketleri.json"
DB_FILE = DATA_DIR / "lastik_stok.db"
PRICE_IMPORT_DIR = DATA_DIR / "fiyat_listesi_import"
LAST_BACKUP_MARKER = DATA_DIR / ".last_daily_backup"
BACKUP_CONFIG_FILE = DATA_DIR / "yedek_ayarlari.json"

DATA_DIR.mkdir(exist_ok=True)
BACKUP_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)
PRICE_IMPORT_DIR.mkdir(exist_ok=True)

file_lock = threading.Lock()


# ==========================================================================
# WEB HAZIRLIK: SQLite veri katmanı
# ==========================================================================
# Not: Uygulamadaki mevcut JSON dosyaları aynen korunur.
# Web ortamında daha güvenli çalışması için ana veri SQLite içine de yazılır.
# Böylece aynı anda birden fazla işlem olduğunda JSON bozulma riski azalır.
USE_SQLITE = os.environ.get("USE_SQLITE", "1") != "0"


def db_baglanti():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE IF NOT EXISTS kv_store (anahtar TEXT PRIMARY KEY, veri TEXT NOT NULL)")
    return conn


def db_getir(anahtar, varsayilan=None):
    varsayilan = [] if varsayilan is None else varsayilan
    if not USE_SQLITE:
        return varsayilan
    with db_baglanti() as conn:
        row = conn.execute("SELECT veri FROM kv_store WHERE anahtar=?", (anahtar,)).fetchone()
    if not row:
        return varsayilan
    try:
        return json.loads(row["veri"])
    except Exception:
        return varsayilan


def db_yaz(anahtar, veri):
    if not USE_SQLITE:
        return
    with db_baglanti() as conn:
        conn.execute(
            "INSERT INTO kv_store (anahtar, veri) VALUES (?, ?) "
            "ON CONFLICT(anahtar) DO UPDATE SET veri=excluded.veri",
            (anahtar, json.dumps(veri, ensure_ascii=False, indent=2))
        )


def json_guvenli_oku(dosya, bozuk_on_eki):
    if not dosya.exists():
        dosya.write_text("[]", encoding="utf-8")
    try:
        return json.loads(dosya.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        bozuk_ad = DATA_DIR / f"{bozuk_on_eki}_bozuk_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.json"
        shutil.copy2(dosya, bozuk_ad)
        dosya.write_text("[]", encoding="utf-8")
        log_yaz(f"{dosya.name} bozuk çıktı, kopyası alındı: {bozuk_ad.name}")
        return []


def json_guvenli_yaz(dosya, veri):
    tmp = dosya.with_suffix(".tmp")
    tmp.write_text(json.dumps(veri, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(dosya)


def sqlite_ilk_aktarim():
    """Mevcut JSON verilerini ilk çalıştırmada SQLite içine taşır."""
    if not USE_SQLITE:
        return
    try:
        with db_baglanti() as conn:
            mevcut = conn.execute("SELECT COUNT(*) AS sayi FROM kv_store").fetchone()["sayi"]
        if mevcut:
            return
        if STOK_FILE.exists():
            db_yaz("stok", json_guvenli_oku(STOK_FILE, "stok"))
        if SATIS_FILE.exists():
            db_yaz("cikis_fisleri", json_guvenli_oku(SATIS_FILE, "cikis_fisleri"))
        if HAREKET_FILE.exists():
            db_yaz("stok_hareketleri", json_guvenli_oku(HAREKET_FILE, "stok_hareketleri"))
        log_yaz("SQLite ilk veri aktarımı tamamlandı.")
    except Exception as e:
        log_yaz(f"SQLite ilk aktarım hatası: {e}")




# ==========================================================================
# YEDEK AYARLARI: USB / Google Drive klasörlerine kopyalama
# ==========================================================================
def yedek_ayarlari_oku():
    varsayilan = {
        "harici_klasorler": [],
        "tam_yedek_zip": True
    }
    if not BACKUP_CONFIG_FILE.exists():
        BACKUP_CONFIG_FILE.write_text(json.dumps(varsayilan, ensure_ascii=False, indent=2), encoding="utf-8")
        return varsayilan
    try:
        data = json.loads(BACKUP_CONFIG_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return varsayilan
        data.setdefault("harici_klasorler", [])
        data.setdefault("tam_yedek_zip", True)
        data["harici_klasorler"] = [str(x).strip() for x in data.get("harici_klasorler", []) if str(x).strip()]
        return data
    except Exception:
        return varsayilan


def yedek_ayarlari_yaz(data):
    klasorler = data.get("harici_klasorler", []) if isinstance(data, dict) else []
    temiz = []
    for yol in klasorler:
        yol = str(yol or "").strip().strip('"')
        if yol and yol not in temiz:
            temiz.append(yol)
    kayit = {
        "harici_klasorler": temiz,
        "tam_yedek_zip": bool(data.get("tam_yedek_zip", True)) if isinstance(data, dict) else True
    }
    BACKUP_CONFIG_FILE.write_text(json.dumps(kayit, ensure_ascii=False, indent=2), encoding="utf-8")
    return kayit


def kritik_dosyalar():
    dosyalar = []
    for dosya in [STOK_FILE, SATIS_FILE, HAREKET_FILE, DB_FILE, LOG_DIR / "islem_kaydi.txt", BACKUP_CONFIG_FILE]:
        if dosya.exists() and dosya.is_file():
            dosyalar.append(dosya)
    return dosyalar


def tam_yedek_zip_olustur(zaman, neden):
    BACKUP_DIR.mkdir(exist_ok=True)
    zip_ad = f"tam_yedek_{zaman}_{neden}.zip"
    zip_yol = BACKUP_DIR / zip_ad
    with zipfile.ZipFile(zip_yol, "w", zipfile.ZIP_DEFLATED) as zf:
        for dosya in kritik_dosyalar():
            try:
                zf.write(dosya, dosya.relative_to(BASE_DIR))
            except Exception:
                pass
    return zip_yol


def harici_yedeklere_kopyala(dosyalar):
    sonuc = []
    ayarlar = yedek_ayarlari_oku()
    for hedef_klasor in ayarlar.get("harici_klasorler", []):
        try:
            hedef = Path(hedef_klasor).expanduser()
            hedef.mkdir(parents=True, exist_ok=True)
            for dosya in dosyalar:
                if dosya and Path(dosya).exists():
                    shutil.copy2(dosya, hedef / Path(dosya).name)
            sonuc.append({"klasor": str(hedef), "ok": True})
            log_yaz(f"Harici yedek kopyalandı: {hedef}")
        except Exception as e:
            sonuc.append({"klasor": hedef_klasor, "ok": False, "hata": str(e)})
            log_yaz(f"Harici yedek kopyalama hatası ({hedef_klasor}): {e}")
    return sonuc

def log_yaz(mesaj):
    zaman = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_DIR / "islem_kaydi.txt", "a", encoding="utf-8") as f:
        f.write(f"[{zaman}] {mesaj}\n")


def stok_oku():
    if USE_SQLITE:
        veri = db_getir("stok", None)
        if veri is not None:
            return veri
    veri = json_guvenli_oku(STOK_FILE, "stok")
    if USE_SQLITE:
        db_yaz("stok", veri)
    return veri


def stok_yaz(veri):
    with file_lock:
        if USE_SQLITE:
            db_yaz("stok", veri)
        json_guvenli_yaz(STOK_FILE, veri)


def satis_oku():
    if USE_SQLITE:
        veri = db_getir("cikis_fisleri", None)
        if veri is not None:
            return veri
    veri = json_guvenli_oku(SATIS_FILE, "cikis_fisleri")
    if USE_SQLITE:
        db_yaz("cikis_fisleri", veri)
    return veri


def satis_yaz(veri):
    with file_lock:
        if USE_SQLITE:
            db_yaz("cikis_fisleri", veri)
        json_guvenli_yaz(SATIS_FILE, veri)


def hareket_oku():
    if USE_SQLITE:
        veri = db_getir("stok_hareketleri", None)
        if veri is not None:
            return veri
    veri = json_guvenli_oku(HAREKET_FILE, "stok_hareketleri")
    if USE_SQLITE:
        db_yaz("stok_hareketleri", veri)
    return veri


def hareket_yaz(veri):
    with file_lock:
        if USE_SQLITE:
            db_yaz("stok_hareketleri", veri)
        json_guvenli_yaz(HAREKET_FILE, veri)


def hareket_ekle(stok_id, tip, adet, once=None, sonra=None, musteri='', aciklama='', fis_id=None, fis_no='', stok_kayit=None):
    if stok_id in (None, '', 'manual'):
        return
    try:
        stok_id_int = int(stok_id)
    except Exception:
        return

    hareketler = hareket_oku()
    stok_kayit = stok_kayit or next((x for x in stok_oku() if int(x.get('id', 0)) == stok_id_int), {})
    hareketler.append({
        'id': max([int(x.get('id', 0)) for x in hareketler], default=0) + 1,
        'stok_id': stok_id_int,
        'tarih': datetime.now().strftime('%d.%m.%Y %H:%M'),
        'tip': str(tip or '').strip(),
        'adet': int(adet or 0),
        'once': once,
        'sonra': sonra,
        'musteri': str(musteri or '').strip(),
        'aciklama': str(aciklama or '').strip(),
        'fis_id': fis_id,
        'fis_no': str(fis_no or '').strip(),
        'marka': stok_kayit.get('marka', ''),
        'kod': stok_kayit.get('kod', ''),
        'desen': stok_kayit.get('desen', ''),
        'ebat': ebat_formatla(stok_kayit.get('ebat', '')),
    })
    hareket_yaz(hareketler)


def hareket_tarih_parse(value):
    raw = str(value or '').strip()
    if not raw:
        return None
    for fmt in ('%d.%m.%Y %H:%M', '%d.%m.%Y', '%Y-%m-%d'):
        try:
            return datetime.strptime(raw, fmt)
        except Exception:
            pass
    digits = re.sub(r'\D', '', raw)
    if len(digits) == 8:
        try:
            return datetime.strptime(f'{digits[:2]}.{digits[2:4]}.{digits[4:8]}', '%d.%m.%Y')
        except Exception:
            return None
    return None


def stok_hareketleri_getir(stok_id, baslangic='', bitis=''):
    try:
        stok_id_int = int(stok_id)
    except Exception:
        return []

    bas_dt = hareket_tarih_parse(baslangic)
    bit_dt = hareket_tarih_parse(bitis)
    if bit_dt and len(str(bitis)) <= 10:
        bit_dt = bit_dt.replace(hour=23, minute=59, second=59)

    sonuc = [x for x in hareket_oku() if int(x.get('stok_id', 0)) == stok_id_int]

    # Eski sürümlerde hareket dosyası yoksa en azından çıkış fişlerinden geçmişi göster.
    if not sonuc:
        for fis in satis_oku():
            if not bool(fis.get('stok_yansit', True)):
                continue
            for satir in fis.get('satirlar', []):
                try:
                    satir_stok_id = int(satir.get('stok_id', 0))
                except Exception:
                    satir_stok_id = 0
                if satir_stok_id == stok_id_int:
                    sonuc.append({
                        'id': 0,
                        'stok_id': stok_id_int,
                        'tarih': fis.get('tarih', ''),
                        'tip': 'Çıkış',
                        'adet': -int(satir.get('adet', 0) or 0),
                        'once': None,
                        'sonra': None,
                        'musteri': fis.get('musteri', ''),
                        'aciklama': satir.get('aciklama', '') or fis.get('aciklama', ''),
                        'fis_id': fis.get('id'),
                        'fis_no': fis.get('fis_no', ''),
                        'marka': satir.get('marka', ''),
                        'kod': satir.get('kod', ''),
                        'desen': satir.get('desen', ''),
                        'ebat': ebat_formatla(satir.get('ebat', '')),
                    })

    def in_range(h):
        dt = hareket_tarih_parse(h.get('tarih'))
        if dt is None:
            return True
        if bas_dt and dt < bas_dt:
            return False
        if bit_dt and dt > bit_dt:
            return False
        return True

    sonuc = [x for x in sonuc if in_range(x)]
    sonuc.sort(key=lambda x: hareket_tarih_parse(x.get('tarih')) or datetime.min, reverse=True)
    return sonuc

def ebat_formatla(value):
    """2255516, 225/55r16, 225-55-16 gibi girişleri 225/55 R16 formatına çevirir."""
    raw = str(value or '').strip().upper()
    digits = re.sub(r'\D', '', raw)
    if len(digits) >= 7:
        return f"{digits[:3]}/{digits[3:5]} R{digits[5:7]}"
    match = re.search(r'(\d{3})\s*[/\- ]?\s*(\d{2})\s*R?\s*(\d{1,2})', raw)
    if match:
        return f"{match.group(1)}/{match.group(2)} R{match.group(3).zfill(2)}"
    return str(value or '').strip()


def tarih_formatla(value):
    """Boşsa kayıt anını, girilirse 27.06.2026 17:12 formatını döndürür."""
    raw = str(value or '').strip()
    if raw == '':
        return datetime.now().strftime('%d.%m.%Y %H:%M')

    digits = re.sub(r'\D', '', raw)
    if len(digits) >= 12:
        return f"{digits[:2]}.{digits[2:4]}.{digits[4:8]} {digits[8:10]}:{digits[10:12]}"

    match = re.search(r'(\d{1,2})[\.\/\-\s](\d{1,2})[\.\/\-\s](\d{4})(?:\s+(\d{1,2})[:\.\-\s]?(\d{1,2}))?', raw)
    if match:
        gun = match.group(1).zfill(2)
        ay = match.group(2).zfill(2)
        yil = match.group(3)
        saat = (match.group(4) or '0').zfill(2)
        dakika = (match.group(5) or '0').zfill(2)
        return f"{gun}.{ay}.{yil} {saat}:{dakika}"

    return raw

def stoklari_ekrana_hazirla(veri):
    hazir = []
    for item in veri:
        kopya = dict(item)
        kopya['ebat'] = ebat_formatla(kopya.get('ebat', ''))
        hazir.append(kopya)
    return hazir


def fiyat_parse_tl(value):
    raw = str(value or '').strip().replace('TL', '').strip()
    if raw == '':
        return 0.0
    raw = raw.replace('.', '').replace(',', '.')
    try:
        return float(raw)
    except ValueError:
        return 0.0


def fiyat_format_tl(value):
    try:
        num = float(value or 0)
    except Exception:
        num = 0
    metin = f"{num:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return metin + " TL"





def fiyat_listesi_kdv_uygula(kayitlar, kdv_yuzde=20):
    """PDF/web fiyatındaki KDV hariç alanını baz alır, üzerine KDV ekleyip stok fiyatı yapar.
    Eğer KDV hariç fiyat yoksa KDV dahil/listedeki fiyatı kullanır.
    """
    try:
        kdv = float(str(kdv_yuzde).replace(',', '.'))
    except Exception:
        kdv = 20.0
    if kdv < 0:
        kdv = 0.0

    sonuc = []
    for rec in kayitlar:
        item = dict(rec)
        fiyat_haric = float(item.get('fiyat_kdv_haric', 0) or 0)
        fiyat_dahil_pdf = float(item.get('fiyat_kdv_dahil', item.get('fiyat_liste', item.get('fiyat', 0))) or 0)
        if fiyat_haric > 0:
            stok_fiyat = round(fiyat_haric * (1 + kdv / 100), 2)
        else:
            stok_fiyat = round(fiyat_dahil_pdf, 2)
        item['fiyat_kdv_haric'] = fiyat_haric
        item['fiyat_kdv_haric_text'] = fiyat_format_tl(fiyat_haric) if fiyat_haric else '-'
        item['fiyat_kdv_dahil_liste'] = fiyat_dahil_pdf
        item['fiyat_kdv_dahil_liste_text'] = fiyat_format_tl(fiyat_dahil_pdf) if fiyat_dahil_pdf else '-'
        item['kdv_yuzde'] = kdv
        item['fiyat'] = stok_fiyat
        item['fiyat_kdv_dahil'] = stok_fiyat
        item['stok_fiyat_text'] = fiyat_format_tl(stok_fiyat)
        sonuc.append(item)
    return sonuc



def continental_web_fiyatlari_kodlardan(kullanici_adi, sifre, kaynak_kayitlar, kdv_yuzde=20):
    """Continental sipariş ekranında ürün kodlarını tek tek arar ve sağdaki net fiyatı okur.
    Okunan net fiyat KDV hariç kabul edilir; stok fiyatı için üzerine %20 KDV eklenir.
    Bu sürüm özellikle Conti ekranındaki "Sipariş > Ara > Lastik Bul" akışına göre güçlendirildi.
    """
    try:
        from .sync_api import sync_, TimeoutError as TimeoutError
    except ImportError as exc:
        raise RuntimeError('Webden veri çekmek için  gerekli. Komutlar: pip install -r requirements.txt  ve  python -m  install chromium') from exc

    def tl_to_float(metin):
        metin = str(metin or '').replace('TL', '').strip()
        metin = re.sub(r'[^\d\.,]', '', metin)
        if not metin:
            return 0.0
        try:
            return float(metin.replace('.', '').replace(',', '.'))
        except Exception:
            return 0.0

    def fiyat_bul_sayfa_text(text):
        """Conti sonuç alanından net fiyatı yakalar.
        Örnek ekranda genelde şu sıra vardır:
        3.913,41 TL
        6.135,00 TL Liste Fiyatı (KDV Hariç)
        Burada Liste Fiyatı'ndan hemen önceki TL değeri net fiyattır.
        """
        text = str(text or '')
        fiyatlar = []
        for m in re.finditer(r'(\d{1,3}(?:\.\d{3})*,\d{2})\s*TL', text):
            fiyatlar.append((m.start(), tl_to_float(m.group(1)), m.group(0)))

        if not fiyatlar:
            return 0.0

        low = text.lower()
        liste_pos = low.find('liste fiyat')
        if liste_pos > 0:
            oncekiler = [x for x in fiyatlar if x[0] < liste_pos]
            if oncekiler:
                return oncekiler[-1][1]

        # Sepet ikonunun olduğu kartta genelde iki fiyat vardır; düşük olan net fiyattır.
        pozitif = [x[1] for x in fiyatlar if x[1] > 0]
        return min(pozitif) if pozitif else 0.0

    def ilk_gorunen_locator(page, selectors, timeout=4000):
        for sel in selectors:
            try:
                locs = page.locator(sel)
                count = locs.count()
                for i in range(count):
                    loc = locs.nth(i)
                    try:
                        loc.wait_for(state='visible', timeout=timeout)
                        return loc
                    except Exception:
                        continue
            except Exception:
                continue
        return None

    def login_yap(page):
        login_url = 'https://www.contionlinecontact.com/cas/login?service=https%3A%2F%2Fwww.contionlinecontact.com%2Fcas%2Foauth2.0%2FcallbackAuthorize%3Fclient_id%3Dprodenvoyproxy%26scope%3DcontiUser%2520email%2520openid%2520profile%26redirect_uri%3Dhttps%253A%252F%252Fwww.contionlinecontact.com%252Flogin%252Fcallback%26response_type%3Dcode%26client_name%3DCasOAuthClient'
        page.goto(login_url, wait_until='domcontentloaded', timeout=60000)

        user_input = ilk_gorunen_locator(page, [
            'input[name="username"]', 'input[id="username"]', 'input[name="user"]',
            'input[type="email"]', 'input[type="text"]'
        ])
        if user_input is None:
            # Bazen oturum zaten açıktır ve direkt siteye düşer.
            if 'contionlinecontact.com/cas/login' not in page.url:
                return
            raise RuntimeError('Giriş ekranında kullanıcı adı alanı bulunamadı.')
        user_input.fill(kullanici_adi)

        pass_input = ilk_gorunen_locator(page, [
            'input[name="password"]', 'input[id="password"]', 'input[type="password"]'
        ])
        if pass_input is None:
            raise RuntimeError('Giriş ekranında şifre alanı bulunamadı.')
        pass_input.fill(sifre)

        clicked = False
        for sel in ['button[type="submit"]', 'input[type="submit"]', 'button:has-text("Giriş")', 'button:has-text("Login")']:
            try:
                page.locator(sel).first.click(timeout=5000)
                clicked = True
                break
            except Exception:
                pass
        if not clicked:
            page.keyboard.press('Enter')

        try:
            page.wait_for_load_state('networkidle', timeout=60000)
        except Exception:
            page.wait_for_timeout(6000)

        body_text = ''
        try:
            body_text = page.locator('body').inner_text(timeout=5000)
        except Exception:
            pass
        if 'hatalı' in body_text.lower() or 'invalid' in body_text.lower():
            raise RuntimeError('Continental giriş başarısız görünüyor. Kullanıcı adı veya şifreyi kontrol et.')

    def siparis_sayfasina_git(page):
        # Ana sayfada üst menüde SİPARİŞ yazısı var. Önce onu tıkla.
        for txt in ['SİPARİŞ', 'SIPARIS', 'Sipariş', 'SİPARIS']:
            try:
                page.get_by_text(txt, exact=False).first.click(timeout=8000)
                try:
                    page.wait_for_load_state('networkidle', timeout=30000)
                except Exception:
                    page.wait_for_timeout(3000)
                break
            except Exception:
                continue

        # Arama kutusu geldiyse doğru sayfadayız.
        arama = ilk_gorunen_locator(page, [
            'input[placeholder*="ürün"]', 'input[placeholder*="Ürün"]',
            'input[placeholder*="EAN"]', 'input[placeholder*="ürün kodu"]',
            'input[type="search"]', 'input[type="text"]'
        ], timeout=2500)
        if arama is not None:
            return

        # Menü tıklanamazsa bilinen URL'leri dene.
        for url in [
            'https://www.contionlinecontact.com/order/',
            'https://www.contionlinecontact.com/home/',
            'https://www.contionlinecontact.com/'
        ]:
            try:
                page.goto(url, wait_until='networkidle', timeout=40000)
                arama = ilk_gorunen_locator(page, [
                    'input[placeholder*="ürün"]', 'input[placeholder*="Ürün"]',
                    'input[placeholder*="EAN"]', 'input[placeholder*="ürün kodu"]',
                    'input[type="search"]', 'input[type="text"]'
                ], timeout=4000)
                if arama is not None:
                    return
            except Exception:
                pass

    def kod_ara(page, kod):
        kod_orj = str(kod or '').strip()
        kod = re.sub(r'\D', '', kod_orj) or kod_orj
        if not kod:
            return 0.0, 'Kod boş'

        search_input = ilk_gorunen_locator(page, [
            'input[placeholder*="ürün"]',
            'input[placeholder*="Ürün"]',
            'input[placeholder*="EAN"]',
            'input[placeholder*="ürün kodu"]',
            'input[type="search"]',
            'input[type="text"]'
        ], timeout=8000)

        if search_input is None:
            return 0.0, 'Arama kutusu bulunamadı'

        try:
            search_input.click()
            page.keyboard.press('Control+A')
            search_input.fill(kod)
        except Exception:
            return 0.0, 'Arama kutusuna kod yazılamadı'

        clicked = False
        # Conti ekranındaki siyah buton: Lastik Bul.
        for sel in [
            'button:has-text("Lastik Bul")',
            'button:has-text("Ara")',
            'button:has-text("Bul")'
        ]:
            try:
                page.locator(sel).last.click(timeout=6000)
                clicked = True
                break
            except Exception:
                pass
        if not clicked:
            for txt in ['Lastik Bul', 'Ara', 'Bul']:
                try:
                    page.get_by_text(txt, exact=False).last.click(timeout=5000)
                    clicked = True
                    break
                except Exception:
                    pass
        if not clicked:
            try:
                search_input.press('Enter')
                clicked = True
            except Exception:
                pass

        if not clicked:
            return 0.0, 'Lastik Bul butonu tıklanamadı'

        # Sonuçlar bazen geç düşüyor. TL veya arama sonuçları yazısını bekle.
        try:
            page.wait_for_function(
                "() => document.body && (/TL|Arama sonuç|Arama sonu|Liste Fiyat/i).test(document.body.innerText)",
                timeout=35000
            )
        except Exception:
            page.wait_for_timeout(5000)

        try:
            text = page.locator('body').inner_text(timeout=15000)
        except Exception:
            text = page.content()

        # Kod sonuç metninde yoksa da fiyat aramayı deniyoruz; bazı ekranlar kodu kısa/uzun yazıyor.
        fiyat = fiyat_bul_sayfa_text(text)
        if fiyat <= 0:
            debug_path = PRICE_IMPORT_DIR / f"conti_kod_{kod}_sonuc_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            try:
                debug_path.write_text(text[:8000], encoding='utf-8')
            except Exception:
                pass
            return 0.0, f'Fiyat bulunamadı. Sonuç metni kaydedildi: {debug_path.name}'
        return fiyat, ''

    if not kaynak_kayitlar:
        raise RuntimeError('Webden fiyat çekmek için en az bir ürün kodu gerekli.')

    sonuc = []
    hatalar = []
    headless = os.environ.get('CONTI_HEADLESS', '').lower() in ['1', 'true', 'yes']
    with sync_() as pw:
        browser = pw.chromium.launch(headless=headless, slow_mo=120 if not headless else 0)
        context = browser.new_context(accept_downloads=True, viewport={'width': 1600, 'height': 900})
        page = context.new_page()
        try:
            login_yap(page)
            siparis_sayfasina_git(page)

            for idx, rec in enumerate(kaynak_kayitlar, start=1):
                kod = str(rec.get('kod', '')).strip()
                if not kod:
                    hatalar.append(f'{idx}. kayıt kodsuz atlandı.')
                    continue

                net_fiyat, hata = kod_ara(page, kod)
                item = dict(rec)
                item['marka'] = item.get('marka') or 'Continental'
                item['fiyat_kdv_haric'] = round(net_fiyat, 2) if net_fiyat else 0
                item['web_hata'] = hata
                if net_fiyat:
                    item['fiyat'] = round(net_fiyat * (1 + float(kdv_yuzde) / 100), 2)
                    item['notlar'] = (item.get('notlar', '') + ' Web net fiyat').strip()
                else:
                    # Fiyat bulunamazsa PDF'deki fiyatı kullanma; kullanıcı hata olduğunu görsün.
                    item['fiyat'] = 0
                    hatalar.append(f'{kod}: {hata}')
                sonuc.append(item)

            if hatalar:
                hata_path = PRICE_IMPORT_DIR / f"continental_web_uyari_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
                hata_path.write_text('\n'.join(hatalar), encoding='utf-8')
            return sonuc
        except Exception as e:
            ekran = PRICE_IMPORT_DIR / f"continental_web_hata_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            try:
                page.screenshot(path=str(ekran), full_page=True)
                raise RuntimeError(f'{e} | Ekran görüntüsü: {ekran.name}')
            except Exception:
                raise
        finally:
            try:
                browser.close()
            except Exception:
                pass


def continental_web_pdf_indir(kullanici_adi, sifre):
    """Eski PDF indirme deneme fonksiyonu artık kullanılmıyor.
    Yerine ürün kodlarını sipariş ekranında arayan continental_web_fiyatlari_kodlardan kullanılıyor.
    """
    raise RuntimeError('Bu sürümde PDF indirme yerine ürün kodundan web fiyatı okuma kullanılıyor.')

def kod_normalize(value):
    digits = re.sub(r'\D', '', str(value or ''))
    digits = digits.lstrip('0')
    return digits or re.sub(r'\W+', '', str(value or '').upper())


def metin_normalize(value):
    return re.sub(r'\s+', ' ', str(value or '').strip().upper())


def marka_tahmin_et(dosya_adi, pdf_text):
    birlesik = (str(dosya_adi or '') + ' ' + str(pdf_text or '')[:2000]).lower()
    markalar = ['continental', 'pirelli', 'lassa', 'goodyear', 'matador', 'barum', 'prometeon', 'anteo', 'pharos']
    for marka in markalar:
        if marka in birlesik:
            return marka.capitalize() if marka != 'goodyear' else 'Goodyear'
    if 'conti' in birlesik:
        return 'Continental'
    return ''


def mevsim_tahmin_et(dosya_adi, pdf_text):
    birlesik = (str(dosya_adi or '') + ' ' + str(pdf_text or '')[:3000]).lower()
    if 'kış' in birlesik or 'kis' in birlesik or 'winter' in birlesik:
        return 'Kış'
    if '4 mevsim' in birlesik or 'allseason' in birlesik or 'all season' in birlesik:
        return '4 Mev.'
    if 'yaz' in birlesik or 'summer' in birlesik:
        return 'Yaz'
    return ''


def pdf_satirlarini_oku(pdf_path):
    try:
        import pdfplumber
    except ImportError as exc:
        raise RuntimeError("PDF okuma için pdfplumber kurulu değil. Komut: pip install -r requirements.txt") from exc

    tum_text = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            text = page.extract_text(x_tolerance=1, y_tolerance=3) or ''
            tum_text.append(text)
    return "\n".join(tum_text)


def fiyat_listesi_pdf_coz(pdf_path, dosya_adi=''):
    text = pdf_satirlarini_oku(pdf_path)
    marka = marka_tahmin_et(dosya_adi, text)
    mevsim = mevsim_tahmin_et(dosya_adi, text)
    kayitlar = []
    son_ebat = ''
    son_index = ''

    # Satırları temizle; PDF'de ürün adı bazen satır kırılabiliyor.
    ham_satirlar = [re.sub(r'\s+', ' ', x).strip() for x in text.splitlines()]
    ham_satirlar = [x for x in ham_satirlar if x]

    # Devam satırlarını önceki satıra bağlamak için basit tampon.
    birlesik_satirlar = []
    for line in ham_satirlar:
        if not re.search(r'\d{2}\s+\d{2}\s+\d{3}', line) and birlesik_satirlar and not re.match(r'^\d{2,3}/\d{2}\s*R', line, re.I):
            # Ürün adının devamı olabilir: ContiPremiumContact / 2 gibi.
            if len(line) < 45 and not any(x in line.lower() for x in ['lastik ebadı', 'kdv', 'etiket', 'seri', 'fiyatlara', 'ab etiket']):
                birlesik_satirlar[-1] += ' ' + line
                continue
        birlesik_satirlar.append(line)

    for line in birlesik_satirlar:
        lower = line.lower()
        if any(skip in lower for skip in ['lastik ebadı', 'yük-hız', 'ürün adı', 'kdv hariç', 'kdv dahil', 'etiket değerleri', 'fiyatlara', 'ab etiket', 'binek yaz', 'binek kış']):
            continue
        if re.match(r'^\d+\s*seri$', lower) or re.match(r'^\d+\s*seri', lower):
            continue

        prices = re.findall(r'\d{1,3}(?:\.\d{3})*,\d{2}', line)
        code_match = re.search(r'\b(\d{2}\s+\d{2}\s+\d{3})\b', line)
        if not prices or not code_match:
            continue

        fiyat_haric = fiyat_parse_tl(prices[-2]) if len(prices) >= 2 else 0
        fiyat_dahil = fiyat_parse_tl(prices[-1])
        kod = code_match.group(1)
        kod_duz = kod.replace(' ', '')

        before = line[:code_match.start()].strip()
        after = line[code_match.end():].strip()

        ebat = ''
        yuk_hiz = ''
        xl_var = ''
        size_match = re.match(r'^(\d{3}/\d{2}\s*R\s*\d{2})(?:\s+(XL))?\s*(.*)$', before, re.I)
        if size_match:
            ebat = ebat_formatla(size_match.group(1))
            xl_var = size_match.group(2) or ''
            kalan = size_match.group(3).strip()
            son_ebat = ebat
        else:
            ebat = son_ebat
            kalan = before

        idx_match = re.match(r'^(?:XL\s+)?(\d{2,3}[A-Z])\s+(.+)$', kalan, re.I)
        if idx_match:
            yuk_hiz = idx_match.group(1).upper()
            urun_adi = idx_match.group(2).strip()
            son_index = yuk_hiz
        else:
            # Bazı satırlarda ebat hücresi birleşik kalır, index de boş olabilir.
            yuk_hiz = son_index
            urun_adi = kalan.strip()

        if xl_var and 'XL' not in yuk_hiz:
            yuk_hiz = ('XL ' + yuk_hiz).strip()

        # Etiket değerleri ve notlar: ürün kodundan sonraki kısımda fiyatlara kadar olan alanı kırp.
        price_pos = after.rfind(prices[-1])
        notlar = ''
        if price_pos >= 0:
            notlar = after[price_pos + len(prices[-1]):].strip()
        notlar = notlar.strip(' -')

        if not ebat or not urun_adi:
            continue

        kayitlar.append({
            'marka': marka,
            'ebat': ebat,
            'yuk_hiz': yuk_hiz,
            'desen': urun_adi,
            'kod': kod_duz,
            'kod_pdf': kod,
            'mevsim': mevsim,
            'fiyat_kdv_haric': fiyat_haric,
            'fiyat_kdv_dahil': fiyat_dahil,
            'fiyat': fiyat_dahil,
            'fiyat_liste': fiyat_dahil,
            'fiyat_liste_text': fiyat_format_tl(fiyat_dahil),
            'notlar': notlar,
            'durum_notu': 'Üretimi Sonlanacak' if 'üretimi sonlan' in notlar.lower() else ''
        })

    # Aynı ürün kodu birden fazla yakalanırsa sonuncuyu koru.
    tekil = {}
    for k in kayitlar:
        key = kod_normalize(k.get('kod')) or (k.get('marka'), k.get('ebat'), k.get('desen'))
        tekil[key] = k
    return list(tekil.values())


def fiyat_listesi_onizleme_hazirla(pdf_kayitlari):
    stoklar = stok_oku()
    stok_by_code = {kod_normalize(x.get('kod')): x for x in stoklar if kod_normalize(x.get('kod'))}
    stok_by_combo = {}
    for x in stoklar:
        combo = (metin_normalize(x.get('marka')), ebat_formatla(x.get('ebat')), metin_normalize(x.get('desen')))
        stok_by_combo[combo] = x

    sonuc = []
    sayac = {'guncellenecek': 0, 'yeni': 0, 'ayni': 0}
    for rec in pdf_kayitlari:
        match = None
        code_key = kod_normalize(rec.get('kod'))
        if code_key in stok_by_code:
            match = stok_by_code[code_key]
        if match is None:
            combo = (metin_normalize(rec.get('marka')), ebat_formatla(rec.get('ebat')), metin_normalize(rec.get('desen')))
            match = stok_by_combo.get(combo)

        eski_fiyat = float(match.get('fiyat', 0) or 0) if match else 0
        yeni_fiyat = float(rec.get('fiyat', 0) or 0)
        if match:
            if abs(eski_fiyat - yeni_fiyat) >= 0.01:
                durum = 'guncellenecek'
            else:
                durum = 'ayni'
        else:
            durum = 'yeni'
        sayac[durum] += 1

        item = dict(rec)
        mevcut_miktar = int(match.get('miktar', 0) or 0) if match else None
        item.update({
            'durum': durum,
            'stok_id': match.get('id') if match else None,
            'eski_fiyat': eski_fiyat,
            'eski_fiyat_text': fiyat_format_tl(eski_fiyat) if match else '-',
            'yeni_fiyat_text': fiyat_format_tl(yeni_fiyat),
            'mevcut_miktar': mevcut_miktar,
            'miktar_text': str(mevcut_miktar) if mevcut_miktar is not None else '-',
            'miktar_value': mevcut_miktar if mevcut_miktar is not None else ''
        })
        sonuc.append(item)

    return sonuc, sayac


def fiyat_listesi_uygula_kayitlari(pdf_kayitlari, yeni_urun_ekle=True, miktarlar=None):
    miktarlar = miktarlar or {}
    stoklar = stok_oku()
    stok_by_code = {kod_normalize(x.get('kod')): x for x in stoklar if kod_normalize(x.get('kod'))}
    mevcut_max_id = max([int(x.get('id', 0)) for x in stoklar], default=0)
    sayac = {'guncellendi': 0, 'eklendi': 0, 'ayni': 0, 'atlanmis': 0, 'miktar_guncellendi': 0}

    yedek_al('fiyat_listesi_oncesi')

    for rec in pdf_kayitlari:
        code_key = kod_normalize(rec.get('kod'))
        match = stok_by_code.get(code_key)

        miktar_girisi = miktarlar.get(str(rec.get('kod', '')), None)
        if miktar_girisi is None:
            miktar_girisi = miktarlar.get(code_key, None)
        miktar_degistir = miktar_girisi not in (None, '')
        try:
            yeni_miktar = int(miktar_girisi) if miktar_degistir else None
            if yeni_miktar is not None and yeni_miktar < 0:
                yeni_miktar = 0
        except Exception:
            yeni_miktar = None
            miktar_degistir = False

        if match:
            eski = float(match.get('fiyat', 0) or 0)
            yeni = float(rec.get('fiyat', 0) or 0)
            match['fiyat'] = yeni
            match['marka'] = rec.get('marka') or match.get('marka', '')
            match['desen'] = rec.get('desen') or match.get('desen', '')
            match['ebat'] = ebat_formatla(rec.get('ebat') or match.get('ebat', ''))
            match['mevsim'] = rec.get('mevsim') or match.get('mevsim', '')
            match['kod'] = rec.get('kod') or match.get('kod', '')
            if miktar_degistir:
                eski_miktar = int(match.get('miktar', 0) or 0)
                match['miktar'] = yeni_miktar
                if eski_miktar != yeni_miktar:
                    sayac['miktar_guncellendi'] += 1
                    hareket_ekle(match.get('id'), 'Fiyat Listesi Miktar', yeni_miktar - eski_miktar, eski_miktar, yeni_miktar, aciklama='Fiyat listesi ekranından miktar güncellendi', stok_kayit=match)
            if rec.get('notlar'):
                match['notlar'] = rec.get('notlar')
            if abs(eski - yeni) >= 0.01:
                sayac['guncellendi'] += 1
            else:
                sayac['ayni'] += 1
        else:
            if not yeni_urun_ekle:
                sayac['atlanmis'] += 1
                continue
            mevcut_max_id += 1
            yeni_kayit = {
                'id': mevcut_max_id,
                'marka': rec.get('marka', ''),
                'kod': rec.get('kod', ''),
                'desen': rec.get('desen', ''),
                'ebat': ebat_formatla(rec.get('ebat', '')),
                'mevsim': rec.get('mevsim', ''),
                'yil': str(datetime.now().year),
                'depo': '',
                'rf_ssr': 'x',
                'fiyat': float(rec.get('fiyat', 0) or 0),
                'miktar': yeni_miktar if yeni_miktar is not None else 0,
                'notlar': rec.get('notlar', '')
            }
            stoklar.append(yeni_kayit)
            stok_by_code[kod_normalize(yeni_kayit.get('kod'))] = yeni_kayit
            sayac['eklendi'] += 1
            if yeni_miktar is not None:
                sayac['miktar_guncellendi'] += 1
            if int(yeni_kayit.get('miktar', 0) or 0) > 0:
                hareket_ekle(yeni_kayit.get('id'), 'Fiyat Listesi Giriş', int(yeni_kayit.get('miktar', 0) or 0), 0, int(yeni_kayit.get('miktar', 0) or 0), aciklama='Fiyat listesinden yeni ürün eklendi', stok_kayit=yeni_kayit)

    stok_yaz(stoklar)
    log_yaz(f"Fiyat listesi uygulandı: güncellendi={sayac['guncellendi']}, eklendi={sayac['eklendi']}, aynı={sayac['ayni']}, atlandı={sayac['atlanmis']}, miktar={sayac['miktar_guncellendi']}")
    return sayac


def yedek_al(neden="manuel"):
    BACKUP_DIR.mkdir(exist_ok=True)
    if not STOK_FILE.exists():
        STOK_FILE.write_text("[]", encoding="utf-8")
    if not SATIS_FILE.exists():
        SATIS_FILE.write_text("[]", encoding="utf-8")
    if not HAREKET_FILE.exists():
        HAREKET_FILE.write_text("[]", encoding="utf-8")

    zaman = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    olusanlar = []

    stok_hedef = BACKUP_DIR / f"stok_yedek_{zaman}_{neden}.json"
    shutil.copy2(STOK_FILE, stok_hedef)
    olusanlar.append(stok_hedef)

    satis_hedef = BACKUP_DIR / f"cikis_fisleri_yedek_{zaman}_{neden}.json"
    shutil.copy2(SATIS_FILE, satis_hedef)
    olusanlar.append(satis_hedef)

    hareket_hedef = BACKUP_DIR / f"stok_hareketleri_yedek_{zaman}_{neden}.json"
    shutil.copy2(HAREKET_FILE, hareket_hedef)
    olusanlar.append(hareket_hedef)

    if DB_FILE.exists():
        db_hedef = BACKUP_DIR / f"lastik_stok_db_yedek_{zaman}_{neden}.db"
        shutil.copy2(DB_FILE, db_hedef)
        olusanlar.append(db_hedef)

    zip_yol = tam_yedek_zip_olustur(zaman, neden)
    olusanlar.append(zip_yol)

    harici_sonuc = harici_yedeklere_kopyala([zip_yol])
    log_yaz(f"Yedek alındı: {zip_yol.name}")
    return {
        "dosya": zip_yol.name,
        "yerel_klasor": str(BACKUP_DIR),
        "harici": harici_sonuc,
        "olusanlar": [x.name for x in olusanlar]
    }


def gunluk_yedek_motoru():
    while True:
        try:
            now = datetime.now()
            bugun = now.strftime("%Y-%m-%d")
            son = LAST_BACKUP_MARKER.read_text(encoding="utf-8").strip() if LAST_BACKUP_MARKER.exists() else ""
            if now.hour >= 18 and son != bugun:
                yedek_al("otomatik")
                LAST_BACKUP_MARKER.write_text(bugun, encoding="utf-8")
        except Exception as e:
            log_yaz(f"Otomatik yedek hatası: {e}")
        time.sleep(60)


sqlite_ilk_aktarim()

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/login')
def login():
    return render_template('login.html')


@app.route('/stok')
def stok():
    return render_template('stok.html')


@app.route('/ekle')
def ekle():
    return render_template('ekle.html')


@app.route('/cikis-fisi')
def cikis_fisi():
    return render_template('cikis_fisi.html')






@app.route('/fiyat-listesi')
def fiyat_listesi():
    return render_template('fiyat_listesi.html')


@app.route('/api/fiyat-listesi/oku', methods=['POST'])
def api_fiyat_listesi_oku():
    if 'pdf' not in request.files:
        return jsonify({'ok': False, 'hata': 'PDF dosyası seçilmedi.'}), 400
    dosya = request.files['pdf']
    if not dosya.filename.lower().endswith('.pdf'):
        return jsonify({'ok': False, 'hata': 'Sadece PDF dosyası yükleyebilirsin.'}), 400

    token = uuid.uuid4().hex
    pdf_path = PRICE_IMPORT_DIR / f"{token}.pdf"
    json_path = PRICE_IMPORT_DIR / f"{token}.json"
    dosya.save(pdf_path)

    try:
        kayitlar = fiyat_listesi_pdf_coz(pdf_path, dosya.filename)
        marka = kayitlar[0].get('marka', '') if kayitlar else marka_tahmin_et(dosya.filename, '')
        onizleme, sayac = fiyat_listesi_onizleme_hazirla(kayitlar)
        json_path.write_text(json.dumps(kayitlar, ensure_ascii=False, indent=2), encoding='utf-8')
        log_yaz(f"Fiyat listesi okundu: {dosya.filename}, ürün sayısı={len(kayitlar)}")
        return jsonify({
            'ok': True,
            'token': token,
            'sayac': sayac,
            'toplam': len(kayitlar),
            'kayitlar': onizleme[:500],
            'marka': marka
        })
    except Exception as e:
        log_yaz(f"Fiyat listesi okuma hatası: {e}")
        return jsonify({'ok': False, 'hata': str(e)}), 500


@app.route('/api/fiyat-listesi/uygula', methods=['POST'])
def api_fiyat_listesi_uygula():
    gelen = request.get_json(force=True)
    token = str(gelen.get('token', '')).strip()
    yeni_urun_ekle = bool(gelen.get('yeni_urun_ekle', True))
    json_path = PRICE_IMPORT_DIR / f"{token}.json"
    if not token or not json_path.exists():
        return jsonify({'ok': False, 'hata': 'Ön izleme dosyası bulunamadı. PDF tekrar okunmalı.'}), 404

    kayitlar = json.loads(json_path.read_text(encoding='utf-8'))
    miktarlar = gelen.get('miktarlar', {}) or {}
    sayac = fiyat_listesi_uygula_kayitlari(kayitlar, yeni_urun_ekle=yeni_urun_ekle, miktarlar=miktarlar)
    return jsonify({'ok': True, 'sayac': sayac})


@app.route('/api/continental-web/oku', methods=['POST'])
def api_continental_web_oku():
    """PDF'den ürün kodlarını alır, Conti sipariş ekranında tek tek aratıp net fiyatı okur.
    Net fiyatın üzerine %20 KDV eklenmiş hali ön izlemeye ve stoka gider.
    """
    token = uuid.uuid4().hex
    json_path = PRICE_IMPORT_DIR / f"{token}.json"

    try:
        # Hem FormData hem JSON destekle.
        if request.content_type and 'multipart/form-data' in request.content_type:
            kullanici_adi = str(request.form.get('username', '')).strip()
            sifre = str(request.form.get('password', '')).strip()
            kaynak_kayitlar = []

            if 'pdf' in request.files and request.files['pdf'].filename:
                dosya = request.files['pdf']
                if not dosya.filename.lower().endswith('.pdf'):
                    return jsonify({'ok': False, 'hata': 'Webden okuma için seçilen dosya PDF olmalı.'}), 400
                pdf_path = PRICE_IMPORT_DIR / f"{token}_kaynak.pdf"
                dosya.save(pdf_path)
                kaynak_kayitlar = fiyat_listesi_pdf_coz(pdf_path, dosya.filename)
            else:
                # PDF seçilmezse mevcut stoktaki Continental kodlarını dene.
                kaynak_kayitlar = [x for x in stok_oku() if metin_normalize(x.get('marka')) == 'CONTINENTAL']
        else:
            gelen = request.get_json(force=True)
            kullanici_adi = str(gelen.get('username', '')).strip()
            sifre = str(gelen.get('password', '')).strip()
            kaynak_kayitlar = gelen.get('kayitlar') or [x for x in stok_oku() if metin_normalize(x.get('marka')) == 'CONTINENTAL']

        if not kullanici_adi or not sifre:
            return jsonify({'ok': False, 'hata': 'Kullanıcı adı ve şifre gerekli.'}), 400

        if not kaynak_kayitlar:
            return jsonify({'ok': False, 'hata': 'Okunacak ürün kodu bulunamadı. Önce Continental PDF seç veya stokta Continental ürün olsun.'}), 400

        # Kodları normalize ederek tekrarlı ürünleri azalt.
        tekil = []
        gorulen = set()
        for rec in kaynak_kayitlar:
            key = kod_normalize(rec.get('kod'))
            if not key or key in gorulen:
                continue
            gorulen.add(key)
            tekil.append(rec)

        kayitlar_web = continental_web_fiyatlari_kodlardan(kullanici_adi, sifre, tekil, 20)
        # JSON'a KDV hariç net web fiyatı kaydedilir; uygulama sırasında tekrar KDV hesaplanır.
        json_path.write_text(json.dumps(kayitlar_web, ensure_ascii=False, indent=2), encoding='utf-8')

        kayitlar_kdvli = fiyat_listesi_kdv_uygula(kayitlar_web, 20)
        onizleme, sayac = fiyat_listesi_onizleme_hazirla(kayitlar_kdvli)

        okunan = len([x for x in kayitlar_web if float(x.get('fiyat_kdv_haric', 0) or 0) > 0])
        log_yaz(f"Continental webden kod bazlı fiyat okundu: okunan={okunan}, toplam={len(kayitlar_web)}, KDV=%20")
        return jsonify({
            'ok': True,
            'token': token,
            'sayac': sayac,
            'toplam': len(kayitlar_web),
            'okunan': okunan,
            'kayitlar': onizleme[:500],
            'marka': 'Continental',
            'kdv_yuzde': 20,
            'mesaj': f'{okunan}/{len(kayitlar_web)} ürünün web fiyatı okundu.'
        })
    except Exception as e:
        log_yaz(f"Continental web okuma hatası: {e}")
        return jsonify({'ok': False, 'hata': str(e)}), 500

@app.route('/duzenle/<int:kayit_id>')
def duzenle(kayit_id):
    for item in stok_oku():
        if int(item.get('id', 0)) == kayit_id:
            item = dict(item)
            item['ebat'] = ebat_formatla(item.get('ebat', ''))
            return render_template('duzenle.html', kayit=item)
    return redirect(url_for('stok'))


@app.route('/api/stok/<int:kayit_id>', methods=['GET'])
def api_stok_tek(kayit_id):
    for item in stok_oku():
        if int(item.get('id', 0)) == kayit_id:
            return jsonify({'ok': True, 'kayit': item})
    return jsonify({'ok': False, 'hata': 'Kayıt bulunamadı'}), 404

@app.route('/api/stok', methods=['GET'])
def api_stok_liste():
    return jsonify(stoklari_ekrana_hazirla(stok_oku()))


@app.route('/api/stok', methods=['POST'])
def api_stok_ekle():
    veri = stok_oku()
    gelen = request.get_json(force=True)
    yeni_id = max([int(x.get('id', 0)) for x in veri], default=0) + 1
    kayit = {
        'id': yeni_id,
        'marka': gelen.get('marka', '').strip(),
        'kod': gelen.get('kod', '').strip(),
        'desen': gelen.get('desen', '').strip(),
        'ebat': ebat_formatla(gelen.get('ebat', '')),
        'mevsim': gelen.get('mevsim', '').strip(),
        'yil': str(gelen.get('yil', '')).strip(),
        'depo': gelen.get('depo', '').strip(),
        'rf_ssr': gelen.get('rf_ssr', 'x').strip() or 'x',
        'fiyat': float(gelen.get('fiyat', 0) or 0),
        'miktar': int(gelen.get('miktar', 0) or 0)
    }
    yedek_al("islem_oncesi")
    veri.append(kayit)
    stok_yaz(veri)
    if int(kayit.get('miktar', 0) or 0) > 0:
        hareket_ekle(yeni_id, 'Giriş', int(kayit.get('miktar', 0) or 0), 0, int(kayit.get('miktar', 0) or 0), aciklama='Yeni stok kaydı', stok_kayit=kayit)
    log_yaz(f"Yeni stok eklendi: ID={yeni_id}, Kod={kayit['kod']}, Marka={kayit['marka']}")
    return jsonify({'ok': True, 'kayit': kayit})


@app.route('/api/stok/<int:kayit_id>', methods=['PUT'])
def api_stok_duzelt(kayit_id):
    veri = stok_oku()
    gelen = request.get_json(force=True)
    for item in veri:
        if int(item.get('id', 0)) == kayit_id:
            yedek_al("duzeltme_oncesi")
            eski_miktar = int(item.get('miktar', 0) or 0)
            item.update({
                'marka': gelen.get('marka', item.get('marka', '')).strip(),
                'kod': gelen.get('kod', item.get('kod', '')).strip(),
                'desen': gelen.get('desen', item.get('desen', '')).strip(),
                'ebat': ebat_formatla(gelen.get('ebat', item.get('ebat', ''))),
                'mevsim': gelen.get('mevsim', item.get('mevsim', '')).strip(),
                'yil': str(gelen.get('yil', item.get('yil', ''))).strip(),
                'depo': gelen.get('depo', item.get('depo', '')).strip(),
                'rf_ssr': gelen.get('rf_ssr', item.get('rf_ssr', 'x')).strip() or 'x',
                'fiyat': float(gelen.get('fiyat', item.get('fiyat', 0)) or 0),
                'miktar': int(gelen.get('miktar', item.get('miktar', 0)) or 0)
            })
            yeni_miktar = int(item.get('miktar', 0) or 0)
            stok_yaz(veri)
            if yeni_miktar != eski_miktar:
                hareket_ekle(kayit_id, 'Stok Düzenleme', yeni_miktar - eski_miktar, eski_miktar, yeni_miktar, aciklama='Stok miktarı düzeltildi', stok_kayit=item)
            log_yaz(f"Stok düzeltildi: ID={kayit_id}, Kod={item.get('kod', '')}")
            return jsonify({'ok': True, 'kayit': item})
    return jsonify({'ok': False, 'hata': 'Kayıt bulunamadı'}), 404


@app.route('/api/stok/<int:kayit_id>', methods=['DELETE'])
def api_stok_sil(kayit_id):
    veri = stok_oku()
    silinen = next((x for x in veri if int(x.get('id', 0)) == kayit_id), None)
    yeni_veri = [x for x in veri if int(x.get('id', 0)) != kayit_id]
    if len(yeni_veri) == len(veri):
        return jsonify({'ok': False, 'hata': 'Kayıt bulunamadı'}), 404
    yedek_al("silme_oncesi")
    stok_yaz(yeni_veri)
    if silinen:
        eski_miktar = int(silinen.get('miktar', 0) or 0)
        hareket_ekle(kayit_id, 'Stok Silme', -eski_miktar, eski_miktar, 0, aciklama='Stok kaydı silindi', stok_kayit=silinen)
    log_yaz(f"Stok silindi: ID={kayit_id}")
    return jsonify({'ok': True})


@app.route('/api/stok/<int:kayit_id>/hareketler', methods=['GET'])
def api_stok_hareketler(kayit_id):
    baslangic = request.args.get('baslangic', '')
    bitis = request.args.get('bitis', '')
    return jsonify({'ok': True, 'hareketler': stok_hareketleri_getir(kayit_id, baslangic, bitis)})


@app.route('/api/yedek-al', methods=['POST'])
def api_yedek_al():
    sonuc = yedek_al("manuel")
    return jsonify({'ok': True, **sonuc})


@app.route('/yedek-ayarlari')
def yedek_ayarlari():
    return render_template('yedek_ayarlari.html')


@app.route('/api/yedek-ayarlari', methods=['GET', 'POST'])
def api_yedek_ayarlari():
    if request.method == 'GET':
        return jsonify({'ok': True, 'ayarlar': yedek_ayarlari_oku()})
    gelen = request.get_json(force=True)
    ayarlar = yedek_ayarlari_yaz(gelen or {})
    log_yaz('Yedek ayarları güncellendi.')
    return jsonify({'ok': True, 'ayarlar': ayarlar})


@app.route('/api/yedek-test', methods=['POST'])
def api_yedek_test():
    sonuc = yedek_al("test")
    return jsonify({'ok': True, **sonuc})




def stok_miktar_degistir(stoklar, stok_id, delta):
    """delta pozitifse stok artar, negatifse stok düşer."""
    if stok_id in (None, '', 'manual'):
        return True, None
    for item in stoklar:
        if int(item.get('id', 0)) == int(stok_id):
            mevcut = int(item.get('miktar', 0) or 0)
            yeni = mevcut + int(delta)
            if yeni < 0:
                return False, f"Yetersiz stok: {item.get('kod', '')} / mevcut {mevcut}, istenen {-int(delta)}"
            item['miktar'] = yeni
            return True, item
    return False, 'Stok kaydı bulunamadı.'


def fis_satirlarini_temizle(satirlar):
    temiz_satirlar = []
    for satir in satirlar or []:
        adet = int(satir.get('adet', 0) or 0)
        if adet <= 0:
            continue
        fiyat = float(satir.get('fiyat', 0) or 0)
        toplam_tutar = float(satir.get('toplam_tutar', 0) or 0)
        if toplam_tutar <= 0:
            toplam_tutar = adet * fiyat
        temiz_satirlar.append({
            'stok_id': satir.get('stok_id'),
            'marka': str(satir.get('marka', '')).strip(),
            'kod': str(satir.get('kod', '')).strip(),
            'desen': str(satir.get('desen', '')).strip(),
            'ebat': ebat_formatla(satir.get('ebat', '')),
            'adet': adet,
            'fiyat': fiyat,
            'toplam_tutar': toplam_tutar,
            'aciklama': str(satir.get('aciklama', '')).strip()
        })
    return temiz_satirlar


def fis_toplamlarini_hesapla(temiz_satirlar, gelen_toplam_tutar=None):
    toplam_adet = sum(int(x.get('adet', 0) or 0) for x in temiz_satirlar)
    satir_toplam = sum(float(x.get('toplam_tutar', 0) or 0) for x in temiz_satirlar)
    otomatik_toplam = sum(int(x.get('adet', 0) or 0) * float(x.get('fiyat', 0) or 0) for x in temiz_satirlar)
    try:
        toplam_tutar = float(gelen_toplam_tutar) if gelen_toplam_tutar not in (None, '') else (satir_toplam or otomatik_toplam)
    except Exception:
        toplam_tutar = satir_toplam or otomatik_toplam
    return toplam_adet, toplam_tutar

@app.route('/api/cikis-fisleri', methods=['GET'])
def api_cikis_fisleri():
    return jsonify(satis_oku())


@app.route('/api/cikis-fisi', methods=['POST'])
def api_cikis_fisi_kaydet():
    gelen = request.get_json(force=True)
    musteri = str(gelen.get('musteri', '')).strip()
    aciklama_genel = str(gelen.get('aciklama', '')).strip()
    gelen_tarih = tarih_formatla(gelen.get('tarih', ''))
    gelen_toplam_tutar = gelen.get('toplam_tutar', None)
    stok_yansit = bool(gelen.get('stok_yansit', True))
    temiz_satirlar = fis_satirlarini_temizle(gelen.get('satirlar', []))

    if not temiz_satirlar:
        return jsonify({'ok': False, 'hata': 'Kaydedilecek satış satırı yok.'}), 400

    stoklar = stok_oku()
    yedek_al('cikis_fisi_oncesi')

    if stok_yansit:
        for satir in temiz_satirlar:
            ok, sonuc = stok_miktar_degistir(stoklar, satir.get('stok_id'), -int(satir.get('adet', 0) or 0))
            if not ok:
                return jsonify({'ok': False, 'hata': sonuc}), 400
        stok_yaz(stoklar)

    fisler = satis_oku()
    yeni_id = max([int(x.get('id', 0)) for x in fisler], default=0) + 1
    fis_no = str(yeni_id).zfill(6)
    toplam_adet, toplam_tutar = fis_toplamlarini_hesapla(temiz_satirlar, gelen_toplam_tutar)
    fis = {
        'id': yeni_id,
        'fis_no': fis_no,
        'tarih': gelen_tarih,
        'musteri': musteri,
        'aciklama': aciklama_genel,
        'toplam_adet': toplam_adet,
        'toplam_tutar': toplam_tutar,
        'stok_yansit': stok_yansit,
        'satirlar': temiz_satirlar
    }
    fisler.append(fis)
    satis_yaz(fisler)
    if stok_yansit:
        for satir in temiz_satirlar:
            try:
                stok_id = int(satir.get('stok_id', 0))
            except Exception:
                stok_id = 0
            stok_kayit = next((x for x in stoklar if int(x.get('id', 0)) == stok_id), None)
            if stok_kayit:
                sonra = int(stok_kayit.get('miktar', 0) or 0)
                adet = int(satir.get('adet', 0) or 0)
                hareket_ekle(stok_id, 'Çıkış', -adet, sonra + adet, sonra, musteri=musteri, aciklama=satir.get('aciklama', '') or aciklama_genel, fis_id=yeni_id, fis_no=fis_no, stok_kayit=stok_kayit)
    log_yaz(f"Çıkış fişi kaydedildi: Fiş No={fis_no}, Toplam Adet={toplam_adet}, Stok Yansıt={stok_yansit}")
    return jsonify({'ok': True, 'fis': fis})


@app.route('/api/cikis-fisi/<int:fis_id>', methods=['PUT'])
def api_cikis_fisi_duzelt(fis_id):
    gelen = request.get_json(force=True)
    fisler = satis_oku()
    fis = next((x for x in fisler if int(x.get('id', 0)) == fis_id), None)
    if not fis:
        return jsonify({'ok': False, 'hata': 'Fiş bulunamadı.'}), 404

    yeni_satirlar = fis_satirlarini_temizle(gelen.get('satirlar', []))
    if not yeni_satirlar:
        return jsonify({'ok': False, 'hata': 'Kaydedilecek satış satırı yok.'}), 400

    eski_stok_yansit = bool(fis.get('stok_yansit', True))
    yeni_stok_yansit = bool(gelen.get('stok_yansit', True))
    stoklar = stok_oku()
    yedek_al('cikis_fisi_duzeltme_oncesi')

    # Önce eski fiş stoğa yansıtıldıysa stokları geri ver.
    if eski_stok_yansit:
        for satir in fis.get('satirlar', []):
            ok, sonuc = stok_miktar_degistir(stoklar, satir.get('stok_id'), int(satir.get('adet', 0) or 0))
            if not ok:
                return jsonify({'ok': False, 'hata': sonuc}), 400
            if sonuc:
                sonra = int(sonuc.get('miktar', 0) or 0)
                adet = int(satir.get('adet', 0) or 0)
                hareket_ekle(satir.get('stok_id'), 'Fiş Düzeltme İade', adet, sonra - adet, sonra, musteri=fis.get('musteri', ''), aciklama='Düzenleme öncesi eski fiş stoğa geri işlendi', fis_id=fis_id, fis_no=fis.get('fis_no', ''), stok_kayit=sonuc)

    # Sonra yeni fiş stoğa yansıtılacaksa yeni miktarı düş.
    if yeni_stok_yansit:
        for satir in yeni_satirlar:
            ok, sonuc = stok_miktar_degistir(stoklar, satir.get('stok_id'), -int(satir.get('adet', 0) or 0))
            if not ok:
                return jsonify({'ok': False, 'hata': sonuc}), 400
            if sonuc:
                sonra = int(sonuc.get('miktar', 0) or 0)
                adet = int(satir.get('adet', 0) or 0)
                hareket_ekle(satir.get('stok_id'), 'Çıkış', -adet, sonra + adet, sonra, musteri=str(gelen.get('musteri', fis.get('musteri', ''))).strip(), aciklama=satir.get('aciklama', '') or str(gelen.get('aciklama', fis.get('aciklama', ''))).strip(), fis_id=fis_id, fis_no=fis.get('fis_no', ''), stok_kayit=sonuc)

    stok_yaz(stoklar)

    toplam_adet, toplam_tutar = fis_toplamlarini_hesapla(yeni_satirlar, gelen.get('toplam_tutar', None))
    fis.update({
        'tarih': tarih_formatla(gelen.get('tarih', fis.get('tarih', ''))),
        'musteri': str(gelen.get('musteri', fis.get('musteri', ''))).strip(),
        'aciklama': str(gelen.get('aciklama', fis.get('aciklama', ''))).strip(),
        'toplam_adet': toplam_adet,
        'toplam_tutar': toplam_tutar,
        'stok_yansit': yeni_stok_yansit,
        'satirlar': yeni_satirlar
    })
    satis_yaz(fisler)
    log_yaz(f"Çıkış fişi düzeltildi: Fiş No={fis.get('fis_no')}, Stok Yansıt={yeni_stok_yansit}")
    return jsonify({'ok': True, 'fis': fis})


@app.route('/api/cikis-fisi/<int:fis_id>', methods=['DELETE'])
def api_cikis_fisi_sil(fis_id):
    fisler = satis_oku()
    fis = next((x for x in fisler if int(x.get('id', 0)) == fis_id), None)
    if not fis:
        return jsonify({'ok': False, 'hata': 'Fiş bulunamadı.'}), 404

    yedek_al('cikis_fisi_silme_oncesi')
    if bool(fis.get('stok_yansit', True)):
        stoklar = stok_oku()
        for satir in fis.get('satirlar', []):
            ok, sonuc = stok_miktar_degistir(stoklar, satir.get('stok_id'), int(satir.get('adet', 0) or 0))
            if not ok:
                return jsonify({'ok': False, 'hata': sonuc}), 400
            if sonuc:
                sonra = int(sonuc.get('miktar', 0) or 0)
                adet = int(satir.get('adet', 0) or 0)
                hareket_ekle(satir.get('stok_id'), 'Fiş Silme İade', adet, sonra - adet, sonra, musteri=fis.get('musteri', ''), aciklama='Çıkış fişi silindi, stok geri işlendi', fis_id=fis_id, fis_no=fis.get('fis_no', ''), stok_kayit=sonuc)
        stok_yaz(stoklar)

    yeni_fisler = [x for x in fisler if int(x.get('id', 0)) != fis_id]
    satis_yaz(yeni_fisler)
    log_yaz(f"Çıkış fişi silindi: Fiş No={fis.get('fis_no')}")
    return jsonify({'ok': True})


if __name__ == '__main__':
    threading.Thread(target=gunluk_yedek_motoru, daemon=True).start()
    app.run(debug=os.environ.get('FLASK_DEBUG', '0') == '1', host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))

"""Urun Gorseli Bul - ekip icin web uygulamasi.

Marka ve urun kodu girilir; urunun gectigi siteler bulunur, galerileri
cikarilir ve gorseller ekranda gosterilir.

Erisim ortak bir sifre ile korunur. Sifre kodun icinde degil, Streamlit'in
"Secrets" bolumunde saklanir.
"""

import hashlib
import hmac
import io
import json
import re
import time
import zipfile
from urllib.parse import urljoin, urlparse

import requests
import streamlit as st
from bs4 import BeautifulSoup
from ddgs import DDGS
from PIL import Image

st.set_page_config(page_title="Ürün Görseli Bul", page_icon="🔎", layout="wide")


# ----------------------------------------------------------------------------
# Sifre kapisi
# ----------------------------------------------------------------------------
def girisi_kontrol_et() -> bool:
    """Dogru sifre girilmeden uygulamanin geri kalani calismaz."""
    if st.session_state.get("giris_yapildi"):
        return True

    st.title("Ürün Görseli Bul")
    st.caption("Devam etmek için ekip şifresini girin.")

    with st.form("giris"):
        sifre = st.text_input("Şifre", type="password")
        gonder = st.form_submit_button("Giriş")

    if gonder:
        dogru = st.secrets.get("sifre", "")
        if not dogru:
            st.error("Uygulama şifresi tanımlanmamış. "
                     "Streamlit → Settings → Secrets bölümüne şifreyi ekleyin.")
            return False
        # Zamanlama saldirilarina karsi sabit sureli karsilastirma
        if hmac.compare_digest(sifre, dogru):
            st.session_state["giris_yapildi"] = True
            st.rerun()
        else:
            st.error("Şifre yanlış.")

    return False


if not girisi_kontrol_et():
    st.stop()


# ----------------------------------------------------------------------------
# Ayarlar
# ----------------------------------------------------------------------------
BASLIK = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
}

GURULTU = ("logo", "icon", "sprite", "banner", "placeholder", "avatar", "pixel",
           "spacer", "loading", "blank", "payment", "visa", "mastercard", "flag",
           "instagram", "facebook", "whatsapp", "trustpilot", "kargo", "cargo",
           "favicon")

ATLANACAK_ALAN = (
    # Sosyal medya - urun fotografi degil, paylasim var
    "youtube.", "facebook.", "instagram.", "pinterest.", "tiktok.",
    "twitter.", "x.com", "reddit.", "aliexpress.",
    # Ansiklopedi, veri ve kurum siteleri. Uzun alfanumerik kodlarda arama
    # motoru birebir eslesme bulamayinca bu tur sayfalari getiriyor.
    "wikipedia.", "wikimedia.", ".gov", ".edu", "ac.uk", "ftp.", "ncbi.",
    "ebi.ac.uk", "nasa.gov", "archive.org", "scribd.", "slideshare.",
)

UZANTI_TAMAM = (".jpg", ".jpeg", ".png", ".webp")


def alan_adi(adres):
    """Adresten alan adini cikarir. www. onekini atar."""
    bulunan = re.findall(r"https?://([^/]+)", adres or "")
    return re.sub(r"^www\.", "", bulunan[0]).lower() if bulunan else ""


def alan_yasakli(adres):
    """Bu adres kara listedeki bir siteye mi ait?"""
    alan = alan_adi(adres)
    return (not alan) or any(k in alan for k in ATLANACAK_ALAN)


# ----------------------------------------------------------------------------
# Gorsel cikarma
# ----------------------------------------------------------------------------
def _mutlak(sayfa, url):
    if not url:
        return ""
    url = url.strip()
    if url.startswith("//"):
        return "https:" + url
    if url.startswith(("http://", "https://")):
        return url
    if url.startswith("data:"):
        return ""
    return urljoin(sayfa, url)


def _srcset_en_buyuk(deger):
    en_iyi, en_genis = "", -1
    for parca in deger.split(","):
        parca = parca.strip().split()
        if not parca:
            continue
        genislik = -1
        if len(parca) > 1 and parca[1].endswith("w"):
            try:
                genislik = int(parca[1][:-1])
            except ValueError:
                genislik = -1
        if genislik > en_genis:
            en_iyi, en_genis = parca[0], genislik
    return en_iyi


def _buyut(url):
    """Kucuk onizleme adreslerini buyuk surume cevirir."""
    url = re.sub(r"([?&]width=)\d+", r"\g<1>1600", url)
    url = re.sub(r"([?&]w=)\d{1,3}\b", r"\g<1>1600", url)
    url = re.sub(r"_\d{2,4}x(\d{2,4})?(?=\.(jpg|jpeg|png|webp))", "", url, flags=re.I)
    return url


def _sadelestir(metin):
    """Karsilastirma icin harf ve rakam disindaki her seyi atar."""
    return re.sub(r"[^a-z0-9]", "", (metin or "").lower())


def kodu_ayikla(sorgu):
    """Sorgu icindeki urun kodunu tahmin eder: rakam iceren en uzun parca."""
    adaylar = [p for p in sorgu.split() if any(k.isdigit() for k in p)]
    return max(adaylar, key=len) if adaylar else ""


def kod_govdesi(kod):
    """Koda yapisik yazilmis renk adini ayirir; yoksa bos doner.

    Bazi markalarda Excel'deki "Orijinal Kod" aslinda iki parcadir ve
    bitisik yazilmistir. American Vintage ornegi:
        MYKO02CE26ORTIE  ->  MYKO02CE26 (model) + ORTIE (renk)
    Markanin kendi adresinde bu ikisi tire ile ayrilir:
        .../EAST18FH23-ORAFLU.html
    Bitisik hali internette hicbir yerde gecmedigi icin arama sifir sonuc
    veriyor. Bu yuzden sondaki 4+ harflik ekin atilmis halini de deniyoruz.

    Sadece "rakam iceren govde + sonda sirf harf" kaliplarinda calisir:
        MYKO02CE26ORTIE -> MYKO02CE26
        910B8224NSZ     -> ""  (ek 3 harf, kisa - dokunmuyoruz)
        PMAA10BS26JER0020305 / ID1481 / 212481-410 -> ""  (rakamla bitiyor)
    """
    sade = _sadelestir(kod)
    esles = re.match(r"^(.*\d.*?)([a-z]{4,})$", sade)
    if not esles:
        return ""
    govde = esles.group(1)
    # Govde hem yeterince uzun hem rakam icermeli, yoksa ayirmak riskli
    if len(govde) < 6 or not any(k.isdigit() for k in govde):
        return ""
    return govde


def sayfayi_dogrula(html, kod, marka=""):
    """Sayfa gercekten bu urune mi ait?

    'tam'   - kodun tamami geciyor
    'kismi' - kodun buyuk bolumu geciyor (site farkli bolmus olabilir)
    'zayif' - kodun govdesi geciyor; ayni model, rengi farkli olabilir
    'marka' - koddan iz yok ama marka adi geciyor
    'yok'   - ne kod ne marka geciyor. Sayfanin urunle alakasi yok, kullanilmaz.
    """
    if not kod:
        return "kismi"
    sade_sayfa = _sadelestir(html)
    sade_kod = _sadelestir(kod)
    if not sade_kod:
        return "kismi"

    # 1) Kodun tamami
    if sade_kod in sade_sayfa:
        return "tam"

    uzunluk = len(sade_kod)

    # 2) Uzun on ek. Siteler kodu farkli bolebiliyor
    #    (PMGB02CS26FAB001-0810 / PMGB02CS26FAB001 0810). %80'lik on ek ayni
    #    urunu yakalar ama baska bir rengi (...FAB002) yakalamaz.
    uzun_ek = max(8, int(uzunluk * 0.8))
    if uzunluk > uzun_ek and sade_kod[:uzun_ek] in sade_sayfa:
        return "kismi"

    # 3) Sayisal govde (212481-410 -> 212481). Renk ayri alanda yazilmis olabilir.
    govde = max(re.findall(r"\d{4,}", kod), key=len, default="")
    if govde and govde in sade_sayfa:
        return "kismi"

    # 3b) Koda yapisik yazilmis renk adi (MYKO02CE26ORTIE -> MYKO02CE26).
    #     Site model kodunu yaziyor, rengi ayri alanda tutuyor demektir.
    renk_ayrilmis = kod_govdesi(kod)
    if renk_ayrilmis and renk_ayrilmis in sade_sayfa:
        return "kismi"

    # 4) Kisa govde: ayni model ailesi, rengi farkli olabilir
    kisa_ek = max(6, int(uzunluk * 0.55))
    if uzunluk > kisa_ek and sade_kod[:kisa_ek] in sade_sayfa:
        return "zayif"

    # 5) Koddan hicbir iz yok. Hic olmazsa marka adi geciyor mu?
    #    Gecmiyorsa bu sayfanin urunle alakasi yoktur (arama motoru uzun
    #    alfanumerik kodlarda alakasiz sayfalar getirebiliyor).
    sade_marka = _sadelestir(marka)
    if sade_marka and len(sade_marka) >= 3 and sade_marka in sade_sayfa:
        return "marka"

    return "yok"


def _json_ld_urunler(corba):
    """Sayfadaki yapisal veriden Product nesnelerini toplar."""
    bulunanlar = []

    def gez(dugum):
        if isinstance(dugum, dict):
            tur = dugum.get("@type", "")
            turler = tur if isinstance(tur, list) else [tur]
            if any("product" in str(t).lower() for t in turler):
                bulunanlar.append(dugum)
            for deger in dugum.values():
                gez(deger)
        elif isinstance(dugum, list):
            for oge in dugum:
                gez(oge)

    for betik in corba.find_all("script", type="application/ld+json"):
        try:
            gez(json.loads(betik.get_text() or "{}"))
        except Exception:
            continue
    return bulunanlar


def _metin(deger):
    """JSON-LD alanlari bazen metin, bazen nesne, bazen liste olarak gelir."""
    if deger is None:
        return ""
    if isinstance(deger, str):
        return deger.strip()
    if isinstance(deger, (int, float)):
        return str(deger)
    if isinstance(deger, list):
        return ", ".join(x for x in (_metin(d) for d in deger) if x)
    if isinstance(deger, dict):
        for anahtar in ("name", "value", "@value", "title"):
            if deger.get(anahtar):
                return _metin(deger[anahtar])
    return ""


def _sozluk(deger):
    """Sozluk bekledigimiz yerde liste ya da metin gelebiliyor.

    Sitelerin yapisal verisi standart degil: "offers" ve "priceSpecification"
    kimi sayfada tek nesne, kimi sayfada liste. Liste geldiginde ilk sozlugu
    aliyoruz; hicbiri sozluk degilse bos sozluk donuyor ki .get() patlamasin.
    """
    if isinstance(deger, dict):
        return deger
    if isinstance(deger, list):
        for oge in deger:
            if isinstance(oge, dict):
                return oge
    return {}


def _liste(deger):
    """Liste bekledigimiz yerde tek nesne gelebiliyor."""
    if isinstance(deger, list):
        return deger
    if deger is None:
        return []
    return [deger]


def sayfa_bilgileri(corba, sayfa_url):
    """Sayfadan urun adi, marka, renk gibi dogrulanabilir bilgileri cikarir.

    Ekip sadece gorsele bakip karar vermesin diye; gorselin yaninda urunun
    adi ve rengi de gorunsun.
    """
    bilgi = {"ad": "", "marka": "", "renk": "", "kod": "",
             "aciklama": "", "ozellikler": []}

    for urun in _json_ld_urunler(corba):
        bilgi["ad"] = bilgi["ad"] or _metin(urun.get("name"))
        bilgi["marka"] = bilgi["marka"] or _metin(urun.get("brand"))
        bilgi["renk"] = bilgi["renk"] or _metin(urun.get("color"))
        bilgi["kod"] = bilgi["kod"] or _metin(urun.get("sku") or urun.get("mpn"))
        bilgi["aciklama"] = bilgi["aciklama"] or _metin(urun.get("description"))

        # Ek ozellikler (beden, materyal, desen gibi)
        for ozellik in _liste(urun.get("additionalProperty"))[:6]:
            if isinstance(ozellik, dict):
                ad = _metin(ozellik.get("name"))
                deger = _metin(ozellik.get("value"))
                if ad and deger:
                    bilgi["ozellikler"].append(f"{ad}: {deger}")

    # Yapisal veri yoksa sayfanin kendi basliklarina dusuyoruz
    if not bilgi["ad"]:
        for etiket in corba.find_all("meta"):
            if etiket.get("property") == "og:title":
                bilgi["ad"] = (etiket.get("content") or "").strip()
                break
    if not bilgi["ad"] and corba.find("h1"):
        bilgi["ad"] = corba.find("h1").get_text(" ", strip=True)
    if not bilgi["ad"] and corba.title:
        bilgi["ad"] = corba.title.get_text(strip=True)

    if not bilgi["aciklama"]:
        for etiket in corba.find_all("meta"):
            if etiket.get("property") == "og:description" or \
               etiket.get("name") == "description":
                bilgi["aciklama"] = (etiket.get("content") or "").strip()
                break

    # Renk sayfada "Renk: Lacivert" gibi yazili olabilir
    if not bilgi["renk"]:
        metin = corba.get_text(" ", strip=True)[:6000]
        eslesme = re.search(r"\b(?:renk|color|colour|colore)\s*[:\-]\s*"
                            r"([A-Za-zÇĞİÖŞÜçğıöşü/ ]{3,30})", metin, re.I)
        if eslesme:
            bilgi["renk"] = eslesme.group(1).strip(" -/")

    for anahtar in ("ad", "marka", "renk", "kod", "aciklama"):
        bilgi[anahtar] = re.sub(r"\s+", " ", bilgi[anahtar])[:300]
    return bilgi


def sayfa_gorselleri(sayfa_url, oturum, kod="", marka=""):
    """Sayfadaki gorselleri, kod dogrulamasini ve urun bilgilerini dondurur."""
    try:
        cevap = oturum.get(sayfa_url, timeout=15)
        cevap.raise_for_status()
    except Exception:
        return [], "yok", {}
    if "text/html" not in cevap.headers.get("Content-Type", ""):
        return [], "yok", {}

    # Sayfa ADRESINI de dogrulamaya katiyoruz. Bircok magaza urun kodunu
    # adrese koyuyor (.../MFAZ02CE26-LIPAVI.html) ama sayfa metnine yazmiyor
    # ya da JavaScript ile sonradan basiyor - o zaman biz goremiyoruz ve
    # markanin KENDI urun sayfasi bile "kod yok" diye eleniyordu.
    # Yonlendirme olmussa gercek adresi kullaniyoruz.
    _gercek_adres = getattr(cevap, "url", "") or sayfa_url
    dogrulama = sayfayi_dogrula(_gercek_adres + " " + cevap.text, kod, marka)
    corba = BeautifulSoup(cevap.text, "html.parser")
    # Bilgi cikarimi sayfanin yapisina bagli; beklenmedik bir bicim gelirse
    # gorselleri kaybetmemek icin sadece bilgiyi bos gecip devam ediyoruz.
    try:
        bilgi = sayfa_bilgileri(corba, sayfa_url)
    except Exception:
        bilgi = {}
    adaylar, gorulen = [], set()

    def ekle(ham):
        url = _buyut(_mutlak(sayfa_url, ham))
        if not url or url in gorulen:
            return
        if any(k in url.lower() for k in GURULTU):
            return
        yol = urlparse(url).path.lower()
        if not (yol.endswith(UZANTI_TAMAM) or "/cdn/" in url or "image" in yol):
            return
        gorulen.add(url)
        adaylar.append(url)

    for etiket in corba.find_all("meta"):
        if etiket.get("property") in ("og:image", "og:image:secure_url") or \
           etiket.get("name") == "twitter:image":
            ekle(etiket.get("content", ""))

    # Sayfanin yapisal verisi: gorseller tek adres ya da dizi olarak yazilabilir
    for betik in corba.find_all("script", type="application/ld+json"):
        metin = betik.get_text() or ""
        for parca in re.findall(r'"([^"\s]+\.(?:jpg|jpeg|png|webp)[^"\s]*)"', metin, re.I):
            ekle(parca.replace("\\/", "/"))

    for resim in corba.find_all("img"):
        if resim.get("srcset"):
            ekle(_srcset_en_buyuk(resim["srcset"]))
        for alan in ("data-zoom-image", "data-large-image", "data-src",
                     "data-original", "data-image", "src"):
            if resim.get(alan):
                ekle(resim[alan])
                break

    return adaylar, dogrulama, bilgi


def gorsel_al(url, oturum, en_az=500, en_fazla_mb=12):
    try:
        cevap = oturum.get(url, timeout=15, stream=True)
        cevap.raise_for_status()
        tur = cevap.headers.get("Content-Type", "")
        if "image" not in tur or "svg" in tur:
            return None
        bayt = b""
        for parca in cevap.iter_content(65536):
            bayt += parca
            if len(bayt) > en_fazla_mb * 1024 * 1024:
                return None
    except Exception:
        return None

    try:
        with Image.open(io.BytesIO(bayt)) as gorsel:
            gen, yuk = gorsel.size
            bicim = (gorsel.format or "JPEG").lower()
    except Exception:
        return None

    if gen < en_az or yuk < en_az:
        return None
    oran = gen / yuk
    if oran > 2.4 or oran < 0.35:          # afis / serit gorseller
        return None

    uzanti = {"jpeg": ".jpg", "png": ".png", "webp": ".webp"}.get(bicim, ".jpg")
    return bayt, gen, yuk, uzanti


# ----------------------------------------------------------------------------
# Arama
# ----------------------------------------------------------------------------
def onizleme_yap(bayt, kenar=520):
    """Izgaranin duzgun hizalanmasi icin kare, dolgulu onizleme uretir.

    Gorsellerin en-boy oranlari farkli oldugundan (1024x1024, 1200x1845...)
    olduklari gibi basilinca satirlar kayiyor ve dugmeler hizasiz duruyor.
    Indirilen dosya orijinal kalir; sadece ekranda gosterilen kucultulur.
    """
    try:
        with Image.open(io.BytesIO(bayt)) as gorsel:
            kucuk = gorsel.convert("RGB")
            kucuk.thumbnail((kenar, kenar), Image.LANCZOS)
            tuval = Image.new("RGB", (kenar, kenar), (255, 255, 255))
            tuval.paste(kucuk, ((kenar - kucuk.width) // 2,
                                (kenar - kucuk.height) // 2))
            cikti = io.BytesIO()
            tuval.save(cikti, format="JPEG", quality=88)
            return cikti.getvalue()
    except Exception:
        return bayt


def _gizli(anahtar):
    """Streamlit Secrets'tan deger okur; tanimli degilse bos doner."""
    try:
        return str(st.secrets.get(anahtar, "") or "").strip()
    except Exception:
        return ""


def arama_motoru():
    """Hangi arama servisi kullanilacak? Iyi olandan kotuye dogru."""
    if _gizli("serper_api_key"):
        return "serper"
    if _gizli("google_api_key") and _gizli("google_cx"):
        return "google_cse"
    return "duckduckgo"


def serper_gorsel_ara(sorgu, adet=10):
    """Serper uzerinden Google Gorseller'de arar.

    Google'in kendi sonuclarini verir ama Programmable Search'un 50 alan adi
    sinirina takilmaz. Tek anahtar yeter, arama motoru kurmaya gerek yok.

    KREDI: Serper 10 sonuca kadar 1 kredi, 11-100 sonuc icin 2 kredi aliyor.
    Bu yuzden hep 10 istiyoruz - urun basina 1 kredi. Zaten gorsellerin
    cogunu bu sayfalarin galerilerinden topluyoruz, 10 aday fazlasiyla yeter.

    Doner: [{"gorsel": ..., "sayfa": ..., "baslik": ...}]
    """
    anahtar = _gizli("serper_api_key")
    if not anahtar:
        return []
    try:
        cevap = requests.post(
            "https://google.serper.dev/images",
            headers={"X-API-KEY": anahtar, "Content-Type": "application/json"},
            json={"q": sorgu, "num": 10},          # 10 = 1 kredi
            timeout=25,
        )
        if cevap.status_code != 200:
            return []
        veri = cevap.json()
    except Exception:
        return []

    bulunanlar = []
    for oge in (veri.get("images") or [])[:adet]:
        gorsel = oge.get("imageUrl") or ""
        if not gorsel:
            continue
        bulunanlar.append({
            "gorsel": gorsel,
            "sayfa": oge.get("link") or "",
            "baslik": (oge.get("title") or "")[:200],
        })
    return bulunanlar


def google_sonucunu_dogrula(oge, kod):
    """Google sonucunun gercekten bu kodla ilgisi var mi?

    Sayfa metnini indirmeden bakabilecegimiz uc yer var: gorselin adresi,
    sayfanin adresi ve baslik. Perakendeciler urun kodunu cogu zaman dosya
    adina ya da adrese koyuyor. Buralarda kod geciyorsa saglam eslesmedir.
    """
    if not kod:
        return "google"
    havuz = _sadelestir(" ".join([
        oge.get("gorsel", ""), oge.get("sayfa", ""), oge.get("baslik", "")]))
    sade_kod = _sadelestir(kod)
    if not sade_kod:
        return "google"
    if sade_kod in havuz:
        return "google"
    uzun_ek = max(8, int(len(sade_kod) * 0.8))
    if len(sade_kod) > uzun_ek and sade_kod[:uzun_ek] in havuz:
        return "google"
    # Koda yapisik renk adi varsa modelsiz hali de sayilir
    # (MYKO02CE26ORTIE -> .../MYKO02CE26-ORTIE.html)
    govde = kod_govdesi(kod)
    if govde and govde in havuz:
        return "google"
    return "google_zayif"


def marka_izi_var(oge, marka):
    """Sonucun adresinde ya da basliginda marka adindan bir iz var mi?

    Kisa ve genel kodlarda ("A-129-S56") Google birebir eslesme bulamayinca
    icinde ayni rakamlar gecen bambaska bir urunu getirebiliyor - mesela bir
    Audi yedek parcasini (06E129629Q). Sirasina guvenip aldigimiz sonuclarda
    en azindan marka adinin gecmesini sart kosuyoruz. Marka bilinmiyorsa bu
    kontrol atlanir.
    """
    if not marka:
        return True
    havuz = _sadelestir(" ".join([
        oge.get("gorsel", ""), oge.get("sayfa", ""), oge.get("baslik", "")]))
    # "Palm Angels" -> palm / angels. Parcalardan biri gecerse yeter; siteler
    # markayi bitisik, ayri ya da kisaltarak yazabiliyor.
    parcalar = [_sadelestir(p) for p in re.split(r"[\s/&,._-]+", marka) if p]
    parcalar = [p for p in parcalar if len(p) >= 3]
    if not parcalar:
        return True
    return any(p in havuz for p in parcalar)


def sonuc_puani(sonuclar, kod, marka, ad=""):
    """Arama sonuclari bu urune ne kadar uyuyor?

    Google bos donmese bile alakasiz seyler dondurebiliyor, o yuzden
    "sonuc geldi mi" yetmez; "gelen sonuclar tutuyor mu" diye bakiyoruz.
      kod adreste/baslikta geciyor : 3 puan (saglam)
      sadece marka geciyor         : 1 puan (idare eder)
    Puan yetersizse sorguyu zenginlestirip (renk, model adi) tekrar ariyoruz.
    """
    if not sonuclar:
        return 0
    if not kod and not marka:
        return YETERLI_PUAN          # olcecek bir sey yok, geleni kabul et
    puan = 0
    # Kod puani: bu sonuclar gercekten BU urune mi ait?
    puan = kod_eslesme_puani(sonuclar, kod)

    # Marka puani TAVANLI. Tavan olmadan markanin 10 koleksiyon sayfasi
    # (10 puan) tek gercek kod eslesmesini (6 puan) yeniyor ve yanlis deneme
    # kazanan secilliyordu.
    if marka:
        puan += min(sum(1 for oge in sonuclar[:10] if marka_izi_var(oge, marka)),
                    MARKA_PUAN_TAVANI)

    # Model adi / resmi magaza sinyali. Kod tutmadiginda hangi denemenin
    # dogru yeri buldugunu ayirt eden sey bu.
    puan += ad_eslesme_puani(sonuclar, ad, marka)
    return puan


def kod_eslesme_puani(sonuclar, kod):
    """Yalnizca URUN KODU eslesmelerinden gelen puan.

    sonuc_puani marka eslesmelerini de sayiyor - hangi denemenin daha iyi
    oldugunu secmek icin bu dogru. Ama "yeterince eminim, aramayi birakayim"
    kararini marka eslesmesiyle vermek yanlis: markanin koleksiyon sayfasi
    ("Fall/Winter 2026 | American Vintage") 10 sonucun 10'unda cikip puani
    doldurabiliyor, biz de dogru urunu buldugumuzu sanip duruyoruz. Halbuki
    o sayfalarda urun yok. Aramayi ancak KOD tuttugunda birakiyoruz.
    """
    if not kod or not sonuclar:
        return 0
    sade_kod = _sadelestir(kod)
    puan = 0
    for oge in sonuclar[:10]:
        havuz = _sadelestir(" ".join([
            oge.get("gorsel", ""), oge.get("sayfa", ""), oge.get("baslik", "")]))
        if sade_kod and sade_kod in havuz:
            puan += YETERLI_PUAN
        elif google_sonucunu_dogrula(oge, kod) == "google":
            puan += 3
    return puan


# Iki saglam eslesme (2x3) ya da dengi. Bu puana ulasinca durup krediyi
# harciyoruz; ulasamazsak sorguyu zenginlestirip bir daha deniyoruz.
YETERLI_PUAN = 6
EN_FAZLA_DENEME = 3
# Marka eslesmelerinin toplayabilecegi en yuksek puan. Yeterli puanin
# altinda kalmali ki marka tek basina "buldum" dedirtemesin.
MARKA_PUAN_TAVANI = 3
# Model adi / resmi magaza sinyalinin toplayabilecegi en yuksek puan.
AD_PUAN_TAVANI = 4

# Model adindaki ayirt edici OLMAYAN kelimeler. "Marisol U SWIMSUIT" icinde
# ayirt edici olan "marisol"; "swimsuit" her mayoda geciyor, eslesirse yanlis
# yonlendirir (alakasiz bir "Black Swimsuit" sonucu puan kazanirdi).
GENEL_KELIMELER = {
    # giyim turleri
    "mayo", "bikini", "swimsuit", "swimwear", "tshirt", "shirt", "gomlek",
    "elbise", "dress", "pantolon", "pants", "jean", "denim", "ceket",
    "jacket", "kazak", "sweater", "jumper", "sweatshirt", "hoodie", "canta",
    "bag", "backpack", "ayakkabi", "shoes", "sneaker", "terlik", "sandalet",
    "bornoz", "sort", "shorts", "etek", "skirt", "bluz", "tunik", "takim",
    "takimi", "yaka", "kollu", "clog", "coat", "mont", "parka", "trench",
    # cinsiyet / genel
    "kadin", "erkek", "unisex", "women", "womens", "woman", "mens", "kids",
    "cocuk", "bebek", "child", "girls", "boys",
    # renkler
    "siyah", "beyaz", "mavi", "yesil", "kirmizi", "sari", "lacivert", "gri",
    "pembe", "mor", "kahverengi", "bej", "black", "white", "blue", "green",
    "red", "yellow", "navy", "grey", "gray", "pink", "brown", "beige",
}


def ad_eslesme_puani(sonuclar, ad, marka):
    """Model adi ve "markanin kendi magazasi" sinyalinden gelen puan.

    Kisa ve genel kodlarda ("A-129-7") kod eslesmesi ise yaramiyor: Google
    icinde 129 gecen Kuran ayetini ya da ucak kuyruk numarasini getirebiliyor.
    Boyle durumlarda iki saglam ipucu kaliyor:

      1. Model adinin ayirt edici kelimesi baslikta geciyor mu?
         ("Marisol U Yaka Mayo" -> marisol tutuyor)
      2. Sonucun alan adi markanin kendi magazasi mi? (ayjeshop.com)

    Bu puan yalnizca HANGI DENEMENIN daha iyi oldugunu secerken kullanilir;
    "aramayi birak" karari hala sadece koda bakar.
    """
    if not sonuclar:
        return 0
    kelimeler = [_sadelestir(p) for p in re.split(r"[\s/&,._-]+", ad or "") if p]
    kelimeler = [p for p in kelimeler
                 if len(p) >= 4 and p not in GENEL_KELIMELER]
    sade_marka = _sadelestir(marka)
    puan = 0
    for oge in sonuclar[:10]:
        baslik = _sadelestir(oge.get("baslik", ""))
        alan = _sadelestir(alan_adi(oge.get("sayfa", "")))
        if kelimeler and any(k in baslik for k in kelimeler):
            puan += 2
        if sade_marka and len(sade_marka) >= 3 and sade_marka in alan:
            puan += 2
    return min(puan, AD_PUAN_TAVANI)


def google_var_mi():
    """Gercek bir gorsel arama servisi tanimli mi?"""
    return arama_motoru() != "duckduckgo"


# Ayni sorgu tekrar sorulursa krediye dokunmasin diye sayac + onbellek.
# Katiligi degistirip ayni listeyi yeniden calistirmak bedava olmali.
_ARAMA_SAYACI = {"toplam": 0}


@st.cache_data(show_spinner=False, ttl=7 * 24 * 3600, max_entries=5000)
def _arama_onbellekli(sorgu, adet, motor):
    """Gercek arama. Sadece onbellekte YOKSA calisir, yani kredi harcar."""
    _ARAMA_SAYACI["toplam"] += 1
    if motor == "serper":
        return serper_gorsel_ara(sorgu, adet)
    if motor == "google_cse":
        return google_gorsel_ara(sorgu, adet)
    return []


def gorsel_arama_yap(sorgu, adet=10):
    """Tanimli servise gore gorsel aramasi yapar (onbellekli)."""
    return _arama_onbellekli(sorgu, adet, arama_motoru())


def google_gorsel_ara(sorgu, adet=20):
    """Google Gorseller'de arar.

    Uzun moda kodlarinda (PMGB02CS26FAB0010810) DuckDuckGo cogu zaman
    alakasiz sonuc veriyor; Google bu kodlari perakendeci sayfalarindan
    indekslemis oluyor. Anahtar tanimliysa once buraya soruyoruz.

    Doner: [{"gorsel": ..., "sayfa": ..., "baslik": ...}]
    """
    if not google_var_mi():
        return []
    anahtar = st.secrets["google_api_key"]
    kimlik = st.secrets["google_cx"]

    bulunanlar, alinan = [], 0
    while alinan < adet:
        parca = min(10, adet - alinan)
        try:
            cevap = requests.get(
                "https://www.googleapis.com/customsearch/v1",
                params={"key": anahtar, "cx": kimlik, "q": sorgu,
                        "searchType": "image", "num": parca,
                        "start": alinan + 1},
                timeout=20,
            )
            if cevap.status_code != 200:
                break
            veri = cevap.json()
        except Exception:
            break

        ogeler = veri.get("items") or []
        if not ogeler:
            break
        for oge in ogeler:
            baglam = oge.get("image") or {}
            bulunanlar.append({
                "gorsel": oge.get("link", ""),
                "sayfa": baglam.get("contextLink", ""),
                "baslik": (oge.get("title") or "")[:200],
            })
        alinan += len(ogeler)
        if len(ogeler) < parca:
            break
    return bulunanlar


def _tek_arama(sorgu, adet=40):
    """Tek bir arama denemesi. Kutuphane sonuc bulamayinca hata firlatiyor,
    bu yuzden hatayi bos sonuc gibi ele aliyoruz."""
    try:
        return DDGS().text(sorgu, max_results=adet) or []
    except Exception:
        return []


def ara_kademeli(marka, ad, kod, renk=""):
    """Sonuc bulana kadar sorguyu kademe kademe degistirir.

    Marka ve orijinal kod sabittir; model adi ise sitelere gore degisir
    ("Spray Bear Over Tee" / "Spray Bear T-Shirt" / "Palm Sport Stripes Polo").
    Bu yuzden once marka + kod deniyoruz - hem daha isabetli hem daha hizli.
    Model adi yalnizca digerleri sonuc vermezse son care olarak kullaniliyor.
    """
    def temizle(metin):
        metin = re.sub(r"[/\\|,;]+", " ", metin or "")
        return re.sub(r"\s+", " ", metin).strip()

    marka, ad, kod, renk = (temizle(marka), temizle(ad),
                            temizle(kod), temizle(renk))

    denemeler = []
    if kod:
        # Ayni kod farkli renklerde olabiliyor. Renk varsa once onu ekliyoruz,
        # yoksa iki satir birebir ayni sonucu getirir.
        if renk:
            denemeler.append(f'{marka} "{kod}" {renk}'.strip())
        if marka:
            denemeler.append(f'{marka} "{kod}"')      # 1. asil yol
        denemeler.append(f'"{kod}"')                  # 2. sadece kod, birebir
        denemeler.append(kod)                         # 3. sadece kod, serbest
        if ad:                                        # 4. son care: model adi
            denemeler.append(temizle(f"{marka} {ad} {kod}"))
    else:
        denemeler.append(temizle(f"{marka} {ad}"))

    gorulen = set()
    for deneme in denemeler:
        if not deneme or deneme in gorulen:
            continue
        gorulen.add(deneme)
        sonuclar = _tek_arama(deneme)
        if sonuclar:
            return sonuclar, deneme
    return [], ""


def urun_gorselleri(urun, kac_gorsel, kac_site, en_kucuk, oturum, katilik="orta",
                    renk_gerekli=False, teshis=None):
    """Tek bir urun icin gorselleri toplar.

    katilik:
      "siki"  - sadece kodun tamaminin gectigi sayfalar
      "orta"  - kodun tamami ya da govdesi gecen sayfalar (varsayilan)
      "gevsek"- dogrulama yapma, arama ne verdiyse al

    teshis: liste verilirse her adim buraya yazilir. None ise (varsayilan)
    hicbir sey yapilmaz - arama davranisi teshisten etkilenmez.
    """
    def _not(metin):
        if teshis is not None:
            teshis.append(metin)
    kod = (urun.get("kod") or "").strip()
    marka = (urun.get("marka") or "").strip()
    ad = (urun.get("ad") or "").strip()
    renk = (urun.get("renk") or "").strip()
    link = (urun.get("url") or "").strip()
    link_verildi = bool(link)
    kullanilan_sorgu = ""

    # --- Kullanici dogrudan urun linki verdiyse aramaya hic gitmiyoruz
    google_sonuclari = []
    if link_verildi:
        bulunan = re.findall(r"https?://([^/]+)", link)
        alan = re.sub(r"^www\.", "", bulunan[0]) if bulunan else "link"
        aday_sayfalar = [(alan, link)]
        kod = ""                      # link verilmisse dogrulamaya gerek yok
    else:
        # --- Once Google Gorseller (anahtar tanimliysa)
        #
        # Kademeli sorgu. Once en sade ve en ayirt edici hali deniyoruz:
        # marka + orijinal kod. Kod dunya capinda tek oldugu icin bu cogu
        # urunu tek basina buluyor ve tek kredi harciyor.
        #
        # Sonuclar tutmuyorsa (bkz. sonuc_puani) sorguyu zenginlestiriyoruz:
        # once renk, sonra model adi ekleniyor. Bunu bastan yapmiyoruz cunku
        # "adidas ID1481 Brown Putty Grey Gold Metallic" gibi uzun sorgular
        # Google'da sifir sonuc veriyor - uzun sorgu ise yarar degil zarar.
        #
        # Tek istisna: ayni kod listede birden fazla renkte varsa renk basa
        # aliniyor, yoksa iki satira ayni gorseller gelir.
        _temiz = lambda m: re.sub(r"\s+", " ", re.sub(r"[/\\|,;]+", " ", m or "")).strip()
        _denemeler = []
        if kod:
            if renk_gerekli and renk:
                _denemeler.append(_temiz(f"{marka} {kod} {renk}"))
            _denemeler.append(_temiz(f"{marka} {kod}"))
            # Koda yapisik renk adi varsa ayrilmis hali. Bitisik yazilan kod
            # internette hicbir yerde gecmiyor, ayrilmis hali geciyor.
            _govde = kod_govdesi(kod)
            if _govde:
                _ek = _sadelestir(kod)[len(_govde):]
                _denemeler.append(_temiz(f"{marka} {_govde} {_ek}"))
                _denemeler.append(_temiz(f"{marka} {_govde}"))
            if renk:
                _denemeler.append(_temiz(f"{marka} {kod} {renk}"))
            if ad:
                _denemeler.append(_temiz(f"{marka} {ad} {renk}"))
            _denemeler.append(_temiz(kod))
        else:
            _denemeler.append(_temiz(f"{marka} {ad} {renk}"))
            _denemeler.append(_temiz(f"{marka} {ad}"))

        google_sorgu, google_sonuclari = "", []
        if google_var_mi():
            _gorulen_deneme, _sayac = set(), 0
            _en_iyi_puan = -1
            for _deneme in _denemeler:
                if not _deneme or _deneme in _gorulen_deneme:
                    continue
                if _sayac >= EN_FAZLA_DENEME:
                    break
                _gorulen_deneme.add(_deneme)
                _sayac += 1
                _bulunan = gorsel_arama_yap(_deneme)
                _puan = sonuc_puani(_bulunan, kod, marka, ad)
                _kod_puan = kod_eslesme_puani(_bulunan, kod)
                _not(f"Sorgu {_sayac}: `{_deneme}` → **{len(_bulunan)} sonuç**, "
                     f"puan {_puan}"
                     + (f" (bunun {_kod_puan}'i koddan)" if kod else "")
                     + f" — durmak için koddan {YETERLI_PUAN} gerek")
                for _s in _bulunan[:5]:
                    _not(f"     · {alan_adi(_s.get('sayfa','')) or '?'} — "
                         f"{(_s.get('baslik') or '')[:70]}")
                if _puan > _en_iyi_puan:
                    _en_iyi_puan = _puan
                    google_sonuclari, google_sorgu = _bulunan, _deneme
                # Durma karari SADECE kod eslesmesine bakar. Marka eslesmesi
                # "bu markanin bir sayfasi" demektir, "bu urun" demek degil.
                if kod:
                    _yeter = _kod_puan >= YETERLI_PUAN
                elif marka:
                    # Kod yoksa marka tek olcutumuz; burada tavan uygulanmaz,
                    # yoksa kodsuz urunler bosuna ikinci kez aranir.
                    _yeter = sum(1 for _o in _bulunan
                                 if marka_izi_var(_o, marka)) >= YETERLI_PUAN
                else:
                    _yeter = bool(_bulunan)
                if _yeter:
                    _not("   → kod doğrulandı, başka sorgu denenmedi")
                    break               # emin olduk, fazladan kredi harcama

        if google_sonuclari:
            _ad = {"serper": "Google Görseller",
                   "google_cse": "Google (sınırlı)"}.get(arama_motoru(), "arama")
            kullanilan_sorgu = f"{_ad}: {google_sorgu}"
            aday_sayfalar, gorulen_alan = [], set()
            for oge in google_sonuclari:
                adres = oge.get("sayfa") or ""
                alan = alan_adi(adres)
                if alan in gorulen_alan:
                    continue
                if alan_yasakli(adres):
                    _not(f"Elendi (kara liste / geçersiz adres): {alan or adres[:40]}")
                    continue
                gorulen_alan.add(alan)
                aday_sayfalar.append((alan, adres))
                if len(aday_sayfalar) >= kac_site * 5:
                    break
            _not(f"Seçilen sorgu: `{google_sorgu}` → gezilecek "
                 f"**{len(aday_sayfalar)} sayfa**")
        else:
            sonuclar, kullanilan_sorgu = ara_kademeli(marka, ad, kod, renk)
            if not sonuclar:
                return [], [], ("arama hiçbir sonuç vermedi — ürün sayfasının linkini "
                                "doğrudan yapıştırmayı deneyin"), ""

            # Ihtiyactan cok aday topluyoruz: bir kismi dogrulamayi gecemeyecek
            aday_sayfalar, gorulen_alan = [], set()
            for sonuc in sonuclar:
                adres = sonuc.get("href") or sonuc.get("link") or ""
                alan = alan_adi(adres)
                if alan in gorulen_alan or alan_yasakli(adres):
                    continue
                gorulen_alan.add(alan)
                aday_sayfalar.append((alan, adres))
                if len(aday_sayfalar) >= kac_site * 5:
                    break

    if not aday_sayfalar:
        return [], [], "ürün sayfası bulunamadı", kullanilan_sorgu

    # "yok" hicbir modda kabul edilmez: o sayfada ne urun kodu ne marka adi
    # geciyor demektir, urunle alakasi yoktur. Arama motoru uzun kodlarda
    # bambaska sayfalar getirebiliyor; buradan geri donuyoruz.
    KADEMELER = {"siki": {"tam"},
                 "orta": {"tam", "kismi"},
                 "gevsek": {"tam", "kismi", "zayif", "marka"}}
    kabul = KADEMELER[katilik]
    # Linki kullanici verdiyse dogrulamaya gerek yok - sayfayi zaten o secti
    if link_verildi:
        kabul = {"tam", "kismi", "zayif", "marka", "yok"}

    imzalar, kayitlar, elenen = set(), [], []
    yedekler = []          # dogrulamayi gecemeyen ama gorsel iceren sayfalar
    kullanilan_site = 0

    def sayfadan_topla(alan, adres, adaylar, dogrulama, bilgi):
        """Bir sayfanin gorsellerini indirip kayitlara ekler."""
        eklenen = 0
        for gorsel_url in adaylar:
            if len(kayitlar) >= kac_gorsel:
                break
            sonuc = gorsel_al(gorsel_url, oturum, en_az=en_kucuk)
            if not sonuc:
                continue
            bayt, gen, yuk, uzanti = sonuc
            imza = hashlib.sha256(bayt).hexdigest()[:20]
            if imza in imzalar:
                continue
            imzalar.add(imza)
            eklenen += 1
            kayitlar.append({"bayt": bayt, "gen": gen, "yuk": yuk,
                             "alan": alan, "uzanti": uzanti, "kaynak": adres,
                             "dogrulama": "link" if link_verildi else dogrulama,
                             "bilgi": bilgi})
        return eklenen

    for alan, adres in aday_sayfalar:
        # Dogrulamayi gecemeyen sayfa site hakkini harcamaz; siradakine bakariz.
        # Cop sonuclar ilk siralari kaplasa bile gercek urun sayfasina ulasiriz.
        if kullanilan_site >= kac_site or len(kayitlar) >= kac_gorsel:
            break
        adaylar, dogrulama, bilgi = sayfa_gorselleri(adres, oturum, kod, marka)
        _not(f"Sayfa: **{alan}** → {len(adaylar)} görsel adayı, "
             f"doğrulama: `{dogrulama}` "
             f"({'kabul' if dogrulama in kabul else 'RET — ' + katilik + ' modda kabul edilmiyor'})")
        if not adaylar:
            _not("     · sayfadan hiç görsel çıkarılamadı (site engelliyor olabilir)")
        if dogrulama not in kabul:
            elenen.append(alan)
            # Otomatik gevsetme yalnizca kodun izinin bulundugu sayfalara
            # iner. Sadece marka adi gecen sayfa (koleksiyon listesi olabilir)
            # ya da hicbir izi olmayan sayfa yedege alinmaz - kullanici
            # isterse "Gevsek" secerek onlari acikca isteyebilir.
            if adaylar and dogrulama in ("kismi", "zayif") and len(yedekler) < kac_site:
                yedekler.append((alan, adres, adaylar, dogrulama, bilgi))
            continue
        kullanilan_site += 1
        _eklendi = sayfadan_topla(alan, adres, adaylar, dogrulama, bilgi)
        _not(f"     · {_eklendi} görsel alındı "
             f"({len(adaylar) - _eklendi} tanesi {en_kucuk}px altı, "
             f"kopya ya da indirilemedi)")
        time.sleep(0.4)

    # Secilen katilikta hic sonuc cikmadiysa elenenlere geri donuyoruz.
    # Kullaniciyi "ayari gevsetin" diye geri gondermek yerine kendimiz
    # gevsetip sonucu acikca etiketliyoruz.
    if not kayitlar and yedekler:
        sira = {"tam": 0, "kismi": 1, "zayif": 2, "marka": 3}
        for alan, adres, adaylar, dogrulama, bilgi in sorted(
                yedekler, key=lambda y: sira.get(y[3], 9)):
            if len(kayitlar) >= kac_gorsel:
                break
            sayfadan_topla(alan, adres, adaylar, dogrulama, bilgi)

    # Sayfalardan sonuc cikmadiysa Google'in dogrudan eslestirdigi gorselleri
    # kullaniyoruz. Google bu kodu zaten urunle eslestirmis; bu bizim sayfa
    # metni kontrolumuzden daha guclu bir kanit.
    if not kayitlar and google_sonuclari:
        # Google sonuclarini da katiliga gore suzuyoruz. Onceden hepsini
        # oldugu gibi aliyorduk; kod hicbir yerde gecmese bile geliyorlardi,
        # bu yuzden farkli urunler karisiyordu.
        #   siki   : sadece kodu adres/baslikta gecen sonuclar
        #   orta   : onlar + Google'in ilk 4 sonucu (siralamanin basi guvenilir)
        #   gevsek : hepsi
        # Sitelerin cogu urun kodunu adrese koymuyor (Palm Angels koymuyor
        # mesela). O yuzden "kod adreste gecsin" tek sart olamaz - oyle
        # yapinca hicbir sonuc kalmiyor. Asil olcut Google'in siralamasi:
        # ilk sonuclar neredeyse hep dogru urun, kuyruk tarafi kayiyor.
        #   siki   : ilk 2 sonuc + kodu adreste dogrulananlar
        #   orta   : ilk 4 sonuc + dogrulananlar
        #   gevsek : hepsi
        # Kara liste burada da gecerli. Onceden sadece sayfa gezme yolunda
        # bakiliyordu; bu yol Google'in verdigi gorseli dogrudan indirdigi
        # icin aliexpress gibi siteler suzgecten kaciyordu.
        temiz_sonuclar = [o for o in google_sonuclari
                          if not alan_yasakli(o.get("sayfa", ""))]

        esik = {"siki": 2, "orta": 4, "gevsek": 10 ** 6}[katilik]
        _not(f"Sayfalardan görsel çıkmadı — Google'ın görsellerine düşülüyor "
             f"({len(temiz_sonuclar)} aday, {katilik} modda ilk {esik} sıra "
             f"+ kodu doğrulananlar kabul)")
        onaylanan = []
        for sira_no, oge in enumerate(temiz_sonuclar):
            etiket = google_sonucunu_dogrula(oge, kod)
            if etiket == "google":
                onaylanan.append((oge, etiket))
            elif sira_no < esik and marka_izi_var(oge, marka):
                # Kod hicbir yerde gecmiyor; sadece siralamaya guveniyoruz.
                # O zaman en azindan marka adi gecsin.
                onaylanan.append((oge, etiket))

        _basarisiz = 0
        for oge, etiket in onaylanan:
            if len(kayitlar) >= kac_gorsel:
                break
            sonuc = gorsel_al(oge.get("gorsel", ""), oturum, en_az=en_kucuk)
            if not sonuc:
                _basarisiz += 1
                continue
            bayt, gen, yuk, uzanti = sonuc
            imza = hashlib.sha256(bayt).hexdigest()[:20]
            if imza in imzalar:
                continue
            imzalar.add(imza)
            sayfa_adresi = oge.get("sayfa", "")
            kayitlar.append({
                "bayt": bayt, "gen": gen, "yuk": yuk,
                "alan": alan_adi(sayfa_adresi) or "google",
                "uzanti": uzanti, "kaynak": sayfa_adresi,
                "dogrulama": etiket,
                "bilgi": {"ad": oge.get("baslik", ""), "marka": "", "renk": "",
                          "kod": "", "aciklama": "", "ozellikler": []},
            })

        _not(f"     · {len(onaylanan)} sonuç kabul edildi, {_basarisiz} tanesi "
             f"indirilemedi ya da {en_kucuk}px altında kaldı")

    kullanilan = sorted({k["alan"] for k in kayitlar})
    _not(f"**SONUÇ: {len(kayitlar)} görsel**")
    if not kayitlar:
        if google_sonuclari:
            neden = ("arama sonuç verdi ama hiçbiri bu ürüne uymadı ya da "
                     "görseller indirilemedi")
        else:
            neden = (f"bakılan {len(elenen)} sayfada bu ürüne dair iz yok")
        return [], kullanilan, (
            f"bulunamadı — {neden}. Ürün sayfasının linkini yapıştırırsanız "
            f"oradan çeker."), kullanilan_sorgu
    return kayitlar, kullanilan, "", kullanilan_sorgu


# ----------------------------------------------------------------------------
# Excel / CSV okuma
# ----------------------------------------------------------------------------
# Sutun basligi tahmini. Sirali degil puanli calisiyor: "Model Adi" hem
# "model" hem "ad" iceriyor, ama "modeladi" tam eslesmesi kazandigi icin
# urun adi sutunu olarak dogru yerlestiriliyor. Sirali eslesme yapsaydik
# "Model Adi" yanlislikla kod sutunu secilebilirdi.
ALAN_ANAHTARLARI = {
    "kod": ("orijinalkod", "originalkod", "urunkodu", "stokkodu", "modelkodu",
            "stilkodu", "barkod", "barcode", "kod", "code", "sku", "mpn",
            "referans", "stil", "style"),
    "ad": ("modeladi", "modelad", "urunadi", "urunad", "productname",
           "modelname", "aciklama", "isim", "name", "title", "adi", "ad",
           "model"),
    "marka": ("markaadi", "marka", "brand", "uretici", "firma", "tedarikci"),
    "renk": ("renkadi", "renk", "color", "colour", "colore", "renkkodu",
             "colorway"),
    # Indirilen dosyalara verilecek ad. Sirketin kendi ic numarasi olabilir.
    "dosya": ("eskimalzemeno", "eskimalzemekodu", "eskimalzeme", "malzemeno",
              "malzemekodu", "eskikod", "ickod", "dosyaadi"),
}

_TURKCE = str.maketrans("ğĞüÜşŞıİöÖçÇ", "gGuUsSiIoOcC")


def _baslik_sadelestir(metin):
    """Sutun basligini karsilastirmaya hazirlar (Turkce harfleri cevirir)."""
    return re.sub(r"[^a-z0-9]", "", str(metin or "").translate(_TURKCE).lower())


def _sutunlari_tahmin_et(sutunlar):
    """Her alan icin en uygun sutunu secer; bir sutun iki alana atanmaz."""
    puanlar = []
    for sutun in sutunlar:
        sade = _baslik_sadelestir(sutun)
        if not sade:
            continue
        for alan, anahtarlar in ALAN_ANAHTARLARI.items():
            en_iyi = 0
            for anahtar in anahtarlar:
                if sade == anahtar:                  # tam eslesme en gucludur
                    en_iyi = max(en_iyi, 100 + len(anahtar))
                elif anahtar in sade:                # icinde geciyor
                    en_iyi = max(en_iyi, len(anahtar))
            if en_iyi:
                puanlar.append((en_iyi, alan, sutun))

    puanlar.sort(key=lambda x: -x[0])
    secim, kullanilan = {}, set()
    for _, alan, sutun in puanlar:
        if alan in secim or sutun in kullanilan:
            continue
        secim[alan] = sutun
        kullanilan.add(sutun)
    return secim


def dosyayi_oku(dosya):
    """Yuklenen Excel/CSV dosyasini tabloya cevirir."""
    import pandas as pd
    ad = (dosya.name or "").lower()
    dosya.seek(0)                      # dosya daha once okunmus olabilir
    if ad.endswith((".xlsx", ".xlsm", ".xls")):
        return pd.read_excel(dosya, dtype=str)
    for ayrac in (";", ",", "\t"):
        try:
            dosya.seek(0)
            tablo = pd.read_csv(dosya, dtype=str, sep=ayrac)
            if tablo.shape[1] > 1:
                return tablo
        except Exception:
            continue
    dosya.seek(0)
    return pd.read_csv(dosya, dtype=str)


# ----------------------------------------------------------------------------
# Arayuz
# ----------------------------------------------------------------------------
st.title("Ürün Görseli Bul")
st.caption("Marka ve ürün kodunu yaz, ürünün geçtiği siteleri bulup görsellerini getirsin.")

with st.sidebar:
    st.header("Ayarlar")
    _motor = arama_motoru()
    if _motor == "serper":
        st.success("Arama: **Google Görseller**\n\n"
                   "Önce *marka + orijinal kod* aranıyor — çoğu üründe bu "
                   "yetiyor, 1 kredi. Sonuçlar tutmazsa sorguya renk ve "
                   "model adı eklenip tekrar aranıyor (en fazla 3 deneme). "
                   "Aynı ürünü tekrar aratmak **bedava** — sonuçlar 7 gün "
                   "hafızada tutuluyor.", icon="✅")
    elif _motor == "google_cse":
        st.info("Arama: **Google (50 site sınırlı)**", icon="ℹ️")
    else:
        st.warning(
            "Arama: **DuckDuckGo**\n\n"
            "Uzun ürün kodlarında sonuç bulamıyor. Gerçek Google sonuçları için "
            "[serper.dev](https://serper.dev) üzerinden ücretsiz anahtar alıp "
            "Streamlit → Settings → Secrets içine ekleyin:\n\n"
            "```\nserper_api_key = \"...\"\n```",
            icon="⚠️")
    st.divider()
    kac_gorsel = st.slider("Ürün başına görsel", 4, 30, 12)
    kac_site = st.slider("Kaç site taransın", 2, 10, 5)
    en_kucuk = st.slider("En küçük görsel (piksel)", 400, 2000, 1000, step=100,
                         help="Görselin hem eni hem boyu bu değerden büyük "
                              "olmalı. Daha küçükleri hiç indirilmez.")

    st.divider()
    st.subheader("Eşleşme katılığı")
    katilik_adi = st.radio(
        "Ürün kodu sayfada geçiyor mu diye kontrol edilsin mi?",
        ["Sıkı", "Orta", "Gevşek"],
        index=1,
        captions=[
            "Sadece kodun tamamının geçtiği sayfalar. En temiz, bazen hiç sonuç vermez.",
            "Kodun tamamı ya da ana gövdesi geçsin. Önerilen.",
            "Kontrol etme, arama ne verdiyse al. Alakasız sonuç gelebilir.",
        ],
        label_visibility="collapsed",
    )
    katilik = {"Sıkı": "siki", "Orta": "orta", "Gevşek": "gevsek"}[katilik_adi]

    st.divider()
    teshis_acik = st.checkbox(
        "Teşhis modu", value=False,
        help="Her ürün için hangi sorguların denendiğini, hangi sayfaların "
             "gezildiğini ve görsellerin neden elendiğini gösterir. "
             "Arama davranışını DEĞİŞTİRMEZ, sadece olan biteni yazar.")

    st.divider()
    st.caption("Sonuç gelmiyorsa: ürün sayfasının linkini doğrudan yapıştırın, "
               "marka adını daha açık yazın (örn. `sprayground backpack "
               "910B8224NSZ`) ya da en küçük görsel değerini düşürün.")
    if st.button("Çıkış yap"):
        st.session_state.clear()
        st.rerun()

satirlar = []

sekme_excel, sekme_yazi = st.tabs(["Excel / CSV yükle", "Elle yaz"])

with sekme_excel:
    dosya = st.file_uploader("Ürün listesi", type=["xlsx", "xlsm", "xls", "csv"],
                             label_visibility="collapsed")
    if dosya:
        try:
            tablo = dosyayi_oku(dosya)
        except Exception as hata:
            st.error(f"Dosya okunamadı: {hata}")
            tablo = None

        if tablo is not None and not tablo.empty:
            sutunlar = list(tablo.columns)
            tahmin = _sutunlari_tahmin_et(sutunlar)
            yok = "(yok)"
            secenek = [yok] + sutunlar

            def _sira(alan, varsayilan=0):
                ad = tahmin.get(alan)
                return secenek.index(ad) if ad in secenek else varsayilan

            hepsi_bulundu = all(tahmin.get(a) for a in ("kod", "marka"))
            ozet_satiri = " · ".join(
                f"{etiket}: **{tahmin[a]}**"
                for a, etiket in (("kod", "Kod"), ("ad", "Model"),
                                  ("marka", "Marka"), ("renk", "Renk"),
                                  ("dosya", "Dosya adı"))
                if tahmin.get(a))
            if ozet_satiri:
                st.markdown(("✓ " if hepsi_bulundu else "") + ozet_satiri)

            # Tahmin tuttuysa acilir menuler kapali durur, ekran kalabalik olmaz
            with st.expander("Sütunları değiştir", expanded=not hepsi_bulundu):
                s1, s2, s3, s4 = st.columns(4)
                with s1:
                    kod_sutunu = st.selectbox(
                        "Orijinal kod", sutunlar,
                        index=(sutunlar.index(tahmin["kod"])
                               if tahmin.get("kod") in sutunlar else 0))
                with s2:
                    ad_sutunu = st.selectbox("Model adı", secenek, index=_sira("ad"))
                with s3:
                    marka_sutunu = st.selectbox("Marka", secenek, index=_sira("marka"))
                with s4:
                    renk_sutunu = st.selectbox("Renk", secenek, index=_sira("renk"))
                dosya_sutunu = st.selectbox(
                    "Dosya adı olarak kullanılacak sütun (eski malzeme no)",
                    secenek, index=_sira("dosya"),
                    help="İndirilen görseller bu sütundaki değerle adlandırılır. "
                         "Boş bırakılırsa ürün kodu kullanılır.")
                st.dataframe(tablo.head(5), use_container_width=True)

            def _hucre(satir, sutun):
                if sutun == yok:
                    return ""
                deger = str(satir.get(sutun, "") or "").strip()
                return "" if deger.lower() in ("nan", "none") else deger

            # Dosyadaki gecerli urunler (kodu olanlar)
            adaylar = []
            for _sira, satir in tablo.iterrows():
                kod_degeri = _hucre(satir, kod_sutunu)
                if not kod_degeri:
                    continue
                adaylar.append({
                    "kod": kod_degeri,
                    "marka": _hucre(satir, marka_sutunu),
                    "ad": _hucre(satir, ad_sutunu),
                    "renk": _hucre(satir, renk_sutunu),
                    "dosya_adi": _hucre(satir, dosya_sutunu),
                    "url": "",
                })

            if not adaylar:
                st.warning("Dosyada ürün kodu olan satır bulunamadı. "
                           "Sütun eşleştirmesini kontrol edin.")
            else:
                st.markdown(f"**İşlenecek ürünleri seçin** ({len(adaylar)} ürün)")

                d1, d2, _bosluk = st.columns([1, 1, 4])
                if d1.button("Tümünü seç", use_container_width=True):
                    st.session_state.pop("urun_secimi", None)
                    st.session_state["_hepsi_secili"] = True
                if d2.button("Temizle", use_container_width=True):
                    st.session_state.pop("urun_secimi", None)
                    st.session_state["_hepsi_secili"] = False

                import pandas as pd
                _varsayilan = st.session_state.get("_hepsi_secili", False)
                gosterim = pd.DataFrame({
                    "Seç": [_varsayilan] * len(adaylar),
                    "Dosya adı": [u["dosya_adi"] for u in adaylar],
                    "Orijinal kod": [u["kod"] for u in adaylar],
                    "Model": [u["ad"] for u in adaylar],
                    "Marka": [u["marka"] for u in adaylar],
                    "Renk": [u["renk"] for u in adaylar],
                })

                duzenlenmis = st.data_editor(
                    gosterim,
                    key="urun_secimi",
                    hide_index=True,
                    use_container_width=True,
                    height=min(420, 80 + 35 * len(adaylar)),
                    column_config={
                        "Seç": st.column_config.CheckboxColumn(
                            "Seç", help="İşlenecek ürünleri işaretleyin",
                            default=False, width="small"),
                    },
                    disabled=["Dosya adı", "Orijinal kod", "Model", "Marka", "Renk"],
                )

                satirlar = [u for u, secili in zip(adaylar, duzenlenmis["Seç"])
                            if bool(secili)]

                if satirlar:
                    _n = len(satirlar)
                    # Gorseller bellekte tutuluyor; cok urun secilirse ucretsiz
                    # sunucunun bellegi dolup uygulama yeniden baslayabiliyor.
                    _sure = f"~{max(1, _n // 2)}-{_n} dakika"
                    if _n > 50:
                        st.error(f"{_n} ürün çok fazla. Uygulama bellek yetersizliğinden "
                                 f"yarıda kesilebilir. **En fazla 30 ürün** seçip "
                                 f"birkaç turda yapmanızı öneririm.")
                    elif _n > 30:
                        st.warning(f"{_n} ürün seçildi (~{_n} kredi, {_sure}). "
                                   f"30'un üstü riskli olabilir; sorun yaşarsanız "
                                   f"daha küçük gruplara bölün.")
                    else:
                        st.success(f"{_n} ürün seçildi · ~{_n} kredi · {_sure}")
                else:
                    st.info("Henüz ürün seçilmedi. Tablodan işaretleyin ya da "
                            "**Tümünü seç**'e basın.")
                if satirlar:
                    ilk = satirlar[0]
                    st.caption(f"Örnek arama: {ilk['marka']} \"{ilk['kod']}\" "
                               f"{ilk.get('renk','')}".strip()
                               + "   ·   model adı yalnızca sonuç çıkmazsa kullanılır")

with sekme_yazi:
    girdi = st.text_area(
        "Her satıra bir ürün — marka + kod, ya da doğrudan ürün linki",
        placeholder=("sprayground 910B8224NSZ\n"
                     "crocs 212481-410\n"
                     "https://rubaiyat.com/products/palm-angels-pmaa10..."),
        height=140,
        label_visibility="collapsed",
    )
    st.caption("Ürünün sayfasını zaten biliyorsan linki yapıştır — arama yapılmaz, "
               "doğrudan o sayfanın görselleri alınır. Uzun ve karışık kodlarda "
               "en garantili yol budur.")
    if girdi and girdi.strip():
        for ham in girdi.strip().splitlines():
            ham = ham.strip()
            if not ham:
                continue
            if re.match(r"https?://", ham):
                satirlar.append({"kod": "", "marka": "", "ad": "",
                                 "renk": "", "dosya_adi": "", "url": ham})
                continue
            # Rakam iceren en uzun parca kod, geri kalani marka sayilir
            kod_p = kodu_ayikla(ham)
            marka_p = " ".join(w for w in ham.split() if w != kod_p)
            satirlar.append({"kod": kod_p, "marka": marka_p, "ad": "",
                             "renk": "", "dosya_adi": "", "url": ""})

# Ayni urun listede birden fazla geciyorsa bir kez isliyoruz: bosuna kredi
# harcanmasin, sonuclarda tekrar cikmasin.
if satirlar:
    _gorulen, _tekil = set(), []
    for _u in satirlar:
        # Renk de anahtarin parcasi: ayni orijinal kod farkli renklerde
        # olabiliyor, bunlar AYRI urun. Rengi katmazsak birini yutardik.
        _anahtar = (_sadelestir(_u.get("kod", "")),
                    _sadelestir(_u.get("marka", "")),
                    _sadelestir(_u.get("renk", "")),
                    _u.get("url", ""))
        if _anahtar in _gorulen:
            continue
        _gorulen.add(_anahtar)
        _tekil.append(_u)
    _tekrar = len(satirlar) - len(_tekil)
    satirlar = _tekil
    st.caption(f"{len(satirlar)} ürün işlenecek"
               + (f" ({_tekrar} tekrar eden satır atlandı)" if _tekrar else ""))

if st.button("Görselleri bul", type="primary", use_container_width=True,
             disabled=not satirlar):
    if not satirlar:
        st.warning("Önce Excel yükleyin ya da elle ürün yazın.")
        st.stop()

    oturum = requests.Session()
    oturum.headers.update(BASLIK)

    kredi_basi = _ARAMA_SAYACI["toplam"]
    # Ayni marka+kod listede birden fazla kez geciyorsa (farkli renkler)
    # rengi aramaya katmamiz gerekiyor, aksi halde ikisi ayni sonucu getirir.
    _kod_sayaci = {}
    for _u in satirlar:
        _ik = (_sadelestir(_u.get("marka", "")), _sadelestir(_u.get("kod", "")))
        _kod_sayaci[_ik] = _kod_sayaci.get(_ik, 0) + 1

    tum_sonuclar = []
    ilerleme = st.progress(0.0, text="Başlıyor...")

    for sira, urun in enumerate(satirlar):
        sorgu = (urun.get("url")
                 or " ".join(x for x in (urun.get("marka"), urun.get("kod"),
                                         urun.get("renk")) if x)
                 or urun.get("ad") or "ürün")
        ilerleme.progress(sira / len(satirlar), text=f"Aranıyor: {sorgu}")
        _defter = [] if teshis_acik else None
        try:
            _ik = (_sadelestir(urun.get("marka", "")), _sadelestir(urun.get("kod", "")))
            kayitlar, alanlar, hata, kullanilan = urun_gorselleri(
                urun, kac_gorsel, kac_site, en_kucuk, oturum, katilik,
                renk_gerekli=_kod_sayaci.get(_ik, 1) > 1, teshis=_defter)
        except Exception as sorun:
            # Bir urunde beklenmedik hata cikarsa listenin geri kalani dursun
            kayitlar, alanlar, kullanilan = [], [], ""
            hata = f"beklenmedik hata: {type(sorun).__name__} — {sorun}"
        # Indirilen dosyalarin adi: eski malzeme no varsa o, yoksa urun kodu
        _taban = (urun.get("dosya_adi") or "").strip() or (urun.get("kod") or "").strip()
        _taban = re.sub(r"[^A-Za-z0-9._-]+", "_", _taban).strip("._-") or "urun"
        tum_sonuclar.append((sorgu, kayitlar, alanlar, hata, kullanilan, _taban,
                             _defter))

    ilerleme.progress(1.0, text="Bitti")
    ilerleme.empty()
    st.session_state["sonuclar"] = tum_sonuclar
    st.session_state["kredi"] = (_ARAMA_SAYACI["toplam"] - kredi_basi, len(satirlar))

# --- Kredi kullanimi ---
if st.session_state.get("kredi") and google_var_mi():
    _yeni, _urun = st.session_state["kredi"]
    if _yeni == 0:
        st.info(f"Bu çalıştırmada **hiç kredi harcanmadı** — {_urun} ürünün "
                f"tamamı hafızadan geldi.", icon="💾")
    elif _yeni < _urun:
        st.info(f"{_urun} ürün için **{_yeni} kredi** harcandı — kalanlar "
                f"hafızadan geldi, onlar bedava.", icon="💾")
    elif _yeni > _urun:
        st.info(f"{_urun} ürün için **{_yeni} kredi** harcandı. Bazı ürünlerde "
                f"ilk arama tutmadığı için sorguya renk/model adı eklenip "
                f"tekrar arandı.", icon="💾")
    else:
        st.caption(f"Bu çalıştırmada {_yeni} kredi harcandı.")

# --- Sonuclari goster ---
# Dugme kimliklerini veriden (sorgu+kaynak+sira) turetmek carpisma yaratiyordu:
# ayni urun listede iki kez gecince iki dugme ayni kimligi aliyor ve Streamlit
# hata veriyordu. Basit bir sayac bunu tamamen ortadan kaldiriyor.
_dugme_no = 0

for _satir in st.session_state.get("sonuclar", []):
    # Eski oturumlarda defter alani olmayabilir
    sorgu, kayitlar, alanlar, hata, kullanilan, taban = _satir[:6]
    defter = _satir[6] if len(_satir) > 6 else None
    st.subheader(sorgu)
    if kullanilan and kullanilan.strip() != sorgu.strip():
        st.caption(f"Aramada kullanılan: `{kullanilan}`")

    if defter:
        with st.expander("🔍 Teşhis — ne oldu?", expanded=not kayitlar):
            st.markdown("\n\n".join(defter))

    if hata:
        st.error(f"{sorgu} — {hata}")
        continue
    if not kayitlar:
        st.warning("Görsel bulunamadı. Marka adını daha açık yazmayı deneyin "
                   "ya da en küçük görsel değerini düşürün.")
        continue

    baglanti = sum(1 for k in kayitlar if k.get("dogrulama") == "link")
    tam = sum(1 for k in kayitlar if k.get("dogrulama") == "tam")
    kismi = sum(1 for k in kayitlar if k.get("dogrulama") == "kismi")
    zayif = sum(1 for k in kayitlar if k.get("dogrulama") == "zayif")
    supheli = sum(1 for k in kayitlar
                  if k.get("dogrulama") in ("marka", "yok", "google_zayif"))
    dagilim = []
    if baglanti:
        dagilim.append(f"{baglanti} verdiğin linkten")
    if tam:
        dagilim.append(f"{tam} kod doğrulandı")
    if kismi:
        dagilim.append(f"{kismi} kısmi eşleşme")
    if zayif:
        dagilim.append(f"{zayif} aynı model")
    if supheli:
        dagilim.append(f"{supheli} sadece marka eşleşti")
    st.caption(f"{len(kayitlar)} görsel · {' · '.join(dagilim)}")
    if supheli:
        st.warning(
            "Aşağıdaki görsellerin bir kısmında ürün kodu **hiçbir yerde "
            "doğrulanamadı** — Google'ın sıralamasına güvenilerek alındı. "
            "Farklı bir ürün olabilir, kartta ürün adını mutlaka kontrol edin. "
            "Daha temiz sonuç için soldan **Sıkı**'yı seçin.")
    elif zayif:
        st.warning(
            "Kodun tamamı bulunamadı, aynı model ailesinden sayfalara inildi. "
            "Doğru model ama **rengi farklı olabilir** — kartta ürün adını ve "
            "rengini kontrol edin.")

    # Gorselleri geldikleri sayfaya gore grupluyoruz: her grubun basinda o
    # sayfanin urun bilgisi duruyor, ekip sadece gorsele bakip karar vermesin.
    gruplar = {}
    for kayit in kayitlar:
        gruplar.setdefault(kayit["kaynak"], []).append(kayit)

    # Dosya numarasi her urunde 01'den baslar (dugme kimliginden bagimsiz)
    _urun_sira = 0

    for kaynak, grup in gruplar.items():
        bilgi = grup[0].get("bilgi") or {}
        alan = grup[0]["alan"]

        with st.container(border=True):
            ust, yan = st.columns([3, 1])
            with ust:
                baslik = bilgi.get("ad") or "(ürün adı bulunamadı)"
                st.markdown(f"**{baslik}**")

                etiketler = []
                if bilgi.get("marka"):
                    etiketler.append(f"Marka: **{bilgi['marka']}**")
                if bilgi.get("renk"):
                    etiketler.append(f"Renk: **{bilgi['renk']}**")
                if bilgi.get("kod"):
                    etiketler.append(f"Sitedeki kod: `{bilgi['kod']}`")
                for ozellik in (bilgi.get("ozellikler") or [])[:4]:
                    etiketler.append(ozellik)
                if etiketler:
                    st.markdown(" · ".join(etiketler))
                else:
                    st.caption("Bu sayfadan ürün bilgisi çıkarılamadı — "
                               "görselleri kontrol ederek kullanın.")

                if bilgi.get("aciklama"):
                    with st.expander("Açıklama"):
                        st.write(bilgi["aciklama"])
            with yan:
                st.caption(alan)
                st.link_button("Sayfayı aç", kaynak, use_container_width=True)

            sutunlar = st.columns(5)
            for i, kayit in enumerate(grup):
                _dugme_no += 1      # dugme kimligi: tum sayfada benzersiz
                _urun_sira += 1     # dosya numarasi: urun icinde sirali
                with sutunlar[i % 5]:
                    st.image(onizleme_yap(kayit["bayt"]),
                             use_container_width=True)
                    rozet = {"tam": "✓ kod doğrulandı",
                             "kismi": "~ kısmi eşleşme",
                             "zayif": "⚠ aynı model, renk farklı olabilir",
                             "marka": "⚠ sadece marka eşleşti",
                             "google": "✓ Google + kod eşleşti",
                             "google_zayif": "~ Google sıralamasından",
                             "yok": "⚠ doğrulanmadı",
                             "link": "✓ verdiğin link"}.get(kayit.get("dogrulama"), "")
                    st.caption(f"**{kayit['alan']}**  \n"
                               f"{kayit['gen']}×{kayit['yuk']} · {rozet}")
                    st.download_button(
                        "İndir",
                        data=kayit["bayt"],
                        file_name=f"{taban}_{_urun_sira:02d}{kayit['uzanti']}",
                        mime=f"image/{kayit['uzanti'].lstrip('.')}",
                        key=f"indir_{_dugme_no}",
                        use_container_width=True,
                    )

# --- Hepsini birden indir ---
sonuclar = st.session_state.get("sonuclar", [])
toplam = sum(len(_s[1]) for _s in sonuclar)
if toplam:
    tampon = io.BytesIO()
    with zipfile.ZipFile(tampon, "w", zipfile.ZIP_DEFLATED) as arsiv:
        for _s in sonuclar:
            sorgu, kayitlar, taban = _s[0], _s[1], _s[5]
            for i, kayit in enumerate(kayitlar, 1):
                arsiv.writestr(f"{taban}/{taban}_{i:02d}{kayit['uzanti']}",
                               kayit["bayt"])

    st.divider()
    st.download_button(
        f"Hepsini indir ({toplam} görsel, zip)",
        data=tampon.getvalue(),
        file_name="urun_gorselleri.zip",
        mime="application/zip",
        type="primary",
        use_container_width=True,
    )
    st.caption("Bayisi olduğunuz markaların görselleri genelde sorunsuz kullanılır; "
               "rakip mağazanın kendi çektiği fotoğraflar telifli olabilir. "
               "Her görselin altında hangi siteden geldiği yazar.")

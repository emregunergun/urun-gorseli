"""Urun Gorseli Bul - ekip icin web uygulamasi.

Marka ve urun kodu girilir; urunun gectigi siteler bulunur, galerileri
cikarilir ve gorseller ekranda gosterilir.

Erisim ortak bir sifre ile korunur. Sifre kodun icinde degil, Streamlit'in
"Secrets" bolumunde saklanir.
"""

import hashlib
import hmac
import io
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

ATLANACAK_ALAN = ("youtube.", "facebook.", "instagram.", "pinterest.", "tiktok.",
                  "twitter.", "x.com", "reddit.", "aliexpress.", "wikipedia.")

UZANTI_TAMAM = (".jpg", ".jpeg", ".png", ".webp")


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


def sayfayi_dogrula(html, kod):
    """Urun kodu sayfada geciyor mu?

    'tam'   - kodun tamami geciyor (212481-410)
    'kismi' - sadece ana govdesi geciyor (212481). Renk/beden ayri yazilmis
              olabilir; bu sayfalar genelde dogru urundur.
    'yok'   - kod hic gecmiyor. Buyuk ihtimalle alakasiz sayfa.
    """
    if not kod:
        return "kismi"
    sade_sayfa = _sadelestir(html)
    if _sadelestir(kod) in sade_sayfa:
        return "tam"
    govde = max(re.findall(r"\d{4,}", kod), key=len, default="")
    if govde and govde in sade_sayfa:
        return "kismi"
    return "yok"


def sayfa_gorselleri(sayfa_url, oturum, kod=""):
    """Sayfadaki galeri gorsellerini ve kod dogrulama sonucunu dondurur."""
    try:
        cevap = oturum.get(sayfa_url, timeout=15)
        cevap.raise_for_status()
    except Exception:
        return [], "yok"
    if "text/html" not in cevap.headers.get("Content-Type", ""):
        return [], "yok"

    dogrulama = sayfayi_dogrula(cevap.text, kod)
    corba = BeautifulSoup(cevap.text, "html.parser")
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

    return adaylar, dogrulama


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
def urun_gorselleri(sorgu, kac_gorsel, kac_site, en_kucuk, oturum, katilik="orta"):
    """Tek bir urun icin gorselleri toplar.

    katilik:
      "siki"  - sadece kodun tamaminin gectigi sayfalar
      "orta"  - kodun tamami ya da govdesi gecen sayfalar (varsayilan)
      "gevsek"- dogrulama yapma, arama ne verdiyse al
    """
    kod = kodu_ayikla(sorgu)
    # Kodu tirnak icine alarak aratmak, arama motorunu birebir eslesmeye zorlar
    aranan = sorgu.replace(kod, f'"{kod}"') if kod else sorgu

    try:
        sonuclar = DDGS().text(aranan, max_results=kac_site * 3)
    except Exception as hata:
        return [], [], f"arama yapılamadı ({hata})"

    sayfalar, gorulen_alan = [], set()
    for s in sonuclar:
        adres = s.get("href") or s.get("link") or ""
        bulunan = re.findall(r"https?://([^/]+)", adres)
        alan = re.sub(r"^www\.", "", bulunan[0]) if bulunan else ""
        if not alan or alan in gorulen_alan or any(k in alan for k in ATLANACAK_ALAN):
            continue
        gorulen_alan.add(alan)
        sayfalar.append((alan, adres))
        if len(sayfalar) >= kac_site:
            break

    if not sayfalar:
        return [], [], "ürün sayfası bulunamadı"

    kabul = {"siki": {"tam"},
             "orta": {"tam", "kismi"},
             "gevsek": {"tam", "kismi", "yok"}}[katilik]

    imzalar, kayitlar, elenen = set(), [], []
    for alan, adres in sayfalar:
        if len(kayitlar) >= kac_gorsel:
            break
        adaylar, dogrulama = sayfa_gorselleri(adres, oturum, kod)
        if dogrulama not in kabul:
            elenen.append(alan)
            continue
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
            kayitlar.append({"bayt": bayt, "gen": gen, "yuk": yuk,
                             "alan": alan, "uzanti": uzanti, "kaynak": adres,
                             "dogrulama": dogrulama})
        time.sleep(0.4)

    kullanilan = sorted({k["alan"] for k in kayitlar})
    if not kayitlar and elenen:
        return [], kullanilan, (f"kod hiçbir sayfada doğrulanamadı "
                                f"({len(elenen)} site elendi) — eşleşme katılığını "
                                f"gevşetin ya da marka adını değiştirin")
    return kayitlar, kullanilan, ""


# ----------------------------------------------------------------------------
# Arayuz
# ----------------------------------------------------------------------------
st.title("Ürün Görseli Bul")
st.caption("Marka ve ürün kodunu yaz, ürünün geçtiği siteleri bulup görsellerini getirsin.")

with st.sidebar:
    st.header("Ayarlar")
    kac_gorsel = st.slider("Ürün başına görsel", 4, 30, 12)
    kac_site = st.slider("Kaç site taransın", 2, 10, 5)
    en_kucuk = st.slider("En küçük görsel (piksel)", 200, 1200, 500, step=50)

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
    st.caption("Sonuç gelmiyorsa marka adını daha açık yazın "
               "(örn. `sprayground backpack 910B8224NSZ`) ya da "
               "en küçük görsel değerini düşürün.")
    if st.button("Çıkış yap"):
        st.session_state.clear()
        st.rerun()

girdi = st.text_area(
    "Marka ve ürün kodu — her satıra bir ürün",
    placeholder="sprayground 910B8224NSZ\ncrocs 212481-410\nnike DV5456-100",
    height=140,
)

if st.button("Görselleri bul", type="primary", use_container_width=True):
    satirlar = [s.strip() for s in (girdi or "").strip().splitlines() if s.strip()]
    if not satirlar:
        st.warning("Önce marka ve ürün kodu yazın.")
        st.stop()

    oturum = requests.Session()
    oturum.headers.update(BASLIK)

    tum_sonuclar = []
    ilerleme = st.progress(0.0, text="Başlıyor...")

    for sira, satir in enumerate(satirlar):
        sorgu = " ".join(satir.split())
        ilerleme.progress(sira / len(satirlar), text=f"Aranıyor: {sorgu}")
        kayitlar, alanlar, hata = urun_gorselleri(
            sorgu, kac_gorsel, kac_site, en_kucuk, oturum, katilik)
        tum_sonuclar.append((sorgu, kayitlar, alanlar, hata))

    ilerleme.progress(1.0, text="Bitti")
    ilerleme.empty()
    st.session_state["sonuclar"] = tum_sonuclar

# --- Sonuclari goster ---
for sorgu, kayitlar, alanlar, hata in st.session_state.get("sonuclar", []):
    st.subheader(sorgu)

    if hata:
        st.error(f"{sorgu} — {hata}")
        continue
    if not kayitlar:
        st.warning("Görsel bulunamadı. Marka adını daha açık yazmayı deneyin "
                   "ya da en küçük görsel değerini düşürün.")
        continue

    tam = sum(1 for k in kayitlar if k.get("dogrulama") == "tam")
    kismi = sum(1 for k in kayitlar if k.get("dogrulama") == "kismi")
    supheli = sum(1 for k in kayitlar if k.get("dogrulama") == "yok")
    dagilim = []
    if tam:
        dagilim.append(f"{tam} kod doğrulandı")
    if kismi:
        dagilim.append(f"{kismi} kısmi eşleşme")
    if supheli:
        dagilim.append(f"{supheli} doğrulanmadı")
    st.caption(f"{len(kayitlar)} görsel · {' · '.join(dagilim)} · "
               f"kaynak: {', '.join(alanlar)}")
    if supheli:
        st.warning("Doğrulanmamış görseller var — ürün kodu o sayfalarda geçmiyor. "
                   "Kontrol ederek kullanın.")

    sutunlar = st.columns(5)
    for i, kayit in enumerate(kayitlar):
        with sutunlar[i % 5]:
            st.image(kayit["bayt"], use_container_width=True)
            rozet = {"tam": "✓ kod doğrulandı",
                     "kismi": "~ kısmi eşleşme",
                     "yok": "⚠ doğrulanmadı"}.get(kayit.get("dogrulama"), "")
            st.caption(f"{kayit['gen']}×{kayit['yuk']} · {kayit['alan']}  \n{rozet}")
            st.download_button(
                "İndir",
                data=kayit["bayt"],
                file_name=f"{re.sub(r'[^A-Za-z0-9._-]+', '_', sorgu)}_"
                          f"{i + 1:02d}{kayit['uzanti']}",
                mime=f"image/{kayit['uzanti'].lstrip('.')}",
                key=f"tek_{sorgu}_{i}",
                use_container_width=True,
            )

# --- Hepsini birden indir ---
sonuclar = st.session_state.get("sonuclar", [])
toplam = sum(len(k) for _, k, _, _ in sonuclar)
if toplam:
    tampon = io.BytesIO()
    with zipfile.ZipFile(tampon, "w", zipfile.ZIP_DEFLATED) as arsiv:
        for sorgu, kayitlar, _, _ in sonuclar:
            klasor = re.sub(r"[^A-Za-z0-9._-]+", "_", sorgu).strip("_")[:60] or "urun"
            for i, kayit in enumerate(kayitlar, 1):
                temiz = re.sub(r"[^a-z0-9]+", "-", kayit["alan"])
                arsiv.writestr(f"{klasor}/{i:02d}_{temiz}{kayit['uzanti']}",
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

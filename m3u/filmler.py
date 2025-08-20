import asyncio
import aiohttp
import re
import os
from itertools import islice
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
import logging
import time

# --- LOGLAMA AYARLARI ---
# Konsola renkli ve düzenli loglama yapmak için temel ayarlar.
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- SABİTLER ---
# Betiğin çalışması için gerekli olan temel URL'ler ve başlık bilgileri.
BASE_URL = "https://dizifun5.com/filmler"  # Hedef URL filmler olarak güncellendi
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "tr-TR,tr;q=0.8,en-US;q=0.5,en;q=0.3",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# --- YARDIMCI FONKSİYONLAR ---

def create_proxy_url(original_url):
    """
    Verilen M3U8 URL'sini, bir proxy servisi üzerinden geçecek şekilde yeniden formatlar.
    Bu, doğrudan erişim sorunlarını aşmaya yardımcı olabilir.
    """
    if not original_url:
        return None
    proxy_url = f"https://3.nejyoner19.workers.dev/?url={original_url}"
    logger.info(f"[PROXY] {original_url} -> {proxy_url}")
    return proxy_url

def sanitize_id(text):
    """
    Metni, M3U formatında geçerli bir TVG ID'sine dönüştürür.
    Türkçe karakterleri ve özel sembolleri temizler.
    """
    if not text:
        return "UNKNOWN_MOVIE"
    
    # Türkçe karakterleri İngilizce karşılıkları ile değiştir
    turkish_chars = {'ç': 'c', 'Ç': 'C', 'ğ': 'g', 'Ğ': 'G', 'ı': 'i', 'I': 'I', 'İ': 'I', 'ö': 'o', 'Ö': 'O', 'ş': 's', 'Ş': 'S', 'ü': 'u', 'Ü': 'U'}
    for tr_char, en_char in turkish_chars.items():
        text = text.replace(tr_char, en_char)
    
    # Geriye kalan alfanümerik olmayan karakterleri temizle
    text = re.sub(r'[^A-Za-z0-9\s]', '', text)
    # Boşlukları alt çizgi ile değiştir
    text = re.sub(r'\s+', '_', text.strip())
    # Tamamen büyük harfe çevir
    text = text.upper()
    # Birden fazla alt çizgiyi tek ile değiştir
    text = re.sub(r'_+', '_', text)
    text = text.strip('_')
    return text if text else "UNKNOWN_MOVIE"

def fix_url(url, base=BASE_URL):
    """
    Göreceli URL'leri (örneğin, /film/adi) mutlak URL'lere dönüştürür.
    """
    if not url:
        return None
    if url.startswith('/'):
        return urljoin(base, url)
    return url

async def fetch_page(session, url, timeout=45):
    """
    Verilen URL'nin içeriğini asenkron olarak çeker.
    Hata yönetimi ve zaman aşımı kontrolleri içerir.
    """
    try:
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=timeout)) as response:
            if response.status == 200:
                return await response.text()
            else:
                logger.warning(f"[!] HTTP {response.status} hatası: {url}")
                return None
    except asyncio.TimeoutError:
        logger.error(f"[!] Zaman aşımı hatası ({timeout}s): {url}")
        return None
    except Exception as e:
        logger.error(f"[!] Sayfa getirme hatası ({url}): {e}")
        return None

# --- M3U8 ÇIKARMA FONKSİYONLARI ---
# Bu fonksiyonlar, video oynatıcı iframe'lerinden M3U8 linkini bulup çıkarmak için tasarlanmıştır.

async def extract_gujan_m3u8(session, gujan_iframe_url):
    """Gujan oynatıcısının iframe'inden M3U8 URL'sini çıkarır."""
    try:
        if gujan_iframe_url.startswith("//"):
            gujan_iframe_url = "https:" + gujan_iframe_url
        content = await fetch_page(session, gujan_iframe_url)
        if not content:
            return None
        
        # M3U8 linkini doğrudan veya script içerisinden regex ile ara
        m3u8_match = re.search(r'file\s*:\s*"([^"]+\.m3u8)"', content)
        if m3u8_match:
            m3u8_url = m3u8_match.group(1)
            logger.info(f"[GUJAN] ✅ M3U8 URL bulundu: {m3u8_url}")
            return m3u8_url
        logger.warning(f"[GUJAN] ❌ M3U8 URL bulunamadı: {gujan_iframe_url}")
        return None
    except Exception as e:
        logger.error(f"[GUJAN] ❌ Hata: {e}")
        return None

async def find_working_premiumvideo_domain(session, file_id, domains=["d1", "d2", "d3", "d4"]):
    """premiumvideo.click için çalışan bir alt alan adını test ederek bulur."""
    for domain in domains:
        m3u8_url = f"https://{domain}.premiumvideo.click/uploads/encode/{file_id}/master.m3u8"
        try:
            # HEAD isteği ile hızlı kontrol
            async with session.head(m3u8_url, timeout=10) as response:
                if response.status == 200:
                    logger.info(f"[PREMIUMVIDEO] ✅ Çalışan domain bulundu: {domain}")
                    return m3u8_url
        except (aiohttp.ClientError, asyncio.TimeoutError):
            continue
    logger.warning(f"[PREMIUMVIDEO] ⚠️ Hiçbir domain çalışmıyor! Varsayılan (d2) kullanılacak.")
    return f"https://d2.premiumvideo.click/uploads/encode/{file_id}/master.m3u8"

# --- ANA İŞLEM FONKSİYONLARI ---

async def get_movies_from_page(session, page_num):
    """
    Belirtilen sayfa numarasındaki tüm film linklerini çeker.
    """
    page_url = f"{BASE_URL}?p={page_num}"
    logger.info(f"Sayfa {page_num} taranıyor: {page_url}")

    content = await fetch_page(session, page_url)
    if not content:
        return [], False

    soup = BeautifulSoup(content, 'html.parser')
    movie_links = []
    # Film linkleri için doğru seçiciyi kullan
    link_elements = soup.select("a.uk-position-cover[href*='/film/']")
    for element in link_elements:
        href = element.get("href")
        if href:
            full_url = fix_url(href)
            if full_url not in movie_links:
                movie_links.append(full_url)

    # Sonraki sayfanın olup olmadığını kontrol et
    next_page_element = soup.select_one(".uk-pagination .uk-pagination-next:not(.uk-disabled)")
    has_next_page = next_page_element is not None

    logger.info(f"[+] Sayfa {page_num}: {len(movie_links)} film linki bulundu. Sonraki sayfa: {'Var' if has_next_page else 'Yok'}")
    return movie_links, has_next_page

async def get_all_movie_links():
    """
    Sitedeki tüm film linklerini, sayfaları gezerek toplar.
    """
    async with aiohttp.ClientSession() as session:
        all_movies = []
        page_num = 1
        while True:
            movie_links, has_next_page = await get_movies_from_page(session, page_num)
            if not movie_links:
                logger.info(f"Sayfa {page_num} boş veya ulaşılamadı. Tarama durduruluyor.")
                break
            
            all_movies.extend(movie_links)
            
            if not has_next_page:
                logger.info(f"Son sayfaya ({page_num}) ulaşıldı.")
                break
            
            page_num += 1
            await asyncio.sleep(0.5) # Sunucuyu yormamak için bekleme

    unique_movies = sorted(list(set(all_movies)))
    logger.info(f"[✓] Toplam {len(unique_movies)} benzersiz film linki toplandı.")
    return unique_movies

async def get_movie_metadata(session, movie_url):
    """
    Film sayfasından başlık ve poster (logo) URL'si gibi meta verileri çeker.
    """
    content = await fetch_page(session, movie_url)
    if not content:
        return "Bilinmeyen Film", ""

    soup = BeautifulSoup(content, 'html.parser')
    title_element = soup.select_one(".text-bold")
    title = title_element.get_text(strip=True) if title_element else "Bilinmeyen Film"
    
    logo_element = soup.select_one(".media-cover img")
    logo_url = logo_element.get("src", "") if logo_element else ""
    logo_url = fix_url(logo_url)

    return title, logo_url

async def extract_m3u8_from_movie_page(session, movie_url):
    """
    Tek bir film sayfasından M3U8 yayın linkini bulur ve çıkarır.
    """
    logger.info(f"[*] Film işleniyor: {movie_url}")
    content = await fetch_page(session, movie_url)
    if not content:
        return None

    soup = BeautifulSoup(content, 'html.parser')
    m3u8_url = None

    # Oynatıcı iframe'lerini öncelik sırasına göre ara
    iframe_selectors = [
        'iframe[src*="gujan.premiumvideo.click"]',
        'iframe[src*="playhouse.premiumvideo.click"]',
        'iframe[src*="premiumvideo.click/player"]',
        'iframe#londonIframe'
    ]

    for selector in iframe_selectors:
        iframe = soup.select_one(selector)
        if iframe:
            src = iframe.get("src") or iframe.get("data-src")
            if src and src != "about:blank":
                iframe_url = fix_url(src)
                logger.info(f"[IFRAME] Bulundu: {iframe_url}")

                if "gujan" in iframe_url:
                    m3u8_url = await extract_gujan_m3u8(session, iframe_url)
                elif "premiumvideo.click" in iframe_url:
                    file_id_match = re.search(r'(?:file_id=|/player/)([a-zA-Z0-9]+)', iframe_url)
                    if file_id_match:
                        file_id = file_id_match.group(1)
                        logger.info(f"[FILE_ID] Bulundu: {file_id}")
                        m3u8_url = await find_working_premiumvideo_domain(session, file_id)
                
                if m3u8_url:
                    break # M3U8 bulununca döngüden çık
    
    if m3u8_url:
        return create_proxy_url(m3u8_url)
    else:
        logger.warning(f"[!] M3U8 URL bulunamadı: {movie_url}")
        return None

async def process_all_movies(all_movie_links, newly_added_links, output_filename="Filmler.m3u"):
    """
    Tüm film linklerini işler ve "Son Eklenenler" ve "Filmler" gruplarıyla M3U dosyasına yazar.
    """
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=10)) as session:
        # Tekrar tekrar aynı filmin verisini çekmemek için bir önbellek (cache) oluşturalım.
        unique_links_to_process = sorted(list(set(all_movie_links + newly_added_links)))
        movie_data_cache = {}

        logger.info(f"\nToplam {len(unique_links_to_process)} benzersiz film verisi çekilecek...")
        
        # Tüm benzersiz filmlerin verilerini asenkron olarak çek ve önbelleğe al
        tasks = [process_single_movie(session, movie_url) for movie_url in unique_links_to_process]
        results = await asyncio.gather(*tasks)

        for i, result in enumerate(results):
            if result:
                # URL'yi anahtar olarak kullanarak film verilerini sakla
                movie_data_cache[unique_links_to_process[i]] = result
        
        logger.info(f"{len(movie_data_cache)} filmin verisi başarıyla çekildi. M3U dosyası oluşturuluyor...")

        with open(output_filename, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")

            # --- 1. "Son Eklenenler" Grubunu Yaz ---
            logger.info("\n--- 'Son Eklenenler' grubu yazılıyor... ---")
            for movie_url in newly_added_links:
                if movie_url in movie_data_cache:
                    title, logo_url, m3u8_url = movie_data_cache[movie_url]
                    tvg_id = sanitize_id(title)
                    display_name = title
                    
                    f.write(
                        f'#EXTINF:-1 tvg-name="{display_name}" '
                        f'tvg-language="Turkish" tvg-country="TR" '
                        f'tvg-id="{tvg_id}" '
                        f'tvg-logo="{logo_url}" '
                        f'group-title="Son Eklenenler",{display_name}\n'
                    )
                    f.write(m3u8_url.strip() + "\n")
                    logger.info(f"[✓] (Son Eklenen) {display_name} eklendi.")

            # --- 2. "Filmler" Grubunu Yaz ---
            logger.info("\n--- 'Filmler' grubu yazılıyor... ---")
            for movie_url in all_movie_links:
                if movie_url in movie_data_cache:
                    title, logo_url, m3u8_url = movie_data_cache[movie_url]
                    tvg_id = sanitize_id(title)
                    display_name = title

                    f.write(
                        f'#EXTINF:-1 tvg-name="{display_name}" '
                        f'tvg-language="Turkish" tvg-country="TR" '
                        f'tvg-id="{tvg_id}" '
                        f'tvg-logo="{logo_url}" '
                        f'group-title="Filmler",{display_name}\n'
                    )
                    f.write(m3u8_url.strip() + "\n")
                    logger.info(f"[✓] (Tüm Filmler) {display_name} eklendi.")

    logger.info(f"\n[✓] {output_filename} dosyası başarıyla oluşturuldu.")


async def process_single_movie(session, movie_url):
    """Bir filmin meta verilerini ve M3U8 linkini çeker."""
    try:
        title, logo_url = await get_movie_metadata(session, movie_url)
        if title == "Bilinmeyen Film":
            return None
        
        m3u8_url = await extract_m3u8_from_movie_page(session, movie_url)
        if not m3u8_url:
            return None
            
        return title, logo_url, m3u8_url
    except Exception as e:
        logger.error(f"[!] Film işleme hatası ({movie_url}): {e}")
        return None

async def main():
    """
    Ana fonksiyon. Betiğin çalışma akışını yönetir.
    """
    start_time = time.time()
    
    # 1. Son eklenen filmleri (sayfa 1'den) al
    async with aiohttp.ClientSession() as session:
        newly_added_urls, _ = await get_movies_from_page(session, 1)
    
    if not newly_added_urls:
        logger.warning("[!] 'Son Eklenenler' için film bulunamadı (sayfa 1 boş olabilir).")

    # 2. Tüm filmleri al (tüm sayfalardan)
    all_movie_urls = await get_all_movie_links()
    if not all_movie_urls:
        logger.error("[!] Hiç film linki bulunamadı. Sitenin yapısı değişmiş olabilir.")
        return

    # 3. Her iki listeyi de işlemesi için ana fonksiyona gönder
    await process_all_movies(all_movie_urls, newly_added_urls, output_filename="Filmler.m3u")

    end_time = time.time()
    logger.info(f"\n[✓] Tüm işlemler tamamlandı. Toplam süre: {end_time - start_time:.2f} saniye")

if __name__ == "__main__":
    # Windows'ta asenkron olay döngüsü ile ilgili olası bir hatayı önlemek için
    if os.name == 'nt':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
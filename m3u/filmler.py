import asyncio
import aiohttp
import re
import os
from itertools import islice
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
import logging
from concurrent.futures import ThreadPoolExecutor
import time


logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# 1. Değişiklik: BASE_URL güncellendi
BASE_URL = "https://dizifun5.com/filmler"
PROXY_BASE_URL = "https://3.nejyoner19.workers.dev/"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "tr-TR,tr;q=0.8,en-US;q=0.5,en;q=0.3",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


def create_proxy_url(original_url):
    """Yeni proxy sistemine göre M3U8 URL'yi dönüştürür"""
    if not original_url:
        return None
    proxy_url = f"https://3.nejyoner19.workers.dev/?url={original_url}"
    logger.info(f"[PROXY] {original_url} -> {proxy_url}")
    return proxy_url


def sanitize_id(text):
    """Metni ID formatına dönüştürür - Türkçe karakterleri düzgün handle eder"""
    if not text:
        return "UNKNOWN"
    turkish_chars = {
        'ç': 'c', 'Ç': 'C', 'ğ': 'g', 'Ğ': 'G', 'ı': 'i', 'I': 'I',
        'İ': 'I', 'i': 'i', 'ö': 'o', 'Ö': 'O', 'ş': 's', 'Ş': 'S',
        'ü': 'u', 'Ü': 'U'
    }
    for turkish_char, english_char in turkish_chars.items():
        text = text.replace(turkish_char, english_char)
    import unicodedata
    text = unicodedata.normalize('NFD', text)
    text = ''.join(c for c in text if unicodedata.category(c) != 'Mn')
    text = re.sub(r'[^A-Za-z0-9\s]', '', text)
    text = re.sub(r'\s+', '_', text.strip())
    text = text.upper()
    text = re.sub(r'_+', '_', text)
    text = text.strip('_')
    return text if text else "UNKNOWN"

def fix_url(url, base="https://dizifun5.com"):
    """URL'yi düzeltir"""
    if not url:
        return None
    if url.startswith('/'):
        return urljoin(base, url)
    return url

async def fetch_page(session, url, timeout=45):  
    """Async olarak sayfa içeriğini getirir"""
    try:
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=timeout)) as response:
            if response.status == 200:
                return await response.text()
            else:
                logger.warning(f"[!] HTTP {response.status} hatası: {url}")
                return None
    except asyncio.TimeoutError:
        logger.error(f"[!] Timeout hatası ({timeout}s): {url}")
        return None
    except Exception as e:
        logger.error(f"[!] Sayfa getirme hatası ({url}): {e}")
        return None

async def extract_gujan_m3u8(session, gujan_iframe_url):
    """Gujan iframe'inden m3u8 URL'sini çıkarır"""
    try:
        if gujan_iframe_url.startswith("//"):
            gujan_iframe_url = "https:" + gujan_iframe_url
        logger.info(f"[GUJAN] İframe URL'sine istek atılıyor: {gujan_iframe_url}")
        content = await fetch_page(session, gujan_iframe_url)
        if not content:
            logger.warning(f"[GUJAN] İframe içeriği alınamadı: {gujan_iframe_url}")
            return None
        soup = BeautifulSoup(content, 'html.parser')
        source_element = soup.select_one('source[type="application/x-mpegURL"]')
        if source_element:
            m3u8_url = source_element.get('src')
            if m3u8_url:
                logger.info(f"[GUJAN] ✅ M3U8 URL bulundu: {m3u8_url}")
                return m3u8_url
        scripts = soup.find_all('script')
        for script in scripts:
            script_content = script.get_text(strip=True)
            m3u8_patterns = [
                r'https?://[^"\s]+/hls/[^"/\s]+/playlist\.m3u8',
                r'https?://[^"\s]+\.m3u8',
                r'"(https?://gujan\.premiumvideo\.click/hls/[^"]+)"'
            ]
            for pattern in m3u8_patterns:
                matches = re.findall(pattern, script_content)
                if matches:
                    m3u8_url = matches[0]
                    logger.info(f"[GUJAN] ✅ Script'ten M3U8 URL bulundu: {m3u8_url}")
                    return m3u8_url
        file_id_match = re.search(r'/e/([a-zA-Z0-9]+)', gujan_iframe_url)
        if file_id_match:
            file_id = file_id_match.group(1)
            constructed_m3u8 = f"https://gujan.premiumvideo.click/hls/{file_id}_o/playlist.m3u8"
            logger.info(f"[GUJAN] ✅ Constructed M3U8 URL: {constructed_m3u8}")
            return constructed_m3u8
        logger.warning(f"[GUJAN] ❌ M3U8 URL bulunamadı: {gujan_iframe_url}")
        return None
    except Exception as e:
        logger.error(f"[GUJAN] ❌ Hata: {e}")
        return None

async def get_correct_domain_from_playhouse(session, file_id, timeout=15):
    """Playhouse URL'ine istek atıp redirect edilen doğru domain'i bulur"""
    playhouse_url = f"https://playhouse.premiumvideo.click/player/{file_id}"
    try:
        logger.info(f"[*] Playhouse URL'ine redirect testi: {playhouse_url}")
        async with session.get(playhouse_url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=timeout), allow_redirects=True) as response:
            final_url = str(response.url)
            logger.info(f"[*] Final redirect URL: {final_url}")
            domain_match = re.search(r'https://([^.]+)\.premiumvideo\.click', final_url)
            if domain_match:
                domain = domain_match.group(1)
                logger.info(f"[✅] Redirect edilen domain bulundu: {domain}")
                m3u8_url = f"https://{domain}.premiumvideo.click/uploads/encode/{file_id}/master.m3u8"
                return domain, m3u8_url
            else:
                logger.warning(f"[⚠️] Redirect URL'den domain çıkarılamadı: {final_url}")
                return await find_working_domain_fallback(session, file_id)
    except Exception as e:
        logger.warning(f"[⚠️] Playhouse hatası: {e}, fallback sistem kullanılıyor")
        return await find_working_domain_fallback(session, file_id)

async def find_working_domain_fallback(session, file_id, domains=["d1", "d2", "d3", "d4"]):
    """Fallback: Eski sistem ile çalışan domain bulma"""
    logger.info(f"[*] Fallback domain testi başlıyor...")
    for domain in domains:
        m3u8_url = f"https://{domain}.premiumvideo.click/uploads/encode/{file_id}/master.m3u8"
        logger.info(f"[*] Fallback test: {domain}")
        try:
            async with session.head(m3u8_url, timeout=10, allow_redirects=True) as response:
                if response.status == 200:
                    logger.info(f"[✅] Fallback domain çalışıyor: {domain}")
                    return domain, m3u8_url
        except Exception:
            continue
    logger.warning(f"[⚠️] Hiçbir domain çalışmıyor! Default d2 kullanılacak.")
    return "d2", f"https://d2.premiumvideo.click/uploads/encode/{file_id}/master.m3u8"

async def get_movies_from_page(session, page_num):
    """Belirli bir sayfadan film listesini alır"""
    filmler_url = f"{BASE_URL}?p={page_num}"
    logger.info(f"Sayfa {page_num} alınıyor: {filmler_url}")

    content = await fetch_page(session, filmler_url)
    if not content:
        logger.warning(f"[!] Sayfa {page_num} alınamadı.")
        return [], False

    soup = BeautifulSoup(content, 'html.parser')
    movie_links = []
    # Selector film sayfasına göre güncellendi
    link_elements = soup.select("a.uk-position-cover[href*='/film/']")
    for element in link_elements:
        href = element.get("href")
        if href:
            full_url = fix_url(href)
            if full_url and full_url not in movie_links:
                movie_links.append(full_url)

    # Sonraki sayfa kontrolü
    has_next_page = soup.select_one(".uk-pagination .uk-pagination-next") is not None
    logger.info(f"[+] Sayfa {page_num}: {len(movie_links)} film linki toplandı. Sonraki sayfa: {'Var' if has_next_page else 'Yok'}")
    return movie_links, has_next_page

async def get_movies_from_homepage():
    """Tüm sayfalardan film listesini alır"""
    async with aiohttp.ClientSession() as session:
        all_movie_links = []
        page_num = 1
        max_pages = 100  
        while page_num <= max_pages:
            movie_links, has_next_page = await get_movies_from_page(session, page_num)
            if not movie_links:
                logger.info(f"[!] Sayfa {page_num} boş, tarama durduruluyor.")
                break
            
            new_count = 0
            for link in movie_links:
                if link not in all_movie_links:
                    all_movie_links.append(link)
                    new_count += 1
            
            logger.info(f"[+] Sayfa {page_num}: {new_count} yeni film eklendi. Toplam: {len(all_movie_links)}")
            if not has_next_page:
                logger.info(f"[✓] Son sayfa ({page_num}) işlendi.")
                break
            page_num += 1
            await asyncio.sleep(0.5)
        logger.info(f"[✓] Toplam {len(all_movie_links)} benzersiz film linki toplandı ({page_num-1} sayfa tarandı).")
        return all_movie_links

async def get_movie_metadata(session, movie_url):
    """Film meta verilerini alır"""
    content = await fetch_page(session, movie_url)
    if not content:
        return "Bilinmeyen Film", ""
    soup = BeautifulSoup(content, 'html.parser')
    title_element = soup.select_one(".text-bold")
    title = title_element.get_text(strip=True) if title_element else "Bilinmeyen Film"
    logo_url = ""
    logo_element = soup.select_one(".media-cover img")
    if logo_element:
        logo_url = logo_element.get("src") or ""
    logo_url = fix_url(logo_url)
    return title, logo_url

async def extract_m3u8_from_movie_page(session, movie_url):
    """Film sayfasından m3u8 linkini çıkarır"""
    content = await fetch_page(session, movie_url)
    if not content:
        return None
    soup = BeautifulSoup(content, 'html.parser')
    m3u8_url = None
    try:
        gujan_iframe_selectors = [
            'iframe[title="dizifunplay"]',
            'iframe[id="altPlayerFrame"]',
            'iframe[src*="gujan.premiumvideo.click"]'
        ]
        for selector in gujan_iframe_selectors:
            iframe_element = soup.select_one(selector)
            if iframe_element:
                src = iframe_element.get("src")
                if src and "gujan.premiumvideo.click" in src:
                    logger.info(f"[+] Gujan iframe bulundu: {src}")
                    m3u8_url = await extract_gujan_m3u8(session, src)
                    if m3u8_url:
                        logger.info(f"[✅] Gujan'dan M3U8 başarıyla alındı!")
                        break
        if not m3u8_url:
            logger.info("[*] Gujan bulunamadı, Playhouse sistemi deneniyor...")
            iframe_selectors = [
                'iframe[title="playhouse"]',
                'iframe[src*="playhouse.premiumvideo.click"]',
                'iframe[src*="premiumvideo.click/player"]'
            ]
            playhouse_url = None
            for selector in iframe_selectors:
                iframe_element = soup.select_one(selector)
                if iframe_element:
                    src = iframe_element.get("src")
                    if src and "playhouse.premiumvideo.click" in src:
                        if src.startswith("//"):
                            src = "https:" + src
                        playhouse_url = src
                        logger.info(f"[+] Playhouse iframe bulundu: {playhouse_url}")
                        break
            if playhouse_url:
                playhouse_match = re.search(r'playhouse\.premiumvideo\.click/player/([a-zA-Z0-9]+)', playhouse_url)
                if playhouse_match:
                    file_id = playhouse_match.group(1)
                    logger.info(f"[+] Playhouse File ID bulundu: {file_id}")
                    _, m3u8_url = await get_correct_domain_from_playhouse(session, file_id)
                    logger.info(f"[+] M3U8: {m3u8_url}")
    except Exception as e:
        logger.error(f"[!] Film işleme genel hatası: {e}")
        return None
    if m3u8_url:
        m3u8_url = create_proxy_url(m3u8_url)
    return m3u8_url

async def process_movies(all_movie_links, output_filename="Filmler.m3u"):
    """Tüm filmleri tek bir dosyaya yazar"""
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=10)) as session:
        with open(output_filename, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")

            for movie_url in all_movie_links:
                try:
                    title, logo_url = await get_movie_metadata(session, movie_url)
                    logger.info(f"\n[+] İşleniyor: {title}")

                    m3u8_url = await extract_m3u8_from_movie_page(session, movie_url)

                    if not m3u8_url:
                        logger.warning(f"[!] m3u8 URL bulunamadı: {movie_url}")
                        continue
                    
                    tvg_id = sanitize_id(title)

                    # 2. Değişiklik: group-title kaldırıldı
                    f.write(
                        f'#EXTINF:-1 tvg-name="{title}" '
                        f'tvg-language="Turkish" tvg-country="TR" '
                        f'tvg-id="{tvg_id}" '
                        f'tvg-logo="{logo_url}",{title}\n'
                    )
                    f.write(m3u8_url.strip() + "\n")
                    logger.info(f"[✓] {title} eklendi.")

                except Exception as e:
                    logger.error(f"[!] Film işleme hatası ({movie_url}): {e}")
                    continue
    logger.info(f"\n[✓] {output_filename} dosyası oluşturuldu.")

async def main():
    start_time = time.time()
    movie_urls = await get_movies_from_homepage()
    if not movie_urls:
        logger.error("[!] Film listesi boş, seçicileri kontrol et.")
        return
    await process_movies(movie_urls)
    end_time = time.time()
    logger.info(f"\n[✓] Tüm işlemler tamamlandı. Süre: {end_time - start_time:.2f} saniye")

if __name__ == "__main__":
    asyncio.run(main())
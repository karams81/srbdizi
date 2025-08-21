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


BASE_URL = "https://dizifun5.com"
PROXY_BASE = "https://3.nejyoner19.workers.dev/?url="  # PROXY ADRESİ DEĞİŞTİRİLDİ
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "tr-TR,tr;q=0.8,en-US;q=0.5,en;q=0.3",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


def create_proxy_url(original_url):
    """Orijinal URL'yi proxy üzerinden geçirir"""
    if not original_url:
        return None
    
    
    if PROXY_BASE in original_url:
        return original_url
    
    
    proxy_url = f"{PROXY_BASE}{original_url}"  # BASİT BİR ŞEKİLDE DEĞİŞTİRİLDİ
    logger.info(f"[+] Proxy URL oluşturuldu: {proxy_url}")
    return proxy_url


def sanitize_id(text):
    """Metni ID formatına dönüştürür - Türkçe karakterleri düzgün handle eder"""
    if not text:
        return "UNKNOWN"
    
    
    turkish_chars = {
        'ç': 'c', 'Ç': 'C',
        'ğ': 'g', 'Ğ': 'G', 
        'ı': 'i', 'I': 'I',
        'İ': 'I', 'i': 'i',
        'ö': 'o', 'Ö': 'O',
        'ş': 's', 'Ş': 'S',
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

def fix_url(url, base=BASE_URL):
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
                content = await response.text()
                return content
            else:
                logger.warning(f"[!] HTTP {response.status} hatası: {url}")
                return None
    except asyncio.TimeoutError:
        logger.error(f"[!] Timeout hatası ({timeout}s): {url}")
        return None
    except Exception as e:
        logger.error(f"[!] Sayfa getirme hatası ({url}): {e}")
        return None

async def extract_gujan_m3u8(session, iframe_url, file_id):
    """Gujan player'dan M3U8 URL'ini çıkarır"""
    try:
        
        logger.info(f"[*] Gujan player sayfası getiriliyor: {iframe_url}")
        
        
        if iframe_url.startswith("//"):
            iframe_url = "https:" + iframe_url
        
        content = await fetch_page(session, iframe_url)
        if not content:
            logger.warning(f"[!] Gujan player sayfası alınamadı: {iframe_url}")
            return None
        
        
        m3u8_url = f"https://gujan.premiumvideo.click/hls/{file_id}_o/playlist.m3u8"
        
        
        is_valid = await test_m3u8_url(session, m3u8_url)
        if is_valid:
            logger.info(f"[✅] Gujan M3U8 URL doğrulandı: {m3u8_url}")
            return m3u8_url
        else:
            logger.warning(f"[⚠️] Gujan M3U8 URL doğrulanamadı: {m3u8_url}")
            return m3u8_url  
    
    except Exception as e:
        logger.error(f"[!] Gujan M3U8 çıkarma hatası: {e}")
        return None

async def get_correct_domain_from_playhouse(session, file_id, timeout=15):
    """Playhouse URL'ine istek atıp redirect edilen doğru domain'i bulur"""
    playhouse_url = f"https://playhouse.premiumvideo.click/player/{file_id}"
    
    try:
        logger.info(f"[*] Playhouse URL'ine redirect testi: {playhouse_url}")
        
        async with session.get(playhouse_url, 
                              headers=HEADERS, 
                              timeout=aiohttp.ClientTimeout(total=timeout),
                              allow_redirects=True) as response:
            
            final_url = str(response.url)
            logger.info(f"[*] Final redirect URL: {final_url}")
            
            domain_match = re.search(r'https://([^.]+)\.premiumvideo\.click', final_url)
            if domain_match:
                domain = domain_match.group(1)
                logger.info(f"[✅] Redirect edilen domain bulundu: {domain}")
                
                m3u8_url = f"https://{domain}.premiumvideo.click/uploads/encode/{file_id}/master.m3u8"
                
                is_valid = await test_m3u8_url(session, m3u8_url)
                if is_valid:
                    logger.info(f"[✅] M3U8 URL doğrulandı: {m3u8_url}")
                    return domain, m3u8_url
                else:
                    logger.warning(f"[⚠️] M3U8 URL doğrulanamadı ama domain bulundu: {domain}")
                    return domain, m3u8_url
            else:
                logger.warning(f"[⚠️] Redirect URL'den domain çıkarılamadı: {final_url}")
                logger.info(f"[*] Fallback: Eski domain test sistemi kullanılıyor")
                return await find_working_domain_fallback(session, file_id)
                
    except asyncio.TimeoutError:
        logger.warning(f"[⚠️] Playhouse timeout, fallback sistem kullanılıyor")
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
        is_working = await test_m3u8_url(session, m3u8_url)
        
        if is_working:
            logger.info(f"[✅] Fallback domain çalışıyor: {domain}")
            return domain, m3u8_url
    
    logger.warning(f"[⚠️] Hiçbir domain çalışmıyor! Default d2 kullanılacak.")
    return "d2", f"https://d2.premiumvideo.click/uploads/encode/{file_id}/master.m3u8"

async def test_m3u8_url(session, url, timeout=15):
    """M3U8 URL test fonksiyonu"""
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout), allow_redirects=True) as response:
            final_url = str(response.url)
            content_type = response.headers.get("Content-Type", "").lower()
            content_length = response.headers.get("Content-Length")
            
            if response.status != 200:
                return False
            
            if "premiumvideo.click" not in final_url:
                return False
            
            try:
                content = await response.content.read(4096)
                text = content.decode('utf-8', errors='ignore')
                
                if not text.strip().startswith("#EXTM3U"):
                    return False
                
                suspicious_patterns = [
                    r"<html", r"<body", r"<title", r"error", r"not found", 
                    r"access denied", r"kerimkirac\.com", r"404", r"403", r"500"
                ]
                
                for pattern in suspicious_patterns:
                    if re.search(pattern, text, re.IGNORECASE):
                        return False
                
                if content_length and int(content_length) < 50:
                    return False
                
                return True
                
            except UnicodeDecodeError:
                return False
                
    except asyncio.TimeoutError:
        return False
    except Exception as e:
        return False

async def get_movies_from_page(session, page_num):
    """Belirli bir sayfadan film listesini alır"""
    filmler_url = f"{BASE_URL}/filmler?p={page_num}"
    logger.info(f"Sayfa {page_num} alınıyor: {filmler_url}")
    
    content = await fetch_page(session, filmler_url)
    if not content:
        logger.warning(f"[!] Film sayfası {page_num} alınamadı.")
        return [], False
    
    soup = BeautifulSoup(content, 'html.parser')
    
    
    movie_links = []
    link_elements = soup.select("a.uk-position-cover[href*='/film/']")

    for element in link_elements:
        href = element.get("href")
        if href:
            full_url = fix_url(href)
            if full_url and full_url not in movie_links:
                movie_links.append(full_url)
    
    
    if not movie_links:
        alt_selectors = [
            ".uk-grid .uk-width-large-1-6 a[href*='/film/']",
            ".uk-grid .uk-width-large-1-5 a[href*='/film/']",
            "a[href*='/film/']"
        ]
        
        for selector in alt_selectors:
            elements = soup.select(selector)
            for element in elements:
                href = element.get("href")
                if href:
                    full_url = fix_url(href)
                    if full_url and full_url not in movie_links:
                        movie_links.append(full_url)
    
    
    has_next_page = False
    pagination_selectors = [
        ".uk-pagination .uk-pagination-next",
        ".pagination .next",
        "a[href*='?p=']",
        ".uk-pagination a"
    ]
    
    for selector in pagination_selectors:
        pagination_elements = soup.select(selector)
        for element in pagination_elements:
            href = element.get("href", "")
            if href and f"?p={page_num + 1}" in href:
                has_next_page = True
                break
        if has_next_page:
            break
    
    
    if not has_next_page and movie_links:
        next_page_url = f"{BASE_URL}/filmler?p={page_num + 1}"
        next_content = await fetch_page(session, next_page_url)
        if next_content:
            next_soup = BeautifulSoup(next_content, 'html.parser')
            next_links = next_soup.select("a[href*='/film/']")
            if next_links:
                has_next_page = True
    
    logger.info(f"[+] Sayfa {page_num}: {len(movie_links)} film linki toplandı. Sonraki sayfa: {'Var' if has_next_page else 'Yok'}")
    return movie_links, has_next_page

async def get_movies_from_homepage():
    """Tüm sayfalardan film listesini alır"""
    async with aiohttp.ClientSession() as session:
        all_movie_links = []
        page_num = 1
        max_pages = 1000  # TÜM SAYFALARI TARAMAK İÇİN YÜKSEK BİR SINIR
        
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
        
        logger.info(f"[✓] Toplam {len(all_movie_links)} benzersiz film linki toplandı ({page_num} sayfa tarandı).")
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

async def extract_m3u8_from_movie(session, movie_url):
    """Film sayfasından m3u8 linkini çıkarır"""
    content = await fetch_page(session, movie_url)
    if not content:
        return None
    
    soup = BeautifulSoup(content, 'html.parser')
    
    logger.info(f"[*] Film işleniyor: {movie_url}")
    
    m3u8_url = None
    
    try:
        
        gujan_iframe = soup.select_one('iframe[title="dizifunplay"]')
        if gujan_iframe:
            src = gujan_iframe.get("src")
            if src and "gujan.premiumvideo.click/e/" in src:
                logger.info(f"[+] Gujan player iframe bulundu: {src}")
                
                
                gujan_match = re.search(r'gujan\.premiumvideo\.click/e/([a-zA-Z0-9]+)', src)
                if gujan_match:
                    file_id = gujan_match.group(1)
                    logger.info(f"[+] Gujan File ID: {file_id}")
                    
                    
                    m3u8_url = await extract_gujan_m3u8(session, src, file_id)
                    if m3u8_url:
                        logger.info(f"[✅] Gujan M3U8 bulundu: {m3u8_url}")
                        return create_proxy_url(m3u8_url)
        
        
        if not m3u8_url:
            iframe_selectors = [
                'iframe[title="playhouse"]',
                'iframe[src*="playhouse.premiumvideo.click"]',
                'iframe[src*="premiumvideo.click/player"]'
            ]
            
            playhouse_url = None
            file_id = None
            
            
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
            
            
            if not playhouse_url:
                scripts = soup.find_all('script')
                for script in scripts:
                    script_content = script.get_text() or ""
                    
                    hex_pattern = re.compile(r'hexToString\w*\("([a-fA-F0-9]+)"\)')
                    hex_matches = hex_pattern.findall(script_content)
                    
                    if hex_matches:
                        logger.info(f"[+] Script içinde {len(hex_matches)} hex URL bulundu.")
                        for hex_value in hex_matches:
                            try:
                                decoded_url = bytes.fromhex(hex_value).decode('utf-8')
                                if decoded_url and "playhouse.premiumvideo.click" in decoded_url:
                                    playhouse_url = decoded_url
                                    if playhouse_url.startswith("//"):
                                        playhouse_url = "https:" + playhouse_url
                                    logger.info(f"[+] Hex'ten çözülen playhouse URL: {playhouse_url}")
                                    break
                            except Exception as e:
                                logger.error(f"[!] Hex çözme hatası: {e}")
                        
                        if playhouse_url:
                            break
            
            
            if playhouse_url:
                playhouse_match = re.search(r'playhouse\.premiumvideo\.click/player/([a-zA-Z0-9]+)', playhouse_url)
                if playhouse_match:
                    file_id = playhouse_match.group(1)
                    logger.info(f"[+] Playhouse File ID bulundu: {file_id}")
                    
                    
                    working_domain, m3u8_url = await get_correct_domain_from_playhouse(session, file_id)
                    logger.info(f"[+] Bulunan domain: {working_domain}, M3U8: {m3u8_url}")
        
        # 3. FALLBACK KONTROLÜ
        if not m3u8_url:
            logger.info("[*] Playhouse ve Gujan bulunamadı, fallback sistem ile deneniyor...")
            
            iframe_selectors_fallback = [
                "iframe#londonIframe",
                "iframe[src*=premiumvideo]",
                "iframe[data-src*=premiumvideo]",
                "iframe[src*=player]",
                "iframe"
            ]
            
            for selector in iframe_selectors_fallback:
                iframe_element = soup.select_one(selector)
                if iframe_element:
                    src = iframe_element.get("src")
                    if not src or src == "about:blank":
                        src = iframe_element.get("data-src")
                    
                    if src and src != "about:blank":
                        iframe_url = fix_url(src)
                        logger.info(f"[+] Fallback iframe URL: {iframe_url}")
                        
                        
                        premium_video_match = re.search(r'premiumvideo\.click/player\.php\?file_id=([a-zA-Z0-9]+)', iframe_url)
                        if premium_video_match:
                            file_id = premium_video_match.group(1)
                            logger.info(f"[+] Fallback File ID: {file_id}")
                            
                            
                            working_domain, m3u8_url = await find_working_domain_fallback(session, file_id)
                            break
    
    except Exception as e:
        logger.error(f"[!] Film işleme genel hatası: {e}")
    
    
    if m3u8_url:
        return create_proxy_url(m3u8_url)
    
    return None

async def process_movies(all_movie_links, output_filename="filmfun.m3u"):
    """Tüm filmleri tek bir dosyaya yazar"""
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=10)) as session:
        with open(output_filename, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
            
            
            semaphore = asyncio.Semaphore(5)
            
            async def process_single_movie(movie_url):
                async with semaphore:
                    try:
                        
                        title, logo_url = await get_movie_metadata(session, movie_url)
                        logger.info(f"\n[+] İşleniyor: {title}")
                        
                        
                        m3u8_url = await extract_m3u8_from_movie(session, movie_url)
                        
                        if m3u8_url:
                            tvg_id = sanitize_id(title)
                            
                            return {
                                'title': title,
                                'logo_url': logo_url,
                                'tvg_id': tvg_id,
                                'm3u8_url': m3u8_url
                            }
                        else:
                            logger.warning(f"[!] m3u8 URL bulunamadı: {title}")
                            return None
                    
                    except Exception as e:
                        logger.error(f"[!] Film işleme hatası ({movie_url}): {e}")
                        return None
            
            
            tasks = [process_single_movie(movie_url) for movie_url in all_movie_links]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            
            successful_count = 0
            for result in results:
                if isinstance(result, Exception):
                    logger.error(f"[!] Task hatası: {result}")
                    continue
                
                if result is None:
                    continue
                
                f.write(
                    f'#EXTINF:-1 tvg-name="{result["title"]}" '
                    f'tvg-language="Turkish" tvg-country="TR" '
                    f'tvg-id="{result["tvg_id"]}" '
                    f'tvg-logo="{result["logo_url"]}" '
                    f'group-title="Filmler",{result["title"]}\n'
                )
                f.write(result["m3u8_url"].strip() + "\n")
                logger.info(f"[✓] {result['title']} eklendi.")
                successful_count += 1

            logger.info(f"\n[✓] {successful_count} film başarıyla eklendi.")

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
    

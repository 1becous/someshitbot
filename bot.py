import os
import re
import logging
import asyncio
import aiohttp

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandStart
from aiogram.types import BufferedInputFile, InputMediaPhoto
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

# Завантаження змінних оточення (Railway підставляє їх автоматично)
BOT_TOKEN = os.getenv("BOT_TOKEN")
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID")
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(x.strip()) for x in ADMIN_IDS_RAW.split(",") if x.strip()]

# Налаштування логування
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Ініціалізація бота
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# Регулярні вирази для лінків (додано прапорець IGNORECASE)
TWITTER_REGEX = re.compile(r'https?://(?:www\.)?(?:twitter|x)\.com/([\w_]+)/status/(\d+)', re.IGNORECASE)
PIXIV_REGEX = re.compile(r'https?://(?:www\.)?pixiv\.net/(?:[\w-]+/)?artworks/(\d+)', re.IGNORECASE)
THREADS_REGEX = re.compile(r'https?://(?:www\.)?threads\.(?:net|com)/(?:@(?P<user>[\w_.]+)/post/|t/)(?P<id>[\w_-]+)', re.IGNORECASE)
INSTAGRAM_REGEX = re.compile(r'https?://(?:www\.)?instagram\.com/(?:[^/]+/)?(?:p|reel|reels)/([\w_-]+)', re.IGNORECASE)

def get_crawler_headers() -> dict:
    """
    Генерує заголовки офіційного краулера Facebook.
    Meta завжди віддає краулерам чистий HTML з оригінальними посиланнями на медіа
    і ніколи не перенаправляє їх на сторінку логіну.
    """
    return {
        "User-Agent": "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uated.html)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9"
    }

def get_browser_headers(referer: str = None) -> dict:
    """Генерує стандартні заголовки браузера для скачування картинок з CDN"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "image",
        "Sec-Fetch-Mode": "no-cors",
        "Sec-Fetch-Site": "cross-site"
    }
    if referer:
        headers["Referer"] = referer
    return headers

def extract_meta_images(html: str) -> list:
    """Витягує чисті оригінальні посилання на зображення з OpenGraph метатегів"""
    photo_urls = []
    meta_tags = re.findall(r'<meta\s+[^>]*>', html, re.IGNORECASE)
    for tag in meta_tags:
        tag_lower = tag.lower()
        # Шукаємо виключно оригінальні зображення в og:image або twitter:image
        if 'og:image' in tag_lower or 'twitter:image' in tag_lower or 'og:image:secure_url' in tag_lower:
            content_match = re.search(r'content=["\']([^"\']+)["\']', tag, re.IGNORECASE)
            if content_match:
                # КРИТИЧНО: Заміна &amp; на & для валідності посилань Meta CDN
                url = content_match.group(1).replace("&amp;", "&")
                # Уникаємо технічних логотипів, іконок чи прев'ю-карток сторонніх сервісів
                if url and "pb=" not in url and "static" not in url and url not in photo_urls:
                    photo_urls.append(url)
    return photo_urls

def extract_meta_title(html: str) -> str:
    """Витягує заголовок сторінки для визначення імені автора"""
    meta_tags = re.findall(r'<meta\s+[^>]*>', html, re.IGNORECASE)
    for tag in meta_tags:
        if 'title' in tag.lower():
            content_match = re.search(r'content=["\']([^"\']+)["\']', tag, re.IGNORECASE)
            if content_match:
                return content_match.group(1)
    return ""

async def download_file(url: str, headers: dict = None) -> bytes:
    """Завантажує файл у бінарний буфер без збереження на диск"""
    if not headers:
        headers = get_browser_headers()
        
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers, timeout=30, ssl=False) as response:
                if response.status == 200:
                    return await response.read()
                else:
                    logger.error(f"❌ ПОМИЛКА ЗАВАНТАЖЕННЯ ФАЙЛУ: статус {response.status} для URL: {url[:80]}...")
        except Exception as e:
            logger.error(f"💥 Виняток при завантаженні файлу: {e}")
    return b""

async def get_twitter_media(status_id: str, author_handle: str, original_url: str):
    """Отримує медіа та дані автора з Twitter за допомогою fxtwitter API"""
    api_url = f"https://api.fxtwitter.com/status/{status_id}"
    logger.info(f"🔍 Запит до Twitter API: {api_url}")
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(api_url, timeout=15) as response:
                logger.info(f"📡 Twitter API статус відповіді: {response.status}")
                if response.status != 200:
                    return None
                    
                data = await response.json()
                tweet = data.get("tweet", {})
                if not tweet:
                    tweet = data
                
                author = tweet.get("author", {})
                user_name = author.get("name", author_handle)
                user_screen_name = author.get("screen_name", author_handle)
                author_link = f"https://x.com/{user_screen_name}"
                
                media = tweet.get("media", {})
                photos = media.get("photos", [])
                photo_urls = [p.get("url") for p in photos if p.get("type") == "photo"]
                
                if not photo_urls and "attachments" in data:
                    photo_urls = [att.get("url") for att in data["attachments"] if att.get("type") == "photo"]
                
                return {
                    "author_name": f"{user_name} (@{user_screen_name})",
                    "author_link": author_link,
                    "media_urls": photo_urls,
                    "source_url": original_url
                }
        except Exception as e:
            logger.error(f"💥 Помилка при запиті до Twitter API: {e}")
    return None

async def get_pixiv_media(illust_id: str, original_url: str):
    """Отримує оригінальний арт з Pixiv за допомогою проксі-сервісу pixiv.cat"""
    api_url = f"https://www.pixiv.net/ajax/illust/{illust_id}"
    logger.info(f"🔍 Запит до Pixiv API: {api_url}")
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Referer": "https://www.pixiv.net/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9"
    }
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(api_url, headers=headers, timeout=15) as response:
                logger.info(f"📡 Pixiv API статус відповіді: {response.status}")
                
                if response.status != 200:
                    logger.warning("⚠️ Pixiv API повернув помилку. Вмикаю аварійний режим...")
                    return {
                        "author_name": "Pixiv Artist",
                        "author_link": f"https://www.pixiv.net/artworks/{illust_id}",
                        "media_urls": [f"https://pixiv.cat/{illust_id}.png"],
                        "source_url": original_url
                    }
                    
                data = await response.json()
                if data.get("error"):
                    return None
                
                body = data.get("body", {})
                author_name = body.get("userName", "Unknown Pixiv Artist")
                author_id = body.get("userId", "")
                author_link = f"https://www.pixiv.net/users/{author_id}" if author_id else "https://www.pixiv.net"
                
                page_count = body.get("pageCount", 1)
                
                photo_urls = []
                if page_count == 1:
                    photo_urls.append(f"https://pixiv.cat/{illust_id}.png")
                else:
                    for p in range(1, page_count + 1):
                        photo_urls.append(f"https://pixiv.cat/{illust_id}-{p}.png")
                
                return {
                    "author_name": author_name,
                    "author_link": author_link,
                    "media_urls": photo_urls,
                    "source_url": original_url
                }
        except Exception as e:
            logger.error(f"💥 Помилка Pixiv API: {e}")
            return {
                "author_name": "Pixiv Artist",
                "author_link": f"https://www.pixiv.net/artworks/{illust_id}",
                "media_urls": [f"https://pixiv.cat/{illust_id}.png"],
                "source_url": original_url
            }

async def get_threads_media(username: str, post_id: str, original_url: str):
    """Отримує чистий оригінальний арт з Threads напряму, минаючи проксі-сервери"""
    # Нормалізуємо посилання на офіційний threads.net (замість threads.com)
    if username:
        direct_url = f"https://www.threads.net/@{username}/post/{post_id}"
    else:
        direct_url = f"https://www.threads.net/t/{post_id}"
        
    logger.info(f"🔍 Пряме сканування Threads: {direct_url}")
    
    headers = get_crawler_headers()
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(direct_url, headers=headers, timeout=15, ssl=False) as response:
                logger.info(f"📡 Threads Direct статус відповіді: {response.status}")
                if response.status != 200:
                    return None
                
                html = await response.text()
                photo_urls = extract_meta_images(html)
                
                if not photo_urls:
                    logger.warning("⚠️ Не знайдено зображень на сторінці Threads.")
                    return None
                
                # Визначаємо нікнейм автора
                raw_title = extract_meta_title(html)
                author_name = f"@{username}" if username else "Threads Artist"
                
                if raw_title:
                    # Прибираємо "on Threads" з заголовку сторінки
                    author_name = raw_title.split("on Threads")[0].strip() if "on Threads" in raw_title else raw_title
                
                if (not username or username == "None") and "@" in author_name:
                    user_match = re.search(r'@([\w_.]+)', author_name)
                    if user_match:
                        username = user_match.group(1)
                
                author_link = f"https://www.threads.net/@{username}" if username else "https://www.threads.net"
                
                logger.info(f"🖼️ Знайдено оригінальних зображень Threads: {len(photo_urls)}")
                return {
                    "author_name": author_name,
                    "author_link": author_link,
                    "media_urls": photo_urls,
                    "source_url": original_url
                }
        except Exception as e:
            logger.error(f"💥 Помилка прямого сканування Threads: {e}")
    return None

async def get_instagram_media(code: str, original_url: str):
    """Отримує чистий оригінальний арт з Instagram напряму, минаючи проксі-сервери"""
    direct_url = f"https://www.instagram.com/p/{code}/"
    logger.info(f"🔍 Пряме сканування Instagram: {direct_url}")
    
    headers = get_crawler_headers()
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(direct_url, headers=headers, timeout=15, ssl=False) as response:
                logger.info(f"📡 Instagram Direct статус відповіді: {response.status}")
                if response.status != 200:
                    return None
                    
                html = await response.text()
                photo_urls = extract_meta_images(html)
                
                if not photo_urls:
                    logger.warning("⚠️ Не знайдено зображень на сторінці Instagram.")
                    return None
                
                # Визначення нікнейму автора
                author_name = "Instagram Artist"
                author_link = "https://www.instagram.com"
                
                raw_title = extract_meta_title(html)
                if raw_title:
                    author_name = raw_title
                    user_match = re.search(r'@([\w_.]+)', raw_title)
                    if user_match:
                        username = user_match.group(1)
                        author_link = f"https://www.instagram.com/{username}"
                        author_name = f"@{username}"
                
                logger.info(f"🖼️ Знайдено оригінальних зображень Instagram: {len(photo_urls)}")
                return {
                    "author_name": author_name,
                    "author_link": author_link,
                    "media_urls": photo_urls,
                    "source_url": original_url
                }
        except Exception as e:
            logger.error(f"💥 Помилка прямого сканування Instagram: {e}")
    return None

def is_admin(user_id: int) -> bool:
    return not ADMIN_IDS or user_id in ADMIN_IDS

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    await message.reply(
        "👋 Бот повністю оновлений!\n\n"
        "Тепер для Instagram та Threads використовується чисте пряме "
        "завантаження оригінальних файлів без водяних знаків та прев'ю-карток!"
    )

@dp.message(F.text)
async def handle_links(message: types.Message):
    if message.chat.type == "private" and not is_admin(message.from_user.id):
        return

    text = message.text
    twitter_match = TWITTER_REGEX.search(text)
    pixiv_match = PIXIV_REGEX.search(text)
    threads_match = THREADS_REGEX.search(text)
    instagram_match = INSTAGRAM_REGEX.search(text)
    
    if not any([twitter_match, pixiv_match, threads_match, instagram_match]):
        return

    status_msg = await message.reply("⏳ Опрацьовую лінк та завантажую чисте медіа...")
    media_data = None
    
    # Стандартні заголовки для скачування файлів з CDN
    headers_for_download = get_browser_headers()

    # 1. Твіттер
    if twitter_match:
        author_handle, status_id = twitter_match.groups()
        original_url = twitter_match.group(0)
        media_data = await get_twitter_media(status_id, author_handle, original_url)
        
    # 2. Pixiv
    elif pixiv_match:
        illust_id = pixiv_match.group(1)
        original_url = pixiv_match.group(0)
        media_data = await get_pixiv_media(illust_id, original_url)
        headers_for_download = {}  # Для pixiv.cat заголовки не потрібні
        
    # 3. Threads
    elif threads_match:
        username = threads_match.group("user")
        post_id = threads_match.group("id")
        original_url = threads_match.group(0)
        media_data = await get_threads_media(username, post_id, original_url)
        headers_for_download = get_browser_headers("https://www.threads.net/")
        
    # 4. Instagram
    elif instagram_match:
        code = instagram_match.group(1)
        original_url = instagram_match.group(0)
        media_data = await get_instagram_media(code, original_url)
        headers_for_download = get_browser_headers("https://www.instagram.com/")

    # Перевірка отриманих даних
    if not media_data or not media_data.get("media_urls"):
        await status_msg.edit_text("❌ Не вдалося отримати медіа-файли за цим посиланням.")
        return

    author_name = media_data["author_name"]
    author_link = media_data["author_link"]
    source_url = media_data["source_url"]
    urls = media_data["media_urls"]

    if len(urls) > 10:
        urls = urls[:10]
        warning_suffix = "\n⚠️ <i>(Показано перші 10 зображень)</i>"
    else:
        warning_suffix = ""

    await status_msg.edit_text(f"📥 Завантажую чисте зображення ({len(urls)} шт.)...")

    downloaded_images = []
    for url in urls:
        logger.info(f"⬇️ Завантаження файлу: {url[:80]}...")
        img_bytes = await download_file(url, headers=headers_for_download)
        if img_bytes:
            downloaded_images.append(img_bytes)

    if not downloaded_images:
        await status_msg.edit_text("❌ Помилка: Не вдалося завантажити картинки з серверів.")
        return

    caption_text = f"🎨 Автор: <a href='{author_link}'>{author_name}</a>\n🔗 <a href='{source_url}'>Джерело</a>{warning_suffix}"

    try:
        if len(downloaded_images) == 1:
            photo = BufferedInputFile(downloaded_images[0], filename="artwork.jpg")
            await bot.send_photo(chat_id=TARGET_CHAT_ID, photo=photo, caption=caption_text)
        else:
            media_group = []
            for idx, img_bytes in enumerate(downloaded_images):
                caption = caption_text if idx == 0 else None
                media_group.append(
                    InputMediaPhoto(
                        media=BufferedInputFile(img_bytes, filename=f"artwork_{idx}.jpg"),
                        caption=caption
                    )
                )
            await bot.send_media_group(chat_id=TARGET_CHAT_ID, media=media_group)

        await status_msg.edit_text("✅ Арт успішно опубліковано!")
    except Exception as e:
        logger.error(f"💥 Помилка Telegram відправки: {e}")
        await status_msg.edit_text(f"❌ Помилка відправки в Telegram: {e}")

async def main():
    logger.info("Старт бота на Railway...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

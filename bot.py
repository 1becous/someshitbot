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

# Завантаження змінних оточення (Railway автоматично підставляє їх)
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

# Регулярні вирази для лінків
TWITTER_REGEX = r'https?://(?:www\.)?(?:twitter|x)\.com/([\w_]+)/status/(\d+)'
PIXIV_REGEX = r'https?://(?:www\.)?pixiv\.net/(?:[\w-]+/)?artworks/(\d+)'

async def download_file(url: str, headers: dict = None) -> bytes:
    """Завантажує файл у бінарний буфер. Працює стабільно завдяки pixiv.cat"""
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers, timeout=30) as response:
                if response.status == 200:
                    return await response.read()
                else:
                    logger.error(f"❌ ПОМИЛКА ЗАВАНТАЖЕННЯ: статус {response.status} для URL: {url}")
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
    """Отримує медіа та дані автора з Pixiv за допомогою AJAX API та обходу блокувань через pixiv.cat"""
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
                
                # Якщо офіційне API заблоковано або недоступне, вмикаємо режим авто-обходу
                if response.status != 200:
                    logger.warning("⚠️ Pixiv API повернув помилку. Вмикаю аварійний режим обходу...")
                    return {
                        "author_name": "Pixiv Artist",
                        "author_link": f"https://www.pixiv.net/artworks/{illust_id}",
                        "media_urls": [f"https://pixiv.cat/{illust_id}.png"],
                        "source_url": original_url
                    }
                    
                data = await response.json()
                if data.get("error"):
                    logger.error(f"❌ Pixiv повернув внутрішню помилку: {data.get('message')}")
                    return None
                
                body = data.get("body", {})
                author_name = body.get("userName", "Unknown Pixiv Artist")
                author_id = body.get("userId", "")
                author_link = f"https://www.pixiv.net/users/{author_id}" if author_id else "https://www.pixiv.net"
                
                page_count = body.get("pageCount", 1)
                
                # Замість оригінальних лінків i.pximg.net ми будуємо посилання на проксі pixiv.cat
                photo_urls = []
                if page_count == 1:
                    photo_urls.append(f"https://pixiv.cat/{illust_id}.png")
                else:
                    # pixiv.cat використовує 1-індексацію для багатосторінкових артів (наприклад, 12345-1.png, 12345-2.png)
                    for p in range(1, page_count + 1):
                        photo_urls.append(f"https://pixiv.cat/{illust_id}-{p}.png")
                
                return {
                    "author_name": author_name,
                    "author_link": author_link,
                    "media_urls": photo_urls,
                    "source_url": original_url
                }
        except Exception as e:
            logger.error(f"💥 Помилка при запиті до Pixiv API: {e}")
            # Аварійний режим у випадку повної відмови мережі до Pixiv
            return {
                "author_name": "Pixiv Artist",
                "author_link": f"https://www.pixiv.net/artworks/{illust_id}",
                "media_urls": [f"https://pixiv.cat/{illust_id}.png"],
                "source_url": original_url
            }

def is_admin(user_id: int) -> bool:
    return not ADMIN_IDS or user_id in ADMIN_IDS

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    await message.reply("👋 Бот активований та використовує технологію обходу Pixiv-блокувань!")

@dp.message(F.text)
async def handle_links(message: types.Message):
    if message.chat.type == "private" and not is_admin(message.from_user.id):
        return

    text = message.text
    twitter_match = re.search(TWITTER_REGEX, text)
    pixiv_match = re.search(PIXIV_REGEX, text)
    
    if not twitter_match and not pixiv_match:
        return

    status_msg = await message.reply("⏳ Починаю опрацювання та завантаження медіа...")
    media_data = None
    headers_for_download = None

    if twitter_match:
        author_handle, status_id = twitter_match.groups()
        original_url = twitter_match.group(0)
        media_data = await get_twitter_media(status_id, author_handle, original_url)
        headers_for_download = {}
        
    elif pixiv_match:
        illust_id = pixiv_match.group(1)
        original_url = pixiv_match.group(0)
        media_data = await get_pixiv_media(illust_id, original_url)
        # Для pixiv.cat заголовки реферера НЕ потрібні, сервіс віддає файли без обмежень
        headers_for_download = {}

    if not media_data or not media_data.get("media_urls"):
        await status_msg.edit_text("❌ Не вдалося отримати дані про цей арт.")
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

    await status_msg.edit_text(f"📥 Завантажую зображення на сервер ({len(urls)} шт.)...")

    downloaded_images = []
    for url in urls:
        img_bytes = await download_file(url, headers=headers_for_download)
        if img_bytes:
            downloaded_images.append(img_bytes)

    if not downloaded_images:
        await status_msg.edit_text("❌ Помилка завантаження: Сервер pixiv.cat тимчасово недоступний або зображення занадто велике.")
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

        await status_msg.edit_text("✅ Арт успішно опубліковано у вашій групі!")
    except Exception as e:
        logger.error(f"💥 Помилка Telegram відправки: {e}")
        await status_msg.edit_text(f"❌ Помилка відправки в Telegram: {e}")

async def main():
    logger.info("Старт бота на Railway...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

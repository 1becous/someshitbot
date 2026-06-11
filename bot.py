import os
import re
import logging
import asyncio
import aiohttp

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandStart
from aiogram.types import BufferedInputFile, InputMediaPhoto, InputMediaVideo
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

# Регулярні вирази для лінків (лише Twitter/X та Pixiv)
TWITTER_REGEX = re.compile(r'https?://(?:www\.)?(?:twitter|x)\.com/([\w_]+)/status/(\d+)', re.IGNORECASE)
PIXIV_REGEX = re.compile(r'https?://(?:www\.)?pixiv\.net/(?:[\w-]+/)?artworks/(\d+)', re.IGNORECASE)

def get_browser_headers(referer: str = None) -> dict:
    """Генерує стандартні заголовки браузера для скачування картинок"""
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

async def download_file(url: str, headers: dict = None) -> bytes:
    """Завантажує файл у бінарний буфер без збереження на диск"""
    if not headers:
        headers = get_browser_headers()
        
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers, timeout=45, ssl=False) as response:
                if response.status == 200:
                    return await response.read()
                else:
                    logger.error(f"❌ ПОМИЛКА СКАЧУВАННЯ ФАЙЛУ: статус {response.status} для URL: {url[:80]}...")
        except Exception as e:
            logger.error(f"💥 Виняток при завантаженні файлу: {e}")
    return b""

async def get_twitter_media(status_id: str, author_handle: str, original_url: str):
    """Отримує медіа (фото, відео, GIF) та дані автора з Twitter за допомогою fxtwitter API"""
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
                
                # Збір медіафайлів
                media_list = []
                media_data = tweet.get("media", {})
                all_media = media_data.get("all", [])
                
                if all_media:
                    for m in all_media:
                        m_type = m.get("type")  # Може бути "photo", "video", "gif"
                        url = m.get("url")
                        if url:
                            # Зберігаємо точний тип для подальшої обробки
                            if m_type == "gif":
                                m_type = "gif"
                            elif m_type == "video":
                                m_type = "video"
                            else:
                                m_type = "photo"
                            media_list.append({"type": m_type, "url": url})
                else:
                    # Резервний пошук, якщо немає масиву 'all'
                    photos = media_data.get("photos", [])
                    for p in photos:
                        if p.get("url"):
                            media_list.append({"type": "photo", "url": p.get("url")})
                    videos = media_data.get("videos", [])
                    for v in videos:
                        if v.get("url"):
                            media_list.append({"type": "video", "url": v.get("url")})
                
                # Додатковий резервний перевірочний блок для vxtwitter формату
                if not media_list and "attachments" in data:
                    for att in data["attachments"]:
                        att_type = att.get("type")
                        url = att.get("url")
                        if url:
                            m_type = "gif" if att_type == "gif" else ("video" if att_type == "video" else "photo")
                            media_list.append({"type": m_type, "url": url})
                
                logger.info(f"📊 Знайдено медіа у твіті: {len(media_list)}")
                return {
                    "author_name": f"{user_name} (@{user_screen_name})",
                    "author_link": author_link,
                    "media_list": media_list,
                    "source_url": original_url
                }
        except Exception as e:
            logger.error(f"💥 Помилка при запиті до Twitter API: {e}")
    return None

async def get_pixiv_media(illust_id: str, original_url: str):
    """Отримує оригінальний арт з Pixiv за допомогою проксі-сервісу pixiv.cat (завжди фото)"""
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
                        "media_list": [{"type": "photo", "url": f"https://pixiv.cat/{illust_id}.png"}],
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
                
                media_list = []
                if page_count == 1:
                    media_list.append({"type": "photo", "url": f"https://pixiv.cat/{illust_id}.png"})
                else:
                    for p in range(1, page_count + 1):
                        media_list.append({"type": "photo", "url": f"https://pixiv.cat/{illust_id}-{p}.png"})
                
                return {
                    "author_name": author_name,
                    "author_link": author_link,
                    "media_list": media_list,
                    "source_url": original_url
                }
        except Exception as e:
            logger.error(f"💥 Помилка Pixiv API: {e}")
            return {
                "author_name": "Pixiv Artist",
                "author_link": f"https://www.pixiv.net/artworks/{illust_id}",
                "media_list": [{"type": "photo", "url": f"https://pixiv.cat/{illust_id}.png"}],
                "source_url": original_url
            }

def is_admin(user_id: int) -> bool:
    return not ADMIN_IDS or user_id in ADMIN_IDS

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    await message.reply(
        "👋 Бот оновлений!\n\n"
        "Я працюю з **Twitter (X)** та **Pixiv**.\n"
        "Додано окреме завантаження **GIF-файлів** (без появи відео-плеєра в Telegram)!"
    )

@dp.message(F.text)
async def handle_links(message: types.Message):
    if message.chat.type == "private" and not is_admin(message.from_user.id):
        return

    text = message.text
    twitter_match = TWITTER_REGEX.search(text)
    pixiv_match = PIXIV_REGEX.search(text)
    
    if not any([twitter_match, pixiv_match]):
        return

    status_msg = await message.reply("⏳ Опрацьовую лінк та завантажую медіа...")
    media_data = None
    
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

    # Перевірка отриманих даних
    if not media_data or not media_data.get("media_list"):
        await status_msg.edit_text("❌ Не вдалося отримати медіа-файли за цим посиланням.")
        return

    author_name = media_data["author_name"]
    author_link = media_data["author_link"]
    source_url = media_data["source_url"]
    media_list = media_data["media_list"]

    if len(media_list) > 10:
        media_list = media_list[:10]
        warning_suffix = "\n⚠️ <i>(Показано перші 10 елементів)</i>"
    else:
        warning_suffix = ""

    await status_msg.edit_text(f"📥 Завантажую медіафайли ({len(media_list)} шт.)...")

    downloaded_items = []
    for item in media_list:
        logger.info(f"⬇️ Завантаження файлу ({item['type']}): {item['url'][:80]}...")
        file_bytes = await download_file(item["url"], headers=headers_for_download)
        if file_bytes:
            downloaded_items.append({
                "type": item["type"],
                "bytes": file_bytes
            })

    if not downloaded_items:
        await status_msg.edit_text("❌ Помилка: Не вдалося завантажити файли з серверів.")
        return

    caption_text = f"🎨 Автор: <a href='{author_link}'>{author_name}</a>\n🔗 <a href='{source_url}'>Джерело</a>{warning_suffix}"

    try:
        if len(downloaded_items) == 1:
            item = downloaded_items[0]
            if item["type"] == "photo":
                photo_file = BufferedInputFile(item["bytes"], filename="artwork.jpg")
                await bot.send_photo(chat_id=TARGET_CHAT_ID, photo=photo_file, caption=caption_text)
            elif item["type"] == "gif":
                # Надсилаємо як анімацію (GIF). Telegram чудово сприймає MP4-файл як циклічну анімацію
                gif_file = BufferedInputFile(item["bytes"], filename="animation.mp4")
                await bot.send_animation(chat_id=TARGET_CHAT_ID, animation=gif_file, caption=caption_text)
            else:  # video
                video_file = BufferedInputFile(item["bytes"], filename="video.mp4")
                await bot.send_video(chat_id=TARGET_CHAT_ID, video=video_file, caption=caption_text)
        else:
            # Створення медіагрупи (альбому).
            # Оскільки Telegram API не дозволяє змішувати InputMediaAnimation в одному альбомі з фото,
            # у випадку наявності кількох медіафайлів (що є рідкістю для GIF) ми обробляємо GIF як InputMediaVideo.
            media_group = []
            for idx, item in enumerate(downloaded_items):
                caption = caption_text if idx == 0 else None
                if item["type"] == "photo":
                    media_group.append(
                        InputMediaPhoto(
                            media=BufferedInputFile(item["bytes"], filename=f"artwork_{idx}.jpg"),
                            caption=caption
                        )
                    )
                else:  # video або gif у складі альбому
                    media_group.append(
                        InputMediaVideo(
                            media=BufferedInputFile(item["bytes"], filename=f"video_{idx}.mp4"),
                            caption=caption
                        )
                    )
            await bot.send_media_group(chat_id=TARGET_CHAT_ID, media=media_group)

        await status_msg.edit_text("✅ Публікацію успішно виконано!")
    except Exception as e:
        logger.error(f"💥 Помилка Telegram відправки: {e}")
        await status_msg.edit_text(f"❌ Помилка відправки в Telegram: {e}")

async def main():
    logger.info("Старт бота на Railway (Twitter & Pixiv)...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

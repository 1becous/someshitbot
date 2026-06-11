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

# На Railway змінні завантажуються автоматично з налаштувань середовища (Environment Variables)
BOT_TOKEN = os.getenv("BOT_TOKEN")
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID")
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(x.strip()) for x in ADMIN_IDS_RAW.split(",") if x.strip()]

# Налаштування логування (Railway збирає ці логи і показує у консолі)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Перевірка наявності обов'язкових змінних
if not BOT_TOKEN:
    logger.critical("Помилка: BOT_TOKEN не встановлено в змінних оточення!")
if not TARGET_CHAT_ID:
    logger.warning("Попередження: TARGET_CHAT_ID не встановлено. Бот може працювати некоректно.")

# Ініціалізація бота
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# Регулярні вирази
TWITTER_REGEX = r'https?://(?:www\.)?(?:twitter|x)\.com/([\w_]+)/status/(\d+)'
PIXIV_REGEX = r'https?://(?:www\.)?pixiv\.net/(?:[\w-]+/)?artworks/(\d+)'

async def download_file(url: str, headers: dict = None) -> bytes:
    """Завантажує файл у бінарний буфер без збереження на диск"""
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers, timeout=30) as response:
                if response.status == 200:
                    return await response.read()
                else:
                    logger.error(f"Помилка завантаження файлу {url}: статус {response.status}")
        except Exception as e:
            logger.error(f"Виняток при завантаженні {url}: {e}")
    return b""

async def get_twitter_media(status_id: str, author_handle: str, original_url: str):
    """Отримує медіа та дані автора з Twitter за допомогою vxtwitter API"""
    api_url = f"https://api.vxtwitter.com/status/{status_id}"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(api_url, timeout=15) as response:
                if response.status != 200:
                    return None
                data = await response.json()
                
                user_name = data.get("user_name", author_handle)
                user_screen_name = data.get("user_screen_name", author_handle)
                author_link = f"https://x.com/{user_screen_name}"
                
                attachments = data.get("attachments", [])
                photo_urls = [att.get("url") for att in attachments if att.get("type") == "photo"]
                
                return {
                    "author_name": f"{user_name} (@{user_screen_name})",
                    "author_link": author_link,
                    "media_urls": photo_urls,
                    "source_url": original_url
                }
        except Exception as e:
            logger.error(f"Помилка при запиті до vxtwitter API: {e}")
    return None

async def get_pixiv_media(illust_id: str, original_url: str):
    """Отримує медіа та дані автора з Pixiv за допомогою офіційного публічного AJAX API"""
    api_url = f"https://www.pixiv.net/ajax/illust/{illust_id}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Referer": "https://www.pixiv.net/"
    }
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(api_url, headers=headers, timeout=15) as response:
                if response.status != 200:
                    return None
                data = await response.json()
                if data.get("error"):
                    logger.error(f"Pixiv API повернув помилку для ID {illust_id}")
                    return None
                
                body = data.get("body", {})
                author_name = body.get("userName", "Unknown Pixiv Artist")
                author_id = body.get("userId", "")
                author_link = f"https://www.pixiv.net/users/{author_id}" if author_id else "https://www.pixiv.net"
                
                page_count = body.get("pageCount", 1)
                original_img_url = body.get("urls", {}).get("original", "")
                
                photo_urls = []
                if original_img_url:
                    for p in range(page_count):
                        page_url = original_img_url.replace("_p0.", f"_p{p}.")
                        photo_urls.append(page_url)
                
                return {
                    "author_name": author_name,
                    "author_link": author_link,
                    "media_urls": photo_urls,
                    "source_url": original_url
                }
        except Exception as e:
            logger.error(f"Помилка при запиті до Pixiv AJAX API: {e}")
    return None

def is_admin(user_id: int) -> bool:
    return not ADMIN_IDS or user_id in ADMIN_IDS

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.reply("У вас немає доступу до цього бота.")
        return
    await message.reply(
        "👋 Привіт! Бот успішно запущений на Railway.app!\n\n"
        "Надішліть мені посилання на пост у Twitter (X) або Pixiv, і я опублікую "
        "медіа у вашому каналі/групі з посиланням на автора.\n\n"
        "Команда `/id` допоможе дізнатися ID цього чату."
    )

@dp.message(Command("id"))
async def cmd_id(message: types.Message):
    await message.reply(f"ID цього чату: <code>{message.chat.id}</code>")

@dp.message(F.text)
async def handle_links(message: types.Message):
    if message.chat.type == "private" and not is_admin(message.from_user.id):
        return

    text = message.text
    twitter_match = re.search(TWITTER_REGEX, text)
    pixiv_match = re.search(PIXIV_REGEX, text)
    
    if not twitter_match and not pixiv_match:
        if message.chat.type == "private":
            await message.reply("Будь ласка, надішліть коректне посилання на Twitter (X) або Pixiv.")
        return

    status_msg = await message.reply("⏳ Опрацьовую посилання...")
    
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
        headers_for_download = {
            "Referer": "https://www.pixiv.net/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        }

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

    await status_msg.edit_text(f"📥 Завантажую зображення ({len(urls)} шт.)...")

    downloaded_images = []
    for url in urls:
        img_bytes = await download_file(url, headers=headers_for_download)
        if img_bytes:
            downloaded_images.append(img_bytes)

    if not downloaded_images:
        await status_msg.edit_text("❌ Сталася помилка при завантаженні зображень.")
        return

    caption_text = f"🎨 Автор: <a href='{author_link}'>{author_name}</a>\n🔗 <a href='{source_url}'>Джерело</a>{warning_suffix}"

    try:
        if len(downloaded_images) == 1:
            photo = BufferedInputFile(downloaded_images[0], filename="artwork.jpg")
            await bot.send_photo(
                chat_id=TARGET_CHAT_ID,
                photo=photo,
                caption=caption_text
            )
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

        await status_msg.edit_text("✅ Арт опубліковано!")
    except Exception as e:
        logger.error(f"Помилка надсилання: {e}")
        await status_msg.edit_text(f"❌ Помилка Telegram: {e}")

async def main():
    logger.info("Старт бота на Railway...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
import asyncio
import logging
import sys
from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import config
from database import Database
from vk_client import VKClient
from tg_bot import TelegramBridge

# Logging configuration
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("vktotg")

db: Database = None
vk_client: VKClient = None
tg_bridge: TelegramBridge = None


async def handle_discussion_forward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Перехватывает автофорвард сообщения из канала в группу обсуждений.
    Запоминает ID сообщения в группе, чтобы слать комментарии именно в его ветку.
    """
    msg = update.effective_message
    if not msg:
        return

    # Проверяем, является ли сообщение автоматическим форвардом из канала
    is_auto = getattr(msg, "is_automatic_forward", False)
    if is_auto:
        # Извлекаем message_id поста в самом канале
        channel_msg_id = getattr(msg, "forward_from_message_id", None)
        
        # Поддержка Telegram Bot API 7.0+ forward_origin
        if not channel_msg_id and hasattr(msg, "forward_origin"):
            origin = getattr(msg, "forward_origin", None)
            if origin and getattr(origin, "type", "") == "channel":
                channel_msg_id = getattr(origin, "message_id", None)

        if channel_msg_id:
            discussion_msg_id = msg.message_id
            db.update_discussion_msg_id(channel_msg_id, discussion_msg_id)
            logger.info(
                f"🔗 Связано обсуждение поста: channel_msg_id={channel_msg_id} "
                f"-> discussion_msg_id={discussion_msg_id}"
            )


async def sync_post_comments(post_data: dict):
    """Синхронизирует новые комментарии для конкретного поста."""
    vk_post_id = post_data["vk_post_id"]
    discussion_msg_id = post_data.get("tg_discussion_msg_id")

    if not discussion_msg_id:
        logger.debug(f"У поста VK {vk_post_id} еще не определен discussion_msg_id")
        return

    comments = await vk_client.get_comments(vk_post_id)
    for comment in comments:
        if db.comment_exists(comment.id):
            continue

        sent_id = await tg_bridge.send_comment(comment, discussion_msg_id, vk_client)
        if sent_id:
            db.save_comment(comment.id, vk_post_id, sent_id)
            await asyncio.sleep(0.5)


async def poll_vk_cycle():
    """Один цикл проверки постов и комментариев из ВК."""
    try:
        # 1. Проверяем новые посты
        posts = await vk_client.get_posts(count=10)
        # Обрабатываем от старых к новым
        new_posts = [p for p in posts if not db.post_exists(p.id)]
        new_posts.reverse()

        for post in new_posts:
            logger.info(f"🆕 Обнаружен новый пост VK {post.id}")
            channel_msg_id = await tg_bridge.send_post(post, vk_client)
            if channel_msg_id:
                db.save_post(post.id, channel_msg_id, post.date)
                # Даём Telegram пару секунд на автоматический форвард в группу обсуждений
                await asyncio.sleep(3)
                
                # Пробуем сразу отправить существующие комментарии, если они уже есть
                stored_post = db.get_post(post.id)
                if stored_post:
                    await sync_post_comments(stored_post)

            await asyncio.sleep(1.5)

        # 2. Проверяем новые комментарии к недавним постам
        recent_posts = db.get_recent_posts(limit=15)
        for post_data in recent_posts:
            await sync_post_comments(post_data)
            await asyncio.sleep(0.3)

    except Exception as e:
        logger.error(f"Ошибка в цикле опроса VK: {e}", exc_info=True)


async def initial_sync():
    """Синхронизация последних N постов при первом запуске."""
    logger.info(f"База данных пуста. Синхронизируем последние {config.INITIAL_POSTS_COUNT} постов...")
    try:
        posts = await vk_client.get_posts(count=config.INITIAL_POSTS_COUNT)
        posts.reverse()

        for post in posts:
            logger.info(f"Публикация начального поста VK {post.id}...")
            channel_msg_id = await tg_bridge.send_post(post, vk_client)
            if channel_msg_id:
                db.save_post(post.id, channel_msg_id, post.date)
                # Ждем форвард в группу
                await asyncio.sleep(3)
                stored_post = db.get_post(post.id)
                if stored_post:
                    await sync_post_comments(stored_post)

            await asyncio.sleep(2)

        logger.info("✅ Первоначальная синхронизация успешно завершена!")
    except Exception as e:
        logger.error(f"Ошибка при первоначальной синхронизации: {e}", exc_info=True)


async def vk_poller_task():
    """Фоновая корутина периодического опроса ВК."""
    # Ждём инициализации бота
    await asyncio.sleep(2)

    if db.is_empty():
        await initial_sync()

    logger.info(f"🚀 Запущен регулярный опрос VK (интервал: {config.POLL_INTERVAL} сек)")
    while True:
        try:
            await poll_vk_cycle()
        except Exception as e:
            logger.error(f"Непредвиденная ошибка в воркере: {e}")
        await asyncio.sleep(config.POLL_INTERVAL)


async def post_init(app: Application):
    """Хук старта приложения: инициализирует Telegram Bridge и запускает поллер."""
    await tg_bridge.init()
    asyncio.create_task(vk_poller_task())


def main():
    global db, vk_client, tg_bridge

    if not config.validate():
        sys.exit(1)

    logger.info("Запуск VK to Telegram бота...")

    db = Database(config.DATABASE_PATH)
    vk_client = VKClient(config.VK_ACCESS_TOKEN, config.VK_GROUP_DOMAIN)
    tg_bridge = TelegramBridge(
        bot_token=config.TELEGRAM_BOT_TOKEN,
        channel_id=config.TELEGRAM_CHANNEL_ID,
        discussion_id=config.TELEGRAM_DISCUSSION_ID or None
    )

    app = ApplicationBuilder().token(config.TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    # Слушаем все входящие служебные сообщения и форварды
    app.add_handler(
        MessageHandler(
            filters.ALL,
            handle_discussion_forward
        )
    )

    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()

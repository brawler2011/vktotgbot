import asyncio
import datetime
import html
import logging
import sys
from typing import Optional

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
)

from config import config
from database import Database
from vk_client import VKClient
from tg_bot import TelegramBridge

import collections

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

# Observability state
start_time: datetime.datetime = datetime.datetime.now()
last_vk_poll_time: Optional[datetime.datetime] = None
last_error_alert_time: Optional[datetime.datetime] = None
last_error_message: Optional[str] = None
error_active: bool = False


async def handle_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправляет отчёт о статусе и здоровье бота администратору."""
    msg = update.effective_message
    if not msg:
        return
    user_id = update.effective_user.id if update.effective_user else None
    if config.ADMIN_TG_ID and user_id != config.ADMIN_TG_ID:
        return

    now = datetime.datetime.now()
    uptime = str(now - start_time).split(".")[0]
    posts_count = len(db.get_recent_posts(limit=1000))
    last_poll_str = (
        f"{int((now - last_vk_poll_time).total_seconds())} сек назад"
        if last_vk_poll_time
        else "еще не проводился"
    )

    status_text = (
        f"📊 <b>Статус VK to TG бота</b>\n\n"
        f"🟢 <b>Состояние:</b> {'Работает штатно' if not error_active else '⚠️ Были ошибки'}\n"
        f"⏱ <b>Аптайм:</b> <code>{uptime}</code>\n"
        f"🎯 <b>Цель:</b> <code>{config.TELEGRAM_CHAT_ID}</code> (топик: {config.TELEGRAM_TOPIC_ID or 'нет'})\n"
        f"👥 <b>VK сообщество:</b> <code>{config.VK_GROUP_DOMAIN}</code>\n"
        f"🔄 <b>Последний опрос VK:</b> {last_poll_str}\n"
        f"📝 <b>Постов в базе:</b> {posts_count}\n"
    )
    await msg.reply_text(status_text, parse_mode=ParseMode.HTML)


post_locks: collections.defaultdict[int, asyncio.Lock] = collections.defaultdict(asyncio.Lock)


async def sync_post_comments(post_data: dict):
    """Синхронизирует новые комментарии для конкретного поста."""
    vk_post_id = post_data["vk_post_id"]
    post_msg_id = post_data.get("tg_post_msg_id")

    if not post_msg_id:
        logger.warning(f"У поста VK {vk_post_id} нет сохраненного tg_post_msg_id для ответов")
        return

    async with post_locks[vk_post_id]:
        comments = await vk_client.get_comments(vk_post_id)
        for comment in comments:
            if db.comment_exists(comment.id):
                continue

            sent_id = await tg_bridge.send_comment(comment, post_msg_id, vk_client)
            if sent_id:
                db.save_comment(comment.id, vk_post_id, sent_id)
                await asyncio.sleep(0.5)


async def poll_vk_cycle():
    """Один цикл проверки постов и комментариев из ВК."""
    global last_vk_poll_time, error_active, last_error_alert_time, last_error_message
    try:
        last_vk_poll_time = datetime.datetime.now()

        # 1. Запрашиваем свежие посты (всего 1 запрос к VK API)
        posts = await vk_client.get_posts(count=10)

        # 2. Проверяем новые посты
        new_posts = [p for p in posts if not db.post_exists(p.id)]
        new_posts.reverse()

        for post in new_posts:
            logger.info(f"🆕 Обнаружен новый пост VK {post.id}")
            tg_post_msg_id = await tg_bridge.send_post(post, vk_client)
            if tg_post_msg_id:
                db.save_post(post.id, tg_post_msg_id, post.date, comments_count=0)

                if tg_bridge.admin_id:
                    await tg_bridge.send_admin(
                        f"📢 <b>Опубликован новый пост из VK:</b>\n"
                        f"ID: <code>{post.id}</code> (msg_id: {tg_post_msg_id})\n"
                        f"🔗 <a href='{post.vk_url}'>Оригинал ВКонтакте</a>"
                    )

                await asyncio.sleep(1.5)
                
                if post.comments_count > 0:
                    stored_post = db.get_post(post.id)
                    if stored_post:
                        await sync_post_comments(stored_post)
                        db.update_comments_count(post.id, post.comments_count)

            await asyncio.sleep(1.5)

        # 3. Проверка комментариев к существующим постам
        for post in posts:
            if not db.post_exists(post.id):
                continue

            stored_count = db.get_stored_comments_count(post.id)
            synced_count = db.get_synced_comments_count(post.id)

            # Проверяем, если в ВК появились новые комментарии, которых еще нет в Telegram
            if post.comments_count > stored_count or post.comments_count > synced_count:
                stored_post = db.get_post(post.id)
                if stored_post:
                    logger.info(
                        f"💬 Обнаружены новые комментарии к посту {post.id}: "
                        f"в ВК {post.comments_count}, в БД {synced_count} (счетчик: {stored_count})"
                    )
                    await sync_post_comments(stored_post)
                    db.update_comments_count(post.id, post.comments_count)
                    await asyncio.sleep(0.5)

        # Если до этого была ошибка, но опрос прошёл успешно — уведомляем о восстановлении
        if error_active:
            error_active = False
            last_error_message = None
            if tg_bridge.admin_id:
                await tg_bridge.send_admin("✅ <b>Ошибки устранены. Работа бота восстановлена.</b>")

    except Exception as e:
        logger.error(f"Ошибка в цикле опроса VK: {e}", exc_info=True)
        error_str = str(e)
        now = datetime.datetime.now()
        should_alert = False
        if not error_active or error_str != last_error_message:
            should_alert = True
        elif last_error_alert_time and (now - last_error_alert_time).total_seconds() > 900:
            should_alert = True

        if should_alert and tg_bridge.admin_id:
            last_error_alert_time = now
            last_error_message = error_str
            error_active = True
            await tg_bridge.send_admin(
                f"🔴 <b>Внимание: Ошибка в работе бота!</b>\n\n"
                f"<code>{html.escape(error_str[:400])}</code>\n\n"
                f"<i>Бот продолжит попытки в фоновом режиме.</i>"
            )


async def initial_sync():
    """Синхронизация последних N постов при первом запуске."""
    logger.info(f"База данных пуста. Синхронизируем последние {config.INITIAL_POSTS_COUNT} постов...")
    try:
        posts = await vk_client.get_posts(count=config.INITIAL_POSTS_COUNT)
        posts.reverse()

        for post in posts:
            logger.info(f"Публикация начального поста VK {post.id}...")
            tg_post_msg_id = await tg_bridge.send_post(post, vk_client)
            if tg_post_msg_id:
                db.save_post(post.id, tg_post_msg_id, post.date, comments_count=0)
                await asyncio.sleep(2)
                if post.comments_count > 0:
                    stored_post = db.get_post(post.id)
                    if stored_post:
                        await sync_post_comments(stored_post)
                        db.update_comments_count(post.id, post.comments_count)

            await asyncio.sleep(2)

        logger.info("✅ Первоначальная синхронизация успешно завершена!")
    except Exception as e:
        logger.error(f"Ошибка при первоначальной синхронизации: {e}", exc_info=True)


async def vk_poller_task():
    """Фоновая корутина периодического опроса ВК."""
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
    if tg_bridge.admin_id:
        target_info = f"топик {tg_bridge.topic_id}" if tg_bridge.topic_id else "основной чат"
        await tg_bridge.send_admin(
            f"🟢 <b>VK to TG бот запущен!</b>\n\n"
            f"🎯 <b>Цель:</b> <code>{tg_bridge.chat_id}</code> ({target_info})\n"
            f"👥 <b>VK сообщество:</b> <code>{config.VK_GROUP_DOMAIN}</code>\n"
            f"⏱ <b>Интервал опроса:</b> {config.POLL_INTERVAL} сек\n\n"
            f"<i>Для проверки состояния отправьте /status</i>"
        )
    asyncio.create_task(vk_poller_task())


async def post_shutdown(app: Application):
    """Хук корректного завершения приложения: закрывает aiohttp сессию."""
    if tg_bridge and tg_bridge.admin_id:
        try:
            await tg_bridge.send_admin("🟡 <b>VK to TG бот перезапускается или остановлен.</b>")
        except Exception:
            pass
    if vk_client:
        await vk_client.close()


def main():
    global db, vk_client, tg_bridge

    if not config.validate():
        sys.exit(1)

    logger.info("Запуск VK to Telegram бота...")

    db = Database(config.DATABASE_PATH)
    vk_client = VKClient(config.VK_ACCESS_TOKEN, config.VK_GROUP_DOMAIN)
    tg_bridge = TelegramBridge(
        bot_token=config.TELEGRAM_BOT_TOKEN,
        chat_id=config.TELEGRAM_CHAT_ID,
        topic_id=config.TELEGRAM_TOPIC_ID,
        admin_id=config.ADMIN_TG_ID
    )

    app = (
        ApplicationBuilder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Команды администратора для мониторинга
    app.add_handler(CommandHandler(["status", "ping"], handle_status))

    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False
    )


if __name__ == "__main__":
    main()

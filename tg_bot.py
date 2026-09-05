import io
import re
import html
import logging
from typing import Optional, List, Union
import telegram
from telegram import Bot, InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
from telegram.constants import ParseMode
try:
    from telegram import ReplyParameters
    HAS_REPLY_PARAMETERS = True
except ImportError:
    HAS_REPLY_PARAMETERS = False

from vk_client import VKPost, VKComment, VKClient

logger = logging.getLogger(__name__)

# Max Telegram caption length
MAX_CAPTION_LEN = 1024
# Max Telegram message length
MAX_MESSAGE_LEN = 4096


def format_vk_text(text: str) -> str:
    """
    Форматирует текст из ВК:
    - Преобразует ссылки вида [id123|Имя] в HTML <a href="...">Имя</a>
    - Экранирует спецсимволы HTML
    """
    if not text:
        return ""

    # Функция замены упоминаний [id123|Имя] или [club123|Группа]
    def replace_vk_mention(match):
        raw_target = match.group(1)
        name = html.escape(match.group(2))
        url = f"https://vk.com/{raw_target}"
        return f'<a href="{url}">{name}</a>'

    # 1. Заменяем упоминания ВК
    # Временно экранируем весь текст кроме упоминаний
    pattern = r'\[(id\d+|club\d+|public\d+)\|([^\]]+)\]'
    
    parts = []
    last_idx = 0
    for m in re.finditer(pattern, text):
        # Обычный текст до совпадения - экранируем
        normal_text = text[last_idx:m.start()]
        parts.append(html.escape(normal_text))
        # Упоминание оформляем ссылкой
        parts.append(replace_vk_mention(m))
        last_idx = m.end()

    # Оставшийся хвост
    parts.append(html.escape(text[last_idx:]))
    formatted = "".join(parts)

    return formatted.strip()


class TelegramBridge:
    def __init__(
        self,
        bot_token: str,
        channel_id: str,
        discussion_id: Optional[str] = None,
        topic_id: Optional[int] = None
    ):
        self.bot = Bot(token=bot_token)
        self.channel_id = channel_id
        self.discussion_id = discussion_id
        self.topic_id = topic_id

    async def init(self) -> None:
        """Определяет linked_chat_id (обсуждение), если оно не задано вручную, и проверяет права."""
        if self.topic_id:
            try:
                me = await self.bot.get_me()
                member = await self.bot.get_chat_member(chat_id=self.channel_id, user_id=me.id)
                logger.info(f"✅ Бот подтверждён в супергруппе {self.channel_id} (статус: {member.status}), топик: {self.topic_id}")
            except Exception as e:
                logger.error(f"Не удалось проверить статус бота в группе {self.channel_id}: {e}")
            return

        if not self.discussion_id:
            try:
                chat = await self.bot.get_chat(chat_id=self.channel_id)
                if chat.linked_chat_id:
                    self.discussion_id = str(chat.linked_chat_id)
                    logger.info(f"Найдена привязанная группа обсуждений: {self.discussion_id}")
                else:
                    logger.warning("⚠️ К каналу пока не привязана группа обсуждений. Комментарии не будут отправляться в ветку.")
            except Exception as e:
                logger.error(f"Не удалось получить информацию о канале {self.channel_id}: {e}")

        if self.discussion_id:
            try:
                me = await self.bot.get_me()
                member = await self.bot.get_chat_member(chat_id=self.discussion_id, user_id=me.id)
                if member.status in ("administrator", "creator"):
                    logger.info(f"✅ Бот подтверждён как администратор группы обсуждений ({self.discussion_id})")
                elif member.status == "member":
                    logger.warning(
                        f"⚠️ ВНИМАНИЕ: Бот является обычным участником в группе {self.discussion_id}. "
                        "Из-за Group Privacy Mode в Telegram бот НЕ будет получать автофорварды постов! "
                        "Обязательно назначьте бота администратором группы обсуждений "
                        "или отключите Privacy Mode через @BotFather (/setprivacy -> Disable)."
                    )
                else:
                    logger.warning(f"⚠️ Статус бота в группе {self.discussion_id}: {member.status}")
            except Exception as e:
                logger.error(
                    f"❌ ВНИМАНИЕ: Бот НЕ добавлен в группу обсуждений {self.discussion_id}! "
                    f"Ошибка: {e}. Обязательно добавьте бота в группу обсуждений и сделайте его администратором!"
                )

    async def resolve_missing_discussions(self, posts_missing: List[dict]) -> dict:
        """
        Сканирует группу обсуждений и находит discussion_msg_id для постов,
        у которых автофорвард был пропущен (например, если бот не был админом).
        Возвращает словарь {tg_channel_msg_id: tg_discussion_msg_id}.
        """
        if not self.discussion_id or not posts_missing:
            return {}

        target_map = {p["tg_channel_msg_id"]: p.get("vk_post_id") for p in posts_missing}
        resolved = {}

        try:
            # Отправляем временный маркер, чтобы узнать максимальный message_id в группе
            marker = await self.bot.send_message(chat_id=self.discussion_id, text=".")
            top_id = marker.message_id
            await self.bot.delete_message(chat_id=self.discussion_id, message_id=top_id)
        except Exception as e:
            logger.debug(f"Не удалось отправить маркер в группу обсуждений: {e}")
            return {}

        # Ищем совпадения в последних сообщениях группы обсуждений (до 100 сообщений назад)
        start_id = max(1, top_id - 100)
        for mid in range(top_id - 1, start_id - 1, -1):
            if not target_map:
                break
            try:
                fwd = await self.bot.forward_message(
                    chat_id=self.discussion_id,
                    from_chat_id=self.discussion_id,
                    message_id=mid
                )
                try:
                    orig_channel_msg_id = getattr(fwd, "forward_from_message_id", None)
                    if not orig_channel_msg_id and hasattr(fwd, "forward_origin"):
                        origin = getattr(fwd, "forward_origin", None)
                        origin_type = getattr(origin, "type", "")
                        if origin and (origin_type == "channel" or str(origin_type).lower().endswith("channel")):
                            orig_channel_msg_id = getattr(origin, "message_id", None)

                    if orig_channel_msg_id and orig_channel_msg_id in target_map:
                        vk_id = target_map.pop(orig_channel_msg_id)
                        resolved[orig_channel_msg_id] = mid
                        logger.info(
                            f"🔍 Автоматически найден discussion_msg_id={mid} "
                            f"для поста VK {vk_id} (channel_msg_id={orig_channel_msg_id})"
                        )
                finally:
                    await self.bot.delete_message(chat_id=self.discussion_id, message_id=fwd.message_id)
            except Exception:
                # Служебные или недоступные для пересылки сообщения пропускаем
                continue

        return resolved

    async def send_post(self, post: VKPost, vk_client: VKClient) -> Optional[int]:
        """
        Публикует пост ВК в канал Telegram.
        Возвращает message_id отправленного сообщения в канале.
        """
        formatted_text = format_vk_text(post.text)

        # Собираем дополнительные ссылки (видео, опросы и т.д.)
        extra_parts = []
        if post.extra_links:
            extra_parts.append("\n".join(post.extra_links))

        # Ссылка на оригинал ВК
        orig_link = f'🔗 <a href="{post.vk_url}">Оригинал ВКонтакте</a>'
        
        full_content = formatted_text
        if extra_parts:
            full_content = f"{full_content}\n\n" + "\n\n".join(extra_parts) if full_content else "\n\n".join(extra_parts)

        channel_msg_id: Optional[int] = None
        post_kwargs = {}
        if self.topic_id:
            post_kwargs["message_thread_id"] = self.topic_id

        try:
            # Сценарий 1: Есть фотографии
            if post.photos:
                # Если 1 фото
                if len(post.photos) == 1:
                    photo_url = post.photos[0].url
                    # Если текст помещается в caption (до 1024 символов)
                    caption = f"{full_content}\n\n{orig_link}".strip()
                    if len(caption) <= MAX_CAPTION_LEN:
                        msg = await self.bot.send_photo(
                            chat_id=self.channel_id,
                            photo=photo_url,
                            caption=caption,
                            parse_mode=ParseMode.HTML,
                            **post_kwargs
                        )
                        channel_msg_id = msg.message_id
                    else:
                        # Текст слишком длинный: сначала фото, потом текст
                        msg_photo = await self.bot.send_photo(
                            chat_id=self.channel_id,
                            photo=photo_url,
                            **post_kwargs
                        )
                        channel_msg_id = msg_photo.message_id
                        msg_text = await self.bot.send_message(
                            chat_id=self.channel_id,
                            text=f"{full_content}\n\n{orig_link}",
                            parse_mode=ParseMode.HTML,
                            **post_kwargs
                        )
                        # Запоминаем ID текста, так как он обычно замыкает пост
                        channel_msg_id = msg_text.message_id

                # Если альбом фото (2 и более, до 10)
                else:
                    media_photos = post.photos[:10]
                    media_group = []
                    caption = f"{full_content}\n\n{orig_link}".strip()
                    
                    for idx, p in enumerate(media_photos):
                        if idx == 0 and len(caption) <= MAX_CAPTION_LEN:
                            media_group.append(InputMediaPhoto(media=p.url, caption=caption, parse_mode=ParseMode.HTML))
                        else:
                            media_group.append(InputMediaPhoto(media=p.url))

                    msgs = await self.bot.send_media_group(
                        chat_id=self.channel_id,
                        media=media_group,
                        **post_kwargs
                    )
                    channel_msg_id = msgs[0].message_id

                    # Если текст был длинным и не влез в caption первого фото
                    if len(caption) > MAX_CAPTION_LEN:
                        msg_text = await self.bot.send_message(
                            chat_id=self.channel_id,
                            text=f"{full_content}\n\n{orig_link}",
                            parse_mode=ParseMode.HTML,
                            **post_kwargs
                        )
                        channel_msg_id = msg_text.message_id

            # Сценарий 2: Только текст (без фото)
            else:
                text_to_send = f"{full_content}\n\n{orig_link}".strip() if full_content else orig_link
                # Если текст > 4096, делим на части
                if len(text_to_send) > MAX_MESSAGE_LEN:
                    chunks = [text_to_send[i:i + 4000] for i in range(0, len(text_to_send), 4000)]
                    for chunk in chunks:
                        msg = await self.bot.send_message(
                            chat_id=self.channel_id,
                            text=chunk,
                            parse_mode=ParseMode.HTML,
                            **post_kwargs
                        )
                        channel_msg_id = msg.message_id
                else:
                    msg = await self.bot.send_message(
                        chat_id=self.channel_id,
                        text=text_to_send,
                        parse_mode=ParseMode.HTML,
                        **post_kwargs
                    )
                    channel_msg_id = msg.message_id

            # Сценарий 3: Документы (PDF, DOCX и т.д.)
            if post.docs:
                for doc in post.docs:
                    try:
                        file_bytes = await vk_client.download_file(doc.url)
                        file_io = io.BytesIO(file_bytes)
                        file_io.name = doc.title
                        await self.bot.send_document(
                            chat_id=self.channel_id,
                            document=file_io,
                            caption=f"📎 {html.escape(doc.title)}",
                            parse_mode=ParseMode.HTML,
                            **post_kwargs
                        )
                    except Exception as e:
                        logger.warning(f"Не удалось отправить документ {doc.title}: {e}")
                        # Если не удалось скачать/отправить как файл, даём ссылку
                        await self.bot.send_message(
                            chat_id=self.channel_id,
                            text=f"📎 <a href='{doc.url}'>{html.escape(doc.title)}</a>",
                            parse_mode=ParseMode.HTML,
                            **post_kwargs
                        )

            logger.info(f"Пост VK {post.id} успешно опубликован (message_id={channel_msg_id})")
            return channel_msg_id

        except Exception as e:
            logger.error(f"Ошибка при публикации поста VK {post.id} в Telegram: {e}", exc_info=True)
            return None

    async def send_comment(self, comment: VKComment, discussion_msg_id: int, vk_client: VKClient) -> Optional[int]:
        """
        Публикует комментарий из ВК в ветку обсуждения конкретного поста или топик супергруппы.
        """
        target_chat_id = self.channel_id if self.topic_id else self.discussion_id
        if not target_chat_id:
            logger.debug("Пропуск комментария: целевой chat_id не задан")
            return None

        formatted_text = format_vk_text(comment.text)
        author_link = f'<a href="{comment.author_url}"><b>{html.escape(comment.author_name)}</b></a>'
        
        lines = [f"💬 {author_link}:"]
        if formatted_text:
            lines.append(formatted_text)
        if comment.extra_links:
            lines.extend(comment.extra_links)

        comment_body = "\n".join(lines)

        reply_kwargs = {}
        if HAS_REPLY_PARAMETERS:
            reply_kwargs["reply_parameters"] = ReplyParameters(message_id=discussion_msg_id)
        else:
            reply_kwargs["reply_to_message_id"] = discussion_msg_id

        if self.topic_id:
            reply_kwargs["message_thread_id"] = self.topic_id

        try:
            # Отправка текста комментария
            msg = await self.bot.send_message(
                chat_id=target_chat_id,
                text=comment_body,
                parse_mode=ParseMode.HTML,
                **reply_kwargs
            )
            sent_msg_id = msg.message_id

            # Если в комментарии были фото
            if comment.photos:
                for p in comment.photos[:5]:
                    await self.bot.send_photo(
                        chat_id=target_chat_id,
                        photo=p.url,
                        **reply_kwargs
                    )

            # Если в комментарии были документы
            if comment.docs:
                for doc in comment.docs:
                    try:
                        file_bytes = await vk_client.download_file(doc.url)
                        file_io = io.BytesIO(file_bytes)
                        file_io.name = doc.title
                        await self.bot.send_document(
                            chat_id=target_chat_id,
                            document=file_io,
                            caption=f"📎 {html.escape(doc.title)}",
                            parse_mode=ParseMode.HTML,
                            **reply_kwargs
                        )
                    except Exception as e:
                        logger.warning(f"Не удалось отправить документ в комментарии: {e}")

            logger.info(f"Комментарий {comment.id} к посту {comment.post_id} опубликован в ветку {discussion_msg_id}")
            return sent_msg_id

        except Exception as e:
            logger.error(f"Ошибка при отправке комментария {comment.id} в обсуждение: {e}", exc_info=True)
            return None

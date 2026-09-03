import html
import logging
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any, Tuple
import aiohttp

logger = logging.getLogger(__name__)

VK_API_VERSION = "5.199"


@dataclass
class PhotoAttachment:
    url: str
    width: int = 0
    height: int = 0


@dataclass
class DocAttachment:
    title: str
    url: str
    size: int = 0
    ext: str = ""


@dataclass
class VKPost:
    id: int
    owner_id: int
    text: str
    date: int
    photos: List[PhotoAttachment] = field(default_factory=list)
    docs: List[DocAttachment] = field(default_factory=list)
    extra_links: List[str] = field(default_factory=list)
    comments_count: int = 0

    @property
    def vk_url(self) -> str:
        return f"https://vk.com/wall{self.owner_id}_{self.id}"


@dataclass
class VKComment:
    id: int
    post_id: int
    owner_id: int
    author_id: int
    author_name: str
    author_url: str
    text: str
    date: int
    photos: List[PhotoAttachment] = field(default_factory=list)
    docs: List[DocAttachment] = field(default_factory=list)
    extra_links: List[str] = field(default_factory=list)


class VKClient:
    def __init__(self, access_token: str, group_domain: str):
        self.access_token = access_token
        self.group_domain = group_domain
        self.owner_id: Optional[int] = None
        self._session: Optional[aiohttp.ClientSession] = None

    async def get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _call_api(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        session = await self.get_session()
        url = f"https://api.vk.com/method/{method}"
        
        request_params = {
            "v": VK_API_VERSION,
            "access_token": self.access_token,
            **params
        }

        async with session.get(url, params=request_params) as resp:
            data = await resp.json()
            if "error" in data:
                err = data["error"]
                logger.error(f"VK API error in {method}: code={err.get('error_code')}, msg={err.get('error_msg')}")
                raise RuntimeError(f"VK API Error ({err.get('error_code')}): {err.get('error_msg')}")
            return data.get("response", {})

    def _extract_attachments(self, raw_attachments: List[Dict[str, Any]]) -> Tuple[List[PhotoAttachment], List[DocAttachment], List[str]]:
        photos: List[PhotoAttachment] = []
        docs: List[DocAttachment] = []
        extra_links: List[str] = []

        for att in raw_attachments:
            att_type = att.get("type")
            if att_type == "photo" and "photo" in att:
                sizes = att["photo"].get("sizes", [])
                if sizes:
                    # Pick largest photo
                    best_size = max(sizes, key=lambda s: s.get("width", 0) * s.get("height", 0) or s.get("type", ""))
                    if "url" in best_size:
                        photos.append(PhotoAttachment(
                            url=best_size["url"],
                            width=best_size.get("width", 0),
                            height=best_size.get("height", 0)
                        ))

            elif att_type == "doc" and "doc" in att:
                doc = att["doc"]
                docs.append(DocAttachment(
                    title=doc.get("title", "Документ"),
                    url=doc.get("url", ""),
                    size=doc.get("size", 0),
                    ext=doc.get("ext", "")
                ))

            elif att_type == "link" and "link" in att:
                link = att["link"]
                url = link.get("url", "")
                title = link.get("title", "")
                extra_links.append(f"🔗 Ссылка: <a href='{html.escape(url)}'>{html.escape(title or url)}</a>")

            elif att_type == "video" and "video" in att:
                vid = att["video"]
                v_owner = vid.get("owner_id", 0)
                v_id = vid.get("id", 0)
                v_title = vid.get("title", "Видеозапись")
                extra_links.append(f"🎬 <a href='https://vk.com/video{v_owner}_{v_id}'>{html.escape(v_title)}</a>")

            elif att_type == "poll" and "poll" in att:
                poll = att["poll"]
                q = poll.get("question", "Опрос")
                extra_links.append(f"📊 <i>Опрос: {html.escape(q)} (голосование в ВК)</i>")

        return photos, docs, extra_links

    async def get_posts(self, count: int = 10) -> List[VKPost]:
        params: Dict[str, Any] = {"count": count}
        domain = self.group_domain.strip()

        # Check if domain is numeric id (e.g. -229780192 or 229780192)
        if domain.lstrip("-").isdigit():
            owner_id = int(domain)
            if owner_id > 0:
                owner_id = -owner_id
            params["owner_id"] = owner_id
        else:
            params["domain"] = domain

        res = await self._call_api("wall.get", params)
        items = res.get("items", [])
        posts: List[VKPost] = []

        for item in items:
            # Skip suggest/postponed if any
            if item.get("post_type") not in ("post", "copy"):
                continue

            owner_id = item.get("owner_id", 0)
            if self.owner_id is None and owner_id != 0:
                self.owner_id = owner_id

            text = item.get("text", "")
            raw_attachments = item.get("attachments", [])

            # Handle repost (copy_history)
            if "copy_history" in item and item["copy_history"]:
                repost = item["copy_history"][0]
                repost_text = repost.get("text", "")
                if repost_text:
                    text = f"{text}\n\n📢 <i>Репост:</i>\n{repost_text}".strip()
                raw_attachments.extend(repost.get("attachments", []))

            photos, docs, extra_links = self._extract_attachments(raw_attachments)
            comments_count = item.get("comments", {}).get("count", 0)

            posts.append(VKPost(
                id=item["id"],
                owner_id=owner_id,
                text=text,
                date=item.get("date", 0),
                photos=photos,
                docs=docs,
                extra_links=extra_links,
                comments_count=comments_count
            ))

        return posts

    async def get_comments(self, post_id: int, owner_id: Optional[int] = None) -> List[VKComment]:
        target_owner_id = owner_id or self.owner_id
        if target_owner_id is None:
            # Fetch at least one post to resolve owner_id
            posts = await self.get_posts(count=1)
            if posts:
                target_owner_id = posts[0].owner_id
                self.owner_id = target_owner_id
            else:
                return []

        params = {
            "owner_id": target_owner_id,
            "post_id": post_id,
            "count": 100,
            "extended": 1,
            "sort": "asc",
            "thread_items_count": 10
        }

        try:
            res = await self._call_api("wall.getComments", params)
        except Exception as e:
            logger.warning(f"Failed to fetch comments for post {post_id}: {e}")
            return []

        items = res.get("items", [])
        profiles = {p["id"]: p for p in res.get("profiles", [])}
        groups = {g["id"]: g for g in res.get("groups", [])}

        def resolve_author(from_id: int) -> Tuple[str, str]:
            if from_id > 0:
                user = profiles.get(from_id)
                if user:
                    name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
                else:
                    name = f"Пользователь ID {from_id}"
                return name, f"https://vk.com/id{from_id}"
            elif from_id < 0:
                group = groups.get(abs(from_id))
                name = group.get("name", "Сообщество") if group else "Группа"
                return name, f"https://vk.com/club{abs(from_id)}"
            return "Неизвестный автор", "https://vk.com"

        comments: List[VKComment] = []

        for item in items:
            if item.get("deleted"):
                continue

            from_id = item.get("from_id", 0)
            author_name, author_url = resolve_author(from_id)
            raw_attachments = item.get("attachments", [])
            photos, docs, extra_links = self._extract_attachments(raw_attachments)

            comments.append(VKComment(
                id=item["id"],
                post_id=post_id,
                owner_id=target_owner_id,
                author_id=from_id,
                author_name=author_name,
                author_url=author_url,
                text=item.get("text", ""),
                date=item.get("date", 0),
                photos=photos,
                docs=docs,
                extra_links=extra_links
            ))

            # Thread / nested replies in VK comments
            thread = item.get("thread", {})
            for sub_item in thread.get("items", []):
                sub_from_id = sub_item.get("from_id", 0)
                sub_name, sub_url = resolve_author(sub_from_id)
                sub_photos, sub_docs, sub_links = self._extract_attachments(sub_item.get("attachments", []))
                comments.append(VKComment(
                    id=sub_item["id"],
                    post_id=post_id,
                    owner_id=target_owner_id,
                    author_id=sub_from_id,
                    author_name=sub_name,
                    author_url=sub_url,
                    text=sub_item.get("text", ""),
                    date=sub_item.get("date", 0),
                    photos=sub_photos,
                    docs=sub_docs,
                    extra_links=sub_links
                ))

        return comments

    async def download_file(self, url: str) -> bytes:
        """Скачивает файл (документ или фото) по URL."""
        session = await self.get_session()
        async with session.get(url) as resp:
            resp.raise_for_status()
            return await resp.read()

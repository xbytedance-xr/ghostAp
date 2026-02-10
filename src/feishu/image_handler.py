import os
import json
import logging
from typing import Optional
from dataclasses import dataclass, field

import lark_oapi as lark
from lark_oapi.api.im.v1 import GetMessageResourceRequest

logger = logging.getLogger(__name__)


@dataclass
class ImageParseResult:
    """飞书消息解析结果：文本 + 图片 key 列表"""
    text: str
    image_keys: list[str] = field(default_factory=list)


@dataclass
class ImageDownloadResult:
    """图片下载结果：成功路径 + 失败 key"""
    saved_paths: list[str] = field(default_factory=list)
    failed_keys: list[str] = field(default_factory=list)


class FeishuImageHandler:
    """处理飞书消息中的图片：解析、下载、存储"""

    IMAGE_DIR_NAME = "picturechat"

    def __init__(self, client: lark.Client):
        self._client = client

    def parse_message(self, message_type: str, content_str: str) -> ImageParseResult:
        """根据消息类型解析文本和图片 key

        支持三种消息类型:
        - text: 纯文本，无图片
        - image: 单张图片，无文字
        - post: 富文本，包含文字和图片
        """
        if message_type == "text":
            return self._parse_text_message(content_str)
        elif message_type == "image":
            return self._parse_image_message(content_str)
        elif message_type == "post":
            return self._parse_post_message(content_str)
        else:
            return ImageParseResult(text="", image_keys=[])

    def _parse_text_message(self, content_str: str) -> ImageParseResult:
        """解析文本消息 {"text": "..."}"""
        try:
            content_dict = json.loads(content_str)
            text = content_dict.get("text", "")
        except json.JSONDecodeError:
            text = content_str
        return ImageParseResult(text=text, image_keys=[])

    def _parse_image_message(self, content_str: str) -> ImageParseResult:
        """解析图片消息 {"image_key": "img_v2_xxx"}"""
        try:
            content_dict = json.loads(content_str)
            image_key = content_dict.get("image_key", "")
            image_keys = [image_key] if image_key else []
        except json.JSONDecodeError:
            image_keys = []
        return ImageParseResult(text="", image_keys=image_keys)

    def _parse_post_message(self, content_str: str) -> ImageParseResult:
        """解析富文本消息，提取文字和图片 key

        飞书 post 格式:
        {
          "zh_cn": {
            "title": "...",
            "content": [
              [
                {"tag": "text", "text": "some text"},
                {"tag": "img", "image_key": "img_v2_xxx"}
              ]
            ]
          }
        }
        """
        try:
            content_dict = json.loads(content_str)
        except json.JSONDecodeError:
            return ImageParseResult(text=content_str, image_keys=[])

        # 按优先级查找语言版本：zh_cn -> en_us -> 第一个可用的
        post_data = None
        for lang in ("zh_cn", "en_us"):
            if lang in content_dict:
                post_data = content_dict[lang]
                break
        if post_data is None and content_dict:
            post_data = next(iter(content_dict.values()), None)

        if not post_data or "content" not in post_data:
            return ImageParseResult(text="", image_keys=[])

        text_parts = []
        image_keys = []

        for row in post_data["content"]:
            for element in row:
                tag = element.get("tag", "")
                if tag == "text":
                    t = element.get("text", "")
                    if t:
                        text_parts.append(t)
                elif tag == "img":
                    img_key = element.get("image_key", "")
                    if img_key:
                        image_keys.append(img_key)

        combined_text = " ".join(text_parts).strip()
        return ImageParseResult(text=combined_text, image_keys=image_keys)

    def download_images(
        self,
        message_id: str,
        image_keys: list[str],
        save_dir: str,
    ) -> ImageDownloadResult:
        """批量下载图片并保存到按消息隔离的子目录

        图片以序号命名（1.png, 2.png, ...），保存在
        ``{save_dir}/{msg_short_id}/`` 子目录下，避免不同消息的图片冲突。

        Args:
            message_id: 飞书消息 ID（下载 API 需要，同时用于子目录命名）
            image_keys: 图片 key 列表
            save_dir: 基础保存目录（不存在会自动创建）

        Returns:
            ImageDownloadResult 包含成功保存的路径和下载失败的 key
        """
        result = ImageDownloadResult()

        if not image_keys:
            return result

        msg_short_id = message_id[-8:] if len(message_id) > 8 else message_id
        msg_dir = os.path.join(save_dir, f"msg_{msg_short_id}")
        os.makedirs(msg_dir, exist_ok=True)

        for index, image_key in enumerate(image_keys, 1):
            saved_path = self._download_single_image(
                message_id, image_key, msg_dir, index,
            )
            if saved_path:
                result.saved_paths.append(saved_path)
            else:
                result.failed_keys.append(image_key)

        return result

    def _download_single_image(
        self,
        message_id: str,
        image_key: str,
        save_dir: str,
        index: int = 1,
    ) -> Optional[str]:
        """下载单张图片，返回保存路径或 None"""
        try:
            request = GetMessageResourceRequest.builder() \
                .message_id(message_id) \
                .file_key(image_key) \
                .type("image") \
                .build()

            response = self._client.im.v1.message_resource.get(request)

            if not response.success():
                logger.warning(
                    "下载图片失败: %s, code=%s, msg=%s",
                    image_key, response.code, response.msg,
                )
                return None

            filename = f"{index}.png"
            filepath = os.path.join(save_dir, filename)

            with open(filepath, "wb") as f:
                f.write(response.file.read())

            logger.info("图片已保存: %s", filepath)
            return filepath

        except Exception as e:
            logger.error("下载图片异常: %s, error=%s", image_key, e)
            return None

    @staticmethod
    def build_image_reference_text(saved_paths: list[str]) -> str:
        """生成追加到用户消息末尾的图片引用文本

        格式（单张）:
            \\n\\n[用户附带了 1 张参考图片，请先查看后再回答]
            图片1: /path/to/1.png
        格式（多张）:
            \\n\\n[用户附带了 N 张参考图片，请先查看后再回答]
            图片1: /path/to/1.png
            图片2: /path/to/2.png
        """
        if not saved_paths:
            return ""

        n = len(saved_paths)
        lines = [f"\n\n[用户附带了 {n} 张参考图片，请先查看后再回答]"]
        for i, path in enumerate(saved_paths, 1):
            lines.append(f"图片{i}: {path}")
        return "\n".join(lines)

    @staticmethod
    def get_image_save_dir(project_root: Optional[str], fallback_dir: str) -> str:
        """确定图片保存目录

        优先使用 project_root/picturechat/，否则 fallback_dir/picturechat/
        """
        base_dir = project_root if project_root else fallback_dir
        return os.path.join(base_dir, FeishuImageHandler.IMAGE_DIR_NAME)

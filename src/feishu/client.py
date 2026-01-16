import json
import httpx
from typing import Optional
from ..config import get_settings


class FeishuClient:
    BASE_URL = "https://open.feishu.cn/open-apis"

    def __init__(self):
        self.settings = get_settings()
        self._tenant_access_token: Optional[str] = None
        self._http_client = httpx.AsyncClient(timeout=30.0)

    async def _get_tenant_access_token(self) -> str:
        if self._tenant_access_token:
            return self._tenant_access_token

        url = f"{self.BASE_URL}/auth/v3/tenant_access_token/internal"
        payload = {
            "app_id": self.settings.app_id,
            "app_secret": self.settings.app_secret,
        }

        response = await self._http_client.post(url, json=payload)
        data = response.json()

        if data.get("code") != 0:
            raise Exception(f"获取tenant_access_token失败: {data}")

        self._tenant_access_token = data["tenant_access_token"]
        return self._tenant_access_token

    async def _request(self, method: str, endpoint: str, **kwargs) -> dict:
        token = await self._get_tenant_access_token()
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {token}"

        url = f"{self.BASE_URL}{endpoint}"
        response = await self._http_client.request(method, url, headers=headers, **kwargs)
        return response.json()

    async def reply_message(self, message_id: str, content: str, msg_type: str = "text") -> dict:
        endpoint = f"/im/v1/messages/{message_id}/reply"

        if msg_type == "text":
            content_payload = json.dumps({"text": content})
        else:
            content_payload = content

        payload = {
            "content": content_payload,
            "msg_type": msg_type,
        }

        return await self._request("POST", endpoint, json=payload)

    async def send_message(self, receive_id: str, content: str, receive_id_type: str = "open_id", msg_type: str = "text") -> dict:
        endpoint = f"/im/v1/messages?receive_id_type={receive_id_type}"

        if msg_type == "text":
            content_payload = json.dumps({"text": content})
        else:
            content_payload = content

        payload = {
            "receive_id": receive_id,
            "content": content_payload,
            "msg_type": msg_type,
        }

        return await self._request("POST", endpoint, json=payload)

    async def close(self):
        await self._http_client.aclose()

    def invalidate_token(self):
        self._tenant_access_token = None

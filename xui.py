import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlparse, urlsplit

import requests


class XUIError(Exception):
    pass


class XUIClient:
    def __init__(self, base_url: str, username: str, password: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.logged_in = False

    def login(self) -> None:
        url = f"{self.base_url}/login"
        response = self.session.post(
            url,
            data={"username": self.username, "password": self.password},
            timeout=20,
        )
        if response.status_code != 200:
            raise XUIError(f"X-UI login failed: HTTP {response.status_code}")
        self.logged_in = True

    def create_client(
        self,
        volume_gb: int,
        inbound_id: int = 1,
        days_valid: int = 30,
        client_email: Optional[str] = None,
        public_host: Optional[str] = None,
        port: int = 443,
        flow: str = "xtls-rprx-vision",
        security: str = "reality",
        network: str = "httpupgrade",
        sni: str = "www.cloudflare.com",
        pbk: str = "",
        sid: str = "",
        fp: str = "chrome",
        path: str = "/assets",
    ) -> str:
        if not self.logged_in:
            self.login()

        client_uuid = str(uuid.uuid4())
        total_gb_bytes = volume_gb * 1024 * 1024 * 1024
        expiry_ms = int(
            (datetime.now(timezone.utc) + timedelta(days=days_valid)).timestamp() * 1000
        )
        email = client_email or f"clt-{client_uuid[:8]}"

        payload = {
            "id": inbound_id,
            "settings": json.dumps(
                {
                    "clients": [
                        {
                            "id": client_uuid,
                            "flow": "",
                            "email": email,
                            "limitIp": 0,
                            "totalGB": total_gb_bytes,
                            "expiryTime": 0,
                            "enable": True,
                            "tgId": "",
                            "subId": f"clt-{client_uuid[:8]}",
                            "comment": "",
                            "reset": 0,
                        }
                    ]
                }
            ),
        }

        url = f"{self.base_url}/panel/api/inbounds/addClient"
        response = self.session.post(url, data=payload, timeout=20)
        if response.status_code != 200:
            raise XUIError(f"X-UI addClient failed: HTTP {response.status_code}")

        data = response.json()
        if not data.get("success"):
            msg = data.get("msg", "unknown error")
            raise XUIError(f"X-UI addClient failed: {msg}")

        host = public_host or (urlparse(self.base_url).hostname or "example.com")
        # Builds a simple VLESS URL. Adjust options to your panel setup if needed.
        vless_link = (
            f"vless://{client_uuid}@{host}:{port}"
            f"?type={network}&security={security}&flow={flow}&sni={sni}&pbk={pbk}&sid={sid}&fp={fp}&path={path}"
            f"#{email}"
        )
        return vless_link

    def _ensure_login(self) -> None:
        if not self.logged_in:
            self.login()

    def _extract_uuid_email_from_link(self, link: str) -> tuple[Optional[str], Optional[str]]:
        parsed = urlsplit(link)
        if parsed.scheme != "vless":
            return None, None

        user_info = parsed.netloc.split("@", 1)[0]
        client_uuid = user_info if user_info else None
        email = parsed.fragment or None
        return client_uuid, email

    def get_client_status(self, link: str) -> Optional[dict[str, Any]]:
        self._ensure_login()

        client_uuid, link_email = self._extract_uuid_email_from_link(link)
        if not client_uuid:
            return None

        url = f"{self.base_url}/panel/api/inbounds/list"
        response = self.session.get(url, timeout=20)
        if response.status_code != 200:
            raise XUIError(f"X-UI inbounds list failed: HTTP {response.status_code}")

        data = response.json()
        if not data.get("success"):
            raise XUIError(f"X-UI inbounds list failed: {data.get('msg', 'unknown error')}")

        inbounds = data.get("obj") or []
        matched_client: Optional[dict[str, Any]] = None
        matched_stat: Optional[dict[str, Any]] = None
        matched_email: Optional[str] = link_email

        for inbound in inbounds:
            settings_raw = inbound.get("settings")
            if not settings_raw:
                continue

            try:
                settings = json.loads(settings_raw)
            except (TypeError, json.JSONDecodeError):
                continue

            clients = settings.get("clients") or []
            for client in clients:
                if str(client.get("id")) == client_uuid:
                    matched_client = client
                    matched_email = client.get("email") or link_email
                    break

            if matched_client is None:
                continue

            client_stats = inbound.get("clientStats") or []
            for stat in client_stats:
                stat_id = str(stat.get("id")) if stat.get("id") is not None else None
                stat_email = stat.get("email")
                if stat_id == client_uuid or (matched_email and stat_email == matched_email):
                    matched_stat = stat
                    break

            break

        if matched_client is None:
            return None

        total_bytes = 0
        used_bytes = 0
        expiry_ms = 0

        if matched_stat:
            total_bytes = int(matched_stat.get("total", 0) or 0)
            used_bytes = int((matched_stat.get("up", 0) or 0) + (matched_stat.get("down", 0) or 0))
            expiry_ms = int(matched_stat.get("expiryTime", 0) or 0)

        if total_bytes <= 0:
            total_bytes = int(matched_client.get("totalGB", 0) or 0)

        if expiry_ms <= 0:
            expiry_ms = int(matched_client.get("expiryTime", 0) or 0)

        remaining_bytes = max(total_bytes - used_bytes, 0)
        return {
            "uuid": client_uuid,
            "email": matched_email,
            "total_bytes": total_bytes,
            "used_bytes": used_bytes,
            "remaining_bytes": remaining_bytes,
            "expiry_ms": expiry_ms,
        }

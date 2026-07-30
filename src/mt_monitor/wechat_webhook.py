"""Safe delivery client for Enterprise WeChat robot webhooks."""

import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# `requests` is imported lazily inside send_text so the rest of the package
# (and the `import` command) stays usable without that dependency.

# Environment variable holding the robot URL; takes precedence over the file.
WEBHOOK_ENV = "QYWECHAT_WEBHOOK"


class WechatWebhookError(Exception):
    """A redacted webhook failure suitable for safe user-facing handling."""

    _MESSAGES = {
        "not_configured": "Enterprise WeChat webhook is not configured.",
        "invalid_configuration": "Enterprise WeChat webhook configuration is invalid.",
        "network_error": "Enterprise WeChat webhook request failed.",
        "http_error": "Enterprise WeChat webhook returned an unsuccessful response.",
        "invalid_response": "Enterprise WeChat webhook returned an invalid response.",
        "business_error": "Enterprise WeChat webhook did not accept the message.",
    }

    def __init__(self, code: str):
        self.code = code
        super().__init__(self._MESSAGES[code])


def load_webhook_url(path: Path | None = None) -> str:
    """Read and validate a robot webhook URL without disclosing its secret key.

    Resolution order: the ``QYWECHAT_WEBHOOK`` environment variable takes
    precedence; otherwise the file at ``path`` (config/notify) is read. Raises
    ``WechatWebhookError`` with code ``not_configured`` when neither is set, or
    ``invalid_configuration`` when the URL shape is wrong.
    """
    env_url = (os.environ.get(WEBHOOK_ENV) or "").strip()
    if env_url:
        source = env_url
    else:
        if path is None or not Path(path).exists():
            raise WechatWebhookError("not_configured")
        try:
            source = Path(path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise WechatWebhookError("invalid_configuration") from exc

    parsed = urlparse(source)
    keys = parse_qs(parsed.query, keep_blank_values=True).get("key", [])
    if not (
        source
        and parsed.scheme == "https"
        and parsed.netloc == "qyapi.weixin.qq.com"
        and parsed.path == "/cgi-bin/webhook/send"
        and not parsed.params
        and not parsed.fragment
        and len(keys) == 1
        and keys[0].strip()
    ):
        raise WechatWebhookError("invalid_configuration")
    return source


class WechatWebhookClient:
    """Deliver text messages only when the Enterprise WeChat API confirms success."""

    def __init__(self, url: str):
        self.url = url

    def send_text(self, content: str) -> None:
        try:
            import requests
        except ImportError as exc:
            raise WechatWebhookError("network_error") from exc
        payload = {"msgtype": "text", "text": {"content": content}}
        try:
            response = requests.post(self.url, json=payload, timeout=15)
        except requests.RequestException as exc:
            raise WechatWebhookError("network_error") from exc

        if response.status_code != 200:
            raise WechatWebhookError("http_error")
        try:
            body = response.json()
        except (ValueError, requests.RequestException) as exc:
            raise WechatWebhookError("invalid_response") from exc
        if not isinstance(body, dict) or body.get("errcode") != 0:
            raise WechatWebhookError("business_error")

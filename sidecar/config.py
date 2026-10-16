"""启动配置：只放连接地址、密码哈希这类改了需要重启的东西。
群号、文案等运行期可改的内容在数据库里，由后台管理。"""
import base64
import hashlib
import hmac
import secrets
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Config:
    onebot_url: str
    onebot_token: str
    web_hosts: list[str]
    web_port: int
    admin_password_hash: str
    data_dir: Path
    backup_dir: Path | None = None
    sealdice_backups: Path | None = None
    backup_keep: int = 8
    desktop_alert: bool = True

    @property
    def db_path(self) -> Path:
        return self.data_dir / "sidecar.db"


def load_config(path: Path) -> Config:
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    onebot, web, paths = raw.get("onebot", {}), raw.get("web", {}), raw.get("paths", {})
    backup, alert = raw.get("backup", {}), raw.get("alert", {})

    def resolve(value: str) -> Path | None:
        if not value:
            return None
        p = Path(value)
        return p if p.is_absolute() else path.parent / p
    return Config(
        onebot_url=onebot["url"],
        onebot_token=onebot.get("token", ""),
        web_hosts=list(web.get("hosts", ["127.0.0.1"])),
        web_port=int(web.get("port", 3220)),
        admin_password_hash=web.get("admin_password_hash", ""),
        data_dir=resolve(paths.get("data_dir", "data")),
        backup_dir=resolve(backup.get("dir", "")),
        sealdice_backups=resolve(backup.get("sealdice_backups", "")),
        backup_keep=int(backup.get("keep", 8)),
        desktop_alert=bool(alert.get("desktop", True)),
    )


# scrypt 参数：n=2^14, r=8, p=1，标准库自带，不需要额外依赖
_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_b64, digest_b64 = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    digest = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt_b64), n=_N, r=_R, p=_P)
    return hmac.compare_digest(digest, base64.b64decode(digest_b64))


def session_secret(data_dir: Path) -> str:
    """会话签名密钥，首次运行时生成并保存在数据目录。"""
    path = data_dir / "session_secret"
    if not path.exists():
        path.write_text(secrets.token_urlsafe(32), encoding="utf-8")
    return path.read_text(encoding="utf-8").strip()

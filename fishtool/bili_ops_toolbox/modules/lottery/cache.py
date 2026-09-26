"""抽奖评论与用户画像的本地 JSON 缓存。"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from core.logger import get_logger

logger = get_logger(__name__)


class LotteryCache:
    """集中管理抽奖缓存路径、容错读取和原子写入。"""

    def __init__(self, cache_dir: Path) -> None:
        """初始化缓存仓储。

        Args:
            cache_dir: 抽奖缓存根目录。

        Returns:
            无。
        """
        self.cache_dir = cache_dir

    def load_dynamic_comments(self, dynamic_id: str) -> List[Dict[str, Any]]:
        """读取动态评论缓存，缺失或损坏时返回空列表。

        Args:
            dynamic_id: B 站动态 ID。

        Returns:
            已缓存的标准评论列表。
        """
        try:
            path = self.dynamic_path(dynamic_id)
            if not path.exists():
                return []
            payload = json.loads(path.read_text(encoding="utf-8"))
            comments = payload.get("comments") if isinstance(payload, dict) else None
            return comments if isinstance(comments, list) else []
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("动态评论缓存读取失败: %s", exc)
            return []

    def save_dynamic_comments(self, dynamic_id: str, comments: List[Dict[str, Any]]) -> None:
        """原子保存动态评论缓存。

        Args:
            dynamic_id: B 站动态 ID。
            comments: 标准评论列表。

        Returns:
            无。
        """
        payload = {"saved_at": datetime.now().isoformat(), "comments": comments}
        self._write_json(self.dynamic_path(dynamic_id), payload, "动态评论")

    def load_profiles(self) -> Dict[str, Dict[str, Any]]:
        """读取用户画像缓存，结构异常时返回空字典。

        Returns:
            以字符串 UID 为键的画像映射。
        """
        try:
            path = self.profile_path()
            if not path.exists():
                return {}
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("读取抽奖用户画像缓存失败: %s", exc)
            return {}

    def save_profiles(self, profiles: Dict[str, Dict[str, Any]]) -> None:
        """原子保存用户画像缓存。

        Args:
            profiles: 以字符串 UID 为键的画像映射。

        Returns:
            无。
        """
        self._write_json(self.profile_path(), profiles, "抽奖用户画像")

    def dynamic_path(self, dynamic_id: str) -> Path:
        """返回指定动态的缓存路径。

        Args:
            dynamic_id: B 站动态 ID。

        Returns:
            动态评论 JSON 路径。
        """
        return self.cache_dir / f"dynamic_{dynamic_id}.json"

    def profile_path(self) -> Path:
        """返回用户画像缓存路径。

        Returns:
            用户画像 JSON 路径。
        """
        return self.cache_dir / "user_profiles.json"

    def _write_json(self, target: Path, payload: Any, label: str) -> None:
        """通过同目录临时文件原子写入 JSON。

        Args:
            target: 最终文件路径。
            payload: 可 JSON 序列化的数据。
            label: 日志中的缓存名称。

        Returns:
            无。
        """
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            temporary.replace(target)
        except (OSError, TypeError) as exc:
            logger.warning("写入%s缓存失败，不影响本次结果: %s", label, exc)

"""文件验证模块：放置和清理 ACME 验证文件。

写入方式对齐宝塔 acme_v2.py write_auth_file()：
- 写入 {站点根目录}/.well-known/acme-challenge/{token}
- 设置文件所有者为 www（通过 public.set_own）
"""

import os


def _set_own(path, user='www'):
    """设置文件/目录所有者，对齐宝塔 public.set_own()"""
    try:
        import public
        public.set_own(path, user)
    except (ImportError, AttributeError, Exception):
        pass  # 非宝塔环境（测试等）跳过


class FileVerifier:
    """验证文件操作，封装放置和清理逻辑"""

    def __init__(self, site_manager, logger=None):
        self._site_mgr = site_manager
        self._logger = logger

    def place_file(self, file_info, site_names):
        """将验证文件写入匹配站点的根目录

        Args:
            file_info: {"path": ".well-known/acme-challenge/xxx", "content": "..."}
            site_names: 证书绑定的站点名称列表

        Returns:
            list[str]: 已写入的完整文件路径列表
        """
        if not file_info:
            return []

        rel_path = file_info.get('path', '')
        content = file_info.get('content', '')
        if not rel_path or not content:
            return []

        # 路径安全校验
        if not self._is_safe_path(rel_path):
            if self._logger:
                self._logger.error("验证文件路径不安全，拒绝写入: %s", rel_path)
            return []

        placed = []
        for site_name in site_names:
            site = self._site_mgr.get_site(site_name)
            if not site:
                if self._logger:
                    self._logger.warn("站点不存在，跳过: %s", site_name)
                continue

            site_root = site.get('path', '')
            if not site_root:
                continue

            full_path = os.path.join(site_root, rel_path)
            try:
                os.makedirs(os.path.dirname(full_path), exist_ok=True)
                with open(full_path, 'w', encoding='utf-8') as f:
                    f.write(content)
                # 对齐宝塔：设置 .well-known 路径所有者为 www
                wellknown_path = os.path.join(site_root, '.well-known')
                _set_own(wellknown_path, 'www')
                _set_own(full_path, 'www')
                placed.append(full_path)
                if self._logger:
                    self._logger.info("验证文件已放置: %s", full_path)
            except OSError as e:
                if self._logger:
                    self._logger.error("写入验证文件失败: %s, error=%s", full_path, str(e))

        return placed

    def cleanup_files(self, placed_paths):
        """清理已放置的验证文件

        Args:
            placed_paths: place_file() 返回的路径列表
        """
        if not placed_paths:
            return

        for path in placed_paths:
            try:
                if os.path.isfile(path):
                    os.remove(path)
                    if self._logger:
                        self._logger.info("验证文件已清理: %s", path)
                # 尝试清理空目录（acme-challenge → .well-known）
                dir_path = os.path.dirname(path)
                for _ in range(2):
                    if dir_path and os.path.isdir(dir_path) and not os.listdir(dir_path):
                        os.rmdir(dir_path)
                        dir_path = os.path.dirname(dir_path)
                    else:
                        break
            except OSError as e:
                if self._logger:
                    self._logger.warn("清理验证文件失败: %s, error=%s", path, str(e))

    @staticmethod
    def _is_safe_path(rel_path):
        """校验相对路径安全性"""
        if '..' in rel_path:
            return False
        if not rel_path.startswith('.well-known/'):
            return False
        # 规范化后再次检查
        normalized = os.path.normpath(rel_path)
        if normalized.startswith('..') or os.path.isabs(normalized):
            return False
        return True

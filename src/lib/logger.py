"""日志模块，含敏感信息过滤。对标 sslctl pkg/logger/logger.go"""

import os
import re
import time
import glob
import logging

# 敏感信息过滤正则
_FILTERS = [
    (re.compile(r'-----BEGIN[^-]*PRIVATE KEY-----[\s\S]*?-----END[^-]*PRIVATE KEY-----'),
     '***REDACTED PRIVATE KEY***'),
    (re.compile(r'Bearer\s+[A-Za-z0-9\-_\.]+'),
     'Bearer ***REDACTED***'),
    (re.compile(r'Basic\s+[A-Za-z0-9+/=]+'),
     'Basic ***REDACTED***'),
    # JSON 敏感字段（双引号，含 api_token 等复合词）
    (re.compile(r'"(\w*(?:token|secret|password|api_?key|private_key)\w*)"\s*:\s*"[^"]*"'),
     lambda m: '"%s": "***REDACTED***"' % m.group(1)),
    # dict repr 敏感字段（单引号，含复合词），覆盖 args 传入的 dict/list 参数
    (re.compile(r"'(\w*(?:token|secret|password|api_?key|private_key)\w*)'\s*:\s*'[^']*'"),
     lambda m: "'%s': '***REDACTED***'" % m.group(1)),
    # key=value 形式（URL 参数等，含复合词）
    (re.compile(r'(\w*(?:token|secret|password|api_?key)\w*)=["\']?[^"\'\s&]+["\']?'),
     lambda m: '%s=***REDACTED***' % m.group(1)),
]

MAX_LOG_FILES = 90


def sanitize(text):
    """过滤敏感信息"""
    for pattern, replacement in _FILTERS:
        if callable(replacement):
            text = pattern.sub(replacement, text)
        else:
            text = pattern.sub(replacement, text)
    return text


class SensitiveFilter(logging.Filter):
    def filter(self, record):
        # 先按 args 格式化出完整消息，再整串脱敏，确保 dict/list 等非 str 参数也被覆盖
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        record.msg = sanitize(message)
        record.args = None
        return True


class Logger:
    def __init__(self, log_dir, name='sslbt'):
        self._log_dir = log_dir
        self._name = name
        self._logger = logging.getLogger(name)
        self._logger.setLevel(logging.DEBUG)
        # 清除已有 handler/filter，防止多次实例化导致重复日志
        self._logger.handlers.clear()
        self._logger.filters.clear()
        self._logger.addFilter(SensitiveFilter())
        self._current_date = None
        self._handler = None
        self._setup_handler()

    def _setup_handler(self):
        today = time.strftime('%Y-%m-%d')
        if self._current_date == today and self._handler:
            return
        self._current_date = today
        if self._handler:
            self._logger.removeHandler(self._handler)
            self._handler.close()
        os.makedirs(self._log_dir, exist_ok=True)
        log_file = os.path.join(self._log_dir, '%s-%s.log' % (self._name, today))
        self._handler = logging.FileHandler(log_file, encoding='utf-8')
        self._handler.setFormatter(logging.Formatter(
            '[%(asctime)s] [%(levelname)s] %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        ))
        self._logger.addHandler(self._handler)
        self._cleanup_old_logs()

    def _cleanup_old_logs(self):
        pattern = os.path.join(self._log_dir, '%s-*.log' % self._name)
        files = sorted(glob.glob(pattern))
        for f in files[:-MAX_LOG_FILES]:
            try:
                os.remove(f)
            except OSError:
                pass

    def _ensure_date(self):
        today = time.strftime('%Y-%m-%d')
        if self._current_date != today:
            self._setup_handler()

    def debug(self, msg, *args):
        self._ensure_date()
        self._logger.debug(msg, *args)

    def info(self, msg, *args):
        self._ensure_date()
        self._logger.info(msg, *args)

    def warning(self, msg, *args):
        self._ensure_date()
        self._logger.warning(msg, *args)

    def error(self, msg, *args):
        self._ensure_date()
        self._logger.error(msg, *args)

    def get_logs(self, date=None, lines=200):
        """读取指定日期的日志，默认今天，返回最后 lines 行"""
        if date is None:
            date = time.strftime('%Y-%m-%d')
        log_file = os.path.join(self._log_dir, '%s-%s.log' % (self._name, date))
        if not os.path.isfile(log_file):
            return ''
        with open(log_file, 'r', encoding='utf-8', errors='replace') as f:
            all_lines = f.readlines()
        return ''.join(all_lines[-lines:])

    def get_log_dates(self):
        """返回可用的日志日期列表"""
        pattern = os.path.join(self._log_dir, '%s-*.log' % self._name)
        files = sorted(glob.glob(pattern), reverse=True)
        dates = []
        prefix = '%s-' % self._name
        for f in files:
            name = os.path.basename(f)
            if name.startswith(prefix) and name.endswith('.log'):
                dates.append(name[len(prefix):-4])
        return dates

    def clear_logs(self):
        """清除所有日志"""
        pattern = os.path.join(self._log_dir, '%s-*.log' % self._name)
        for f in glob.glob(pattern):
            try:
                os.remove(f)
            except OSError:
                pass
        self._current_date = None
        self._setup_handler()

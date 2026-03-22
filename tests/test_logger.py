"""日志模块测试"""

import os
import time
import glob
import logging
import pytest
from unittest.mock import patch

from lib.logger import Logger, sanitize, MAX_LOG_FILES


class TestSanitize:
    def test_private_key(self):
        text = 'key: -----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----'
        result = sanitize(text)
        assert '***REDACTED PRIVATE KEY***' in result
        assert 'MIIE' not in result

    def test_bearer_token(self):
        text = 'Authorization: Bearer abc123.xyz-456_789'
        result = sanitize(text)
        assert 'Bearer ***REDACTED***' in result
        assert 'abc123' not in result

    def test_basic_auth(self):
        text = 'Authorization: Basic dXNlcjpwYXNz'
        result = sanitize(text)
        assert 'Basic ***REDACTED***' in result

    def test_json_token(self):
        text = '"token": "my-secret-value"'
        result = sanitize(text)
        assert '"token": "***REDACTED***"' in result
        assert 'my-secret-value' not in result

    def test_json_password(self):
        text = '"password": "super-secret"'
        result = sanitize(text)
        assert '"password": "***REDACTED***"' in result

    def test_query_param_token(self):
        text = 'url?token=abc123&order=456'
        result = sanitize(text)
        assert 'token=***REDACTED***' in result
        assert 'abc123' not in result
        assert 'order=456' in result

    def test_no_false_positive(self):
        text = '证书部署成功: site=example.com, order_id=12345'
        result = sanitize(text)
        assert result == text

    def test_ec_private_key(self):
        text = '-----BEGIN EC PRIVATE KEY-----\nMHQ...\n-----END EC PRIVATE KEY-----'
        result = sanitize(text)
        assert '***REDACTED PRIVATE KEY***' in result


class TestLogger:
    def test_log_to_file(self, tmp_data_dir):
        log_dir = os.path.join(tmp_data_dir, 'logs')
        logger = Logger(log_dir)
        logger.info("测试消息 %s", "hello")
        content = logger.get_logs()
        assert '测试消息 hello' in content

    def test_get_logs_empty(self, tmp_data_dir):
        log_dir = os.path.join(tmp_data_dir, 'logs')
        logger = Logger(log_dir)
        content = logger.get_logs(date='2000-01-01')
        assert content == ''

    def test_get_log_dates(self, tmp_data_dir):
        log_dir = os.path.join(tmp_data_dir, 'logs')
        logger = Logger(log_dir)
        logger.info("test")
        dates = logger.get_log_dates()
        today = time.strftime('%Y-%m-%d')
        assert today in dates

    def test_clear_logs(self, tmp_data_dir):
        log_dir = os.path.join(tmp_data_dir, 'logs')
        logger = Logger(log_dir)
        logger.info("will be cleared")
        logger.clear_logs()
        content = logger.get_logs()
        assert 'will be cleared' not in content

    def test_sensitive_filter_in_log(self, tmp_data_dir):
        """敏感信息通过日志过滤器被替换"""
        log_dir = os.path.join(tmp_data_dir, 'logs')
        logger = Logger(log_dir)
        logger.info("Bearer abc123.xyz-456_789")
        content = logger.get_logs()
        assert 'abc123' not in content
        assert 'REDACTED' in content

    def test_cleanup_old_logs(self, tmp_data_dir):
        """超过 MAX_LOG_FILES 个文件自动清理"""
        log_dir = os.path.join(tmp_data_dir, 'logs')
        os.makedirs(log_dir, exist_ok=True)
        # 创建超过限制的日志文件
        for i in range(MAX_LOG_FILES + 5):
            path = os.path.join(log_dir, 'sslbt-2020-01-%02d.log' % (i + 1))
            with open(path, 'w') as f:
                f.write('test')
        logger = Logger(log_dir)
        files = glob.glob(os.path.join(log_dir, 'sslbt-*.log'))
        assert len(files) <= MAX_LOG_FILES + 1  # +1 for today's log

    def test_log_lines_limit(self, tmp_data_dir):
        """get_logs 返回最后 N 行"""
        log_dir = os.path.join(tmp_data_dir, 'logs')
        logger = Logger(log_dir)
        for i in range(10):
            logger.info("line %d", i)
        content = logger.get_logs(lines=3)
        lines = content.strip().split('\n')
        assert len(lines) == 3

    def test_no_duplicate_handlers(self, tmp_data_dir):
        """多次实例化不会累积 handler"""
        log_dir = os.path.join(tmp_data_dir, 'logs')
        name = 'test_dup_%s' % id(self)
        Logger(log_dir, name=name)
        Logger(log_dir, name=name)
        Logger(log_dir, name=name)
        py_logger = logging.getLogger(name)
        assert len(py_logger.handlers) == 1
        assert len(py_logger.filters) == 1

"""证书工具测试"""

import subprocess
from unittest.mock import MagicMock

import pytest

from lib.cert_utils import (
    validate_cert_pem, validate_key_pem, build_fullchain, parse_cert_info,
    validate_site_name_component,
    PEM_CERT_RE, PEM_KEY_RE,
)


class TestValidation:
    def test_valid_cert_pem(self):
        cert = "-----BEGIN CERTIFICATE-----\nMIIBtest\n-----END CERTIFICATE-----"
        ok, err = validate_cert_pem(cert)
        assert ok is True

    def test_invalid_cert_pem(self):
        ok, err = validate_cert_pem("not a cert")
        assert ok is False
        assert 'PEM' in err

    def test_empty_cert(self):
        ok, err = validate_cert_pem("")
        assert ok is False

    def test_valid_key_pem(self):
        key = "-----BEGIN RSA PRIVATE KEY-----\nMIIBtest\n-----END RSA PRIVATE KEY-----"
        ok, err = validate_key_pem(key)
        assert ok is True

    def test_ec_key_pem(self):
        key = "-----BEGIN EC PRIVATE KEY-----\nMIIBtest\n-----END EC PRIVATE KEY-----"
        ok, err = validate_key_pem(key)
        assert ok is True

    def test_invalid_key_pem(self):
        ok, err = validate_key_pem("not a key")
        assert ok is False

    def test_key_size_limit(self):
        key = "-----BEGIN RSA PRIVATE KEY-----\n" + "A" * 20000 + "\n-----END RSA PRIVATE KEY-----"
        ok, err = validate_key_pem(key)
        assert ok is False
        assert '大小限制' in err


class TestBuildFullchain:
    def test_cert_with_ca(self):
        cert = "-----BEGIN CERTIFICATE-----\nLEAFCERT\n-----END CERTIFICATE-----"
        ca = "-----BEGIN CERTIFICATE-----\nCACERT\n-----END CERTIFICATE-----"
        chain = build_fullchain(cert, ca)
        assert 'LEAFCERT' in chain
        assert 'CACERT' in chain
        assert chain.index('LEAFCERT') < chain.index('CACERT')

    def test_cert_without_ca(self):
        cert = "-----BEGIN CERTIFICATE-----\nLEAF\n-----END CERTIFICATE-----"
        chain = build_fullchain(cert, '')
        assert 'LEAF' in chain
        assert chain.endswith('\n')


class TestRegex:
    def test_cert_regex(self):
        pem = "-----BEGIN CERTIFICATE-----\nMIIBtest\n-----END CERTIFICATE-----"
        assert PEM_CERT_RE.search(pem) is not None

    def test_key_regex_rsa(self):
        pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIBtest\n-----END RSA PRIVATE KEY-----"
        assert PEM_KEY_RE.search(pem) is not None

    def test_key_regex_ec(self):
        pem = "-----BEGIN EC PRIVATE KEY-----\nMIIBtest\n-----END EC PRIVATE KEY-----"
        assert PEM_KEY_RE.search(pem) is not None

    def test_key_regex_generic(self):
        pem = "-----BEGIN PRIVATE KEY-----\nMIIBtest\n-----END PRIVATE KEY-----"
        assert PEM_KEY_RE.search(pem) is not None


class TestSanParsing:
    def test_parse_failure_logs_bounded_openssl_stderr(self, monkeypatch):
        """OpenSSL 失败时记录退出码、实际路径和截断后的 stderr，不记录证书正文"""
        logger = MagicMock()
        pem = "-----BEGIN CERTIFICATE-----\nSECRET-CERT-BODY\n-----END CERTIFICATE-----"
        stderr = 'unknown option -ext ' + 'x' * 400
        monkeypatch.setattr(
            subprocess, 'run',
            lambda command, **kwargs: subprocess.CompletedProcess(
                command, 1, stdout='', stderr=stderr))
        monkeypatch.setattr('lib.cert_utils.shutil.which',
                            lambda command: '/usr/bin/openssl')

        assert parse_cert_info(pem, logger=logger) is None

        logger.error.assert_called_once()
        log_args = logger.error.call_args.args
        message = log_args[0] % log_args[1:]
        assert 'reason=openssl_exit_1' in message
        assert 'openssl=/usr/bin/openssl' in message
        assert 'unknown option -ext' in message
        assert 'SECRET-CERT-BODY' not in message
        assert len(message.split('detail=', 1)[1]) == 259

    def test_invalid_not_after_logs_locale(self, monkeypatch):
        """日期无法解析时记录原始日期与 LC_TIME，便于识别 locale 问题"""
        logger = MagicMock()
        output = """subject= /CN=example.com
notBefore=Jan  1 00:00:00 2026 GMT
notAfter=invalid-date
serial=ABC123
issuer= /CN=Test CA
"""
        monkeypatch.setattr(
            subprocess, 'run',
            lambda command, **kwargs: subprocess.CompletedProcess(
                command, 0, stdout=output, stderr=''))
        monkeypatch.setattr('lib.cert_utils.locale.setlocale',
                            lambda category: 'zh_CN.UTF-8')

        assert parse_cert_info('certificate', logger=logger) is None

        log_args = logger.error.call_args.args
        message = log_args[0] % log_args[1:]
        assert 'reason=invalid_not_after' in message
        assert 'value=invalid-date' in message
        assert 'LC_TIME=zh_CN.UTF-8' in message

    def test_parse_cert_info_with_runtime_openssl(self, tmp_path):
        """用 PATH 中的真实 openssl 生成并解析含 DNS/IP SAN 的证书"""
        config_file = tmp_path / 'openssl.cnf'
        cert_file = tmp_path / 'cert.pem'
        key_file = tmp_path / 'key.pem'
        config_file.write_text("""[req]
distinguished_name = dn
x509_extensions = v3
prompt = no

[dn]
CN = runtime.example

[v3]
subjectAltName = DNS:runtime.example,DNS:www.runtime.example,IP:192.0.2.1
""")
        subprocess.run(
            ['openssl', 'req', '-new', '-x509', '-nodes', '-newkey', 'rsa:2048',
             '-keyout', str(key_file), '-out', str(cert_file), '-days', '1',
             '-config', str(config_file)],
            capture_output=True, text=True, check=True, timeout=30,
        )

        info = parse_cert_info(cert_file.read_text())

        assert info is not None
        assert info['common_name'] == 'runtime.example'
        assert info.get('not_after') is not None
        assert info.get('serial')
        assert info['domains'] == [
            'runtime.example', 'www.runtime.example', '192.0.2.1']

    def test_parse_cert_info_supports_openssl_1_0_2(self, monkeypatch):
        """OpenSSL 1.0.2 无 x509 -ext，仍应通过 -text 解析证书信息"""
        output = """subject= /CN=example.com
notBefore=Jan  1 00:00:00 2026 GMT
notAfter=Apr  1 00:00:00 2027 GMT
serial=ABC123
issuer= /CN=Test CA
        X509v3 Subject Alternative Name:
            DNS:example.com, DNS:www.example.com, IP Address:192.0.2.1
"""
        commands = []

        def fake_run(command, **kwargs):
            commands.append(command)
            if '-ext' in command:
                return subprocess.CompletedProcess(
                    command, 1, stdout='', stderr='unknown option -ext')
            return subprocess.CompletedProcess(command, 0, stdout=output, stderr='')

        monkeypatch.setattr(subprocess, 'run', fake_run)

        info = parse_cert_info(
            "-----BEGIN CERTIFICATE-----\nTEST\n-----END CERTIFICATE-----")

        assert info is not None
        assert info['common_name'] == 'example.com'
        assert info['not_after'].year == 2027
        assert info['serial'] == 'ABC123'
        assert info['domains'] == ['example.com', 'www.example.com', '192.0.2.1']
        assert '-text' in commands[0]
        assert '-ext' not in commands[0]

    def test_multiline_san_extraction(self):
        """SAN 跨多行输出时也能正确提取"""
        import re
        # 模拟 openssl 输出中的多行 SAN
        output = """subject=CN = example.com
notBefore=Jan  1 00:00:00 2026 GMT
notAfter=Apr  1 00:00:00 2026 GMT
serial=ABC123
issuer=CN = Test CA
            DNS:example.com, DNS:www.example.com,
            DNS:mail.example.com, DNS:*.example.com"""
        san_entries = re.findall(r'DNS:([^\s,]+)', output)
        assert len(san_entries) == 4
        assert 'example.com' in san_entries
        assert 'www.example.com' in san_entries
        assert 'mail.example.com' in san_entries
        assert '*.example.com' in san_entries

    def test_single_line_san(self):
        """单行 SAN 正常工作"""
        import re
        output = "DNS:a.com, DNS:b.com, DNS:c.com"
        san_entries = re.findall(r'DNS:([^\s,]+)', output)
        assert san_entries == ['a.com', 'b.com', 'c.com']

    def test_ip_san_extraction(self):
        """IP Address SAN 提取"""
        import re
        output = "DNS:example.com, IP Address:192.168.1.1, IP Address:10.0.0.1"
        dns_entries = re.findall(r'DNS:([^\s,]+)', output)
        ip_entries = re.findall(r'IP Address:([^\s,]+)', output)
        assert dns_entries == ['example.com']
        assert ip_entries == ['192.168.1.1', '10.0.0.1']

    def test_mixed_dns_ip_san(self):
        """DNS 和 IP 混合 SAN 多行输出"""
        import re
        output = """subject=CN = example.com
            DNS:example.com, DNS:www.example.com,
            IP Address:192.168.1.1, IP Address:2001:db8::1"""
        dns_entries = re.findall(r'DNS:([^\s,]+)', output)
        ip_entries = re.findall(r'IP Address:([^\s,]+)', output)
        assert len(dns_entries) == 2
        assert len(ip_entries) == 2
        assert '192.168.1.1' in ip_entries
        assert '2001:db8::1' in ip_entries

    def test_pure_ip_san(self):
        """纯 IP 证书（无 DNS SAN）"""
        import re
        output = "subject=CN = 192.168.1.1\nIP Address:192.168.1.1"
        dns_entries = re.findall(r'DNS:([^\s,]+)', output)
        ip_entries = re.findall(r'IP Address:([^\s,]+)', output)
        assert dns_entries == []
        assert ip_entries == ['192.168.1.1']


class TestValidateSiteNameComponent:
    """site_name 用作路径组件时的穿越防护：拒绝空/分隔符/../绝对路径"""

    @pytest.mark.parametrize('bad', [
        '../x', '..', '/etc/passwd', 'a/b', 'a\\b',
        '../../etc/passwd', 'foo/..', '/', '\\', 'C:\\Windows',
    ])
    def test_rejects_malicious(self, bad):
        assert validate_site_name_component(bad) is not None

    @pytest.mark.parametrize('good', [
        'www.example.com', 'example.com', 'sub.example.com',
        'a.b.c.example.com', 'my-site_01.example.com',
    ])
    def test_accepts_normal_domain(self, good):
        assert validate_site_name_component(good) is None

    def test_rejects_empty_and_non_str(self):
        assert validate_site_name_component('') is not None
        assert validate_site_name_component(None) is not None
        assert validate_site_name_component(123) is not None

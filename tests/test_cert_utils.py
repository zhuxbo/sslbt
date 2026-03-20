"""证书工具测试"""

import pytest
from lib.cert_utils import (
    validate_cert_pem, validate_key_pem, build_fullchain,
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

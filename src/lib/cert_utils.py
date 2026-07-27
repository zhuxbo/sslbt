"""证书工具模块：验证、解析、CSR 生成。对标 sslctl pkg/csr + pkg/validator"""

import os
import re
import hashlib
import locale
import shutil
import tempfile
from datetime import datetime, timezone

# OpenSSL 通过 ssl 模块间接使用，CSR 生成需要 subprocess 调用 openssl 命令行
import subprocess

PEM_CERT_RE = re.compile(
    r'-----BEGIN CERTIFICATE-----\s*\S[\s\S]*?-----END CERTIFICATE-----'
)
PEM_KEY_RE = re.compile(
    r'-----BEGIN (?:RSA |EC )?PRIVATE KEY-----\s*\S[\s\S]*?-----END (?:RSA |EC )?PRIVATE KEY-----'
)
PEM_CSR_RE = re.compile(
    r'-----BEGIN CERTIFICATE REQUEST-----\s*\S[\s\S]*?-----END CERTIFICATE REQUEST-----'
)

MAX_CERT_SIZE = 65536  # 64KB
MAX_KEY_SIZE = 16384  # 16KB
MAX_PARSE_DIAGNOSTIC_SIZE = 256


def validate_cert_pem(pem_text):
    """验证 PEM 证书格式"""
    if not pem_text or len(pem_text) > MAX_CERT_SIZE:
        return False, '证书内容为空或超过大小限制'
    if not PEM_CERT_RE.search(pem_text):
        return False, '无效的 PEM 证书格式'
    return True, ''


def validate_key_pem(pem_text):
    """验证 PEM 私钥格式"""
    if not pem_text or len(pem_text) > MAX_KEY_SIZE:
        return False, '私钥内容为空或超过大小限制'
    if not PEM_KEY_RE.search(pem_text):
        return False, '无效的 PEM 私钥格式'
    return True, ''


def validate_site_name_component(site_name):
    """校验 site_name 用作文件路径组件的安全性，合法返回 None，否则返回错误原因。

    site_name 在部分场景直接拼入证书目录路径（deployer 回退读证书、site_manager
    SSL 检测），须拒绝可造成目录穿越或绝对路径逃逸的取值：空值、路径分隔符
    （/ 与 \\）、上级目录引用（..）、绝对路径。纯展示/匹配用途不受此约束。
    """
    if not site_name or not isinstance(site_name, str):
        return '站点名为空或类型非法'
    if '/' in site_name or '\\' in site_name:
        return '站点名含路径分隔符'
    if '..' in site_name:
        return '站点名含上级目录引用'
    if os.path.isabs(site_name):
        return '站点名为绝对路径'
    return None


def _log_parse_failure(logger, reason, detail=''):
    """记录不含证书正文的解析诊断；日志失败不得影响证书处理。"""
    if not logger:
        return
    clean_detail = ' '.join(str(detail).split())
    if len(clean_detail) > MAX_PARSE_DIAGNOSTIC_SIZE:
        clean_detail = clean_detail[:MAX_PARSE_DIAGNOSTIC_SIZE] + '...'
    openssl_path = shutil.which('openssl') or '(PATH 中未找到)'
    try:
        logger.error(
            "证书解析诊断: reason=%s, openssl=%s, detail=%s",
            reason, openssl_path, clean_detail or '(无)',
        )
    except Exception:
        pass


def parse_cert_info(pem_text, logger=None):
    """解析证书信息，返回 dict: common_name, domains, not_before, not_after, serial, issuer"""
    tmp = None
    try:
        tmp = tempfile.NamedTemporaryFile(suffix='.pem', delete=False, mode='w')
        tmp.write(pem_text)
        tmp.close()

        result = subprocess.run(
            ['openssl', 'x509', '-in', tmp.name, '-noout',
             '-subject', '-dates', '-serial', '-issuer', '-text'],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            _log_parse_failure(
                logger, 'openssl_exit_%s' % result.returncode, result.stderr)
            return None

        output = result.stdout
        info = {}

        # Common Name
        cn_match = re.search(r'CN\s*=\s*([^\s/,]+)', output)
        info['common_name'] = cn_match.group(1) if cn_match else ''

        # Dates
        nb_match = re.search(r'notBefore=(.+)', output)
        na_match = re.search(r'notAfter=(.+)', output)
        if nb_match:
            info['not_before'] = _parse_openssl_date(nb_match.group(1).strip())
        if na_match:
            not_after_text = na_match.group(1).strip()
            info['not_after'] = _parse_openssl_date(not_after_text)
            if info['not_after'] is None:
                try:
                    lc_time = locale.setlocale(locale.LC_TIME)
                except Exception:
                    lc_time = '(读取失败)'
                _log_parse_failure(
                    logger, 'invalid_not_after',
                    'value=%s, LC_TIME=%s' % (not_after_text, lc_time))
                return None
        else:
            _log_parse_failure(logger, 'missing_not_after', result.stderr)
            return None

        # Serial
        serial_match = re.search(r'serial=([A-Fa-f0-9]+)', output)
        info['serial'] = serial_match.group(1).upper() if serial_match else ''

        # Issuer
        issuer_match = re.search(r'issuer=(.+)', output)
        info['issuer'] = issuer_match.group(1).strip() if issuer_match else ''

        # SAN domains（支持多行输出，含 DNS 和 IP Address）
        domains = [info['common_name']] if info.get('common_name') else []
        san_entries = re.findall(r'DNS:([^\s,]+)', output)
        for d in san_entries:
            d = d.strip()
            if d and d not in domains:
                domains.append(d)
        ip_entries = re.findall(r'IP Address:([^\s,]+)', output)
        for ip in ip_entries:
            ip = ip.strip()
            if ip and ip not in domains:
                domains.append(ip)
        info['domains'] = domains

        # 过期天数
        if 'not_after' in info:
            now = datetime.now(timezone.utc)
            delta = info['not_after'] - now
            info['days_remaining'] = delta.days
        return info
    except Exception as e:
        _log_parse_failure(
            logger, 'exception_%s' % type(e).__name__, str(e))
        return None
    finally:
        if tmp and os.path.exists(tmp.name):
            os.unlink(tmp.name)


def _parse_openssl_date(date_str):
    """解析 openssl 日期格式: 'Mar  1 00:00:00 2026 GMT'"""
    for fmt in ('%b %d %H:%M:%S %Y %Z', '%b  %d %H:%M:%S %Y %Z'):
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def verify_cert_key_match(cert_pem, key_pem):
    """验证证书和私钥是否匹配"""
    cert_tmp = key_tmp = None
    try:
        cert_tmp = tempfile.NamedTemporaryFile(suffix='.pem', delete=False, mode='w')
        cert_tmp.write(cert_pem)
        cert_tmp.close()

        key_tmp = tempfile.NamedTemporaryFile(suffix='.key', delete=False, mode='w')
        key_tmp.write(key_pem)
        key_tmp.close()

        cert_mod = subprocess.run(
            ['openssl', 'x509', '-in', cert_tmp.name, '-noout', '-modulus'],
            capture_output=True, text=True, timeout=10
        )
        key_mod = subprocess.run(
            ['openssl', 'rsa', '-in', key_tmp.name, '-noout', '-modulus'],
            capture_output=True, text=True, timeout=10
        )

        if cert_mod.returncode != 0 or key_mod.returncode != 0:
            # 尝试 EC key
            if key_mod.returncode != 0:
                cert_pub = subprocess.run(
                    ['openssl', 'x509', '-in', cert_tmp.name, '-noout', '-pubkey'],
                    capture_output=True, text=True, timeout=10
                )
                key_pub = subprocess.run(
                    ['openssl', 'ec', '-in', key_tmp.name, '-pubout'],
                    capture_output=True, text=True, timeout=10
                )
                if cert_pub.returncode == 0 and key_pub.returncode == 0:
                    return cert_pub.stdout.strip() == key_pub.stdout.strip()
            return False

        return cert_mod.stdout.strip() == key_mod.stdout.strip()
    except Exception:
        return False
    finally:
        for f in (cert_tmp, key_tmp):
            if f and os.path.exists(f.name):
                os.unlink(f.name)


def parse_csr_info(csr_pem):
    """校验 CSR 签名并返回稳定的归属信息。

    返回 ``{'hash', 'common_name', 'public_key_der'}``；CSR 缺失、格式错误、
    签名无效或公钥无法导出时返回 None。hash 基于解析后的 DER，不受 PEM
    换行和包裹格式影响。
    """
    if not csr_pem or not PEM_CSR_RE.search(str(csr_pem)):
        return None
    csr_tmp = None
    try:
        csr_tmp = tempfile.NamedTemporaryFile(suffix='.csr', delete=False, mode='w')
        csr_tmp.write(csr_pem)
        csr_tmp.close()

        verify = subprocess.run(
            ['openssl', 'req', '-in', csr_tmp.name, '-noout', '-verify', '-subject'],
            capture_output=True, text=True, timeout=10,
        )
        if verify.returncode != 0:
            return None
        der = subprocess.run(
            ['openssl', 'req', '-in', csr_tmp.name, '-outform', 'DER'],
            capture_output=True, timeout=10,
        )
        pub = subprocess.run(
            ['openssl', 'req', '-in', csr_tmp.name, '-noout', '-pubkey'],
            capture_output=True, timeout=10,
        )
        if der.returncode != 0 or pub.returncode != 0:
            return None
        pub_der = subprocess.run(
            ['openssl', 'pkey', '-pubin', '-outform', 'DER'],
            input=pub.stdout, capture_output=True, timeout=10,
        )
        if pub_der.returncode != 0:
            return None
        subject = verify.stdout or ''
        cn_match = re.search(r'(?:^|[,/])\s*CN\s*=\s*([^,/]+)', subject)
        return {
            'hash': hashlib.sha256(der.stdout).hexdigest(),
            'common_name': cn_match.group(1).strip() if cn_match else '',
            'public_key_der': pub_der.stdout,
        }
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        if csr_tmp and os.path.exists(csr_tmp.name):
            os.unlink(csr_tmp.name)


def verify_csr_key_match(csr_pem, key_pem):
    """验证 CSR 公钥与私钥公钥是否一致。"""
    csr_info = parse_csr_info(csr_pem)
    if not csr_info:
        return False
    key_tmp = None
    try:
        key_tmp = tempfile.NamedTemporaryFile(suffix='.key', delete=False, mode='w')
        key_tmp.write(key_pem)
        key_tmp.close()
        key_pub = subprocess.run(
            ['openssl', 'pkey', '-in', key_tmp.name, '-pubout', '-outform', 'DER'],
            capture_output=True, timeout=10,
        )
        return key_pub.returncode == 0 and key_pub.stdout == csr_info['public_key_der']
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        if key_tmp and os.path.exists(key_tmp.name):
            os.unlink(key_tmp.name)


def generate_csr(domains, key_type='rsa', key_size=2048):
    """生成 CSR 和私钥。仅使用 CN，不添加 SAN。

    返回 (csr_pem, key_pem, csr_hash)
    """
    cn = domains[0] if isinstance(domains, list) else domains
    key_file = csr_file = None
    try:
        key_file = tempfile.NamedTemporaryFile(suffix='.key', delete=False)
        key_file.close()
        csr_file = tempfile.NamedTemporaryFile(suffix='.csr', delete=False)
        csr_file.close()

        if key_type == 'ecdsa':
            curve = 'prime256v1'
            subprocess.run(
                ['openssl', 'ecparam', '-genkey', '-name', curve, '-out', key_file.name],
                capture_output=True, check=True, timeout=30
            )
        else:
            subprocess.run(
                ['openssl', 'genrsa', '-out', key_file.name, str(key_size)],
                capture_output=True, check=True, timeout=30
            )

        subprocess.run(
            ['openssl', 'req', '-new', '-key', key_file.name, '-out', csr_file.name,
             '-subj', '/CN=%s' % cn],
            capture_output=True, check=True, timeout=30
        )

        with open(key_file.name, 'r') as f:
            key_pem = f.read()
        with open(csr_file.name, 'r') as f:
            csr_pem = f.read()

        csr_info = parse_csr_info(csr_pem)
        if not csr_info:
            raise RuntimeError("CSR 生成后校验失败")
        csr_hash = csr_info['hash']

        return csr_pem, key_pem, csr_hash
    except subprocess.CalledProcessError as e:
        raise RuntimeError("CSR 生成失败: %s" % (e.stderr.decode() if e.stderr else str(e)))
    finally:
        for f in (key_file, csr_file):
            if f and os.path.exists(f.name):
                os.unlink(f.name)


def build_fullchain(cert_pem, ca_pem):
    """拼接完整证书链：叶子证书 + 中间证书"""
    chain = cert_pem.strip()
    if ca_pem:
        chain += '\n' + ca_pem.strip()
    return chain + '\n'


def cert_expires_at_rfc3339(pem_text):
    """解析证书过期时间，返回 RFC3339 格式字符串"""
    info = parse_cert_info(pem_text)
    if info and 'not_after' in info:
        return info['not_after'].strftime('%Y-%m-%dT%H:%M:%SZ')
    return ''


def cert_serial_hex(pem_text):
    """返回证书序列号的十六进制格式"""
    info = parse_cert_info(pem_text)
    if info and info.get('serial'):
        return info['serial']
    return ''

"""SSRF 防护：阻止访问内网 IP（spec 10.1）"""

import socket
import ipaddress
from urllib.parse import urlparse

_BLOCKED_NETWORKS = [
    ipaddress.ip_network('10.0.0.0/8'),
    ipaddress.ip_network('172.16.0.0/12'),
    ipaddress.ip_network('192.168.0.0/16'),
    ipaddress.ip_network('127.0.0.0/8'),
    ipaddress.ip_network('169.254.169.254/32'),
    ipaddress.ip_network('0.0.0.0/32'),
    ipaddress.ip_network('::1/128'),
    ipaddress.ip_network('::/128'),
    ipaddress.ip_network('::ffff:127.0.0.0/104'),
    ipaddress.ip_network('fc00::/7'),
    ipaddress.ip_network('fe80::/10'),
]


def verify_ip(ip_str):
    """验证单个 IP 是否安全（非内网）。安全返回 None，不安全返回原因字符串。

    loopback 地址放行。
    """
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return None
    if addr.is_loopback:
        return None
    for net in _BLOCKED_NETWORKS:
        if addr in net:
            return '禁止访问内网地址: %s' % ip_str
    return None


def check_ssrf(url):
    """检查 URL 是否指向内网 IP。安全返回 None，不安全返回原因字符串。

    loopback 地址放行（spec: 仅 localhost/127.0.0.1 允许 HTTP）。
    """
    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        return 'URL 缺少主机名'

    try:
        addr_infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror:
        return None

    for _, _, _, _, sockaddr in addr_infos:
        ip_str = sockaddr[0]
        reason = verify_ip(ip_str)
        if reason:
            return '%s (%s)' % (reason, hostname)

    return None

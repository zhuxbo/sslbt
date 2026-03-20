"""续签引擎。对标 sslctl pkg/certops/renew.go 状态机"""

import os
from datetime import datetime, timezone, timedelta

from . import cert_utils
from .api_client import APIError

# 常量，对标 sslctl
PULL_RENEW_DEFAULT_DAY = 13
LOCAL_RENEW_DEFAULT_DAY = 15
SERVER_AUTO_RENEW_DAYS = 14
MAX_ISSUE_RETRY_COUNT = 10
CSR_PENDING_TIMEOUT_HOURS = 24
RETRY_RESET_DAYS = 7


def needs_renewal(cert_entry, renew_before_days, renew_mode):
    """判断证书是否需要续签"""
    expires_at = cert_entry.get('metadata', {}).get('cert_expires_at', '')
    if not expires_at:
        return False

    try:
        exp_dt = datetime.fromisoformat(expires_at.replace('Z', '+00:00'))
    except (ValueError, AttributeError):
        return False

    now = datetime.now(timezone.utc)
    days_remaining = (exp_dt - now).days

    if renew_mode == 'local':
        last_state = cert_entry.get('metadata', {}).get('last_issue_state', '')
        if last_state == 'processing':
            return days_remaining <= renew_before_days
        return SERVER_AUTO_RENEW_DAYS < days_remaining <= renew_before_days
    else:
        return days_remaining <= renew_before_days


class RenewEngine:
    """续签引擎"""

    def __init__(self, config_manager, api_factory, deployer, logger=None):
        self._config = config_manager
        self._api_factory = api_factory
        self._deployer = deployer
        self._logger = logger
        self._data_dir = config_manager._data_dir

    def check_and_renew_all(self):
        """检查并续签所有证书"""
        certs = self._config.get_certs()
        results = []

        for cert in certs:
            if not cert.get('enabled', True):
                continue
            order_id = cert.get('order_id')
            if not order_id:
                continue

            renew_mode = self._config.get_renew_mode(cert)
            renew_days = self._config.get_renew_before_days(cert)

            if not needs_renewal(cert, renew_days, renew_mode):
                continue

            api = self._api_factory(cert)
            if not api:
                if self._logger:
                    self._logger.warn("证书 order_id=%s 缺少 API 配置，跳过续签", order_id)
                continue

            if self._logger:
                self._logger.info("证书需要续签: order_id=%s, mode=%s", order_id, renew_mode)

            try:
                if renew_mode == 'local':
                    result = self._renew_local(cert, api)
                else:
                    result = self._renew_pull(cert, api)
                results.append({
                    'order_id': order_id,
                    'status': 'success' if result else 'pending',
                    'message': '续签完成' if result else '等待签发',
                })
            except Exception as e:
                if self._logger:
                    self._logger.error("续签失败: order_id=%s, error=%s", order_id, str(e))
                results.append({
                    'order_id': order_id,
                    'status': 'failure',
                    'message': str(e),
                })

        return results

    def _check_deploy_results(self, results, order_id):
        """检查 deploy_multi 结果：全部失败视为部署失败，部分失败记录警告但仍视为成功"""
        if not results:
            raise RuntimeError("部署结果为空: order_id=%s" % order_id)
        success_count = sum(1 for r in results if r.get('status'))
        fail_count = len(results) - success_count
        if success_count == 0:
            failed_msgs = '; '.join(r.get('message', '') for r in results if not r.get('status'))
            raise RuntimeError("所有站点部署失败: %s" % failed_msgs)
        if fail_count > 0 and self._logger:
            failed = [r['site_name'] for r in results if not r.get('status')]
            self._logger.warn(
                "部分站点部署失败: order_id=%s, failed_sites=%s",
                order_id, ','.join(failed),
            )
        return True

    def _renew_pull(self, cert_entry, api):
        """Pull 模式续签：查询订单 → 证书就绪则部署"""
        order_id = cert_entry['order_id']
        if self._logger:
            self._logger.info("Pull 模式续签: order_id=%s", order_id)

        cert_data = api.query_order(order_id)

        status = cert_data.get('status', '')
        certificate = cert_data.get('certificate', '')
        ca_certificate = cert_data.get('ca_certificate', '')
        private_key = cert_data.get('private_key', '')

        if status != 'active' or not certificate:
            if self._logger:
                self._logger.info("证书未就绪: status=%s", status)
            return False

        if not ca_certificate:
            if self._logger:
                self._logger.warn("缺少中间证书，等待下次检查")
            return False

        # 构建完整证书链并部署
        fullchain = cert_utils.build_fullchain(certificate, ca_certificate)
        site_names = cert_entry.get('site_name', [])
        if isinstance(site_names, str):
            site_names = [site_names] if site_names else []
        domains = cert_entry.get('domains', [])

        if not site_names:
            if self._logger:
                self._logger.warn("未绑定站点，跳过部署")
            return False

        results = self._deployer.deploy_multi(
            site_names=site_names,
            fullchain_pem=fullchain,
            key_pem=private_key,
            order_id=order_id,
            domains=domains,
            api_client=api,
        )
        return self._check_deploy_results(results, order_id)

    def _renew_local(self, cert_entry, api):
        """Local 模式续签：生成 CSR → 提交 → 等待签发 → 部署"""
        order_id = cert_entry['order_id']
        meta = cert_entry.get('metadata', {})

        if self._logger:
            self._logger.info("Local 模式续签: order_id=%s", order_id)

        # 检查重试计数是否需要重置
        self._check_retry_reset(order_id, meta)

        # 检查重试次数
        retry_count = meta.get('issue_retry_count', 0)
        if retry_count >= MAX_ISSUE_RETRY_COUNT:
            raise RuntimeError("CSR 提交重试次数已达上限 (%d)" % MAX_ISSUE_RETRY_COUNT)

        last_state = meta.get('last_issue_state', '')

        if last_state == 'processing':
            return self._handle_processing(cert_entry, api)
        else:
            return self._submit_new_csr(cert_entry, api)

    def _check_retry_reset(self, order_id, meta):
        """检查是否需要重置重试计数（提交超过 7 天）"""
        submitted_at = meta.get('csr_submitted_at', '')
        if not submitted_at:
            return
        try:
            sub_dt = datetime.fromisoformat(submitted_at.replace('Z', '+00:00'))
            if datetime.now(timezone.utc) - sub_dt > timedelta(days=RETRY_RESET_DAYS):
                if self._logger:
                    self._logger.info("CSR 提交超过 %d 天，重置重试计数", RETRY_RESET_DAYS)
                self._config.update_metadata(order_id, {
                    'issue_retry_count': 0,
                    'last_issue_state': '',
                    'csr_submitted_at': '',
                })
        except (ValueError, AttributeError):
            pass

    def _handle_processing(self, cert_entry, api):
        """处理已提交 CSR 的 processing 状态"""
        order_id = cert_entry['order_id']
        meta = cert_entry.get('metadata', {})

        # 检查 CSR pending 超时
        submitted_at = meta.get('csr_submitted_at', '')
        if submitted_at:
            try:
                sub_dt = datetime.fromisoformat(submitted_at.replace('Z', '+00:00'))
                if datetime.now(timezone.utc) - sub_dt > timedelta(hours=CSR_PENDING_TIMEOUT_HOURS):
                    if self._logger:
                        self._logger.info("CSR pending 超时，清除状态")
                    self._config.update_metadata(order_id, {
                        'last_issue_state': '',
                        'csr_submitted_at': '',
                    })
                    return False
            except (ValueError, AttributeError):
                pass

        # 查询订单状态
        cert_data = api.query_order(order_id)
        status = cert_data.get('status', '')

        if status == 'processing':
            if self._logger:
                self._logger.info("证书仍在处理中，继续等待")
            return False

        if status != 'active':
            if self._logger:
                self._logger.info("证书状态异常: %s，清除状态", status)
            self._config.update_metadata(order_id, {
                'last_issue_state': '',
                'csr_submitted_at': '',
            })
            return False

        # 证书已签发，读取 pending key 并部署
        certificate = cert_data.get('certificate', '')
        ca_certificate = cert_data.get('ca_certificate', '')

        if not certificate or not ca_certificate:
            if self._logger:
                self._logger.warn("证书内容不完整")
            return False

        pending_key = self._read_pending_key(cert_entry)
        if not pending_key:
            if self._logger:
                self._logger.error("pending key 不存在")
            self._config.update_metadata(order_id, {
                'last_issue_state': '',
                'csr_submitted_at': '',
            })
            return False

        fullchain = cert_utils.build_fullchain(certificate, ca_certificate)
        site_names = cert_entry.get('site_name', [])
        if isinstance(site_names, str):
            site_names = [site_names] if site_names else []
        domains = cert_entry.get('domains', [])

        if not site_names:
            if self._logger:
                self._logger.warn("未绑定站点，跳过部署")
            return False

        results = self._deployer.deploy_multi(
            site_names=site_names,
            fullchain_pem=fullchain,
            key_pem=pending_key,
            order_id=order_id,
            domains=domains,
            api_client=api,
        )

        # 清理 pending key
        self._cleanup_pending_key(cert_entry)
        return self._check_deploy_results(results, order_id)

    def _submit_new_csr(self, cert_entry, api):
        """生成并提交新的 CSR"""
        order_id = cert_entry['order_id']
        domains = cert_entry.get('domains', [])

        if not domains:
            raise RuntimeError("未配置域名")

        # 清理可能残留的 pending key（崩溃恢复场景）
        self._cleanup_pending_key(cert_entry)

        # 生成 CSR 和私钥
        csr_pem, key_pem, csr_hash = cert_utils.generate_csr(domains)

        # 保存 pending key
        self._save_pending_key(cert_entry, key_pem)

        # 递增重试计数（先持久化）
        meta = cert_entry.get('metadata', {})
        retry_count = meta.get('issue_retry_count', 0) + 1
        self._config.update_metadata(order_id, {
            'issue_retry_count': retry_count,
        })

        # 提交 CSR
        try:
            cert_data = api.submit_csr(order_id, csr_pem, domains)
        except APIError:
            self._cleanup_pending_key(cert_entry)
            raise

        status = cert_data.get('status', 'processing')
        now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

        self._config.update_metadata(order_id, {
            'csr_submitted_at': now,
            'last_csr_hash': csr_hash,
            'last_issue_state': status,
        })

        if status == 'active':
            # 立即签发，部署
            certificate = cert_data.get('certificate', '')
            ca_certificate = cert_data.get('ca_certificate', '')
            if certificate and ca_certificate:
                fullchain = cert_utils.build_fullchain(certificate, ca_certificate)
                site_names = cert_entry.get('site_name', [])
                if isinstance(site_names, str):
                    site_names = [site_names] if site_names else []
                if site_names:
                    results = self._deployer.deploy_multi(
                        site_names=site_names,
                        fullchain_pem=fullchain,
                        key_pem=key_pem,
                        order_id=order_id,
                        domains=domains,
                        api_client=api,
                    )
                    self._cleanup_pending_key(cert_entry)
                    return self._check_deploy_results(results, order_id)

        if self._logger:
            self._logger.info("CSR 已提交，等待签发: status=%s", status)
        return False

    def _pending_key_path(self, cert_entry):
        cert_name = cert_entry.get('cert_name', 'order-%s' % cert_entry.get('order_id'))
        return os.path.join(self._data_dir, 'pending-keys', cert_name, 'pending-key.pem')

    def _save_pending_key(self, cert_entry, key_pem):
        path = self._pending_key_path(cert_entry)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, key_pem.encode('utf-8'))
        finally:
            os.close(fd)

    def _read_pending_key(self, cert_entry):
        path = self._pending_key_path(cert_entry)
        if not os.path.isfile(path):
            return None
        if os.path.islink(path):
            return None  # 拒绝符号链接
        with open(path, 'r') as f:
            return f.read()

    def _cleanup_pending_key(self, cert_entry):
        path = self._pending_key_path(cert_entry)
        try:
            if os.path.isfile(path):
                os.remove(path)
            # 清理空目录
            dir_path = os.path.dirname(path)
            if os.path.isdir(dir_path) and not os.listdir(dir_path):
                os.rmdir(dir_path)
        except OSError:
            pass

"""续签引擎。对标 sslctl pkg/certops/renew.go 状态机"""

import os
import json
import time
import random
import fcntl
import tempfile
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from . import cert_utils
from .api_client import APIError, ERR_ORDER_IN_PROGRESS
from .deployer import DeployError, check_web_config, probe_panel_runtime
from .config import (
    MAX_ISSUE_RETRY_COUNT, MAX_DEPLOY_ATTEMPT_COUNT, MAX_FAILED_SITE_RETRY_COUNT,
    MAX_NO_PROGRESS_DAYS, MAX_BLOCK_REPORT_COUNT,
    ISSUE_STATE_PROCESSING, ISSUE_STATE_ACTIVE, ISSUE_STATE_CAPPED, ISSUE_STATE_EXPIRED,
    TERMINAL_ISSUE_STATES, IN_FLIGHT_ISSUE_STATES,
    CAPPED_PHASE_ISSUE, CAPPED_PHASE_DEPLOY, CAPPED_PHASE_STALLED,
    RENEW_MODE_LOCAL, derive_or_validate_renew_policy,
    classify_order_status, ORDER_CLASS_WAITING,
    ORDER_CLASS_TERMINAL, ORDER_CLASS_CHAIN, ORDER_CLASS_UNKNOWN,
)

# 常量，对标 sslctl
RENEW_DEFAULT_DAYS = 14
MAX_RENEW_BEFORE_DAYS = 30  # 续签应在到期前 30 天内，超限视为服务端异常值（spec 2.9）
# MAX_ISSUE_RETRY_COUNT / MAX_DEPLOY_ATTEMPT_COUNT 由 config 统一定义（各自 >= 10 触顶）
SAFETY_MARGIN_HOURS = 24    # 自动动作安全余量：剩余有效期 < 此值不启动新动作（spec §3.2/§11）
RENEW_SLEEP_MIN = 5
RENEW_SLEEP_MAX = 120
SPREAD_TOTAL_MAX = 600  # 分散延迟总量上限（秒）
MAX_RENEW_BATCH = 100   # 单次续签证书数量上限

# 抢锁重试间隔（秒）。总等待窗口由调用方给出：cron 可以等，面板同步请求不能
LOCK_RETRY_INTERVAL = 5
CRON_LOCK_WAIT = 120    # cron 续签：覆盖典型交互式部署（几秒到几十秒）
PANEL_LOCK_WAIT = 6     # 面板「续签」按钮：同步 HTTP，超过几秒就是页面卡死

# 连续多少轮拿到同一张证书才判定「服务端未更替」。取 2 而非 1：
# F8 之后部分失败的证书会次日重试，那次补部署必然是同一张证书，属正常重试
CERT_UNCHANGED_ROUNDS = 2

# renew_status.json 里跳过明细的条数上限：该文件每次打开设置页都被整份读出发给浏览器
MAX_SKIPPED_DETAIL = 50

# 非关键上报（部署结果回调、阻断上报）的进程内熔断阈值（spec §11）。
# 单次回调最坏耗时 = MAX_RETRIES(3) × TIMEOUT_POST(60s) + 退避 3s ≈ 183 秒，而 cron
# 批量上限 100 张证书，逐张各等一份完整超时预算时最坏耗时随证书数线性放大到数小时。
# 连续失败说明上报通道整体不可用（服务端宕机/网络不通），后续每一次都是同样的干等：
# 熔断后收敛到三次失败的代价。部署结果判定与本地落盘完全不受影响——熔断只砍网络等待。
CALLBACK_BREAKER_THRESHOLD = 3

# 两次无进展观测的合理间隔上限。超过即视为系统时间跳变而非真实经过的时间——
# 与 deployer 的站点缺失确认同一护栏：无进展时限依赖时钟单调，一次前跳就能
# 让 14 天立即成立，把还在正常等待签发的证书直接打成停更
_CLOCK_SANITY_MAX_DAYS = 60


def _normalize_issue_status(status):
    """按 spec §2.4 分类归一服务端状态，供「有无在途订单」判定使用。

    在途等待（pending / processing / approving / unpaid / cancelling）与**未知新增状态**
    一律归一为 processing：只查询、不重复提交、不增计数、不重生 CSR。未知归等待是刻意的
    保守方向——反向（当终态）会让服务端新增的一个中间态把全量证书打进停机。
    active / 真终态 / 链式状态原样返回，由调用方按类别分流。
    """
    cls = classify_order_status(status)
    if cls in (ORDER_CLASS_WAITING, ORDER_CLASS_UNKNOWN):
        return ISSUE_STATE_PROCESSING
    return status


def _expiry_unknown(meta):
    """判断 metadata 中的到期时间是否未知（为空或不可解析）"""
    expires_at = meta.get('cert_expires_at', '')
    if not expires_at:
        return True
    try:
        datetime.fromisoformat(expires_at.replace('Z', '+00:00'))
        return False
    except (ValueError, AttributeError):
        return True


def _remaining_hours(meta):
    """返回证书剩余有效期（小时）；到期时间未知（空/不可解析）返回 None"""
    expires_at = meta.get('cert_expires_at', '')
    if not expires_at:
        return None
    try:
        exp_dt = datetime.fromisoformat(expires_at.replace('Z', '+00:00'))
    except (ValueError, AttributeError):
        return None
    return (exp_dt - datetime.now(timezone.utc)).total_seconds() / 3600.0


def _compact_reason(text, limit=300):
    """压平多行诊断为单行并限长：面板与回调都直接展示，原样带换行会撑爆布局"""
    compact = ' '.join(str(text).split())
    return compact[:limit] + '...' if len(compact) > limit else compact


def _token_key(cert_entry):
    """轮内 token 黑名单的键：同一 (url, token) 的调用共享服务端的认证与限流判定。

    仅用于进程内 set/dict，不写盘、不进日志、不下发前端——token 原文只在内存里当键用。
    """
    api_cfg = cert_entry.get('api') or {}
    return (api_cfg.get('url', ''), api_cfg.get('token', ''))


def _token_label(cert_entry):
    """面板展示用的 token 标识：只保留 API 主机名，绝不含 token 原文"""
    api_cfg = cert_entry.get('api') or {}
    try:
        host = urlparse(api_cfg.get('url', '')).hostname or ''
    except ValueError:
        host = ''
    return host


def _deploy_callback_decision(results):
    """从底层部署结果推导编排层回调的 (status, message)。

    全部站点成功 → ('success', '')；任一明确失败 → ('failure', 各失败站点原因摘要)。
    与 deploy_multi 内部回调判定同构（metadata 落盘失败经 DeployError 单独路径处理）。
    """
    if all(r.get('status') for r in results):
        return 'success', ''
    fail_parts = ['%s: %s' % (r.get('site_name', ''), r.get('message', ''))
                  for r in results if not r.get('status')]
    return 'failure', '; '.join(fail_parts)


def needs_renewal(cert_entry, renew_before_days):
    """判断证书是否需要进入续签/查询流程（续签决策以服务端为主，spec §3.4）。

    本函数仅做"是否放行进入续签流程"的前置判定，不发起任何网络请求；到期时间
    未知时的实际 API 查询回填由各模式处理器完成（local 见 RenewEngine._renew_local
    的回填分支，pull 见 RenewEngine._renew_pull 的查询-部署）。

    - 已明确过期（剩余天数 < 0）：停止，等待人工处理（spec §3.2）
    - local 模式已提交 CSR（last_issue_state == 'processing'）：放行进入查询流程跟进签发
    - cert_expires_at 为空/不可解析：到期时间未知，放行交由处理器查询回填后再判定
      （覆盖首次部署遇 processing、metadata 写失败、带外换证等中间态，避免 cron 永不接手）
    - 到期时间已知且未到期：仅当剩余天数 ≤ renew_before_days 才续签
    """
    meta = cert_entry.get('metadata', {})
    if meta.get('failed_site_names'):
        return True
    expires_at = meta.get('cert_expires_at', '')

    days_remaining = None
    if expires_at:
        try:
            exp_dt = datetime.fromisoformat(expires_at.replace('Z', '+00:00'))
            days_remaining = (exp_dt - datetime.now(timezone.utc)).days
        except (ValueError, AttributeError):
            days_remaining = None

    # 已明确过期，停止续签，等待人工处理
    if days_remaining is not None and days_remaining < 0:
        return False

    # 已提交 CSR 待签发：无论到期时间是否已知都需进入查询流程
    if meta.get('last_issue_state', '') == 'processing':
        return True

    # 到期时间未知（空/不可解析）：视为需处理，进入 API 查询回填 metadata
    if days_remaining is None:
        return True

    return days_remaining <= renew_before_days


class RenewEngine:
    """续签引擎"""

    def __init__(self, config_manager, api_factory, deployer, logger=None, file_verifier=None):
        self._config = config_manager
        self._api_factory = api_factory
        self._deployer = deployer
        self._logger = logger
        self._file_verifier = file_verifier
        self._data_dir = config_manager._data_dir
        # 本轮整体中止原因（运行环境不可用等）。调用方据此区分"中止"与"跑完但无需续签"，
        # 二者都返回空列表，但对用户的含义相反
        self.last_abort_reason = ''
        # 非关键上报的连续失败计数（熔断用）。实例级：宝塔每次请求新建插件实例，
        # 手动路径天然各自独立，cron 一轮一个实例
        self._callback_fail_streak = 0

    def check_and_renew_all(self, spread=False, lock_wait=0):
        """检查并续签所有证书

        Args:
            spread: 是否在续签间加随机延迟，避免集中请求 API（cron 调用时为 True）
            lock_wait: 抢锁最长等待秒数。cron 不赶时间，可以等；面板按钮是同步 HTTP
                请求，必须短等——窗口若硬编码在这里，点一次按钮会让页面挂住两分钟。
                手动部署与 cron 共用同一把 data/renew.lock，撞车是真实路径，
                零重试会让 cron 白白丢掉当天唯一的续签窗口。
        """
        self.last_abort_reason = ''
        lock_path = os.path.join(self._data_dir, 'renew.lock')
        try:
            # O_NOFOLLOW 与 _write_renew_status 的 mkstemp 硬化标准对齐；
            # 打不开时（data/ 只读、inode 耗尽、路径被换成符号链接）不能让整轮静默消失
            lock_fd = os.fdopen(
                os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600), 'w')
        except OSError as e:
            self.last_abort_reason = '无法打开续签锁文件: %s' % e
            if self._logger:
                self._logger.error("无法打开续签锁文件，本轮中止: %s", str(e))
            self._write_renew_status([], aborted_reason=self.last_abort_reason)
            return []

        if not self._acquire_lock(lock_fd, lock_wait):
            lock_fd.close()
            self.last_abort_reason = '另一个部署或续签任务正在执行，本轮已跳过'
            if self._logger:
                self._logger.info("另一个续签进程正在运行，跳过本次检查")
            self._write_lock_skip()
            return []

        try:
            return self._do_renew_all(spread)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()

    @staticmethod
    def _acquire_lock(lock_fd, lock_wait):
        """非阻塞抢锁，最多重试到 lock_wait 秒；拿到返回 True"""
        deadline = time.time() + max(0, lock_wait)
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError:
                if time.time() >= deadline:
                    return False
                time.sleep(min(LOCK_RETRY_INTERVAL, max(0.1, deadline - time.time())))

    def _write_lock_skip(self):
        """抢锁失败写独立小文件，绝不去读改写 renew_status.json

        那条路径按定义就是没拿到锁的进程，与正在收尾的持锁进程并发读改写会用陈旧快照
        覆盖对方刚写的真实计数——修一个竞态时不该新造一个。
        """
        path = os.path.join(self._data_dir, 'renew_lock_skip.json')
        data = json.dumps({
            'at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        }, ensure_ascii=False).encode('utf-8')
        try:
            fd, tmp = tempfile.mkstemp(dir=self._data_dir, prefix='.lock_skip-', suffix='.tmp')
        except OSError:
            return
        try:
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def _do_renew_all(self, spread=False):
        """实际续签逻辑（已持有进程锁）"""
        # 运行环境闸门：宝塔运行时不可用时整轮中止。这是进程级、与证书无关的故障，
        # 逐证书发 failure 回调既定位不到根因，也不属于 spec §2.8 的"部署结果"上报范围，
        # 故一个回调都不发，改由 renew_status 的 aborted_reason + 面板告警通知用户
        runtime_err = probe_panel_runtime()
        if runtime_err:
            self.last_abort_reason = _compact_reason(runtime_err)
            if self._logger:
                self._logger.error("宝塔运行时不可用，本轮续签整体中止: %s", runtime_err)
            self._write_renew_status([], aborted_reason=self.last_abort_reason)
            return []

        # 配置降级（主配置损坏）：此时 get_certs 恒为空，照常跑完会写出全 0 的新鲜状态，
        # 与"确实无需续签"在面板上不可区分——正是要消除的那种健康假象
        if getattr(self._config, 'is_degraded', lambda: False)():
            self.last_abort_reason = '配置文件损坏，已停止自动续签以防覆盖，请人工修复'
            if self._logger:
                self._logger.error("配置降级态，本轮续签整体中止")
            self._write_renew_status([], aborted_reason=self.last_abort_reason)
            return []

        certs = self._config.get_certs()

        # 阶段 1: 收集需续签的证书和对应 API 客户端
        pending_list = []
        collect_failures = []
        skipped = []
        for cert in certs:
            try:
                self._collect_one(cert, pending_list, skipped)
            except Exception as e:
                # 逐证书隔离：此前阶段 1 一行 try 都没有，而 APIClient.__init__ 会对
                # SSRF 命中 / token 非法 / 协议非法主动 raise ValueError——一张证书的
                # 构造失败会让整批后续证书连一次 API 调用都没有，且 renew_status 保留
                # 上一轮的成功记录，面板看起来完全健康
                oid = cert.get('order_id', '?')
                if self._logger:
                    self._logger.error("证书预处理失败，跳过该证书: order_id=%s, error=%s", oid, str(e))
                collect_failures.append({
                    'order_id': oid, 'status': 'failure', 'message': '预处理失败: %s' % e})

        return self._process_pending(pending_list, spread, collect_failures, skipped)

    def _collect_one(self, cert, pending_list, skipped=None):
        """阶段 1 的单证书前置过滤：命中任一跳过条件即返回，否则追加到待处理列表

        skipped 收集「需要向用户解释」的跳过原因。not_due 不记——证书列表本来就逐张
        显示状态，把几百张不到期的证书写进状态文件只是噪音，而该文件每次打开设置页
        都会被整份读出发给浏览器。
        """
        def _skip(reason):
            if skipped is not None:
                skipped.append({'order_id': cert.get('order_id', 0), 'reason': reason})
        if not cert.get('enabled', True):
            return
        order_id = cert.get('order_id')
        if not order_id:
            return
        meta = cert.get('metadata', {})
        state = meta.get('last_issue_state', '')

        # EXPIRED 自愈：必须在终态 continue 之前判定，否则永远执行不到。
        # 一次时钟前跳（NTP 抽风、快照恢复、容器漂移）就会把还有几十天有效期的证书
        # 永久打成「已过期停更」，且标签本身是错的。
        # 守卫：仅当剩余有效期能被解析出来且高于安全余量才解除——元数据损坏时保持终态，
        # 否则真过期的证书会反复启动新动作，违反 spec §3.2
        if state == ISSUE_STATE_EXPIRED:
            hours_now = _remaining_hours(meta)
            if hours_now is not None and hours_now > SAFETY_MARGIN_HOURS:
                if self._logger:
                    self._logger.warning(
                        "证书实际未过期（剩余 %.1fh），解除过期终态: order_id=%s",
                        hours_now, cert.get('order_id'))
                # 只清状态，不动两个计数：那是另一回事，清了会绕过触顶保护
                self._persist_meta(cert['order_id'], {'last_issue_state': ''})
                meta['last_issue_state'] = ''
                state = ''

        # 触顶 / 过期 / policy 阻断为终态：静默跳过，不启动新动作、不发回调、不计数
        if state in TERMINAL_ISSUE_STATES:
            _skip('terminal:%s' % state)
            return

        renew_mode = self._config.get_renew_mode(cert)

        # 触顶检查（计数分离）：签发（仅 local）与部署各自 >= 10 → 进入 CAPPED，不发回调
        # 签发触顶只拦截"新的 CSR 提交"；已进入 processing（CSR 已被接受）的证书继续轮询签发，
        # 不因签发计数被误判触顶而停止部署
        has_in_flight = state in IN_FLIGHT_ISSUE_STATES or self._has_pending_csr(cert)
        has_failed_sites = bool(meta.get('failed_site_names'))
        if has_failed_sites and meta.get('failed_site_retry_count', 0) >= MAX_FAILED_SITE_RETRY_COUNT:
            if self._logger:
                self._logger.error(
                    "失败绑定重试已达上限，等待人工处理: order_id=%s, sites=%s",
                    order_id, ','.join(meta.get('failed_site_names', [])))
            _skip('failed_sites_capped')
            return
        if renew_mode == RENEW_MODE_LOCAL and not has_in_flight and not has_failed_sites \
                and meta.get('issue_retry_count', 0) >= MAX_ISSUE_RETRY_COUNT:
            self._enter_capped(cert, CAPPED_PHASE_ISSUE)
            _skip('capped:%s' % CAPPED_PHASE_ISSUE)
            return
        if not has_failed_sites and not has_in_flight \
                and meta.get('deploy_attempt_count', 0) >= MAX_DEPLOY_ATTEMPT_COUNT:
            self._enter_capped(cert, CAPPED_PHASE_DEPLOY)
            _skip('capped:%s' % CAPPED_PHASE_DEPLOY)
            return

        # 到期准入：已过期 → 转 EXPIRED 静默；剩余 < 安全余量 → 本轮不启动新动作
        hours = _remaining_hours(meta)
        if hours is not None:
            if hours <= 0:
                self._enter_expired(cert)
                _skip('expired')
                return
            if hours < SAFETY_MARGIN_HOURS and not has_in_flight and not has_failed_sites:
                if self._logger:
                    self._logger.info(
                        "剩余有效期不足安全余量（%.1fh < %dh），本轮不启动新动作: order_id=%s",
                        hours, SAFETY_MARGIN_HOURS, order_id)
                _skip('safety_margin')
                return

        # 无进展停更：纯 GET 轮询不计入任何尝试计数（spec §3.2），到期闸门是它唯一的
        # 边界，而上面那段在 cert_expires_at 为空时（hours is None）整段跳过。放在到期
        # 判定之后：两者同时成立时 EXPIRED 更准确，停更时限只兜住到期时间未知的缺口
        if self._stalled_too_long(cert):
            self._enter_capped(cert, CAPPED_PHASE_STALLED)
            self._cleanup_stalled_artifacts(cert)
            _skip('capped:%s' % CAPPED_PHASE_STALLED)
            return

        renew_days = self._config.get_renew_before_days(cert)
        if not needs_renewal(cert, renew_days):
            return
        api = self._api_factory(cert)
        if not api:
            if self._logger:
                self._logger.warning("证书 order_id=%s 缺少 API 配置，跳过续签", order_id)
            _skip('no_api_config')
            return
        pending_list.append((cert, api, renew_mode))

    def _select_batch(self, pending_list):
        """从待处理列表中选出本轮批次（上限 MAX_RENEW_BATCH）

        此前按配置文件顺序直接截断，尾部证书会确定性饿死：卡在 processing 的证书
        每轮都进列表且不会自行退出，100 张卡住就永远轮不到第 101 张。

        单纯按紧急度排序解决不了——processing 证书正是因为临期才提交的 CSR，
        剩余有效期必然更短，排序只会让它们更稳地占住配额。所以：
        - processing 组限额一半，剩余名额留给会发起新动作的证书
        - 组内按剩余有效期升序（紧急优先）
        - 仍超额时按 last_attempt_at 升序轮转，保证 ceil(N/100) 轮内全部触达
        """
        if len(pending_list) <= MAX_RENEW_BATCH:
            return pending_list

        def remaining(item):
            hours = _remaining_hours(item[0].get('metadata', {}))
            return hours if hours is not None else float('inf')

        def last_attempt(item):
            return item[0].get('metadata', {}).get('last_attempt_at', '')

        processing, fresh = [], []
        for item in pending_list:
            state = item[0].get('metadata', {}).get('last_issue_state', '')
            (processing if state == ISSUE_STATE_PROCESSING else fresh).append(item)

        # 轮转优先于紧急度：先按上次尝试时间排，同组内再按紧急度
        for group in (processing, fresh):
            group.sort(key=lambda i: (last_attempt(i), remaining(i)))

        quota = MAX_RENEW_BATCH // 2
        picked = processing[:quota]
        picked += fresh[:MAX_RENEW_BATCH - len(picked)]
        if len(picked) < MAX_RENEW_BATCH:
            rest = [i for i in processing[quota:] if i not in picked]
            picked += rest[:MAX_RENEW_BATCH - len(picked)]

        if self._logger:
            self._logger.warning(
                "需续签证书 %d 个，超过单次上限 %d，本轮处理 %d 个（processing %d），"
                "其余下轮按上次尝试时间轮转",
                len(pending_list), MAX_RENEW_BATCH, len(picked),
                sum(1 for i in picked if i in processing))
        return picked

    def _process_pending(self, pending_list, spread, collect_failures, skipped=None):
        """阶段 2/3：选批、计算延迟、逐个续签并汇总"""
        pending_list = self._select_batch(pending_list)
        sleep_min, sleep_max = self._calc_spread_delay(len(pending_list))
        skipped = [] if skipped is None else skipped

        # 阶段 3: 逐个续签。预处理失败的证书先计入，确保它们出现在汇总与面板里
        results = list(collect_failures)
        # 轮内 token 黑名单（spec §2.2「本轮停止」）。限流与 token/账号/IP 类失败都发生在
        # 服务端中间件、与订单无关，同一 token 的后续证书必然同样失败：逐张重试零收益，
        # 却会在 local 模式下每张烧一次签发额度，并把限流窗口的计数器继续推高。
        # 按 (url, token) 而非全局拉黑——本项目每张证书自带 api 配置，别的 token 应照常跑
        blocked = {}
        # token 级阻断时回调必然同样发不出去（同一个 token），面板是唯一的用户侧提示入口
        auth_blocks = []
        for idx, (cert, api, renew_mode) in enumerate(pending_list):
            order_id = cert['order_id']

            token_key = _token_key(cert)
            if token_key in blocked:
                skipped.append({'order_id': order_id,
                                'reason': 'auth_blocked:%s' % blocked[token_key]})
                # 单条目降到 debug：逐条目各记一条同因告警会淹没日志（spec §2.2），
                # 真正的原因由轮末那条汇总承担
                if self._logger:
                    self._logger.debug("Token 已被拒，跳过该证书（零请求）: order_id=%s, %s",
                                       order_id, blocked[token_key])
                continue

            # 延迟只在证书之间加，第一个不等（idx 而非 results 长度——后者已含预处理失败项）
            if spread and idx:
                delay = random.randint(sleep_min, sleep_max)
                if self._logger:
                    self._logger.info("等待 %d 秒后处理下一个证书", delay)
                time.sleep(delay)

            if self._logger:
                self._logger.info("证书需要续签: order_id=%s, mode=%s", order_id, renew_mode)

            # 记录本轮已触达，供下一轮轮转排序使用（超额时保证 ceil(N/100) 轮内全部处理到）
            self._persist_meta(order_id, {
                'last_attempt_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')})

            try:
                if renew_mode == RENEW_MODE_LOCAL:
                    result = self._renew_local(cert, api)
                else:
                    result = self._renew_pull(cert, api)
                results.append({
                    'order_id': order_id,
                    'status': 'success' if result else 'pending',
                    'message': '续签完成' if result else '等待签发',
                })
            except APIError as e:
                if self._logger:
                    self._logger.error("续签失败: order_id=%s, error=%s", order_id, str(e))
                if e.auth_blocked:
                    blocked[token_key] = e.error_code
                    auth_blocks.append({'error_code': e.error_code,
                                        'api_host': _token_label(cert),
                                        'retry_after': e.retry_after})
                    if self._logger:
                        self._logger.error(
                            "API 拒绝该 Token（%s），本轮跳过其余使用该 Token 的证书: "
                            "order_id=%s, retry_after=%s", e.error_code, order_id,
                            e.retry_after or '-')
                results.append({
                    'order_id': order_id,
                    'status': 'failure',
                    'message': str(e),
                })
            except Exception as e:
                if self._logger:
                    self._logger.error("续签失败: order_id=%s, error=%s", order_id, str(e))
                results.append({
                    'order_id': order_id,
                    'status': 'failure',
                    'message': str(e),
                })

        # 汇总日志
        if self._logger:
            total = len(results)
            if total:
                success = sum(1 for r in results if r['status'] == 'success')
                pending = sum(1 for r in results if r['status'] == 'pending')
                failed = sum(1 for r in results if r['status'] == 'failure')
                self._logger.info("续签检查完成: %d 个证书, 成功=%d, 等待=%d, 失败=%d",
                                  total, success, pending, failed)
            else:
                self._logger.info("续签检查完成: 无需续签")
            # Token 被拒的轮末汇总（spec §2.2）：单条目只记 debug，这里统一给出被拒 token
            # 数、跳过条目数与处置指引。缺了这条，「本轮几乎什么都没做」的真正原因反而
            # 被埋掉——尤其是整批同一个 token 时，上面那行汇总会显示成一个无害的小数字
            if blocked:
                token_skipped = sum(1 for s in skipped
                                    if str(s.get('reason', '')).startswith('auth_blocked:'))
                self._logger.error(
                    "本轮有 %d 个部署 Token 被服务端拒绝（%s），已跳过 %d 张证书且未发起任何"
                    "请求；请到「设置」核对 Token 配置或稍后重试，修好后下轮自动恢复",
                    len(blocked), '、'.join(sorted(set(blocked.values()))), token_skipped)

        self._write_renew_status(results, skipped=skipped, auth_blocks=auth_blocks)
        return results

    def _write_renew_status(self, results, aborted_reason='', skipped=None, auth_blocks=None):
        """写入最近一次续签运行的轻量状态（供面板展示），失败不影响续签

        复用数据目录，原子写 + 0600 权限，与 config/session 落盘约定一致。

        aborted_reason 非空表示本轮整体没跑（运行环境不可用等）：此时 last_run 虽是新鲜的，
        但面板不得据此判定健康——否则一台永久跑不动的机器会同时显示"最近续签正常"和错误告警。

        auth_blocks 是本轮被服务端拒绝的 token 列表（spec §2.2）：这类失败下回调用的是
        同一个坏 token、必然也发不出去，面板是唯一提示入口。只含 error_code 与 API 主机名，
        不含 token 原文。参数均可选，既有直接调用方（含测试）不受影响。
        """
        status = {
            'last_run': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'total': len(results),
            'success': sum(1 for r in results if r.get('status') == 'success'),
            'pending': sum(1 for r in results if r.get('status') == 'pending'),
            'failure': sum(1 for r in results if r.get('status') == 'failure'),
            'aborted_reason': aborted_reason,
        }
        # 跳过明细：只记需要向用户解释的原因（not_due 不入册）。数组硬上限——
        # get_certs 本身没有上限，而本文件每次打开设置页都会被整份读出发给浏览器
        skipped = skipped or []
        status['skipped_count'] = len(skipped)
        status['skipped'] = skipped[:MAX_SKIPPED_DETAIL]
        status['skipped_overflow'] = max(0, len(skipped) - MAX_SKIPPED_DETAIL)
        # 同一 token 只会被拉黑一次，条数天然等于 token 数；仍按上限截断保持文件有界
        status['auth_blocks'] = (auth_blocks or [])[:MAX_SKIPPED_DETAIL]
        path = os.path.join(self._data_dir, 'renew_status.json')
        data = json.dumps(status, ensure_ascii=False).encode('utf-8')
        # 随机名临时文件（mkstemp 以 O_EXCL|O_CREAT 创建，不跟随符号链接）+ 原子替换，
        # 避免固定名 .tmp 被预置为符号链接时经 O_TRUNC 覆盖任意目标文件
        try:
            fd, tmp = tempfile.mkstemp(dir=self._data_dir, prefix='.renew_status-', suffix='.tmp')
        except OSError as e:
            if self._logger:
                self._logger.warning("写入续签状态文件失败: %s", str(e))
            return
        try:
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except OSError as e:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            if self._logger:
                self._logger.warning("写入续签状态文件失败: %s", str(e))

    @staticmethod
    def _calc_spread_delay(count):
        """根据需续签证书数量动态计算延迟区间

        保证总延迟不超过 SPREAD_TOTAL_MAX（默认 600 秒），
        证书少时用较大间隔（30-120s），证书多时自动缩短。
        """
        if count <= 1:
            return RENEW_SLEEP_MIN, RENEW_SLEEP_MAX
        gaps = count - 1
        avg_max = SPREAD_TOTAL_MAX // gaps
        sleep_max = max(RENEW_SLEEP_MIN, min(avg_max, RENEW_SLEEP_MAX))
        sleep_min = max(RENEW_SLEEP_MIN, sleep_max // 3)
        return sleep_min, sleep_max

    def _check_deploy_results(self, results, order_id):
        """检查 deploy_multi 结果：全部失败视为部署失败，部分失败记录警告但仍视为成功

        站点缺失（site_removed 已确认删除并解绑 / site_missing 首轮疑似删除）时按
        失败上报，与部署回调的 failure 语义一致；缺失站点恢复或解绑后的后续轮次
        不再出现该站点，恢复 success。
        """
        if not results:
            raise RuntimeError("部署结果为空: order_id=%s" % order_id)
        success_count = sum(1 for r in results if r.get('status'))
        fail_count = len(results) - success_count
        if success_count == 0:
            failed_msgs = '; '.join(r.get('message', '') for r in results if not r.get('status'))
            raise RuntimeError("所有站点部署失败: %s" % failed_msgs)
        removed = [r['site_name'] for r in results if r.get('site_removed')]
        if removed:
            raise RuntimeError("站点已删除，已解除绑定: %s" % ','.join(removed))
        remove_failed = [r['site_name'] for r in results if r.get('site_remove_failed')]
        if remove_failed:
            raise RuntimeError("站点已删除，但解除绑定持久化失败: %s" % ','.join(remove_failed))
        # 疑似删除（首轮缺失，尚未解绑）：与回调 failure 一致按失败上报，等待二次确认
        suspected = [r['site_name'] for r in results if r.get('site_missing')]
        if suspected:
            raise RuntimeError("站点疑似已删除，待二次确认: %s" % ','.join(suspected))
        if fail_count > 0:
            # 部分失败改判为失败，恢复内部一致性：_deploy_callback_decision 在任一站点失败时
            # 就已返回 failure 并上报服务端，而此处曾返回 True 让本地汇总与面板显示成功——
            # 正是 _check_deploy_environment 注释所反对的双口径
            failed = [r['site_name'] for r in results if not r.get('status')]
            if self._logger:
                self._logger.warning(
                    "部分站点部署失败: order_id=%s, failed_sites=%s",
                    order_id, ','.join(failed))
            raise RuntimeError("部分站点部署失败: %s" % ','.join(failed))
        return True

    def _update_renew_before_days(self, api):
        """从 api.last_renew_before_days 更新全局配置"""
        try:
            days = int(getattr(api, 'last_renew_before_days', 0) or 0)
        except (TypeError, ValueError):
            return
        if days > MAX_RENEW_BEFORE_DAYS:
            if self._logger:
                self._logger.warning(
                    "服务端返回的 renew_before_days=%s 超过上限 %s（续签应在到期前 30 天内），保留本地配置",
                    days, MAX_RENEW_BEFORE_DAYS)
            return
        if days > 0:
            cfg = self._config.get_config()
            cfg['schedule']['renew_before_days'] = days
            self._config.save_config(cfg)

    def _callback_breaker_open(self):
        """非关键上报是否已熔断（连续失败达阈值）"""
        return self._callback_fail_streak >= CALLBACK_BREAKER_THRESHOLD

    def _send_callback_guarded(self, send):
        """非关键上报的统一出口：进程内连续失败即熔断（spec §11）

        连续失败说明上报通道整体不可用（服务端宕机、网络不通），后续每一次都是同样的
        干等；逐证书各等一份完整超时预算时最坏耗时随证书数线性放大。成功即清零——
        抖动恢复后重新获得完整额度。返回是否真的送出。
        """
        if self._callback_breaker_open():
            if self._logger:
                self._logger.warning(
                    "非关键上报已连续失败 %d 次，跳过本轮剩余同类上报（仅保留本地记录）",
                    self._callback_fail_streak)
            return False
        try:
            send()
        except Exception as e:
            self._callback_fail_streak += 1
            if self._logger:
                self._logger.warning("部署回调失败（非关键，连续第 %d 次）: %s",
                                     self._callback_fail_streak, str(e))
            return False
        self._callback_fail_streak = 0
        return True

    def _send_failure_callback(self, api, order_id, message):
        """发送 failure 部署回调（非关键路径，失败仅记日志）"""
        now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        return self._send_callback_guarded(
            lambda: api.callback(order_id=order_id, status='failure',
                                 deployed_at=now, message=message))

    # ==================== 触顶 / 过期 状态转移（静默，不发回调） ====================

    def _persist_meta(self, order_id, updates):
        """尽力持久化终态 metadata：失败仅记日志，不中断整批续签

        仅供可由下一轮前置条件重新推导的 CAPPED/EXPIRED 状态使用。部署意图与明确
        结果属于外部动作硬门禁，必须直接调用 update_metadata 并传播写入失败。
        """
        try:
            self._config.update_metadata(order_id, updates)
        except Exception as e:
            if self._logger:
                self._logger.warning("持久化 metadata 失败（非关键）: order_id=%s, error=%s", order_id, str(e))

    def _enter_capped(self, cert_entry, stage):
        """触顶：置 CAPPED 并记录阶段（issue/deploy），静默等待人工处理，不发回调"""
        order_id = cert_entry['order_id']
        self._persist_meta(order_id, {
            'last_issue_state': ISSUE_STATE_CAPPED,
            'capped_phase': stage,
        })
        meta = cert_entry.setdefault('metadata', {})
        meta['last_issue_state'] = ISSUE_STATE_CAPPED
        meta['capped_phase'] = stage
        if self._logger:
            self._logger.warning("证书触顶静默（阶段=%s），等待人工处理: order_id=%s", stage, order_id)

    # ==================== 无进展停更（纯查询路径的绝对边界） ====================

    def _mark_no_progress(self, cert_entry, order_id):
        """记录"本轮只查询、没有任何进展"的起点。

        锚定首次、不滑动窗口：每轮都刷新时间戳等于永远达不到时限，那正是要修的问题。
        进展的判据是"证书状态真的往前走了"——部署发生、CSR 被接受、订单回到可用——
        而不是"这一轮跑完没报错"。
        """
        meta = cert_entry.setdefault('metadata', {})
        if meta.get('no_progress_since'):
            return
        now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        self._persist_meta(order_id, {'no_progress_since': now})
        meta['no_progress_since'] = now

    def _clear_no_progress(self, cert_entry, order_id):
        """有实际进展时清零停更计时"""
        meta = cert_entry.setdefault('metadata', {})
        if not meta.get('no_progress_since'):
            return
        self._persist_meta(order_id, {'no_progress_since': ''})
        meta['no_progress_since'] = ''

    def _stalled_too_long(self, cert_entry):
        """自首次无进展起是否已超过 MAX_NO_PROGRESS_DAYS

        时间戳缺失/损坏、时钟回拨、跳变过大一律返回 False（保守方向：宁可多查
        几轮，也不把还在正常等待签发的证书误判成停更）。损坏时重新锚定当前时间，
        否则一个坏时间戳会让该证书永远绕过这道闸门。
        """
        meta = cert_entry.get('metadata', {})
        since = meta.get('no_progress_since', '')
        if not since:
            return False
        try:
            start = datetime.fromisoformat(str(since).replace('Z', '+00:00'))
        except (ValueError, AttributeError):
            start = None
        if start is None or start.tzinfo is None:
            self._mark_no_progress_reset(cert_entry)
            return False
        elapsed = datetime.now(timezone.utc) - start
        if elapsed < timedelta(0) or elapsed > timedelta(days=_CLOCK_SANITY_MAX_DAYS):
            if self._logger:
                self._logger.warning(
                    "系统时间异常跳变，无进展计时重新锚定: order_id=%s", cert_entry.get('order_id'))
            self._mark_no_progress_reset(cert_entry)
            return False
        return elapsed >= timedelta(days=MAX_NO_PROGRESS_DAYS)

    def _mark_no_progress_reset(self, cert_entry):
        """重新锚定无进展计时到当前时刻（时间戳损坏/时钟跳变时的修复分支）"""
        now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        self._persist_meta(cert_entry.get('order_id'), {'no_progress_since': now})
        cert_entry.setdefault('metadata', {})['no_progress_since'] = now

    def _cleanup_stalled_artifacts(self, cert_entry):
        """停更时清掉在途产物：私钥不能因为一张永远签不出来的证书永久驻留磁盘

        验证文件同样清理——订单已停止跟进，留在 webroot 下的 challenge 文件
        既无用又对外可读。
        """
        self._cleanup_pending_key(cert_entry)
        self._cleanup_pending_csr(cert_entry)
        self._cleanup_verify_files(cert_entry.get('metadata', {}))
        self._persist_meta(cert_entry.get('order_id'), {
            'pending_file_verify': '',
            'pending_verify_paths': [],
        })

    def _enter_expired(self, cert_entry):
        """过期：置 EXPIRED 静默终止，仅留本地日志与人工入口，不发回调"""
        order_id = cert_entry['order_id']
        self._persist_meta(order_id, {'last_issue_state': ISSUE_STATE_EXPIRED})
        cert_entry.setdefault('metadata', {})['last_issue_state'] = ISSUE_STATE_EXPIRED
        if self._logger:
            self._logger.warning("证书已过期，静默终止，等待人工处理: order_id=%s", order_id)

    # ==================== 部署编排（计数 + 回调收敛到编排层） ====================

    @staticmethod
    def _deployment_sites(cert_entry):
        """部分接纳后仅返回仍失败的绑定，否则返回全部绑定。"""
        meta = cert_entry.get('metadata', {})
        failed = meta.get('failed_site_names') or []
        if failed:
            return list(dict.fromkeys(failed))
        sites = cert_entry.get('site_name', [])
        if isinstance(sites, str):
            sites = [sites] if sites else []
        return list(sites)

    def _begin_failed_site_retry(self, cert_entry):
        """失败绑定补部署轮次在查询前独立计数。"""
        meta = cert_entry.setdefault('metadata', {})
        if not meta.get('failed_site_names'):
            return False
        count = int(meta.get('failed_site_retry_count', 0) or 0) + 1
        self._config.update_metadata(
            cert_entry['order_id'], {'failed_site_retry_count': count})
        meta['failed_site_retry_count'] = count
        return True

    def _rollback_failed_site_retry(self, cert_entry):
        """订单仍在签发、尚无可部署证书时不消耗失败绑定配额。"""
        meta = cert_entry.setdefault('metadata', {})
        count = max(0, int(meta.get('failed_site_retry_count', 0) or 0) - 1)
        self._config.update_metadata(
            cert_entry['order_id'], {'failed_site_retry_count': count})
        meta['failed_site_retry_count'] = count

    def _begin_deploy_attempt(self, order_id, cert_entry):
        """递增部署计数并标记 started（持久化一个新的部署意图）。

        崩溃恢复重放同一意图不自增：若上轮已 started 未结束，复用同一尝试。
        返回本次尝试后的部署计数。
        """
        meta = cert_entry.setdefault('metadata', {})
        if meta.get('deploy_started'):
            return meta.get('deploy_attempt_count', 0)
        count = meta.get('deploy_attempt_count', 0) + 1
        self._config.update_metadata(order_id, {
            'deploy_attempt_count': count,
            'deploy_started': True,
        })
        meta['deploy_attempt_count'] = count
        meta['deploy_started'] = True
        return count

    def _conclude_deploy_attempt(self, order_id, cert_entry, counts_cleared):
        """结束一次部署尝试：清除 started 标记。

        counts_cleared=True（至少一个站点成功，deploy_multi 已清零计数与 started）时仅同步内存；
        否则（全失败/落盘前失败）保留部署计数，仅清 started 供下一轮作为新意图重新递增。
        """
        meta = cert_entry.setdefault('metadata', {})
        if counts_cleared:
            meta['deploy_attempt_count'] = 0
            meta['deploy_started'] = False
            return
        if meta.get('deploy_started'):
            self._config.update_metadata(order_id, {'deploy_started': False})
            meta['deploy_started'] = False

    def _clear_deploy_block(self, cert_entry, order_id):
        """环境恢复后清除阻断标记，避免面板一直显示已消失的旧原因

        同时清零上报计数：环境真的恢复过，下次再坏是新一轮故障，应当重新获得
        完整的上报额度。
        """
        meta = cert_entry.setdefault('metadata', {})
        if not meta.get('last_deploy_block_reason') and not meta.get('last_deploy_block_at') \
                and not meta.get('block_report_count'):
            return
        self._persist_meta(order_id, {'last_deploy_block_reason': '', 'last_deploy_block_at': '',
                                      'block_report_count': 0})
        meta['last_deploy_block_reason'] = ''
        meta['last_deploy_block_at'] = ''
        meta['block_report_count'] = 0
        if self._logger:
            self._logger.info("Web 服务配置已恢复，清除环境阻断标记: order_id=%s", order_id)

    def _report_block_once(self, cert_entry, api, order_id, message, prev_reason):
        """阻断上报的统一闸门：原因变化才发，且总次数封顶 MAX_BLOCK_REPORT_COUNT

        原因相等这一道抑制不足以构成边界——原因串由 checkWebConfig 返回值或异常
        文本拼成，含 PID/路径/时间等可变内容时每轮都算"变化"；环境好坏抖动时
        每次复发也重新触发。阻断按设计不递增 deploy_attempt_count（修好即自动
        恢复，不必人工解除），于是此前整条回调路径没有任何上限。计数由
        _clear_deploy_block 在环境恢复时清零，故长期坏 → 静默，坏后修好 → 额度重置。
        """
        if message == prev_reason:
            return
        meta = cert_entry.setdefault('metadata', {})
        count = meta.get('block_report_count', 0)
        if count >= MAX_BLOCK_REPORT_COUNT:
            if self._logger:
                self._logger.warning(
                    "环境阻断上报已达上限 %d 次，转为静默（仅本地记录与面板告警）: order_id=%s",
                    MAX_BLOCK_REPORT_COUNT, order_id)
            return
        # 熔断打开时本次根本不会发出去，不能消耗额度：否则一次上报通道故障就能
        # 把 10 次阻断上报额度凭空烧完，等通道恢复时该证书已永久静默
        if self._callback_breaker_open():
            if self._logger:
                self._logger.warning(
                    "上报通道已熔断，本次阻断上报跳过且不消耗额度: order_id=%s", order_id)
            return
        count += 1
        self._persist_meta(order_id, {'block_report_count': count})
        meta['block_report_count'] = count
        self._send_failure_callback(api, order_id, message)

    def _report_block(self, cert_entry, api, order_id, message):
        """记录部署阻断原因并按变化触发上报一次（与环境闸门同一机制与纪律）

        不递增 deploy_attempt_count：这不是一次"新的部署尝试意图"，且计数会让证书在
        10 轮后静默 CAPPED，问题修好还要人工解除。
        """
        now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        meta = cert_entry.setdefault('metadata', {})
        prev_reason = meta.get('last_deploy_block_reason', '')
        if self._logger:
            self._logger.error("部署阻断: order_id=%s, reason=%s", order_id, message)
        self._persist_meta(order_id, {
            'last_deploy_block_reason': message,
            'last_deploy_block_at': now,
        })
        meta['last_deploy_block_reason'] = message
        meta['last_deploy_block_at'] = now
        self._report_block_once(cert_entry, api, order_id, message, prev_reason)

    def _check_deploy_environment(self, cert_entry, api, order_id):
        """部署前环境闸门：Web 配置本就损坏时放弃本轮，返回阻断原因或 None。

        计数语义：环境阻断不是"一个新的部署尝试意图"，不递增 deploy_attempt_count——
        否则一个无关站点的坏配置会在 10 轮后把所有证书静默推入 CAPPED，且配置修好后
        还要人工解除。不计数则修好即自动恢复。

        回调语义：仍按"明确部署失败"上报一次（spec §2.8 排除的只有触顶/过期/policy 阻断）。
        服务端以"订单最新一条上报仍为 failure"做状态判定并按 TTL 提醒，不上报会让该订单
        从服务端的失败视图里消失，才是真正的静默过期。

        本轮结果同样计为失败而非等待：既然已按 failure 上报服务端，本地汇总与面板
        必须给出一致口径，否则用户看到"续签成功"却什么都没发生。

        check_web_config() 自身抛异常（面板内部失败、nginx 二进制缺失、配置目录不可读等）
        与它返回错误同样处置：整轮开头的 probe_panel_runtime 只保证模块可导入，不保证调用
        不抛。裸调用会让异常穿透到上层通用 except，届时回调发不出、阻断原因不落盘、计数
        停在 0 而永不触顶——正是本函数要消灭的那种不可见失败。
        """
        try:
            env_err = check_web_config()
        except Exception as e:
            env_err = 'Web 配置检查执行异常: %s' % e
        if not env_err:
            self._clear_deploy_block(cert_entry, order_id)
            return None
        now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        message = 'Web 服务配置校验失败（非本次部署导致）: %s' % _compact_reason(env_err)
        if self._logger:
            self._logger.error("环境阻断，本轮跳过部署且不计数: order_id=%s, error=%s", order_id, env_err)
        meta = cert_entry.setdefault('metadata', {})
        prev_reason = meta.get('last_deploy_block_reason', '')
        self._persist_meta(order_id, {
            'last_deploy_block_reason': message,
            'last_deploy_block_at': now,
        })
        meta['last_deploy_block_reason'] = message
        meta['last_deploy_block_at'] = now
        # 变化触发 + 次数封顶：服务端 DeployFailureReminderCommand 是电平驱动（判据为"订单
        # 最新一行仍为 failure"），一行就足以让订单永久留在失败视图，逐日重发零信息增量却会
        # 淹没管理端列表且被 PurgeCommand 的终态过滤永久保留。与本模块既有的"仅状态变化才
        # 记录/落盘"同一纪律，上限见 _report_block_once
        self._report_block_once(cert_entry, api, order_id, message, prev_reason)
        return message

    def _deploy_and_report(self, cert_entry, api, fullchain, key_pem, site_names, domains, order_id):
        """编排层统一部署：环境闸门 → 递增部署意图 → 底层部署（抑制回调）→ 结果落盘后统一回调。

        底层 deploy_multi 只返回结构化结果、不自行回调；每次成功与每次明确失败各尽力上报一次；
        第 10 次（最后一次）失败在 message 标注"已达重试上限"。返回底层结果列表；
        环境阻断时已上报 failure，抛 RuntimeError 让本轮按失败收敛（口径与服务端一致）。
        """
        block_reason = self._check_deploy_environment(cert_entry, api, order_id)
        if block_reason:
            raise RuntimeError(block_reason)
        # 部署前的序列号：deploy_multi 仅在全部站点成功时才覆写它，
        # 因此部分失败轮次不会污染更替判定
        was_failed_retry = bool(cert_entry.get('metadata', {}).get('failed_site_names'))
        prev_serial = cert_entry.get('metadata', {}).get('cert_serial', '')
        count = self._begin_deploy_attempt(order_id, cert_entry)
        at_cap = count >= MAX_DEPLOY_ATTEMPT_COUNT
        now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        try:
            results = self._deployer.deploy_multi(
                site_names=site_names, fullchain_pem=fullchain, key_pem=key_pem,
                order_id=order_id, domains=domains, api_client=api, send_callback=False)
        except DeployError as e:
            # 底层在结果落盘前失败（校验 / metadata 写失败）：编排层补发失败回调后再抛出
            self._conclude_deploy_attempt(order_id, cert_entry, counts_cleared=False)
            self._send_deploy_callback(api, order_id, 'failure', now, str(e), at_cap)
            raise
        counts_cleared = any(r.get('status') for r in results)
        status, message = _deploy_callback_decision(results)
        self._conclude_deploy_attempt(order_id, cert_entry, counts_cleared)

        # 证书更替检测：服务端反复返回同一张旧证书时，此前每轮都报 success，
        # 服务端最新一行永远是成功、面板一路「正常」→「即将过期」→「已过期」
        if status == 'success' and not was_failed_retry:
            # 本轮服务端交付的序列号直接从待部署的 fullchain 解析，**不从 metadata 读回**：
            # deploy_multi 只 update_metadata 写盘、不回写内存 cert_entry，从 metadata
            # 读出来的永远还是部署前的旧值，与 prev_serial 恒等 —— 那会让每张正常续签的
            # 证书在第二次自动续签时被误判成「服务端未更替」并改判 failure，而服务端的
            # 失败提醒是电平驱动的，健康证书就此永久留在失败视图里
            new_serial = ''
            cert_info = cert_utils.parse_cert_info(fullchain, logger=self._logger)
            if cert_info:
                new_serial = cert_info.get('serial', '')
            stale = self._track_cert_unchanged(cert_entry, order_id, prev_serial, new_serial)
            if stale:
                status, message = 'failure', stale
                # deploy_multi 的正常成功路径已清零证书级计数；改判失败必须恢复
                # 本次尝试并保留无进展边界，否则相同旧证书会无限改写。
                self._config.update_metadata(order_id, {
                    'deploy_attempt_count': count,
                    'deploy_started': False,
                })
                meta = cert_entry.setdefault('metadata', {})
                meta['deploy_attempt_count'] = count
                meta['deploy_started'] = False
                self._mark_no_progress(cert_entry, order_id)

        self._send_deploy_callback(api, order_id, status, now, message, at_cap)
        return results

    def _track_cert_unchanged(self, cert_entry, order_id, prev_serial, new_serial):
        """全部站点成功时比对序列号，判断服务端是否真的换了证书

        两个序列号都由调用方传入，本方法不从 metadata 读取任何一端：`deploy_multi`
        只写盘、不回写内存 cert_entry，读 metadata 拿到的"新值"其实还是旧值。

        本方法独占 `unchanged_cert_rounds` 的所有权（序列号变化时清零、相同时递增），
        它**不得**出现在 `DEPLOY_SUCCESS_RESET_KEYS` 里（spec §3.8）：检测在部署之后
        执行，若部署成功也清零，计数就会每轮先归零再递增到 1，永远达不到阈值。

        只在编排层（自动续签）判定，手动部署不参与——用户点两次「部署」、粘贴私钥后
        重新部署、加绑站点后部署，都会用同一张证书，那是正常操作不是服务端故障。

        判据要求两端序列号都非空：cert_utils 解析失败时返回 ''，老 OpenSSL 上
        '' == '' 会让每次部署都误报。到期时间未前移只作为序列号缺失时的降级判据，
        不与序列号并列——CA 重签常保留原订单剩余有效期，新序列号 + 相同 notAfter
        是完全正常的结果，而 local 模式走的恰恰是重签路径。

        连续 2 轮才升级为 failure：单轮相同可能是上一轮部分失败后的正常重试
        （F8 之后失败站点会次日重试，那次补部署必然是同一张证书）。
        """
        meta = cert_entry.setdefault('metadata', {})
        # 上一轮有站点失败时归零：那一轮压根没写 cert_serial，本轮相同属预期重试
        if not (prev_serial and new_serial and prev_serial == new_serial):
            if meta.get('unchanged_cert_rounds'):
                self._persist_meta(order_id, {'unchanged_cert_rounds': 0})
                meta['unchanged_cert_rounds'] = 0
            return ''

        rounds = meta.get('unchanged_cert_rounds', 0) + 1
        self._persist_meta(order_id, {'unchanged_cert_rounds': rounds})
        meta['unchanged_cert_rounds'] = rounds
        if self._logger:
            self._logger.warning("服务端返回的证书未更替（第 %d 轮）: order_id=%s, serial=%s",
                                 rounds, order_id, new_serial)
        if rounds < CERT_UNCHANGED_ROUNDS:
            return ''
        return '服务端连续 %d 轮返回同一张证书（序列号 %s 未变），证书未实际更新' % (
            rounds, new_serial)

    def _send_deploy_callback(self, api, order_id, status, deployed_at, message, at_cap):
        """编排层统一发送部署结果回调（非关键路径，失败仅记日志）。

        第 10 次（最后一次）部署失败在 message 标注"已达重试上限"（自由文本，零协议变化）。
        """
        if status == 'failure' and at_cap:
            message = ('%s；已达重试上限' % message) if message else '已达重试上限'
        return self._send_callback_guarded(
            lambda: api.callback(order_id=order_id, status=status,
                                 deployed_at=deployed_at, message=message))

    def _track_order_status(self, cert_entry, order_id, status):
        """记录服务端返回的订单状态（展示专用，不参与任何门禁判定），返回是否发生变化

        pull 模式此前对非 active 一律 info 日志 + 返回 False，本轮被计为「等待签发」，
        面板显示「自动续签中」——一张已被取消的订单会这样每天轮询到过期为止。
        用 _persist_meta 而非 update_metadata：该值每轮都能从 API 重新推导，
        订单不存在时不该把良性 pending 变成失败。

        返回值供调用方实现 spec §2.4 的「仅状态相对上一轮变化时才告警并计入失败统计」：
        这类订单每日查询即可自愈，逐轮告警是零信息增量的噪声。
        """
        meta = cert_entry.setdefault('metadata', {})
        if meta.get('last_order_status', '') == status:
            return False
        self._persist_meta(order_id, {'last_order_status': status})
        meta['last_order_status'] = status
        return True

    def _query_order(self, cert_entry, api, order_id):
        """查询订单，并把订单级确定性失败（spec §2.2）落成可解释、有边界的状态。

        order_not_found / cert_not_found / invalid_order 是配置问题而非网络抖动，须记入
        展示字段并起停更计时后原样抛出（本轮该证书停止，等人工核对配置）：
        - 不碰任何尝试计数——查询路径本就不计数，spec §3.2；
        - 必须 _mark_no_progress——否则一张订单已被删除的证书每轮都在这里抛异常，
          永远走不到下方的 _mark_no_progress，14 天停更边界形同不存在，
          local 模式的 pending 私钥也就永久驻留磁盘。
        """
        try:
            return api.query_order(order_id)
        except APIError as e:
            if e.order_rejected:
                self._track_order_status(cert_entry, order_id, e.error_code)
                self._mark_no_progress(cert_entry, order_id)
                if self._logger:
                    self._logger.error("订单查询被拒（%s），等待人工核对配置: order_id=%s",
                                       e.error_code, order_id)
            raise

    def _renew_pull(self, cert_entry, api):
        """Pull 模式续签：查询订单 → 证书就绪则部署"""
        order_id = cert_entry['order_id']
        failed_retry = self._begin_failed_site_retry(cert_entry)
        if self._logger:
            self._logger.info("Pull 模式续签: order_id=%s", order_id)

        cert_data = self._query_order(cert_entry, api, order_id)
        self._update_renew_before_days(api)
        order_id = self._check_order_update(cert_entry, cert_data)

        status = cert_data.get('status', '')
        certificate = cert_data.get('certificate', '')
        ca_certificate = cert_data.get('ca_certificate', '')
        private_key = cert_data.get('private_key', '')

        if status != 'active' or not certificate:
            if failed_retry and classify_order_status(status) == ORDER_CLASS_WAITING:
                self._rollback_failed_site_retry(cert_entry)
            # 记录服务端订单状态供面板展示。不写 last_issue_state——那是 spec 定义的
            # 带门禁语义的字段，把 cancelled 之类写进去会让证书被前置过滤永久跳过，
            # 违反「后续轮次仍可查询自愈」；renewed/reissued 更是链延续标记而非故障
            changed = self._track_order_status(cert_entry, order_id, status)
            self._mark_no_progress(cert_entry, order_id)
            # 仅状态变化时按类别升级日志级别（spec §2.4）：终态/链式异常需人工介入，
            # 而在途等待与未知新增状态属正常轮询，逐轮 error 是零信息增量的噪声
            cls = classify_order_status(status)
            if self._logger:
                if changed and cls == ORDER_CLASS_CHAIN:
                    self._logger.error(
                        "订单状态 %s 表示续费/重签链数据异常（服务端本应自动跟随），"
                        "等待人工处理: order_id=%s", status, order_id)
                elif changed and cls == ORDER_CLASS_TERMINAL:
                    self._logger.error("订单已进入终态 %s，等待人工处理: order_id=%s",
                                       status, order_id)
                else:
                    self._logger.info("证书未就绪: status=%s", status)
            return False

        self._track_order_status(cert_entry, order_id, '')

        if not ca_certificate:
            # 同样是"查了但拿不到可用证书"，必须计入停更时限：否则服务端长期只返回
            # 叶子证书就能让这条路径每天空查询到永远
            self._mark_no_progress(cert_entry, order_id)
            if self._logger:
                self._logger.warning("缺少中间证书，等待下次检查")
            return False

        # 构建完整证书链并部署
        fullchain = cert_utils.build_fullchain(certificate, ca_certificate)
        site_names = self._deployment_sites(cert_entry)
        domains = cert_entry.get('domains', [])

        if not site_names:
            # 服务端已签发 active 证书，客户端却没有部署目标：复用既有阻断机制记录原因
            # （已持久化、已在面板渲染成橙色告警、环境恢复时自动清除），并按失败上报一次。
            # 此前只记 warning 并返回 False，本轮被计为「等待签发」，服务端零回调，
            # 面板对已有到期时间的证书显示「正常」——两侧都看不出证书压根没部署上
            self._report_block(cert_entry, api, order_id, '证书已签发但未绑定任何站点，无法部署')
            raise RuntimeError('未绑定站点，无法部署')

        # 拿到可部署证书即为进展：即使随后部署失败，那条路径由 deploy_attempt_count 兜底
        self._clear_no_progress(cert_entry, order_id)
        results = self._deploy_and_report(
            cert_entry, api, fullchain, private_key, site_names, domains, order_id)
        return self._check_deploy_results(results, order_id)

    def _renew_local(self, cert_entry, api):
        """Local 模式续签：生成 CSR → 提交 → 等待签发 → 部署"""
        order_id = cert_entry['order_id']
        meta = cert_entry.get('metadata', {})

        if self._logger:
            self._logger.info("Local 模式续签: order_id=%s", order_id)

        last_state = meta.get('last_issue_state', '')

        # 部分绑定已接纳证书后，不再创建新 CSR；查询当前 active 证书并只补失败绑定。
        if meta.get('failed_site_names') and not self._has_pending_csr(cert_entry):
            self._begin_failed_site_retry(cert_entry)
            cert_data = self._query_order(cert_entry, api, order_id)
            self._update_renew_before_days(api)
            order_id = self._check_order_update(cert_entry, cert_data)
            status = cert_data.get('status', '')
            if status != ISSUE_STATE_ACTIVE:
                if classify_order_status(status) == ORDER_CLASS_WAITING:
                    self._rollback_failed_site_retry(cert_entry)
                self._track_order_status(cert_entry, order_id, status)
                self._mark_no_progress(cert_entry, order_id)
                return False
            key_pem = self._find_active_private_key(cert_entry, cert_data)
            if not key_pem:
                raise RuntimeError("已接纳证书的配对私钥不可用，无法补部署失败绑定")
            certificate = cert_data.get('certificate', '')
            ca_certificate = cert_data.get('ca_certificate', '')
            if not certificate or not ca_certificate:
                raise RuntimeError("证书内容不完整，无法补部署失败绑定")
            results = self._deploy_and_report(
                cert_entry, api, cert_utils.build_fullchain(certificate, ca_certificate),
                key_pem, self._deployment_sites(cert_entry),
                cert_entry.get('domains', []), order_id)
            return self._check_deploy_results(results, order_id)

        # 有在途订单（processing / active 秒签待部署，spec §1.5）：只查询、绝不提交新 CSR
        if last_state in IN_FLIGHT_ISSUE_STATES:
            return self._handle_processing(cert_entry, api)

        # 存在在途 CSR（上轮 POST 结果不确定）：以同一 CSR 恢复，不重新生成、不增计数
        if self._has_pending_csr(cert_entry):
            return self._recover_pending_submit(cert_entry, api)

        # 到期时间未知且无在途 CSR：先查询 API 回填元数据再按正常逻辑判定，
        # 避免对"部署成功但元数据丢失/带外换证"的证书盲目重新提交 CSR（对齐 sslctl）
        if _expiry_unknown(meta):
            return self._refresh_and_maybe_renew_local(cert_entry, api)

        # 签发触顶防御（正常由前置过滤拦截并置 CAPPED；直接调用时在此静默停止，绝不第 11 次）
        if meta.get('issue_retry_count', 0) >= MAX_ISSUE_RETRY_COUNT:
            self._enter_capped(cert_entry, CAPPED_PHASE_ISSUE)
            return False

        return self._submit_new_csr(cert_entry, api)

    def _recover_pending_submit(self, cert_entry, api):
        """上轮 CSR 提交结果不确定：只查询，并用服务端 CSR 判断归属。"""
        order_id = cert_entry['order_id']
        cert_data = self._query_order(cert_entry, api, order_id)
        self._update_renew_before_days(api)
        order_id = self._check_order_update(cert_entry, cert_data)
        status = _normalize_issue_status(cert_data.get('status', ''))

        if status in IN_FLIGHT_ISSUE_STATES:
            self._config.update_metadata(order_id, {'last_issue_state': ISSUE_STATE_PROCESSING})
            cert_entry.setdefault('metadata', {})['last_issue_state'] = ISSUE_STATE_PROCESSING
            if self._logger:
                self._logger.info("在途 CSR 恢复：进入 CSR 归属校验: order_id=%s", order_id)
            return self._handle_processing(cert_entry, api, cert_data=cert_data)

        # 真终态 / 链式异常：只写展示字段 last_order_status，**不写 last_issue_state**
        # （spec §2.4）。在途标记保持不动，本条路径恒为「只查询」，绝不落到新的 POST；
        # 仅状态变化时记 error，避免每轮重复刷日志
        raw_status = cert_data.get('status', '')
        if self._track_order_status(cert_entry, order_id, raw_status):
            if self._logger:
                if classify_order_status(raw_status) == ORDER_CLASS_CHAIN:
                    self._logger.error(
                        "在途 CSR 查询到链式状态 %s（续费/重签链数据异常），等待人工处理: order_id=%s",
                        raw_status, order_id)
                else:
                    self._logger.error(
                        "在途 CSR 查询到订单终态，停止自动提交等待人工处理: order_id=%s, status=%s",
                        order_id, raw_status)
        elif self._logger:
            self._logger.info("订单仍处于状态 %s，等待人工处理: order_id=%s", raw_status, order_id)
        self._mark_no_progress(cert_entry, order_id)
        return False

    def _refresh_and_maybe_renew_local(self, cert_entry, api):
        """Local 模式到期时间未知：查询 API 回填元数据后再按正常续签逻辑判定

        - 查询失败：向上抛出（本轮按失败处理），不盲目提交 CSR
        - 服务端未返回证书内容 / 证书解析失败：本轮跳过（返回 False），下轮再试
        - 回填成功后：仍需续签（临期/已过期）→ 提交新 CSR；否则本轮不续签
        """
        order_id = cert_entry['order_id']
        cert_data = self._query_order(cert_entry, api, order_id)
        self._update_renew_before_days(api)
        order_id = self._check_order_update(cert_entry, cert_data)

        certificate = cert_data.get('certificate', '')
        if not certificate:
            self._mark_no_progress(cert_entry, order_id)
            if self._logger:
                self._logger.warning(
                    "证书到期时间未知且服务端未返回证书内容，本轮跳过: order_id=%s, status=%s",
                    order_id, cert_data.get('status', ''))
            return False

        cert_info = cert_utils.parse_cert_info(
            certificate, logger=self._logger)
        if not cert_info or not cert_info.get('not_after'):
            self._mark_no_progress(cert_entry, order_id)
            if self._logger:
                self._logger.warning("回填到期时间失败（证书解析错误），本轮跳过: order_id=%s", order_id)
            return False

        expires_at = cert_info['not_after'].strftime('%Y-%m-%dT%H:%M:%SZ')
        cert_serial = cert_info.get('serial', '')
        self._config.update_metadata(order_id, {
            'cert_expires_at': expires_at,
            'cert_serial': cert_serial,
        })
        meta = cert_entry.setdefault('metadata', {})
        meta['cert_expires_at'] = expires_at
        meta['cert_serial'] = cert_serial
        if self._logger:
            self._logger.info("证书到期时间已从服务端回填: order_id=%s, expires_at=%s",
                              order_id, expires_at)

        # 回填后按正常逻辑判定：剩余期限充足则本轮不续签
        renew_days = self._config.get_renew_before_days(cert_entry)
        if not needs_renewal(cert_entry, renew_days):
            # 到期时间已回填成功、证书健康：这是进展，不是停滞
            self._clear_no_progress(cert_entry, order_id)
            if self._logger:
                self._logger.info("回填后剩余期限充足，本轮不续签: order_id=%s", order_id)
            return False

        return self._submit_new_csr(cert_entry, api, prequery=cert_data)

    def _check_order_update(self, cert_entry, cert_data):
        """检查 API 返回的 order_id 是否变化（续费），变化则更新配置和 pending key 路径"""
        old_id = cert_entry['order_id']
        new_id = cert_data.get('order_id')
        if not new_id or int(new_id) == int(old_id):
            return old_id
        new_id = int(new_id)
        if self._logger:
            self._logger.info("订单续费，ID 更新: %s → %s", old_id, new_id)
        try:
            self._config.update_order_id(old_id, new_id)
        except ValueError as e:
            if self._logger:
                self._logger.warning("更新订单 ID 失败: %s", str(e))
            return old_id
        # 重命名 pending key 目录
        old_name = cert_entry.get('cert_name', 'order-%s' % old_id)
        new_name = 'order-%d' % new_id
        old_dir = os.path.join(self._data_dir, 'pending-keys', old_name)
        new_dir = os.path.join(self._data_dir, 'pending-keys', new_name)
        try:
            if os.path.isdir(old_dir) and not os.path.exists(new_dir):
                os.rename(old_dir, new_dir)
        except OSError as e:
            if self._logger:
                self._logger.warning("重命名 pending key 目录失败: %s", str(e))
        # 更新内存中的 cert_entry
        cert_entry['order_id'] = new_id
        cert_entry['cert_name'] = new_name
        return new_id

    def _server_csr_ownership(self, cert_entry, server_csr, pending_key):
        """返回服务端 CSR 是否属于本机；无法可靠判断时返回 None。"""
        info = cert_utils.parse_csr_info(server_csr)
        meta = cert_entry.get('metadata', {})
        expected_hash = meta.get('last_csr_hash', '')
        domains = cert_entry.get('domains', [])
        expected_cn = domains[0] if domains else ''
        if not info or not expected_hash or not expected_cn:
            return None
        if info.get('hash') != expected_hash:
            return False
        if info.get('common_name', '').rstrip('.').lower() != str(expected_cn).rstrip('.').lower():
            return False
        return cert_utils.verify_csr_key_match(server_csr, pending_key)

    def _clear_csr_metadata(self, cert_entry, order_id):
        updates = {'last_csr_hash': '', 'csr_submitted_at': ''}
        self._config.update_metadata(order_id, updates)
        cert_entry.setdefault('metadata', {}).update(updates)

    def _find_active_private_key(self, cert_entry, cert_data):
        """按 API 私钥、已成功绑定站点私钥的顺序寻找 active 证书配对私钥。"""
        certificate = cert_data.get('certificate', '')
        if not certificate:
            return None
        candidates = [cert_data.get('private_key', '')]
        failed = set(cert_entry.get('metadata', {}).get('failed_site_names') or [])
        sites = cert_entry.get('site_name', [])
        if isinstance(sites, str):
            sites = [sites] if sites else []
        for site_name in sites:
            if site_name in failed:
                continue
            try:
                import panelSite
                previous = self._deployer._capture_current_ssl(
                    panelSite.panelSite(), site_name)
            except Exception:
                previous = None
            if previous:
                candidates.append(previous.get('key', ''))
        for key_pem in candidates:
            if key_pem and cert_utils.verify_cert_key_match(certificate, key_pem):
                return key_pem
        return None

    def _handle_processing(self, cert_entry, api, cert_data=None):
        """处理已提交 CSR 的 processing 状态"""
        order_id = cert_entry['order_id']
        meta = cert_entry.get('metadata', {})
        failed_retry = self._begin_failed_site_retry(cert_entry)

        # 查询订单状态（只 GET，不重复 POST，不增计数）
        if cert_data is None:
            cert_data = self._query_order(cert_entry, api, order_id)
        self._update_renew_before_days(api)
        order_id = self._check_order_update(cert_entry, cert_data)
        # pending / 已在处理 统一归一为 processing 继续等待（spec §2.6/§3.5）
        status = _normalize_issue_status(cert_data.get('status', ''))

        pending_key = self._read_pending_key(cert_entry)
        if pending_key:
            ownership = self._server_csr_ownership(cert_entry, cert_data.get('csr', ''), pending_key)
            if ownership is None:
                if failed_retry:
                    self._rollback_failed_site_retry(cert_entry)
                changed = self._track_order_status(
                    cert_entry, order_id, cert_data.get('status', ''))
                self._mark_no_progress(cert_entry, order_id)
                if self._logger:
                    log = self._logger.error if changed else self._logger.info
                    log("服务端 CSR 缺失或无法验证，保留 pending 并停止本轮: order_id=%s",
                        order_id)
                return False
            if ownership is False:
                self._cleanup_pending_key(cert_entry)
                self._cleanup_pending_csr(cert_entry)
                self._clear_csr_metadata(cert_entry, order_id)
                if status == ISSUE_STATE_PROCESSING:
                    if failed_retry:
                        self._rollback_failed_site_retry(cert_entry)
                    self._persist_meta(order_id, {'last_issue_state': ISSUE_STATE_PROCESSING})
                    meta['last_issue_state'] = ISSUE_STATE_PROCESSING
                    self._track_order_status(cert_entry, order_id, cert_data.get('status', ''))
                    self._mark_no_progress(cert_entry, order_id)
                    if self._logger:
                        self._logger.warning(
                            "服务端在途 CSR 不属于本机，清理本机 pending 后只查询跟随: order_id=%s",
                            order_id)
                    return False
                if status == ISSUE_STATE_ACTIVE:
                    pending_key = self._find_active_private_key(cert_entry, cert_data)
                    if not pending_key:
                        if failed_retry:
                            self._rollback_failed_site_retry(cert_entry)
                        self._persist_meta(order_id, {'last_issue_state': ''})
                        meta['last_issue_state'] = ''
                        return self._submit_new_csr(cert_entry, api, prequery=cert_data)

        if status == ISSUE_STATE_PROCESSING:
            if failed_retry:
                self._rollback_failed_site_retry(cert_entry)
            # 检查是否有新的验证文件需要放置
            self._try_place_verify_file(cert_entry, cert_data)
            # 记录原始状态供面板展示：unpaid / cancelling 这类可自愈中间态在这里与正常
            # 等签发同样归一为 processing，若不落展示字段，卡在 unpaid 的证书与正常等待
            # 完全无法区分，用户只能等 14 天后停更才发现
            self._track_order_status(cert_entry, order_id, cert_data.get('status', ''))
            self._mark_no_progress(cert_entry, order_id)
            if self._logger:
                self._logger.info("证书仍在处理中，继续等待")
            return False

        if status != ISSUE_STATE_ACTIVE:
            # 真终态 / 链式异常：订单状态**只写 last_order_status**（展示专用），
            # 绝不写 last_issue_state（spec §2.4）。后者语义是「有无在途订单」，混入订单
            # 状态会让两个概念互相覆盖，还会带来一条扣费路径：状态被改写成 cancelled 之类
            # 自由文本后既不在 TERMINAL_ISSUE_STATES（前置过滤拦不住）、又不等于
            # processing（_renew_local 不再走查询分支），下一轮直接落到 _submit_new_csr
            # 发出 POST，而 POST 会触发服务端 pay 扣费。保持在途标记不动，本条路径就
            # 恒为「只查询」，边界由无进展时限提供
            raw_status = cert_data.get('status', '')
            changed = self._track_order_status(cert_entry, order_id, raw_status)
            cls = classify_order_status(raw_status)
            if changed:
                self._cleanup_verify_files(meta)
                self._config.update_metadata(order_id, {
                    'pending_file_verify': '',
                    'pending_verify_paths': [],
                })
                meta['pending_file_verify'] = ''
                meta['pending_verify_paths'] = []
                if self._logger:
                    if cls == ORDER_CLASS_CHAIN:
                        self._logger.error(
                            "订单状态 %s 表示续费/重签链数据异常（服务端本应自动跟随），"
                            "等待人工处理: order_id=%s", raw_status, order_id)
                    else:
                        self._logger.error("订单状态异常: %s，等待人工处理: order_id=%s",
                                           raw_status, order_id)
            elif self._logger:
                self._logger.info("订单仍处于状态 %s，等待人工处理: order_id=%s",
                                  raw_status, order_id)
            self._mark_no_progress(cert_entry, order_id)
            return False

        # 证书已签发，清理验证文件
        self._cleanup_verify_files(meta)

        # 读取 pending key 并部署
        certificate = cert_data.get('certificate', '')
        ca_certificate = cert_data.get('ca_certificate', '')

        if not certificate or not ca_certificate:
            self._mark_no_progress(cert_entry, order_id)
            if self._logger:
                self._logger.warning("证书内容不完整")
            return False

        if not pending_key:
            pending_key = self._find_active_private_key(cert_entry, cert_data)
        if not pending_key:
            self._persist_meta(order_id, {'last_issue_state': ''})
            meta['last_issue_state'] = ''
            return self._submit_new_csr(cert_entry, api, prequery=cert_data)

        fullchain = cert_utils.build_fullchain(certificate, ca_certificate)
        site_names = self._deployment_sites(cert_entry)
        domains = cert_entry.get('domains', [])

        if not site_names:
            # 无绑定站点可部署（如站点删除确认解绑后）：不再静默跳过留下
            # processing 孤儿（每日空查询+私钥永驻）——清理 pending 私钥、清空
            # 签发状态使状态收敛，发 failure 回调并按失败上报；
            # 用户重新绑定站点后走正常重签流程
            if self._logger:
                self._logger.error("无绑定站点可部署，清除签发状态收敛: order_id=%s", order_id)
            self._cleanup_pending_key(cert_entry)
            self._cleanup_pending_csr(cert_entry)
            self._config.update_metadata(order_id, {
                'last_issue_state': '',
                'csr_submitted_at': '',
                'pending_file_verify': '',
                'pending_verify_paths': [],
            })
            self._send_failure_callback(api, order_id, '无绑定站点可部署')
            raise RuntimeError("无绑定站点可部署，已清除签发状态，请重新绑定站点")

        self._clear_no_progress(cert_entry, order_id)
        results = self._deploy_and_report(
            cert_entry, api, fullchain, pending_key, site_names, domains, order_id)

        # 任一绑定成功即已把私钥写入正式站点，清理 pending 副本；后续失败绑定
        # 从已成功站点读取配对私钥。仅本轮全部失败时保留 pending（spec §3.8）。
        if any(r.get('status') for r in results):
            self._cleanup_pending_key(cert_entry)
            self._cleanup_pending_csr(cert_entry)
        return self._check_deploy_results(results, order_id)

    def _submit_new_csr(self, cert_entry, api, prequery=None):
        """生成并提交新的 CSR（一次新的签发逻辑尝试，递增计数）"""
        try:
            order_id = int(cert_entry.get('order_id', 0))
        except (TypeError, ValueError):
            order_id = 0
        if order_id <= 0:
            raise RuntimeError("订单 ID 无效，请重新 setup 或人工修复配置")
        domains = cert_entry.get('domains', [])

        if not domains:
            raise RuntimeError("未配置域名")

        meta = cert_entry.setdefault('metadata', {})
        if meta.get('issue_retry_count', 0) >= MAX_ISSUE_RETRY_COUNT:
            self._enter_capped(cert_entry, CAPPED_PHASE_ISSUE)
            return False
        hours = _remaining_hours(meta)
        if hours is not None and hours < SAFETY_MARGIN_HOURS:
            if self._logger:
                self._logger.warning(
                    "剩余有效期不足安全余量，不建立新 CSR 尝试: order_id=%s", order_id)
            return False

        # query-first：只有本次预查询明确 active 才允许生成私钥、计数和 POST。
        cert_data = prequery
        if cert_data is None:
            cert_data = self._query_order(cert_entry, api, order_id)
            self._update_renew_before_days(api)
            order_id = self._check_order_update(cert_entry, cert_data)
        raw_status = cert_data.get('status', '') if isinstance(cert_data, dict) else ''
        if raw_status != ISSUE_STATE_ACTIVE:
            self._track_order_status(cert_entry, order_id, raw_status)
            self._mark_no_progress(cert_entry, order_id)
            if classify_order_status(raw_status) in (ORDER_CLASS_WAITING, ORDER_CLASS_UNKNOWN):
                self._persist_meta(order_id, {'last_issue_state': ISSUE_STATE_PROCESSING})
                meta['last_issue_state'] = ISSUE_STATE_PROCESSING
            if self._logger:
                self._logger.info(
                    "CSR 提交预查询未处于 active，本轮停止: order_id=%s, status=%s",
                    order_id, raw_status or '(缺失)')
            return False

        # 清理可能残留的 pending（无在途 CSR 才走到这里）
        self._cleanup_pending_key(cert_entry)
        self._cleanup_pending_csr(cert_entry)

        # 校验/派生验证方式（SAN 含 IP 强制 file）；不兼容立即拒绝，不生成/不提交/不增计数
        _, validation_method, err = derive_or_validate_renew_policy(
            domains, cert_entry.get('renew_mode', ''), cert_entry.get('validation_method', ''))
        if err:
            raise RuntimeError(err)

        # 生成 CSR 和私钥
        csr_pem, key_pem, csr_hash = cert_utils.generate_csr(domains)

        # 网络请求前：原子持久化 pending key + CSR + CSR 哈希，并递增 issue_retry_count
        # （计数 = 持久化一个新的逻辑尝试意图；下轮恢复重放同一 CSR 不再递增）
        self._save_pending_key(cert_entry, key_pem)
        self._save_pending_csr(cert_entry, csr_pem)
        # 回滚快照：认证/限流类失败发生在服务端中间件、请求从未进入业务层，那次"尝试意图"
        # 事实上不存在，须连同计数一起还原（见 _do_submit_csr）
        rollback = {
            'issue_retry_count': meta.get('issue_retry_count', 0),
            'last_csr_hash': meta.get('last_csr_hash', ''),
            'csr_submitted_at': meta.get('csr_submitted_at', ''),
        }
        retry_count = meta.get('issue_retry_count', 0) + 1
        now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        try:
            self._config.update_metadata(order_id, {
                'issue_retry_count': retry_count,
                'last_csr_hash': csr_hash,
                'csr_submitted_at': now,
            })
        except Exception:
            self._cleanup_pending_key(cert_entry)
            self._cleanup_pending_csr(cert_entry)
            meta.update(rollback)
            raise
        meta['issue_retry_count'] = retry_count
        meta['last_csr_hash'] = csr_hash
        meta['csr_submitted_at'] = now

        return self._do_submit_csr(cert_entry, api, csr_pem, validation_method,
                                   rollback=rollback)

    def _do_submit_csr(self, cert_entry, api, csr_pem, validation_method, rollback=None):
        """执行 CSR 提交并处理响应。

        - 传输不确定（超时/断连/解析失败）：保留 pending key + CSR 作为在途标记，
          下轮查询订单状态恢复（不重复 POST），返回 False
        - token/账号/限流类确定性失败（spec §2.2）：请求被服务端中间件拒在业务层之外，
          没有创建任何东西也没有发生任何"尝试"，清理 pending 并**回滚签发计数**后抛出
        - 明确业务拒绝（含服务端未接收提交）：确认未创建新证书，清理 pending key + CSR，
          计数保留（服务端确实处理并拒绝了这次尝试），抛出
        - 成功：pending / 已在处理 归一 processing，放置验证文件，标记 processing
        """
        order_id = cert_entry['order_id']
        domains = cert_entry.get('domains', [])
        try:
            cert_data = api.submit_csr(order_id, csr_pem, domains,
                                       validation_method=validation_method)
        except APIError as e:
            if getattr(e, 'transport', False):
                # 不确定结果：保留 pending key + CSR，下轮以同一 CSR 恢复，不重生不增计数。
                # 恢复走查询路径同样不计数，因此这里必须起停更计时——否则一条永远
                # 超时的链路会以"在途 CSR"的名义无限重试查询
                self._mark_no_progress(cert_entry, order_id)
                if self._logger:
                    self._logger.warning("CSR 提交结果不确定（%s），保留 pending 待下轮恢复: order_id=%s",
                                         str(e), order_id)
                return False
            # 明确业务拒绝：确认未创建新证书，清理 pending
            self._cleanup_pending_key(cert_entry)
            self._cleanup_pending_csr(cert_entry)
            self._clear_csr_metadata(cert_entry, order_id)
            if e.error_code == ERR_ORDER_IN_PROGRESS:
                # spec §2.2 里唯一的过渡态：服务端明确告知订单已在途（unpaid/pending，
                # 签发进行中），完成后自行消失。必须归一到「已在处理」而非按业务拒绝
                # 停止——后者每轮都会重新生成并提交 CSR、每轮各烧一次签发额度，10 轮后
                # 把一张正在正常签发的证书误判触顶，正是 spec 要求「不做永久停止或
                # 退避升级」所禁止的。归一后下轮只查询订单状态，等服务端签完自愈。
                #
                # 与 sslctl 的差异（刻意）：**不保留本轮的 pending key/CSR**。能走到
                # _submit_new_csr 说明本地认为无在途单（last_issue_state 空且无在途 CSR），
                # 服务端却说有——那服务端签的是更早的那个 CSR，本轮这把私钥必然与签出的
                # 证书不配对。留着它只会让 deployer 的配对校验抛 DeployError，把坑从签发侧
                # 挪到部署侧（转而烧部署额度，10 轮后 CAPPED(deploy)）。清掉后若订单真的
                # 签出，_handle_processing 会走「pending key 不存在」分支清空状态，
                # 下轮以新 CSR 重新提交，自然收敛
                self._persist_meta(order_id, {'last_issue_state': ISSUE_STATE_PROCESSING})
                cert_entry.setdefault('metadata', {})['last_issue_state'] = ISSUE_STATE_PROCESSING
                self._mark_no_progress(cert_entry, order_id)
                if self._logger:
                    self._logger.info(
                        "订单已在途（服务端签发进行中），归一 processing 等待签发完成: "
                        "order_id=%s, %s", order_id, str(e))
                return False
            # 认证/限流类失败被中间件拒在业务层之外，这次尝试事实上从未发生：回滚计数，
            # 否则一次限流风暴就能烧掉全部 10 次签发额度，把整批证书打成 CAPPED(issue)，
            # 而每一张都要人工 reset 才能恢复
            if e.auth_blocked and rollback:
                self._persist_meta(order_id, dict(rollback))
                meta = cert_entry.setdefault('metadata', {})
                meta.update(rollback)
                if self._logger:
                    self._logger.warning(
                        "CSR 提交被服务端拒于业务层之外（%s），已回滚签发计数: order_id=%s",
                        e.error_code, order_id)
            raise

        self._update_renew_before_days(api)
        order_id = self._check_order_update(cert_entry, cert_data)
        # pending / 已在处理 统一归一 processing（只查询、不重复提交、不增计数、不重生 CSR）
        status = _normalize_issue_status(cert_data.get('status', 'processing'))

        meta_update = {'last_issue_state': status}
        if status == ISSUE_STATE_PROCESSING:
            file_info = cert_data.get('file')
            if file_info and self._file_verifier:
                site_names = cert_entry.get('site_name', [])
                if isinstance(site_names, str):
                    site_names = [site_names] if site_names else []
                placed = self._file_verifier.place_file(file_info, site_names)
                meta_update['pending_file_verify'] = file_info
                meta_update['pending_verify_paths'] = placed
                # CSR 已提交无法撤回，但"验证文件没放上去"必须可见——否则订单会一直
                # 卡在 processing 等一个永远不会通过的 CA 验证
                incomplete = len(placed) != len(site_names)
                meta_update['verify_file_place_failed'] = incomplete
                if incomplete and self._logger:
                    self._logger.error(
                        "验证文件未能覆盖全部站点，CA 验证将失败: order_id=%s, 已放置 %d/%d",
                        order_id, len(placed), len(site_names))

        self._config.update_metadata(order_id, meta_update)
        # CSR 被服务端接受 = 一次真实进展，停更计时重新起算。不会因此变成无限循环：
        # 重新提交只可能经 _submit_new_csr，那条路径每次递增 issue_retry_count，10 次触顶
        self._clear_no_progress(cert_entry, order_id)
        if self._logger:
            self._logger.info("CSR 已提交，等待签发: status=%s", status)
        return False

    def _cleanup_verify_files(self, meta):
        """清理 metadata 中记录的验证文件"""
        if not self._file_verifier:
            return
        paths = meta.get('pending_verify_paths', [])
        if paths:
            self._file_verifier.cleanup_files(paths)

    @staticmethod
    def _verify_files_intact(meta, site_names):
        """已放置的验证文件是否仍然完整覆盖全部绑定站点

        三个条件缺一不可：
        - 列表非空——all([]) 恒为 True，而空列表正是"一次都没放上去"的症状
        - 覆盖全部站点——多站点里一个成功一个失败时列表非空但不完整，
          CA 可能恰好去验失败的那个
        - 文件仍在盘上——部署清理、站点重建、人工删除都会让它消失
        """
        paths = meta.get('pending_verify_paths', [])
        if not paths or len(paths) != len(site_names):
            return False
        return all(os.path.isfile(p) for p in paths)

    def _try_place_verify_file(self, cert_entry, cert_data):
        """检查 API 返回是否有新的验证文件需要放置"""
        if not self._file_verifier:
            return
        file_info = cert_data.get('file')
        if not file_info:
            return
        meta = cert_entry.get('metadata', {})
        old_file = meta.get('pending_file_verify', '')
        site_names = cert_entry.get('site_name', [])
        if isinstance(site_names, str):
            site_names = [site_names] if site_names else []
        # 去重判据是"上次是否真的放上去了"，不是"file_info 变没变"：
        # 首轮放置失败（站点清单查询异常等）时 pending_file_verify 照样落盘，
        # 之后每轮都被短路，文件一次都没写进去而订单永远卡在 processing
        if old_file and old_file == file_info and self._verify_files_intact(meta, site_names):
            return
        # 清理旧文件，放置新文件
        self._cleanup_verify_files(meta)
        site_names = cert_entry.get('site_name', [])
        if isinstance(site_names, str):
            site_names = [site_names] if site_names else []
        placed = self._file_verifier.place_file(file_info, site_names)
        self._config.update_metadata(cert_entry['order_id'], {
            'pending_file_verify': file_info,
            'pending_verify_paths': placed,
        })

    def _pending_key_path(self, cert_entry):
        cert_name = cert_entry.get('cert_name', 'order-%s' % cert_entry.get('order_id'))
        return os.path.join(self._data_dir, 'pending-keys', cert_name, 'pending-key.pem')

    def _save_pending_key(self, cert_entry, key_pem):
        path = self._pending_key_path(cert_entry)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # 先删残留再以 O_EXCL 创建：O_EXCL 对悬空符号链接同样报 FileExistsError，
        # 不清理会让该证书每轮续签都异常（与 _save_pending_csr 同一处理）
        try:
            if os.path.lexists(path):
                os.remove(path)
        except OSError:
            pass
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
            # lexists 而非 isfile：悬空符号链接也要清掉，否则下次 O_EXCL 创建失败
            if os.path.lexists(path):
                os.remove(path)
            # 清理空目录
            dir_path = os.path.dirname(path)
            if os.path.isdir(dir_path) and not os.listdir(dir_path):
                os.rmdir(dir_path)
        except OSError as e:
            if self._logger:
                self._logger.error("清理 pending key 失败: %s, error=%s", os.path.basename(path), str(e))

    # ==================== 在途 CSR 持久化（response-loss 恢复：作为在途标记，恢复走查询） ====================

    def _pending_csr_path(self, cert_entry):
        cert_name = cert_entry.get('cert_name', 'order-%s' % cert_entry.get('order_id'))
        return os.path.join(self._data_dir, 'pending-keys', cert_name, 'pending-csr.pem')

    def _has_pending_csr(self, cert_entry):
        """在途 CSR 存在（pending key + pending CSR 均在且非符号链接）= 上轮提交结果不确定，需恢复"""
        key_path = self._pending_key_path(cert_entry)
        csr_path = self._pending_csr_path(cert_entry)
        return (os.path.isfile(key_path) and not os.path.islink(key_path)
                and os.path.isfile(csr_path) and not os.path.islink(csr_path))

    def _read_pending_csr(self, cert_entry):
        path = self._pending_csr_path(cert_entry)
        if not os.path.isfile(path) or os.path.islink(path):
            return None
        with open(path, 'r') as f:
            return f.read()

    def _save_pending_csr(self, cert_entry, csr_pem):
        path = self._pending_csr_path(cert_entry)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # 先删残留再以 O_EXCL 创建，避免固定名被预置符号链接经 O_TRUNC 覆盖任意目标
        try:
            if os.path.lexists(path):
                os.remove(path)
        except OSError:
            pass
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, csr_pem.encode('utf-8'))
        finally:
            os.close(fd)

    def _cleanup_pending_csr(self, cert_entry):
        path = self._pending_csr_path(cert_entry)
        try:
            if os.path.lexists(path):
                os.remove(path)
            dir_path = os.path.dirname(path)
            if os.path.isdir(dir_path) and not os.listdir(dir_path):
                os.rmdir(dir_path)
        except OSError as e:
            if self._logger:
                self._logger.error("清理 pending CSR 失败: %s, error=%s", os.path.basename(path), str(e))

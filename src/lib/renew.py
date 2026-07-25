"""续签引擎。对标 sslctl pkg/certops/renew.go 状态机"""

import os
import json
import time
import random
import fcntl
import tempfile
from datetime import datetime, timezone

from . import cert_utils
from .api_client import APIError
from .deployer import DeployError, check_web_config, probe_panel_runtime
from .config import (
    MAX_ISSUE_RETRY_COUNT, MAX_DEPLOY_ATTEMPT_COUNT,
    ISSUE_STATE_PROCESSING, ISSUE_STATE_CAPPED, ISSUE_STATE_EXPIRED,
    TERMINAL_ISSUE_STATES, CAP_STAGE_ISSUE, CAP_STAGE_DEPLOY,
    RENEW_MODE_LOCAL, derive_or_validate_renew_policy,
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

# 服务端"已在处理"类状态统一归一为 processing（只查询、不重复提交、不增计数、不重生 CSR）：
# 提交响应只会是 pending / processing；查询在 processing → active 之间可能出现短暂中间态 approving
_PROCESSING_ALIASES = ('processing', 'pending', 'approving')


def _normalize_issue_status(status):
    """归一服务端状态：pending / approving / 已在处理 → processing（spec §2.6/§3.5）"""
    return ISSUE_STATE_PROCESSING if status in _PROCESSING_ALIASES else status


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
        if renew_mode == RENEW_MODE_LOCAL and state != ISSUE_STATE_PROCESSING \
                and meta.get('issue_retry_count', 0) >= MAX_ISSUE_RETRY_COUNT:
            self._enter_capped(cert, CAP_STAGE_ISSUE)
            _skip('capped:%s' % CAP_STAGE_ISSUE)
            return
        if meta.get('deploy_attempt_count', 0) >= MAX_DEPLOY_ATTEMPT_COUNT:
            self._enter_capped(cert, CAP_STAGE_DEPLOY)
            _skip('capped:%s' % CAP_STAGE_DEPLOY)
            return

        # 到期准入：已过期 → 转 EXPIRED 静默；剩余 < 安全余量 → 本轮不启动新动作
        hours = _remaining_hours(meta)
        if hours is not None:
            if hours <= 0:
                self._enter_expired(cert)
                _skip('expired')
                return
            if hours < SAFETY_MARGIN_HOURS:
                if self._logger:
                    self._logger.info(
                        "剩余有效期不足安全余量（%.1fh < %dh），本轮不启动新动作: order_id=%s",
                        hours, SAFETY_MARGIN_HOURS, order_id)
                _skip('safety_margin')
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

        # 阶段 3: 逐个续签。预处理失败的证书先计入，确保它们出现在汇总与面板里
        results = list(collect_failures)
        for idx, (cert, api, renew_mode) in enumerate(pending_list):
            order_id = cert['order_id']

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

        self._write_renew_status(results, skipped=skipped)
        return results

    def _write_renew_status(self, results, aborted_reason='', skipped=None):
        """写入最近一次续签运行的轻量状态（供面板展示），失败不影响续签

        复用数据目录，原子写 + 0600 权限，与 config/session 落盘约定一致。

        aborted_reason 非空表示本轮整体没跑（运行环境不可用等）：此时 last_run 虽是新鲜的，
        但面板不得据此判定健康——否则一台永久跑不动的机器会同时显示"最近续签正常"和错误告警。
        参数可选，既有直接调用方（含测试）不受影响。
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

    def _send_failure_callback(self, api, order_id, message):
        """发送 failure 部署回调（非关键路径，失败仅记日志）"""
        now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        try:
            api.callback(order_id=order_id, status='failure', deployed_at=now, message=message)
        except Exception as e:
            if self._logger:
                self._logger.warning("部署回调失败（非关键）: %s", str(e))

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
            'cap_stage': stage,
        })
        meta = cert_entry.setdefault('metadata', {})
        meta['last_issue_state'] = ISSUE_STATE_CAPPED
        meta['cap_stage'] = stage
        if self._logger:
            self._logger.warning("证书触顶静默（阶段=%s），等待人工处理: order_id=%s", stage, order_id)

    def _enter_expired(self, cert_entry):
        """过期：置 EXPIRED 静默终止，仅留本地日志与人工入口，不发回调"""
        order_id = cert_entry['order_id']
        self._persist_meta(order_id, {'last_issue_state': ISSUE_STATE_EXPIRED})
        cert_entry.setdefault('metadata', {})['last_issue_state'] = ISSUE_STATE_EXPIRED
        if self._logger:
            self._logger.warning("证书已过期，静默终止，等待人工处理: order_id=%s", order_id)

    # ==================== 部署编排（计数 + 回调收敛到编排层） ====================

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
        """环境恢复后清除阻断标记，避免面板一直显示已消失的旧原因"""
        meta = cert_entry.setdefault('metadata', {})
        if not meta.get('last_deploy_block_reason') and not meta.get('last_deploy_block_at'):
            return
        self._persist_meta(order_id, {'last_deploy_block_reason': '', 'last_deploy_block_at': ''})
        meta['last_deploy_block_reason'] = ''
        meta['last_deploy_block_at'] = ''
        if self._logger:
            self._logger.info("Web 服务配置已恢复，清除环境阻断标记: order_id=%s", order_id)

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
        if message != prev_reason:
            self._send_failure_callback(api, order_id, message)

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
        # 变化触发：服务端 DeployFailureReminderCommand 是电平驱动（判据为"订单最新一行仍为
        # failure"），一行就足以让订单永久留在失败视图，逐日重发零信息增量却会淹没管理端列表
        # 且被 PurgeCommand 的终态过滤永久保留。与本模块既有的"仅状态变化才记录/落盘"同一纪律
        if message != prev_reason:
            self._send_failure_callback(api, order_id, message)
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
        if status == 'success':
            stale = self._track_cert_unchanged(cert_entry, order_id, prev_serial)
            if stale:
                status, message = 'failure', stale

        self._send_deploy_callback(api, order_id, status, now, message, at_cap)
        return results

    def _track_cert_unchanged(self, cert_entry, order_id, prev_serial):
        """全部站点成功时比对序列号，判断服务端是否真的换了证书

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
        new_serial = meta.get('cert_serial', '')
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
        try:
            api.callback(order_id=order_id, status=status, deployed_at=deployed_at, message=message)
        except Exception as e:
            if self._logger:
                self._logger.warning("部署回调失败（非关键）: %s", str(e))

    def _track_order_status(self, cert_entry, order_id, status):
        """记录服务端返回的订单状态（展示专用，不参与任何门禁判定）

        pull 模式此前对非 active 一律 info 日志 + 返回 False，本轮被计为「等待签发」，
        面板显示「自动续签中」——一张已被取消的订单会这样每天轮询到过期为止。
        用 _persist_meta 而非 update_metadata：该值每轮都能从 API 重新推导，
        订单不存在时不该把良性 pending 变成失败。
        """
        meta = cert_entry.setdefault('metadata', {})
        if meta.get('last_order_status', '') == status:
            return
        self._persist_meta(order_id, {'last_order_status': status})
        meta['last_order_status'] = status

    def _renew_pull(self, cert_entry, api):
        """Pull 模式续签：查询订单 → 证书就绪则部署"""
        order_id = cert_entry['order_id']
        if self._logger:
            self._logger.info("Pull 模式续签: order_id=%s", order_id)

        cert_data = api.query_order(order_id)
        self._update_renew_before_days(api)
        order_id = self._check_order_update(cert_entry, cert_data)

        status = cert_data.get('status', '')
        certificate = cert_data.get('certificate', '')
        ca_certificate = cert_data.get('ca_certificate', '')
        private_key = cert_data.get('private_key', '')

        if status != 'active' or not certificate:
            # 记录服务端订单状态供面板展示。不写 last_issue_state——那是 spec 定义的
            # 带门禁语义的字段，把 cancelled 之类写进去会让证书被前置过滤永久跳过，
            # 违反「后续轮次仍可查询自愈」；renewed/reissued 更是链延续标记而非故障
            self._track_order_status(cert_entry, order_id, status)
            if self._logger:
                self._logger.info("证书未就绪: status=%s", status)
            return False

        self._track_order_status(cert_entry, order_id, '')

        if not ca_certificate:
            if self._logger:
                self._logger.warning("缺少中间证书，等待下次检查")
            return False

        # 构建完整证书链并部署
        fullchain = cert_utils.build_fullchain(certificate, ca_certificate)
        site_names = cert_entry.get('site_name', [])
        if isinstance(site_names, str):
            site_names = [site_names] if site_names else []
        domains = cert_entry.get('domains', [])

        if not site_names:
            # 服务端已签发 active 证书，客户端却没有部署目标：复用既有阻断机制记录原因
            # （已持久化、已在面板渲染成橙色告警、环境恢复时自动清除），并按失败上报一次。
            # 此前只记 warning 并返回 False，本轮被计为「等待签发」，服务端零回调，
            # 面板对已有到期时间的证书显示「正常」——两侧都看不出证书压根没部署上
            self._report_block(cert_entry, api, order_id, '证书已签发但未绑定任何站点，无法部署')
            raise RuntimeError('未绑定站点，无法部署')

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

        if last_state == ISSUE_STATE_PROCESSING:
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
            self._enter_capped(cert_entry, CAP_STAGE_ISSUE)
            return False

        return self._submit_new_csr(cert_entry, api)

    def _recover_pending_submit(self, cert_entry, api):
        """上轮 CSR 提交结果不确定：查询订单，据服务端实际状态恢复，绝不重复 POST。

        服务端状态机保证：提交成功响应只会是 pending/processing（均表示 CSR 已收到）；
        服务端未接收提交时返回错误信息（走明确业务拒绝路径清理 pending），不会以状态表达。
        因此恢复只需查询：
        - pending/processing/approving/active → 归一 processing 走查询路径
          （只 GET、不增计数、不重生 CSR）；active 则读 pending key 部署
        - 其他状态为 active 之后的订单终态 → 持久化后停止，等待人工处理
        """
        order_id = cert_entry['order_id']
        cert_data = api.query_order(order_id)
        self._update_renew_before_days(api)
        order_id = self._check_order_update(cert_entry, cert_data)
        status = _normalize_issue_status(cert_data.get('status', ''))

        if status in (ISSUE_STATE_PROCESSING, 'active'):
            # 已在处理/已签发：归一 processing，交由 processing 路径查询/部署
            self._config.update_metadata(order_id, {'last_issue_state': ISSUE_STATE_PROCESSING})
            cert_entry.setdefault('metadata', {})['last_issue_state'] = ISSUE_STATE_PROCESSING
            if self._logger:
                self._logger.info("在途 CSR 恢复：服务端已在处理，归一 processing: order_id=%s", order_id)
            return self._handle_processing(cert_entry, api)

        # 订单终态：持久化实际状态后停止，等待人工处理；仅状态变化时记 error，避免每轮重复刷日志
        meta = cert_entry.setdefault('metadata', {})
        if meta.get('last_issue_state', '') != status:
            self._config.update_metadata(order_id, {'last_issue_state': status})
            meta['last_issue_state'] = status
            if self._logger:
                self._logger.error("在途 CSR 查询到订单终态，停止自动提交等待人工处理: order_id=%s, status=%s",
                                   order_id, status)
        elif self._logger:
            self._logger.info("订单仍处于终态 %s，等待人工处理: order_id=%s", status, order_id)
        return False

    def _refresh_and_maybe_renew_local(self, cert_entry, api):
        """Local 模式到期时间未知：查询 API 回填元数据后再按正常续签逻辑判定

        - 查询失败：向上抛出（本轮按失败处理），不盲目提交 CSR
        - 服务端未返回证书内容 / 证书解析失败：本轮跳过（返回 False），下轮再试
        - 回填成功后：仍需续签（临期/已过期）→ 提交新 CSR；否则本轮不续签
        """
        order_id = cert_entry['order_id']
        cert_data = api.query_order(order_id)
        self._update_renew_before_days(api)
        order_id = self._check_order_update(cert_entry, cert_data)

        certificate = cert_data.get('certificate', '')
        if not certificate:
            if self._logger:
                self._logger.warning(
                    "证书到期时间未知且服务端未返回证书内容，本轮跳过: order_id=%s, status=%s",
                    order_id, cert_data.get('status', ''))
            return False

        cert_info = cert_utils.parse_cert_info(
            certificate, logger=self._logger)
        if not cert_info or not cert_info.get('not_after'):
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
            if self._logger:
                self._logger.info("回填后剩余期限充足，本轮不续签: order_id=%s", order_id)
            return False

        return self._submit_new_csr(cert_entry, api)

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

    def _handle_processing(self, cert_entry, api):
        """处理已提交 CSR 的 processing 状态"""
        order_id = cert_entry['order_id']
        meta = cert_entry.get('metadata', {})

        # 查询订单状态（只 GET，不重复 POST，不增计数）
        cert_data = api.query_order(order_id)
        self._update_renew_before_days(api)
        order_id = self._check_order_update(cert_entry, cert_data)
        # pending / 已在处理 统一归一为 processing 继续等待（spec §2.6/§3.5）
        status = _normalize_issue_status(cert_data.get('status', ''))

        if status == ISSUE_STATE_PROCESSING:
            # 检查是否有新的验证文件需要放置
            self._try_place_verify_file(cert_entry, cert_data)
            if self._logger:
                self._logger.info("证书仍在处理中，继续等待")
            return False

        if status != 'active':
            # 仅在状态发生变化时记 error 并落盘，避免每轮 cron 重复刷日志/重复写文件
            if meta.get('last_issue_state', '') != status:
                if self._logger:
                    self._logger.error("证书状态异常: %s，持久化实际状态并等待人工处理", status)
                self._cleanup_verify_files(meta)
                self._config.update_metadata(order_id, {
                    'last_issue_state': status,
                    'pending_file_verify': '',
                    'pending_verify_paths': [],
                })
                meta['last_issue_state'] = status
                meta['pending_file_verify'] = ''
                meta['pending_verify_paths'] = []
            elif self._logger:
                self._logger.info("订单仍处于异常状态 %s，等待人工处理: order_id=%s", status, order_id)
            return False

        # 证书已签发，清理验证文件
        self._cleanup_verify_files(meta)

        # 读取 pending key 并部署
        certificate = cert_data.get('certificate', '')
        ca_certificate = cert_data.get('ca_certificate', '')

        if not certificate or not ca_certificate:
            if self._logger:
                self._logger.warning("证书内容不完整")
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

        results = self._deploy_and_report(
            cert_entry, api, fullchain, pending_key, site_names, domains, order_id)

        # 清理判据 = 私钥是否已被消费（任一站点部署成功即已写入站点），与
        # _check_deploy_results 是否抛错解耦：部分成功+站点缺失/删除时仍抛错上报，
        # 但私钥必须清理不泄漏；全失败（success=0）保留 pending key 供下轮重试（spec §3.8）
        if any(r.get('status') for r in results):
            self._cleanup_pending_key(cert_entry)
            self._cleanup_pending_csr(cert_entry)
        return self._check_deploy_results(results, order_id)

    def _submit_new_csr(self, cert_entry, api):
        """生成并提交新的 CSR（一次新的签发逻辑尝试，递增计数）"""
        order_id = cert_entry['order_id']
        domains = cert_entry.get('domains', [])

        if not domains:
            raise RuntimeError("未配置域名")

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
        meta = cert_entry.setdefault('metadata', {})
        retry_count = meta.get('issue_retry_count', 0) + 1
        now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        self._config.update_metadata(order_id, {
            'issue_retry_count': retry_count,
            'last_csr_hash': csr_hash,
            'csr_submitted_at': now,
        })
        meta['issue_retry_count'] = retry_count
        meta['last_csr_hash'] = csr_hash

        return self._do_submit_csr(cert_entry, api, csr_pem, validation_method)

    def _do_submit_csr(self, cert_entry, api, csr_pem, validation_method):
        """执行 CSR 提交并处理响应。

        - 传输不确定（超时/断连/解析失败）：保留 pending key + CSR 作为在途标记，
          下轮查询订单状态恢复（不重复 POST），返回 False
        - 明确业务拒绝（含服务端未接收提交）：确认未创建新证书，清理 pending key + CSR，抛出
        - 成功：pending / 已在处理 归一 processing，放置验证文件，标记 processing
        """
        order_id = cert_entry['order_id']
        domains = cert_entry.get('domains', [])
        try:
            cert_data = api.submit_csr(order_id, csr_pem, domains,
                                       validation_method=validation_method)
        except APIError as e:
            if getattr(e, 'transport', False):
                # 不确定结果：保留 pending key + CSR，下轮以同一 CSR 恢复，不重生不增计数
                if self._logger:
                    self._logger.warning("CSR 提交结果不确定（%s），保留 pending 待下轮恢复: order_id=%s",
                                         str(e), order_id)
                return False
            # 明确业务拒绝：确认未创建新证书，清理 pending
            self._cleanup_pending_key(cert_entry)
            self._cleanup_pending_csr(cert_entry)
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

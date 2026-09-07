#!/usr/bin/env python3
"""按变更选择本地检查；耗时记录只用于改进规则，不作为通过缓存。"""

import argparse
import json
import shlex
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT).decode('utf-8', 'surrogateescape')


def changed_files(base=None):
    # 分开取暂存/未暂存，避免两处相反的修改相互抵消；重命名保留旧、新路径。
    comparisons = [('HEAD',), ('--cached', 'HEAD'), ()]
    if base:
        commit = git('rev-parse', '--verify', '--end-of-options', base + '^{commit}').strip()
        ancestor = git('merge-base', commit, 'HEAD').strip()
        comparisons.append((ancestor, 'HEAD'))
    paths = set()
    for comparison in comparisons:
        paths.update(filter(None, git('diff', '--name-only', '--no-renames', '-z', *comparison, '--').split('\0')))
    paths.update(filter(None, git('ls-files', '--others', '--exclude-standard', '-z').split('\0')))
    return sorted(paths)


def select_scope(paths, full=False, tests=()):
    if full or not paths:
        return 'full', [], '显式全量或没有可判定的变更范围'
    selected = set(tests)
    runtime = False
    for name in paths:
        path = ROOT / name
        # 文档不改变产物；部署规范仍会改变跨仓行为，必须保守升级。
        if name != 'deploy-spec.md' and name.endswith('.md') and not name.startswith(('src/', 'deploy/', 'build/')):
            continue
        if name == 'scripts/check-agent-config.py':
            selected.add('tests/test_agent_config.py')
        elif name.startswith('tests/test_') and name.endswith('.py') and path.is_file():
            selected.add(name)
        elif name.startswith('src/') and name.endswith(('.py', '.html')):
            runtime = True
        else:
            return 'full', [], '构建、安装、共享测试设施、规范或未分类路径: ' + name
    if runtime and not tests:
        return 'runtime', sorted(selected), '插件变更：运行时回归；不重复测试未涉及的发布事务和打包'
    if selected:
        return 'focused', sorted(selected), '定向测试；运行时缩小范围须先审查直接调用链'
    return 'docs', [], '仅文档或智能体说明'


def commands_for(scope, tests, base=None):
    commands = [[PYTHON, 'scripts/check-agent-config.py']]
    if scope != 'docs':
        pytest = [PYTHON, '-m', 'pytest', '-q', '-W', 'error', '--durations=5']
        if scope == 'focused':
            pytest.extend(tests)
        else:
            pytest.append('tests/')
            if scope == 'runtime':
                for release_test in ('tests/test_release_script.py', 'tests/test_release_manifest.py'):
                    if release_test not in tests:
                        pytest.append('--ignore=' + release_test)
        commands.append(pytest)
        commands.append([PYTHON, '-m', 'flake8', 'src/', 'tests/', 'scripts/finish-check.py', '--max-line-length=120',
                         '--exclude=__pycache__'])
    if scope == 'full':
        commands.append(['bash', 'build/release.sh', '--dry-run', '0.0.0-finish-check'])
    commands.extend([['git', 'diff', '--check'], ['git', 'diff', '--cached', '--check']])
    if base:
        commit = git('rev-parse', '--verify', '--end-of-options', base + '^{commit}').strip()
        commands.append(['git', 'diff', '--check', git('merge-base', commit, 'HEAD').strip(), 'HEAD', '--'])
    return commands


def run_commands(commands):
    results = []
    for command in commands:
        print('+ ' + shlex.join(command), flush=True)
        start = time.monotonic()
        try:
            code = subprocess.run(command, cwd=ROOT).returncode
        except OSError as error:
            print(str(error), file=sys.stderr)
            code = 127
        except KeyboardInterrupt:
            code = 130
        results.append({'command': command, 'seconds': round(time.monotonic() - start, 3), 'exit_code': code})
        if code:
            return results, code
    return results, 0


def save_report(report):
    directory = ROOT / '.superpowers' / 'finish-check'
    directory.mkdir(parents=True, exist_ok=True)
    # 每轮单独文件，避免并发覆盖；只记命令和结果，不收集日志、环境变量或业务数据。
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', prefix='run-', suffix='.json',
                                     dir=directory, delete=False) as output:
        json.dump(report, output, ensure_ascii=True, indent=2)
        return Path(output.name)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', help='比较 merge-base(base, HEAD)..HEAD，并包含当前工作区')
    parser.add_argument('--full', action='store_true', help='全量本地门禁；CI 模拟和正式发布必须使用')
    parser.add_argument('--plan', action='store_true', help='仅打印范围和命令，不执行、不记录')
    parser.add_argument('--tests', nargs='+', default=[], help='局部低风险变更经调用链审查后选用的测试文件或 nodeid')
    args = parser.parse_args(argv)
    for test in args.tests:
        path = (ROOT / test.split('::', 1)[0]).resolve()
        if ROOT / 'tests' not in path.parents or not path.is_file() or path.suffix != '.py':
            parser.error('--tests 必须指向本仓 tests/ 下存在的 Python 测试: ' + test)
    try:
        paths = changed_files(args.base)
        scope, tests, reason = select_scope(paths, args.full, args.tests)
        if args.tests and scope != 'focused':
            parser.error('当前范围要求 ' + scope + '，不能用 --tests 缩小；移除此参数后重跑')
        commands = commands_for(scope, tests, args.base)
        head = git('rev-parse', 'HEAD').strip()
    except subprocess.CalledProcessError:
        print('无法确定 Git 范围；修正 base/仓库状态后重跑，不能按文档检查降级。', file=sys.stderr)
        return 2
    print('finish-check 范围: ' + scope + '；' + reason, flush=True)
    print('变更文件: ' + ', '.join(paths), flush=True)
    if args.plan:
        for command in commands:
            print(shlex.join(command))
        return 0
    results, code = run_commands(commands)
    report = {'created_at': datetime.now(timezone.utc).isoformat(), 'head': head, 'base': args.base,
              'scope': scope, 'reason': reason, 'files': paths, 'planned_commands': commands,
              'results': results, 'exit_code': code}
    try:
        output = save_report(report)
        print('检查记录: ' + str(output))
    except OSError as error:
        print('检查记录写入失败: ' + str(error), file=sys.stderr)
        return code or 1
    print('本地自动检查' + ('通过' if code == 0 else '失败') + '；范围=' + scope)
    return code if code >= 0 else 1


if __name__ == '__main__':
    sys.exit(main())

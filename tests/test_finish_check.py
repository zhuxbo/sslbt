"""验证精简检查不会丢失变更、吞掉失败或绕过全量边界。"""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location('finish_check', ROOT / 'scripts' / 'finish-check.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


@pytest.mark.parametrize('paths, expected', [
    (['README.md'], 'docs'),
    (['AGENTS.md', 'skills/finish-check.md', '.agents/skills/finish-check/SKILL.md'], 'docs'),
    (['scripts/check-agent-config.py'], 'focused'),
    (['tests/test_logger.py'], 'focused'),
    (['src/lib/logger.py', 'README.md'], 'runtime'),
    (['src/index.html'], 'runtime'),
    (['src/lib/config.py', 'src/lib/renew.py'], 'runtime'),
    (['src/install.sh'], 'full'),
    (['scripts/build.sh'], 'full'),
    (['scripts/finish-check.py'], 'full'),
    (['Makefile'], 'full'),
    (['.github/workflows/ci.yml'], 'full'),
    (['tests/conftest.py'], 'full'),
    (['tests/mock_bt/public.py'], 'full'),
    (['tests/test_deleted.py'], 'full'),
    (['deploy-spec.md'], 'full'),
    (['new-unknown-file'], 'full'),
    ([], 'full'),
])
def test_automatic_scope(paths, expected):
    assert MODULE.select_scope(paths)[0] == expected


def test_explicit_full_wins_over_docs_and_focused_tests():
    assert MODULE.select_scope(['README.md'], full=True)[0] == 'full'
    assert MODULE.select_scope(['Makefile'], tests=['tests/test_logger.py'])[0] == 'full'


def test_focused_tests_include_changed_tests():
    scope, tests, _ = MODULE.select_scope(
        ['src/lib/logger.py', 'tests/test_main.py'], tests=['tests/test_logger.py'])
    assert scope == 'focused'
    assert tests == ['tests/test_logger.py', 'tests/test_main.py']


def test_runtime_keeps_changed_release_tests():
    scope, tests, _ = MODULE.select_scope(['src/lib/logger.py', 'tests/test_release_script.py'])
    commands = MODULE.commands_for(scope, tests)
    command = next(command for command in commands if 'pytest' in command)
    assert '--ignore=tests/test_release_script.py' not in command
    assert '--ignore=tests/test_release_manifest.py' in command


def test_docs_avoids_pytest_lint_and_build():
    commands = MODULE.commands_for('docs', [])
    assert all('pytest' not in command and 'flake8' not in command and 'bash' not in command for command in commands)
    assert ['git', 'diff', '--cached', '--check'] in commands


def test_full_retains_tests_lint_governance_and_one_build():
    commands = MODULE.commands_for('full', [])
    assert sum('build/release.sh' in command for command in commands) == 1
    assert any('scripts/check-agent-config.py' in command for command in commands)
    assert any('flake8' in command for command in commands)
    command = next(command for command in commands if 'pytest' in command)
    assert 'tests/' in command and command[command.index('-W') + 1] == 'error'
    assert not any(arg.startswith('--ignore') for arg in command)


@pytest.fixture
def repository(tmp_path, monkeypatch):
    monkeypatch.setattr(MODULE, 'ROOT', tmp_path)
    MODULE.git('init', '-q')
    MODULE.git('config', 'user.name', 'Test')
    MODULE.git('config', 'user.email', 'test@example.com')
    (tmp_path / 'README.md').write_text('baseline\n')
    MODULE.git('add', 'README.md')
    MODULE.git('commit', '-qm', 'test: baseline')
    return tmp_path


def test_git_includes_staged_unstaged_untracked_and_rename_sources(repository):
    MODULE.git('mv', 'README.md', 'renamed file.md')
    (repository / 'renamed file.md').write_text('working change\n')
    (repository / 'new file.py').write_text('')
    assert MODULE.changed_files() == ['README.md', 'new file.py', 'renamed file.md']


def test_git_keeps_opposing_staged_and_worktree_changes(repository):
    (repository / 'README.md').write_text('staged\n')
    MODULE.git('add', 'README.md')
    (repository / 'README.md').write_text('baseline\n')
    assert MODULE.changed_files() == ['README.md']


def test_base_adds_committed_changes_and_rejects_invalid_ref(repository):
    base = MODULE.git('rev-parse', 'HEAD').strip()
    (repository / 'committed.py').write_text('')
    MODULE.git('add', 'committed.py')
    MODULE.git('commit', '-qm', 'test: change')
    (repository / 'untracked.py').write_text('')
    assert MODULE.changed_files(base) == ['committed.py', 'untracked.py']
    commands = MODULE.commands_for('full', [], base)
    assert ['git', 'diff', '--check', base, 'HEAD', '--'] in commands
    with pytest.raises(subprocess.CalledProcessError):
        MODULE.changed_files('--not-a-ref')


def test_failure_stops_and_preserves_exit_status(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 5)

    monkeypatch.setattr(MODULE.subprocess, 'run', run)
    results, code = MODULE.run_commands([['first'], ['must-not-run']])
    assert calls == [['first']]
    assert code == 5 and results[0]['exit_code'] == 5


def test_reports_do_not_overwrite_or_capture_business_output(tmp_path, monkeypatch):
    monkeypatch.setattr(MODULE, 'ROOT', tmp_path)
    report = {'scope': 'docs', 'results': [], 'exit_code': 0}
    first = MODULE.save_report(report)
    second = MODULE.save_report(report)
    assert first != second
    assert json.loads(first.read_text()) == report
    assert first.stat().st_mode & 0o777 == 0o600


def test_plan_does_not_execute_or_save_report(repository, monkeypatch):
    def forbidden(*args):
        pytest.fail('plan must not execute or save')

    monkeypatch.setattr(MODULE, 'run_commands', forbidden)
    monkeypatch.setattr(MODULE, 'save_report', forbidden)
    assert MODULE.main(['--plan']) == 0


def test_full_scope_rejects_narrowing(repository):
    (repository / 'tests').mkdir()
    (repository / 'tests/test_example.py').write_text('')
    (repository / 'Makefile').write_text('')
    with pytest.raises(SystemExit) as result:
        MODULE.main(['--tests', 'tests/test_example.py'])
    assert result.value.code == 2

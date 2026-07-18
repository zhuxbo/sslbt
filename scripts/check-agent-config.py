#!/usr/bin/env python3
"""Deterministic repository guard for deploy-spec section 12."""

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / 'skills'

EXPECTED_CLAUDE = """# 项目智能体规则

@AGENTS.md

本文件仅为 Claude 兼容入口。禁止在此追加项目规则；需要调整时修改 `AGENTS.md` 或其引用的权威资料。
"""

EXPECTED_COMMANDS = {
    'build-release.md': """读取并严格遵循 `skills/build-release.md`。

将用户参数 `$ARGUMENTS` 原样作为构建版本或 bundle 参数传入。
""",
    'finish-check.md': """读取并严格遵循 `skills/finish-check.md`。

将用户参数 `$ARGUMENTS` 原样作为检查范围或附加要求传入。
""",
    'remote-release.md': """读取并严格遵循 `skills/remote-release.md`。

将用户参数 `$ARGUMENTS` 原样作为发布流程参数传入。
""",
}


def fail(errors, message):
    errors.append(message)


def main():
    errors = []
    if (ROOT / 'CLAUDE.md').read_text(encoding='utf-8') != EXPECTED_CLAUDE:
        fail(errors, 'CLAUDE.md 不符合固定薄模板')

    router_path = SKILLS / 'SKILL.md'
    router = router_path.read_text(encoding='utf-8')
    if not router.startswith('---\nname:'):
        fail(errors, 'skills/SKILL.md 缺少入口元数据')

    leaf_names = set(re.findall(r'`skills/([a-z0-9]+(?:-[a-z0-9]+)*\.md)`', router))
    actual_leaf_names = {path.name for path in SKILLS.glob('*.md') if path.name != 'SKILL.md'}
    if leaf_names != actual_leaf_names:
        fail(errors, 'skills/SKILL.md 路由与叶子文件集合不一致')

    nested = [path for path in SKILLS.rglob('*') if path.is_dir()]
    if nested:
        fail(errors, 'skills/ 下禁止二级目录: ' + ', '.join(str(path.relative_to(ROOT)) for path in nested))
    for name in actual_leaf_names:
        if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*\.md', name):
            fail(errors, 'Skill 叶子文件名不是 kebab-case: ' + name)

    command_dir = ROOT / '.claude' / 'commands'
    actual_commands = {path.name for path in command_dir.glob('*.md')}
    if actual_commands != set(EXPECTED_COMMANDS):
        fail(errors, 'Claude 薄工具入口集合发生漂移')
    for name, expected in EXPECTED_COMMANDS.items():
        path = command_dir / name
        if path.exists() and path.read_text(encoding='utf-8') != expected:
            fail(errors, str(path.relative_to(ROOT)) + ' 不符合固定薄模板')

    old_reference = re.compile(r'skills/[a-z0-9-]+/SKILL\.md')
    for path in ROOT.rglob('*.md'):
        if '.git' in path.parts or '.superpowers' in path.parts or path == ROOT / 'deploy-spec.md':
            continue
        if old_reference.search(path.read_text(encoding='utf-8')):
            fail(errors, str(path.relative_to(ROOT)) + ' 仍引用旧二级 Skill 路径')

    if errors:
        for error in errors:
            print('ERROR:', error, file=sys.stderr)
        return 1
    print('智能体配置与 Skill 结构防漂移检查通过')
    return 0


if __name__ == '__main__':
    sys.exit(main())

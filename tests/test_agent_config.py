import importlib.util
from pathlib import Path


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / 'scripts' / 'check-agent-config.py'
SPEC = importlib.util.spec_from_file_location('check_agent_config', SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_router_metadata_rejects_blank_values():
    for router in ('---\nname:   \ndescription: ok\n---\n', '---\nname: ok\ndescription:   \n---\n'):
        errors = []
        MODULE.parse_router_metadata(router, errors)
        assert errors

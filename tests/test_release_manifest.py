import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).parents[1] / 'scripts' / 'release_manifest.py'
SPEC = importlib.util.spec_from_file_location('release_manifest', SCRIPT)
release_manifest = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_manifest)


def create_bundle(tmp_path, version='1.2.3', channel='main', dirty=False):
    bundle = tmp_path / version
    assets = bundle / 'assets'
    assets.mkdir(parents=True)
    asset = assets / 'sslbt.zip'
    asset.write_bytes(b'one-build-only')
    bootstrap_dir = bundle / 'bootstrap'
    bootstrap_dir.mkdir()
    bootstrap = bootstrap_dir / 'install.sh'
    bootstrap.write_bytes(b'#!/bin/sh\n')
    manifest = bundle / 'manifest.json'
    release_manifest.create_manifest(SimpleNamespace(
        version=version,
        channel=channel,
        commit='a' * 40,
        dirty=dirty,
        epoch=1_700_000_000,
        asset=str(asset),
        bootstrap=str(bootstrap),
        output=str(manifest),
    ))
    return bundle, manifest


def test_manifest_binds_single_asset_and_verifies(tmp_path):
    bundle, manifest_path = create_bundle(tmp_path)
    manifest = release_manifest.verify_manifest(str(manifest_path), str(bundle))
    assert manifest['assets'][0]['name'] == 'sslbt.zip'
    assert manifest['github_only_assets'] == []


def test_main_rejects_dirty_bundle(tmp_path):
    with pytest.raises(ValueError, match='脏工作区'):
        create_bundle(tmp_path, dirty=True)


def test_dev_index_can_replace_same_version(tmp_path):
    bundle, manifest = create_bundle(tmp_path, '1.2.4-rc.1', 'dev', True)
    output = tmp_path / 'releases.json'
    args = SimpleNamespace(base=None, manifest=str(manifest), bundle_dir=str(bundle), output=str(output), keep=5)
    release_manifest.create_index(args)
    release_manifest.create_index(SimpleNamespace(**{**vars(args), 'base': str(output)}))
    data = json.loads(output.read_text())
    assert data['dev']['latest'] == '1.2.4-rc.1'
    assert len(data['dev']['versions']) == 1
    assert data['dev']['versions'][0]['dirty'] is True


def test_main_index_is_immutable_and_must_increase(tmp_path):
    bundle, manifest = create_bundle(tmp_path)
    output = tmp_path / 'releases.json'
    args = SimpleNamespace(base=None, manifest=str(manifest), bundle_dir=str(bundle), output=str(output), keep=5)
    release_manifest.create_index(args)
    with pytest.raises(ValueError, match='禁止覆盖'):
        release_manifest.create_index(SimpleNamespace(**{**vars(args), 'base': str(output)}))

    older_bundle, older_manifest = create_bundle(tmp_path, '1.2.2')
    with pytest.raises(ValueError, match='高于'):
        release_manifest.create_index(SimpleNamespace(
            base=str(output), manifest=str(older_manifest), bundle_dir=str(older_bundle),
            output=str(tmp_path / 'older.json'), keep=5,
        ))


def test_tampered_asset_fails_verification(tmp_path):
    bundle, manifest = create_bundle(tmp_path)
    (bundle / 'assets' / 'sslbt.zip').write_bytes(b'tampered')
    with pytest.raises(ValueError, match='不一致'):
        release_manifest.verify_manifest(str(manifest), str(bundle))

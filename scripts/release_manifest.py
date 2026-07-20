#!/usr/bin/env python3
"""Create and verify sslbt release bundles and releases.json candidates."""

import argparse
import datetime
import hashlib
import json
import os
import re
import sys


SEMVER_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-((?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
ASSET_NAME = "sslbt.zip"


def parse_version(version):
    version = version.removeprefix("v")
    match = SEMVER_RE.fullmatch(version)
    if not match:
        raise ValueError("版本号必须是完整 SemVer")
    core = tuple(int(value) for value in match.groups()[:3])
    prerelease = match.group(4)
    return version, core, prerelease


def compare_versions(left, right):
    _, left_core, left_pre = parse_version(left)
    _, right_core, right_pre = parse_version(right)
    if left_core != right_core:
        return (left_core > right_core) - (left_core < right_core)
    if left_pre is None or right_pre is None:
        return (left_pre is None) - (right_pre is None)
    left_parts = left_pre.split(".")
    right_parts = right_pre.split(".")
    for left_part, right_part in zip(left_parts, right_parts):
        if left_part == right_part:
            continue
        left_num = left_part.isdigit()
        right_num = right_part.isdigit()
        if left_num and right_num:
            return (int(left_part) > int(right_part)) - (int(left_part) < int(right_part))
        if left_num != right_num:
            return -1 if left_num else 1
        return (left_part > right_part) - (left_part < right_part)
    return (len(left_parts) > len(right_parts)) - (len(left_parts) < len(right_parts))


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def timestamp_from_epoch(epoch):
    return datetime.datetime.fromtimestamp(int(epoch), datetime.timezone.utc).replace(microsecond=0).isoformat()


def write_json(path, data):
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, path)


def load_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def create_manifest(args):
    version, _, prerelease = parse_version(args.version)
    expected_channel = "dev" if prerelease else "main"
    if args.channel != expected_channel:
        raise ValueError("稳定版只能进入 main，预发布版只能进入 dev")
    if args.channel == "main" and args.dirty:
        raise ValueError("main 正式 bundle 不允许来自脏工作区")
    asset_path = os.path.abspath(args.asset)
    manifest = {
        "schema_version": 1,
        "product": "sslbt",
        "version": version,
        "channel": args.channel,
        "source_commit": args.commit,
        "dirty": bool(args.dirty),
        "created_at": timestamp_from_epoch(args.epoch),
        "assets": [{
            "name": ASSET_NAME,
            "path": "assets/" + ASSET_NAME,
            "size": os.path.getsize(asset_path),
            "sha256": sha256_file(asset_path),
        }],
        "bootstrap": {
            "name": "install.sh",
            "path": "bootstrap/install.sh",
            "size": os.path.getsize(args.bootstrap),
            "sha256": sha256_file(args.bootstrap),
        },
        "integrity": {"type": "sha256", "detached_signature": False},
        "github_only_assets": [],
    }
    write_json(args.output, manifest)


def verify_manifest(manifest_path, bundle_dir):
    manifest = load_json(manifest_path)
    if manifest.get("schema_version") != 1 or manifest.get("product") != "sslbt":
        raise ValueError("不支持的发布 manifest")
    version, _, prerelease = parse_version(manifest.get("version", ""))
    expected_channel = "dev" if prerelease else "main"
    if manifest.get("channel") != expected_channel:
        raise ValueError("manifest 通道与版本类型不符")
    if expected_channel == "main" and manifest.get("dirty") is not False:
        raise ValueError("main manifest 必须声明 dirty=false")
    assets = manifest.get("assets")
    if not isinstance(assets, list) or len(assets) != 1 or assets[0].get("name") != ASSET_NAME:
        raise ValueError("规范正式资产集合必须且只能包含 sslbt.zip")
    asset = assets[0]
    expected_path = "assets/" + ASSET_NAME
    if asset.get("path") != expected_path:
        raise ValueError("manifest 资产路径不合法")
    asset_path = os.path.realpath(os.path.join(bundle_dir, expected_path))
    bundle_root = os.path.realpath(bundle_dir) + os.sep
    if not asset_path.startswith(bundle_root) or not os.path.isfile(asset_path):
        raise ValueError("bundle 缺少 sslbt.zip")
    if os.path.getsize(asset_path) != asset.get("size") or sha256_file(asset_path) != asset.get("sha256"):
        raise ValueError("bundle 资产与 manifest 不一致")
    bootstrap = manifest.get("bootstrap", {})
    bootstrap_path = os.path.realpath(os.path.join(bundle_dir, "bootstrap/install.sh"))
    if bootstrap.get("name") != "install.sh" or bootstrap.get("path") != "bootstrap/install.sh":
        raise ValueError("manifest 引导文件定义无效")
    if not bootstrap_path.startswith(bundle_root) or not os.path.isfile(bootstrap_path):
        raise ValueError("bundle 缺少 install.sh")
    if (os.path.getsize(bootstrap_path) != bootstrap.get("size")
            or sha256_file(bootstrap_path) != bootstrap.get("sha256")):
        raise ValueError("bundle 引导文件与 manifest 不一致")
    return manifest


def command_verify(args):
    verify_manifest(args.manifest, args.bundle_dir)


def normalized_index(data):
    if not isinstance(data, dict):
        raise ValueError("releases.json 顶层必须是对象")
    result = dict(data)
    for channel in ("main", "dev"):
        value = result.get(channel, {"latest": "", "versions": []})
        if not isinstance(value, dict) or not isinstance(value.get("versions", []), list):
            raise ValueError("releases.json 通道结构无效")
        result[channel] = value
    return result


def create_index(args):
    manifest = verify_manifest(args.manifest, args.bundle_dir)
    if args.base and os.path.exists(args.base) and os.path.getsize(args.base):
        data = normalized_index(load_json(args.base))
    else:
        data = normalized_index({})
    channel = manifest["channel"]
    version = manifest["version"]
    channel_data = data[channel]
    versions = channel_data.get("versions", [])
    existing = [entry for entry in versions if entry.get("version") == version]
    latest = channel_data.get("latest", "")
    if channel == "main":
        if existing:
            raise ValueError("main 正式版本已存在，禁止覆盖")
        if latest and compare_versions(version, latest) <= 0:
            raise ValueError("main 正式版本必须高于当前 latest")
    entry = {
        "version": version,
        "released_at": manifest["created_at"][:10],
        "checksums": {ASSET_NAME: "sha256:" + manifest["assets"][0]["sha256"]},
        "source_commit": manifest["source_commit"],
        "dirty": manifest["dirty"],
    }
    versions = [item for item in versions if item.get("version") != version]
    versions.insert(0, entry)
    channel_data["latest"] = version
    channel_data["versions"] = versions[: args.keep]
    write_json(args.output, data)


def check_index(index_path, manifest_path, bundle_dir):
    manifest = verify_manifest(manifest_path, bundle_dir)
    data = normalized_index(load_json(index_path))
    channel = data[manifest["channel"]]
    if channel.get("latest") != manifest["version"]:
        raise ValueError("latest 未指向 manifest 版本")
    matching = [entry for entry in channel.get("versions", []) if entry.get("version") == manifest["version"]]
    if len(matching) != 1:
        raise ValueError("索引中 manifest 版本不唯一")
    checksum = "sha256:" + manifest["assets"][0]["sha256"]
    if matching[0].get("checksums") != {ASSET_NAME: checksum}:
        raise ValueError("索引 checksums 与 manifest 不一致")
    if matching[0].get("source_commit") != manifest["source_commit"]:
        raise ValueError("索引 source_commit 与 manifest 不一致")
    if matching[0].get("dirty") is not manifest["dirty"]:
        raise ValueError("索引 dirty 与 manifest 不一致")


def command_check_index(args):
    check_index(args.index, args.manifest, args.bundle_dir)


def command_canonical(args):
    data = normalized_index(load_json(args.index))
    print(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def build_parser():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("--version", required=True)
    create.add_argument("--channel", choices=("main", "dev"), required=True)
    create.add_argument("--commit", required=True)
    create.add_argument("--dirty", action="store_true")
    create.add_argument("--epoch", required=True, type=int)
    create.add_argument("--asset", required=True)
    create.add_argument("--bootstrap", required=True)
    create.add_argument("--output", required=True)
    create.set_defaults(func=create_manifest)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--manifest", required=True)
    verify.add_argument("--bundle-dir", required=True)
    verify.set_defaults(func=command_verify)
    index = subparsers.add_parser("index")
    index.add_argument("--base")
    index.add_argument("--manifest", required=True)
    index.add_argument("--bundle-dir", required=True)
    index.add_argument("--output", required=True)
    index.add_argument("--keep", type=int, default=5)
    index.set_defaults(func=create_index)
    check = subparsers.add_parser("check-index")
    check.add_argument("--index", required=True)
    check.add_argument("--manifest", required=True)
    check.add_argument("--bundle-dir", required=True)
    check.set_defaults(func=command_check_index)
    canonical = subparsers.add_parser("canonical")
    canonical.add_argument("--index", required=True)
    canonical.set_defaults(func=command_canonical)
    return parser


def main():
    try:
        args = build_parser().parse_args()
        args.func(args)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print("错误: %s" % error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

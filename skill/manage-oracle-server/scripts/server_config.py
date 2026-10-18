#!/usr/bin/env python3
"""Private profile validation and atomic merge; never read a private key."""
from __future__ import annotations

import argparse
import copy
import ipaddress
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta


class SkillError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


SKILL_ROOT = Path(__file__).resolve().parents[1]
MAX_CONFIG_BYTES = 1024 * 1024
ALIAS = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")


def validate_private_path(value, must_exist=False, kind="file") -> Path:
    """Require a native absolute path outside this skill and every Git worktree."""
    if not isinstance(value, (str, os.PathLike)) or not str(value) or "\x00" in str(value):
        raise SkillError("invalid_path", "需要当前执行环境中的绝对路径。")
    path = Path(value)
    if not path.is_absolute():
        raise SkillError("invalid_path", "请提供当前执行环境中的绝对路径，不自动换用其他主机的路径。")
    try:
        resolved = path.resolve()
        if resolved == SKILL_ROOT or SKILL_ROOT in resolved.parents:
            raise SkillError("unsafe_storage", "个人配置与状态必须保存在 skill 和 Git 工作区之外。")
        for parent in (resolved, *resolved.parents):
            if (parent / ".git").exists():
                raise SkillError("unsafe_storage", "个人配置与状态必须保存在 Git 工作区之外。")
        if resolved.exists():
            if kind == "file" and not resolved.is_file():
                raise SkillError("invalid_path", "目标必须是普通文件。")
            if kind == "dir" and not resolved.is_dir():
                raise SkillError("invalid_path", "目标必须是目录。")
        elif must_exist:
            raise SkillError("missing_file", "指定的本机文件不存在；请核对配置，不自动寻找替代文件。")
        return resolved
    except (OSError, ValueError, RuntimeError):
        raise SkillError("invalid_path", "无法解析或访问指定路径。") from None


def resolve_config_path(config_path=None) -> Path:
    value = config_path or os.environ.get("ORACLE_SERVER_CONFIG")
    if value is None:
        value = str(Path.home() / ".config" / "oracle-server" / "profiles.json")
    return validate_private_path(value)


def _no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SkillError("duplicate_key", "JSON 含重复字段，不能可靠确定配置。")
        result[key] = value
    return result


def read_json(path: Path):
    try:
        if path.stat().st_size > MAX_CONFIG_BYTES:
            raise SkillError("oversize_config", "配置文件过大。")
        return json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=_no_duplicates)
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise SkillError("invalid_json", "无法读取有效的 UTF-8 JSON；未输出其内容。") from None


def load_config(config_path=None, allow_missing=False) -> dict:
    path = resolve_config_path(config_path)
    if not path.exists() and allow_missing:
        return {"version": 1, "profiles": {}}
    if not path.is_file():
        raise SkillError("missing_config", "尚未配置实例；先按 configuration.md 收集并确认所需字段。")
    data = read_json(path)
    if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
        raise SkillError("unsupported_config", "仅支持 version 为整数 1 的配置；不自动迁移未知版本。")
    if not isinstance(data.get("profiles"), dict):
        raise SkillError("invalid_config", "profiles 必须是对象。")
    return data


def _text(value, label, pattern=None, maximum=256):
    if not isinstance(value, str) or not value or len(value) > maximum or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise SkillError("invalid_field", f"{label} 无效。")
    if "<" in value or ">" in value or (pattern is not None and not re.fullmatch(pattern, value)):
        raise SkillError("invalid_field", f"{label} 必须是已确认的有效值，不能使用占位符。")
    return value


def confirmation(value, label):
    try:
        _text(value, label)
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if timestamp.tzinfo is None or timestamp > datetime.now(timezone.utc) + timedelta(minutes=5):
            raise ValueError()
    except (ValueError, TypeError):
        raise SkillError("missing_confirmation", f"{label} 需要真实的、带时区的用户确认时间。") from None


def validate_ocid(value, resource_type=None):
    value = _text(value, "OCID", r"ocid1\.[a-z0-9_]+\.[A-Za-z0-9._-]+", 512)
    if resource_type and not value.startswith(f"ocid1.{resource_type}."):
        raise SkillError("invalid_ocid", "OCID 资源类型与目标不一致。")
    return value


def validate_ssh(profile):
    host = _text(profile.get("host"), "host", maximum=253)
    if "%" in host or any(c.isspace() for c in host) or host.startswith("-"):
        raise SkillError("invalid_host", "host 必须是单个 IP 或合法主机名。")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", part) for part in host.rstrip(".").split(".")):
            raise SkillError("invalid_host", "host 必须是单个 IP 或合法主机名。") from None
    port = profile.get("port")
    if type(port) is not int or not 1 <= port <= 65535:
        raise SkillError("invalid_port", "port 必须是 1–65535 的整数。")
    _text(profile.get("user"), "SSH user", r"[A-Za-z_][A-Za-z0-9_.-]{0,63}")
    confirmation(profile.get("key_path_confirmed_at"), "key_path_confirmed_at")
    validate_private_path(profile.get("key_path"), must_exist=True)
    if "ssh_config_file" in profile:
        confirmation(profile.get("ssh_config_confirmed_at"), "ssh_config_confirmed_at")
        validate_private_path(profile["ssh_config_file"], must_exist=True)


def validate_oci(profile):
    settings = profile.get("oci")
    if not isinstance(settings, dict):
        raise SkillError("oci_not_configured", "此实例未配置 OCI 身份；SSH 管理仍可单独使用。")
    confirmation(settings.get("confirmed_at"), "oci.confirmed_at")
    if settings.get("auth_mode") not in ("api_key", "security_token"):
        raise SkillError("invalid_auth_mode", "本机 OCI auth_mode 仅支持 api_key 或 security_token。")
    validate_private_path(settings.get("config_file"), must_exist=True)
    _text(settings.get("config_profile"), "OCI config_profile", r"[A-Za-z0-9_.-]{1,128}")
    _text(settings.get("region"), "region", r"[a-z][a-z0-9-]{1,62}")
    validate_ocid(settings.get("instance_id"), "instance")
    compartment = validate_ocid(settings.get("compartment_id"))
    if not compartment.startswith(("ocid1.compartment.", "ocid1.tenancy.")):
        raise SkillError("invalid_compartment", "compartment_id 必须是 compartment 或根 tenancy OCID。")
    for field in ("metric_compartment_id", "alarm_compartment_id", "notification_compartment_id"):
        if field in settings:
            value = validate_ocid(settings[field])
            if not value.startswith(("ocid1.compartment.", "ocid1.tenancy.")):
                raise SkillError("invalid_compartment", f"{field} 类型无效。")
    if "topic_id" in settings:
        validate_ocid(settings["topic_id"], "onstopic")


def read_profile(server, config_path=None, need_ssh=False, need_oci=False) -> dict:
    if not isinstance(server, str) or not ALIAS.fullmatch(server):
        raise SkillError("invalid_server", "必须显式选择有效实例别名，不默认操作最近实例。")
    data = load_config(config_path)
    profile = data["profiles"].get(server)
    if not isinstance(profile, dict):
        raise SkillError("unknown_server", "配置中没有该实例别名。")
    if need_ssh:
        validate_ssh(profile)
    if need_oci:
        validate_oci(profile)
    return copy.deepcopy(profile)


def _merge(old, new):
    result = copy.deepcopy(old)
    for key, value in new.items():
        result[key] = _merge(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) else copy.deepcopy(value)
    return result


def atomic_json(path: Path, data):
    path = validate_private_path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".oracle-profile-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def config_lock(path):
    """Portable exclusive lock; never steal a lock from a possibly live writer."""
    lock = path.with_name(path.name + ".lock")
    validate_private_path(lock)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise SkillError("config_busy", "另一个配置写入可能尚未完成；先核实进程及文件状态，不自动删除锁。") from None
    try:
        os.close(fd)
        yield
    finally:
        lock.unlink()


def upsert_profile(server, incoming, config_path=None):
    path = resolve_config_path(config_path)
    with config_lock(path):
        _upsert_profile(server, incoming, path)


def _upsert_profile(server, incoming, config_path=None):
    if not isinstance(server, str) or not ALIAS.fullmatch(server) or not isinstance(incoming, dict) or not incoming:
        raise SkillError("invalid_profile", "需要有效别名和非空实例对象。")
    data = load_config(config_path, allow_missing=True)
    previous = data["profiles"].get(server, {})
    if not isinstance(previous, dict):
        raise SkillError("invalid_profile", "已有实例配置损坏；不能直接覆盖。")
    result = _merge(previous, incoming)
    if any(field in previous and previous.get(field) != result.get(field) for field in ("host", "port", "user")) and previous.get("key_path_confirmed_at") == result.get("key_path_confirmed_at"):
        raise SkillError("confirmation_required", "SSH 目标或账号变化，需要重新确认此目标使用的密钥路径并记录本次时间。")
    for field, timestamp in (("key_path", "key_path_confirmed_at"), ("ssh_config_file", "ssh_config_confirmed_at")):
        if field in previous and previous.get(field) != result.get(field) and previous.get(timestamp) == result.get(timestamp):
            raise SkillError("confirmation_required", "路径发生变化，需要重新确认并记录本次真实确认时间。")
    old_oci, new_oci = previous.get("oci"), result.get("oci")
    if isinstance(old_oci, dict) and isinstance(new_oci, dict):
        binding_fields = ("auth_mode", "config_file", "config_profile", "region", "instance_id", "compartment_id",
                          "metric_compartment_id", "alarm_compartment_id", "notification_compartment_id", "topic_id")
        if any(old_oci.get(k) != new_oci.get(k) for k in binding_fields) and old_oci.get("confirmed_at") == new_oci.get("confirmed_at"):
            raise SkillError("confirmation_required", "OCI 身份或实例绑定变化，需要本次用户确认记录。")
    ssh_fields = {"host", "port", "user", "key_path", "key_path_confirmed_at", "ssh_config_file", "ssh_config_confirmed_at"}
    if ssh_fields & set(incoming):
        validate_ssh(result)
    if "oci" in incoming:
        validate_oci(result)
    if not any(k in result for k in ("host", "oci")):
        raise SkillError("invalid_profile", "实例至少需要完整 SSH 或 OCI 配置。")
    data["profiles"][server] = result
    atomic_json(resolve_config_path(config_path), data)


def main(argv=None):
    parser = argparse.ArgumentParser(description="检查/合并包外私有实例配置；不连接服务器，不读取私钥内容。")
    parser.add_argument("--config", help="包外配置绝对路径")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check")
    check.add_argument("--server", required=True)
    check.add_argument("--ssh", action="store_true")
    check.add_argument("--oci", action="store_true")
    update = sub.add_parser("upsert")
    update.add_argument("--server", required=True)
    update.add_argument("--input", required=True, help="仅当前实例对象的包外 JSON；只写真实用户确认记录")
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            read_profile(args.server, args.config, need_ssh=args.ssh, need_oci=args.oci)
        else:
            payload = read_json(validate_private_path(args.input, must_exist=True))
            upsert_profile(args.server, payload, args.config)
        print(json.dumps({"status": "ok", "server": args.server, "operation": args.command}, ensure_ascii=False))
        return 0
    except SkillError as exc:
        print(json.dumps({"status": "error", "code": exc.code, "message": exc.message}, ensure_ascii=False))
        return 2
    except (OSError, ValueError, TypeError):
        print(json.dumps({"status": "error", "code": "local_error", "message": "本地配置操作失败；未输出敏感路径或原始异常。"}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())

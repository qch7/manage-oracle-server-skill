# Oracle Server Management Skill

[![Validate](https://github.com/qch7/manage-oracle-server-skill/actions/workflows/validate.yml/badge.svg)](https://github.com/qch7/manage-oracle-server-skill/actions/workflows/validate.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

面向 Codex 等支持 `SKILL.md` 的 Agent 的 Oracle Cloud Infrastructure（OCI）Linux 服务器管理 skill。通过 SSH 维护实例，通过 Oracle 官方 SDK/CLI 查询和管理云资源，并提供磁盘容量监控与告警工具。

A reusable agent skill for OCI Linux server management, explicit local SSH profiles, resource inspection, and disk monitoring. User-specific connection details and credentials stay outside the repository.

## 功能

- **实例内运维**：SSH 连接、Linux 系统诊断、Docker、systemd、存储检查与维护。
- **云资源检查**：实例、启动卷/块卷、VNIC、路由、安全规则，以及指标、通知和告警的定向查询。
- **磁盘监控**：Linux 文件系统可用空间、使用率、inode 指标采集；Instance Principals 发布；systemd service/timer 模板。
- **告警变更**：生成有时效的审阅计划，确认后应用，并通过回读核验结果。
- **配置隔离**：多个实例别名，首次 SSH 使用确认本机私钥完整路径；私有配置放在 skill 和 Git 工作区之外。

完整 Agent 指令见 [SKILL.md](skill/manage-oracle-server/SKILL.md)。查询入口只覆盖文档中列明的操作；其他 OCI 操作由 Agent 在用户授权范围内调用官方 SDK/CLI。安装 skill 不会自动安装依赖、连接服务器或创建云资源。

## 环境要求

| 用途 | 要求 |
| --- | --- |
| 使用 skill | 能加载 `SKILL.md` 的 Agent，以及对应工具执行能力 |
| SSH 运维 | 本机 OpenSSH、用户提供的实例连接资料 |
| 配置脚本、离线测试 | Python 3.10+，建议 3.12 |
| 本包 OCI 脚本 | 在选定 Python 环境安装 `oci==2.188.0` |
| OCI CLI | 按需独立安装；不属于本包 Python requirements |
| 磁盘采集部署 | OCI Linux 实例、systemd、Python venv、已授权的实例身份与 Monitoring 权限 |

本机管理脚本支持 Windows/Linux/macOS；磁盘真实采样在目标 Linux 执行。SDK、CLI 和服务器采集器宜使用各自的隔离环境。仅做 SSH 管理无需安装 OCI SDK/CLI。

## 安装 skill

### 通过 Agent 安装

在提供 skill-installer 的 Codex 环境中发送：

```text
使用 $skill-installer 从以下地址安装 skill：
https://github.com/qch7/manage-oracle-server-skill/tree/main/skill/manage-oracle-server
```

安装路径指向仓库中的 `skill/manage-oracle-server` 子目录。若同名 skill 已存在，先检查和备份已有版本，不直接覆盖个人修改。

### 手动安装

先克隆仓库，再复制完整 skill 目录。以下命令不会安装 SDK、修改服务器或覆盖已有同名 skill。

Windows PowerShell：

```powershell
git clone https://github.com/qch7/manage-oracle-server-skill.git
if ($LASTEXITCODE -ne 0) { throw 'Clone failed' }
$codexDir = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }
$skillsDir = Join-Path $codexDir 'skills'
$destination = Join-Path $skillsDir 'manage-oracle-server'
if (Test-Path -LiteralPath $destination) { throw 'Skill already exists; review it before updating.' }
New-Item -ItemType Directory -Path $skillsDir -Force | Out-Null
Copy-Item -LiteralPath '.\manage-oracle-server-skill\skill\manage-oracle-server' -Destination $destination -Recurse
```

Linux/macOS：

```bash
git clone https://github.com/qch7/manage-oracle-server-skill.git && (
    skills_dir="${CODEX_HOME:-$HOME/.codex}/skills"
    destination="$skills_dir/manage-oracle-server"
    if [ -e "$destination" ] || [ -L "$destination" ]; then
        echo 'Skill already exists; review it before updating.' >&2
        exit 1
    fi
    mkdir -p "$skills_dir" &&
        cp -R manage-oracle-server-skill/skill/manage-oracle-server "$destination"
)
```

## 首次配置

在安装后的对话中调用 `$manage-oracle-server`，提供本次需要的配置：

1. **SSH**：实例别名、IP/域名、用户名、端口，以及本机私钥的完整路径。Agent 必须取得用户对私钥路径的明确确认；只检查文件元数据，不要求上传、粘贴或复制私钥。
2. **OCI**：只在需要云端操作时配置身份、region、实例及 compartment 绑定。支持 API Key 和浏览器会话；登录密码、MFA、密钥口令仅在本机输入。SSH 私钥不会自动充当 OCI API 签名密钥。
3. **私有配置**：默认使用用户主目录下 `.config/oracle-server/profiles.json`，可通过 `ORACLE_SERVER_CONFIG` 指定包外绝对路径。真实连接配置与凭据保存在 skill 和 Git 工作区之外；`runtimes.json` 与连接配置备份保留在用户配置目录。
4. **运维状态**：任务脚本、下载产物、日志、计划和执行记录统一放在用户指定工作目录下的 `state/`，不再默认写入用户配置目录。状态目录保持在 skill 和源码 Git 仓库之外，不纳入版本控制；切换目录执行 skill 脚本时仍沿用已确定的状态绝对路径。

安装只是复制文件，没有自动执行的安装钩子；负责交互配置的 Agent 在首次使用时完成确认。确认时间、计划哈希等字段用于校验一致性，无法代替真实用户授权。

配置格式、原子更新和运行环境说明见 [配置与连接](skill/manage-oracle-server/references/configuration.md)，认证步骤见 [OCI 身份配置](skill/manage-oracle-server/references/oci-authentication.md)。示例中的尖括号占位符必须替换为用户已核对的信息，不能直接执行。

## 使用示例

```text
使用 $manage-oracle-server，配置我指定的 OCI 实例，先确认 SSH 私钥路径。
```

```text
使用 $manage-oracle-server，只读检查 home 实例的系统、Docker 服务和磁盘空间。
```

```text
使用 $manage-oracle-server，检查 home 实例端口不可达的原因，核对实例内监听及 OCI 网络规则。
```

```text
使用 $manage-oracle-server，为 home 实例准备磁盘容量监控部署和告警计划，先展示需要创建的资源与发布范围。
```

`home` 只是示例别名，没有预置服务器。脚本示例需在已安装的 skill 目录运行，并使用正确的 Python 环境：

```text
python scripts/server_config.py check --server home --ssh
python scripts/oci_ops.py --server home inventory
python scripts/oci_ops.py --server home network
python scripts/oci_ops.py --server home monitoring inspect
```

`check` 只校验本地配置；不证明 SSH/OCI 认证或云端资源健康。查询结果 `partial` 表示证据不完整，不能当作资源不存在。离线测试也不代替真实部署、指标回读和通知验收。

## 文档与目录

| 路径 | 内容 |
| --- | --- |
| [skill/manage-oracle-server/](skill/manage-oracle-server/SKILL.md) | 可单独安装的 skill 包 |
| [references/configuration.md](skill/manage-oracle-server/references/configuration.md) | 实例配置、SDK/CLI 环境、SSH |
| [references/oci-authentication.md](skill/manage-oracle-server/references/oci-authentication.md) | API Key、浏览器会话与目标绑定 |
| [references/oci-operations.md](skill/manage-oracle-server/references/oci-operations.md) | 系统、存储、实例与备份 |
| [references/oci-networking.md](skill/manage-oracle-server/references/oci-networking.md) | VNIC、路由与安全规则 |
| [references/oci-monitoring.md](skill/manage-oracle-server/references/oci-monitoring.md) | 指标查询、回读验证、告警计划 |
| [references/disk-monitoring.md](skill/manage-oracle-server/references/disk-monitoring.md) | 采集器部署、迁移、修复与验收 |
| [references/sources.md](skill/manage-oracle-server/references/sources.md) | 官方资料与离线测试 |
| [scripts/](skill/manage-oracle-server/scripts/server_config.py) | 本地配置、OCI 查询/告警、磁盘采集脚本 |
| [assets/](skill/manage-oracle-server/assets/disk-metrics.example.json) | 脱敏配置示例、systemd 模板与图标 |
| [tests/](skill/manage-oracle-server/tests/test_server_config.py) | 使用合成数据和模拟响应的离线测试 |

## 本地验证

在仓库根目录的开发环境中安装验证依赖并执行：

```text
python -m pip install -r requirements-dev.txt
python -B tools/validate.py
python -B -m unittest discover -s tests -v
python -B -m unittest discover -s skill/manage-oracle-server/tests -v
```

如需运行真实 SDK 模型与序列化合同测试，另在该开发环境安装：

```text
python -m pip install -r skill/manage-oracle-server/requirements.txt
```

缺少 SDK 时相关合同测试会跳过；测试不需要真实 SSH/OCI 凭据，不连接 OCI、SSH 或 IMDS。CI 在 Windows/Linux、Python 3.10/3.12 上安装固定 SDK 并执行上述检查。发布检查包含格式、相对文档链接、示例 JSON、Python 语法与常见敏感信息模式，仍需人工检查待提交的文件。

## 安全与贡献

变更前核对目标与影响；OCI 资源创建、删除和终止必须按 skill 的授权流程执行。不要在 issue、PR、截图或日志中公开私钥、令牌、个人私钥路径和真实连接配置。参见 [SECURITY.md](SECURITY.md) 和 [CONTRIBUTING.md](CONTRIBUTING.md)。

项目采用 [MIT 许可证](LICENSE)。本项目与 Oracle、OpenAI 无隶属或官方支持关系。

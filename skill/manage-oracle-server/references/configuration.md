# 配置与连接

## 私有配置

默认文件为用户主目录下 `.config/oracle-server/profiles.json`，可由 `ORACLE_SERVER_CONFIG` 或 `--config` 指定原生绝对路径。配置、状态和计划必须在 skill 与所有 Git 工作区之外；Linux/macOS 限制目录/文件权限，Windows 核对 ACL。

```json
{
  "version": 1,
  "profiles": {
    "home": {
      "host": "<confirmed-host>",
      "port": 22,
      "user": "<confirmed-user>",
      "key_path": "<confirmed-native-absolute-key-path>",
      "key_path_confirmed_at": "<actual-confirmation-time-with-timezone>",
      "oci": {
        "auth_mode": "api_key",
        "config_file": "<confirmed-native-absolute-oci-config-path>",
        "config_profile": "<confirmed-profile>",
        "region": "<confirmed-region>",
        "instance_id": "<confirmed-instance-ocid>",
        "compartment_id": "<confirmed-compartment-or-tenancy-ocid>",
        "confirmed_at": "<actual-confirmation-time-with-timezone>"
      }
    }
  }
}
```

SSH 字段组和 `oci` 字段组可分别省略，保留的组需完整。别名只用小写字母、数字、`_`、`-`，以字母或数字开头，最多 64 字符。主机使用 IP 或合法主机名，IPv6 不加方括号；端口单独保存。

首次连接让用户确认该实例的本机 SSH 私钥完整路径；已有真实确认可复用。路径失效时让用户修正，不搜索替代密钥、不改 ACL，也不从其他 skill、源码或日志猜连接信息。主机、账号、端口或密钥改变时重新核对。

OCI 身份需确认配置路径、profile、认证模式、region、实例和 compartment。本机脚本支持 `api_key`、`security_token`；令牌模式使用 `SecurityTokenSigner`。签名密钥与 token 路径需为原生绝对路径，可展开配置中显式写出的 `~`。SSH key 不能自动充当 API key。首次配置、会话续期和只读验证按 [oci-authentication.md](oci-authentication.md) 继续，密码、MFA 和密钥口令仅在本机输入。

OCI 可选字段：`metric_compartment_id`、`alarm_compartment_id`、`notification_compartment_id`、`topic_id`。前三项默认使用实例 compartment；跨位置资源按实际值配置，不自动改写 SSH 地址。

使用 OCI CLI 时显式传入 `--config-file`、`--profile`、`--auth`、`--region` 和操作目标 OCID，不依赖默认 profile、region 或其他环境中的目标设置。

## 校验与更新

```text
python scripts/server_config.py --config <absolute-profiles.json> check --server home --ssh
python scripts/server_config.py --config <absolute-profiles.json> check --server home --oci
python scripts/server_config.py --config <absolute-profiles.json> upsert --server home --input <absolute-private-fragment.json>
```

`--input` 是当前实例对象，如 `{"oci": {...}}`，不含 `profiles` 外层。工具合并未知字段并保留其他实例；身份或目标变化需更新对应的真实确认时间。`check` 不带通道参数只检查外层结构，不能证明认证可用。

配置同级的私有 `state/` 保存挂载点、受保护服务、观察时间和执行记录。未知配置版本停止；锁冲突先检查持有进程，不按文件年龄删除锁。历史记录不代替实时检查。

## 本机 SDK 与 CLI 环境

按当前任务检查并复用环境，不因安装 skill 就安装全部工具。先读取 `profiles.json` 同级可选的 `runtimes.json`，优先使用用户已选择且验证有效的路径，再检查默认位置和 PATH。

```json
{
  "version": 1,
  "sdk_python": "<absolute-sdk-python-path>",
  "oci_cli": "<absolute-oci-executable-path>",
  "sdk_version": "2.188.0",
  "cli_version": "<verified-cli-version>"
}
```

安装验证后记录实际路径和版本；使用前核对文件及版本，不仅凭记录判定可用。此文件只保存运行环境信息，不含 OCI 凭据，未知版本先核对。

| 任务 | 所需环境 | 本地检查 |
| --- | --- | --- |
| SSH 运维、配置检查 | OpenSSH、Python；无需 SDK/CLI | `ssh -V`、所选 Python 的 `--version` |
| 本包 `oci_ops.py` | Python 3.10+，建议 3.12；`oci==2.188.0` | 用实际执行脚本的 Python 导入 `oci` 并读取包版本 |
| OCI CLI 操作 | 独立安装的 `oci-cli` 及其依赖 | 对实际 CLI 路径执行 `--version` |

CLI 不在 PATH 时只说明不能直接调用，可核对用户提供的安装路径。CLI 自带 SDK 依赖不代表当前 Python 能导入它；两者的 SDK 版本约束可能不同，默认分开环境。

用户要求 OCI 操作或检查 CLI/SDK 可用性时，缺失或版本不匹配的处理分为三步：

1. 给出具体安装方案：工具与已核对的版本、所用解释器、展开后的绝对安装路径、下载依赖、环境影响及安装后的验收方式。只检查可用性也要给方案；不要只说“可以安装”或“未安装”。仅做 SSH 运维则无需额外推荐 OCI 工具。
2. 明确询问是否执行该方案。提出方案不修改环境；OCI 身份缺失另行列出，不作为省略方案的理由。
3. 获确认后由 Agent 安装并验证；已有同一具体安装授权则直接执行。未确认时暂停安装，继续其他独立检查，并保留安装问题供用户决定。仅要求安装/更新 skill 文件不等于授权安装运行环境。

默认使用包外的用户级 venv，不改系统 Python、Anaconda 或全局 PATH。SDK 环境建议为用户主目录下 `.local/share/oracle-server/venv`，CLI 环境为 `.local/share/oracle-server/cli-venv`；安装计划中展开为本机绝对路径。用户拒绝安装时保留已完成配置并说明缺项。

获授权后安装 SDK 的 PowerShell 示例：

```powershell
$skillDir = '<absolute-skill-directory>'
$venvDir = Join-Path $HOME '.local/share/oracle-server/venv'
python -m venv $venvDir
if ($LASTEXITCODE -ne 0) { throw 'venv creation failed' }
$ociPython = Join-Path $venvDir 'Scripts/python.exe'
& $ociPython -m pip install -r (Join-Path $skillDir 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'dependency installation failed' }
```

CLI 按官方安装说明在已确认的独立环境安装约定版本，记录可执行文件绝对路径，无需全局改 PATH。安装后分别验证 SDK 导入/版本、CLI `--version`；随后进入 [认证检查](oci-authentication.md)，区分工具可用、身份有效和资源权限，不把认证缺失当作任务终点。

Linux/macOS 使用 `bin/python`；服务器创建自己的原生 venv，不复制本机环境。升级 SDK 后重跑合同测试。

## SSH 与文件传输

在 SSH 校验通过后，使用参数数组调用本机 OpenSSH：

```powershell
$configPath = if ($env:ORACLE_SERVER_CONFIG) { $env:ORACLE_SERVER_CONFIG } else {
    Join-Path $HOME '.config/oracle-server/profiles.json'
}
$profileName = 'home' # 用户本次指定的别名
& $ociPython (Join-Path $skillDir 'scripts/server_config.py') --config $configPath check --server $profileName --ssh
if ($LASTEXITCODE -ne 0) { throw 'SSH profile validation failed' }
$config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
$profile = $config.profiles.PSObject.Properties[$profileName].Value
$sshArgs = @(
    '-F', 'none', '-i', $profile.key_path, '-p', [string]$profile.port,
    '-o', 'IdentitiesOnly=yes', '-o', 'IdentityAgent=none',
    '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
    '-o', 'StrictHostKeyChecking=yes', '-o', 'ForwardAgent=no',
    '-o', 'ClearAllForwardings=yes', '-o', 'PermitLocalCommand=no',
    '-o', 'ControlMaster=no', '-o', 'ControlPath=none',
    '-T', '-l', $profile.user, $profile.host, 'uname -m'
)
& ssh @sshArgs
if ($LASTEXITCODE -ne 0) { throw 'SSH failed' }
```

- `-F none` 隔离未知 SSH 配置；客户端不支持时先核对版本。`-i` 与 `IdentitiesOnly` 不能单独排除其他 `IdentityFile`。
- 未知指纹通过可信渠道核对，再由用户交互接受；指纹变化先调查。不得关闭校验、自动回答 yes 或删除 `known_hosts` 绕过问题。
- 加密私钥在本机交互解锁，或使用用户明确确认的受控 agent；禁用 agent forwarding。
- 堡垒机/代理可使用包外 `ssh_config_file` 和真实 `ssh_config_confirmed_at`。先审阅 Include、目标、身份、代理、转发和命令选项；未审阅前也不运行可能触发 `Match exec` 的 `ssh -G`。
- SFTP 优先；SCP/SFTP 端口参数为 `-P`，沿用相同身份和校验。远端 shell 仍解析命令和路径，复杂变更先准备可审阅脚本。
- 通过执行工具设置整体时限；`ConnectTimeout` 仅限制连接。持续任务分段检查并汇报。

# OCI 身份配置

工具安装后沿“身份 → 目标绑定 → 只读验证”推进。缺少身份时说明路径并收集必要信息；用户只要求安装工具时提供下一步，不自行登录或创建云端凭据。

## 1. 检查与选择

优先复用用户提供或私有 profile 中已确认的配置文件与 profile。仅检查必要字段和凭据文件元数据，不打印配置全文，不读取私钥或 token 内容。

没有可用配置时，确认 region 与认证方式：

| 方式 | 适用情况 | 用户参与 |
| --- | --- | --- |
| `security_token` | 临时管理、首次验证 | 浏览器完成登录/MFA；会话到期后续期或重新登录 |
| `api_key` | 长期运维、稳定复用 | 提供 Tenancy/User OCID；确认专用公钥登记到哪个账户 |

OCID 是标识符，不是密钥。region 不根据 IP 猜测；不能将 SSH key、Auth Token、Console 密码当作 API signing key。

## 2. 浏览器会话

确认本机配置位置、profile、region 和会话文件目录；已有文件/profile 先检查，避免覆盖。使用已验证的 CLI：

```powershell
$ociCli = '<verified-cli-absolute-path>'
$authConfig = Join-Path $HOME '.oci/config'
$sessionDir = Join-Path $HOME '.oci/sessions/<new-profile>'
& $ociCli session authenticate --region '<confirmed-region>' --config-location $authConfig --profile-name '<new-profile>' --token-location $sessionDir
```

Agent 启动流程并处理非秘密参数，用户在浏览器输入登录信息和 MFA。完成后核对返回状态及配置字段，不仅凭浏览器关闭认定成功。`--no-browser` 需要已有 API key 或会话，不是跳过首次认证的办法。

会话验证使用 `session validate --config-file ... --profile ... --auth security_token`；`--local` 只检查本机到期时间，不能证明云端接受身份。需要续期时在既有授权范围内使用 `session refresh`，失败或超出可续期范围再引导用户登录。口令不进入聊天。

## 3. 长期 API Key

1. 确认 Tenancy OCID、User OCID、region、新 profile 名和本机私有存储位置。
2. 准备专用签名密钥。官方 `oci setup keys` 可生成密钥对；先核对当前帮助和已有路径，不使用 overwrite 覆盖旧密钥。私钥留在本机并限制文件访问；需要加密时由用户在本机输入口令，不写进聊天或配置正文。
3. 计算并展示公钥指纹。登记前列出用户、tenancy、指纹、数量及权限影响，遵守主 skill 的资源创建确认规则。API Key 使用该用户已有的 IAM 权限，生成密钥不增加 IAM 策略权限。
4. 用已认证的官方工具或控制台登记公钥；首次没有任何 API 身份时，指导账户所有者完成这一步，或在其已登录的控制台会话中协助。只上传公钥，不能用 SSH 权限代替 IAM 授权。
5. 准备包含 `user`、`tenancy`、`fingerprint`、`key_file`、`region` 的专用 INI profile，`key_file` 使用绝对路径。保留已有 profile；不在 profile 中保存明文口令。未登记公钥时标记“本机材料已准备，云端登记待完成”。

私钥生成与公钥登记分开记录；用户提供了 OCID 或生成了本地文件，都不能当作登记成功的证据。

## 4. 目标绑定与验收

- 用所选身份做一次任务相关的只读 API 请求。尚未绑定实例时，可读取明确指定用户的必要身份字段；绑定后优先读取指定实例。限制输出，不扫描整个 tenancy。
- 区分签名/认证失败与权限不足；403 或授权类 404 不通过扩大 IAM、重建密钥或换目标修复。
- 确认实例 OCID、实例 compartment 和 region。可使用用户提供的信息，或在获授权的目标上读取必要元数据，不使用 Cloud Shell 的身份冒充目标实例。
- 经用户核对后，用 `server_config.py upsert` 合并 `oci` 字段，保留 SSH 与其他实例，记录真实确认时间。配置检查通过不代表云端可用，需保留实际 API 验证证据。
- 交付时分别报告运行环境、认证、IAM、目标绑定和只读验证；仍缺用户操作时给出具体步骤与所需信息，不能只报“未配置”。

依据：[CLI 会话认证](https://docs.oracle.com/en-us/iaas/Content/API/SDKDocs/clitoken.htm)、[API 签名密钥](https://docs.oracle.com/en-us/iaas/Content/API/Concepts/apisigningkey.htm)。

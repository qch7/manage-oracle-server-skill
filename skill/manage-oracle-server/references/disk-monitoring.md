# 磁盘容量监控

## 统一方案

默认监控根分区 `/`，每 60 秒采样，通过官方 SDK 的 Instance Principals 发布到 OCI Monitoring。

| 项目 | 约定 |
| --- | --- |
| 程序与 venv | `/opt/oracle-disk-metrics/` |
| 配置 | `/etc/oracle-disk-metrics/config.json` |
| 服务用户 | `oracle-metrics`，无登录权限 |
| 单元 | `oracle-disk-metrics.service`、`.timer` |
| namespace | 新部署默认 `personal_server`；已有配置按实际核对 |
| 维度 | `resourceId`、`mountpoint` |
| `DiskAvailableBytes` | 普通用户可用块数 × 块大小，单位 bytes |
| `DiskUsedPercent` | used / (used + available) × 100，单位 percent |
| `InodeUsedPercent` | 已用 inode / 总 inode × 100；无有效 inode 总数时省略 |

采集入口为 [disk_metrics.py](../scripts/disk_metrics.py)，使用 [配置模板](../assets/disk-metrics.example.json) 和 [service](../assets/oracle-disk-metrics.service)、[timer](../assets/oracle-disk-metrics.timer)。Windows 可生成范围计划和运行 OCI 管理入口；实际采样在目标 Linux 执行。

## 1. 预检与云端准备

- 核对实例、region、OS、架构、systemd、sudo、真实挂载点和已有 timer/cron。Cloud Shell 不是目标实例，不能在其中部署采集器。
- 通过固定 IMDSv2 地址核对目标 instance OCID；不匹配就停止。仅核验必要身份字段，不读取 user-data。
- 已有采集器先检查程序、配置、单元来源和最近发布，避免重复部署或向同一指标流双重发布。
- 本机 OCI 身份负责云端管理，服务器实例身份只负责指标发布；不向服务器复制 API key。

由 Agent 通过官方 SDK/CLI 复用或准备动态组、策略和 Notifications 资源；创建/修改按 [授权规则](../SKILL.md#授权与操作边界) 执行。动态组精确匹配单实例：

```text
ALL {instance.id = '<INSTANCE_OCID>'}
```

发布权限限定实际指标 compartment 和 namespace，例如：

```text
Allow dynamic-group <DYNAMIC_GROUP_NAME> to use metrics in compartment id <METRIC_COMPARTMENT_OCID> where all {target.metrics.namespace = 'personal_server', request.permission = 'METRIC_WRITE'}
```

根 tenancy 使用 `in tenancy`；按身份域核对动态组引用及 policy 附着位置。实例身份可被同实例其他代码使用，不能按 Linux 用户隔离。IAM 传播失败有限重试，不扩大权限；namespace 在首次成功发布后才出现。告警与订阅准备见 [oci-monitoring.md](oci-monitoring.md#云端准备)。

## 2. 部署与范围确认

经已确认的 SSH/SFTP 传输采集器、`requirements.txt`、单元模板及私有配置，核对摘要。已有文件先确认归属并备份，记录 timer 的启用/运行状态及原 venv；同名未知文件和符号链接不直接覆盖。

用户要求部署监控时，可在任务范围内准备隔离环境和自有文件。按实际发行版补齐 Python venv 支持，避免全局 pip、整机升级和重启。默认路径和权限：

```bash
# 仅在账号与目录确认不存在时创建，nologin 路径按发行版核对。
sudo useradd --system --no-create-home --shell /usr/sbin/nologin oracle-metrics
sudo install -d -o root -g root -m 0755 /opt/oracle-disk-metrics
sudo install -d -o root -g oracle-metrics -m 0750 /etc/oracle-disk-metrics
sudo python3 -m venv /opt/oracle-disk-metrics/venv
```

程序与 venv 由 root 管理；配置使用 `root:oracle-metrics`、`0640`，按 requirements 安装 SDK。新版本先准备独立 venv，验证后切换并保留旧环境用于恢复。不要直接重建正在使用的环境。

挂载点必须存在、实际已挂载且不经过符号链接；失败不退回父目录采样。最多 16 个挂载点；bind mount 可能不被 `os.path.ismount` 识别，需先适配核验。增加挂载点时同步调整 `RequiresMountsFor`，并评估 `ProtectHome` 等隔离对访问的影响。

在目标上以最终服务用户执行预览；此时尚未发布：

```text
sudo -u oracle-metrics /opt/oracle-disk-metrics/venv/bin/python /opt/oracle-disk-metrics/disk_metrics.py --config /etc/oracle-disk-metrics/config.json --dry-run
sudo -u oracle-metrics /opt/oracle-disk-metrics/venv/bin/python /opt/oracle-disk-metrics/disk_metrics.py --config /etc/oracle-disk-metrics/config.json --describe-scope
```

向用户展示实例、region、指标 compartment、namespace、指标、挂载点、60 秒周期及费用影响，确认持续发布范围；已有相同具体授权可复用。再把返回的 `scope_sha256` 和真实确认时间填入配置的 `publishing_approval`。变更范围重新确认；字段不能代替真实授权。

采集器默认仅本地采样，显式 `--publish` 才访问 OCI；发布前核对固定 IMDSv2 实例身份与区域，不接受自定义元数据地址。发布使用独立 ingestion endpoint，关闭单次自动重试，并检查 `failed_metrics_count == 0`。部分挂载点失败返回 partial，其余有效样本仍可发布。

## 3. 启动与验收

安装已审阅的模板到 `/etc/systemd/system/`，保持 root 所有和服务隔离。验证后加载：

```bash
sudo systemd-analyze verify /etc/systemd/system/oracle-disk-metrics.service /etc/systemd/system/oracle-disk-metrics.timer
sudo systemctl daemon-reload
sudo systemctl start oracle-disk-metrics.service
```

启动前记录真实 UTC baseline。确认首次发布成功，再启用周期任务：

```bash
sudo systemctl enable --now oracle-disk-metrics.timer
systemctl show oracle-disk-metrics.service -p Result -p ExecMainStatus -p ActiveState
systemctl list-timers oracle-disk-metrics.timer --all --no-pager
```

oneshot 完成后 inactive 可以正常。将样本与同主机 `df -hT`、`df -i` 对照；HTTP 200 不代替发布结果检查。失败时恢复已备份的自有文件、旧环境及原 timer 状态，核实恢复结果；不触碰业务服务。

待正常采样产生新点后，通过本机 OCI 身份验收：

```text
python scripts/oci_ops.py --server home monitoring verify --namespace personal_server --metrics DiskUsedPercent DiskAvailableBytes InodeUsedPercent --mountpoints / --since <recorded-ISO8601-baseline>
```

仅在实际提供 inode 指标时将其加入验收。每个指标 × 挂载点至少两个不同分钟的新点，最新点默认不超过 3 分钟。结果与限制见 [新数据验收](oci-monitoring.md#新数据验收)。

没有本机读取身份时，可使用已获授权的其他读取主体或具备读取权限的实例身份，通过官方 SDK 查询同一时间窗与维度；仅发布成功时标为“发布已验证，持续回读待验证”。不为验收自动扩大采集器权限。

## 4. 图表与告警

图表按实际 region、compartment、namespace、`resourceId`、`mountpoint` 筛选，使用 `1m/Mean`；容量指标不使用 Sum/Rate，也不合并不同挂载点。

| 建议规则 | 指标与条件 | 持续时间 | 严重程度 |
| --- | --- | --- | --- |
| 空间预警 | `DiskUsedPercent >= 85` | `PT10M` | WARNING |
| 空间严重 | `DiskUsedPercent >= 95` | `PT5M` | CRITICAL |
| inode 预警 | `InodeUsedPercent >= 85` | `PT10M` | WARNING |
| 采集断流 | `DiskUsedPercent` 缺失，检测期 `72h` | `PT10M` | ERROR |

阈值按负载确定，可先启用空间预警与断流；inode 无数据时不创建对应阈值规则。每个重要挂载点分别配置；CPU 缺失告警不能替代磁盘采集断流告警。若用剩余空间阈值，GiB 转 Bytes 乘以 `1024^3`。

按 [告警计划与执行](oci-monitoring.md#计划与执行) 创建 spec、展示计划、确认后提交并回读。通知正文保留动态状态及实例、挂载点、查询和排障建议，避免固定写“磁盘已满”。缺失检测期结束后的 OK/RESET 不能证明采集恢复，需看到新点。

分开报告部署、timer、发布、新数据、告警、订阅和实际通知状态。默认不填满磁盘测试、不向正式指标注入伪造点，也不自动发送测试消息。

## 旧部署与维护

已有 `/opt/oci-disk-monitoring`、`oci-disk-monitoring.timer` 或 `DiskAvailableGiB` 时，保留其运行状态，先核对真实配置、归属和引用；通用查询/验收仍可读取旧指标，无需另一个 skill。

需要统一部署时，准备旧文件备份、指标单位与 namespace、IAM/告警/图表依赖、切换窗口和恢复计划。确认后先停旧发布，再启新方案，禁止两个 timer 同时写同一指标流。GiB 改 Bytes 时同步换算阈值；保留历史数据和恢复材料，删除旧文件或云资源按主规则确认。

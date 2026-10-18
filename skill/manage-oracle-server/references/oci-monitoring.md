# 指标、通知与告警

## 查询与证据

```text
python scripts/oci_ops.py --server home monitoring inspect
python scripts/oci_ops.py --server home monitoring query --namespace oci_computeagent --metric CpuUtilization --hours 1
```

`inspect` 检查指标定义、告警状态、主题和订阅，可用 `--namespace` 缩小范围。指标按实例 `resourceId` 过滤；`unscoped_compartment_*` 只表示同 compartment，不证明属于当前实例。订阅不输出地址或确认令牌。

`query` 固定实例 `resourceId`；时间窗为 1 分钟至 24 小时，`--interval` 为 `1m/5m/1h`，`--statistic` 为 `mean/min/max/sum/count`。最多输出 50 个流、2500 点；截断返回 `partial`。namespace/metric 大小写敏感，字母开头，仅含字母、数字和下划线。

指标、告警、通知可分属不同 compartment，分别使用配置中的 `metric_compartment_id`、`alarm_compartment_id`、`notification_compartment_id`。`topic_id` 指定现有主题；订阅按主题和通知位置筛选。

Compute 的磁盘读写量属于 I/O；空间与 inode 需要 [文件系统采集](disk-monitoring.md)。实例 RUNNING、指标存在和告警 OK 都不能单独证明应用健康或通知已到达。

## 云端准备

通过已配置的 SDK/OCI CLI 查询并复用 IAM、Notifications topic 和订阅；缺少资源时准备具体创建/修改调用，按 [授权规则](../SKILL.md#授权与操作边界) 确认后由 Agent 执行。

告警需要 ACTIVE topic；Email 订阅仍需收件人完成邮件确认，PENDING 不能当作已送达。只读检查不自动发送测试通知；测试须有针对目的地和消息的明确授权。

## 新数据验收

```text
python scripts/oci_ops.py --server home monitoring verify --namespace personal_server --metrics DiskUsedPercent DiskAvailableBytes --mountpoints / --since <recorded-ISO8601-baseline>
```

本机身份需能读取绑定实例和实际指标位置。命令只查一次，不等待、不部署、不连接 SSH；每个指标查询一次 `1m/mean()` 并核对实例、compartment、namespace 和完整维度。

| 参数 | 范围 |
| --- | --- |
| `--namespace` | 实际名称，最多 128 字符 |
| `--metrics` | 1–4 个不同指标；支持 Bytes、GiB、空间和 inode 百分比 |
| `--mountpoints` | 1–16 个规范 Linux 绝对挂载点 |
| `--since` | 真实部署/观察基准，带时区，过去 24 小时内 |
| `--minimum-points` | 每组合不同 UTC 分钟数，1–10，默认 2 |
| `--max-age-minutes` | 最新点最大年龄，1–60 分钟，默认 3 |

只计 baseline 之后、非未来、数值有效的点；额外维度、重复流和读取不完整分别报告。空间百分比限 0–100，容量不得为负。

| 结果 | 含义 |
| --- | --- |
| `ok / verified`，退出码 0 | 所选指标 × 挂载点全部满足数量与新鲜度 |
| `partial / pending`，退出码 2 | 数据不足或陈旧，尚未通过 |
| `partial` 或 `error / incomplete`，退出码 2 或 1 | 权限、范围、歧义或上限导致无法判断 |

`pending` 时按任务有限等待后重查；不能倒填 baseline、降低要求或扩权制造通过。结果仅证明该窗口的新数据，仍需结合 timer 状态。

## 告警 spec

将 [alarm-spec.example.json](../assets/alarm-spec.example.json) 填入包外私有文件；不含身份或任意 MQL。

| 字段 | 规则 |
| --- | --- |
| `kind` | `threshold` 或 `absence` |
| `display_name` | 明确名称；名称不保证全局唯一 |
| `namespace`、`metric` | 实际指标 |
| `compartment_id`、`metric_compartment_id` | 显式填写告警和指标位置 |
| `topic_id` | 已核对的 ACTIVE topic |
| `pending_duration` | 整分钟，`PT1M`–`PT1H` |
| 阈值字段 | `statistic` 为 mean/max/min；`comparison` 为基本比较运算符；`threshold` 为有限数值 |
| 缺失字段 | `absence_detection_period` 为 1m–72h，不短于 pending，不混用阈值字段 |
| `mountpoint` | 可选，Linux 绝对路径；不含空格、引号或转义字符 |
| `alarm_id` | 更新时必填；省略则创建，不按同名覆盖 |
| `severity`、`is_enabled` | 默认 WARNING、true |
| `description`、`body` | 可选，各最多 1024 字符；description 映射 `alarm_summary` |

查询固定 `1m` 窗口与 resolution，绑定实例并按 `resourceId`、可选 mountpoint 分组。`absence_detection_period` 限定持续缺失的检测范围，`pending_duration` 是触发延迟，两者不可混淆；见 [Oracle 缺失检测说明](https://docs.oracle.com/en-us/iaas/Content/Monitoring/Tasks/create-edit-alarm-query-absence-detection-period.htm)。

## 计划与执行

```text
python scripts/oci_ops.py --server home monitoring plan-alarm --spec <absolute-private-spec.json> --out <new-absolute-private-plan.json>
python scripts/oci_ops.py --server home monitoring apply-alarm --plan <absolute-private-plan.json> --approve-plan <user-approved-plan-sha256>
```

1. `plan-alarm` 只读核对实例、topic、同名告警和更新目标，生成有效 15 分钟的新计划。先向用户展示 `data.review` 的操作、位置、MQL、启用状态、目的地、影响和费用；告警可能立即通知订阅者。
2. 用户确认具体创建或更新计划后才执行 `apply-alarm`。哈希只绑定内容，不能代替用户答复。
3. 执行前重查范围、有效期、topic、同名对象和旧 ETag；创建使用固定 retry token，更新使用 `if_match`。写入关闭自动重试，随后回读受管字段。
4. `applied/already_applied/recovered` 表示配置已核验；`uncertain/partial_failure` 仍未完成。检查 `cloud_write_attempted`，错误不代表未提交。

同名冲突、复杂 overrides/suppression 或关联不明时停止覆盖。`.journal.json` 和 `.lock` 保存在私有计划目录；进入 started/uncertain 后只读核对已有结果，不换计划盲目重试，也不按锁年龄清理。计划过期先查实际状态，不能改时间或哈希绕过检查。

最终区分指标持续上报、告警配置、订阅确认和通知实测；缺少投递证据就保留该项未验证。

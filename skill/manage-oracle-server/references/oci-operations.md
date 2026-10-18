# 实例、系统与存储

## 云端盘点

```text
python scripts/oci_ops.py --server home inventory
```

按绑定的 instance OCID 读取状态、shape、镜像、可用域及实际关联的 VNIC、启动卷和块卷。关联资源可能跨 compartment，按准确 ID 查询。

JSON 包含 `status`、`server`、`observed_at`、`data`、`coverage`、`errors`；`ok/partial/error` 对应退出码 `0/2/1`。每列表最多 10 页、1000 项，每次最多 200 次读取；上限或权限错误保留部分证据，不解释为资源不存在。

## 主机与服务

按问题选择必要命令：

```bash
hostname
id
uname -m
cat /etc/os-release
uptime
free -h
df -h
df -i
lsblk -f
findmnt
ps -eo user,pid,ppid,comm,%cpu,%mem,stat --sort=-%cpu
```

服务排查使用指定单元的 `systemctl` 状态、`ss -lntup`、格式化容器状态和有限时间范围日志。只有需要目录占用分析时再运行限定范围的 `du`。不输出进程完整参数、容器环境或日志全文。

根据实际 OS 与架构选择包管理器、软件和镜像；ARM64 与 AMD64 不可混用。`exec format error` 先检查二进制/镜像架构。OCI Run Command 另有镜像与插件要求，不能默认代替 SSH。

| 问题 | 云端检查 | 主机与应用检查 |
| --- | --- | --- |
| SSH 超时 | 地址、VNIC、路由、安全规则 | sshd、监听、防火墙、负载 |
| CPU 高 | 定向 CPU 指标与时间 | 进程、服务、对应负载 |
| 磁盘满 | 卷容量与挂载关联 | 文件系统空间、inode、挂载源、目录占用 |
| 重启后服务失败 | 生命周期与实例身份 | 单元依赖、挂载、容器启动策略、应用健康 |

卷大小不能说明文件系统剩余空间。只需实例内检查时使用 SSH，无需补配 OCI 身份。网络与监控分别见 [oci-networking.md](oci-networking.md)、[oci-monitoring.md](oci-monitoring.md)。

## 实例、卷与备份变更

本包没有实例创建/终止或卷备份写入命令。需要时查当前官方 API，准备具体 SDK/CLI 调用，并遵守 [授权规则](../SKILL.md#授权与操作边界)：

1. 核对准确 ID、region、compartment、当前状态、依赖和受影响服务。
2. 说明停机、公网地址变化、卷保留/删除设置、费用及恢复步骤。
3. 数据库/应用先考虑一致性；卷备份不自动保证应用可恢复。记录备份 ID、时间与可恢复状态。
4. 授权后执行，有限等待并重新读取；超时先查询资源或 work request。

云端扩卷与主机分区/文件系统扩容分别处理；核对设备、UUID、挂载源和使用者后再改分区、`fstab` 或挂载。Free Tier 资格、费用、容量与回收按实际账户和当前资料判断；预算告警不能硬性限制费用。

IAM 只授予任务需要的权限。跨 compartment 按资源实际位置授权；`NotAuthorizedOrNotFound` 不能单独证明对象不存在。

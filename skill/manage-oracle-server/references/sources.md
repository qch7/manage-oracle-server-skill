# 官方资料与验证

SDK 基线为 `oci==2.188.0`；API、IAM、区域与镜像支持以执行时官方文档和实际账户为准。

| 主题 | 官方资料 |
| --- | --- |
| SDK 配置、分页、重试 | [Configuration](https://docs.oracle.com/en-us/iaas/tools/python/latest/configuration.html)、[Pagination](https://docs.oracle.com/en-us/iaas/tools/python/latest/pagination.html)、[Retries](https://docs.oracle.com/en-us/iaas/tools/python/latest/sdk_behaviors/retries.html) |
| CLI | [概览](https://docs.oracle.com/en-us/iaas/Content/API/Concepts/cliconcepts.htm)、[命令参考](https://docs.oracle.com/en-us/iaas/tools/oci-cli/latest/oci_cli_docs/) |
| 实例、网络、存储 | [ComputeClient](https://docs.oracle.com/en-us/iaas/tools/python/latest/api/core/client/oci.core.ComputeClient.html)、[VirtualNetworkClient](https://docs.oracle.com/en-us/iaas/tools/python/latest/api/core/client/oci.core.VirtualNetworkClient.html)、[BlockstorageClient](https://docs.oracle.com/en-us/iaas/tools/python/latest/api/core/client/oci.core.BlockstorageClient.html) |
| 指标、告警、通知 | [MonitoringClient](https://docs.oracle.com/en-us/iaas/tools/python/latest/api/monitoring/client/oci.monitoring.MonitoringClient.html)、[CreateAlarmDetails](https://docs.oracle.com/en-us/iaas/tools/python/latest/api/monitoring/models/oci.monitoring.models.CreateAlarmDetails.html)、[Notifications](https://docs.oracle.com/en-us/iaas/tools/python/latest/api/ons/client/oci.ons.NotificationControlPlaneClient.html) |
| 磁盘指标与实例身份 | [Publishing Custom Metrics](https://docs.oracle.com/en-us/iaas/Content/Monitoring/Tasks/publishingcustommetrics.htm)、[Instance Principals](https://docs.oracle.com/en-us/iaas/Content/Identity/Tasks/callingservicesfrominstances.htm)、[Monitoring IAM](https://docs.oracle.com/en-us/iaas/Content/Identity/Reference/monitoringpolicyreference.htm) |
| SSH | [ssh](https://man.openbsd.org/ssh.1)、[ssh_config](https://man.openbsd.org/ssh_config.5) |

## 离线验证

在 skill 根目录运行：

```text
python -B -m unittest discover -s tests -v
python scripts/oci_ops.py --help
python scripts/oci_ops.py --server example monitoring --help
python scripts/oci_ops.py --server example monitoring verify --help
python scripts/disk_metrics.py --help
```

测试使用合成配置和模拟响应，不连接真实 OCI、SSH 或 IMDS。安装固定 SDK 后运行模型及序列化合同测试；缺少 SDK 时这些测试会跳过。离线通过不能代替服务器部署、云端新数据与通知投递验收。

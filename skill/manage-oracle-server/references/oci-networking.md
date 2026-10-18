# 网络排障

## 收集证据

明确访问方向、源位置、IPv4/IPv6、目标地址、协议、端口和时间，核对 DNS 与实际实例地址。

```text
python scripts/oci_ops.py --server home network
```

脚本沿 instance → VNIC attachment → VNIC → IP/子网 → NSG、安全列表、路由及直接下一跳读取，校验返回 ID。输出 `reachability: not_evaluated`，只收集证据，不模拟完整流量；`partial` 不能当作云网络已排除。

## 判断路径

- 专用路由表按源 IP、VNIC、子网的实际优先级选择，不合并，也不因所选表缺路由而自动回退。结合目的前缀、下一跳和返回路径判断；IGW、NAT、Service Gateway 用途不同。
- **关联 NSG 与 Security List 的允许规则共同生效，无需两者各放行一次。** stateful/stateless 重叠、返回流量、主机防火墙和应用监听分别检查。
- IPv4、IPv6 分别核对地址、路由、规则和监听。公网 IP 不等于公网可达；私网管理可能经过 VPN 或堡垒机。
- VLAN、复杂 DRG、网络防火墙等超出脚本覆盖范围，按真实拓扑继续查询。

主机检查选用：

```bash
ss -lntup
systemctl is-active <confirmed-service>
```

服务名正确引用；确认监听在 loopback、指定地址或所有接口。识别实际生效的 UFW、nftables/iptables 或 firewalld，不另启防火墙。Docker 还需检查端口映射和转发规则，UFW 状态不能单独证明容器端口受限。

## 修复与复核

用“失败环节、证据、缺项、最小修复”组织结论。共享规则变更先盘点其他使用者，按 [授权规则](../SKILL.md#授权与操作边界) 明确端口、协议、来源和影响。

保留当前 SSH 会话及已核实的恢复通道。授权后修改，再核对云端配置、新建管理连接与实际应用流量；避免用关闭防火墙或全面放行代替定位。

依据：[OCI Security Rules](https://docs.oracle.com/en-us/iaas/Content/Network/Concepts/securityrules.htm)、[Docker 防火墙](https://docs.docker.com/engine/network/packet-filtering-firewalls/)。

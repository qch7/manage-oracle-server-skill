# 贡献指南

欢迎修复文档、兼容性问题和脚本行为。提交 issue 时描述预期行为、实际结果、OS/Python/SDK 版本及脱敏后的最小复现；敏感问题按 [SECURITY.md](SECURITY.md) 处理。

## 开发与验证

从仓库根目录运行，在隔离的 Python 3.10+ 环境安装开发依赖。需要 SDK 合同测试时同时安装 skill 的固定 SDK 依赖：

```text
python -m pip install -r requirements-dev.txt -r skill/manage-oracle-server/requirements.txt
python -B tools/validate.py
python -B -m unittest discover -s tests -v
python -B -m unittest discover -s skill/manage-oracle-server/tests -v
```

测试只使用合成配置、临时文件和模拟 API；不得要求贡献者连接生产实例或提供真实凭据。对修改的行为补充必要回归测试。升级 SDK 时同步核对模型、序列化合同和文档版本。

## 修改约定

- `SKILL.md` 保持简短，具体模式与操作细节放在对应参考中；更新命令时同步更新示例。
- 保持 Python 3.10+ 兼容，优先使用标准库及官方 SDK，不把云端调用放在模块导入阶段。
- 保留首次 SSH 私钥路径确认、主机密钥校验、明确目标绑定、变更授权和失败后的实际状态核查。
- SDK/传输异常不得原样输出凭据；查询失败或分页截断应保留不完整状态，不假装为空或成功。
- 私有配置、日志、执行计划、虚拟环境、缓存与 ZIP 产物不进入提交。`.gitignore` 不会保护已经跟踪的文件。
- 不在 CI 中运行 SSH、真实云资源创建/删除、IMDS 查询或通知测试。

提交 PR 前检查 `git diff --cached`，确认没有真实地址、个人目录、OCID、密钥或 token。PR 描述说明问题、改动行为及验证结果；通过贡献即同意将贡献按本项目的 MIT 许可证分发。

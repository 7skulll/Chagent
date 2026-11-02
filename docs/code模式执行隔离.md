# code 模式执行隔离（C0 / C2）

更新：2026-11-02。状态：新增 `isolated` 执行档已实现，并在本机 macOS 通过执行探针；不代表 code 模式已全面开放。网页已支持满足配置的只读 code，见[只读验收记录](只读code验收-2026-11-02.md)；可写/交互 C3、C4 出网授权和 C5 五项目回归仍未完成。

## 当前实现

`CHAGENT_SANDBOX=isolated` 使用 macOS seatbelt 默认拒绝策略。`workspace-write` 保留为旧诊断档，仍然不是读取隔离，不能用于生产模型的 code 命令。正常 `create_runner` 的 code 命令面要求 `isolated`，配置不满足时构造失败；确定性 demo / 显式工具诊断入口仍单独保留，不属于生产模型入口。

- 只允许读取当前工作区、该 sandbox 实例的私有 scratch 和明确列出的系统/Python 运行时。运行时目录通过 `runtime_roots()` 生成，并在审计记录中列出；不开放整个用户目录、`/opt`、共享临时目录或整个 `/System`（避免包含 Data 卷）。
- 默认拒绝写入；仅允许工作区、私有 scratch 和 `/dev/null`。`HOME` 与 `TMPDIR` 指向 scratch 下的 0700 子目录，服务环境中的 API Key、数据库地址、代理、PYTHONPATH、DYLD 和 shell 启动变量不继承。
- 环境白名单在 `create_subprocess_exec` 启动前传入，wrapper 内再执行一次 `env -i`。一次性 exec、`run_tests`、会话长进程共用此边界。PATH 仅包含声明的 Python 与系统工具目录；Git 使用空全局配置和显式 CommandLineTools，避免读取宿主 Git 凭据或发起安装提示。
- 默认不开放网络、通用 Mach 服务、其他进程的信息与信号权限。`isolated + network=true` 直接拒绝；网页搜索仍是 agent 进程内的独立能力，不受命令沙箱控制。
- 每次构造执行策略都重新检查工作区；拒绝源码目录及其父子目录、与运行时重叠的目录，以及预先存在的多硬链接常规文件。扫描不跟随目录符号链接，检查失败就拒绝。沙箱也验证不能从外部文件创建硬链接。
- 新隔离档只接受 seatbelt 后端。后端缺失或任意必要探针失败均拒绝执行，不建议或自动切换到 off。shell 的后代继承内核策略，现有取消、超时和 run 结束回收继续作用于进程组。
- 私有 scratch 的生命周期是 Sandbox 实例，`close()` 或实例回收时清理；不是跨服务持久化的 HOME。同实例同工作区的命令共享 scratch，不能把它当作不同会话之间的隔离。

## C0 调查结论

旧 narrow-read 候选退出 -6 的原因已缩小并复现：shell 启动需要根 vnode 的 `(allow file-read* (literal "/"))`，增加这一条后启动成功。对根目录的 literal 授权不等于 `(subpath "/")`；没有开放根目录下所有文件。

Git 的系统启动器还会寻找开发工具选择信息；显式设置 `DEVELOPER_DIR=/Library/Developer/CommandLineTools` 后，合成项目中的 `git init` / `git status` 通过，无需开放用户偏好或整个 `/private/var/db`。

## 验收与复现

```sh
PYTHONPATH=src .venv/bin/python scripts/probe_code_isolation.py \
  --output docs/diagnostics/code-isolation-2026-11-02.json
```

需要在允许施加 seatbelt 的环境运行。嵌套沙箱中的拒绝或测试跳过不计为边界通过。

探针使用合成工作区和合成 secret，验证：shell/Python 真正启动，工作区读取写入成功，其他项目读取/越界写入被拒，符号链接与 Data 卷别名读取被拒，程序源码不可读，外部硬链接创建被拒，HOME/tmp 可用，环境清空，其他进程信号被拒，回环连接被拒。网络有实际监听器的宿主连接正向对照；Python 和 shell 启动失败不能算断网成功。

工具级验收另外运行实际 `ExecCommandTool`、`RunTestsTool` 和 `ProcessManager`：shell、Git 初始化/状态、环境检查、Python unittest 和分次返回的长命令均通过。原始结果见 [执行隔离探针](diagnostics/code-isolation-2026-11-02.json)。这是合成项目验收，不是 C5。

## 适用边界与剩余工作

- 本轮只支持当前 macOS 系统工具与 Python 运行时。Node/npm、Go、Rust、浏览器、开发服务、依赖安装和其他 OS 未验收；禁止为方便运行而扩大到用户 HOME 或所有文件读取。
- 运行时白名单中的文件可被命令读取，其中不得存放应用源码或凭据；应用源码与运行时发生重叠时启动策略拒绝。元数据、操作系统和实际工作区路径仍可能可见，这是访问隔离，不是虚拟文件系统或路径隐藏。
- 同一主机账户主动改写文件、并发替换工作区、复制源码、修改 Python/系统运行时不在当前威胁模型内；已有硬链接被拒，但不会识别任意语义相同的源码副本。工作区扫描有随文件数增长的成本。
- 全产品统一入口策略、虚拟 `/workspace`、模型请求出口的路径检查仍属于《源码保护与工作区隔离方案》的后续工作，不能由本轮执行探针推定完成。
- 审批授权/`process write`、命令出网授权和真实项目回归继续独立实施。保持 `CHAGENT_CODE_MODE=off`，不以本轮 C2 证据替代全部开放门槛。


回归记录：本机完整 unittest 519 项通过、0 跳过，55.517 秒；随后隔离专项 7 项通过、0 跳过。统一入口与文件身份、scratch 会话归属的下一步只在[实施计划](实施计划.md)维护。

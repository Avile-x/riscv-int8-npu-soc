# ADR-0001：仓库与工具链选择

- 日期：2026-10-27
- 状态：定了，不再改（除非有明确理由）

## 背景

想在家里这台机器上做 RTL，需要一个能真跑的仿真 + 综合闭环。机器上已经有 Git、Python、Icarus 和 Questa，但没有 Yosys / Verilator。WSL2 里的 Ubuntu 26.04.1 是装好的，apt 里有 Verilator 5.032 和 Yosys 0.52。

## 选项

| 选项 | 优点 | 缺点 |
|---|---|---|
| A. WSL Ubuntu 里跑 Verilator + Yosys | 仿真和综合都是自由软件；RTL 命令全在 Linux 路径下，跟以后跑脚本一致 | 得进 WSL；WSL 里只有 `python3`，没有 `python` 别名 |
| B. Windows 上用 Icarus / Questa + 另装 Yosys | 复用现成工具 | 综合还得装；路径和教程里写的不一样，容易来回踩坑 |

## 决定

选 A。

- 仓库放在 `D:\Desktop\riscv-npu-soc`，WSL 里是 `/mnt/d/Desktop/riscv-npu-soc`，单独一个 git 仓库，不塞进别的目录。
- 执行环境：WSL2 Ubuntu 26.04.1 LTS（x86_64），bash。
- 仿真：Verilator 5.032（二态）；四态用 Windows 那边的 Icarus 14.0，走 `wslpath` 转路径。
- 综合：Yosys 0.52。
- 脚本解释器：`python3` 3.14.4。
- 板卡厂商工具先不装，FPGA 那步再说。

## 理由和后果

- 理由：一条命令能从头跑到尾，不用在 Windows 和 WSL 之间倒腾文件；S0 不需要板卡和 license。
- 后果：以后所有 RTL 命令默认在 WSL 里执行，README 里写的命令都得带 `python3`。
- 验证：实际版本记在 `env/tool_versions.txt`，改工具版本要顺手更新这个文件。

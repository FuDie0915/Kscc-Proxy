#!/usr/bin/env bash
# ============================================================
#  KSCC Proxy 启动脚本 (git-bash / WSL)
#  用法:  bash start.sh                (默认配置)
#         ./start.sh --port 9000       (需先 chmod +x start.sh)
#         bash start.sh --host 0.0.0.0 --port 9000
#  自动切到仓库根(本脚本上一级),在那里 `python -m kscc_proxy`。
#  与机器无关:不依赖盘符/绝对路径,只要 scripts/ 与 kscc_proxy/
#  同在仓库根即可,clone 到任意位置都能跑。
#  配置缺失(kscc_proxy/config/kscc_proxy.json 不存在,或 kscc_token /
#  kscc_base_url 未填)时,会在终端交互式引导填写并写回。
#  首次运行前先装依赖:
#      pip install -r kscc_proxy/requirements.txt
# ============================================================

# 切到仓库根(本脚本所在 scripts/ 的上一级),以便 python -m kscc_proxy 能找到包
cd "$(dirname "$0")/.."

python -m kscc_proxy --config "kscc_proxy/config/kscc_proxy.json" "$@"

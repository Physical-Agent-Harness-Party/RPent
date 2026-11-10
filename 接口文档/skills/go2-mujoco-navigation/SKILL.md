---
name: go2-mujoco-navigation
description: 机器狗在 MuJoCo 中导航行走的完整流程（通过局域网 HTTP，默认服务器 192.168.104.191）：理解用户“去哪里”的指令 → 读仿真中机器狗的当前位置 → 调用 A* 规划服务（8765）得到路线 → 交给仿真控制服务（8770）让 Go2 沿路线走过去 → 汇报结果，没到达时重新规划。用户说“让狗去广告牌”“导航到厕所通道”“先去 A 再去 B 然后回来”“走到 (2,3)”“往前走 2 米”“在仿真里巡一圈”时使用。
---

# 机器狗在 MuJoCo 中导航行走

本流程把两个技能串起来，细节分别以它们为准：

- `astar-path-planning`：把用户的说法翻译成规划请求，调用 A* 规划服务，解读返回的路线；
- `mujoco-path-following`：调用仿真控制服务，启动仿真器、读位姿、让机器狗沿路线行走、查询进度和结果。

两个服务都在服务器上，本流程可以在局域网内任何一台电脑上执行，只需要 `curl`：

```bash
H=${ROBOT_HOST:-192.168.104.191}     # 服务器 IP；用户告知变化或设置了 ROBOT_HOST 时以它为准
A=http://$H:8765                     # A* 规划服务
S=http://$H:8770                     # 仿真控制服务
```

curl 一律加 `--noproxy '*'`。Windows 上用 `curl.exe`，JSON 放进文件用 `--data-binary @文件` 发送。

## 流程

### 1. 理解指令

从用户的话里提取**有序的目标列表**和**可选的到达朝向**。每个目标是点位名称/编号/语义标签/场景图物体（字符串），或坐标 `{"x":…, "y":…, "yaw": 可选}`。
翻译规则见 `astar-path-planning` 的“第二步”。要点：

- 先查点位表 `curl -s --noproxy '*' $A/waypoints`，对照名称和语义标签确定每个目标；
- 对不上或对上多个时，列出候选请用户选，不要猜；
- “往前走 2 米”这类相对移动，要先读第 3 步的位姿再换算成坐标；
- 用户没说“回来”就不要自动加返回充电桩。

### 2. 检查两个服务

```bash
curl -s --noproxy '*' $A/health
curl -s --noproxy '*' $S/health
```

- 任何一个连不上：告诉用户哪个服务（地址、端口）不通，需要在服务器上处理，不要反复重试；
- 两边的 `map_id`、`map_version` 必须一致，否则坐标对不上，告诉用户；
- 仿真服务的 `simulator_running` 为 false：`curl -s --noproxy '*' -X POST $S/simulator/start`；
- `pose.fallen` 为 true：告诉用户机器狗倒了，同意后 `-d '{"restart": true}'` 重启（机器狗回到原点）；
- `walk.state` 为 `running`：上一次行走还没结束，问用户是等待还是停止；
- `other_controllers` 不为空：有别的程序在控制机器狗，问用户能否停掉（`POST $S/controllers/stop_others`，之后重启仿真器）；
- `warnings` 原样转告用户。

### 3. 读当前位姿

```bash
POSE=$(curl -s --noproxy '*' $S/pose); echo "$POSE"
# {"ok": true, "x": 0.58, "y": 1.59, "yaw": 0.64, "tilt_deg": 0.5, "fallen": false}
```

每次规划前都要重新读：机器狗站着时也会慢慢滑动，上一次的终点坐标不可靠。

### 4. 规划路线

```bash
curl -s --noproxy '*' -X POST $A/plan -H 'Content-Type: application/json' \
     -d "{\"start\": $POSE, \"goals\": [\"厕所通道\", \"广告牌\"]}" > nav_plan.json
cat nav_plan.json
```

`/pose` 的返回可以直接作为 `start`（多余字段会被忽略）。坐标目标直接写进 `goals`，如 `[{"x": 2, "y": 3}]`。

- `ok` 为 false：按 `astar-path-planning` 第四步处理，不要进入行走；
- `ok` 为 true：向用户简要说明路线——依次去哪些地方、全程多长、预计用时（全程 ÷ 0.18 m/s + 每段约 10 秒）、`warnings`。
  然后直接开始走；用户事先要求“先给我看路线”时，等用户确认。

### 5.（可选）预演

路线较长、经过狭窄处，或用户想先看效果时：

```bash
curl -s --noproxy '*' -X POST $S/preview --data-binary @nav_plan.json
```

`ok` 为 false 时不要在仿真器里走，看 `log` 找原因；需要时按返回的 `image` 地址下载轨迹图。

### 6. 行走并跟踪进度

```bash
curl -s --noproxy '*' -X POST $S/walk --data-binary @nav_plan.json
# 之后每 5–10 秒：
curl -s --noproxy '*' $S/walk
```

直到 `state` 为 `finished`。行走中可以把 `last_status` 里的位置简要告诉用户。用户要求停下时 `POST $S/walk/stop`。

### 7. 判读结果并汇报

看 `GET $S/walk` 返回中的 `result`、`log`、`fell` 和 `pose`：

- `result.ok` 为 true：告诉用户依次到达了哪些地方、各自的位置误差和用时、机器狗当前位置。
- 有段 `reached` 为 false（日志里有 `[跳过]`）：回到第 3 步重新读位姿，以当前位置为起点、只包含**还没到达的目标**重新规划，再走一次；同一目标连续失败两次就停下，把 `[跳过]` 的原因和当前位置告诉用户。
- `fell` 为 true：告诉用户机器狗倒了，经同意后重启仿真器（机器狗回到原点），从第 3 步重来。

## 示例

用户：“让狗先去厕所通道，再去广告牌。”

1. 点位表里有 `wp_02 厕所通道`、`wp_01 广告牌`，`goals = ["厕所通道", "广告牌"]`；
2. 两个服务的地图都是 `go2_room_selfbuilt/2026-11-09.2`，仿真器在运行，没有其他控制程序；
3. 位姿 `(0.00, 0.00, 0.00)`；
4. 规划：wp_02 4.6 m、wp_01 5.7 m，全程 10.3 m，预计约 80 秒，无提示；
5. `POST /walk`，每 10 秒查一次进度；
6. 结果：厕所通道误差 0.05 m、用时 30 s；广告牌误差 0.12 m、用时 35 s；当前位置约 (4.6, 6.8)。

用户：“往前走 2 米。”

读位姿得 `(x, y, yaw)`，目标为 `{"x": x + 2·cos(yaw), "y": y + 2·sin(yaw), "yaw": yaw}`（保持朝向），规划后行走。

用户：“走到 (2, 3)，然后回充电桩。”

`goals = [{"x": 2, "y": 3}, "wp_00"]`。

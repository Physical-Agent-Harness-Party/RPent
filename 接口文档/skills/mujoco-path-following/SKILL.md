---
name: mujoco-path-following
description: 通过局域网 HTTP 调用仿真控制服务（默认 192.168.104.191:8770），在 unitree_mujoco 仿真器里让 Go2 机器狗沿一条路线（A* 规划服务的返回，或一串地图坐标）行走。包括查询状态和位姿、启动/重启仿真器、不开窗口预演、开始行走、查询进度、停止、判读结果。用户说“让狗沿这条路线走”“按这些坐标点走”“在 MuJoCo 里跑一下”“机器狗现在在哪”“重启仿真”时使用。
---

# 在 MuJoCo 仿真中按路线控制 Go2 行走

本技能只管“沿给定的路线走”，不管路线从哪来。路线由 `astar-path-planning` 技能生成；两者串起来的完整流程见 `go2-mujoco-navigation` 技能。

仿真器和行走控制程序运行在服务器上（它们之间的通信只能在同一台电脑内进行），服务器把这些操作做成了 HTTP 接口。
本技能可以在局域网内任何一台电脑上使用，只需要 `curl`。

## 服务器地址

| 服务 | 地址 |
|---|---|
| 仿真控制服务（本技能） | `http://192.168.104.191:8770` |
| A* 规划服务（`astar-path-planning` 用） | `http://192.168.104.191:8765` |

用户告诉你服务器 IP 变了，或环境变量 `ROBOT_HOST` 已设置时，以它为准：

```bash
H=${ROBOT_HOST:-192.168.104.191}
S=http://$H:8770
```

curl 一律加 `--noproxy '*'`（避开系统代理）。Windows 上用 `curl.exe`，JSON 请求体放进文件后用 `--data-binary @文件` 发送。

坐标全部是地图坐标系（米、弧度；原点为充电桩），与 A* 服务相同，A* 返回的坐标可以直接使用。

## 接口速查

| 操作 | 命令 |
|---|---|
| 查状态 | `curl -s --noproxy '*' $S/health` |
| 读位姿 | `curl -s --noproxy '*' $S/pose` |
| 启动仿真器 | `curl -s --noproxy '*' -X POST $S/simulator/start` |
| 重启仿真器（机器狗回原点） | `curl -s --noproxy '*' -X POST $S/simulator/start -d '{"restart": true}'` |
| 关闭仿真器 | `curl -s --noproxy '*' -X POST $S/simulator/stop` |
| 按 A* 结果行走 | `curl -s --noproxy '*' -X POST $S/walk --data-binary @nav_plan.json` |
| 按坐标行走 | `curl -s --noproxy '*' -X POST $S/walk -d '{"points": [[0,0],[2,1],[2,4]], "final_yaw": 1.57}'` |
| 查进度 | `curl -s --noproxy '*' $S/walk` |
| 停止行走 | `curl -s --noproxy '*' -X POST $S/walk/stop` |
| 预演（不开窗口） | `curl -s --noproxy '*' -X POST $S/preview --data-binary @nav_plan.json` |
| 下载预演轨迹图 | `curl -s --noproxy '*' -o preview.png $S/files/<预演返回的文件名>.png` |
| 停掉遗留控制程序 | `curl -s --noproxy '*' -X POST $S/controllers/stop_others` |

所有返回都是 JSON，以 `ok` 判断成败，失败时 `error` 是中文原因。HTTP 409 表示当前状态不允许（仿真器没运行、正在行走等）。

## 标准流程

### 1. 查状态

```bash
curl -s --noproxy '*' $S/health
```

逐项检查：

| 字段 | 期望 | 不符合时 |
|---|---|---|
| `map_id`、`map_version` | 与 A* 服务 `/health` 一致 | 坐标对不上，不要行走，告诉用户（换地图需在服务器上操作，见文末） |
| `simulator_running` | true | 第 2 步启动 |
| `pose.fallen` | false | 机器狗倒了，重启仿真器 |
| `walk.state` | 不是 `running` | 上一次行走还没完，等它结束或停止它 |
| `other_controllers` | 空 | 有别的程序在控制机器狗，两者同时发指令会让机器狗倒着走、原地打转。问用户能否停掉；同意后 `POST /controllers/stop_others`，再重启仿真器 |
| `warnings` | 空 | 原样转告用户 |

### 2. 启动仿真器（没有运行时）

```bash
curl -s --noproxy '*' -X POST $S/simulator/start
# {"ok": true, "message": "仿真器已启动，机器狗在地图原点", "pid": ..., "pose": {...}}
```

- 约 5 秒返回。仿真器窗口显示在服务器的桌面上，用户可以在那台电脑前观看。
- 每次启动机器狗都在地图原点、朝 +x；第一次行走时会先站起来（约 3 秒）。
- 返回 500 且提到窗口打不开或显存：服务器没有登录桌面，或显卡显存被占满，需要用户在服务器上处理。
- 机器狗倒了只能重启：`-d '{"restart": true}'`。重启会把机器狗放回原点，先告诉用户。

### 3. 读位姿

```bash
curl -s --noproxy '*' $S/pose
# {"ok": true, "x": 2.65, "y": 6.597, "yaw": 0.1456, "tilt_deg": 0.6, "fallen": false}
```

规划和行走前**每次都重新读**：机器狗站着不动时也会慢慢向后滑（约 1 cm/s），上一次的终点坐标不可靠。

### 4.（可选）预演

在服务器上用纯 MuJoCo 把同一条路线快速跑一遍，不影响仿真器，适合先确认路线走得通：

```bash
curl -s --noproxy '*' -X POST $S/preview --data-binary @nav_plan.json
# 也可以 -d '{"points": [[0,0],[2,1]], "spawn": [0, 0, 0]}'；不给 spawn 时从仿真中机器狗的当前位姿出发
```

返回 `ok`、每段结果 `result`、日志 `log`、轨迹图地址 `image`。用时约为实际行走的十分之一，长路线可能要几十秒，curl 不要设太短的超时。
需要看轨迹图时用 `curl -o` 下载后查看：深灰为障碍、红线为实际轨迹、绿点为各段终点。预演不到达就不要在仿真器里走。

### 5. 开始行走

请求体三选一：

| 请求体 | 说明 |
|---|---|
| A* 服务 `/plan` 的完整返回（文件 `--data-binary @nav_plan.json`，或包成 `{"plan": ...}`） | 按 `legs` 逐段走，每段终点转到目标点位的朝向；会核对地图版本 |
| `{"points": [[x, y], ...], "final_yaw": 可选}` | 整条折线算一段；不给 `final_yaw` 则到达后保持行进方向 |
| 以上任一种再加 `"force": true` | 路线穿过障碍也照走（默认拒绝）。不要随便加，手写坐标被拒绝时改用 A* 规划 |

- 路线第一个点离机器狗不超过 0.3 m 时从当前位置直接开始；更远时先走到第一个点（直线有障碍会用 A* 绕过去）。
- 成功立即返回 `{"ok": true, "walk_id": "...", ...}`，行走在后台进行。
- 被拒绝时 `error` 会说明原因：仿真器没运行、机器狗倒了、上一次行走没结束、有其他控制程序、路线穿墙、地图版本不一致。

### 6. 查进度直到结束

```bash
curl -s --noproxy '*' $S/walk
```

每 5–10 秒查一次，直到 `state` 为 `finished`。预计用时：全程长度 ÷ 0.18 m/s，再加每段 5–10 秒。

| 字段 | 含义 |
|---|---|
| `state` | `running` / `finished`。`result` 出现后还要站稳约 3 秒才变成 `finished` |
| `last_status` | 最近状态：`t= 14.0s [follow] pos=(-0.18, +1.28) yaw= +92.7 …`，`follow` 行走、`align` 原地转向、`pause` 停顿 |
| `log` | 关键事件：`[出发]`、`[到达] … 位置误差 0.05 m`、`[跳过] … 20 秒没有进展`（卡住，放弃该段并继续下一段）、`[摔倒]` |
| `result.ok` | 每段都到达为 true |
| `result.legs[i]` | `reached`、`position_error`（米，通常 0.05–0.12）、`yaw_error`（弧度）、`time`（秒） |
| `fell` | 摔倒了 |
| `pose` | 当前位姿 |

需要中途停下（用户要求、或明显走偏）：`POST /walk/stop`，机器狗会站稳。

## 服务器端维护（需要登录服务器 192.168.104.191，告诉用户去做）

服务是用户级 systemd 服务 `robot-sim-service`（代码 `/home/oc/projects/robot/slam_map_demo/scripts/sim_service.py`）：

- 状态 / 重启 / 日志：`systemctl --user status|restart robot-sim-service`，`journalctl --user -u robot-sim-service -f`；
- 换地图：改 `/home/oc/projects/robot/slam_map_demo/systemd/robot-sim-service.service` 中的地图包路径，`systemctl --user daemon-reload && systemctl --user restart robot-sim-service`，A* 服务也换成同一张地图；
- 仿真器窗口需要服务器已登录桌面；
- 完整接口说明：服务器上 `/home/oc/projects/robot/slam_map_demo/仿真控制服务接口文档.md`。

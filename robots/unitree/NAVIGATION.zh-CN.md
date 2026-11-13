# 导航路径规划服务接口文档

本文是外部名称点位规划服务的接口与部署参考。IP、服务器路径、地图版本和点位坐标均取自示例环境，实际值以服务返回为准。
RPent 的运行方式、单目标执行及到达验收约束见 [Unitree 使用说明](README.md)。
RPent 还可以发送 `{x, y, yaw?}` 坐标目标，但规划服务须提供相应能力；下文描述名称点位接口。

## 概述

本服务根据机器狗**当前所在位置**和一个或多个**目标点位名称**，在地图上规划一条避开障碍的行走路线，
以 JSON 格式返回。给出多个目标时，按先后顺序依次前往，每两个相邻目标之间单独规划一段。

- 规划算法：A* 栅格搜索，再把栅格路径拉直成少量拐点组成的折线；路线尽量走在通道中间，离障碍至少 0.3 m。
- 所用地图：与 MuJoCo 仿真巡航相同的导航栅格（地图包中的 `mujoco/collision_hfield.npz`），
  点位来自地图包中的 `waypoints.yaml`。
- 访问方式：HTTP 和 WebSocket 两种，同时提供，局域网内任何设备都可调用；功能完全相同，按习惯任选一种。
- 代码：`slam_map_demo/scripts/nav_service.py`。

本服务**只做规划，不控制机器狗**：它返回路线，由调用方的控制程序让机器狗沿路线行走。

## 坐标系约定

请求和返回中的所有坐标都使用**地图坐标系**：

| 项目 | 约定 |
|---|---|
| 原点 | 地图包选定的原点（room 地图即充电桩点位 `wp_00` 所在位置） |
| x 轴、y 轴 | 水平面内，x 轴为地图的正前方，y 轴为 x 轴逆时针转 90° |
| z 轴 | 竖直向上，地面 z = 0 |
| 长度单位 | 米 |
| 角度单位 | 弧度，以 +x 方向为 0，逆时针为正，范围 -π ~ π（约 -3.14 ~ 3.14） |

请求里的“当前位置”必须是**同一张地图、同一坐标系**下的位置，例如来自在这张地图上运行的激光定位。
地图重建或坐标原点变化后，点位和定位结果都要随之更新。

以下为地图 `go2_room_selfbuilt/2026-11-06.1` 的 4 个点位示例，实际地图与点位以服务返回为准：

| 点位编号 | 名称 | 语义标签 | 站位 (x, y) | 到达后朝向 |
|---|---|---|---|---|
| wp_00 | 充电桩（地图原点） | dock | (0.000, 0.000) | 0.000 |
| wp_01 | 门口墙壁 | doorway, wall | (2.847, -0.219) | -1.586 |
| wp_02 | 广告牌 | billboard | (0.199, 6.265) | 2.637 |
| wp_03 | 厕所通道 | restroom, corridor | (2.515, 2.922) | 0.086 |

以服务实际返回为准，可随时通过“查询点位列表”接口查看。

---

## 启动与停止服务

### 运行环境

- 运行服务的电脑：当前为 `192.168.104.191`（如 IP 变化，用 `hostname -I` 查看）。
- Python 环境：`/home/oc/miniconda3/envs/unitree-mujoco/bin/python`，需要 numpy、scipy、PyYAML、websockets（均已安装）。
- 地图包里需要有 `meta.yaml`、`waypoints.yaml` 和 `mujoco/collision_hfield.npz`（由 `map_to_mujoco.py` 生成）。

### 启动

在 `slam_map_demo` 目录下执行：

```bash
P=/home/oc/miniconda3/envs/unitree-mujoco/bin/python
$P -I -u scripts/nav_service.py serve maps/go2_room_selfbuilt/2026-11-06.1
```

启动成功后显示：

```
地图 go2_room_selfbuilt/2026-11-06.1，4 个点位：充电桩（地图原点）、门口墙壁、广告牌、厕所通道
HTTP      http://0.0.0.0:8765  （/health、/waypoints、/plan）
WebSocket ws://0.0.0.0:8766
```

`0.0.0.0` 表示监听本机所有网卡，局域网设备用本机的局域网 IP 访问。

可选参数：

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--host` | `0.0.0.0` | 监听地址；改为 `127.0.0.1` 则只允许本机访问 |
| `--http-port` | `8765` | HTTP 端口 |
| `--ws-port` | `8766` | WebSocket 端口 |

关闭终端后仍要继续运行时，用后台方式启动，日志写入文件：

```bash
nohup $P -I -u scripts/nav_service.py serve maps/go2_room_selfbuilt/2026-11-06.1 > /tmp/nav_service.log 2>&1 &
```

### 停止

- 前台运行时按 `Ctrl+C`；
- 后台运行时执行 `pkill -f "^/home/oc/miniconda3/envs/unitree-mujoco/bin/python.*nav_service.py serve"`
  （匹配完整的启动命令，避免误停其他命令行中恰好含有这段文字的进程）。

### 修改点位或地图后

服务只在启动时读取一次点位和地图。修改了 `waypoints.yaml` 或重新生成了导航栅格后，**需要重启服务**才会生效。

### 局域网访问不通时

1. 在运行服务的电脑上先测 `curl http://127.0.0.1:8765/health`，确认服务本身正常；
2. 再在另一台设备上测 `curl http://192.168.104.191:8765/health`；
3. 本机通、其他设备不通时，检查两台设备是否在同一网段，以及运行服务的电脑防火墙是否放行 8765、8766 端口
   （例如 `sudo ufw allow 8765/tcp` 和 `sudo ufw allow 8766/tcp`）。

### 命令行直接规划（不启动服务）

调试时可以不启动服务，直接规划一次并打印 JSON，返回内容与接口完全相同：

```bash
$P -I scripts/nav_service.py plan maps/go2_room_selfbuilt/2026-11-06.1 \
    --start 0 0 [--yaw 0] --goals 广告牌 厕所通道 [--step 0.2]
```

规划成功时退出码为 0，失败时为 1。

---

## 接口总览

| 方式 | 地址 | 作用 |
|---|---|---|
| HTTP GET | `http://192.168.104.191:8765/health` | 检查服务是否正常 |
| HTTP GET | `http://192.168.104.191:8765/waypoints` | 查询点位列表 |
| HTTP GET | `http://192.168.104.191:8765/plan?...` | 路径规划，参数写在网址里 |
| HTTP POST | `http://192.168.104.191:8765/plan` | 路径规划，参数以 JSON 放在请求体里 |
| WebSocket | `ws://192.168.104.191:8766` | 以上三种功能，通过消息中的 `type` 字段区分 |

所有 HTTP 返回都是 JSON（`Content-Type: application/json; charset=utf-8`），并允许跨域访问，网页可以直接调用。

HTTP 状态码：

| 状态码 | 含义 |
|---|---|
| 200 | 成功，返回体中 `ok` 为 `true` |
| 400 | 请求有误或规划失败（名称找不到、目标不可达、参数格式错误等），返回体中 `ok` 为 `false` |
| 404 | 访问了不存在的地址 |

无论成功失败，都应以返回体中的 `ok` 字段为准判断结果。

---

## 检查服务状态

```bash
curl http://192.168.104.191:8765/health
```

返回：

```json
{"ok": true, "map_id": "go2_room_selfbuilt", "map_version": "2026-11-06.1", "waypoints": 4}
```

| 字段 | 类型 | 含义 |
|---|---|---|
| `ok` | 布尔 | 服务正常时为 `true` |
| `map_id` | 字符串 | 当前加载的地图名称 |
| `map_version` | 字符串 | 当前加载的地图版本 |
| `waypoints` | 整数 | 点位数量（含充电桩） |

## 查询点位列表

```bash
curl http://192.168.104.191:8765/waypoints
```

返回（节选）：

```json
{
  "ok": true,
  "map_id": "go2_room_selfbuilt",
  "map_version": "2026-11-06.1",
  "waypoints": [
    {
      "id": "wp_02",
      "name": "广告牌",
      "semantic_tags": ["billboard"],
      "stand_pose": {"x": 0.199, "y": 6.265, "z": 0.0, "yaw": 2.637},
      "lookat": {"x": -0.951, "y": 6.9, "z": 0.5}
    }
  ]
}
```

`waypoints` 中每个点位的字段：

| 字段 | 类型 | 含义 |
|---|---|---|
| `id` | 字符串 | 点位编号，如 `wp_02`，在一张地图内唯一 |
| `name` | 字符串 | 点位名称，如“广告牌” |
| `semantic_tags` | 字符串数组 | 语义标签，如 `["billboard"]`，可能为空 |
| `stand_pose` | 对象 | 站位：机器狗到达该点位时应站的位置和朝向 |
| `stand_pose.x`、`stand_pose.y` | 数字 | 站位坐标（米） |
| `stand_pose.z` | 数字 | 站位高度，地面上恒为 0 |
| `stand_pose.yaw` | 数字 | 到达后应朝向的方向（弧度），即面向 `lookat` 的方向 |
| `lookat` | 对象或 `null` | 到达后要看向的位置（米），例如广告牌本身；充电桩没有，为 `null` |

---

## 路径规划

### 请求方式一：GET（参数写在网址里）

```bash
curl "http://192.168.104.191:8765/plan?x=0&y=0&goals=广告牌,厕所通道&step=0.2"
```

| 参数 | 必填 | 类型 | 含义 |
|---|---|---|---|
| `x` | 是 | 数字 | 当前位置 x 坐标（米） |
| `y` | 是 | 数字 | 当前位置 y 坐标（米） |
| `yaw` | 否 | 数字 | 当前朝向（弧度）。目前不参与规划，只在返回中原样带回 |
| `goals` | 是 | 字符串 | 目标点位名称，多个用逗号分隔（中英文逗号均可），按先后顺序前往 |
| `step` | 否 | 数字 | 大于 0 时，返回中额外给出按此间距（米）加密的稠密路径点 `dense_path` |

适合手动测试，直接粘到浏览器地址栏也能看到结果。注意：

- 点位名称本身含逗号时无法用这种方式传递，请改用 POST；
- 网址里的中文可以直接写（如上例），也可以先做网址编码（“广告牌”编码后为 `%E5%B9%BF%E5%91%8A%E7%89%8C`），服务两种都能识别。
  按规范应编码，用程序拼网址时建议用标准库函数编码（如 Python 的 `urllib.parse.urlencode`）。

### 请求方式二：POST（参数以 JSON 放在请求体里）

```bash
curl -X POST http://192.168.104.191:8765/plan \
     -H 'Content-Type: application/json' \
     -d '{"start": {"x": 1.0, "y": 1.5, "yaw": 0}, "goals": ["广告牌", "厕所通道"], "step": 1.0}'
```

| 字段 | 必填 | 类型 | 含义 |
|---|---|---|---|
| `start` | 是 | 对象 | 当前位置 |
| `start.x` | 是 | 数字 | 当前位置 x 坐标（米） |
| `start.y` | 是 | 数字 | 当前位置 y 坐标（米） |
| `start.yaw` | 否 | 数字 | 当前朝向（弧度）。目前不参与规划，只在返回中原样带回 |
| `goals` | 是 | 字符串数组 | 目标点位名称列表，按先后顺序前往，至少一个。也接受逗号分隔的单个字符串 |
| `step` | 否 | 数字 | 大于 0 时，返回中额外给出按此间距（米）加密的稠密路径点 `dense_path`；不填、为 0 或负数时不返回 |

适合程序对接。两种方式的规划结果和返回格式完全相同。

### 目标名称的匹配规则

`goals` 中的每个名称按以下顺序匹配点位，在某一级找到唯一结果就停止：

1. 点位编号完全相同，如 `wp_02`（不区分大小写）；
2. 点位名称完全相同，如 `广告牌`；
3. 某个语义标签相同，如 `billboard`、`restroom`（不区分大小写）；
4. 点位名称包含该文字，如 `厕` 匹配“厕所通道”，`门口` 匹配“门口墙壁”。

同一级匹配到多个点位时返回失败，并列出这几个候选，请写得更具体；所有级别都匹配不到时也返回失败。
**只要有一个名称匹配失败，整个请求都不做规划。**

同一个点位可以重复出现，例如 `["广告牌", "厕所通道", "广告牌"]` 表示去广告牌、再去厕所通道、再回广告牌。

### 成功返回示例

以上面 POST 请求为例（起点 (1.0, 1.5)，先去广告牌、再去厕所通道，加密间距 1.0 m）：

```json
{
  "ok": true,
  "map_id": "go2_room_selfbuilt",
  "map_version": "2026-11-06.1",
  "frame": "map",
  "units": {"length": "meter", "angle": "radian"},
  "start": {"x": 1.0, "y": 1.5, "yaw": 0.0},
  "goals": ["wp_02", "wp_03"],
  "total_length": 8.954,
  "legs": [
    {
      "query": "广告牌",
      "goal": {
        "id": "wp_02",
        "name": "广告牌",
        "semantic_tags": ["billboard"],
        "stand_pose": {"x": 0.199, "y": 6.265, "z": 0.0, "yaw": 2.637},
        "lookat": {"x": -0.951, "y": 6.9, "z": 0.5}
      },
      "from": {"x": 1.0, "y": 1.5},
      "length": 4.887,
      "path": [[1.0, 1.5], [0.8, 1.7], [0.199, 6.265]],
      "headings": [2.3562, 1.7017],
      "dense_path": [[1.0, 1.5], [0.8, 1.7], [0.68, 2.613], [0.56, 3.526],
                     [0.439, 4.439], [0.319, 5.352], [0.199, 6.265]]
    },
    {
      "query": "厕所通道",
      "goal": {
        "id": "wp_03",
        "name": "厕所通道",
        "semantic_tags": ["restroom", "corridor"],
        "stand_pose": {"x": 2.515, "y": 2.922, "z": 0.0, "yaw": 0.086},
        "lookat": {"x": 3.868, "y": 3.039, "z": 0.5}
      },
      "from": {"x": 0.199, "y": 6.265},
      "length": 4.067,
      "path": [[0.199, 6.265], [2.515, 2.922]],
      "headings": [-0.9649],
      "dense_path": [[0.199, 6.265], [0.662, 5.596], [1.125, 4.928],
                     [1.589, 4.259], [2.052, 3.591], [2.515, 2.922]]
    }
  ],
  "warnings": ["起点离障碍仅 0.18 m，前往 wp_02 时先直线移到最近的可通行处"],
  "planning_time_ms": 77.1
}
```

### 返回字段说明

顶层字段：

| 字段 | 类型 | 含义 |
|---|---|---|
| `ok` | 布尔 | 规划成功为 `true` |
| `map_id` | 字符串 | 地图名称 |
| `map_version` | 字符串 | 地图版本。调用方可据此确认自己用的定位坐标和服务是同一版地图 |
| `frame` | 字符串 | 坐标系，固定为 `map`（地图坐标系） |
| `units` | 对象 | 单位：长度为米（`meter`），角度为弧度（`radian`） |
| `start` | 对象 | 请求中的起点，原样带回（`x`、`y`，以及请求中提供了的 `yaw`） |
| `goals` | 字符串数组 | 每个目标名称最终匹配到的点位编号，顺序与请求一致 |
| `total_length` | 数字 | 所有路段长度之和（米） |
| `legs` | 对象数组 | 路段列表，一个目标对应一段，顺序与 `goals` 一致，字段见下表 |
| `warnings` | 字符串数组 | 提示信息，没有时为空数组。目前会在起点离障碍过近或起点落在障碍、未扫描区域里时给出提示 |
| `planning_time_ms` | 数字 | 本次规划耗时（毫秒） |

`legs` 中每个路段的字段：

| 字段 | 类型 | 含义 |
|---|---|---|
| `query` | 字符串 | 请求中写的目标名称，原样带回，便于对照 |
| `goal` | 对象 | 该段的目标点位，字段与“查询点位列表”中的点位相同：`id`、`name`、`semantic_tags`、`stand_pose`、`lookat` |
| `goal.stand_pose.yaw` | 数字 | 走到终点后应转向的朝向（弧度），使机器狗面向 `lookat` |
| `from` | 对象 | 该段起点：第一段为请求中的起点，之后各段为上一个目标的站位 |
| `length` | 数字 | 该段折线长度（米） |
| `path` | 二维数组 | 该段路线的拐点，格式 `[[x, y], ...]`（米）。第一个点为该段起点，最后一个点为目标站位，相邻两点之间按直线行走 |
| `headings` | 数字数组 | 折线中每一小段的行进方向（弧度），第 i 个值是从 `path[i]` 走向 `path[i+1]` 的方向，个数比 `path` 少一个 |
| `dense_path` | 二维数组 | 仅在请求带 `step` 时出现：沿折线每隔约 `step` 米取一个点，格式同 `path`，并包含 `path` 中的所有拐点，适合控制程序逐点跟踪 |

### 失败返回

失败时 `ok` 为 `false`，HTTP 状态码为 400。

```json
{
  "ok": false,
  "error": "找不到名为「食堂」的点位",
  "available": ["wp_00 充电桩（地图原点）", "wp_01 门口墙壁", "wp_02 广告牌", "wp_03 厕所通道"]
}
```

| 字段 | 类型 | 含义 |
|---|---|---|
| `ok` | 布尔 | 固定为 `false` |
| `error` | 字符串 | 失败原因（中文） |
| `available` | 字符串数组 | 当前可用的点位（“编号 名称”）。名称错误、目标不可达等规划类错误时提供；请求格式错误时没有此字段 |

常见失败原因：

| `error` 内容 | 原因与处理 |
|---|---|
| 找不到名为「…」的点位 | 名称写错或该点位不存在，参照 `available` 修改 |
| 「…」匹配到多个点位：… | 名称太笼统，同时对上多个点位，写完整名称或编号 |
| 从 (x, y) 到 … 规划失败：目标不可达 | 起点在墙外、未扫描的零碎区域等与目标不连通的地方；通常是定位坐标有误或用了别的地图的坐标 |
| 缺少起点… / 需要数字参数 x、y… | 没有传起点，或坐标不是数字 |
| 至少需要一个目标名称 / 目标名称为空 | `goals` 为空 |
| 起点坐标不是有效数字 | 坐标为 NaN 或无穷大 |
| 请求体不是有效的 JSON… / 请求格式错误… | POST 请求体不是合法 JSON，或字段类型不对 |

---

## WebSocket 接口

地址：`ws://192.168.104.191:8766`

连接建立后可以连续发送多条消息，每条消息是一个 JSON 对象，服务对每条消息回复一个 JSON 对象。
适合需要频繁请求、不想每次重新建立连接的程序。

请求消息：

| 字段 | 必填 | 含义 |
|---|---|---|
| `type` | 否 | 请求类型：`plan`（路径规划，默认）、`waypoints`（点位列表）、`health`（服务状态） |
| `request_id` | 否 | 任意值，回复中原样带回，用于把回复和请求对应起来 |
| `start`、`goals`、`step` | `plan` 时必填前两项 | 与 HTTP POST 请求体中的同名字段完全相同 |

回复内容与 HTTP 对应接口的返回体完全相同；请求带了 `request_id` 时，回复中多一个同值的 `request_id` 字段。

示例：

```text
→ {"type": "plan", "request_id": 7, "start": {"x": 0, "y": 0}, "goals": ["广告牌", "门口墙壁"]}
← {"ok": true, "goals": ["wp_02", "wp_01"], "total_length": 13.445, "legs": [...], ..., "request_id": 7}

→ {"type": "waypoints"}
← {"ok": true, "map_id": "go2_room_selfbuilt", "map_version": "2026-11-06.1", "waypoints": [...]}
```

---

## 调用示例

### Python（HTTP，只用标准库）

```python
import json
import urllib.request

req = urllib.request.Request(
    "http://192.168.104.191:8765/plan",
    data=json.dumps({"start": {"x": 0, "y": 0}, "goals": ["广告牌", "厕所通道"], "step": 0.2}).encode(),
    headers={"Content-Type": "application/json"},
)
try:
    res = json.load(urllib.request.urlopen(req, timeout=10))
except urllib.error.HTTPError as e:   # 规划失败时状态码为 400，返回体仍是 JSON
    res = json.load(e)

if res["ok"]:
    for leg in res["legs"]:
        print(leg["goal"]["name"], leg["length"], leg["path"])
else:
    print("失败：", res["error"])
```

### Python（WebSocket）

```python
import asyncio
import json
from websockets.asyncio.client import connect

async def main():
    # proxy=None：websockets 15 及以后的版本默认走系统代理环境变量，局域网直连时要关掉
    async with connect("ws://192.168.104.191:8766", proxy=None) as ws:
        await ws.send(json.dumps({"type": "plan", "request_id": 1,
                                  "start": {"x": 0, "y": 0}, "goals": ["广告牌", "厕所通道"]},
                                 ensure_ascii=False))
        print(json.loads(await ws.recv()))

asyncio.run(main())
```

### 网页 JavaScript

```javascript
const res = await fetch("http://192.168.104.191:8765/plan", {
  method: "POST",
  headers: {"Content-Type": "application/json"},
  body: JSON.stringify({start: {x: 0, y: 0}, goals: ["广告牌", "厕所通道"]}),
}).then(r => r.json());
console.log(res.ok ? res.legs : res.error);
```

---

## 如何使用返回的路线

控制程序拿到规划结果后，建议按以下步骤让机器狗执行：

1. 检查 `ok`。为 `false` 时不要行走，根据 `error` 处理；
2. 检查 `map_version` 是否与定位所用地图一致，不一致说明坐标对不上，不要行走；
3. 若 `warnings` 不为空，记录下来。起点离障碍过近时，路线第一小段是一条脱离障碍的直线，应低速通过；
4. 按 `legs` 的顺序逐段执行。每一段内：
   - 依次走向 `path`（或 `dense_path`）中的每个点，`headings` 给出每一小段应保持的行进方向；
   - 走到最后一个点（目标站位）后，原地转向 `goal.stand_pose.yaw`；
   - 到达判定可参考点位库中的默认容差：位置误差不超过 0.15 m，朝向误差不超过 0.2 弧度（约 11°）；
5. 完成一段后，在该点位执行要做的任务（如拍照、播报），再进入下一段。

注意：

- 路线是按发起请求时的起点和静态地图规划的，不感知行人、临时摆放的物体等动态障碍。行走中被挡住或明显偏离路线时，
  应以当前定位位置为起点重新请求规划，并去掉已经到达的目标；
- 单次规划耗时通常在 0.03 ~ 0.4 秒之间，可以在行走中按需重新规划；
- 服务对每个请求独立处理，不记录机器狗状态，多台设备同时调用互不影响。

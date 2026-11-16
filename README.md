# OSM 道路图层批次入仓系统

面向城市 GIS 数据员的 Web 服务：把只含 `node` / `way` 的 OpenStreetMap 分区摘录
（`.osm`）和离线变更文件（`.osc`）按**批次**入仓到 SQLite，分析人员可按入仓批次
查看道路图层并下载当时的 GeoJSON 快照。

技术栈：Flask 3 + SQLite（WAL 模式）+ gunicorn，纯 Docker Compose 部署。

## 快速启动

```bash
docker compose up --build
# 打开 http://localhost:8000
```

数据库持久化在命名卷 `osm_batch_data` 中；`samples/` 以只读方式挂载到容器
`/samples`，可直接在网页上传试用。

本地无 Docker 时的开发启动：

```bash
pip install -r requirements.txt
python -m gunicorn --bind 0.0.0.0:8000 --threads 4 wsgi:app
# 或：flask --app wsgi run --debug
```

## 网页操作

1. 首页选择 `.osm` / `.osc` 文件上传，每个文件形成一个批次（自动编号）。
2. 批次列表显示状态（已提交 / 已拒绝 / 处理中）、本批对象版本数、拒绝原因。
3. 批次详情页显示：可见/已删除对象统计、可导出道路、待补全道路及缺失节点 id。
4. 点击「下载本批道路 GeoJSON」得到 `batch_<id>_highways.geojson`
   （GeoJSON FeatureCollection，几何为 LineString，坐标按 lon/lat 顺序）。
5. `GET /health` 健康检查；拒绝批次的导出返回 HTTP 409。

## 合并与版本规则

按对象 `(类型, id, version)` 合并，所有规则在单事务内整批生效：

| 情况 | 处理 |
| --- | --- |
| 版本号低于库中最高版本（旧版重传） | **静默忽略**，不能修改或恢复任何对象，包括已删除对象 |
| 同版本、内容完全相同 | 重复跳过（重复上传同一文件幂等） |
| 同版本、内容不同 | **整批拒绝**，回滚全部对象写入，批次留拒绝原因 |
| 更高版本、可见 | 成为对象当前状态 |
| 更高版本、不可见（delete） | 墓碑版本：对象撤下，版本历史永久保留 |
| 含 `relation` 等非 node/way 元素、XML 非法、字段缺失 | 整批拒绝 |

## 道路生成规则

- 仅带 `highway` 标签的可见 way 参与道路图层，按 `<nd>` 有序节点生成 LineString。
- way 引用的节点缺失（从未上传）或已删除 → **整条 way 列为待补全**，不进 GeoJSON；
  后续批次补齐可见节点后，在新批次导出中自动恢复。
- 共享节点被移动或删除时，即使引用它的 way 没有重传，所有相关道路的几何同步更新、
  或整条撤下；way 改引用（新版本换 `<nd>` 列表）后，旧节点不再影响它。

## 批次不可变与并发

- `object_versions` 是只追加事件表；某批次 B 的快照 = 所有 `batch_id <= B` 的每对象
  最高版本。后来补传的数据**绝不混入**历史批次：历史批次的节点坐标、way 引用、
  标签、待补全状态永远保持当时结果。
- 每批在一个 `BEGIN IMMEDIATE` 事务内完成对象写入与状态翻转；失败即 ROLLBACK，
  只留下标记 rejected 的批次审计行，不留半批。
- 进程重启时，启动钩子把残留 `processing` 批次的对象行清除并标记拒绝（崩溃恢复）。
- WAL + 只读快照查询：导出 GeoJSON 不开启写事务，导出期间新批次仍可提交。

## 样例文件（`samples/`，建议按编号顺序上传）

| 文件 | 场景 |
| --- | --- |
| `01_base_extract.osm` | 基础摘录：2 条 highway 道路 + 1 条非 highway way，含共享节点 n3 |
| `02_missing_node.osm` | **缺节点**：w102 引用未入仓的 n9 → 待补全；含同版重传 |
| `03_fill_gap_and_move_shared.osc` | 补齐 n9 道路恢复；**共享节点** n3 移动，两条路几何联动更新 |
| `04_delete_shared_node.osc` | 删除共享节点 n4（保留墓碑版本），w101 未重传仍被整条撤下 |
| `05_old_version_ignored.osc` | 旧版 n4 v1 重传被忽略、不能恢复对象；同版重复跳过 |
| `06_way_reroute.osc` | w101 改引用绕开 n4，旧节点不再影响它，道路恢复 |
| `07_out_of_order.osc` | **乱序变更**：way 先于节点、同批前向引用、同文件重复、同对象多版本 |
| `08_same_version_conflict.osc` | 同版异内容冲突，整批拒绝（同批合法 way 也回滚） |
| `09_recreate_node_higher_version.osc` | 只有更高版本才能重建已删除节点，墓碑历史保留 |
| `10_relation_rejected.osm` | 含 relation，整批拒绝 |

## 测试

```bash
pip install pytest
python -m pytest tests/ -v
```

23 个用例覆盖：版本合并全部规则、缺节点补全、共享节点联动、改线、乱序、
整批原子性、批次快照不可变、HTTP 上传/下载、并发写入与崩溃恢复。

## 目录结构

```
app/
  config.py       环境变量配置
  db.py           SQLite 连接、建表、重启恢复
  osm_parser.py   .osm/.osc 严格解析（defusedxml）
  ingestion.py    批次合并规则与单事务入仓
  roads.py        as-of 快照重建、待补全判定、GeoJSON 序列化
  web.py          Flask 路由
  templates/      中文网页模板
samples/          10 个场景样例
tests/            pytest 测试（23 用例）
wsgi.py           gunicorn 入口
Dockerfile / docker-compose.yml
```

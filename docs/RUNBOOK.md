# 运行手册 lineageguard

## 1 启动

```bash
docker compose up -d --build
curl -s localhost:18214/api/health
```

宿主端口 `18214`，容器内 `8000`。启动时如果库是空的，服务会自动把 `sample_data/` 里的样例跑一遍，页面打开即有数据。

## 2 截图与验收命令

```bash
docker compose config --quiet
docker compose exec -T app python -m pytest -q
```

## 3 主流程

1. 打开 `http://localhost:18214/`，看规格单的字段清单与采信诊断。
2. `/types` 比较两个类型写法，看读者 / 写者两向结论。
3. `/mapping` 看旧名到新名、别名 SQL、冲突项。
4. `/impact` 看受影响节点、环、断链、单边登记。
5. `/batches` 点「跑一次」，再点「陈旧提交」验证 409。
6. `/spec` 导出迁移单与三份账目 CSV。
7. `/archives` 导出当前批次归档，再在另一节点恢复，核对来源身份与本地顺序。

## 4 重建数据库

```bash
docker compose down -v
docker compose up -d --build
```

命名卷 `lineageguard-data` 挂着 `/data/lineageguard.sqlite`。删卷即清空批次、快照与隔离区。

## 5 接口

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/health` | 服务与库状态、当前批次号 |
| POST | `/api/schemas/import` | 采信样例文件，返回快照摘要与诊断 |
| GET | `/api/schemas/quarantine` | 隔离区 JSON |
| GET | `/api/schemas/quarantine.csv` | 隔离区 CSV |
| GET | `/api/types` | 类型格与别名表 |
| POST | `/api/types/compare` | 两个类型写法比较 |
| POST | `/api/compat` | 一对版本的两向判定 |
| GET | `/api/mapping` | 映射表、别名、冲突 |
| GET | `/api/impact` | 影响面、环、断链 |
| POST | `/api/runs` | 跑批次，可带基线版本号与生效区间 |
| GET | `/api/batches` | 批次历史 |
| GET | `/api/reconcile` | 四账对账 |
| GET | `/api/export/spec.md` | 迁移单 Markdown |
| GET | `/api/export/fields.csv` | 字段账 CSV |
| GET | `/api/export/refs.csv` | 引用账 CSV |
| GET | `/api/export/ledger.csv` | 账目 CSV |
| GET | `/api/export/manifest.json` | 运行清单 |
| GET | `/api/export/archive.json` | 按批次导出归档包（可带 `run_id`、`kind`） |
| GET | `/api/export/archive-manifest.json` | 归档摘要清单，不含包体 |
| POST | `/api/archives/restore` | 恢复归档包，包体直接作为请求体 |
| GET | `/api/archives` | 当前批次、导入批次、未完成导入与冲突 |
| POST | `/api/archives/replay` | 续跑停在半路的导入 |
| GET | `/api/archives/{run_id}` | 单个导入批次的来源与导入记录 |

## 6 失败处置

| 现象 | 原因 | 处置 |
|---|---|---|
| 409 `VERSION_STALE` | 提交带的基线序号落后 | 去掉 `base_version_seq` 或填当前序号 |
| 400 `WINDOW_INVERTED` | 生效区间起点不早于终点 | 检查 `effective_from` / `effective_to` |
| 400 `BAD_INPUT` | 文件名、参数或 JSON 结构不对 | 看返回的 `detail` |
| 404 `NOT_FOUND` | 还没跑过批次 | 先在 `/batches` 点「跑一次」 |
| 422 `ARCHIVE_REJECTED` | 归档包被摘要、分段或关联校验挡下 | 看 `detail.reason`，整包重导，坏包不要拆开重打包 |
| 409 `ARCHIVE_CONFLICT` | 同源同号但内容不同 | 到 `/archives` 看冲突记录，确认哪份是原证据 |
| 409 `IMPORT_INTERRUPTED` | 导入中断，批次不可见 | 用同一归档重放，或点「续跑未完成导入」 |

## 7 灾备：归档与恢复

```bash
# 导出当前批次（也可加 ?run_id= 指定批次）
curl -s localhost:18214/api/export/archive.json -o batch.archive.json
# 恢复（在灾备库上）
curl -s -X POST --data-binary @batch.archive.json \
     -H 'Content-Type: application/json' \
     localhost:18214/api/archives/restore
```

- 恢复保留来源身份与原版本号，另分配本地顺序；当前批次不被抢占。
- 同一归档重复恢复返回 `duplicate`，不新增行。
- 同源同号异内容隔离为冲突，不覆盖原记录。
- 坏包（被改过、缺段、摘要不符）整包拒绝，一行不写。
- 导入中断后批次不可见；重启或 `POST /api/archives/replay` 会续跑，沿用原预分配顺序。

## 8 端口与并发

本服务固定 18214。同时起多个栈时改 `docker-compose.yml` 的端口映射与 `lineageguard-data` 卷名，避免与本机其他项目抢端口、串数据。

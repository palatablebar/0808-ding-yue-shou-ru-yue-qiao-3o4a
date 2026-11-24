# 阶段接口

`POST /api/stages/<stage>` 接收 `multipart/form-data`。字段 `sample=dirty`、`sample=clean` 或 `file=<CSV>` 三选一。阶段为 `intake`、`prepare`、`calculate`、`policy`、`balance`。预览不入库。

`POST /api/runs` 接收同样的输入，执行全批并返回运行报告。`GET /api/runs` 返回最近 50 批。`GET /api/runs/<id>` 查询一批和当前复核状态。

`PATCH /api/runs/<id>` 接收 `revision`、`status` 和 `note`。状态使用 `APPROVED` 或 `HELD`。冲突返回 409，客户端读取新版本后重试。

`GET /api/runs/<id>/evidence` 下载内含运行清单、行号隔离、业务证据和复核信息的 JSON。

## 来源版本与补录

`POST /api/runs/<id>/supplements` 接收同样的 `multipart/form-data` 输入，把迟到来源记录挂到该批次所属谱系：

- 记录键已在前序批次采信、且内容有变化时，生成新版本批次，只重算受影响业务分组（`lineage.affected`），其余分组在 `cases[].provenance` 标记 `FROZEN` 并沿用旧结论；重算组标记 `RECALCULATED`。
- 记录键没有前序依据（含补录文件内的坏行）返回 409 `UNBASED_BACKFILL`，`blockers` 给出每条参与记录的源行号，不写入任何新版本、不改动已签发结论。
- 同一记录出现无法判定先后的矛盾副本返回 409 `VERSION_CONFLICT`，`blockers[].participants` 定位补录内全部副本及当前生效版本。
- 与当前生效版本完全一致的纯回放返回 `200` 且 `status=NO_CHANGE`，链头不前进。
- 每次补录（含被阻断的）都留痕，可通过 `GET /api/runs/<id>/attempts` 回读。

`GET /api/runs/<id>/lineage` 返回该批次所属谱系的全部版本（深度、前序、受影响分组、复核状态）和当前链头。`GET /api/runs/<id>/records/<record>/versions` 返回单个原记录的版本序列、引入批次与源行号，用于单独回读旧版。历史批次的报告与证据字节保持不变。

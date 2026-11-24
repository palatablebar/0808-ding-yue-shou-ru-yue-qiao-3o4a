# 订阅收入月桥

订阅收入分析员可在工作台上传来源 CSV，对照规则判断差异，签发合格批次或记录暂缓意见。

按月初月末及月内事件拆分新增、扩张、收缩和流失。

## 启动

```sh
docker compose up --build -d --wait
```

打开 <http://localhost:18302>。页面预览带异常样例。选中阶段查看计算证据，选择可签发样例并执行批次，然后提交复核意见。

批次签发后收到迟到来源记录时，在批次复核区上传补录 CSV（可下载 `late.csv` 样例）：系统把补录挂到来源版本链，只重算受影响业务分组，其余分组沿用已签发结论；无依据或矛盾补录会被拒绝并定位全部参与记录，历史版本与证据可在版本链中单独回读且不可变。

## 核验

```sh
docker compose exec -T app pytest -q
```

[输入字段](docs/DATA-DICTIONARY.md) · [阶段接口](docs/HTTP.md) · [操作步骤](docs/OPERATIONS.md) · [规则与测试](docs/DEPTH.md)

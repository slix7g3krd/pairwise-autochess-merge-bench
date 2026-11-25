# pairwise-autochess-merge-bench

## 领域

游戏、图形与多媒体

## 主题

自走棋合成与备战席

## 说明

每玩家独立随机流、合成原子迁移、阵容快照与出售屏障。

初始状态下 `gameplay/autochess_bench.py` 中所有函数抛出 `NotImplementedError`，测试全部失败。
修复实现后运行：

```bash
python -m pytest -q
```

期望全部通过。

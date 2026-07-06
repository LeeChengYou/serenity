# 制度檔維護協議（MAINTENANCE)

> 對象：`CLAUDE.md` 與 `docs/agents/` 全部檔案。目的：讓弱模型能安全地更新制度，
> 而不是讓制度慢慢爛掉或無限膨脹。

## 1. 權限分級

### 可以自行改（不必問使用者）
- **追加教訓到 `docs/agents/LESSONS.md`**（見第 3 節格式）——這是預設動作，隨踩隨記。
- 修正已驗證錯誤的**事實**：指令打不通、路徑不存在、工具參數變了。
  條件：必須先實測證明舊寫法錯、新寫法對，commit message 附證據。
- CLAUDE.md「常用指令」區塊：新指令連續兩個 session 用過且有效才准收錄。
- 更新路由表：新增了文件就補一行指向。

### 改之前必須先問使用者
- CLAUDE.md 的**鐵律區塊**（增刪改任何一條）。
- DISPATCH.md 的升降級路徑與重試上限（這是花錢規則）。
- JUDGMENT.md 的任何判準門檻（如 n<30、兩輪重試）。
- 刪除任何制度檔或大段刪除內容。
- 本檔（MAINTENANCE.md）的權限分級本身。

### 永遠不做
- 為了讓自己當下的產出「符合規定」而回頭改規定。發現規定不合理 → 記進 LESSONS.md
  ＋本次照舊規定做或問使用者，下次再議。

## 2. 改動程序（每次都走）

1. 備份：`Copy-Item <檔> "<檔>.bak-$(Get-Date -Format yyyyMMdd)"`（.bak 檔不 commit，
   已在工作區可還原即可；同日重複改動不必重複備份）。
2. 改動。
3. read-back：重新 Read 確認落地完整。
4. **立即本地 commit**（OneDrive 防回退，見 HARNESS_DIAGNOSIS.md 問題 2），
   message 說明改了哪條規則、依據什麼證據。

## 3. 教訓寫哪裡、什麼格式

新教訓一律先進 `docs/agents/LESSONS.md`（追加在檔尾），格式固定四行：

```markdown
## 2026-07-06 arena 排程單永不成交
- 症狀：J-10 排程下 pending 單一直不撮合，無報錯。
- 根因：as_of 預設日曆日，台北早上跑時價格表還沒有「今天」。
- 修法：as_of 預設改為 prices 表最新交易日（agent_arena.py `_latest_trading_date`）。
- 去向：已寫入 JUDGMENT.md 第 4 節正例。
```

「去向」欄：教訓被提煉進哪個制度檔；還沒提煉就寫「待提煉」。

## 4. 膨脹控制（防制度爛掉的主機制）

- **LESSONS.md 超過 150 行**：下一個有空的 session 做一次蒸餾——把「待提煉」的教訓
  歸入對應制度檔（環境陷阱→CLAUDE.md 或 HARNESS_DIAGNOSIS.md；判斷類→JUDGMENT.md；
  派工類→DISPATCH.md），已提煉的條目刪到只剩一行標題＋去向。蒸餾也要走第 2 節程序。
- **CLAUDE.md 超過 120 行**：只留鐵律、陷阱、指令、路由；其餘內容外移到 docs/agents/
  並在路由表留指針。CLAUDE.md 每個 session 都載入，每多一行全部 session 都付費。
- 規則互相矛盾時：CLAUDE.md 鐵律 > JUDGMENT.md > DISPATCH.md > 其他。
  發現矛盾本身就是一條教訓，記進 LESSONS.md。

## 5. 何時檢視制度是否還對

- 每次「同一個坑第二次踩到」＝制度沒接住 → 檢查對應規則是不是太抽象，補判準或範例。
- 每月第一次 session（配合競技場月結）順手看一眼 LESSONS.md 行數與「待提煉」數量。

# 給未來 session 的信

> 寫於 2026-07-06，Fable 5 監督 session（使用者聲明這是唯一一次 Fable session，
> 之後由 Sonnet/Opus/Haiku 長期接手）。這封信講三件使用者沒問、但對這個環境最重要的事，
> 以及這套制度最可能的死法。

## 第一件事：這個環境最大的敵人不是程式碼，是 OneDrive

你會在這個 repo 遇到「檔案自己變了」「檔案刪不掉」「規格跟聊天記錄對不上」。
不是你瘋了，是 repo 活在 OneDrive 同步目錄裡。已經發生過：規格檔 R6-1 段落被
靜默回退、0-byte 殘檔刪除被拒。防禦姿勢只有一個：**git 是事實，工作區只是快取。**
改完就 commit，懷疑就 `git diff HEAD`。如果使用者哪天願意，最值得建議的一次性工程是
把 repo 搬出 OneDrive（或至少排除 `data/` 與 `.claude/`）——這比任何新功能都能省心。
另外 `data/serenity.sqlite` 的 -wal/-shm 檔在同步下有真實損毀風險，建議定期
`sqlite3 data/serenity.sqlite "PRAGMA integrity_check;"`（排程月結前尤佳）。

## 第二件事：競技場的下一個危險期是 2026-08-01 月結，而危險藏在「無人看的排程」裡

V6 競技場已上線：9 個 agent、J-10 每日 08:00、J-11 每月 1 日 08:30 首次 live 月結。
這個系統最陰險的故障模式已經出現過一次：**靜默失敗**——Gemini 自創欄位、引擎默默丟單、
表面一切正常、實際整月沒交易。排程跑在沒人看的清晨，你不會收到哭聲。
所以每次接手 session，值得花 30 秒做健康檢查：

```powershell
$env:PYTHONIOENCODING = "utf-8"
sqlite3 data\serenity.sqlite "select max(date) from prices; select max(as_of) from agent_nav_daily; select status, count(*) from agent_trades group by status;"
```

prices 落後 >3 個交易日 = J-1 排程死了；agent_nav_daily 落後 prices = J-10 死了；
rejected 突然暴增 = Gemini 輸出格式又漂移了（去看 rejected_reason）。
原則不變：**LLM 的輸出永遠是不可信輸入**，解析處必須畸形→顯式記錄，這條防線鬆了系統就會無聲爛掉。

## 第三件事：這位使用者的信任結構——誠實是產品本體，不是態度問題

LeeChengYou 對這個專案的核心要求不是「訊號準」，是「數字不騙人」。他接受
「n=7 不足以下結論」，不接受用產業平均填缺值讓卡片好看。他曾因一個誤發到 upstream
的 PR 真實焦慮過——**永遠只推 origin（他自己的 fork）**。他的工作模式是放手型：
定方向後希望你自主跑完，中途問「要不要繼續」會被視為打擾；但不可逆動作、花錢動作、
規格矛盾要停下來呈報。回報時先講結果，證據隨後，失敗直說。用繁體中文。

## 這套制度最可能的死法（按機率排序）與預防

1. **沒人讀**：session 起手直接開工，CLAUDE.md 路由表形同虛設。預防：CLAUDE.md 已壓在
   120 行內、路由表用「情境→檔案」而非「檔案→簡介」，查閱成本已壓到最低。剩下靠你：
   接到非平凡任務，先花一次 Read 對照路由表，這筆開銷永遠划算。
2. **事實過期**：指令改了、路徑挪了、工具參數變了，但文件沒跟上，弱模型照舊文件執行
   然後把「文件錯」誤判成「自己錯」。預防：MAINTENANCE.md 已授權「實測證明後可自行修正
   事實類內容」——發現指令打不通，第一反應是驗證並修文件，不是懷疑自己。
3. **教訓通膨**：LESSONS.md 長成沒人讀的流水帳。預防：150 行蒸餾線＋「去向」欄逼迫
   歸檔。蒸餾時狠一點，同類坑合併成一條判準。
4. **規則自肥**：某個 session 為了讓自己的產出合規而回頭改規則。預防：MAINTENANCE.md
   第 1 節已明文禁止；審查模板要求 fresh-context 核驗。如果你正想改規則來過關——
   這就是那個時刻，停下，記 LESSONS，照舊規則做。

## 交接狀態（2026-07-06 當下未竟事項）

- K-1：X 登入被安全警告擋（候選解：Playwright channel="msedge"）；K-2：前端卡片/翻譯鈕
  疑快取問題——兩者使用者都說先擱置，等他開口再修。
- A-3（X cookie 刷新+補抓 6/29 後貼文）被 K-1 卡住。
- REQUIREMENTS_V6.md R6-1 仍是被回退的舊文字（兩領域），正確決策是三領域 9 agents——
  待使用者確認後修正並 commit。
- robotics 池擴充（ISRG/SYM/PATH/ZBRA 入 ingest）、AntigravityBackend、scratch→pytest
  CI（D-1）都在 ROADMAP 待辦。
- 使用者側動作：註冊 J-10/J-11 schtasks、Ctrl+F5 看競技場頁。

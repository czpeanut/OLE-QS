# 模型作答結果（尚未寫回題庫）

`generate_answers.py` 對整個題庫跑出來的作答，**還沒有 merge 進 `data/bank/`**。
放在版控裡是因為這是花錢跑出來的東西，暫存目錄會被回收，重跑一次要再花一次。

## 這批是怎麼跑的

```bash
python scripts/generate_answers.py data/bank --figures data/assets \
    --providers gemini -o data/model-answers --workers 8
```

模型 `gemini-3.7-flash`，5771 題（已有答案的 295 題與品管閘門會剔除的題目都不送）。

| 狀態 | 題數 | 意義 |
|---|---|---|
| `single_source` | 5041 | 有作答，但只有一個模型，無法交叉驗證 |
| `missing_context` | 724 | 模型回報缺少看不到的素材 —— **93% 是完全沒送圖的題目** |
| `failed` | 6 | 4 題暫時性網路錯誤、2 題 JSON 逸出（後者已修） |

## 準確率：81%（下限）

拿 295 題答案卷確認過的題目當校準集實測：答對 204、答錯 47、缺素材 44，
**204/251 = 81%**。

說「下限」是因為抽查那些「答錯」的案例時，有一部分其實是**正解本身壞掉**
（答案卷解析抄到題幹文字），或只是格式不同（正解把兩個小題黏成 `(1)2(2)7`，
模型分成兩個答案）。日誌只印出前 12 個錯誤案例，其中 5 題是模型真的答錯、
3 題是正解或格式問題、4 題是缺素材 —— 這個比例沒有推及全部 47 題的證據。

## 為什麼還沒 merge

單一模型的問題不在正確率是 81%，而在**你不知道是哪 19%**。實測模型的
信心值中位數是 1.00，信心低於 0.7 的只有 10% —— 遠低於實際錯誤率，
拿它當篩選條件沒有用。

補上 `ANTHROPIC_API_KEY` 再跑一次，兩個不同家族的模型交叉比對後，
不一致的才需要人看；那才是這個腳本原本的設計。

## 要 merge 的話

```bash
python scripts/generate_answers.py data/bank --figures data/assets \
    --providers gemini --merge
```

會重跑一次（會再花一次錢）。答案寫成 `answer_status: ai_generated`，
永遠不會是 `verified`；`missing_context` 與 `failed` 的題目不寫入。
教師解答卷會把這些答案標成「⚠ AI 作答，未經確認」。

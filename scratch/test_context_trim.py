import json

def trim_chat_history(messages, max_turns=6):
    # 保留最前面的系統初始提示（如果有的話，但通常系統指令是用 systemInstruction 發送）
    # 這裡模擬對訊息陣列進行滑動窗口截斷，只保留最近的 max_turns 條訊息
    if len(messages) <= max_turns:
        return messages, False
        
    trimmed = messages[-max_turns:]
    return trimmed, True

def run_test():
    print("=== 測試 4: 對話歷史上下文滑動窗口截斷測試 ===")
    
    # 模擬長對話歷史
    mock_history = [
        {"role": "user", "content": "你好，我想研究機器人。"},
        {"role": "model", "content": "好的，機器人的卡點主要在於減速器與馬達。"},
        {"role": "user", "content": "那諧波減速器龍頭是誰？"},
        {"role": "model", "content": "是綠的諧波和哈默納科。"},
        {"role": "user", "content": "他們的毛利率如何？"},
        {"role": "model", "content": "綠的諧波毛利率約為 40% 左右。"},
        {"role": "user", "content": "那半導體材料呢？"},
        {"role": "model", "content": "半導體材料包含光阻劑、矽晶圓與靶材。"},
        {"role": "user", "content": "光阻劑龍頭是誰？"} # 第 9 條訊息
    ]
    
    print(f"原始對話輪數: {len(mock_history)}")
    
    max_turns = 4
    trimmed_history, did_trim = trim_chat_history(mock_history, max_turns=max_turns)
    
    print(f"截斷後對話輪數: {len(trimmed_history)} (是否執行截斷: {did_trim})")
    print("保留的對話歷史片段:")
    for idx, msg in enumerate(trimmed_history, 1):
        print(f"  {idx}. [{msg['role']}]: {msg['content']}")
        
    assert len(trimmed_history) == max_turns
    assert trimmed_history[-1]["content"] == "光阻劑龍頭是誰？"
    
    print("[SUCCESS] 對話歷史滑動截斷算法測試成功！")

if __name__ == "__main__":
    run_test()

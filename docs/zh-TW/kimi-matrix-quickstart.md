# Kimi + Matrix 快速上手（繁體中文）

這份文件給想用 **Kimi CLI** 搭配 **Matrix** 的使用者：  
你可以只跑 Matrix，也可以同時跑 Telegram + Matrix。

---

## 1) 先決條件

1. Python 3.11+
2. 已安裝 `ductor`
3. 已安裝並可執行 `kimi` 指令
4. 環境變數已設定 `KIMI_API_KEY`
5. Matrix 機器人帳號（homeserver、user_id、password）

建議先檢查：

```bash
kimi --version
echo "$KIMI_API_KEY"
```

若 `KIMI_API_KEY` 沒值，請先設定（依你的 shell/部署方式）：

```bash
export KIMI_API_KEY="your_key_here"
```

---

## 2) 安裝 Matrix 支援

```bash
ductor install matrix
```

或用 pip 額外安裝：

```bash
pip install "ductor[matrix]"
```

---

## 3) 設定成 Matrix only（推薦先這樣）

編輯 `~/.ductor/config/config.json`：

```json
{
  "transport": "matrix",
  "provider": "kimi",
  "model": "kimi-for-coding",
  "matrix": {
    "homeserver": "https://matrix-client.matrix.org",
    "user_id": "@my_ductor_bot:matrix.org",
    "password": "YOUR_MATRIX_PASSWORD",
    "allowed_rooms": [],
    "allowed_users": ["@you:matrix.org"],
    "store_path": "matrix_store"
  }
}
```

說明：
- `provider: "kimi"`：預設走 Kimi CLI
- `model: "kimi-for-coding"`：Kimi 預設程式碼模型
- `allowed_users`：建議先鎖定你自己的 Matrix 帳號

---

## 4) 同時跑 Telegram + Matrix（可選）

若你想雙通道並行：

```json
{
  "transports": ["telegram", "matrix"],
  "provider": "kimi",
  "model": "kimi-for-coding",
  "telegram_token": "YOUR_TELEGRAM_TOKEN",
  "allowed_user_ids": [123456789],
  "matrix": {
    "homeserver": "https://matrix-client.matrix.org",
    "user_id": "@my_ductor_bot:matrix.org",
    "password": "YOUR_MATRIX_PASSWORD",
    "allowed_rooms": [],
    "allowed_users": ["@you:matrix.org"]
  }
}
```

---

## 5) 啟動

```bash
ductor
```

第一次 Matrix 登入成功後，憑證會保存到：

```text
~/.ductor/matrix_store/credentials.json
```

之後通常不需要再靠 password 登入（改用 token）。

---

## 6) 在聊天裡確認 Kimi 已生效

1. 使用 `/status` 檢查 provider/model
2. 使用 `/model` 切換 provider 到 Kimi（若有多 provider）
3. 送出測試訊息，例如：「請用 3 點總結目前專案架構」

---

## 7) 常見問題（Kimi + Matrix）

### Q1: 顯示找不到 `kimi` 指令
- 確認 `kimi` 已安裝且在 PATH。
- 在同一個執行環境（service/container）內執行 `kimi --version`。

### Q2: Kimi 看起來已安裝但仍無法使用
- 最常見是 `KIMI_API_KEY` 沒有傳進 bot 的執行環境。
- 如果你用 systemd/launchd/Task Scheduler，記得把環境變數加到服務設定中。

### Q3: Matrix 沒收到訊息
- 先檢查 `allowed_rooms`/`allowed_users`。
- 若設了 `group_mention_only=true`，群組房間需 @mention 或 reply 才會觸發。
- 確認 homeserver URL 是完整 `https://...`。

### Q4: Matrix token 失效
- 刪除 `~/.ductor/matrix_store/credentials.json` 後重啟，讓它重新用 password 登入。

---

## 8) 進階建議（給 Kimi 使用者）

- 用 `/model` 在不同房間/主題切換模型；每個 session context 是隔離的。
- 若你長期只用 Kimi，可把 `provider` 固定為 `kimi`，減少切換成本。
- 可搭配 `cron` + Kimi 做定時任務（摘要、巡檢、報表整理）。

---

## 參考文件

- Matrix 安裝（英文）：`docs/matrix-setup.md`
- 安裝總覽（英文）：`docs/installation.md`
- 設定欄位（英文）：`docs/config.md`

